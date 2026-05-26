"""
Half-cascade text brain — raw HTTP, no vendor SDK.

The classic STT pipeline (RMS gate + Deepgram nova-3 WS) delivers a
final transcript; that text is routed through a cheap chat-completion
HTTP call (Gemini Flash / GPT-4o mini). The model returns plain text
which the brain inspects to pick one of two paths:

  - **chit-chat**: arbitrary reply text → streamed sentence-by-sentence
    into the existing TTSService (one consistent ElevenLabs voice
    across both paths). With the OpenAI provider the stream lets TTS
    start synthesising sentence 1 while the model is still generating
    sentence 2 — ~3-4s off time-to-first-audio vs the old non-stream
    tool-call path.
  - **delegate**: the literal token ``[DELEGATE]`` as the very first
    characters of the reply → caller forwards the original transcript
    to OpenClaw via ``/api/sensing/event`` (classic flow unchanged).
    Streaming clients short-circuit on this marker after the first
    SSE delta and never wait for the rest of the response.

Why raw HTTP instead of google-genai / openai SDKs:
  - One vendored dependency to keep up to date (``requests``, already
    used elsewhere) instead of two.
  - Full control over the message array shape — easy to merge OpenClaw
    history with the in-process chit-chat log and sort by timestamp.
  - Logs are byte-exact — what we send is what's on the wire, no SDK
    transform hiding in between.

Memory layout — see ``workspace.py`` and ``summarizer.py``:

  - The brain owns a workspace dir (``LELAMP_BRAIN_WORKSPACE``,
    default ``/root/.brain/workspace``) mirroring OpenClaw's
    convention. Inside it: ``MEMORY.md`` (per-day diary) and
    ``session/<date>.jsonl`` + ``bench/<date>.jsonl`` rotated daily.
  - Recent chit-chat turns + delegated turns from OpenClaw's JSONL are
    merged per call into a chronological history. When the merged
    window exceeds the cap, the oldest slice is folded into a rolling
    summary (async, off the critical path) and prepended to the LLM
    request so older context degrades to a summary instead of being
    dropped.
  - At day rollover the previous day's rolling summary + raw turns are
    summarized into a 3-6 bullet diary entry appended to brain
    ``MEMORY.md``, which then feeds the static system prompt on the
    next process restart.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

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
    DELEGATE_PREFIX,
    language_hint,
    resolve_stt_language,
)
from lelamp.service.brain.summarizer import RollingSummary
from lelamp.service.brain.workspace import BrainWorkspace

logger = logging.getLogger("lelamp.brain.text")


# Env values that map to "no brain — go straight to OpenClaw".
_DISABLED_VALUES = frozenset({"", "none", "off", "classic", "disabled"})

# Per-call HTTP timeout. Chat completion latency is usually 1-3s; 15s
# gives some headroom for slow networks without wedging the voice loop.
_HTTP_TIMEOUT_S = float(os.environ.get("LELAMP_BRAIN_HTTP_TIMEOUT", "15"))

# How many evicted turns to batch before refreshing the rolling
# summary. Smaller = fresher summary but more LLM calls; 10 keeps cost
# at ~$0.003/day in typical chit-chat usage.
_SUMMARY_EVICT_BATCH = int(os.environ.get("LELAMP_BRAIN_SUMMARY_EVICT_BATCH", "10"))

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# Sentence boundary for streaming → TTS chunking. Splits after `.`, `!`,
# `?`, `…` followed by whitespace, or on any run of newlines. Covers
# Vietnamese / English / most other Latin-script languages — punctuation
# semantics are the same. Each match is the *gap*; the punctuation char
# is kept with the preceding sentence so TTS prosody stays natural.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")


def _drain_complete_sentences(buffer: str, on_sentence: Callable[[str], None]) -> str:
    """Emit any *complete* sentences sitting at the head of ``buffer``
    via ``on_sentence``; return the unconsumed tail (a partial sentence
    still being assembled by the streaming LLM)."""
    last = 0
    for m in _SENTENCE_BOUNDARY.finditer(buffer):
        sent = buffer[last:m.end()].strip()
        if sent:
            on_sentence(sent)
        last = m.end()
    return buffer[last:]


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
        latency_s: wall-clock time of the brain HTTP call (until the
            LLM finishes streaming the last token).
        first_sentence_s: wall-clock time from ``decide()`` start to
            when the first complete sentence was handed to
            ``on_sentence`` (i.e. pushed into ``TTSService.speak_queue``
            on the OpenAI streaming path). This is the closest proxy
            for time-to-first-audio that the brain can measure without
            instrumenting the TTS pipeline. ``0.0`` when no callback
            was provided, the turn was a delegate, or the reply had no
            sentence boundaries (single-shot tail flush at the end).
            On the Gemini non-streaming path, this collapses to
            ``≈ latency_s`` because every sentence ships in one batch
            after the full reply lands.
        error: human-readable error string when ``decision == "error"``.
    """

    decision: str = "delegate"
    reply: str = ""
    transcript: str = ""
    latency_s: float = 0.0
    first_sentence_s: float = 0.0
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

        # In-memory mirror of the recent session log (user + assistant
        # chit-chat turns the brain handled itself). Bounded so a long-
        # running process doesn't grow unbounded; the workspace's daily
        # JSONL files are the authoritative store on disk.
        self._session_history: list[dict] = []
        self._session_history_max = int(
            os.environ.get("LELAMP_BRAIN_SESSION_HISTORY_MAX", "10")
        ) * 2

        # Filesystem layer: workspace dir, daily JSONL writers, MEMORY.md.
        self._workspace = BrainWorkspace()
        self._restore_session_from_workspace()

        # Compression layer: rolling summary + day-rollover diary.
        # ``llm_call`` is bound to ``self._complete_text`` at use time
        # (the summarizer is provider-agnostic; it just needs a
        # ``prompt -> text`` callable).
        self._summary = RollingSummary(
            self._workspace,
            evict_batch=_SUMMARY_EVICT_BATCH,
        )

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
            # Kick a one-shot catch-up pass for any past days that have
            # a session file but no matching entry in MEMORY.md — runs
            # on a background thread so init isn't blocked.
            self._summary.catch_up_unsummarized_days(self._complete_text)
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

    def decide(
        self,
        text: str,
        speaker: str = "unknown",
        on_sentence: Optional[Callable[[str], None]] = None,
    ) -> TextBrainDecision:
        """Send ``text`` to the brain and return a Decision. Never
        raises — always returns a TextBrainDecision (delegate-on-error
        is the safe default).

        ``on_sentence``: optional callback invoked with each complete
        sentence as the streaming reply arrives (OpenAI only — Gemini
        path still buffers, then fires the callback once at the end so
        callers don't need a separate code path). Used to feed
        ``TTSService.speak_queue`` so audio playback starts ~3-4s
        sooner on chit-chat replies. If ``None``, the brain accumulates
        the reply silently and the caller handles TTS dispatch.

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
        # Day rollover detector — kicks an async diary write for
        # yesterday if today's date just changed. Cheap; safe to call
        # every turn.
        self._summary.maybe_close_day(self._complete_text)
        if self._provider == "gemini":
            decision = self._decide_gemini(text, speaker, on_sentence)
        elif self._provider == "openai":
            decision = self._decide_openai(text, speaker, on_sentence)
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
        self._write_bench(text, speaker, decision)
        return decision

    # --- session history (in-memory mirror of workspace session JSONL) ------

    def _restore_session_from_workspace(self) -> None:
        """Replay the tail of the workspace's daily session files into
        ``self._session_history`` so a restart picks up where the
        previous process left off. ``role: "summary"`` checkpoints are
        not chat turns and are filtered out here (the RollingSummary
        re-reads them separately on its own init)."""
        records = self._workspace.session.load_tail_records(self._session_history_max)
        self._session_history = [
            {
                "role": r["role"],
                "text": (r.get("text") or "").strip(),
                "ts": float(r.get("ts") or 0.0),
            }
            for r in records
            if r.get("role") in ("user", "assistant")
            and (r.get("text") or "").strip()
        ]
        if self._session_history:
            logger.info(
                "TextBrain: restored %d session entries from workspace %s",
                len(self._session_history), self._workspace.session.dir,
            )

    def _append_session_turn(self, role: str, text: str) -> None:
        """Push a turn onto the in-process session log + trim to cap.
        Stamps each entry with ``time.time()`` so the per-call merge
        with OpenClaw history can sort everything chronologically.
        Also appends to the workspace's daily JSONL (best-effort) so a
        service restart can reload the recent session."""
        text = (text or "").strip()
        if not text:
            return
        entry = {"role": role, "text": text, "ts": time.time()}
        self._session_history.append(entry)
        if len(self._session_history) > self._session_history_max:
            self._session_history = self._session_history[-self._session_history_max:]
        self._workspace.session.write(entry)

    # --- bench -------------------------------------------------------------

    def _write_bench(self, user_text: str, speaker: str, decision: TextBrainDecision) -> None:
        """Append one JSONL record per decide() call to the workspace's
        daily bench file. Truncates long texts so the file stays
        grep-friendly — full text already lives in the session log and
        OpenClaw JSONL."""
        if not self._workspace.bench.enabled:
            return
        truncate = 200
        record = {
            "ts": time.time(),
            "provider": self._provider,
            "model": self._model,
            "decision": decision.decision,
            "latency_s": round(decision.latency_s, 3),
            # Time-to-first-sentence — proxy for time-to-first-audio.
            # ``0.0`` for delegate turns and single-shot replies that
            # had no callback wired up; otherwise the wall-clock gap
            # from ``decide()`` start to the first sentence handed off
            # to ``on_sentence`` (i.e. pushed into TTS).
            "first_sentence_s": round(decision.first_sentence_s, 3),
            "prompt_tokens": decision.prompt_tokens,
            "response_tokens": decision.response_tokens,
            "total_tokens": decision.total_tokens,
            "speaker": speaker,
            "user_text": user_text[:truncate],
            "reply": (decision.reply or "")[:truncate],
            "transcript": (decision.transcript or "")[:truncate],
            "error": decision.error,
        }
        self._workspace.bench.write(record)

    # --- timestamp utilities ------------------------------------------------

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

    # --- merged history + eviction-trigger ---------------------------------

    def _merge_history(self) -> list[dict]:
        """Return the chronologically-sorted list of turns to pass to
        the LLM. Capped at ``2 * _session_history_max`` (newest tail).
        When the cap evicts entries, kick an async rolling-summary
        refresh so older context degrades to a summary rather than
        being dropped on the floor.
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
            evicted = merged[: len(merged) - cap]
            self._summary.summarize_evicted_async(evicted, self._complete_text)
            merged = merged[-cap:]
        return merged

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

    # --- prompt assembly -----------------------------------------------------

    def _build_static_system_instruction(self) -> str:
        """Cache-friendly system instruction: DECISION_RULES + language
        hint + IDENTITY + USER + (OpenClaw MEMORY + brain MEMORY) +
        KNOWLEDGE + SOUL. Recent conversation turns are NOT included
        here — they're appended as chat history per-call so the cached
        prefix stays byte-stable."""
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
            ctx.brain_memory = self._workspace.read_memory_md()
            block = ctx.to_system_prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def _rolling_summary_message_text(self) -> str:
        """Format the rolling summary as a single user-message string,
        or ``""`` when there is no summary to inject."""
        summary = self._summary.text
        if not summary:
            return ""
        return f"[Earlier in this conversation: {summary}]"

    def _log_outbound_context(self, role_text_pairs: list[tuple[str, str]]) -> None:
        """Multi-line dump of every message about to hit the LLM.

        Static system message is replaced with a ``<N chars cached
        static prompt>`` placeholder because it never changes between
        turns — printing 15k chars per request floods journalctl and
        adds nothing observable. Everything else (rolling summary,
        history turns, current user input) is logged verbatim so an
        operator can reconstruct what the model saw on each decision.
        """
        logger.info("brain.context [%s] n=%d", self._provider, len(role_text_pairs))
        for i, (role, text) in enumerate(role_text_pairs):
            if role == "system":
                logger.info(
                    "brain.context [%s] #%02d %s: <%d chars cached static prompt>",
                    self._provider, i, role, len(text),
                )
            else:
                logger.info(
                    "brain.context [%s] #%02d %s: %s",
                    self._provider, i, role, text,
                )

    # --- gemini path (raw HTTP) ----------------------------------------------

    def _decide_gemini(
        self,
        text: str,
        speaker: str,
        on_sentence: Optional[Callable[[str], None]] = None,
    ) -> TextBrainDecision:
        """POST to Gemini ``generateContent`` and parse the response.

        Routing is decided by inspecting the model's *text* output: a
        reply that starts with the literal ``[DELEGATE]`` token means
        hand the turn off to OpenClaw; anything else is the chit-chat
        reply. See ``prompts.DECISION_RULES`` for the prompt side.

        Request shape (v1beta REST):

            POST .../models/{model}:generateContent?key=KEY
            {
              "systemInstruction": {"parts": [{"text": "..."}]},
              "contents": [
                {"role": "user"|"model", "parts": [{"text": "..."}]},
                ...
              ]
            }

        Gemini path is non-streaming (one request → one full response).
        When ``on_sentence`` is provided, the buffered reply is split
        into sentences once at the end and dispatched in order — same
        callback shape as the streaming OpenAI path so callers don't
        branch on provider.
        """
        t0 = time.time()
        url = _GEMINI_ENDPOINT.format(model=self._model)
        # Build chat-history-shaped contents so the static system
        # instruction stays byte-stable (cache-friendly). Gemini roles
        # are "user" / "model".
        contents: list[dict] = []
        summary_msg = self._rolling_summary_message_text()
        if summary_msg:
            contents.append({"role": "user", "parts": [{"text": summary_msg}]})
        for entry in self._merge_history():
            role = "user" if entry["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": entry["text"]}]})
        contents.append({"role": "user", "parts": [{"text": text}]})

        # Per-turn dump of everything the model is about to see —
        # system prompt stays as a length placeholder so the log is
        # readable, history + summary + current input are full text.
        self._log_outbound_context(
            [("system", self._cached_static_system)]
            + [(c["role"], c["parts"][0]["text"]) for c in contents]
        )

        payload = {
            "systemInstruction": {"parts": [{"text": self._cached_static_system}]},
            "contents": contents,
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

        # Concatenate every text part Gemini returned (usually just one).
        out = ""
        for cand in (data.get("candidates") or []):
            content = cand.get("content") or {}
            for part in (content.get("parts") or []):
                t = part.get("text")
                if t:
                    out += t

        out = out.strip()
        if not out:
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                error="no text in response",
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )

        # Delegate marker takes the entire reply by contract; case-
        # insensitive match in case the model lowercases it.
        if out.upper().startswith(DELEGATE_PREFIX):
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )

        # Chit-chat: optionally fan out sentences via callback so the
        # caller can stream them into TTS one by one. ``first_sentence``
        # captures the moment the first sentence is handed off — for the
        # non-streaming Gemini path this is essentially ``latency`` (the
        # full reply lands as one chunk) but the field is reported for
        # parity with the OpenAI streaming path.
        first_sentence: list[Optional[float]] = [None]
        if on_sentence is not None:
            def _tracked(s: str) -> None:
                if first_sentence[0] is None:
                    first_sentence[0] = time.time() - t0
                on_sentence(s)
            _drain_complete_sentences(out + "\n", _tracked)
        return TextBrainDecision(
            decision="chitchat", reply=out, latency_s=latency,
            first_sentence_s=first_sentence[0] or 0.0,
            prompt_tokens=prompt_tok, response_tokens=resp_tok,
            total_tokens=total_tok,
        )

    # --- openai path (raw HTTP) ----------------------------------------------

    def _decide_openai(
        self,
        text: str,
        speaker: str,
        on_sentence: Optional[Callable[[str], None]] = None,
    ) -> TextBrainDecision:
        """POST to OpenAI ``chat.completions`` with SSE streaming and
        parse the response incrementally.

        Routing is decided by inspecting the first few tokens of the
        streamed reply: if they form the literal ``[DELEGATE]`` marker,
        we short-circuit the stream and hand the turn off to OpenClaw
        without waiting for the rest of the response. Otherwise, every
        complete sentence is dispatched via ``on_sentence`` as it
        arrives so the TTS pipeline can start synthesising the first
        sentence while the model is still generating the second.

        Request shape:

            POST .../v1/chat/completions
            Authorization: Bearer KEY
            {
              "model": "...",
              "messages": [...],
              "stream": true,
              "stream_options": {"include_usage": true}
            }
        """
        t0 = time.time()
        messages: list[dict] = [
            {"role": "system", "content": self._cached_static_system},
        ]
        summary_msg = self._rolling_summary_message_text()
        if summary_msg:
            messages.append({"role": "user", "content": summary_msg})
        for entry in self._merge_history():
            messages.append({"role": entry["role"], "content": entry["text"]})
        messages.append({"role": "user", "content": text})

        # Per-turn dump of everything the model is about to see —
        # system prompt stays as a length placeholder so the log is
        # readable, history + summary + current input are full text.
        self._log_outbound_context(
            [(m["role"], m["content"]) for m in messages]
        )

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            # Final SSE chunk carries the usage block — without this the
            # bench file would always log zero tokens for streamed
            # responses.
            "stream_options": {"include_usage": True},
        }
        # Optional `reasoning_effort` override — relevant for the
        # gpt-5 / gpt-5.x family which defaults to "medium" reasoning
        # and burns multiple seconds of hidden thinking tokens before
        # emitting the first output token. For a voice front-door we
        # want the lowest possible TTFB, so set this to "minimal"
        # (gpt-5*) or "none" (gpt-5.1+) when running on a reasoning
        # model. Sent only when the env var is set so older non-
        # reasoning models (gpt-4o*, gpt-4.1*) don't reject the field.
        reasoning_effort = os.environ.get(
            "LELAMP_OPENAI_REASONING_EFFORT", ""
        ).strip()
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        try:
            resp = requests.post(
                _OPENAI_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                timeout=_HTTP_TIMEOUT_S,
                stream=True,
            )
        except requests.RequestException as e:
            logger.warning("TextBrain[openai] HTTP error: %s", e)
            return TextBrainDecision(
                decision="error", transcript=text,
                latency_s=time.time() - t0, error=str(e),
            )

        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning("TextBrain[openai] %s", err)
            resp.close()
            return TextBrainDecision(
                decision="error", transcript=text,
                latency_s=time.time() - t0, error=err,
            )

        # Streaming state:
        # - ``buffer`` is the full accumulated content (chit-chat
        #   reply, or the prefix-detection window).
        # - ``pending`` is the slice of ``buffer`` that hasn't been
        #   drained into ``on_sentence`` yet.
        # - ``prefix_decided`` flips True once we know the reply is
        #   chit-chat (i.e. not the delegate marker). Until then we
        #   buffer rather than fan out, so a delegate reply never
        #   leaks audio.
        # - ``first_sentence_ts`` captures the wall-clock moment the
        #   first sentence hands off to the TTS callback — the closest
        #   proxy for time-to-first-audio that the brain can observe
        #   without instrumenting the audio path. Reported via
        #   ``TextBrainDecision.first_sentence_s`` so the bench file
        #   can plot real perceived latency rather than the
        #   full-stream-complete number.
        buffer = ""
        pending = ""
        prefix_decided = False
        is_delegate = False
        prompt_tok = resp_tok = total_tok = 0
        first_sentence_ts: Optional[float] = None

        # Wrap the caller's callback so the first sentence stamps a
        # timer without touching the call sites below. Cheap (one if
        # check per sentence) and keeps the streaming loop readable.
        tracked_on_sentence: Optional[Callable[[str], None]] = None
        if on_sentence is not None:
            user_cb = on_sentence

            def tracked_on_sentence(s: str) -> None:
                nonlocal first_sentence_ts
                if first_sentence_ts is None:
                    first_sentence_ts = time.time()
                user_cb(s)

        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                if not raw_line.startswith("data: "):
                    continue
                payload_str = raw_line[len("data: "):]
                if payload_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue

                # Usage chunk (always last when stream_options is on)
                # has no choices array.
                usage = chunk.get("usage")
                if usage:
                    prompt_tok = int(usage.get("prompt_tokens") or 0)
                    resp_tok = int(usage.get("completion_tokens") or 0)
                    total_tok = int(
                        usage.get("total_tokens") or (prompt_tok + resp_tok)
                    )
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if not delta:
                    continue
                buffer += delta

                if not prefix_decided:
                    # We only need to inspect what comes after any
                    # leading whitespace; the prompt says "no leading
                    # whitespace" but models occasionally emit one.
                    stripped = buffer.lstrip()
                    if not stripped:
                        continue
                    if stripped[0] == "[":
                        # Could be the delegate marker OR a voice marker
                        # like "[chuckle]". Wait until we have enough
                        # chars to disambiguate.
                        if len(stripped) >= len(DELEGATE_PREFIX):
                            if stripped.upper().startswith(DELEGATE_PREFIX):
                                is_delegate = True
                                break
                            prefix_decided = True
                            pending = buffer  # nothing fanned out yet
                        else:
                            # Keep buffering until we have ~10 chars.
                            continue
                    else:
                        prefix_decided = True
                        pending = buffer

                # Chit-chat path: drain any complete sentences sitting
                # at the head of ``pending`` into the callback.
                else:
                    pending += delta

                if not is_delegate and tracked_on_sentence is not None and pending:
                    pending = _drain_complete_sentences(pending, tracked_on_sentence)
        finally:
            resp.close()

        latency = time.time() - t0

        if is_delegate:
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )

        full_reply = buffer.strip()
        if not full_reply:
            return TextBrainDecision(
                decision="delegate", transcript=text, latency_s=latency,
                error="empty stream",
                prompt_tokens=prompt_tok, response_tokens=resp_tok,
                total_tokens=total_tok,
            )

        # Flush the final partial sentence (no trailing punctuation) so
        # the user hears the whole reply. Goes through the tracked
        # wrapper so a single-sentence reply with no internal terminator
        # still records a first_sentence_s.
        if tracked_on_sentence is not None:
            tail = pending.strip()
            if tail:
                tracked_on_sentence(tail)

        first_sentence_s = (
            (first_sentence_ts - t0) if first_sentence_ts is not None else 0.0
        )
        return TextBrainDecision(
            decision="chitchat", reply=full_reply, latency_s=latency,
            first_sentence_s=first_sentence_s,
            prompt_tokens=prompt_tok, response_tokens=resp_tok,
            total_tokens=total_tok,
        )

    # --- plain-text completion (for summarizer) -----------------------------

    def _complete_text(self, prompt: str) -> str:
        """Synchronous plain-text completion (no tools, no chat
        history) used by the rolling summarizer / day-rollover diary.
        Re-uses the same provider/model as decide() — the cost
        differential isn't worth a second model + key.

        Returns ``""`` on any failure so the summarizer can skip the
        update without raising into a daemon thread."""
        prompt = (prompt or "").strip()
        if not prompt or not self.available:
            return ""
        try:
            if self._provider == "openai":
                return self._complete_text_openai(prompt)
            if self._provider == "gemini":
                return self._complete_text_gemini(prompt)
        except requests.RequestException as e:
            logger.warning("TextBrain[%s] summary completion HTTP error: %s",
                           self._provider, e)
        except Exception as e:
            logger.warning("TextBrain[%s] summary completion failed: %s",
                           self._provider, e)
        return ""

    def _complete_text_openai(self, prompt: str) -> str:
        resp = requests.post(
            _OPENAI_ENDPOINT,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.warning(
                "TextBrain[openai] summary HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return ""
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()

    def _complete_text_gemini(self, prompt: str) -> str:
        url = _GEMINI_ENDPOINT.format(model=self._model)
        resp = requests.post(
            url,
            params={"key": self._api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.warning(
                "TextBrain[gemini] summary HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return ""
        data = resp.json()
        for cand in (data.get("candidates") or []):
            for part in ((cand.get("content") or {}).get("parts") or []):
                text = part.get("text")
                if text:
                    return text.strip()
        return ""


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
