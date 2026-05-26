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
from collections import deque
from typing import Any, Callable, Optional

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

# Extra mute window AFTER TTSService.speaking flips False. Real
# speakers + room reverb can leak a few hundred ms of late audio that
# Gemini's server VAD will treat as a new user turn, kicking off a
# self-conversation loop. Tunable via env so we can dial it for the
# room. Defaulted to 600ms — long enough for ElevenLabs 24 kHz PCM
# tail-out + typical living-room reverb, short enough that the user
# can interrupt naturally.
_POST_TTS_HOLDOFF_S = float(os.environ.get("LELAMP_LIVE_POST_TTS_HOLDOFF_S", "0.6"))


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
        decorate_callback: Optional[Callable[[str, list], tuple]] = None,
        send_to_lumi_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self._brain = brain
        self._tts = tts_service
        self._alsa_device = alsa_device
        self._input_device = input_device
        self._np = None
        self._sd = None

        # Hooks back into VoiceService for delegate formatting.
        # ``decorate_callback(transcript, audio_buffer) -> (decorated, name)``
        #   wraps VoiceService._identify_and_decorate. Runs the speaker
        #   recognizer on the buffered mic audio and prefixes the
        #   transcript with the speaker label so OpenClaw sees the
        #   same shape it gets from the call-mode path.
        # ``send_to_lumi_callback(message, event_type)`` wraps
        #   VoiceService._send_to_lumi. Reuses the same retry + echo-
        #   filter logic call mode uses so live and call deliver to
        #   the sensing endpoint identically.
        # Both default to None — if the caller doesn't wire them, the
        # runner falls back to a no-prefix raw POST so live mode still
        # functions (degraded format).
        self._decorate_callback = decorate_callback
        self._send_to_lumi_callback = send_to_lumi_callback

        # Rolling mic-frame buffer for speaker recognition. ~30 s
        # window matches the call-mode loop (see voice_service.py
        # AUDIO_BUF_MAX). Frames go in regardless of TTS echo gate
        # so the recognizer has the user's actual voice when delegate
        # fires, not silence.
        _audio_buf_max = max(1, int(30_000 / _FRAME_DURATION_MS))
        self._audio_buf: deque = deque(maxlen=_audio_buf_max)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session = None
        self._session_lock = threading.Lock()

        # Tracks the moment TTSService.speaking flipped from True → False
        # so we can keep muting the mic for ``_POST_TTS_HOLDOFF_S`` to
        # ride out late reverb / speaker tail-out. None = currently
        # speaking OR holdoff has already lapsed.
        self._tts_stopped_at: Optional[float] = None
        self._tts_was_speaking = False

        # Per-session text buffer for the brain's reply (used to drain
        # complete sentences into TTS as they arrive). Reset every time
        # a fresh session starts and every time turn_complete fires.
        self._reply_buf = ""
        self._reply_lock = threading.Lock()

        # Mirror of the call-mode per-turn logs so the journal shows
        # the same `brain.input` / `brain.chitchat` shape regardless
        # of which brain mode is active. Accumulated per turn from
        # the provider's incremental on_user_input / on_text callbacks
        # and flushed (logged) on turn_complete.
        self._user_input_buf = ""
        self._reply_log_buf = ""

        # Snapshot of the user-input transcript at the moment its
        # is_final event fired this turn. Stays set across the rest of
        # the turn so the [DELEGATE] text-marker fallback in _on_text
        # can synthesise an on_delegate call after the buffer itself
        # is cleared. Cleared at the next on_user_input partial.
        self._last_user_input = ""

        # ``brain.input`` log latch — flipped True the first time we
        # log the user transcript for the current turn so we don't
        # double-log on turn_complete. Reset every new turn (first
        # on_user_input partial of the turn).
        # We log on the FIRST reply-text partial rather than waiting
        # for turn_complete because turn_complete only fires after
        # Gemini finishes generating its reply — which makes the
        # journal look like "Lumi replied with nothing for several
        # seconds, then both brain.input and brain.chitchat suddenly
        # appear together". Logging at first reply-token gives the
        # natural ordering: hear → think → reply.
        self._user_input_logged = False

        # ``brain.tts.start`` log latch — flipped True the first time
        # we hand a sentence to TTSService.speak_queue this turn. The
        # journal then shows when the speaker actually starts kêu
        # (TTFA proxy) instead of having to wait for turn_complete
        # to see the full reply text — useful for measuring streaming
        # win vs the non-streaming baseline.
        self._tts_start_logged = False

        # Per-turn delegate detection. Gemini Live should call the
        # ``delegate_to_lumi`` tool, but it occasionally emits the
        # literal ``[DELEGATE]`` text marker from the shared
        # DECISION_RULES instead. When that happens we route the
        # last user-input transcript through on_delegate ourselves
        # and suppress TTS playback so the user doesn't hear
        # "DELEGATE" spoken aloud. Reset every turn_complete.
        self._delegate_text_detected = False

        # Cursor for the OpenClaw history sync mechanism. Holds the
        # epoch-seconds timestamp of the most-recent OpenClaw turn the
        # brain has already pushed into the live session. On every
        # turn_complete we re-read OpenClaw JSONL, filter turns whose
        # ts > _last_synced_ts, and push them via
        # ``session.send_context_turns`` so the live brain stays
        # current with Telegram / web / other-voice traffic without
        # restarting the WS.
        # Initialised when the first session starts (to the latest
        # ts in the initial history snapshot) so we don't re-push
        # the turns we just baked into system_instruction.
        self._last_synced_ts: float = 0.0

        # Per-turn session restart flag. When True the inner mic loop
        # exits → session closes → outer loop opens a fresh one with
        # a fresh load_context(). Set every turn_complete because the
        # Developer-API ``gemini-3.1-flash-live-preview`` doesn't
        # support mid-session ``send_client_content`` for history
        # injection — restart is the only way to pick up OpenClaw
        # turns (Telegram, web, etc.) that landed between voice turns.
        # Cost: ~0.5-1s connect overhead per turn (happens during the
        # silence right after Lumi finishes replying, before user
        # speaks next — no impact on perceived response latency).
        self._restart_after_turn = False

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
            on_user_input=self._on_user_input,
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
            self._reply_log_buf = ""
            self._user_input_buf = ""
            self._last_user_input = ""
            self._delegate_text_detected = False
            self._restart_after_turn = False
            self._user_input_logged = False
            self._tts_start_logged = False
        # Re-arm echo gate state. Don't leak holdoff timing across
        # sessions — a stale ``_tts_stopped_at`` from the previous
        # session would otherwise keep muting the mic on a fresh open.
        self._tts_stopped_at = None
        self._tts_was_speaking = False
        # Snapshot the initial history cursor so the first post-turn
        # sync only pushes truly NEW OpenClaw turns. Pulls the same
        # turns that load_context() just baked into the system prompt.
        self._last_synced_ts = self._latest_openclaw_ts()

        try:
            with mic_ctx as mic:
                while not self._stop_event.is_set() and not session.is_closed():
                    if self._restart_after_turn:
                        # Turn just ended — bail out so the outer loop
                        # opens a fresh session with a fresh
                        # ``load_context()``. Picks up any OpenClaw
                        # turns (Telegram, web, other voice sessions)
                        # that landed while this turn was running.
                        # Connect cost ~0.5-1s lands during the
                        # natural silence after Lumi finishes
                        # replying, so the user doesn't perceive it.
                        logger.info(
                            "Live brain turn ended — restarting session for fresh history"
                        )
                        break
                    try:
                        data, _ = mic.read(frame_size)
                    except IOError as e:
                        logger.info("Live mic EOF: %s", e)
                        break
                    frame_bytes = data.tobytes()
                    # Always feed the rolling buffer so the speaker
                    # recognizer has fresh audio when delegate fires —
                    # even for frames the echo gate would otherwise
                    # drop from Gemini.
                    self._audio_buf.append(frame_bytes)
                    # Echo gate: drop frames going to Gemini while
                    # TTSService is speaking so Gemini Live doesn't
                    # pick up its own ElevenLabs output as a new
                    # user turn.
                    if self._tts_is_speaking():
                        continue
                    session.send_audio(frame_bytes)
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
        marks turn_complete — flush whatever's left as a single chunk
        and log the full reply line so the journal mirrors the call-mode
        ``brain.chitchat`` log shape.

        Also handles the ``[DELEGATE]`` text-marker fallback: when
        Gemini emits the literal token instead of calling the
        delegate tool, we suppress TTS for this turn and synthesise
        an on_delegate(user_input) call from the buffered mic
        transcription. The detection is sticky for the whole turn —
        once the prefix lands we never speak any of this reply, even
        if the model trails it with more text."""
        if not text and not is_final:
            return
        delegate_marker = "[DELEGATE]"
        tail: Optional[str] = None
        full_reply: Optional[str] = None
        delegate_transcript: Optional[str] = None
        user_input_to_log: Optional[str] = None

        with self._reply_lock:
            if text:
                self._reply_buf += text
                self._reply_log_buf += text
                # First reply token of this turn — log the user
                # transcript NOW (rather than waiting for
                # turn_complete) so the journal reads
                # "brain.input → … reply streams … → brain.chitchat"
                # instead of dumping both at the end together.
                if not self._user_input_logged:
                    snapshot = self._user_input_buf.strip()
                    if snapshot:
                        user_input_to_log = snapshot
                        self._user_input_logged = True

            # Sticky prefix detection. Only check while we haven't
            # already decided this turn is a delegate.
            if not self._delegate_text_detected:
                stripped = self._reply_buf.lstrip()
                if stripped:
                    if stripped[0] == "[":
                        if len(stripped) >= len(delegate_marker):
                            if stripped.upper().startswith(delegate_marker):
                                self._delegate_text_detected = True
                        # else: keep buffering until we have ~10 chars
                        # — could still be the marker or a voice tag
                        # like "[chuckle]" which is legitimate chit-chat.

            if is_final:
                full_reply = self._reply_log_buf.strip()
                tail = self._reply_buf.strip()
                if self._delegate_text_detected:
                    # _on_user_input fires its is_final BEFORE we run
                    # (per the gemini_live ordering swap), so the live
                    # accumulator is already empty. Read the snapshot
                    # written by _on_user_input instead.
                    delegate_transcript = self._last_user_input.strip()
                # Re-arm per-turn latches. Without this, the second
                # turn of a long-lived session would silently skip
                # the brain.input log because the flag carries over
                # from the previous turn.
                self._user_input_logged = False
                # Reset all per-turn buffers + the sticky flag.
                self._reply_buf = ""
                self._reply_log_buf = ""
                # NOTE: _user_input_buf is reset in _on_user_input when
                # its own is_final fires (which now arrives BEFORE this
                # block thanks to the swapped order in gemini_live.py
                # _handle_server_content). We don't clear it here so a
                # late-arriving partial doesn't go to the next turn.
                self._delegate_text_detected_was = self._delegate_text_detected
                self._delegate_text_detected = False
            else:
                # Only drain when we haven't latched delegate — never
                # leak audio for what's going to be a delegate turn.
                if not self._delegate_text_detected:
                    self._reply_buf = _drain_complete_sentences(
                        self._reply_buf, self._speak_sentence,
                    )

        if user_input_to_log:
            logger.info("brain.input  [live] %r", user_input_to_log)

        if not is_final:
            return

        # Final fire — either log chit-chat + flush tail to TTS, or
        # synthesise a delegate from the captured user-input buffer.
        if getattr(self, "_delegate_text_detected_was", False):
            if delegate_transcript:
                logger.info(
                    "brain.chitchat [live] '[DELEGATE] suppressed → delegate via %r'",
                    delegate_transcript,
                )
                self._on_delegate(delegate_transcript)
            else:
                logger.warning(
                    "live brain emitted [DELEGATE] but no user input "
                    "buffered — dropping turn"
                )
            self._delegate_text_detected_was = False
            # Force-close the session: once Gemini has the literal
            # ``[DELEGATE]`` token in its dialog history it tends to
            # go silent for every subsequent turn (response=0). We
            # don't restart-per-turn for the chit-chat path (memory
            # regression — see the chit-chat branch comment below),
            # but we DO restart specifically for the delegate path
            # because the alternative is "Lumi never replies again
            # until GoAway".
            logger.info(
                "Live brain: closing session after [DELEGATE] text "
                "to recover from Gemini's response=0 spiral"
            )
            with self._session_lock:
                if self._session is not None:
                    try:
                        self._session.close()
                    except Exception:
                        pass
            return

        if tail:
            self._speak_sentence(tail)
        if full_reply:
            logger.info("brain.chitchat [live] %r", full_reply)

        # No per-turn session restart — we accept stale history
        # within a Gemini session (~10-15 min until GoAway). Restart
        # was tried and dropped on 2026-05-26: it gave fresh
        # OpenClaw history every turn but Gemini lost its in-session
        # memory, so replies turned into generic "I can do X, Y, Z"
        # feature lists instead of following the conversation. Quality
        # regression > stale-history win. See live/README.md
        # §"Known gaps".

    def _on_user_input(self, text: str, is_final: bool) -> None:
        """User mic transcription from the live provider. Accumulates
        partials. The ``brain.input`` journal line is normally logged
        from :meth:`_on_text` on the first reply-token (so the natural
        order in the journal is input → reply, not "both at the end"),
        but THIS method handles the fallback case where Gemini decides
        the right answer is silence — no reply means _on_text never
        fires, so we log here on is_final.

        Side effect: when ``is_final`` fires we snapshot the
        transcript into ``self._last_user_input`` so the [DELEGATE]
        text-marker path in :meth:`_on_text` can still recover the
        user's words after the accumulator buffer itself is cleared."""
        if not text and not is_final:
            return
        # First partial of a new turn — wipe the previous turn's
        # snapshot so a delayed delegate marker can't grab stale text.
        if text and not is_final and not self._user_input_buf:
            with self._reply_lock:
                self._last_user_input = ""

        with self._reply_lock:
            if text:
                self._user_input_buf += text
            if is_final:
                final_text = self._user_input_buf.strip()
                self._user_input_buf = ""
                if final_text:
                    self._last_user_input = final_text
                fallback_log = final_text and not self._user_input_logged
                if fallback_log:
                    self._user_input_logged = True
            else:
                final_text = None
                fallback_log = False
        if fallback_log:
            logger.info("brain.input  [live] %r", final_text)

    def _speak_sentence(self, sentence: str) -> None:
        """Push one sentence into TTSService. Uses speak_queue when
        available so consecutive sentences play gapless. Logs
        ``brain.tts.start`` on the first sentence of the turn so the
        journal shows when the speaker actually starts speaking
        (loa bắt đầu kêu) — closer to perceived latency than the
        ``brain.chitchat`` log which fires later at turn_complete."""
        if not sentence or self._tts is None:
            return
        if not self._tts_start_logged:
            logger.info("brain.tts.start [live] %r", sentence)
            self._tts_start_logged = True
        try:
            if hasattr(self._tts, "speak_queue"):
                self._tts.speak_queue(sentence)
            else:
                self._tts.speak(sentence)
        except Exception as e:
            logger.warning("LiveBrainRunner TTS push failed: %s", e)

    def _on_delegate(self, transcript: str) -> None:
        """Brain decided this turn is a task for OpenClaw. Forward
        through VoiceService's send-to-Lumi pipeline so the message
        gets the same speaker prefix + echo filter + retry logic the
        call-mode path uses. Without this OpenClaw sees a raw
        transcript with no ``"<Name>: …"`` prefix and routes / logs
        the turn wrong."""
        transcript = (transcript or "").strip()
        if not transcript:
            return

        # Snapshot the rolling audio buffer for speaker recognition.
        # ``list(deque)`` is a one-pass copy of frame bytes; we don't
        # mutate the buffer, we just hand a frozen view to the
        # recognizer.
        audio_snapshot = list(self._audio_buf)

        # Decorate: run speaker recog → ``"<Name>: <transcript>"`` or
        # ``"Unknown Speaker:[voice:hash] <transcript> (audio…)"``
        # using VoiceService's existing identifier. Falls back to
        # ``"Unknown Speaker: <transcript>"`` so OpenClaw still gets
        # a parsable shape even when no decorator was injected.
        decorated = f"Unknown Speaker: {transcript}"
        if self._decorate_callback is not None:
            try:
                result = self._decorate_callback(transcript, audio_snapshot)
                if isinstance(result, tuple) and result:
                    decorated = result[0] or decorated
            except Exception as e:
                logger.warning("Live decorate_callback failed: %s", e)

        # Strip the ``(audio saved at …)`` + enrollment-hint suffix
        # that the call-mode formatter appends. Useful for OpenClaw's
        # offline enrollment job but pure noise for the live delegate
        # path — the audio frames are already in Gemini's
        # transcription, no need to re-cite a file path.
        decorated = re.sub(
            r"\s*\(audio (?:saved|save) at .*$",
            "",
            decorated,
            flags=re.DOTALL,
        ).rstrip()

        # Ensure the message always carries a speaker prefix. The
        # call-mode decorator skips the prefix entirely when audio
        # buffer is too short or the speaker server errored — for
        # delegate routing that's a regression (OpenClaw's text
        # handlers expect ``"<Name>: …"`` / ``"Unknown Speaker: …"``
        # at the head). Default to ``Unknown Speaker:`` so the wire
        # shape is always parseable.
        if not (
            decorated.startswith("Speaker - ")
            or decorated.startswith("Unknown Speaker")
        ):
            decorated = f"Unknown Speaker: {decorated}"

        logger.info("brain.delegate [live] → Lumi: %r", decorated)

        if self._send_to_lumi_callback is not None:
            try:
                self._send_to_lumi_callback(decorated, "voice")
                return
            except Exception as e:
                logger.warning(
                    "Live send_to_lumi_callback failed (%s) — falling back "
                    "to raw POST", e,
                )

        # Last-resort raw POST when the runner was instantiated
        # without callbacks.
        try:
            requests.post(
                _LUMI_SENSING_URL,
                json={"type": "voice", "message": decorated},
                timeout=2.0,
            )
        except Exception as e:
            logger.warning("Live delegate POST failed: %s", e)

    def _on_error(self, err: Exception) -> None:
        logger.warning("Live brain session error: %s", err)

    # --- OpenClaw history sync --------------------------------------------

    def _load_openclaw_turns(self) -> list[dict]:
        """Read the current OpenClaw JSONL tail. Returns a list of
        ``{"role", "text", "ts"}`` dicts ordered oldest → newest.
        Wraps context_loader internals so the runner doesn't have
        to know the OPENCLAW_AGENTS_DIR resolution rules."""
        try:
            from lelamp.service.brain.context_loader import (
                DEFAULT_AGENTS_SUBDIR, DEFAULT_HISTORY_LIMIT,
                DEFAULT_SESSION_KEY, DEFAULT_WORKSPACE,
                DEFAULT_WORKSPACE_SUBDIR, _read_openclaw_history,
            )
        except Exception:
            return []
        workspace_root = os.environ.get("OPENCLAW_WORKSPACE")
        if workspace_root:
            workspace_root = workspace_root.rstrip("/")
            if workspace_root.endswith("/" + DEFAULT_WORKSPACE_SUBDIR):
                workspace_root = workspace_root[: -len("/" + DEFAULT_WORKSPACE_SUBDIR)]
        else:
            workspace_root = DEFAULT_WORKSPACE
        agents_dir = os.environ.get(
            "OPENCLAW_AGENTS_DIR",
            f"{workspace_root}/{DEFAULT_AGENTS_SUBDIR}",
        )
        session_key = os.environ.get("OPENCLAW_SESSION_KEY") or DEFAULT_SESSION_KEY
        try:
            raw = _read_openclaw_history(agents_dir, session_key, DEFAULT_HISTORY_LIMIT)
        except Exception:
            return []
        out: list[dict] = []
        for turn in raw:
            text = (turn.text or "").strip()
            if not text:
                continue
            ts = self._to_epoch(turn.time)
            out.append({
                "role": "user" if turn.role == "user" else "model",
                "text": text,
                "ts": ts,
            })
        out.sort(key=lambda x: x["ts"])
        return out

    @staticmethod
    def _to_epoch(value) -> float:
        if value is None or value == "":
            return 0.0
        if isinstance(value, (int, float)):
            v = float(value)
            return v / 1000.0 if v > 1e11 else v
        s = str(value).strip()
        if not s:
            return 0.0
        try:
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        try:
            v = float(s)
            return v / 1000.0 if v > 1e11 else v
        except (ValueError, TypeError):
            return 0.0

    def _latest_openclaw_ts(self) -> float:
        turns = self._load_openclaw_turns()
        return max((t["ts"] for t in turns), default=0.0)

    def _sync_openclaw_history(self) -> None:
        """Push any OpenClaw turns with ts > _last_synced_ts into the
        live session as additional context (turn_complete=False).
        Bumps the cursor so the next sync only sees newer turns."""
        with self._session_lock:
            session = self._session
        if session is None:
            return
        if not hasattr(session, "send_context_turns"):
            return  # Not a Gemini Live session (or older shape)

        turns = self._load_openclaw_turns()
        new_turns = [t for t in turns if t["ts"] > self._last_synced_ts]
        if not new_turns:
            return
        try:
            session.send_context_turns([
                {"role": t["role"], "text": t["text"]} for t in new_turns
            ])
        except Exception as e:
            logger.warning("OpenClaw history sync raised: %s", e)
            return
        # Bump cursor only after a successful push so a transient
        # failure can be retried on the next turn.
        self._last_synced_ts = max(t["ts"] for t in new_turns)
        # Visibility — mirrors the call-mode brain.context dumps so the
        # journal shows exactly which turns we injected. Capped at the
        # first 80 chars of each text so the log stays scannable.
        for t in new_turns:
            preview = t["text"][:80]
            logger.info(
                "brain.history.sync [live] +%s %r", t["role"], preview,
            )

    # --- helpers -----------------------------------------------------------

    def _tts_is_speaking(self) -> bool:
        """True while we should drop mic frames — covers both the
        actual TTS playback window and a ``_POST_TTS_HOLDOFF_S``
        cooldown right after, so room reverb / speaker tail-out
        doesn't loop back into Gemini's VAD and trigger a
        self-conversation."""
        if self._tts is None:
            return False
        speaking_now = False
        try:
            if hasattr(self._tts, "speaking"):
                speaking_now = bool(self._tts.speaking)
        except Exception:
            speaking_now = False

        # Edge-detect the speaking→silent transition so we can start
        # the post-TTS holdoff timer exactly once per reply.
        if self._tts_was_speaking and not speaking_now:
            self._tts_stopped_at = time.time()
        self._tts_was_speaking = speaking_now

        if speaking_now:
            return True
        if (
            self._tts_stopped_at is not None
            and (time.time() - self._tts_stopped_at) < _POST_TTS_HOLDOFF_S
        ):
            return True
        # Holdoff lapsed — clear so we don't drag it forever.
        self._tts_stopped_at = None
        return False
