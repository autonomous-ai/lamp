"""
Gemini Live brain — speech-in router that decides chit-chat vs task.

The model receives raw 16 kHz mic audio and is instructed to either:
  - speak directly back (chit-chat — audio chunks flow to the speaker), or
  - call the `delegate_to_lumi` function tool with the user's transcript
    (task — VoiceService forwards the transcript to OpenClaw exactly the
    way an STT final would be forwarded).

VoiceService is synchronous, the SDK is async. We bridge by running a
single asyncio loop in a dedicated thread per session; the public
BrainSession methods marshal across thread boundaries via call_soon_
threadsafe and a thread-safe close event.
"""

import asyncio
import logging
import os
import threading
from typing import Callable, Optional

from lelamp.service.brain.base import Brain, BrainSession
from lelamp.service.brain.context_loader import BrainContext, load_context

logger = logging.getLogger("lelamp.brain.gemini")

DEFAULT_MODEL = os.environ.get(
    "LELAMP_GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"
)
DEFAULT_VOICE = os.environ.get("LELAMP_GEMINI_LIVE_VOICE", "Aoede")
DEFAULT_LANGUAGE = os.environ.get("LELAMP_GEMINI_LIVE_LANGUAGE", "")

# Map lumi config's `stt_language` short codes onto Gemini Live BCP-47
# tags. Empty / "auto" / unknown → leave language_code unset so Gemini
# auto-detects from the audio (good for mixed-language households).
_STT_LANG_TO_BCP47 = {
    "vi": "vi-VN",
    "en": "en-US",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "ko": "ko-KR",
    "ja": "ja-JP",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-US",
    "pt": "pt-BR",
    "id": "id-ID",
    "th": "th-TH",
}


def _resolve_language(language: Optional[str]) -> str:
    """Pick a BCP-47 language tag for Gemini Live.

    Source priority:
      1. ``language`` arg passed to GeminiLiveBrain (explicit).
      2. ``LELAMP_GEMINI_LIVE_LANGUAGE`` env override.
      3. Lumi config's ``stt_language`` (so the brain follows the same
         language the classic STT pipeline was tuned for).
      4. Empty → leave unset so Gemini auto-detects.
    """
    candidate = (language or DEFAULT_LANGUAGE or "").strip()
    if not candidate:
        try:
            from lelamp.config import _lumi_cfg_get
            candidate = (_lumi_cfg_get("stt_language") or "").strip()
        except Exception:
            candidate = ""
    if not candidate or candidate.lower() == "auto":
        return ""
    return _STT_LANG_TO_BCP47.get(candidate.lower(), candidate)

# How the chit-chat reply is rendered to the user:
#   "native"   — Gemini Live streams 24 kHz PCM out, we play it directly.
#   "fallback" — Gemini Live emits text, we hand it to the existing
#                TTSService so the user keeps the same ElevenLabs/OpenAI
#                voice as task replies.
DEFAULT_TTS_OUTPUT_MODE = os.environ.get("LELAMP_BRAIN_TTS", "native").strip().lower()
VALID_TTS_OUTPUT_MODES = ("native", "fallback")

DELEGATE_TOOL_NAME = "delegate_to_lumi"

# The rules block always comes first; SOUL.md + recent turns are appended
# by context_loader. Kept short — the model holds it for the whole session
# and long prompts inflate first-token latency on Live.
DECISION_RULES = """\
You are Lumi — a smart, warm voice assistant living in a lamp.

Your DEFAULT is to chat with the user in your own voice. Almost
everything they say is chit-chat. Reply briefly (1–2 short sentences) in
the user's language, in the character described below.

ONLY call the `delegate_to_lumi(transcript=<verbatim>)` tool when the
user CLEARLY asks for a concrete action that needs a skill — turning
devices on/off, setting reminders, looking up real-time info (weather,
prices, email, who's home), playing music, telling long stories.
Examples that DO delegate:
  "bật đèn ngủ", "tắt nhạc", "nhắc tôi 5 phút nữa", "giá BTC hôm nay",
  "mở camera", "kể chuyện cười dài".

Examples that DO NOT delegate (these are chit-chat — reply in voice):
  greetings ("hello", "ê Lumi", "야"), short acknowledgements ("vâng",
  "ok", "à"), questions about you ("tên là gì?", "bạn khỏe không?"),
  reactions ("đẹp ha", "vui ghê"), comments overheard, single words,
  garbled audio, and questions about our conversation itself
  ("nãy giờ mình nói gì?" — answer from your own session memory).

When unsure → CHIT-CHAT. Never speak AND call the tool in the same turn.

Your spoken reply is plain prose only. Never include operator markup —
no `[HW:/...]`, no `/emotion ...`, no `[emotion: ...]`, no JSON blobs.
Voice-style markers like `[chuckle]`, `[laughs softly]`, `[sigh]` are
fine.

**IMPORTANT — about the SOUL block below.** The persona description
below is shared with a bigger Lumi system that has many skills (music,
sensing, posture, wellbeing, /emotion physical control, etc.). YOU are
only the voice front-door of that system. So:
  - Lumi can *do* all the things SOUL describes — you can mention them
    conversationally ("I can play music for you", "I can dim the light").
  - BUT you cannot trigger any of them yourself. To actually do them,
    call `delegate_to_lumi(transcript=…)` — the bigger Lumi will run the
    skill and reply on its own.
  - Ignore any SOUL rule that asks you to emit `/emotion`, `/servo`,
    `/led`, `[sensing:…]`, or any slash/bracket command. Those are
    operator-side and forbidden in YOUR spoken reply.
  - SOUL's mandatory `/emotion before you speak` does NOT apply to you —
    you don't have direct hardware. Replace it with a voice-style marker
    like `[chuckle]` instead.
"""


class GeminiLiveBrain(Brain):
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
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._model = model
        self._voice = voice
        self._language = _resolve_language(language)
        self._decision_rules = decision_rules
        self._context = context  # if None, loaded lazily per session (always fresh)
        if tts_output_mode not in VALID_TTS_OUTPUT_MODES:
            logger.warning("unknown tts_output_mode=%r — defaulting to 'native'", tts_output_mode)
            tts_output_mode = "native"
        self._tts_output_mode = tts_output_mode
        self._client = None
        self._types = None
        self._import_error: Optional[Exception] = None

        if not self._api_key:
            logger.warning("GeminiLiveBrain: no API key (GEMINI_API_KEY) — brain disabled")
            return

        try:
            from google import genai
            from google.genai import types
            self._client = genai.Client(api_key=self._api_key)
            self._types = types
            logger.info(
                "GeminiLiveBrain ready (model=%s, voice=%s, lang=%s, tts=%s)",
                self._model, self._voice, self._language, self._tts_output_mode,
            )
        except ImportError as e:
            self._import_error = e
            logger.warning("google-genai not installed — `pip install google-genai`")
        except Exception as e:
            self._import_error = e
            logger.warning("GeminiLiveBrain init failed: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None and self._types is not None

    def create_session(self) -> BrainSession:
        ctx = self._context or load_context()
        system_instruction = self._build_system_instruction(ctx)
        return GeminiLiveSession(
            client=self._client,
            types=self._types,
            model=self._model,
            voice=self._voice,
            language=self._language,
            system_instruction=system_instruction,
            tts_output_mode=self._tts_output_mode,
        )

    def _build_system_instruction(self, ctx: BrainContext) -> str:
        block = ctx.to_system_prompt_block()
        if block:
            return f"{self._decision_rules}\n\n{block}"
        return self._decision_rules


class GeminiLiveSession(BrainSession):
    """One streaming session. Owns a dedicated asyncio loop + thread.

    Lifecycle:
        start()        — spawn thread, wait for SETUP_TIMEOUT or setup_complete
        send_audio()   — non-blocking, marshals chunk into the loop
        close()        — set the close event, join the thread
    """

    SETUP_TIMEOUT_S = 5.0
    AUDIO_QUEUE_MAX = 256  # ~16 s of 16 kHz PCM at 64 ms frames — drop if model stalls

    def __init__(
        self,
        client,
        types,
        model: str,
        voice: str,
        language: str,
        system_instruction: str,
        tts_output_mode: str = "native",
    ):
        self._client = client
        self._types = types
        self._model = model
        self._voice = voice
        self._language = language
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

    def start(
        self,
        on_delegate: Callable[[str], None],
        on_audio_chunk: Callable[[bytes], None],
        on_text: Optional[Callable[[str, bool], None]] = None,
        on_user_input: Optional[Callable[[str, bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> bool:
        self._on_delegate = on_delegate
        self._on_audio_chunk = on_audio_chunk
        self._on_text = on_text
        self._on_user_input = on_user_input
        self._on_error = on_error

        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="brain-gemini-live"
        )
        self._thread.start()

        if not self._setup_done.wait(timeout=self.SETUP_TIMEOUT_S):
            logger.warning("Gemini Live setup did not complete within %.1fs", self.SETUP_TIMEOUT_S)
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
            logger.warning("Gemini Live session crashed: %s", e)
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

        config = self._build_config()
        try:
            async with self._client.aio.live.connect(
                model=self._model, config=config
            ) as session:
                self._setup_done.set()
                logger.info("Gemini Live session open (model=%s)", self._model)
                send_task = asyncio.create_task(self._send_loop(session), name="brain-send")
                recv_task = asyncio.create_task(self._recv_loop(session), name="brain-recv")
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
            logger.info("Gemini Live session closed")

    def _build_config(self):
        types = self._types
        function_decl = types.FunctionDeclaration(
            name=DELEGATE_TOOL_NAME,
            description=(
                "Delegate the user's request to the Lumi backend (OpenClaw). "
                "Call this for any request that needs an action, tool, lookup, "
                "schedule, or long-form answer. Do not speak when calling this."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "transcript": types.Schema(
                        type=types.Type.STRING,
                        description="Exact transcript of what the user just said.",
                    ),
                },
                required=["transcript"],
            ),
        )

        # Gemini Live's `gemini-3.1-flash-live-preview` only supports AUDIO
        # response modality — TEXT-only sessions get a 1011 "Internal
        # error encountered" handshake failure. So we always request AUDIO;
        # what differs by mode is what we do with the response:
        #
        #   native   → play PCM chunks via PCMAudioSink (speaker).
        #   fallback → drop PCM chunks; speak the transcript through our
        #              own TTSService (ElevenLabs) for a single voice.
        #
        # output_audio_transcription is enabled in BOTH modes so the Flow
        # Monitor can show *what* Lumi just said even when audio plays
        # natively. input_audio_transcription gives us the user's text.
        speech_kwargs = dict(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice),
            ),
        )
        # Only set language_code when we have a confident hint. Leaving
        # it unset tells Gemini "auto-detect" — better for households
        # where users switch languages mid-conversation.
        if self._language:
            speech_kwargs["language_code"] = self._language

        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=self._system_instruction)]
            ),
            tools=[types.Tool(function_declarations=[function_decl])],
            speech_config=types.SpeechConfig(**speech_kwargs),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

    async def _send_loop(self, session) -> None:
        types = self._types
        assert self._audio_queue is not None
        while not self._closed:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            try:
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
            except Exception as e:
                logger.warning("Gemini Live send failed: %s", e)
                raise

    async def _recv_loop(self, session) -> None:
        # `session.receive()` is a *per-turn* generator: it yields events
        # until `turn_complete` and then ends, even though the WebSocket
        # is still open. To keep one Gemini Live session across many
        # turns we restart the iterator each time it falls out — only
        # break when the session is explicitly closed or the SDK raises.
        while not self._closed:
            try:
                async for response in session.receive():
                    if self._closed:
                        return
                    if getattr(response, "tool_call", None):
                        await self._handle_tool_call(session, response.tool_call)
                        continue
                    self._handle_server_content(response)
                    self._log_usage(response)
            except Exception as e:
                logger.warning("Gemini Live recv failed: %s", e)
                raise
            # receive() ended naturally → next turn awaits.

    def _log_usage(self, response) -> None:
        """Emit a single-line token tally when the SDK surfaces usage
        metadata. Grep ``brain.usage`` in journalctl to see per-turn cost.
        Cumulative totals are maintained for the lifetime of this
        GeminiLiveSession (i.e. one WS connection)."""
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        prompt = getattr(usage, "prompt_token_count", None) or 0
        resp = getattr(usage, "response_token_count", None) or 0
        total = getattr(usage, "total_token_count", None) or (prompt + resp)
        if not total:
            return
        self._tokens_prompt = getattr(self, "_tokens_prompt", 0) + prompt
        self._tokens_response = getattr(self, "_tokens_response", 0) + resp
        self._tokens_total = getattr(self, "_tokens_total", 0) + total
        logger.info(
            "brain.usage  +prompt=%d +response=%d +total=%d  cumulative=%d (prompt=%d response=%d)",
            prompt, resp, total,
            self._tokens_total, self._tokens_prompt, self._tokens_response,
        )

    async def _handle_tool_call(self, session, tool_call) -> None:
        """Fire on_delegate, then ACK the tool call so Gemini doesn't
        stall waiting for a response. Session stays open for the next
        turn — keeping it alive avoids the ~700 ms reconnect + context
        reload tax we paid per utterance before."""
        types = self._types
        function_responses = []
        for fc in tool_call.function_calls or []:
            if fc.name != DELEGATE_TOOL_NAME:
                logger.info("ignoring unknown tool call: %s", fc.name)
                continue
            transcript = (fc.args or {}).get("transcript", "").strip()
            if transcript:
                logger.info("delegate_to_lumi → %r", transcript)
                if self._on_delegate is not None:
                    try:
                        self._on_delegate(transcript)
                    except Exception as e:
                        logger.warning("on_delegate callback raised: %s", e)
            else:
                logger.info("delegate_to_lumi called with empty transcript — ignoring")
            function_responses.append(types.FunctionResponse(
                name=fc.name,
                id=fc.id,
                response={"result": "delegated"},
            ))
        if function_responses:
            try:
                await session.send_tool_response(function_responses=function_responses)
            except Exception as e:
                logger.warning("send_tool_response failed: %s", e)

    def _handle_server_content(self, response) -> None:
        content = getattr(response, "server_content", None)
        if content is None:
            return

        native = self._tts_output_mode == "native"

        # Audio chunks → speaker, but only in native mode. In fallback we
        # drop them on purpose; the transcription gives us the same words
        # and the caller speaks them via ElevenLabs for one consistent voice.
        if native:
            turn = getattr(content, "model_turn", None)
            if turn is not None:
                for part in turn.parts or []:
                    inline = getattr(part, "inline_data", None)
                    if inline is not None and getattr(inline, "data", None):
                        if self._on_audio_chunk is not None:
                            try:
                                self._on_audio_chunk(inline.data)
                            except Exception as e:
                                logger.warning("on_audio_chunk callback raised: %s", e)

        # Brain-reply transcription — what Gemini just said. Used to log
        # the reply (both modes) and to TTS via ElevenLabs (fallback).
        out_tr = getattr(content, "output_transcription", None)
        if out_tr is not None:
            text = getattr(out_tr, "text", None) or ""
            if text and self._on_text is not None:
                try:
                    self._on_text(text, False)
                except Exception:
                    pass

        # User-input transcription — what the brain heard the user say.
        # Emitted incrementally while user is speaking; the caller uses
        # this to log the user turn and trigger per-turn speaker ID.
        in_tr = getattr(content, "input_transcription", None)
        if in_tr is not None:
            text = getattr(in_tr, "text", None) or ""
            if text and self._on_user_input is not None:
                try:
                    self._on_user_input(text, False)
                except Exception:
                    pass

        if getattr(content, "turn_complete", False):
            # turn_complete fires once Gemini finished its reply — at
            # that point both transcripts (user input and brain reply)
            # are settled, so we surface a "final" event on each.
            if self._on_text is not None:
                try:
                    self._on_text("", True)
                except Exception:
                    pass
            if self._on_user_input is not None:
                try:
                    self._on_user_input("", True)
                except Exception:
                    pass
