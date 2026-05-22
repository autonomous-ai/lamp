"""
Brain benchmark — per-turn JSONL tracker so we can A/B providers.

For every brain turn we log:
  - provider (gemini / openai / …)
  - decision (chitchat / delegate / error / empty)
  - latencies: first user-audio in → first reply token, → first reply
    audio, → decision (delegate dispatch or speech complete)
  - token usage (when the provider reports it)
  - user text + brain reply text (truncated, for spot-checks)
  - speaker tag

Output: ``$LELAMP_BRAIN_BENCH_DIR/YYYY-MM-DD.jsonl`` (default
``/root/local/brain_bench/``). Append-only, one record per turn.

The :class:`BrainBenchmark` is single-instance-per-VoiceService and
single-turn-at-a-time (mic loop is sequential). All writes go through
a lock so log lines from late-arriving callbacks (e.g. usage event
after end_turn) don't corrupt the file. If the bench dir isn't
writable, the tracker disables itself and falls back to a no-op —
brain itself never fails because of benchmarking.

Use ``lelamp.brain_benchmark_report`` to read the JSONL back and print
summary stats.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lelamp.brain.bench")

DEFAULT_BENCH_DIR = os.environ.get("LELAMP_BRAIN_BENCH_DIR", "/root/local/brain_bench")
DEFAULT_ENABLED = os.environ.get("LELAMP_BRAIN_BENCH_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off",
)
# Truncate user / reply text to keep JSONL lines reasonable; full text
# lives in the agent JSONL anyway, this is for spot-checks only.
TEXT_TRUNCATE = 200


@dataclass
class BrainTurn:
    """One brain turn — from first mic frame to decision finalised."""

    provider: str
    started_at: float                              # epoch when start_turn() fired
    first_audio_in_at: Optional[float] = None      # first mic frame actually shipped
    first_reply_token_at: Optional[float] = None   # first text delta from brain
    first_reply_audio_at: Optional[float] = None   # first audio chunk for chit-chat
    delegate_at: Optional[float] = None            # when on_delegate fired
    completed_at: Optional[float] = None           # end_turn()

    decision: str = "pending"   # chitchat | delegate | error | empty
    user_input: str = ""
    reply: str = ""
    speaker: str = "unknown"

    prompt_tokens: int = 0
    response_tokens: int = 0
    total_tokens: int = 0

    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        d = asdict(self)
        # Pre-compute the latencies a reader would care about so reports
        # don't have to re-derive them. Rounded to 3dp = ms precision.
        d["latency_first_token_s"] = _latency(self.started_at, self.first_reply_token_at)
        d["latency_first_audio_s"] = _latency(self.started_at, self.first_reply_audio_at)
        d["latency_decision_s"] = _latency(
            self.started_at,
            self.delegate_at if self.decision == "delegate" else self.completed_at,
        )
        d["latency_total_s"] = _latency(self.started_at, self.completed_at)
        # ISO timestamp for human eyeballing
        d["started_iso"] = datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")
        return d


def _latency(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None:
        return None
    return round(end - start, 3)


class BrainBenchmark:
    """Lifecycle for one brain provider's turns.

    Caller (VoiceService) creates one of these per `_continuous_brain_loop`
    invocation and feeds it events from the brain callbacks. Everything is
    best-effort — if the FS is read-only or a callback runs after close
    the tracker just drops the data."""

    def __init__(
        self,
        provider: str,
        bench_dir: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self._provider = provider
        self._enabled = DEFAULT_ENABLED if enabled is None else enabled
        self._dir = Path(bench_dir or DEFAULT_BENCH_DIR)
        self._lock = threading.Lock()
        self._current: Optional[BrainTurn] = None

        if self._enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                logger.info(
                    "brain benchmark active for provider=%s dir=%s",
                    provider, self._dir,
                )
            except OSError as e:
                logger.warning(
                    "brain bench dir %s not writable (%s) — disabling tracker",
                    self._dir, e,
                )
                self._enabled = False

    # --- lifecycle -----------------------------------------------------------

    def start_turn(self) -> None:
        """Begin a fresh turn. Idempotent — already-open turns are
        force-closed as ``empty`` so we don't lose a record if the
        previous turn never received an end signal (rare: session
        disconnect mid-turn)."""
        if not self._enabled:
            return
        with self._lock:
            if self._current is not None:
                # Salvage whatever we had as an "empty" record so the
                # session disconnect doesn't silently drop data.
                self._current.completed_at = time.time()
                if self._current.decision == "pending":
                    self._current.decision = "empty"
                self._write_locked(self._current)
            self._current = BrainTurn(provider=self._provider, started_at=time.time())

    def mark_audio_in(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._current and self._current.first_audio_in_at is None:
                self._current.first_audio_in_at = time.time()

    def mark_user_input(self, text: str) -> None:
        if not self._enabled or not text:
            return
        text = text.strip()
        if not text:
            return
        with self._lock:
            if self._current:
                # Provider transcripts arrive as deltas — append, don't
                # overwrite; on_user_input might fire multiple times.
                merged = (self._current.user_input + " " + text).strip()
                self._current.user_input = merged[:TEXT_TRUNCATE]

    def mark_speaker(self, name: str) -> None:
        if not self._enabled or not name:
            return
        with self._lock:
            if self._current:
                self._current.speaker = name

    def mark_reply_token(self, text: str) -> None:
        if not self._enabled or not text:
            return
        now = time.time()
        with self._lock:
            if not self._current:
                return
            if self._current.first_reply_token_at is None:
                self._current.first_reply_token_at = now
            merged = (self._current.reply + text)
            self._current.reply = merged[:TEXT_TRUNCATE]

    def mark_reply_audio(self) -> None:
        if not self._enabled:
            return
        now = time.time()
        with self._lock:
            if self._current and self._current.first_reply_audio_at is None:
                self._current.first_reply_audio_at = now

    def mark_usage(self, prompt: int, response: int, total: int) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._current:
                self._current.prompt_tokens += prompt
                self._current.response_tokens += response
                self._current.total_tokens += total

    def end_turn(self, decision: str, error: str = "") -> None:
        """Close the current turn and append to the JSONL file. Safe to
        call when no turn is open — silently no-ops."""
        if not self._enabled:
            return
        now = time.time()
        with self._lock:
            if self._current is None:
                return
            self._current.completed_at = now
            self._current.decision = decision
            self._current.error = error
            if decision == "delegate" and self._current.delegate_at is None:
                self._current.delegate_at = now
            self._write_locked(self._current)
            self._current = None

    # --- IO ------------------------------------------------------------------

    def _write_locked(self, turn: BrainTurn) -> None:
        if not self._enabled:
            return
        date = datetime.fromtimestamp(turn.started_at).strftime("%Y-%m-%d")
        path = self._dir / f"{date}.jsonl"
        try:
            line = json.dumps(turn.to_record(), ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.warning("brain bench write failed (%s) — disabling tracker", e)
            self._enabled = False
