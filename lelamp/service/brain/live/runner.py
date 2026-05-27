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

# Extra mute window AFTER TTSService.speaking flips False. Real
# speakers + room reverb can leak a few hundred ms of late audio that
# Gemini's server VAD will treat as a new user turn, kicking off a
# self-conversation loop. Tunable via env so we can dial it for the
# room. Defaulted to 600ms — long enough for ElevenLabs 24 kHz PCM
# tail-out + typical living-room reverb, short enough that the user
# can interrupt naturally.
_POST_TTS_HOLDOFF_S = float(os.environ.get("LELAMP_LIVE_POST_TTS_HOLDOFF_S", "0.6"))

# How long to keep streaming frames after the local VAD last said
# "this is speech". Without a hold-over, the frame-level gate cuts
# mid-utterance every time a quiet consonant / between-word breath
# drops below RMS threshold — OpenAI then sees fragmented audio it
# can't transcribe. 1.5s matches the classic SILENCE_TIMEOUT_S /
# call-mode IDLE→SPEECH state machine, so live mode end-of-turn
# behaviour is roughly identical to what users already learned in
# call mode. Tunable.
_LIVE_VAD_HOLD_S = float(os.environ.get("LELAMP_LIVE_VAD_HOLD_S", "1.5"))

# Idle-close window. If the active realtime session sees no speech-
# frame from the local VAD for this many seconds (and no TTS / reply
# is in flight), the runner closes the WS and re-enters Phase 1
# (mic open, no session) to wait for the next conversation. Without
# this gate, an always-on lamp pays for a session open + system-prompt
# cache miss on every GoAway (~10-15 min) — 24h × 4-6 reconnects/h =
# ~100-150 full-prompt opens/day even at 3am with nobody home.
# Default 90s — long enough that natural turn-taking pauses don't
# churn reconnects (a real conversation refreshes the timer every
# few seconds), short enough that idle billing is bounded. Set 0 to
# disable (legacy always-open behaviour).
_IDLE_CLOSE_S = float(os.environ.get("LELAMP_LIVE_IDLE_CLOSE_S", "90"))


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
        is_speech_callback: Optional[Callable[[bytes], bool]] = None,
        workspace=None,
    ):
        self._brain = brain
        self._tts = tts_service
        self._alsa_device = alsa_device
        self._input_device = input_device
        self._np = None
        self._sd = None
        # Per-provider BrainWorkspace (typed loose — see brain classes
        # for the matching field). Used to persist chit-chat turn
        # pairs after each is_final reply so the NEXT session can pick
        # them back up via load_context's extra_session_dir merge.
        # Skip the persist path entirely when the workspace is None or
        # its session writer is disabled (e.g. LELAMP_BRAIN_WORKSPACE
        # off, disk full, etc.) — the runner still functions in
        # ephemeral mode, just with the in-session-only memory
        # behaviour that exists pre-A-layout.
        self._workspace = workspace

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
        # Local VAD chain (RMS → WebRTC → Silero) shared with the call
        # mode path. ``is_speech_callback(frame_bytes) -> bool`` returns
        # True when the frame contains speech, False for silence /
        # background. Used to suppress non-speech frames before they
        # reach the realtime provider — saves tokens (OpenAI Realtime
        # bills on audio in) AND keeps the provider's server VAD from
        # latching onto room noise / TV background. None disables the
        # gate entirely (stream every frame, original behaviour).
        self._is_speech_callback = is_speech_callback

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

        # Per-utterance speech-present flag. Flipped True whenever a
        # mic frame passes the local VAD gate (RMS → WebRTC → Silero)
        # and gets shipped to the provider. Read on
        # ``_on_user_input`` is_final to decide whether to ask the
        # provider for a response — pure echo / room noise that
        # bypassed the gate but happened to transcribe (Whisper
        # hallucination) won't trigger a wasted response if the flag
        # is False. Reset every turn_complete + on transcript
        # is_final. Stays True (degraded behaviour) when no VAD
        # callback was wired so the runner still works without a
        # local gate.
        self._utterance_has_speech = self._is_speech_callback is None

        # Timestamp of the most recent frame the local VAD verdict'd
        # as speech. ``-inf`` so the IDLE check (now - ts > HOLD_S)
        # holds true at startup — we don't stream the first
        # ``_LIVE_VAD_HOLD_S`` of boot audio to OpenAI just because
        # the timer hasn't been written to yet.
        self._last_speech_frame_ts: float = float("-inf")

        # Mirror of the call-mode per-turn logs so the journal shows
        # the same `brain.input` / `brain.chitchat` shape regardless
        # of which brain mode is active. Accumulated per turn from
        # the provider's incremental on_user_input / on_text callbacks
        # and flushed (logged) on turn_complete.
        self._user_input_buf = ""
        self._reply_log_buf = ""

        # Snapshot of the user-input transcript at the moment its
        # is_final event fired this turn. Stays set across the rest of
        # the turn so _on_delegate can recover the user's words after
        # the live accumulator buffer itself is cleared. Cleared at
        # the next on_user_input partial.
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
        """Open mic, wait for the first speech frame, then open a
        BrainSession and push frames until the session closes
        (delegate, GoAway, error) or the idle-close timer lapses.

        Two phases per call:

          Phase 1 (idle): mic open, no realtime WS. Local VAD watches
            the stream; the rolling audio buffer keeps filling so
            speaker recog has 30s of warm audio when a delegate fires.
            Echo gate drops TTS tail / Lumi's own voice. We exit Phase
            1 only on a positive VAD verdict — no session is opened
            (and no provider tokens are spent) until then.

          Phase 2 (active): open the WS, send the trigger frame first
            so the first word isn't lost, then run the normal stream
            loop. Exits when the provider closes the session, when
            ``_restart_after_turn`` flips, OR when the idle-close
            timer (``LELAMP_LIVE_IDLE_CLOSE_S``) lapses — at which
            point the outer loop calls us again and we re-enter
            Phase 1.
        """
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

        with mic_ctx as mic:
            # ---- Phase 1: idle (no session) ---------------------------
            trigger_frame = self._wait_for_speech(mic, frame_size)
            if trigger_frame is None:
                return  # stop_event fired during idle wait

            # ---- Speaker identification (parallel with session.start) -
            # Run voiceprint match on the rolling audio buffer in a
            # background thread so the cost (~50-200ms ONNX on Pi)
            # overlaps the WS connect (~500-1000ms) — net-zero added
            # latency. Result is read after session.start returns, so
            # we never block on speaker ID and a stuck recognizer
            # never blocks the session open. The recognizer needs at
            # least SPEAKER_MIN_AUDIO_S (~1.5s) of audio; on a brand-
            # new boot when the rolling buffer is shorter than that,
            # it silently returns None and we fall back to no-label.
            audio_snapshot = list(self._audio_buf)
            speaker_result: dict = {}
            speaker_thread: Optional[threading.Thread] = None
            if self._decorate_callback is not None and audio_snapshot:
                def _id_thread():
                    try:
                        decorated, name = self._decorate_callback("", audio_snapshot)
                        speaker_result["decorated"] = decorated or ""
                        speaker_result["name"] = (name or "").strip()
                    except Exception as e:
                        logger.debug("speaker ID thread raised: %s", e)
                speaker_thread = threading.Thread(
                    target=_id_thread, daemon=True, name="live-speaker-id",
                )
                speaker_thread.start()

            # ---- Phase 2: active (session open) -----------------------
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
                self._restart_after_turn = False
                self._user_input_logged = False
                self._tts_start_logged = False
                # Phase 1 only exits on a real speech frame (or the
                # no-VAD legacy path), so the current utterance
                # already has speech.
                self._utterance_has_speech = True
            # Re-arm echo gate state. Don't leak holdoff timing across
            # sessions — a stale ``_tts_stopped_at`` from the previous
            # session would otherwise keep muting the mic on a fresh open.
            self._tts_stopped_at = None
            self._tts_was_speaking = False
            # Snapshot the initial history cursor so the first post-turn
            # sync only pushes truly NEW OpenClaw turns. Pulls the same
            # turns that load_context() just baked into the system prompt.
            self._last_synced_ts = self._latest_openclaw_ts()

            # Collect the speaker-ID result that ran in parallel with
            # session.start. Bounded join — never block more than 1s
            # waiting for a stuck recognizer, just open the session
            # without a speaker label in that worst case.
            speaker_name = ""
            if speaker_thread is not None:
                speaker_thread.join(timeout=1.0)
                speaker_name = speaker_result.get("name", "") or ""
            if (
                speaker_name
                and speaker_name.lower() != "unknown"
                and hasattr(session, "send_context_turns")
            ):
                # Push a single system-role item so the model knows
                # who's currently in front of the mic and can address
                # them by name without delegating just to look it up.
                # No response.create — context-only, the next user
                # turn will fire the actual reply.
                try:
                    session.send_context_turns([
                        {"role": "system", "text": f"Current speaker: {speaker_name}"}
                    ])
                    logger.info("brain.speaker [live] identified as %r", speaker_name)
                except Exception as e:
                    logger.debug("speaker label inject failed: %s", e)
            elif speaker_thread is not None:
                logger.info(
                    "brain.speaker [live] unknown (no enrolled match — enroll flow runs only on delegate path for now)"
                )

            try:
                # Ship the trigger frame first so the first word of the
                # utterance isn't dropped by the Phase 1 → Phase 2
                # transition.
                session.send_audio(trigger_frame)
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
                    # Idle-close: TTS quiet + no reply being generated
                    # + no speech-frame for IDLE_CLOSE_S → close the WS
                    # and let the outer loop re-enter Phase 1 (mic stays
                    # closed for ~50ms during mic_ctx churn but no
                    # session billing until next speech).
                    if _IDLE_CLOSE_S > 0:
                        with self._reply_lock:
                            reply_in_flight = bool(self._reply_log_buf)
                        if (
                            not self._tts_is_speaking()
                            and not reply_in_flight
                            and (time.time() - self._last_speech_frame_ts) > _IDLE_CLOSE_S
                        ):
                            logger.info(
                                "Live brain idle %.0fs — closing session to save tokens",
                                _IDLE_CLOSE_S,
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
                    # Local VAD gate (when wired). Skipping silence
                    # frames keeps the provider's server VAD from
                    # auto-committing on background noise and cuts
                    # the audio-in token bill — the realtime billing
                    # model charges per second of input audio, so
                    # streaming pure silence 24/7 is pure waste.
                    # Callback expects a numpy int16 frame (same
                    # shape voice_service's classic VAD loop gets),
                    # not raw bytes — see _make_live_vad_check.
                    #
                    # State machine (mirrors call-mode IDLE→SPEECH):
                    # any positive VAD verdict refreshes a hold-over
                    # timer; while the timer is fresh we forward
                    # every frame regardless of RMS so quiet
                    # consonants / inter-word breath don't fragment
                    # the utterance and starve OpenAI's transcriber.
                    if self._is_speech_callback is not None:
                        try:
                            is_speech = bool(self._is_speech_callback(data))
                        except Exception as e:
                            logger.debug("is_speech_callback raised: %s", e)
                            is_speech = True  # fail-open: don't drop on error
                        now = time.time()
                        if is_speech:
                            self._last_speech_frame_ts = now
                            # Flag the current utterance as having
                            # real speech so request_response() fires
                            # after the transcript completes.
                            # Without this flag a pure-echo turn
                            # (Whisper hallucinated on TTS tail
                            # leaking past the holdoff) would still
                            # trigger a model response.
                            self._utterance_has_speech = True
                        elif (now - self._last_speech_frame_ts) > _LIVE_VAD_HOLD_S:
                            # Holdoff lapsed — back to IDLE, drop this
                            # silence frame.
                            continue
                    session.send_audio(frame_bytes)
            finally:
                with self._session_lock:
                    self._session = None
                try:
                    session.close()
                except Exception:
                    pass

    def _wait_for_speech(self, mic, frame_size: int) -> Optional[bytes]:
        """Phase 1 loop — keep the mic running but don't open the
        realtime session until the local VAD says someone is talking.
        Returns the trigger frame on speech, or ``None`` if the stop
        event fires first. When no VAD callback is wired (legacy
        always-on behaviour) the very first frame triggers immediately.

        The rolling audio buffer keeps filling here so when a session
        opens and the user delegates within the first ~30s, the
        speaker recognizer still has warm audio to chew on. The echo
        gate also runs in this phase so TTS playback from a previous
        session can't trigger a new session via room reverb."""
        while not self._stop_event.is_set():
            try:
                data, _ = mic.read(frame_size)
            except IOError as e:
                logger.info("Live mic EOF in idle phase: %s", e)
                return None
            frame_bytes = data.tobytes()
            self._audio_buf.append(frame_bytes)
            if self._tts_is_speaking():
                continue
            if self._is_speech_callback is None:
                # No VAD wired — fall back to legacy behaviour: the
                # first non-TTS frame opens the session. This is
                # degraded cost mode and only happens if voice_service
                # didn't pass a VAD callback in.
                return frame_bytes
            try:
                is_speech = bool(self._is_speech_callback(data))
            except Exception as e:
                logger.debug("is_speech_callback raised in idle: %s", e)
                is_speech = True  # fail-open: don't sit silent on a VAD bug
            if is_speech:
                self._last_speech_frame_ts = time.time()
                return frame_bytes
        return None

    # --- callbacks ---------------------------------------------------------

    def _on_text(self, text: str, is_final: bool) -> None:
        """Reply transcript from the brain. Buffer + sentence-split +
        push each complete sentence into TTSService.speak_queue. ``is_final``
        marks turn_complete — flush whatever's left as a single chunk
        and log the full reply line so the journal mirrors the call-mode
        ``brain.chitchat`` log shape.

        Delegate routing in live mode goes through the function tool
        only (``delegate_to_lumi``). The legacy ``[DELEGATE]`` text
        marker that call mode uses was a fallback here too but the
        runner no longer scans for it — the marker text is enabled
        only on the call-mode prompt; live-mode prompts explicitly
        forbid it and both Gemini Live + OpenAI Realtime have native
        function-call channels."""
        if not text and not is_final:
            return
        tail: Optional[str] = None
        full_reply: Optional[str] = None
        user_input_to_log: Optional[str] = None

        with self._reply_lock:
            if text:
                self._reply_buf += text
                self._reply_log_buf += text
                # Gemini Live fires _on_user_input("", True) only at
                # turn_complete, which lands AFTER the model's reply
                # has finished streaming — making brain.transcript
                # log ~10-30s after the user actually finished
                # speaking. OpenAI Realtime gives a proper
                # transcription.done event before its response so
                # its brain.transcript fires at the right moment.
                # Bridge for Gemini: when the model starts emitting
                # reply tokens, that IS the signal "Gemini decided
                # the user is done". If we have an accumulated user
                # input buffer that wasn't logged yet, flush it now
                # so the journal shows transcript → tts.start in
                # natural order. Safe for OpenAI too because its
                # is_final path already sets _user_input_logged=True
                # before the first reply token arrives.
                if (
                    not self._user_input_logged
                    and self._user_input_buf.strip()
                ):
                    user_input_to_log = self._user_input_buf.strip()
                    self._user_input_logged = True

            if is_final:
                full_reply = self._reply_log_buf.strip()
                tail = self._reply_buf.strip()
                # Per-turn latches (_user_input_logged / _tts_start_logged)
                # used to reset HERE — moved to after the tail flush
                # below. Resetting too early made the is_final tail
                # _speak_sentence log a SECOND brain.tts.start (with
                # the last sentence, not the first), since the flag
                # was already False by the time tail flush ran.
                self._reply_buf = ""
                self._reply_log_buf = ""
                # NOTE: _user_input_buf is reset in _on_user_input when
                # its own is_final fires (which now arrives BEFORE this
                # block thanks to the swapped order in gemini_live.py
                # _handle_server_content). We don't clear it here so a
                # late-arriving partial doesn't go to the next turn.
            else:
                self._reply_buf = _drain_complete_sentences(
                    self._reply_buf, self._speak_sentence,
                )

        if user_input_to_log:
            # Logged as "transcript" not "input": the model in live
            # mode listens to audio natively — this string is from
            # the side-channel ASR (Whisper / Google ASR) and is
            # debug-only, NOT what the model actually saw. See
            # README §"Native multimodal vs side-channel ASR".
            logger.info("brain.transcript [live] %r", user_input_to_log)

        if not is_final:
            return

        if tail:
            self._speak_sentence(tail)
        # Now that the last sentence of this turn has been handed to
        # TTS, re-arm the per-turn latches for the NEXT turn. Without
        # this the next turn would silently skip its brain.input +
        # brain.tts.start logs because the flags carry over.
        self._user_input_logged = False
        self._tts_start_logged = False
        if full_reply:
            logger.info("brain.chitchat [live] %r", full_reply)
            # Keep one turn back so the next turn's echo check (in
            # _should_skip_response) can compare against it after
            # _reply_log_buf has been cleared.
            self._last_full_reply = full_reply
            # Persist the turn pair so the NEXT session (after GoAway,
            # idle-close, or process restart) can pick this chit-chat
            # up via load_context's extra_session_dir merge. Skipped
            # for delegate turns — those already land in OpenClaw's
            # JSONL via the sensing endpoint, so writing them here
            # would double-count. Skipped when no workspace is wired
            # (degraded ephemeral mode).
            self._persist_chitchat_turn(self._last_user_input.strip(), full_reply)

        # History sync used to fire HERE (after turn_complete) — moved
        # to _on_user_input is_final so the model sees fresh OpenClaw
        # turns BEFORE generating the current reply (e.g. user says
        # "summarise our chat" — sync runs, then request_response, so
        # the summary covers the freshly-pushed turns instead of
        # waiting one turn behind).

    def _persist_chitchat_turn(self, user_text: str, assistant_text: str) -> None:
        """Append the {user, assistant} turn pair to the per-provider
        workspace's session JSONL. Format matches what
        ``TextBrain._append_session_turn`` writes so context_loader's
        merge logic treats both modes' history identically::

            {"role": "user"|"assistant", "text": "...", "ts": <epoch_seconds>}

        Skipped when the workspace is None or its session writer is
        disabled (LELAMP_BRAIN_WORKSPACE=off, disk failure, etc.).
        Best-effort — never raises into the brain callback path.
        """
        if self._workspace is None:
            return
        session = getattr(self._workspace, "session", None)
        if session is None or not getattr(session, "enabled", False):
            return
        now = time.time()
        try:
            if user_text:
                session.write({"role": "user", "text": user_text, "ts": now})
            if assistant_text:
                session.write({"role": "assistant", "text": assistant_text, "ts": now})
        except Exception as e:
            logger.debug("live brain chit-chat persist failed: %s", e)

    def _on_user_input(self, text: str, is_final: bool) -> None:
        """User mic transcription from the live provider. Accumulates
        partials. The ``brain.input`` journal line is normally logged
        from :meth:`_on_text` on the first reply-token (so the natural
        order in the journal is input → reply, not "both at the end"),
        but THIS method handles the fallback case where the model
        decides the right answer is silence — no reply means
        _on_text never fires, so we log here on is_final.

        Providers disagree on the is_final shape: Gemini Live fires
        ``("", True)``; OpenAI Realtime fires ``(full_transcript, True)``
        after streaming deltas. To handle both: when ``is_final`` and
        ``text`` is non-empty, treat ``text`` as the server's
        authoritative final transcript and REPLACE the accumulated
        delta buffer with it. Otherwise (empty final OR partial) keep
        the existing accumulator path. Prevents the doubled
        ``"Em đi đi.Em đi đi."`` bug where OpenAI deltas + the final
        transcript both got appended.

        Side effect: when ``is_final`` fires we snapshot the
        transcript into ``self._last_user_input`` so :meth:`_on_delegate`
        can still recover the user's words after the live accumulator
        buffer itself is cleared."""
        if not text and not is_final:
            return
        # First partial of a new turn — wipe the previous turn's
        # snapshot so a late callback can't grab stale text.
        if text and not is_final and not self._user_input_buf:
            with self._reply_lock:
                self._last_user_input = ""

        with self._reply_lock:
            if is_final:
                # Server's authoritative final overrides anything we
                # buffered from deltas (OpenAI Realtime ships both).
                if text:
                    self._user_input_buf = text
                final_text = self._user_input_buf.strip()
                self._user_input_buf = ""
                if final_text:
                    self._last_user_input = final_text
                # Log brain.input on transcript-final UNLESS the
                # early-log path in _on_text already flushed it for
                # this turn. The early-log path fires on the first
                # reply token (Gemini bridge for natural ordering)
                # and sets _user_input_logged=True so this is_final
                # branch skips the duplicate. For OpenAI (no early
                # log path needed since is_final fires before any
                # reply), _user_input_logged is False here so the
                # is_final log fires normally.
                fallback_log = bool(final_text) and not self._user_input_logged
                if fallback_log:
                    self._user_input_logged = True
                # Snapshot speech-present flag and reset for the next
                # utterance. We use the snapshot below to decide
                # whether to fire response.create.
                had_speech = self._utterance_has_speech
                self._utterance_has_speech = self._is_speech_callback is None
            else:
                if text:
                    self._user_input_buf += text
                final_text = None
                fallback_log = False
                had_speech = False
        if fallback_log:
            # See note above: "transcript" not "input" — this is the
            # ASR side-channel, not what the live model saw.
            logger.info("brain.transcript [live] %r", final_text)

        # Gate response.create — only fires when:
        #   1. The session supports explicit response.create
        #      (OpenAIRealtimeSession; Gemini Live auto-fires).
        #   2. The transcript looks like real user speech (passes
        #      length + hallucination + echo filter).
        #   3. Local VAD saw at least one speech frame this utterance
        #      (or no VAD wired — fail-open).
        # Otherwise log and skip — saves tokens + stops the
        # echo-loop / hallucination loop the user is hitting.
        if not is_final:
            return
        with self._session_lock:
            session = self._session
        if session is None:
            return
        if not hasattr(session, "request_response"):
            return  # Gemini Live: auto-fires, nothing to do here.
        if not final_text:
            return
        skip_reason = self._should_skip_response(final_text, had_speech)
        if skip_reason:
            logger.info(
                "brain.skip   [live] %r — %s",
                final_text, skip_reason,
            )
            return
        # Pull any OpenClaw turns (Telegram, web chat, other voice
        # sessions, the just-completed delegate's reply) into the
        # live session BEFORE the model generates this reply — so
        # the answer is grounded in the freshest context, not "one
        # turn behind". ``then_request_response=True`` chains the
        # response.create call inside the same async coroutine as
        # the item.create batch so there's no asyncio await race
        # putting response.create on the WS before the last item.
        # Returns False when there were no new turns AND no chained
        # request_response was scheduled — in that case we fire
        # request_response ourselves.
        scheduled = self._sync_openclaw_history(then_request_response=True)
        if not scheduled:
            try:
                session.request_response()
            except Exception as e:
                logger.warning("request_response failed: %s", e)

    # Known Whisper / gpt-4o-transcribe hallucinations on silence or
    # short / low-info audio. The ASR latches onto language-common
    # boilerplate when the actual audio doesn't carry much speech —
    # "Hẹn gặp lại các bạn trong những video tiếp theo" is the
    # Vietnamese YouTube outro pattern; "Thanks for watching" is the
    # English equivalent. Match is substring + case-insensitive
    # because the ASR sometimes drops the leading word or appends
    # extra punctuation.
    _HALLUCINATION_PATTERNS = (
        "hẹn gặp lại các bạn",
        "trong những video tiếp theo",
        "thanks for watching",
        "subscribe to the channel",
        "like and subscribe",
        "ご視聴ありがとうございました",
    )

    def _should_skip_response(self, transcript: str, had_speech: bool) -> Optional[str]:
        """Return a non-empty reason string when this transcript
        should NOT trigger a model response, or None to proceed."""
        t = transcript.strip()
        if not t:
            return "empty transcript"
        if not had_speech:
            return "no speech frames passed local VAD this utterance"
        # Length floor — single-token transcripts (\"uh\", \"oh\")
        # are usually mic pop / lip-smack noise, not a real prompt.
        # Two-token floor catches the common Vietnamese filler "Ừm."
        # which the user does sometimes actually mean — keep it
        # permissive to avoid blocking real input.
        if len(t) < 2:
            return f"transcript too short ({len(t)} chars)"
        lo = t.lower()
        for pat in self._HALLUCINATION_PATTERNS:
            if pat in lo:
                return f"matches ASR hallucination pattern {pat!r}"
        # Echo of Lumi's own most recent reply — happens when the
        # TTS tail leaks past the post-TTS holdoff and ASR transcribes
        # it. Compare against the trailing portion of the last
        # chit-chat we logged. Conservative: only skip on a very
        # high-overlap match (>=80% of transcript chars in the last
        # reply) to avoid swallowing real "yes I heard you" follow-ups.
        last_reply = (self._reply_log_buf or "")
        # _reply_log_buf is cleared on turn_complete, so we also keep
        # a one-turn-back snapshot for the echo compare.
        last_full = getattr(self, "_last_full_reply", "") or ""
        haystacks = (last_reply.lower(), last_full.lower())
        for h in haystacks:
            if not h:
                continue
            if lo in h:
                return "transcript is a substring of last reply (echo)"
        return None

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
        call-mode path uses.

        The delegate tool takes no arguments — we pull the user's
        actual transcript from the ASR side-channel (the same source
        ``brain.input`` logs from). The ``transcript`` parameter is
        kept on the signature only for legacy callers and is
        ignored. Gemini fires the function_call before turn_complete,
        so ``_last_user_input`` (only set at is_final) may be empty;
        we fall back to the live delta accumulator in that case.
        OpenAI's transcription.completed event fires before any tool
        call, so ``_last_user_input`` is populated by then."""
        transcript = (self._last_user_input or "").strip()
        if not transcript:
            with self._reply_lock:
                transcript = self._user_input_buf.strip()
        if not transcript:
            logger.info("delegate fired with no user transcript captured — dropping turn")
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

    # OpenClaw's sensing endpoint decorates every voice transcript with
    # a speaker label prefix: ``Unknown Speaker:`` /
    # ``Unknown Speaker[voice:abc123]:`` / ``Speaker - Bob:``. Useful
    # for OpenClaw's per-speaker routing, but when those turns get
    # synced back into the brain session as ``role=user`` content, the
    # model treats the prefix as literal user speech and starts copying
    # ``Unknown Speaker:`` into delegate transcripts. We strip the
    # prefix before injection — the role marker on the conversation
    # item already tells the model who spoke.
    _SPEAKER_PREFIX_RE = re.compile(
        r"^(?:Unknown Speaker(?:\[voice:[^\]]*\])?|Speaker\s*-\s*[^:]+):\s*"
    )

    @classmethod
    def _strip_speaker_prefix(cls, text: str) -> str:
        return cls._SPEAKER_PREFIX_RE.sub("", text, count=1).strip()

    # Prefixes that mark a turn as "not real user conversation" — sensing
    # events, vision frame descriptions, untrusted metadata wrappers,
    # operator instructions. Injecting these mid-session derails the
    # model: it starts talking about "what it just saw" or "the user
    # asked the time again" when really the user just said "làm thơ đi".
    _SYNC_SKIP_PREFIXES = (
        "[whisper]", "[sensing:", "[activity]", "[emotion]",
        "[speech_emotion]", "[posture", "[motion", "[ambient]",
        "Sender (untrusted metadata):", "(system)", "[HW:", "/emotion",
        "/led", "/servo",
    )

    def _is_syncable_turn(self, turn: dict) -> bool:
        """True when this OpenClaw turn looks like real conversation
        worth injecting into the live session's context. Filters out
        sensing events, vision detections, operator commands, system
        noise — anything that would confuse the realtime model about
        what the user actually said."""
        text = (turn.get("text") or "").strip()
        if not text:
            return False
        # Strip leading "[user] " / "[user] [ambient] " priority tags
        # that Lumi server adds — keep the substantive part.
        head = text
        for tag in ("[user] [ambient] ", "[user] "):
            if head.startswith(tag):
                head = head[len(tag):]
                break
        if not head.strip():
            return False
        for prefix in self._SYNC_SKIP_PREFIXES:
            if head.startswith(prefix):
                return False
        return True

    def _sync_openclaw_history(self, then_request_response: bool = False) -> bool:
        """Push any OpenClaw turns with ts > _last_synced_ts into the
        live session as additional context. Filters out sensing /
        operator / vision noise so the realtime model only sees real
        conversation. Bumps the cursor so the next sync only sees
        newer turns.

        ``then_request_response`` chains ``response.create`` inside
        the SAME async coroutine that pushes the items, guaranteeing
        the model's reply is grounded in the freshly-pushed context.
        Without this flag a runner-side ``session.request_response()``
        could race the sync coroutine at asyncio await boundaries and
        ship response.create before the last item.create lands.

        Returns True when it scheduled work on the session loop (either
        synced turns OR a chained response.create), False when there
        was nothing to do and the caller should fire request_response
        on its own."""
        with self._session_lock:
            session = self._session
        if session is None:
            return False
        if not hasattr(session, "send_context_turns"):
            return False  # Gemini Live: no inline-sync support yet

        turns = self._load_openclaw_turns()
        # All unfiltered turns past the cursor — used to advance the
        # cursor past noise events too (otherwise we'd re-check the
        # same sensing event every turn).
        all_new = [t for t in turns if t["ts"] > self._last_synced_ts]
        new_turns = [t for t in all_new if self._is_syncable_turn(t)]
        # Bump cursor past everything we've seen so noise we filtered
        # out doesn't get re-scanned on the next turn. Safe to do
        # before the send because the send is best-effort: a failed
        # WS write doesn't justify re-pushing the same JSONL noise.
        if all_new:
            self._last_synced_ts = max(t["ts"] for t in all_new)
        if not new_turns and not then_request_response:
            return False
        # Visibility — log BEFORE the send so the journal shows what
        # we're about to inject even if the send hangs / fails.
        for t in new_turns:
            preview = t["text"][:80]
            logger.info(
                "brain.history.sync [live] +%s %r", t["role"], preview,
            )
        # Build the sync payload:
        #   1. Strip ``Unknown Speaker: …`` / ``Speaker - <name>: …``
        #      prefix from user turns. OpenClaw's sensing endpoint
        #      decorates every voice transcript with that prefix for
        #      its own routing; replaying the decoration back into
        #      the brain session makes the model think the user
        #      literally said the words "Unknown Speaker:" and then
        #      copy them verbatim into delegate transcripts. The
        #      role=user marker on the conversation.item already
        #      tells the model who spoke.
        #   2. Prefix the cleaned text with ``[HH:MM]`` so the model
        #      can reason about how long ago each OpenClaw turn
        #      happened ("anh hỏi nãy giờ", "vừa xong em nói gì").
        #      OpenAI Realtime has no message-level timestamp field
        #      (verified against the SDK schema 2026-05-27), so
        #      embedding in text is the only path. Matches the format
        #      context_loader.to_system_prompt_block uses for the
        #      initial RECENT CONVERSATION block.
        from datetime import datetime as _dt
        payload: list[dict] = []
        for t in new_turns:
            text = t["text"]
            if t["role"] == "user":
                text = self._strip_speaker_prefix(text)
            try:
                clock = _dt.fromtimestamp(t["ts"]).strftime("%H:%M")
                text = f"[{clock}] {text}"
            except (OverflowError, ValueError, KeyError):
                pass
            payload.append({"role": t["role"], "text": text})

        try:
            session.send_context_turns(
                payload,
                then_request_response=then_request_response,
            )
        except TypeError:
            # Older session shape without the chained flag — fall back
            # to the unchained send. Caller will need to fire
            # request_response separately.
            try:
                session.send_context_turns(payload)
            except Exception as e:
                logger.warning("OpenClaw history sync raised: %s", e)
                return False
            return False
        except Exception as e:
            logger.warning("OpenClaw history sync raised: %s", e)
            return False
        return True

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
