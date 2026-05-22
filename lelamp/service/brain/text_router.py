"""
Half-cascade text brain.

Replaces the audio realtime brain (Gemini Live / OpenAI Realtime) with
a much simpler architecture: let the classic STT pipeline (RMS gate +
Deepgram nova-3 WS) do its job to get a final transcript, then route
that *text* through a cheap chat-completion call (Gemini Flash / GPT-4o
mini) with function calling. The model picks one of two paths:

  - **chit-chat**: returns a short reply text → spoken via the existing
    TTSService (one consistent ElevenLabs voice across both paths).
  - **delegate**: calls ``delegate_to_lumi(transcript=…)`` → caller
    forwards the original transcript to OpenClaw via ``/api/sensing/
    event`` (classic flow unchanged).

Why this beats the audio-realtime brain:
  - STT is the part that already works (proven Deepgram pipeline).
  - Token cost ~10x cheaper (text in/out vs audio in/out on Live API).
  - No WS session lifecycle headache (GoAway, manual VAD, resumption).
  - Each call is one synchronous HTTP request — easy to log, retry,
    cancel.

Trade-off: the model loses audio nuance (tone, sigh, laughter pause).
For chit-chat decisions in Vietnamese voice-assistant land that hasn't
mattered enough to justify the realtime complexity.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lelamp.service.brain.context_loader import (
    DEFAULT_AGENTS_SUBDIR,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_SESSION_KEY,
    DEFAULT_WORKSPACE,
    DEFAULT_WORKSPACE_SUBDIR,
    _read_openclaw_history,
    load_context,
)
from lelamp.service.brain.prompts import (
    DECISION_RULES,
    DELEGATE_TOOL_DESCRIPTION,
    DELEGATE_TOOL_NAME,
    language_hint,
    resolve_stt_language,
)

logger = logging.getLogger("lelamp.brain.text")


# Env values that map to "no brain — go straight to OpenClaw".
_DISABLED_VALUES = frozenset({"", "none", "off", "classic", "disabled"})


@dataclass
class TextBrainDecision:
    """One brain turn's verdict.

    Attributes:
        decision: ``chitchat`` (brain replies directly), ``delegate``
            (forward to OpenClaw), or ``error`` (provider broken — caller
            should fall through to OpenClaw as a safe default).
        reply: populated when ``decision == "chitchat"``. The text
            TTSService should speak.
        transcript: echoed STT text — populated when ``decision ==
            "delegate"``. Usually identical to the input but the model is
            free to clean it up before delegating.
        latency_s: wall-clock time of the brain HTTP call.
        error: human-readable error string when ``decision == "error"``.
    """

    decision: str = "delegate"
    reply: str = ""
    transcript: str = ""
    latency_s: float = 0.0
    error: str = ""
    prompt_tokens: int = 0
    response_tokens: int = 0
    total_tokens: int = 0


def is_disabled(provider: Optional[str]) -> bool:
    return (provider or "").strip().lower() in _DISABLED_VALUES


class TextBrain:
    """Single class with a provider switch inside.

    Picked over a factory + abstract base because we only have two
    providers (Gemini + OpenAI) and they share ~80 % of the decision
    plumbing. If a third one shows up split on first sight of
    duplication."""

    SUPPORTED_PROVIDERS = ("gemini", "openai")

    def __init__(self, provider: str):
        self._provider = (provider or "").strip().lower()
        self._language = resolve_stt_language()
        self._client = None
        self._import_error: Optional[Exception] = None
        # provider-specific
        self._model: str = ""
        self._types = None
        # In-process session memory — chit-chat turns the brain handled
        # directly never reach OpenClaw, so without this they vanish
        # between turns and the brain forgets its own conversation.
        # Stores ``[{"role": "user"|"assistant", "text": "...",
        # "ts": float}, ...]`` in chronological order, capped at
        # SESSION_HISTORY_MAX turns (each turn = 2 entries).
        #
        # Also persisted to a JSONL file so a service restart no longer
        # wipes the chit-chat memory — we reload the last N entries on
        # init. Disable persistence by setting
        # LELAMP_BRAIN_SESSION_LOG=/dev/null (or unsetting the path
        # and pointing it at a non-writable location — both gracefully
        # degrade to in-memory only).
        #
        # Note for a future pass — "Option B" from the design discussion:
        # POST every chit-chat back to OpenClaw via /api/sensing/event
        # with a `brain_chitchat` type so OpenClaw's own JSONL is the
        # single source of truth. Trade-off: cross-process visibility
        # for the cost of teaching OpenClaw the new event type and
        # making sure we don't loop our own output back in as a new
        # user turn. Defer until cross-process memory becomes a real
        # requirement.
        self._session_history: list[dict] = []
        self._session_history_max = int(
            os.environ.get("LELAMP_BRAIN_SESSION_HISTORY_MAX", "10")
        ) * 2
        self._session_log_path: Optional[Path] = self._init_session_log()
        self._load_session_log()

        if self._provider == "gemini":
            self._init_gemini()
        elif self._provider == "openai":
            self._init_openai()
        else:
            logger.warning(
                "TextBrain: unknown provider %r (supported: %s) — brain disabled",
                provider, self.SUPPORTED_PROVIDERS,
            )

        # Static system prompt cached at startup. Recent conversation
        # turns are fetched per-call (still cheap — one JSONL read) and
        # passed as chat history so the static prefix stays byte-stable
        # across turns. That stable prefix lets OpenAI / Gemini's prompt
        # cache kick in: after the first call, the IDENTITY / USER /
        # MEMORY / KNOWLEDGE / SOUL blocks bill at ~50 % of normal input
        # token rate. Changes to those files require a service restart
        # to take effect — fine for the kind of files involved.
        if self.available:
            self._cached_static_system = self._build_static_system_instruction()
            logger.info(
                "TextBrain[%s] static prompt cached (%d chars) — recent turns fetched per call",
                self._provider, len(self._cached_static_system),
            )
        else:
            self._cached_static_system = ""

    # --- per-provider init ---------------------------------------------------

    def _init_gemini(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            logger.warning("TextBrain[gemini]: no GEMINI_API_KEY — brain disabled")
            return
        try:
            from google import genai
            from google.genai import types
            self._client = genai.Client(api_key=api_key)
            self._types = types
            self._model = os.environ.get("LELAMP_GEMINI_TEXT_MODEL", "gemini-2.5-flash")
            logger.info(
                "TextBrain[gemini] ready (model=%s, lang=%s)",
                self._model, self._language or "auto",
            )
        except ImportError as e:
            self._import_error = e
            logger.warning("google-genai not installed: %s", e)
        except Exception as e:
            self._import_error = e
            logger.warning("TextBrain[gemini] init failed: %s", e)

    def _init_openai(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("TextBrain[openai]: no OPENAI_API_KEY — brain disabled")
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key)
            self._model = os.environ.get("LELAMP_OPENAI_TEXT_MODEL", "gpt-4o-mini")
            logger.info(
                "TextBrain[openai] ready (model=%s, lang=%s)",
                self._model, self._language or "auto",
            )
        except ImportError as e:
            self._import_error = e
            logger.warning("openai SDK not installed: %s", e)
        except Exception as e:
            self._import_error = e
            logger.warning("TextBrain[openai] init failed: %s", e)

    # --- public --------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def decide(self, text: str, speaker: str = "unknown") -> TextBrainDecision:
        """Send ``text`` to the brain and return a Decision. Never
        raises — always returns a TextBrainDecision (delegate-on-error
        is the safe default).

        Side effect: on a chit-chat decision the (user, reply) pair is
        appended to the in-process session history so the next call sees
        continuity. Delegate decisions are NOT recorded here — the
        forwarded transcript hits OpenClaw which logs it to the agent
        JSONL, and the next call will read it from there.
        """
        text = (text or "").strip()
        if not text:
            return TextBrainDecision(
                decision="delegate", transcript="", error="empty input",
            )
        if not self.available:
            return TextBrainDecision(
                decision="delegate", transcript=text, error="brain not available",
            )
        if self._provider == "gemini":
            decision = self._decide_gemini(text, speaker)
        elif self._provider == "openai":
            decision = self._decide_openai(text, speaker)
        else:
            return TextBrainDecision(
                decision="delegate", transcript=text,
                error=f"unknown provider {self._provider}",
            )
        if decision.decision == "chitchat" and decision.reply:
            self._append_session_turn("user", text)
            self._append_session_turn("assistant", decision.reply)
            logger.info(
                "TextBrain[%s] session_history size=%d (cap=%d)",
                self._provider, len(self._session_history), self._session_history_max,
            )
        return decision

    def _append_session_turn(self, role: str, text: str) -> None:
        """Push a turn onto the in-process session log + trim to cap.
        Stamps each entry with ``time.time()`` so the per-call merge
        with OpenClaw history can sort everything chronologically.
        Also appends to the JSONL log on disk (best-effort) so a
        service restart can reload the recent session."""
        text = (text or "").strip()
        if not text:
            return
        entry = {"role": role, "text": text, "ts": time.time()}
        self._session_history.append(entry)
        # Cap at SESSION_HISTORY_MAX entries — drops oldest pair first.
        if len(self._session_history) > self._session_history_max:
            self._session_history = self._session_history[-self._session_history_max:]
        self._write_session_log(entry)

    def _init_session_log(self) -> Optional[Path]:
        """Resolve the JSONL persistence path + make sure the parent
        dir exists. Returns ``None`` (disabled persistence) on any
        failure — brain still works, just loses memory on restart."""
        raw = os.environ.get(
            "LELAMP_BRAIN_SESSION_LOG", "/root/local/brain/session.jsonl"
        ).strip()
        if not raw or raw in ("/dev/null", "off", "none"):
            logger.info("TextBrain[%s] session log persistence disabled", self._provider)
            return None
        path = Path(raw)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                "TextBrain[%s] session log parent dir %s unavailable: %s — "
                "running in-memory only", self._provider, path.parent, e,
            )
            return None
        logger.info(
            "TextBrain[%s] session log persistence enabled at %s",
            self._provider, path,
        )
        return path

    def _load_session_log(self) -> None:
        """Replay the JSONL tail into ``self._session_history`` so a
        restart picks up where the previous process left off.

        Reads ONLY the last ``session_history_max`` lines (the tail
        TextBrain.decide will actually use) — no point loading megabytes
        of old session log on init."""
        if self._session_log_path is None or not self._session_log_path.exists():
            return
        try:
            with open(self._session_log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning(
                "TextBrain[%s] could not read session log %s: %s",
                self._provider, self._session_log_path, e,
            )
            return
        tail = lines[-self._session_history_max:]
        loaded = 0
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role")
            text = (obj.get("text") or "").strip()
            ts = float(obj.get("ts") or 0.0)
            if role not in ("user", "assistant") or not text:
                continue
            self._session_history.append({"role": role, "text": text, "ts": ts})
            loaded += 1
        if loaded:
            logger.info(
                "TextBrain[%s] session log restored %d entries from %s",
                self._provider, loaded, self._session_log_path,
            )

    def _write_session_log(self, entry: dict) -> None:
        """Append one entry as a JSONL line. Silent on failure — disk
        I/O errors shouldn't break the brain decision path."""
        if self._session_log_path is None:
            return
        try:
            with open(self._session_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.debug(
                "TextBrain[%s] session log write failed: %s",
                self._provider, e,
            )

    @staticmethod
    def _to_epoch(value) -> float:
        """Best-effort coerce a timestamp field (Unix epoch sec/ms or
        ISO8601 string) to seconds-since-epoch. Returns 0.0 when the
        value is missing or unparseable — those entries land at the
        head of the merged list, which mimics "no timing info known"."""
        if value is None or value == "":
            return 0.0
        if isinstance(value, (int, float)):
            v = float(value)
            return v / 1000.0 if v > 1e11 else v  # heuristic: > 10^11 = ms
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

    def _merge_history(self) -> list[dict]:
        """Return a single chronologically-sorted list of turns, merging
        recent OpenClaw conversation (delegate flow) with the in-process
        session_history (chit-chat the brain handled itself).

        Each entry is ``{"role": "user"|"assistant", "text": str,
        "ts": float}``. Capped at ``2 * _session_history_max`` total
        entries (newest tail) so a long OpenClaw log doesn't blow up
        the prompt.
        """
        merged: list[dict] = []
        for turn in self._load_recent_turns():
            text = (turn.text or "").strip()
            if not text:
                continue
            merged.append({
                "role": "user" if turn.role == "user" else "assistant",
                "text": text,
                "ts": self._to_epoch(turn.time),
            })
        for entry in self._session_history:
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            merged.append({
                "role": entry["role"],
                "text": text,
                "ts": float(entry.get("ts", 0.0)),
            })
        merged.sort(key=lambda e: e["ts"])
        # Cap newest tail so total context stays bounded.
        cap = self._session_history_max * 2
        if len(merged) > cap:
            merged = merged[-cap:]
        return merged

    # --- prompt assembly -----------------------------------------------------

    def _build_static_system_instruction(self) -> str:
        """Cache-friendly system instruction: DECISION_RULES + language
        hint + IDENTITY + USER + MEMORY + KNOWLEDGE + SOUL. Recent
        conversation turns are NOT included here — they're appended as
        chat history per-call so the cached prefix stays byte-stable."""
        parts = [DECISION_RULES]
        hint = language_hint(self._language)
        if hint:
            parts.append(hint)
        try:
            ctx = load_context(include_history=False)
        except Exception as e:
            logger.debug("context_loader (static) failed in text brain: %s", e)
            ctx = None
        if ctx is not None:
            block = ctx.to_system_prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def _load_recent_turns(self):
        """Per-call fetch of just the recent OpenClaw conversation
        turns. Skips load_context (which re-reads IDENTITY / USER /
        MEMORY / KNOWLEDGE / SOUL) and goes straight to the JSONL —
        the static blocks were cached at startup, no point re-reading.
        Returns ``[]`` on any failure."""
        try:
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
            return _read_openclaw_history(agents_dir, session_key, DEFAULT_HISTORY_LIMIT)
        except Exception as e:
            logger.debug("recent_turns fetch failed: %s", e)
            return []

    # --- gemini path ---------------------------------------------------------

    def _decide_gemini(self, text: str, speaker: str) -> TextBrainDecision:
        types = self._types
        t0 = time.time()
        try:
            tool = types.Tool(function_declarations=[
                types.FunctionDeclaration(
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
                ),
            ])
            config = types.GenerateContentConfig(
                system_instruction=self._cached_static_system,
                tools=[tool],
                # Force tool-or-text: model picks one of the two paths,
                # never both. AUTO is the SDK default — making it
                # explicit so the choice is documented in code.
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="AUTO"),
                ),
            )
            # Build chat-history-shaped contents so the static system
            # instruction stays byte-stable (cache-friendly). Gemini
            # roles are "user" / "model". History is the timestamp-
            # merged interleave of OpenClaw turns (delegate flow) +
            # in-process session turns (chit-chat the brain handled
            # itself) — see _merge_history.
            contents = []
            for entry in self._merge_history():
                role = "user" if entry["role"] == "user" else "model"
                contents.append(types.Content(
                    role=role, parts=[types.Part(text=entry["text"])],
                ))
            contents.append(types.Content(
                role="user", parts=[types.Part(text=text)],
            ))
            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
            latency = time.time() - t0
            usage = getattr(response, "usage_metadata", None)
            prompt_tok = getattr(usage, "prompt_token_count", 0) or 0
            resp_tok = getattr(usage, "candidates_token_count", 0) or 0
            total_tok = getattr(usage, "total_token_count", 0) or (prompt_tok + resp_tok)

            # Walk response.candidates[].content.parts looking for either
            # a function_call (= delegate) or text (= chitchat).
            for cand in (response.candidates or []):
                content = getattr(cand, "content", None)
                if content is None:
                    continue
                for part in (content.parts or []):
                    fn = getattr(part, "function_call", None)
                    if fn is not None and getattr(fn, "name", "") == DELEGATE_TOOL_NAME:
                        args = dict(getattr(fn, "args", None) or {})
                        transcript = str(args.get("transcript", text)).strip() or text
                        return TextBrainDecision(
                            decision="delegate", transcript=transcript,
                            latency_s=latency,
                            prompt_tokens=prompt_tok, response_tokens=resp_tok,
                            total_tokens=total_tok,
                        )
                    text_part = getattr(part, "text", None)
                    if text_part:
                        return TextBrainDecision(
                            decision="chitchat", reply=text_part.strip(),
                            latency_s=latency,
                            prompt_tokens=prompt_tok, response_tokens=resp_tok,
                            total_tokens=total_tok,
                        )
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                error="no usable parts in response",
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )
        except Exception as e:
            logger.warning("TextBrain[gemini] error: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text,
                latency_s=time.time() - t0, error=str(e),
            )

    # --- openai path ---------------------------------------------------------

    def _decide_openai(self, text: str, speaker: str) -> TextBrainDecision:
        t0 = time.time()
        try:
            tool_def = {
                "type": "function",
                "function": {
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
            }
            # Build message list with the static system at index 0 +
            # the timestamp-merged conversation history (OpenClaw +
            # in-process brain session, interleaved chronologically) +
            # current user. The system message is byte-stable so
            # OpenAI's prompt cache (auto for prefixes ≥1024 tokens)
            # kicks in.
            messages = [
                {"role": "system", "content": self._cached_static_system},
            ]
            for entry in self._merge_history():
                messages.append({
                    "role": entry["role"], "content": entry["text"],
                })
            messages.append({"role": "user", "content": text})
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=[tool_def],
                tool_choice="auto",
            )
            latency = time.time() - t0
            usage = getattr(response, "usage", None)
            prompt_tok = getattr(usage, "prompt_tokens", 0) or 0
            resp_tok = getattr(usage, "completion_tokens", 0) or 0
            total_tok = getattr(usage, "total_tokens", 0) or (prompt_tok + resp_tok)

            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if fn is None or fn.name != DELEGATE_TOOL_NAME:
                    continue
                try:
                    args = json.loads(fn.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                transcript = str(args.get("transcript", text)).strip() or text
                return TextBrainDecision(
                    decision="delegate", transcript=transcript, latency_s=latency,
                    prompt_tokens=prompt_tok, response_tokens=resp_tok,
                    total_tokens=total_tok,
                )
            content = (msg.content or "").strip()
            if content:
                return TextBrainDecision(
                    decision="chitchat", reply=content, latency_s=latency,
                    prompt_tokens=prompt_tok, response_tokens=resp_tok,
                    total_tokens=total_tok,
                )
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                error="no content/tool_call in response",
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )
        except Exception as e:
            logger.warning("TextBrain[openai] error: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text,
                latency_s=time.time() - t0, error=str(e),
            )


def build_text_brain_from_env() -> Optional[TextBrain]:
    """Read ``LELAMP_BRAIN_PROVIDER`` and return a ready TextBrain — or
    ``None`` when brain mode is disabled / SDK unavailable. Caller
    should treat ``None`` as "send transcript straight to OpenClaw"."""
    provider = (os.environ.get("LELAMP_BRAIN_PROVIDER") or "").strip().lower()
    if is_disabled(provider):
        return None
    brain = TextBrain(provider)
    if not brain.available:
        return None
    return brain
