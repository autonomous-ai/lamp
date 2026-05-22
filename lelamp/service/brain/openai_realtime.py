"""
OpenAI Realtime brain — speech-in router that decides chit-chat vs task.

Wire shape mirrors :mod:`lelamp.service.brain.gemini_live` exactly: the
factory hands VoiceService a :class:`BrainSession`, which spawns a thread
hosting one asyncio loop that owns a single Realtime WebSocket. Mic
frames are marshalled in via ``send_audio``; the model decides to either:

  - speak directly back (chit-chat — PCM audio chunks flow to the speaker), or
  - call the ``delegate_to_lumi`` function tool with the user's transcript
    (task — VoiceService forwards the transcript to OpenClaw exactly the
    way an STT final would be forwarded).

Audio plumbing
--------------
* Mic capture: 16 kHz PCM int16 mono (matches VoiceService output).
* OpenAI Realtime expects: 24 kHz PCM int16 mono. We resample 16 → 24
  with ``scipy.signal.resample_poly(up=3, down=2)`` before base64-encoding
  each chunk. Polyphase resampling is mono-frame-rate cheap on the Pi.
* Audio out: 24 kHz PCM int16 mono — matches :class:`PCMAudioSink` default
  rate, so no further resampling on the playback side.

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
import base64
import json
import logging
import os
import threading
from typing import Any, Callable, Optional

from lelamp.service.brain.base import Brain, BrainSession
from lelamp.service.brain.context_loader import BrainContext, load_context
from lelamp.service.brain.prompts import (
    DECISION_RULES,
    DELEGATE_TOOL_DESCRIPTION,
    DELEGATE_TOOL_NAME,
    language_hint,
    resolve_stt_language,
)

logger = logging.getLogger("lelamp.brain.openai")

DEFAULT_MODEL = os.environ.get("LELAMP_OPENAI_REALTIME_MODEL", "gpt-realtime")
DEFAULT_VOICE = os.environ.get("LELAMP_OPENAI_REALTIME_VOICE", "alloy")
# Optional explicit override — if blank, fall back to lumi config's
# stt_language (same source classic STT + Gemini brain use).
DEFAULT_LANGUAGE = os.environ.get("LELAMP_OPENAI_REALTIME_LANGUAGE", "")

# Output rendering — same env var as Gemini, same semantics:
#   "native"   — play OpenAI's PCM audio out the speaker directly.
#   "fallback" — drop the audio chunks; speak the transcribed reply via
#                the existing TTSService for a single consistent voice.
DEFAULT_TTS_OUTPUT_MODE = os.environ.get("LELAMP_BRAIN_TTS", "native").strip().lower()
VALID_TTS_OUTPUT_MODES = ("native", "fallback")

# OpenAI Realtime uses 24 kHz mono PCM16 on both directions. VoiceService
# captures 16 kHz so we polyphase-resample 16 → 24 (up=3, down=2) before
# sending. Output already matches PCMAudioSink's default rate.
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
        decision_rules: str = DECISION_RULES,
        tts_output_mode: str = DEFAULT_TTS_OUTPUT_MODE,
    ):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._model = model
        self._voice = voice
        # Language source priority (matches GeminiLiveBrain):
        #   1. ``language`` arg passed to constructor (explicit override)
        #   2. ``LELAMP_OPENAI_REALTIME_LANGUAGE`` env var
        #   3. lumi config's ``stt_language``
        #   4. empty → no hint, model auto-detects
        # OpenAI Realtime has no per-API language field — the hint goes
        # straight into the system prompt via prompts.language_hint().
        self._language = (language or DEFAULT_LANGUAGE or resolve_stt_language() or "").strip()
        self._decision_rules = decision_rules
        self._context = context  # if None, loaded lazily per session (always fresh)
        if tts_output_mode not in VALID_TTS_OUTPUT_MODES:
            logger.warning("unknown tts_output_mode=%r — defaulting to 'native'", tts_output_mode)
            tts_output_mode = "native"
        self._tts_output_mode = tts_output_mode
        self._client = None
        self._import_error: Optional[Exception] = None

        if not self._api_key:
            logger.warning("OpenAIRealtimeBrain: no API key (OPENAI_API_KEY) — brain disabled")
            return

        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key)
            logger.info(
                "OpenAIRealtimeBrain ready (model=%s, voice=%s, lang=%s, tts=%s)",
                self._model, self._voice, self._language or "auto", self._tts_output_mode,
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
        ctx = self._context or load_context()
        system_instruction = self._build_system_instruction(ctx)
        return OpenAIRealtimeSession(
            client=self._client,
            model=self._model,
            voice=self._voice,
            system_instruction=system_instruction,
            tts_output_mode=self._tts_output_mode,
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
        tts_output_mode: str = "native",
    ):
        self._client = client
        self._model = model
        self._voice = voice
        self._system_instruction = system_instruction
        self._tts_output_mode = tts_output_mode

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
                await conn.session.update(session=self._build_session_config())
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
            logger.info("OpenAI Realtime session closed")

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
        return {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": self._system_instruction,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": INPUT_RATE_HZ},
                    # whisper-1 is multilingual and the Realtime API's
                    # default — leaving language unset lets it auto-detect,
                    # which is the right call for households that switch
                    # languages mid-chat.
                    "transcription": {"model": "whisper-1"},
                    # server_vad lets OpenAI decide when a turn ends. The
                    # alternative (`turn_detection: None`) puts that on
                    # the client via input_audio_buffer.commit — we don't
                    # need it because VoiceService already gates mic
                    # frames during TTS playback.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": INPUT_RATE_HZ},
                    "voice": self._voice,
                },
            },
            "tools": [
                {
                    "type": "function",
                    "name": DELEGATE_TOOL_NAME,
                    "description": DELEGATE_TOOL_DESCRIPTION,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "transcript": {
                                "type": "string",
                                "description": "Exact transcript of what the user just said.",
                            },
                        },
                        "required": ["transcript"],
                    },
                },
            ],
            "tool_choice": "auto",
        }

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
        logger.debug("openai realtime session event: %s", event.type)

    async def _on_response_audio_delta(self, conn, event) -> None:
        if self._tts_output_mode != "native":
            return  # fallback mode plays the reply via TTSService, not raw PCM
        delta = getattr(event, "delta", None)
        if not delta:
            return
        try:
            pcm = base64.b64decode(delta)
        except Exception:
            return
        if pcm and self._on_audio_chunk is not None:
            try:
                self._on_audio_chunk(pcm)
            except Exception as e:
                logger.warning("on_audio_chunk callback raised: %s", e)

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
        # `.completed` carries the full final transcript; emit it as the
        # final user-input event so callers that buffered deltas can either
        # use deltas OR this single final string and end up with the same
        # text.
        text = getattr(event, "transcript", None) or ""
        if text and self._on_user_input is not None:
            try:
                self._on_user_input(text, False)
            except Exception:
                pass
        if self._on_user_input is not None:
            try:
                self._on_user_input("", True)
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
        if name != DELEGATE_TOOL_NAME:
            logger.info("ignoring unknown tool call: %s", name)
            return
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            logger.warning("delegate_to_lumi: bad JSON args: %r", raw_args)
            args = {}
        transcript = str(args.get("transcript", "")).strip()
        if transcript:
            logger.info("delegate_to_lumi → %r", transcript)
            if self._on_delegate is not None:
                try:
                    self._on_delegate(transcript)
                except Exception as e:
                    logger.warning("on_delegate callback raised: %s", e)
        else:
            logger.info("delegate_to_lumi called with empty transcript — ignoring")
        # ACK the tool call so the server doesn't stall the response. We
        # deliberately do NOT call response.create() afterwards: in the
        # delegate case the bigger Lumi will speak, so we want OpenAI to
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
