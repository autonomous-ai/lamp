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
import time
from typing import Callable, Optional

from lelamp.service.brain.live.base import Brain, BrainSession
from lelamp.service.brain.context_loader import BrainContext, load_context
from lelamp.service.brain.prompts import (
    DECISION_RULES_LIVE,
    DELEGATE_TOOL_DESCRIPTION,
    DELEGATE_TOOL_NAME,
    language_hint,
)

logger = logging.getLogger("lelamp.brain.gemini")

# Gemini Live model selection.
#
# Default: `gemini-2.5-flash-live-preview`. The newer 3.1 model exists
# but restricts ``send_client_content`` to initial-seeding only — after
# the first turn, callers can only push text via ``send_realtime_input``
# which Gemini treats as a fresh user message. We rely on
# ``send_client_content(turn_complete=False)`` mid-session so brain
# (not Gemini) owns conversation history end-to-end (see live/README.md
# §"Why brain owns history"). 2.5 keeps that flexibility.
#
# Override via env if you want to A/B test 3.x or older 2.0-flash-live.
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

# Reply rendering: hard-wired to ElevenLabs (via TTSService.speak_queue
# in LiveBrainRunner). Gemini Live still emits AUDIO chunks because the
# 3.x Live models on Developer API don't support a text-only response
# modality — we drop the chunks and rely on output_audio_transcription
# for the words we feed into ElevenLabs. The previous LELAMP_BRAIN_TTS
# env var (native | fallback) is gone — companion deployments always
# want one consistent voice across both branches, no toggle needed.


class GeminiLiveBrain(Brain):
    """Factory holding the shared client + system prompt for the session."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        language: str = DEFAULT_LANGUAGE,
        context: Optional[BrainContext] = None,
        decision_rules: str = DECISION_RULES_LIVE,
    ):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._model = model
        self._voice = voice
        self._language = _resolve_language(language)
        self._decision_rules = decision_rules
        self._context = context  # if None, loaded lazily per session (always fresh)
        self._client = None
        self._types = None
        self._import_error: Optional[Exception] = None

        # Cross-session resumption handle. Gemini Live hard-caps each WS
        # at ~10-15 min — the server emits a session_resumption_update
        # event with a handle we can pass on the next connect to keep
        # the in-server conversation memory + skip re-billing the full
        # system_instruction + context. Updated by every running session
        # whenever the SDK reports a new resumable handle.
        self._resumption_handle: Optional[str] = None
        self._resumption_lock = threading.Lock()

        if not self._api_key:
            logger.warning("GeminiLiveBrain: no API key (GEMINI_API_KEY) — brain disabled")
            return

        try:
            from google import genai
            from google.genai import types
            self._client = genai.Client(api_key=self._api_key)
            self._types = types
            logger.info(
                "GeminiLiveBrain ready (model=%s, voice=%s, lang=%s, tts=elevenlabs)",
                self._model, self._voice, self._language,
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

    def _get_handle(self) -> Optional[str]:
        with self._resumption_lock:
            return self._resumption_handle

    def _set_handle(self, handle: Optional[str]) -> None:
        with self._resumption_lock:
            self._resumption_handle = handle

    def create_session(self) -> BrainSession:
        ctx = self._context or load_context()
        system_instruction = self._build_system_instruction(ctx)
        # Visibility log — dumps the recent-history turns we just baked
        # into the system instruction so the journal mirrors the
        # call-mode ``brain.context`` dumps. Live mode loads history
        # once per session (every ~10-15 min after Gemini GoAway), not
        # per turn — Gemini's in-session memory carries everything
        # spoken while the WS stays open.
        turns = list(ctx.recent_turns) if ctx and ctx.recent_turns else []
        logger.info(
            "brain.context [live] system=%d chars history_turns=%d (loaded at session start, "
            "Gemini keeps in-session memory after this)",
            len(system_instruction), len(turns),
        )
        for i, t in enumerate(turns):
            text = (t.text or "").strip()
            if not text:
                continue
            logger.info("brain.context [live] #%02d %s: %s", i, t.role, text)
        return GeminiLiveSession(
            client=self._client,
            types=self._types,
            model=self._model,
            voice=self._voice,
            language=self._language,
            system_instruction=system_instruction,
            resumption_handle=self._get_handle(),
            on_handle_update=self._set_handle,
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
        resumption_handle: Optional[str] = None,
        on_handle_update: Optional[Callable[[Optional[str]], None]] = None,
    ):
        self._client = client
        self._types = types
        self._model = model
        self._voice = voice
        self._language = language
        self._system_instruction = system_instruction
        self._resumption_handle = resumption_handle
        self._on_handle_update = on_handle_update

        # State shared across threads — guarded by simple flags + Events.
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._audio_queue: Optional[asyncio.Queue] = None
        self._close_event: Optional[asyncio.Event] = None
        self._setup_done = threading.Event()
        self._closed = False
        # Live SDK session handle. Populated in _run() once the WS is
        # open; consumed by send_audio (via the audio queue) and by
        # notify_activity_start/end (via call_soon_threadsafe).
        self._session = None

        # Callbacks installed by start()
        self._on_delegate: Optional[Callable[[str], None]] = None
        self._on_audio_chunk: Optional[Callable[[bytes], None]] = None
        self._on_text: Optional[Callable[[str, bool], None]] = None
        self._on_user_input: Optional[Callable[[str, bool], None]] = None
        self._on_error: Optional[Callable[[Exception], None]] = None
        self._on_usage: Optional[Callable[[int, int, int], None]] = None

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

    def send_context_turns(self, turns: list[dict]) -> None:
        """Inject extra conversation history into the live session
        without triggering a model response.

        ``turns`` is a list of ``{"role": "user"|"model", "text": ...}``
        dicts. The runner uses this to push OpenClaw turns (Telegram,
        web, other voice sessions) that landed while this voice
        session was open — keeps the live brain in sync with the
        rest of the system without restarting the WS.

        Requires the model to be ``gemini-2.5-flash-live-preview`` or
        newer with mid-session ``send_client_content`` support. On
        ``gemini-3.1-flash-live-preview`` the SDK will reject this
        call after the first model turn — error is logged, the
        runner falls back to "stale history until next reconnect".
        Sets ``turn_complete=False`` so the model treats this purely
        as context, not as a new user prompt.
        """
        if not turns:
            return
        if self._loop is None or self._session is None or self._closed:
            return
        types = self._types
        try:
            contents = [
                types.Content(
                    role="user" if t.get("role") == "user" else "model",
                    parts=[types.Part(text=t.get("text", ""))],
                )
                for t in turns
                if (t.get("text") or "").strip()
            ]
        except (AttributeError, TypeError) as e:
            logger.warning(
                "send_context_turns: SDK shape rejected the content (%s) — "
                "skipping sync", e,
            )
            return
        if not contents:
            return

        async def _push():
            t0 = time.time()
            try:
                await self._session.send_client_content(
                    turns=contents, turn_complete=False,
                )
                logger.info(
                    "brain.history.sync [live] pushed %d turn(s) in %.2fs",
                    len(contents), time.time() - t0,
                )
            except Exception as e:
                logger.warning(
                    "brain.history.sync [live] failed after %.2fs: %s",
                    time.time() - t0, e,
                )

        try:
            asyncio.run_coroutine_threadsafe(_push(), self._loop)
        except RuntimeError:
            # Loop closed mid-call — silent drop, will retry next turn.
            pass

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

    def notify_activity_start(self) -> None:
        self._dispatch_activity(start=True)

    def notify_activity_end(self) -> None:
        self._dispatch_activity(start=False)

    def _dispatch_activity(self, start: bool) -> None:
        if self._closed or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._send_activity(start))
            )
        except RuntimeError:
            # Loop already closed — silent drop.
            pass

    async def _send_activity(self, start: bool) -> None:
        """Send Gemini Live's manual activity_start/activity_end events.
        No-op when ``self._session`` is gone OR when the SDK doesn't
        expose ActivityStart/ActivityEnd (older google-genai versions),
        in which case the server is still on auto-VAD and our explicit
        events were already silently ignored."""
        session = self._session
        if session is None:
            return
        types = self._types
        try:
            if start:
                payload = types.ActivityStart()
                await session.send_realtime_input(activity_start=payload)
            else:
                payload = types.ActivityEnd()
                await session.send_realtime_input(activity_end=payload)
        except AttributeError:
            # Old SDK — types.ActivityStart / ActivityEnd missing.
            # Server is on auto-VAD anyway; just don't bother.
            return
        except Exception as e:
            # Don't escalate — session might be tearing down for an
            # unrelated reason and we don't want activity ack failures
            # to mask the real cause downstream.
            logger.debug("activity %s event failed: %s", "start" if start else "end", e)

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
        # t_connect captures WS handshake + Gemini setup time so the
        # journal shows how long a session restart actually costs.
        t_connect_start = time.time()
        self._session_start_ts: Optional[float] = None
        try:
            async with self._client.aio.live.connect(
                model=self._model, config=config
            ) as session:
                self._session = session  # exposed for notify_activity_*
                self._setup_done.set()
                connect_elapsed = time.time() - t_connect_start
                self._session_start_ts = time.time()
                logger.info(
                    "Gemini Live session open (model=%s) — connect took %.2fs",
                    self._model, connect_elapsed,
                )
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
            self._session = None
            lifetime = (
                time.time() - self._session_start_ts
                if self._session_start_ts is not None
                else 0.0
            )
            logger.info(
                "Gemini Live session closed (lifetime %.1fs)", lifetime,
            )

    def _build_config(self):
        types = self._types
        function_decl = types.FunctionDeclaration(
            name=DELEGATE_TOOL_NAME,
            description=DELEGATE_TOOL_DESCRIPTION,
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

        # Request AUDIO modality (the only one this model supports on
        # Developer API) plus output_audio_transcription so we get
        # text to feed into ElevenLabs. The PCM chunks themselves are
        # dropped in _handle_server_content — LiveBrainRunner only
        # cares about the transcript.
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
            input_audio_transcription=self._build_input_transcription(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            **self._build_resumption_kwargs(),
            **self._build_manual_activity_kwargs(),
        )

    def _build_manual_activity_kwargs(self) -> dict:
        """Server VAD path. Default behaviour: Gemini Live decides turn
        boundaries from the audio stream with its built-in VAD.

        Tunable via env vars (all optional — unset → SDK / server defaults):

          LELAMP_LIVE_VAD_SILENCE_MS
              ``silence_duration_ms`` for ``automatic_activity_detection``.
              Default per docs is 100ms which the same docs flag as too
              aggressive (fragments natural pauses). Recommend 500-800.

          LELAMP_LIVE_VAD_START_SENSITIVITY     low | high
          LELAMP_LIVE_VAD_END_SENSITIVITY       low | high
              Maps to ``StartSensitivity`` / ``EndSensitivity`` enums.
              ``low`` makes the detector less twitchy on background
              noise / breath; ``high`` matches the SDK default.

          LELAMP_LIVE_VAD_PREFIX_PADDING_MS
              ``prefix_padding_ms`` — how much pre-trigger audio Gemini
              keeps. Default 20ms.

        We never set ``disabled=True`` — the manual-signal path was
        retired (TTS echo gate is enough). Returns ``{}`` when no env
        var is set so the connection keeps the SDK defaults verbatim.
        """
        types = self._types
        silence_ms_raw = os.environ.get("LELAMP_LIVE_VAD_SILENCE_MS", "").strip()
        start_sens = os.environ.get("LELAMP_LIVE_VAD_START_SENSITIVITY", "").strip().lower()
        end_sens = os.environ.get("LELAMP_LIVE_VAD_END_SENSITIVITY", "").strip().lower()
        prefix_ms_raw = os.environ.get("LELAMP_LIVE_VAD_PREFIX_PADDING_MS", "").strip()

        # Bail early if nothing's configured.
        if not (silence_ms_raw or start_sens or end_sens or prefix_ms_raw):
            return {}

        kwargs: dict = {}
        if silence_ms_raw:
            try:
                kwargs["silence_duration_ms"] = int(silence_ms_raw)
            except ValueError:
                logger.warning("LELAMP_LIVE_VAD_SILENCE_MS=%r is not an int — ignored", silence_ms_raw)
        if prefix_ms_raw:
            try:
                kwargs["prefix_padding_ms"] = int(prefix_ms_raw)
            except ValueError:
                logger.warning("LELAMP_LIVE_VAD_PREFIX_PADDING_MS=%r is not an int — ignored", prefix_ms_raw)
        if start_sens:
            try:
                kwargs["start_of_speech_sensitivity"] = getattr(
                    types.StartSensitivity,
                    f"START_SENSITIVITY_{start_sens.upper()}",
                )
            except (AttributeError, TypeError) as e:
                logger.warning("VAD start sensitivity %r rejected by SDK: %s", start_sens, e)
        if end_sens:
            try:
                kwargs["end_of_speech_sensitivity"] = getattr(
                    types.EndSensitivity,
                    f"END_SENSITIVITY_{end_sens.upper()}",
                )
            except (AttributeError, TypeError) as e:
                logger.warning("VAD end sensitivity %r rejected by SDK: %s", end_sens, e)

        if not kwargs:
            return {}

        try:
            return {
                "realtime_input_config": types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(**kwargs),
                ),
            }
        except (AttributeError, TypeError) as e:
            logger.warning(
                "automatic_activity_detection unsupported by this SDK (%s) — "
                "falling back to server defaults", e,
            )
            return {}

    def _build_resumption_kwargs(self) -> dict:
        """Enable session resumption. Passing handle=None opts the
        connection into the feature so the server starts emitting
        ``session_resumption_update`` events; we capture those handles
        and on the next connect (after the ~10-min GoAway) we hand the
        most recent handle back so Gemini keeps the in-server
        conversation memory instead of re-billing the full
        system_instruction + history."""
        types = self._types
        try:
            cfg = types.SessionResumptionConfig(handle=self._resumption_handle)
            return {"session_resumption": cfg}
        except (AttributeError, TypeError) as e:
            # Older google-genai versions may not expose
            # SessionResumptionConfig — degrade gracefully (we'll still
            # auto-reconnect, just paying the full re-bill each cycle).
            logger.info("session resumption unsupported by this SDK (%s)", e)
            return {}

    def _build_input_transcription(self):
        """AudioTranscriptionConfig for incoming mic audio.

        Note: ``language_codes=[...]`` is tempting (hard Whisper lock)
        but is *Gemini Enterprise Agent Platform only* — passing it on
        a Developer API key crashes the session with::

            language_codes parameter is only supported in Gemini
            Enterprise Agent Platform mode, not in Gemini Developer
            API mode.

        The SDK accepts the kwarg client-side, the server rejects it at
        handshake. So we don't set it here — we lean on the
        ``language_hint(...)`` paragraph already in system_instruction
        plus ``speech_config.language_code`` (set in native mode only,
        which biases output language). That's the strongest combo the
        Developer tier allows."""
        types = self._types
        return types.AudioTranscriptionConfig()

    @staticmethod
    def _is_goaway_close(err: BaseException) -> bool:
        """True when the SDK raised because the server sent a GoAway +
        the WS was force-closed with 1008 ``policy violation``. That
        path is *expected* every ~10-15 min (Gemini Live hard session
        cap) — the outer loop reconnects with the latest resumption
        handle, so we log it at INFO level instead of WARNING so the
        journal isn't full of fake alarms."""
        msg = str(err)
        return "1008" in msg or "GoAway" in msg or "goaway" in msg.lower()

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
                if self._is_goaway_close(e):
                    logger.info(
                        "Gemini Live send: session expired (GoAway) — outer loop will reconnect"
                    )
                    return  # graceful: finish task without raising
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
                    # Update resumption handle FIRST so the latest valid
                    # handle is captured even if the server kicks us
                    # between this update and the next iteration.
                    self._handle_resumption_update(response)
                    self._handle_go_away(response)
                    self._handle_server_content(response)
                    self._log_usage(response)
            except Exception as e:
                if self._is_goaway_close(e):
                    logger.info(
                        "Gemini Live recv: session expired (GoAway) — outer loop will reconnect"
                    )
                    return  # graceful: finish task without raising
                logger.warning("Gemini Live recv failed: %s", e)
                raise

    def _handle_resumption_update(self, response) -> None:
        """Capture the latest resumable handle Gemini gives us. The
        server emits ``session_resumption_update`` periodically; only
        handles where ``resumable=True`` should be persisted (others
        are interim and would refuse on reconnect)."""
        update = getattr(response, "session_resumption_update", None)
        if update is None:
            return
        new_handle = getattr(update, "new_handle", None)
        resumable = getattr(update, "resumable", False)
        if resumable and new_handle and self._on_handle_update is not None:
            try:
                self._on_handle_update(new_handle)
            except Exception as e:
                logger.warning("on_handle_update callback raised: %s", e)

    def _handle_go_away(self, response) -> None:
        """Gemini Live sends ``go_away`` ~30 s before it force-closes the
        WS with 1008. Log it once so the journal explains why the next
        couple of seconds look quiet, then let the loop drain naturally
        — the outer reconnect path takes over.

        Some SDK versions surface go_away on `response.go_away`, others
        nest it under server_content; we check both."""
        for src in (response, getattr(response, "server_content", None)):
            if src is None:
                continue
            go_away = getattr(src, "go_away", None)
            if go_away is None:
                continue
            time_left = getattr(go_away, "time_left", None)
            logger.info(
                "Gemini Live: server GoAway received (time_left=%s) — will reconnect soon",
                time_left,
            )
            return
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
        if self._on_usage is not None:
            try:
                self._on_usage(prompt, resp, total)
            except Exception as e:
                logger.warning("on_usage callback raised: %s", e)

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

        # Audio chunks intentionally dropped — LiveBrainRunner routes
        # everything through ElevenLabs via on_text. The Live model
        # still produces PCM (the 3.x Live tier doesn't support a
        # text-only response modality on Developer API), we just
        # don't forward it. on_audio_chunk is still part of the Brain
        # interface so legacy callers don't break.

        # Brain-reply transcription — what Gemini just said. Used by
        # Flow Monitor logging in native mode and by TTSService route
        # in fallback mode.
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
            # Fire the user-input final FIRST so callers log
            # ``brain.input`` before ``brain.chitchat`` (user spoke,
            # then brain replied — easier to read the journal that way).
            if self._on_user_input is not None:
                try:
                    self._on_user_input("", True)
                except Exception:
                    pass
            if self._on_text is not None:
                try:
                    self._on_text("", True)
                except Exception:
                    pass
