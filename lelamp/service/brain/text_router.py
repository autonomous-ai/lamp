"""
Half-cascade text brain — raw HTTP, no vendor SDK.

The classic STT pipeline (RMS gate + Deepgram nova-3 WS) delivers a
final transcript; that text is routed through a cheap chat-completion
HTTP call (Gemini Flash / GPT-4o mini) with function calling. The model
picks one of two paths:

  - **chit-chat**: returns a short reply text → spoken via the existing
    TTSService (one consistent ElevenLabs voice across both paths).
  - **delegate**: calls ``delegate_to_lumi(transcript=…)`` → caller
    forwards the original transcript to OpenClaw via ``/api/sensing/
    event`` (classic flow unchanged).

Why raw HTTP instead of google-genai / openai SDKs:
  - One vendored dependency to keep up to date (``requests``, already
    used elsewhere) instead of two.
  - Full control over the message array shape — easy to merge OpenClaw
    history with the in-process chit-chat log and sort by timestamp.
  - Logs are byte-exact — what we send is what's on the wire, no SDK
    transform hiding in between.
  - SDK upgrades have broken us twice before; the REST endpoints below
    are stable contracts.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

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

# Per-call HTTP timeout. Chat completion latency is usually 1-3s; 15s
# gives some headroom for slow networks without wedging the voice loop.
_HTTP_TIMEOUT_S = float(os.environ.get("LELAMP_BRAIN_HTTP_TIMEOUT", "15"))

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"


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
        self._api_key: Optional[str] = None
        self._model: str = ""
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
        self._api_key = api_key
        self._model = os.environ.get("LELAMP_GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        logger.info(
            "TextBrain[gemini] ready (model=%s, lang=%s)",
            self._model, self._language or "auto",
        )

    def _init_openai(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("TextBrain[openai]: no OPENAI_API_KEY — brain disabled")
            return
        self._api_key = api_key
        self._model = os.environ.get("LELAMP_OPENAI_TEXT_MODEL", "gpt-4o-mini")
        logger.info(
            "TextBrain[openai] ready (model=%s, lang=%s)",
            self._model, self._language or "auto",
        )

    # --- public --------------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._api_key) and bool(self._model)

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

    # --- gemini path (raw HTTP) ----------------------------------------------

    def _decide_gemini(self, text: str, speaker: str) -> TextBrainDecision:
        """POST to Gemini ``generateContent`` and parse the response.

        Request shape (v1beta REST):

            POST .../models/{model}:generateContent?key=KEY
            {
              "systemInstruction": {"parts": [{"text": "..."}]},
              "contents": [
                {"role": "user"|"model", "parts": [{"text": "..."}]},
                ...
              ],
              "tools": [{"functionDeclarations": [{...}]}],
              "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}}
            }
        """
        t0 = time.time()
        url = _GEMINI_ENDPOINT.format(model=self._model)
        # Build chat-history-shaped contents so the static system
        # instruction stays byte-stable (cache-friendly). Gemini roles
        # are "user" / "model".
        contents: list[dict] = []
        for entry in self._merge_history():
            role = "user" if entry["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": entry["text"]}]})
        contents.append({"role": "user", "parts": [{"text": text}]})

        payload = {
            "systemInstruction": {"parts": [{"text": self._cached_static_system}]},
            "contents": contents,
            "tools": [{
                "functionDeclarations": [{
                    "name": DELEGATE_TOOL_NAME,
                    "description": DELEGATE_TOOL_DESCRIPTION,
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "transcript": {
                                "type": "STRING",
                                "description": "Exact transcript of what the user just said.",
                            },
                        },
                        "required": ["transcript"],
                    },
                }],
            }],
            # AUTO is the API default; making it explicit so the choice
            # is documented in code (force tool-or-text, never both).
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        }
        try:
            resp = requests.post(
                url,
                params={"key": self._api_key},
                json=payload,
                timeout=_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as e:
            logger.warning("TextBrain[gemini] HTTP error: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text,
                latency_s=time.time() - t0, error=str(e),
            )

        latency = time.time() - t0
        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning("TextBrain[gemini] %s", err)
            return TextBrainDecision(
                decision="error", transcript=text, latency_s=latency, error=err,
            )
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning("TextBrain[gemini] non-JSON response: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text, latency_s=latency,
                error="non-JSON response",
            )

        usage = data.get("usageMetadata") or {}
        prompt_tok = int(usage.get("promptTokenCount") or 0)
        resp_tok = int(usage.get("candidatesTokenCount") or 0)
        total_tok = int(usage.get("totalTokenCount") or (prompt_tok + resp_tok))

        # Walk candidates[].content.parts looking for either a
        # functionCall (= delegate) or text (= chitchat).
        for cand in (data.get("candidates") or []):
            content = cand.get("content") or {}
            for part in (content.get("parts") or []):
                fn = part.get("functionCall")
                if fn and fn.get("name") == DELEGATE_TOOL_NAME:
                    args = fn.get("args") or {}
                    transcript = str(args.get("transcript", text)).strip() or text
                    return TextBrainDecision(
                        decision="delegate", transcript=transcript,
                        latency_s=latency,
                        prompt_tokens=prompt_tok, response_tokens=resp_tok,
                        total_tokens=total_tok,
                    )
                text_part = part.get("text")
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

    # --- openai path (raw HTTP) ----------------------------------------------

    def _decide_openai(self, text: str, speaker: str) -> TextBrainDecision:
        """POST to OpenAI ``chat.completions`` and parse the response.

        Request shape:

            POST .../v1/chat/completions
            Authorization: Bearer KEY
            {
              "model": "...",
              "messages": [
                {"role": "system", "content": "..."},
                {"role": "user"|"assistant", "content": "..."},
                ...
              ],
              "tools": [{"type": "function", "function": {...}}],
              "tool_choice": "auto"
            }
        """
        t0 = time.time()
        # Build message list with the static system at index 0 + the
        # timestamp-merged conversation history + current user. The
        # system message is byte-stable so OpenAI's prompt cache (auto
        # for prefixes ≥1024 tokens) kicks in.
        messages: list[dict] = [
            {"role": "system", "content": self._cached_static_system},
        ]
        for entry in self._merge_history():
            messages.append({"role": entry["role"], "content": entry["text"]})
        messages.append({"role": "user", "content": text})

        payload = {
            "model": self._model,
            "messages": messages,
            "tools": [{
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
            }],
            "tool_choice": "auto",
        }
        try:
            resp = requests.post(
                _OPENAI_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as e:
            logger.warning("TextBrain[openai] HTTP error: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text,
                latency_s=time.time() - t0, error=str(e),
            )

        latency = time.time() - t0
        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning("TextBrain[openai] %s", err)
            return TextBrainDecision(
                decision="error", transcript=text, latency_s=latency, error=err,
            )
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning("TextBrain[openai] non-JSON response: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text, latency_s=latency,
                error="non-JSON response",
            )

        usage = data.get("usage") or {}
        prompt_tok = int(usage.get("prompt_tokens") or 0)
        resp_tok = int(usage.get("completion_tokens") or 0)
        total_tok = int(usage.get("total_tokens") or (prompt_tok + resp_tok))

        choices = data.get("choices") or []
        if not choices:
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                error="no choices in response",
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )
        msg = choices[0].get("message") or {}
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") != DELEGATE_TOOL_NAME:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            transcript = str(args.get("transcript", text)).strip() or text
            return TextBrainDecision(
                decision="delegate", transcript=transcript, latency_s=latency,
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )
        content = (msg.get("content") or "").strip()
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


def build_text_brain_from_env() -> Optional[TextBrain]:
    """Read ``LELAMP_BRAIN_PROVIDER`` and return a ready TextBrain — or
    ``None`` when brain mode is disabled / config missing. Caller
    should treat ``None`` as "send transcript straight to OpenClaw"."""
    provider = (os.environ.get("LELAMP_BRAIN_PROVIDER") or "").strip().lower()
    if is_disabled(provider):
        return None
    brain = TextBrain(provider)
    if not brain.available:
        return None
    return brain
