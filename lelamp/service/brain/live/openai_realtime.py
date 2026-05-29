"""
OpenAI Realtime brain — speech-in router that decides chit-chat vs task.

Wire shape mirrors :mod:`lelamp.service.brain.gemini_live` exactly: the
factory hands VoiceService a :class:`BrainSession`, which spawns a thread
hosting one asyncio loop that owns a single Realtime WebSocket. Mic
frames are marshalled in via ``send_audio``; the model decides to either:

  - speak directly back (chit-chat — PCM audio chunks flow to the speaker), or
  - call the ``delegate_to_lamp`` function tool with the user's transcript
    (task — VoiceService forwards the transcript to OpenClaw exactly the
    way an STT final would be forwarded).

Audio plumbing
--------------
* Mic capture: 16 kHz PCM int16 mono (matches VoiceService output).
* OpenAI Realtime expects: 24 kHz PCM int16 mono. We resample 16 → 24
  with ``scipy.signal.resample_poly(up=3, down=2)`` before base64-encoding
  each chunk. Polyphase resampling is mono-frame-rate cheap on the Pi.
* Audio out: 24 kHz PCM int16 mono — dropped (we use ElevenLabs via
  the runner's text path). The native audio stream is silenced via
  ``output_modalities=["text"]`` whenever supported.

Provider switch
---------------
Selection happens in :mod:`lelamp.service.brain.factory` —
``LELAMP_BRAIN_PROVIDER=openai`` activates this brain. Tunables:

  ``OPENAI_API_KEY``                 — required.
  ``LELAMP_OPENAI_REALTIME_MODEL``   — default ``gpt-realtime``.
  ``LELAMP_OPENAI_REALTIME_VOICE``   — default ``alloy``.
  ``LELAMP_BRAIN_TTS``               — ``native`` (default) | ``fallback``,
                                       shared with Gemini brain.
"""

import asyncio
import time
import base64
import json
import logging
import os
import threading
from typing import Any, Callable, Optional

from lelamp.service.brain.live.base import Brain, BrainSession
from lelamp.service.brain.context_loader import BrainContext, load_context
from lelamp.service.brain.prompts import (
    DECISION_RULES_LIVE,
    DELEGATE_TOOL_DESCRIPTION,
    DELEGATE_TOOL_NAME,
    WAIT_FOR_USER_TOOL_DESCRIPTION,
    WAIT_FOR_USER_TOOL_NAME,
    language_hint,
    resolve_stt_language,
)

logger = logging.getLogger("lelamp.brain.openai")

DEFAULT_MODEL = os.environ.get("LELAMP_OPENAI_REALTIME_MODEL", "gpt-realtime")
DEFAULT_VOICE = os.environ.get("LELAMP_OPENAI_REALTIME_VOICE", "alloy")
# Optional explicit override — if blank, fall back to lamp config's
# stt_language (same source classic STT + Gemini brain use).
DEFAULT_LANGUAGE = os.environ.get("LELAMP_OPENAI_REALTIME_LANGUAGE", "")
# ASR model for the Realtime input transcription. `whisper-1` is the
# legacy default and hallucinates language-common phrases on silence
# / low-information audio (Vietnamese: "Hẹn gặp lại các bạn trong
# những video tiếp theo", English: "Thanks for watching", etc.).
# `gpt-4o-mini-transcribe` is the newer ASR — same multilingual
# coverage but trained on cleaner data + less prone to those YouTube
# outro hallucinations. `gpt-4o-transcribe` is the bigger sibling
# (slightly better quality, ~2x cost).
# Default = whisper-1 (proven, but hallucinates outro phrases on silence).
# Set LELAMP_OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe (or
# gpt-4o-transcribe) once verified working on your account — those
# newer ASR models hallucinate less but are gated behind feature flags
# on some OpenAI tiers and the Realtime API silently disables
# transcription if it doesn't accept the model name.
DEFAULT_TRANSCRIBE_MODEL = os.environ.get(
    "LELAMP_OPENAI_TRANSCRIBE_MODEL", "whisper-1"
)

# Output rendering: hard-wired to ElevenLabs via LiveBrainRunner.
# OpenAI Realtime still streams 24 kHz PCM out (we have no text-only
# realtime tier), we drop the chunks in _handle_response_audio_delta
# and rely on the transcript events for the words we feed into
# ElevenLabs. Previous LELAMP_BRAIN_TTS env var (native | fallback)
# removed — companion deployments want one voice everywhere.

# OpenAI Realtime uses 24 kHz mono PCM16 on both directions. VoiceService
# captures 16 kHz so we polyphase-resample 16 → 24 (up=3, down=2) before
# sending.
INPUT_RATE_HZ = 24_000
MIC_RATE_HZ = 16_000


class OpenAIRealtimeBrain(Brain):
    """Factory holding the shared client + system prompt for the session."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        language: str = DEFAULT_LANGUAGE,
        context: Optional[BrainContext] = None,
        decision_rules: str = DECISION_RULES_LIVE,
        workspace=None,
    ):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._model = model
        self._voice = voice
        # Language source priority (matches GeminiLiveBrain):
        #   1. ``language`` arg passed to constructor (explicit override)
        #   2. ``LELAMP_OPENAI_REALTIME_LANGUAGE`` env var
        #   3. lamp config's ``stt_language``
        #   4. empty → no hint, model auto-detects
        # OpenAI Realtime has no per-API language field — the hint goes
        # straight into the system prompt via prompts.language_hint().
        self._language = (language or DEFAULT_LANGUAGE or resolve_stt_language() or "").strip()
        self._decision_rules = decision_rules
        self._context = context  # if None, loaded lazily per session (always fresh)
        # Per-provider BrainWorkspace. When set, create_session() seeds
        # load_context() with this workspace's session dir so chit-chat
        # from previous WS sessions (same provider) feeds back into the
        # next session's system prompt. See gemini_live.py for the same
        # mechanism. Typed loose to avoid an import cycle.
        self._workspace = workspace
        self._client = None
        self._import_error: Optional[Exception] = None

        if not self._api_key:
            logger.warning("OpenAIRealtimeBrain: no API key (OPENAI_API_KEY) — brain disabled")
            return

        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key)
            logger.info(
                "OpenAIRealtimeBrain ready (model=%s, voice=%s, lang=%s, tts=elevenlabs)",
                self._model, self._voice, self._language or "auto",
            )
        except ImportError as e:
            self._import_error = e
            logger.warning("openai SDK not installed — `pip install openai`")
        except Exception as e:
            self._import_error = e
            logger.warning("OpenAIRealtimeBrain init failed: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None

    def create_session(self) -> BrainSession:
        extra_dir = None
        if self._workspace is not None and self._workspace.session.enabled:
            d = self._workspace.session.dir
            if d is not None:
                extra_dir = str(d)
        ctx = self._context or load_context(extra_session_dir=extra_dir)
        system_instruction = self._build_system_instruction(ctx)
        return OpenAIRealtimeSession(
            client=self._client,
            model=self._model,
            voice=self._voice,
            language=self._language,
            system_instruction=system_instruction,
        )

    def _build_system_instruction(self, ctx: BrainContext) -> str:
        parts: list[str] = [self._decision_rules]
        hint = language_hint(self._language)
        if hint:
            parts.append(hint)
        block = ctx.to_system_prompt_block()
        if block:
            parts.append(block)
        return "\n\n".join(parts)


class OpenAIRealtimeSession(BrainSession):
    """One streaming session. Owns a dedicated asyncio loop + thread.

    Lifecycle:
        start()        — spawn thread, wait for SETUP_TIMEOUT or session ready
        send_audio()   — non-blocking, marshals chunk into the loop
        close()        — set the close event, join the thread
    """

    SETUP_TIMEOUT_S = 5.0
    AUDIO_QUEUE_MAX = 256  # ~16 s of 16 kHz PCM at 64 ms frames — drop if model stalls

    def __init__(
        self,
        client,
        model: str,
        voice: str,
        system_instruction: str,
        language: str = "",
    ):
        self._client = client
        self._model = model
        self._voice = voice
        self._language = language
        self._system_instruction = system_instruction

        # State shared across threads — guarded by simple flags + Events.
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._audio_queue: Optional[asyncio.Queue] = None
        self._close_event: Optional[asyncio.Event] = None
        self._setup_done = threading.Event()
        self._closed = False

        # Callbacks installed by start()
        self._on_delegate: Optional[Callable[[str], None]] = None
        self._on_audio_chunk: Optional[Callable[[bytes], None]] = None
        self._on_text: Optional[Callable[[str, bool], None]] = None
        self._on_user_input: Optional[Callable[[str, bool], None]] = None
        self._on_error: Optional[Callable[[Exception], None]] = None
        self._on_usage: Optional[Callable[[int, int, int], None]] = None

        # Live connection handle — populated inside _run() once the WS
        # handshake completes, cleared on close. Exposed so
        # ``send_context_turns`` can schedule mid-session item.create
        # events from any thread.
        self._conn: Optional[object] = None

        # call_id → function name, filled when response.output_item.added
        # arrives for a function_call item. Needed because
        # response.function_call_arguments.{delta,done} only carry call_id
        # + arguments; the name is established by the parent item event.
        self._fn_names: dict[str, str] = {}

        # Cumulative token tally — see _log_usage.
        self._tokens_input = 0
        self._tokens_output = 0
        self._tokens_total = 0

        # Lazy resampler — imported once on first send.
        self._resample_poly = None
        self._np = None

    def start(
        self,
        on_delegate: Callable[[str], None],
        on_audio_chunk: Callable[[bytes], None],
        on_text: Optional[Callable[[str, bool], None]] = None,
        on_user_input: Optional[Callable[[str, bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_usage: Optional[Callable[[int, int, int], None]] = None,
    ) -> bool:
        self._on_delegate = on_delegate
        self._on_audio_chunk = on_audio_chunk
        self._on_text = on_text
        self._on_user_input = on_user_input
        self._on_error = on_error
        self._on_usage = on_usage

        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="brain-openai-realtime"
        )
        self._thread.start()

        if not self._setup_done.wait(timeout=self.SETUP_TIMEOUT_S):
            logger.warning("OpenAI Realtime setup did not complete within %.1fs", self.SETUP_TIMEOUT_S)
            self.close()
            return False
        return not self._closed

    def send_audio(self, pcm16k_bytes: bytes) -> None:
        if self._closed or self._loop is None or self._audio_queue is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue_audio, pcm16k_bytes)
        except RuntimeError:
            # Loop already closed — treat as silent drop
            pass

    def _enqueue_audio(self, chunk: bytes) -> None:
        """Runs inside the asyncio loop — bounded to avoid runaway memory if
        the model stalls and the recv side stops draining."""
        assert self._audio_queue is not None
        if self._audio_queue.qsize() >= self.AUDIO_QUEUE_MAX:
            try:
                self._audio_queue.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass
        self._audio_queue.put_nowait(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and self._close_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._close_event.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
            self._thread = None

    def is_closed(self) -> bool:
        return self._closed

    # ----- thread / asyncio internals --------------------------------------

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as e:
            logger.warning("OpenAI Realtime session crashed: %s", e)
            if self._on_error is not None:
                try:
                    self._on_error(e)
                except Exception:
                    pass
        finally:
            self._closed = True
            self._setup_done.set()  # unblock start() if we died before setup

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._audio_queue = asyncio.Queue()
        self._close_event = asyncio.Event()

        try:
            # GA shape — `client.realtime.connect`, NOT `.beta.realtime`.
            # The beta endpoint started rejecting handshakes with
            # 4000 `beta_api_shape_disabled` once GA shipped.
            async with self._client.realtime.connect(model=self._model) as conn:
                self._conn = conn
                config = self._build_session_config()
                # Log the wire payload at a friendly level so we can
                # verify: which transcribe model the server actually
                # uses, language lock, voice, tool wiring. If the user
                # sees a turn fire with no brain.input downstream, this
                # is the first place to look — a silent server reject
                # of `transcription.model` shows up as the next
                # `session.updated` not containing it.
                logger.info(
                    "OpenAI Realtime session.update sent — model=%s voice=%s lang=%s "
                    "transcription=%s tool_choice=%s",
                    self._model, self._voice, self._language or "auto",
                    config.get("audio", {}).get("input", {}).get("transcription"),
                    config.get("tool_choice"),
                )
                await conn.session.update(session=config)
                self._setup_done.set()
                logger.info("OpenAI Realtime session open (model=%s)", self._model)

                send_task = asyncio.create_task(self._send_loop(conn), name="brain-send")
                recv_task = asyncio.create_task(self._recv_loop(conn), name="brain-recv")
                close_task = asyncio.create_task(self._close_event.wait(), name="brain-close")

                done, pending = await asyncio.wait(
                    {send_task, recv_task, close_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Surface the first non-cancellation error if any
                for t in done:
                    if t is close_task:
                        continue
                    if t.cancelled():
                        continue
                    err = t.exception()
                    if err and self._on_error is not None:
                        try:
                            self._on_error(err)
                        except Exception:
                            pass
        finally:
            self._conn = None
            logger.info("OpenAI Realtime session closed")

    def request_response(self) -> None:
        """Explicitly fire ``response.create`` from any thread.

        Pairs with ``turn_detection.create_response = false`` — the
        server commits the input buffer and ships the transcription
        completion event, but stays quiet until the runner decides
        the transcript is worth a response. Filters out phantom
        triggers from Whisper hallucinating on TTS echo / room noise.

        Idempotent on closed sessions: silently drops if the WS is
        gone."""
        if self._loop is None or self._conn is None or self._closed:
            return

        async def _create():
            try:
                await self._conn.response.create()
            except Exception as e:
                logger.warning("response.create failed: %s", e)

        try:
            asyncio.run_coroutine_threadsafe(_create(), self._loop)
        except RuntimeError:
            pass

    def send_context_turns(
        self, turns: list[dict], then_request_response: bool = False
    ) -> None:
        """Inject extra conversation history into the live session.

        Sends one ``conversation.item.create`` event per turn through
        the open WS. OpenAI Realtime treats these as added context
        only — generation only happens when the caller sends
        ``response.create``. Set ``then_request_response=True`` to
        chain the create call inside the SAME async coroutine; this
        guarantees the model sees the freshly-pushed items in its
        context when it generates the reply (otherwise scheduling
        two separate coroutines via run_coroutine_threadsafe can
        race at asyncio await boundaries and ship response.create
        on the WS before the last item.create lands).

        ``turns`` is a list of ``{"role": "user"|"model", "text": ...}``
        dicts; ``"model"`` is normalised to OpenAI's ``"assistant"``.
        Marshals onto the session's asyncio loop via
        ``run_coroutine_threadsafe`` so it's safe to call from the
        VoiceService thread."""
        if not turns and not then_request_response:
            return
        if self._loop is None or self._conn is None or self._closed:
            return
        normalized = []
        for t in turns:
            text = (t.get("text") or "").strip()
            if not text:
                continue
            role = t.get("role") or "user"
            if role == "model":
                role = "assistant"
            normalized.append({"role": role, "text": text})
        # Only bail when there's nothing to push AND no chained
        # response.create — otherwise we'd swallow the trigger when
        # the caller wants a response without history.
        if not normalized and not then_request_response:
            return

        async def _push():
            t0 = time.time()
            pushed = 0
            try:
                for entry in normalized:
                    # Content type must match role: ``input_text`` for
                    # role=user (things the user said), ``output_text``
                    # for role=assistant (things the model previously
                    # said). The server rejects mismatches with
                    # ``"Invalid value: 'input_text'. Value must be
                    # 'output_text'."``.
                    content_type = (
                        "output_text"
                        if entry["role"] == "assistant"
                        else "input_text"
                    )
                    await self._conn.conversation.item.create(
                        item={
                            "type": "message",
                            "role": entry["role"],
                            "content": [{
                                "type": content_type,
                                "text": entry["text"],
                            }],
                        },
                    )
                    pushed += 1
                logger.info(
                    "brain.history.sync [live] pushed %d turn(s) in %.2fs",
                    pushed, time.time() - t0,
                )
            except Exception as e:
                logger.warning(
                    "brain.history.sync [live] failed after %d/%d turns "
                    "in %.2fs: %s", pushed, len(normalized),
                    time.time() - t0, e,
                )
            if then_request_response:
                try:
                    await self._conn.response.create()
                except Exception as e:
                    logger.warning(
                        "response.create (chained after sync) failed: %s", e,
                    )

        try:
            asyncio.run_coroutine_threadsafe(_push(), self._loop)
        except RuntimeError:
            # Loop closed mid-call — silent drop; runner will retry
            # on the next turn boundary.
            pass

    def _build_transcription_kwargs(self) -> dict[str, Any]:
        """Build the audio.input.transcription payload. Forces the ASR
        to the configured ISO-639-1 language when one is set; otherwise
        auto-detect. Model name comes from
        ``LELAMP_OPENAI_TRANSCRIBE_MODEL`` (default
        ``gpt-4o-mini-transcribe``) — see DEFAULT_TRANSCRIBE_MODEL for
        why we avoid ``whisper-1``."""
        kwargs: dict[str, Any] = {"model": DEFAULT_TRANSCRIBE_MODEL}
        if self._language and self._language.lower() not in ("auto", ""):
            kwargs["language"] = self._language.split("-")[0].lower()
        return kwargs

    def _build_session_config(self) -> dict[str, Any]:
        """Build the session.update payload for the GA Realtime API.

        Shape diverges from the legacy beta: audio settings nest under
        ``audio.input`` / ``audio.output`` (each with its own ``format``,
        plus transcription / turn_detection / voice respectively), and
        ``modalities`` is renamed to ``output_modalities``. The
        ``type: "realtime"`` discriminator is required (the alternative
        ``"transcription"`` opens a different no-audio-out session shape).

        Only PCM rate currently supported by the SDK's typed shape is
        24 kHz — VoiceService sends 16 kHz from the mic, so the send
        loop resamples each chunk with ``scipy.signal.resample_poly``.
        """
        # Reasoning effort (gpt-realtime-2 + later reasoning-class
        # models only): minimal | low (default) | medium | high | xhigh.
        # We default the field to ``minimal`` for a voice front-door —
        # the model spends fewer hidden thinking tokens before
        # emitting its first audio token, shrinking TTFB. Earlier
        # non-reasoning models (gpt-realtime, gpt-realtime-1.5)
        # silently ignore the field, so it's safe to send always.
        # Override per-deployment with
        # ``LELAMP_OPENAI_REALTIME_REASONING_EFFORT``; unset / empty
        # means "don't send the field, let the server pick its
        # default".
        reasoning_effort = os.environ.get(
            "LELAMP_OPENAI_REALTIME_REASONING_EFFORT", "minimal",
        ).strip()

        # Output modality. ``audio`` = server generates audio + paired
        # transcript (we drop the audio and play the transcript through
        # ElevenLabs). ``text`` = server only generates text — skips
        # audio token generation entirely (~4× cheaper per output token
        # at GA pricing, and the average reply is dominated by output
        # tokens). Default = ``text`` because LiveBrainRunner routes
        # every reply through ElevenLabs via on_text; the audio chunks
        # were always silently dropped. Override with
        # ``LELAMP_LIVE_OPENAI_OUTPUT_MODALITY=audio`` if a downstream
        # eventually wants raw provider audio.
        output_modality = (
            os.environ.get("LELAMP_LIVE_OPENAI_OUTPUT_MODALITY", "text")
            .strip().lower()
            or "text"
        )
        if output_modality not in ("text", "audio"):
            logger.warning(
                "LELAMP_LIVE_OPENAI_OUTPUT_MODALITY=%r invalid — using 'text'",
                output_modality,
            )
            output_modality = "text"

        cfg: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": [output_modality],
            "instructions": self._system_instruction,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": INPUT_RATE_HZ},
                    # Whisper transcription. Setting `language` forces
                    # ASR to interpret input as that ISO-639-1 tongue —
                    # unlike Gemini Developer API's `language_codes`
                    # (which is Enterprise-only), Whisper accepts this
                    # on every tier. Strip the region tag because
                    # Whisper expects short codes (vi, not vi-VN). If
                    # `self._language` is empty/`auto`, fall back to
                    # auto-detect so multilingual households still work.
                    "transcription": self._build_transcription_kwargs(),
                    # server_vad commits the input buffer + fires the
                    # transcription completion event when speech ends,
                    # but we OPT OUT of auto-firing responses
                    # (``create_response: false``). The runner gates
                    # response.create itself in :meth:`request_response`
                    # so a phantom transcript (Whisper hallucination on
                    # TTS echo / room noise) doesn't burn tokens on a
                    # full audio response. ``interrupt_response: false``
                    # also keeps a generating response alive if new
                    # speech lands mid-reply — we don't support barge-in
                    # in live mode and cancellation just produces stub
                    # ``response.done status=cancelled`` events.
                    #
                    # VAD sensitivity (threshold / prefix_padding_ms /
                    # silence_duration_ms) is intentionally left unset so
                    # the Realtime API applies its own ``server_vad``
                    # defaults (currently 0.5 / 300ms / 500ms) — we track
                    # the provider's tuning instead of pinning it. NOTE:
                    # ``create_response`` / ``interrupt_response`` are NOT
                    # VAD tuning — they are the runner's gating contract
                    # and must stay set regardless of sensitivity.
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": False,
                    },
                },
            },
            "tools": [
                {
                    "type": "function",
                    "name": DELEGATE_TOOL_NAME,
                    "description": DELEGATE_TOOL_DESCRIPTION,
                    # No parameters — runner uses the side-channel
                    # ASR transcript automatically. Stops the model
                    # from hallucinating delegate transcripts pulled
                    # from recent history context.
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "type": "function",
                    "name": WAIT_FOR_USER_TOOL_NAME,
                    "description": WAIT_FOR_USER_TOOL_DESCRIPTION,
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            ],
            "tool_choice": "auto",
        }
        # Only attach the audio.output block when actually generating
        # audio. In text-only mode the field is pointless and some GA
        # API versions reject an `audio.output` config that contradicts
        # `output_modalities=["text"]`.
        if output_modality == "audio":
            cfg["audio"]["output"] = {
                "format": {"type": "audio/pcm", "rate": INPUT_RATE_HZ},
                "voice": self._voice,
            }
        if reasoning_effort:
            cfg["reasoning"] = {"effort": reasoning_effort}
        return cfg

    def _ensure_resampler(self) -> None:
        """Lazy-import scipy + numpy. Both already in lelamp deps; lazy
        load keeps the brain module importable on machines without scipy
        (rare but happens during early bring-up)."""
        if self._resample_poly is not None:
            return
        import numpy as np
        from scipy.signal import resample_poly
        self._np = np
        self._resample_poly = resample_poly

    def _upsample_16k_to_24k(self, pcm16k: bytes) -> bytes:
        """Resample 16 kHz int16 LE mono → 24 kHz int16 LE mono."""
        self._ensure_resampler()
        np = self._np
        arr = np.frombuffer(pcm16k, dtype=np.int16)
        if arr.size == 0:
            return b""
        up = self._resample_poly(arr.astype(np.float32), up=3, down=2)
        # Clip into int16 range before casting to avoid wrap-around on peaks.
        up = np.clip(up, -32768, 32767).astype(np.int16)
        return up.tobytes()

    async def _send_loop(self, conn) -> None:
        assert self._audio_queue is not None
        while not self._closed:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            try:
                chunk_24k = self._upsample_16k_to_24k(chunk)
                if not chunk_24k:
                    continue
                audio_b64 = base64.b64encode(chunk_24k).decode("ascii")
                await conn.input_audio_buffer.append(audio=audio_b64)
            except Exception as e:
                logger.warning("OpenAI Realtime send failed: %s", e)
                raise

    async def _recv_loop(self, conn) -> None:
        """One Realtime WebSocket session yields events for the lifetime of
        the connection — unlike Gemini Live there is no per-turn iterator
        wrap-around. We just dispatch by event.type until close."""
        try:
            async for event in conn:
                if self._closed:
                    return
                etype = getattr(event, "type", None)
                if etype is None:
                    continue
                handler = _EVENT_HANDLERS.get(etype)
                if handler is not None:
                    try:
                        await handler(self, conn, event)
                    except Exception as e:
                        logger.warning("OpenAI Realtime handler %s raised: %s", etype, e)
        except Exception as e:
            logger.warning("OpenAI Realtime recv failed: %s", e)
            raise

    # ----- per-event handlers ----------------------------------------------
    # Kept as small `async` methods + a dispatch table so the recv loop
    # stays a one-line `async for` — easier to reason about than a 200-line
    # if/elif tree.

    async def _on_session_event(self, conn, event) -> None:
        # Promote to INFO + dump what the server echoed back. If a
        # transcription model name was silently rejected, the server's
        # session.updated will *not* include `transcription.model` in
        # the audio.input block — a 1-line diff vs what we sent.
        sess = getattr(event, "session", None)
        audio = getattr(sess, "audio", None) if sess is not None else None
        ai_input = getattr(audio, "input", None) if audio is not None else None
        echoed_transcription = getattr(ai_input, "transcription", None)
        echoed_turn_detection = getattr(ai_input, "turn_detection", None)
        echoed_voice = (
            getattr(getattr(audio, "output", None), "voice", None)
            if audio is not None else None
        )
        logger.info(
            "OpenAI Realtime %s — server echo: voice=%s transcription=%s "
            "turn_detection=%s",
            event.type, echoed_voice, echoed_transcription,
            echoed_turn_detection,
        )

    async def _on_response_audio_delta(self, conn, event) -> None:
        # Audio chunks intentionally dropped — LiveBrainRunner routes
        # everything through ElevenLabs via on_text. Kept as a no-op so
        # the response.audio.delta event handler still consumes the
        # event (otherwise the SDK queues them).
        return

    async def _on_response_text_delta(self, conn, event) -> None:
        delta = getattr(event, "delta", None) or ""
        if delta and self._on_text is not None:
            try:
                self._on_text(delta, False)
            except Exception:
                pass

    async def _on_response_text_done(self, conn, event) -> None:
        if self._on_text is not None:
            try:
                self._on_text("", True)
            except Exception:
                pass

    async def _on_user_transcript_delta(self, conn, event) -> None:
        delta = getattr(event, "delta", None) or ""
        if delta and self._on_user_input is not None:
            try:
                self._on_user_input(delta, False)
            except Exception:
                pass

    async def _on_user_transcript_done(self, conn, event) -> None:
        # ``.completed`` carries the full final transcript. Fire it
        # directly as the is_final event (text, True) — the runner
        # will then REPLACE its accumulated delta buffer with this
        # authoritative final, no double-counting. The previous
        # implementation also fired ``(text, False)`` first which
        # appended the full text on top of deltas, producing
        # "ItchyItchy" / "Em đi đi.Em đi đi." style duplicates.
        text = getattr(event, "transcript", None) or ""
        if self._on_user_input is not None:
            try:
                self._on_user_input(text, True)
            except Exception:
                pass

    async def _on_output_item_added(self, conn, event) -> None:
        """Record (call_id → name) so we know which tool the upcoming
        ``response.function_call_arguments.done`` event belongs to."""
        item = getattr(event, "item", None)
        if item is None:
            return
        if getattr(item, "type", None) != "function_call":
            return
        call_id = getattr(item, "call_id", None)
        name = getattr(item, "name", None)
        if call_id and name:
            self._fn_names[call_id] = name

    async def _on_function_call_done(self, conn, event) -> None:
        call_id = getattr(event, "call_id", None)
        name = getattr(event, "name", None) or self._fn_names.get(call_id or "", "")
        raw_args = getattr(event, "arguments", "") or ""
        if name == WAIT_FOR_USER_TOOL_NAME:
            # No-op tool — the model used it to acknowledge silence /
            # ASR hallucination instead of speaking. ACK it so the
            # server doesn't stall, but do NOT log it loudly (would
            # spam the journal with one line per quiet frame batch).
            logger.debug("wait_for_user invoked — staying silent")
            if call_id:
                try:
                    await conn.conversation.item.create(
                        item={
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps({"result": "waiting"}),
                        },
                    )
                except Exception as e:
                    logger.warning("wait_for_user ACK failed: %s", e)
            return
        if name != DELEGATE_TOOL_NAME:
            logger.info("ignoring unknown tool call: %s", name)
            return
        # Tool takes no arguments — the runner pulls the actual
        # user transcript from the ASR side-channel. Fire on_delegate
        # unconditionally so the runner can route this turn.
        logger.info("delegate_to_lamp signal received")
        if self._on_delegate is not None:
            try:
                self._on_delegate("")
            except Exception as e:
                logger.warning("on_delegate callback raised: %s", e)
        # ACK the tool call so the server doesn't stall the response. We
        # deliberately do NOT call response.create() afterwards: in the
        # delegate case the bigger Lamp will speak, so we want OpenAI to
        # stay quiet until the next user utterance.
        if call_id:
            try:
                await conn.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"result": "delegated"}),
                    },
                )
            except Exception as e:
                logger.warning("conversation.item.create (tool output) failed: %s", e)

    async def _on_response_done(self, conn, event) -> None:
        # Emit a turn marker even when no user transcription / no reply
        # text shows up downstream (often when Whisper finds the audio
        # too short or empty — turn quietly closes without firing
        # brain.input). Lets the operator see that the turn DID
        # complete on the server side; if there's no brain.input
        # afterwards, the issue is transcription-side, not session-side.
        response = getattr(event, "response", None)
        status = getattr(response, "status", "?") if response is not None else "?"
        output_items = getattr(response, "output", None) or []
        logger.info(
            "OpenAI Realtime response.done — status=%s output_items=%d",
            status, len(output_items),
        )
        self._log_usage(event)

    async def _on_error_event(self, conn, event) -> None:
        err = getattr(event, "error", None)
        msg = getattr(err, "message", None) or str(err)
        logger.warning("OpenAI Realtime error event: %s", msg)
        if self._on_error is not None:
            try:
                self._on_error(RuntimeError(msg))
            except Exception:
                pass

    def _log_usage(self, event) -> None:
        """Emit a single-line token tally when the SDK surfaces usage
        metadata. Grep ``brain.usage`` in journalctl to see per-turn cost.
        Cumulative totals are maintained for the lifetime of this session."""
        response = getattr(event, "response", None)
        usage = getattr(response, "usage", None) if response is not None else None
        if usage is None:
            return
        in_tok = getattr(usage, "input_tokens", None) or 0
        out_tok = getattr(usage, "output_tokens", None) or 0
        total = getattr(usage, "total_tokens", None) or (in_tok + out_tok)
        if not total:
            return
        self._tokens_input += in_tok
        self._tokens_output += out_tok
        self._tokens_total += total
        logger.info(
            "brain.usage  +input=%d +output=%d +total=%d  cumulative=%d (input=%d output=%d)",
            in_tok, out_tok, total,
            self._tokens_total, self._tokens_input, self._tokens_output,
        )
        if self._on_usage is not None:
            try:
                self._on_usage(in_tok, out_tok, total)
            except Exception as e:
                logger.warning("on_usage callback raised: %s", e)


# Event dispatch table — populated after the methods are defined so we
# can reference unbound functions. Keys are the GA Realtime API event
# type strings (the legacy beta names ``response.audio.delta`` etc. were
# renamed to ``response.output_audio.delta`` etc.). Values are the bound
# coroutine methods on :class:`OpenAIRealtimeSession`. To support a new
# event type, add a method above and a row here.
_EVENT_HANDLERS: dict[str, Callable[..., Any]] = {
    "session.created":   OpenAIRealtimeSession._on_session_event,
    "session.updated":   OpenAIRealtimeSession._on_session_event,
    # Audio + transcripts (GA names)
    "response.output_audio.delta":              OpenAIRealtimeSession._on_response_audio_delta,
    "response.output_audio_transcript.delta":   OpenAIRealtimeSession._on_response_text_delta,
    "response.output_audio_transcript.done":    OpenAIRealtimeSession._on_response_text_done,
    "response.output_text.delta":               OpenAIRealtimeSession._on_response_text_delta,
    "response.output_text.done":                OpenAIRealtimeSession._on_response_text_done,
    "conversation.item.input_audio_transcription.delta":
        OpenAIRealtimeSession._on_user_transcript_delta,
    "conversation.item.input_audio_transcription.completed":
        OpenAIRealtimeSession._on_user_transcript_done,
    # Tool routing
    "response.output_item.added":            OpenAIRealtimeSession._on_output_item_added,
    "response.function_call_arguments.done": OpenAIRealtimeSession._on_function_call_done,
    # End-of-turn + errors
    "response.done":                         OpenAIRealtimeSession._on_response_done,
    "error":                                 OpenAIRealtimeSession._on_error_event,
}
