"""
PCM audio sink — plays raw int16 mono PCM out the default output device.

Used by the brain to play the chit-chat reply audio that Gemini Live
streams back when ``LELAMP_BRAIN_TTS=native``. Gemini emits 24 kHz;
backends resample on the fly.

Two backends, tried in order:

  1. **aplay subprocess** — preferred on the Pi. aplay respects ALSA
     dmix/plug, which lets it cohabit with TTSService *if* the system
     ALSA config exposes a shared device. Override the device via
     ``LELAMP_BRAIN_OUTPUT_ALSA`` (defaults to ``LELAMP_AUDIO_OUTPUT_ALSA``
     then ``default``).

  2. **sounddevice.OutputStream** — fallback for hosts without an aplay
     binary (Mac dev box). PortAudio's CoreAudio host API can usually
     open the default output even when something else has it too.

If the chosen device is held exclusively by TTSService (typical Pi
setup where PortAudio reserves ``plughw:CARD=...``), both backends
will fail and the brain falls back to STT mode. Configure dmix in
``/etc/asound.conf`` or pick a shared device via the env override to
enable native audio on those boxes — TTSService internals are off-
limits to this module on purpose.

The sink has an internal queue + writer thread so ``push()`` never
blocks the brain receive loop. Echo gating is the caller's
responsibility — VoiceService consults ``speaking`` and silences the
mic while audio is playing.
"""

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger("lelamp.brain.audio")

DEFAULT_INPUT_RATE = 24000   # Gemini Live audio output rate (int16 LE mono)
DEFAULT_CHANNELS = 1
WRITER_TIMEOUT_S = 0.5       # max time the writer waits on the queue before checking _running

# Echo gate window — how long after the last PCM chunk we still report
# `speaking=True`. Must comfortably outlast the ALSA output buffer (often
# 150-300 ms on the Pi) AND any inter-chunk gap from Gemini Live streaming,
# otherwise the mic catches our own reply tail and feeds it back to the
# brain as a fake user turn. 2 s is conservative but cheap.
SPEAKING_DECAY_S = float(os.environ.get("LELAMP_BRAIN_SINK_DECAY_S", "2.0"))

# Default ALSA device used by aplay subprocess — `plug:` wraps the named
# device with rate/format conversion AND respects dmix, so we can share
# the output card with TTSService. Override via LELAMP_BRAIN_OUTPUT_ALSA.
DEFAULT_ALSA_OUTPUT = os.environ.get(
    "LELAMP_BRAIN_OUTPUT_ALSA",
    os.environ.get("LELAMP_AUDIO_OUTPUT_ALSA", "default"),
)


class _AplayWriter:
    """aplay subprocess that takes raw int16 LE mono PCM on stdin.

    aplay opens ALSA via the standard config (respects dmix/plug), which
    is how we cohabit with TTSService — PortAudio grabs an exclusive
    handle on the same card, aplay just streams through the shared mixer.
    """

    def __init__(self, alsa_device: str, rate: int, channels: int):
        cmd = [
            "aplay", "-q", "-D", alsa_device,
            "-t", "raw", "-f", "S16_LE",
            "-r", str(rate), "-c", str(channels),
        ]
        # bufsize=0 → unbuffered stdin so audio reaches the card right
        # after write(); stderr=PIPE so we can surface ALSA errors instead
        # of dumping them on the service journal.
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self._cmd = " ".join(cmd)
        # Tiny wait to surface immediate failures (bad device name etc.).
        time.sleep(0.05)
        if self._proc.poll() is not None:
            err = (self._proc.stderr.read() if self._proc.stderr else b"").decode("utf-8", "replace")
            raise OSError(f"aplay exited rc={self._proc.returncode}: {err.strip()}")

    def write(self, data: bytes) -> None:
        if self._proc.stdin is None or self._proc.poll() is not None:
            raise BrokenPipeError("aplay process is gone")
        self._proc.stdin.write(data)

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class _SDWriter:
    """sounddevice OutputStream wrapper used only as the Mac/dev fallback
    — on the Pi aplay is preferred so we don't fight PortAudio."""

    def __init__(self, sd, np, rate: int, channels: int, device):
        self._np = np
        self._channels = channels
        self._stream = sd.OutputStream(
            samplerate=rate, channels=channels, dtype="int16", device=device,
        )
        self._stream.start()

    def write(self, data: bytes) -> None:
        samples = self._np.frombuffer(data, dtype=self._np.int16)
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels)
        self._stream.write(samples)

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            logger.debug("brain audio sink close: %s", e)


class PCMAudioSink:
    """Thread-safe PCM player. push() is non-blocking; a background writer
    thread feeds whichever backend (aplay or sounddevice) opened."""

    def __init__(
        self,
        sample_rate: int = DEFAULT_INPUT_RATE,
        channels: int = DEFAULT_CHANNELS,
        output_device: Optional[int] = None,
        alsa_device: Optional[str] = None,
    ):
        self._sample_rate = sample_rate
        self._channels = channels
        self._output_device = output_device
        self._alsa_device = alsa_device or DEFAULT_ALSA_OUTPUT
        self._queue: queue.Queue[Optional[bytes]] = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._writer = None  # _AplayWriter | _SDWriter
        self._sd = None
        self._np = None
        self._last_push_time: float = 0.0

        try:
            import sounddevice as sd
            self._sd = sd
        except ImportError:
            logger.info("sounddevice not available — brain audio sink will need aplay")

        try:
            import numpy as np
            self._np = np
        except ImportError:
            logger.warning("numpy not available — brain audio sink disabled")

    @property
    def available(self) -> bool:
        # Numpy is the only hard requirement; either backend (aplay or
        # sounddevice) being usable is enough.
        if self._np is None:
            return False
        return shutil.which("aplay") is not None or self._sd is not None

    @property
    def speaking(self) -> bool:
        """True while we're still draining audio out the speaker — covers
        both pending queue items AND the ALSA buffer that keeps playing
        after the last push() call. VoiceService consults this to keep
        the mic muted long enough that our own reply tail doesn't loop
        back into Gemini Live as a fake user turn."""
        if not self._queue.empty():
            return True
        return (time.time() - self._last_push_time) < SPEAKING_DECAY_S

    def start(self) -> bool:
        if not self.available:
            return False
        if self._running:
            return True

        self._writer = self._open_backend()
        if self._writer is None:
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="brain-audio-out"
        )
        self._thread.start()
        return True

    def _open_backend(self):
        """aplay first (shared-friendly via dmix), sounddevice as fallback."""
        if shutil.which("aplay") is not None:
            try:
                w = _AplayWriter(self._alsa_device, self._sample_rate, self._channels)
                logger.info(
                    "brain audio sink: aplay backend (device=%s, %dHz mono int16)",
                    self._alsa_device, self._sample_rate,
                )
                return w
            except Exception as e:
                logger.info(
                    "brain audio sink: aplay failed (%s) — falling back to sounddevice", e,
                )
        if self._sd is not None:
            try:
                w = _SDWriter(self._sd, self._np, self._sample_rate, self._channels, self._output_device)
                logger.info(
                    "brain audio sink: sounddevice backend (%dHz mono int16)",
                    self._sample_rate,
                )
                return w
            except Exception as e:
                logger.warning("brain audio sink: sounddevice also failed: %s", e)
        return None

    def push(self, pcm_bytes: bytes) -> None:
        """Enqueue a PCM chunk for playback. No-op if sink isn't running."""
        if not self._running or not pcm_bytes:
            return
        self._last_push_time = time.time()
        self._queue.put(pcm_bytes)

    def flush(self) -> None:
        """Drop all pending audio (e.g. on user interrupt)."""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(None)  # poison pill
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception as e:
                logger.debug("brain audio sink stop: %s", e)
            self._writer = None
        logger.info("brain audio sink stopped")

    def _writer_loop(self) -> None:
        assert self._writer is not None
        while self._running:
            try:
                chunk = self._queue.get(timeout=WRITER_TIMEOUT_S)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                self._writer.write(chunk)
            except Exception as e:
                logger.warning("brain audio sink write failed: %s", e)
                # Writer is dead; signal stop so start() can be re-tried later.
                break
