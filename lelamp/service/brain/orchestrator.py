"""BrainOrchestrator — single integration surface between
VoiceService (transport) and brain/ (policy).

VoiceService used to inline ~200 lines of brain wiring: mode picker,
provider factory, VAD-chain closure for the live runner, post-TTS
end-to-end latency tracker, and the half-cascade text-brain decision
block inside ``_loop``. All of that lives here now. VoiceService just
constructs one ``BrainOrchestrator``, asks ``in_live_mode``, and
defers to ``handle_stt_final`` for the call-mode path.

This module knows nothing about the audio device, ALSA, sounddevice,
STT providers, or VoiceService internals beyond the explicit
dependencies passed to ``__init__``. That keeps the brain
self-contained — tests can instantiate the orchestrator with stubs
without spinning up the whole voice stack."""

import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("lelamp.brain.orchestrator")


class BrainOrchestrator:
    """Owns the call-mode text brain OR the live-mode realtime runner
    (mutually exclusive, picked by ``LELAMP_BRAIN_MODE``). Exposes a
    handful of hooks VoiceService calls in its lifecycle:

    * :meth:`in_live_mode` — VoiceService short-circuits its classic
      STT loop when this returns True (the live runner owns the mic).
    * :meth:`start` / :meth:`stop` — lifecycle for the live runner.
      No-op in call mode.
    * :meth:`handle_stt_final` — call-mode hook fired after STT
      finalises a transcript. Returns True when the brain replied
      (chit-chat path); False to let VoiceService forward the
      decorated message to OpenClaw the usual way.
    """

    def __init__(
        self,
        *,
        # TTS for both modes (live runner pipes sentences into it,
        # call mode uses speak_queue for sentence-streamed chit-chat).
        tts_service,
        # ALSA / sounddevice mic config — only consumed by the live
        # runner, but accepted in both modes so the constructor
        # signature stays stable across mode flips.
        alsa_device: Optional[str],
        input_device: Optional[int],
        # Speaker recog + Lumi forwarder — both wrap VoiceService
        # methods. The live runner needs them so delegate turns are
        # decorated + sent the same way the classic path does.
        decorate_callback: Callable,
        send_to_lumi_callback: Callable,
        # Local VAD chain components — the live runner uses these to
        # gate frames before they hit the realtime provider. None
        # values are tolerated (degraded modes — see
        # ``_make_live_vad_check``).
        np_module,
        webrtcvad_instance,
        silero_instance,
        webrtcvad_check: Callable,
        silero_check: Callable,
        rms_threshold: int,
        stt_rate: int,
    ):
        self._tts = tts_service
        self._alsa_device = alsa_device
        self._input_device = input_device
        self._decorate_callback = decorate_callback
        self._send_to_lumi_callback = send_to_lumi_callback
        self._np = np_module
        self._webrtcvad = webrtcvad_instance
        self._silero = silero_instance
        self._webrtcvad_check = webrtcvad_check
        self._silero_check = silero_check
        self._rms_threshold = rms_threshold
        self._stt_rate = stt_rate

        # Mode picker — env LELAMP_BRAIN_MODE = call (default) | live.
        #   call: half-cascade text brain. Classic STT pipeline (RMS +
        #         Silero + Deepgram) does the audio→text work; the
        #         brain only picks chit-chat vs delegate after STT.
        #         See lelamp/service/brain/call/text_router.py.
        #   live: realtime brain (Gemini Live / OpenAI Realtime). Mic
        #         frames stream straight to the provider, which does
        #         server-side VAD + STT + decision. The reply text is
        #         routed through ElevenLabs via LiveBrainRunner so the
        #         user keeps one voice across both modes.
        # In live mode VoiceService's classic VAD pipeline is bypassed
        # entirely — the runner owns the mic, the only shared state
        # is TTSService (for the echo gate + speak_queue dispatch).
        self._brain_mode = os.environ.get("LELAMP_BRAIN_MODE", "call").strip().lower()
        self._text_brain = None
        self._live_runner = None
        if self._brain_mode == "live":
            self._init_live_brain()
        else:
            self._init_call_brain()

    # --- lifecycle ---------------------------------------------------

    @property
    def in_live_mode(self) -> bool:
        """True when the live runner is wired and ready. VoiceService
        uses this to skip its classic STT loop — the runner owns
        the mic in live mode."""
        return self._live_runner is not None

    def start(self) -> None:
        """Start the live runner (no-op in call mode)."""
        if self._live_runner is not None:
            self._live_runner.start()
            logger.info(
                "BrainOrchestrator started in LIVE mode — classic VAD/STT loop not running"
            )

    def stop(self) -> None:
        """Stop the live runner (no-op in call mode)."""
        if self._live_runner is not None:
            self._live_runner.stop()

    # --- call-mode STT hook -----------------------------------------

    def handle_stt_final(
        self, final_text: str, user: str, event_type: str,
    ) -> bool:
        """Half-cascade brain hook — runs after VoiceService gets a
        final STT transcript. Only active in call mode AND only on
        the plain ``voice`` event type (wake-word events have their
        own routing).

        Returns:
            True  — brain handled the turn (chit-chat reply spoken).
                    Caller MUST NOT also forward to Lumi.
            False — brain abstained / delegated / errored. Caller
                    should fall through to the normal Lumi forward.
        """
        if (
            self._text_brain is None
            or not self._text_brain.available
            or event_type != "voice"
            or not final_text.strip()
        ):
            return False

        logger.info("brain.input  [%s] %r", user, final_text)
        # t0 for end-to-end timing: the moment the STT transcript is
        # in hand and we hand it to the brain. That's also the moment
        # the user has stopped speaking from the listener's
        # perspective.
        t_voice_final = time.time()
        # Stream each completed sentence straight into the TTS queue
        # so playback starts on the first sentence while the model is
        # still generating the rest of the reply. Skips delegate
        # replies — the brain only fires this callback once it has
        # confirmed the reply is chit-chat (not the [DELEGATE] marker).
        on_sentence = None
        if self._tts is not None and hasattr(self._tts, "speak_queue"):
            on_sentence = self._tts.speak_queue
        decision = self._text_brain.decide(
            final_text, speaker=user, on_sentence=on_sentence,
        )
        logger.info(
            "brain.decide [%s] decision=%s latency=%.2fs tokens=%d "
            "(prompt=%d response=%d)",
            user, decision.decision, decision.latency_s,
            decision.total_tokens, decision.prompt_tokens, decision.response_tokens,
        )
        if decision.decision == "chitchat" and decision.reply:
            logger.info("brain.chitchat [%s] %r", user, decision.reply)
            try:
                # If we couldn't stream sentence-by-sentence (no
                # speak_queue available), fall back to the original
                # single-shot dispatch so the reply still gets spoken.
                if on_sentence is None and self._tts is not None:
                    self._tts.speak(decision.reply)
                # Off-loop tracker waits for TTS playback to finish,
                # then logs the full STT-final → speech-end latency.
                # Daemon thread; never blocks the voice loop. Only
                # meaningful for chit-chat (delegate path's "end" is
                # OpenClaw's own response).
                self._track_chitchat_e2e(user, t_voice_final, decision)
            except Exception as e:
                logger.warning("brain chitchat speak failed: %s", e)
            return True
        if decision.decision == "delegate":
            logger.info("brain.delegate [%s] → Lumi", user)
            return False
        # decision == "error" — log and let it fall through to Lumi
        # (safe default; never silently drop user input).
        logger.warning(
            "brain.error [%s] %s — falling through to Lumi",
            user, decision.error or "(no detail)",
        )
        return False

    # --- internal init paths ----------------------------------------

    def _init_live_brain(self) -> None:
        try:
            from lelamp.service.brain.live.factory import make_brain
            from lelamp.service.brain.live.runner import LiveBrainRunner
            from lelamp.service.brain.workspace import BrainWorkspace
        except Exception as e:
            logger.warning("Live brain import failed, keeping classic STT path: %s", e)
            return

        provider = os.environ.get("LELAMP_BRAIN_PROVIDER", "gemini").strip().lower()
        try:
            # Per-provider workspace — see workspace.py docstring for
            # layout-A rationale. Each live provider gets its own
            # session/bench/MEMORY.md under <root>/live-<provider>/ so
            # chit-chat history doesn't cross-contaminate when A/B
            # testing. The brain reads it via load_context's
            # extra_session_dir; the runner writes turn pairs into it
            # on every chit-chat reply.
            live_workspace = BrainWorkspace(subdir=f"live-{provider}")
            live_brain = make_brain(provider, workspace=live_workspace)
        except Exception as e:
            logger.warning("Live brain init exception, keeping classic STT path: %s", e)
            return

        if live_brain is None or not live_brain.available:
            logger.warning(
                "Live brain init failed (provider=%s) — falling back to classic STT",
                provider,
            )
            return

        self._live_runner = LiveBrainRunner(
            brain=live_brain,
            tts_service=self._tts,
            alsa_device=self._alsa_device,
            input_device=self._input_device,
            # Hook back into VoiceService so delegate turns go through
            # the same speaker-prefix + echo-filter + retry pipeline
            # as call mode (otherwise OpenClaw sees a raw transcript
            # without the "<Name>: …" prefix).
            decorate_callback=self._decorate_callback,
            send_to_lumi_callback=self._send_to_lumi_callback,
            # Reuse the same VAD chain call mode uses (RMS → WebRTC →
            # Silero) so the live runner only sends meaningful audio
            # to the provider — saves cost + bandwidth + privacy vs
            # the previous "stream 24/7" behaviour.
            is_speech_callback=self._make_live_vad_check(),
            # Same workspace handle the brain reads from — the runner
            # appends {user, assistant} turn pairs after each chit-chat
            # reply so the next session sees them via load_context's
            # extra_session_dir merge.
            workspace=live_workspace,
        )
        logger.info(
            "BrainOrchestrator brain mode=live (provider=%s) — classic VAD bypassed",
            provider,
        )

    def _init_call_brain(self) -> None:
        try:
            from lelamp.service.brain import build_text_brain_from_env
            self._text_brain = build_text_brain_from_env()
            if self._text_brain is not None:
                logger.info(
                    "BrainOrchestrator brain mode=call (text router — provider=%s, model=%s)",
                    self._text_brain.provider, self._text_brain.model,
                )
        except Exception as e:
            logger.warning("Text brain init failed, keeping classic STT path: %s", e)

    # --- helpers ----------------------------------------------------

    def _make_live_vad_check(self) -> Callable[[object], bool]:
        """Build a closure that reproduces the call-mode VAD chain
        (RMS → WebRTC → Silero) for the live runner.

        Returns a function ``is_speech(data) -> bool`` where ``data``
        is a numpy int16 frame at ``stt_rate``. Each gate short-
        circuits — RMS first (cheapest), WebRTC second (~0.1 ms C
        path), Silero last (~20 ms ONNX). The chain matches
        VoiceService._vad_loop so live and call see the same
        "is this speech?" verdict for any given frame."""
        np = self._np
        webrtcvad = self._webrtcvad
        silero = self._silero
        is_webrtc = self._webrtcvad_check
        is_silero = self._silero_check
        rms_threshold = self._rms_threshold
        stt_rate = self._stt_rate

        def check(data) -> bool:
            try:
                rms = float(np.sqrt(np.mean(np.square(data.astype(np.float32)))))
            except Exception:
                return True  # numpy issue — pass-through to keep stream alive
            if rms < rms_threshold:
                return False
            if webrtcvad is not None and not is_webrtc(data, stt_rate):
                return False
            if silero is not None and not is_silero(data, stt_rate):
                return False
            return True

        return check

    def _track_chitchat_e2e(self, user, t_voice_final, decision) -> None:
        """Spawn a daemon thread that waits for the queued chit-chat
        reply to finish playing through TTS, then emits one
        ``brain.chitchat.e2e`` log line with the full STT-final →
        speech-end wall-clock.

        Approach: poll ``tts.speaking`` (cheap bool flag). 60s safety
        cap covers long replies on slow synth. We don't try to break
        the latency into ttfa vs synth vs playback — the ``speaking``
        flag flips to True the moment ``speak_queue`` accepts (not
        when the first audio frame leaves the speaker), so any
        "first audio out" timing measured from this flag is wrong by
        the ElevenLabs synth TTFB. One honest end-to-end number
        beats two misleading sub-numbers."""
        if self._tts is None:
            return

        def _run():
            poll = 0.05
            end_deadline = time.time() + 60.0
            while time.time() < end_deadline and self._tts.speaking:
                time.sleep(poll)
            t_play_end = self._tts.last_spoken_time or time.time()
            total = t_play_end - t_voice_final
            logger.info(
                "brain.chitchat.e2e [%s] total=%.2fs (decide=%.2fs) reply=%r",
                user, total, decision.latency_s, decision.reply[:80],
            )

        threading.Thread(
            target=_run, daemon=True, name="brain-chitchat-e2e",
        ).start()
