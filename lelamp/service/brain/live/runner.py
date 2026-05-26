"""
LiveBrainRunner — drives the realtime brain loop on its own thread.

In live mode we bypass the classic VoiceService VAD pipeline (RMS gate
+ Silero + Deepgram STT + SILENCE_TIMEOUT) entirely: the upstream
provider (Gemini Live, OpenAI Realtime) handles end-of-turn detection
server-side. The runner just:

  1. Opens the mic the same way VoiceService would (arecord plughw
     subprocess on the Pi, sounddevice fallback elsewhere).
  2. Streams every frame into a single BrainSession.
  3. Buffers the reply transcript ``on_text(...)`` into sentences and
     pushes each sentence into ``TTSService.speak_queue`` — same
     ElevenLabs voice as the call-mode path, so the voice the user
     hears doesn't change when we toggle modes.
  4. On ``on_delegate(transcript)``, forwards to Lumi the same way the
     classic STT path does (POST /api/sensing/event) so OpenClaw's
     turn pipeline is identical regardless of which brain mode picked
     the delegate.

Echo gate: when the TTSService reports it's currently speaking we drop
the incoming mic frame instead of pushing it to the provider. This is
the live-mode equivalent of the call-mode mic pause-while-TTS — keeps
Gemini Live from hearing its own ElevenLabs output and treating it as
a new user turn.

The runner owns the session lifecycle: when a session closes (delegate
fired, provider GoAway, network blip), it opens a fresh one and keeps
going until ``stop()`` is called.
"""

import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

import requests

from lelamp.service.brain.live.base import Brain

logger = logging.getLogger("lelamp.brain.live.runner")

# Same sentence boundary regex the call-mode brain uses. Kept duplicated
# (not imported from call/) so the live runner has no dependency on the
# call subpackage — they can evolve independently.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")


def _drain_complete_sentences(buffer: str, on_sentence: Callable[[str], None]) -> str:
    """Pop any complete sentences from the head of ``buffer`` into
    ``on_sentence``; return the unconsumed tail."""
    last = 0
    for m in _SENTENCE_BOUNDARY.finditer(buffer):
        sent = buffer[last:m.end()].strip()
        if sent:
            on_sentence(sent)
        last = m.end()
    return buffer[last:]


# Mic constants — kept in sync with voice_service.py. Re-imported here
# instead of from voice_service to avoid a circular dep when
# voice_service imports the runner.
_STT_RATE = 16000
_CHANNELS = 1
_FRAME_DURATION_MS = 64
_LUMI_SENSING_URL = "http://127.0.0.1:5000/api/sensing/event"


class _ArecordStream:
    """Subprocess wrapper around ALSA arecord — same shape as the one
    in voice_service.py. Duplicated for the same reason as the
    sentence regex: keep brain/live/ self-contained."""

    def __init__(self, alsa_device: str, rate: int, channels: int, blocksize: int, np):
        self._device = alsa_device
        self._rate = rate
        self._channels = channels
        self._blocksize = blocksize
        self._np = np
        self._proc: Optional[subprocess.Popen] = None
        self._bytes_per_frame = 2 * channels

    def __enter__(self):
        self._proc = subprocess.Popen(
            ["arecord", "-D", self._device, "-f", "S16_LE",
             "-r", str(self._rate), "-c", str(self._channels),
             "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *args):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
            self._proc = None

    def read(self, frames: int):
        n_bytes = frames * self._bytes_per_frame
        raw = self._proc.stdout.read(n_bytes)
        if not raw:
            raise IOError("arecord process exited (stdout EOF)")
        if len(raw) < n_bytes:
            raw = raw + b"\x00" * (n_bytes - len(raw))
        return self._np.frombuffer(raw, dtype=self._np.int16).reshape(frames, self._channels), False


class LiveBrainRunner:
    """Owns the mic + a long-running BrainSession; pushes reply
    sentences into TTSService.speak_queue and delegate transcripts
    into Lumi's sensing endpoint."""

    def __init__(
        self,
        brain: Brain,
        tts_service,
        alsa_device: Optional[str] = None,
        input_device: Optional[int] = None,
    ):
        self._brain = brain
        self._tts = tts_service
        self._alsa_device = alsa_device
        self._input_device = input_device
        self._np = None
        self._sd = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session = None
        self._session_lock = threading.Lock()

        # Per-session text buffer. Reset every time a fresh session
        # starts and every time turn_complete fires.
        self._reply_buf = ""
        self._reply_lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            import numpy as np
            import sounddevice as sd  # noqa: F401 — only used when alsa_device is None
            self._np = np
            self._sd = sd
        except ImportError as e:
            logger.warning("LiveBrainRunner: missing numpy/sounddevice — %s", e)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._main_loop, name="live-brain-runner", daemon=True,
        )
        self._thread.start()
        logger.info("LiveBrainRunner started (alsa=%s, device=%s)",
                    self._alsa_device, self._input_device)

    def stop(self) -> None:
        self._stop_event.set()
        with self._session_lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # --- main loop ---------------------------------------------------------

    def _main_loop(self) -> None:
        """Outer loop — keep spawning sessions until stop()."""
        frame_size = int(_STT_RATE * _FRAME_DURATION_MS / 1000)
        while not self._stop_event.is_set():
            try:
                self._run_one_session(frame_size)
            except Exception as e:
                logger.warning("Live brain session crashed: %s — restarting in 2s", e)
                if not self._stop_event.wait(2.0):
                    continue

    def _run_one_session(self, frame_size: int) -> None:
        """Open mic, open a BrainSession, push frames until the session
        closes itself (delegate, GoAway, error). The outer loop will
        then open a fresh one."""
        if self._alsa_device is not None:
            mic_ctx = _ArecordStream(
                alsa_device=self._alsa_device, rate=_STT_RATE,
                channels=_CHANNELS, blocksize=frame_size, np=self._np,
            )
        else:
            mic_ctx = self._sd.InputStream(
                samplerate=_STT_RATE, channels=_CHANNELS, dtype="int16",
                blocksize=frame_size, device=self._input_device,
            )

        session = self._brain.create_session()
        ok = session.start(
            on_delegate=self._on_delegate,
            on_audio_chunk=lambda _: None,  # text-out via on_text; drop audio
            on_text=self._on_text,
            on_user_input=lambda *_: None,
            on_error=self._on_error,
        )
        if not ok:
            logger.warning("Live brain session.start() returned False — sleeping 2s")
            self._stop_event.wait(2.0)
            return

        with self._session_lock:
            self._session = session
        with self._reply_lock:
            self._reply_buf = ""

        try:
            with mic_ctx as mic:
                while not self._stop_event.is_set() and not session.is_closed():
                    try:
                        data, _ = mic.read(frame_size)
                    except IOError as e:
                        logger.info("Live mic EOF: %s", e)
                        break
                    # Echo gate: drop frames while TTSService is speaking
                    # so Gemini Live doesn't pick up its own ElevenLabs
                    # output as a new user turn.
                    if self._tts_is_speaking():
                        continue
                    session.send_audio(data.tobytes())
        finally:
            with self._session_lock:
                self._session = None
            try:
                session.close()
            except Exception:
                pass

    # --- callbacks ---------------------------------------------------------

    def _on_text(self, text: str, is_final: bool) -> None:
        """Reply transcript from the brain. Buffer + sentence-split +
        push each complete sentence into TTSService.speak_queue. ``is_final``
        marks turn_complete — flush whatever's left as a single chunk."""
        if not text and not is_final:
            return
        with self._reply_lock:
            if text:
                self._reply_buf += text
            if is_final:
                # Flush any trailing partial sentence + reset for next turn.
                tail = self._reply_buf.strip()
                self._reply_buf = ""
            else:
                tail = None
            # Drain any complete sentences sitting at the head.
            self._reply_buf = _drain_complete_sentences(
                self._reply_buf, self._speak_sentence,
            )
        if is_final and tail:
            # Tail isn't a complete sentence by punctuation, but turn ended
            # — speak it anyway so the user hears the last few words.
            self._speak_sentence(tail)

    def _speak_sentence(self, sentence: str) -> None:
        """Push one sentence into TTSService. Uses speak_queue when
        available so consecutive sentences play gapless."""
        if not sentence or self._tts is None:
            return
        try:
            if hasattr(self._tts, "speak_queue"):
                self._tts.speak_queue(sentence)
            else:
                self._tts.speak(sentence)
        except Exception as e:
            logger.warning("LiveBrainRunner TTS push failed: %s", e)

    def _on_delegate(self, transcript: str) -> None:
        """Brain decided this turn is a task for OpenClaw. Forward the
        transcript to Lumi's sensing endpoint exactly the way the
        classic STT path forwards it, so OpenClaw sees the same shape
        regardless of which brain mode is active."""
        transcript = (transcript or "").strip()
        if not transcript:
            return
        logger.info("brain.delegate [live] → Lumi: %r", transcript)
        try:
            requests.post(
                _LUMI_SENSING_URL,
                json={"type": "voice", "message": transcript},
                timeout=2.0,
            )
        except Exception as e:
            logger.warning("Live delegate POST failed: %s", e)

    def _on_error(self, err: Exception) -> None:
        logger.warning("Live brain session error: %s", err)

    # --- helpers -----------------------------------------------------------

    def _tts_is_speaking(self) -> bool:
        if self._tts is None:
            return False
        try:
            if hasattr(self._tts, "speaking"):
                return bool(self._tts.speaking)
        except Exception:
            pass
        return False
