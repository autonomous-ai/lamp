"""
Rolling summary + per-day diary for the half-cascade text brain.

Two compression layers, both async (off the ``decide()`` critical path):

  - **Rolling summary** — eviction-triggered. When the merged history
    window grows past the session-history cap, the oldest
    ``evict_batch`` turns are handed to an LLM that folds them into the
    previous rolling summary. The result is prepended to ``contents``
    on subsequent ``decide()`` calls as
    ``[Earlier in this conversation: ...]`` so the model never loses
    older context — it just degrades from raw turns to a short summary.

  - **Daily diary** — day-rollover triggered. When ``decide()`` first
    fires on a new local day, yesterday's rolling summary + the raw
    turns from yesterday's session file are summarized into a small
    bullet-list diary entry appended to brain ``MEMORY.md``. The
    rolling summary then resets for the new day; the diary stays as
    permanent long-term memory (loaded into the static system prompt
    on the next process restart).

Why both layers and not one:

  - Rolling alone would lose continuity across restarts (it lives in
    memory + the session JSONL for the current day; after midnight the
    file rotates and the in-memory state is irrelevant to "yesterday").
  - Diary alone wouldn't help during a long single-day session — the
    model would still drop old turns once the window cap evicts them
    and the diary wouldn't refresh until next day.

Both flows take ``llm_call: Callable[[str], str]`` so this module
doesn't need to know which provider TextBrain picked.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from lelamp.service.brain.workspace import BrainWorkspace, _today_str

logger = logging.getLogger("lelamp.brain.summarizer")

# Sentinel JSONL ``role`` value used when the summarizer writes a
# checkpoint of the current rolling summary into today's session file.
# That checkpoint lets us restore the rolling text after a service
# restart in the middle of a day, instead of starting cold from "".
ROLLING_SUMMARY_ROLE = "summary"


class RollingSummary:
    """In-memory rolling summary + diary trigger.

    All public methods are non-blocking — heavy work (LLM calls,
    MEMORY.md writes) runs on daemon threads. The :prop:`text` accessor
    returns whatever summary the last completed refresh produced; a
    stale summary is preferred over blocking ``decide()`` while a
    refresh is in flight.
    """

    def __init__(
        self,
        workspace: BrainWorkspace,
        evict_batch: int = 10,
        summary_target_tokens: int = 300,
    ):
        self._workspace = workspace
        self._evict_batch = max(1, evict_batch)
        self._summary_target_tokens = summary_target_tokens
        self._lock = threading.Lock()
        self._rolling_text: str = ""
        self._last_day: str = _today_str()
        self._summary_pending = False
        self._diary_pending = False
        # Highest ``ts`` of any turn that has already been folded into
        # ``_rolling_text``. Eviction batches whose newest entry is
        # already at or below this watermark are skipped — otherwise
        # every ``decide()`` past the cap would kick a redundant LLM
        # call to re-summarize the same evicted slice.
        self._covered_until_ts: float = 0.0
        self._restore_from_session()

    @property
    def text(self) -> str:
        """Current rolling summary. Empty string when the day just
        rolled over or no eviction has happened yet."""
        with self._lock:
            return self._rolling_text

    # --- restore on init -----------------------------------------------------

    def _restore_from_session(self) -> None:
        """Re-seed ``self._rolling_text`` from the most recent
        ``role: "summary"`` checkpoint in today's session file. Without
        this, restarting the service mid-day would forget every summary
        produced earlier in the day."""
        if not self._workspace.session.enabled:
            return
        today_path = self._workspace.session.today_path()
        if today_path is None or not today_path.is_file():
            return
        records = self._workspace.session.read_file(today_path)
        for record in reversed(records):
            if record.get("role") == ROLLING_SUMMARY_ROLE:
                text = (record.get("text") or "").strip()
                if text:
                    self._rolling_text = text
                    try:
                        self._covered_until_ts = float(
                            record.get("covered_until_ts") or 0.0
                        )
                    except (TypeError, ValueError):
                        self._covered_until_ts = 0.0
                    logger.info(
                        "rolling summary restored from %s (%d chars, watermark=%.0f)",
                        today_path.name, len(text), self._covered_until_ts,
                    )
                return

    # --- eviction-triggered summary -----------------------------------------

    def summarize_evicted_async(
        self,
        evicted: list[dict],
        llm_call: Callable[[str], str],
    ) -> None:
        """Kick a background refresh of the rolling summary from the
        ``evicted`` window-tail. No-op when:

          - the workspace is disabled (nowhere to checkpoint),
          - a refresh is already in flight (we don't queue — the next
            eviction will pick up the work),
          - the evicted batch is smaller than ``evict_batch`` (not
            worth the LLM call yet).
        """
        if not self._workspace.session.enabled:
            return
        with self._lock:
            if self._summary_pending:
                return
            # Drop entries already folded into the previous summary so
            # we don't re-summarize the same slice over and over.
            new_batch = [
                e for e in evicted
                if float(e.get("ts") or 0.0) > self._covered_until_ts
            ]
            if len(new_batch) < self._evict_batch:
                return
            self._summary_pending = True
            previous = self._rolling_text
        batch = list(new_batch)
        new_watermark = max(float(e.get("ts") or 0.0) for e in batch)

        def run() -> None:
            try:
                new_summary = self._build_summary_text(previous, batch, llm_call)
                if not new_summary:
                    return
                with self._lock:
                    self._rolling_text = new_summary
                    self._covered_until_ts = new_watermark
                self._workspace.session.write({
                    "role": ROLLING_SUMMARY_ROLE,
                    "ts": time.time(),
                    "text": new_summary,
                    "evicted_count": len(batch),
                    "covered_until_ts": new_watermark,
                })
                logger.info(
                    "rolling summary refreshed (%d chars; absorbed %d evicted turns; watermark=%.0f)",
                    len(new_summary), len(batch), new_watermark,
                )
            except Exception as e:
                logger.warning("rolling summary refresh failed: %s", e)
            finally:
                with self._lock:
                    self._summary_pending = False

        threading.Thread(target=run, daemon=True, name="brain-summary").start()

    def _build_summary_text(
        self,
        previous: str,
        evicted: list[dict],
        llm_call: Callable[[str], str],
    ) -> str:
        """Prompt the LLM to fold ``evicted`` turns into a refreshed
        rolling summary. Returns the new summary text (stripped).
        Returns ``""`` on empty / unusable response."""
        turns_text = "\n".join(
            f"{e.get('role', 'unknown')}: {(e.get('text') or '').strip()}"
            for e in evicted
            if (e.get("text") or "").strip()
        )
        if not turns_text:
            return ""
        prev_block = (
            f"\n\nPrevious summary (extend it):\n{previous}\n" if previous else ""
        )
        prompt = (
            "You are maintaining a rolling summary of a casual voice "
            "conversation between a user and a smart lamp companion. "
            f"Keep the summary under ~{self._summary_target_tokens} tokens. "
            "Track who said what, recurring themes, named entities, "
            "stated preferences, and any unresolved questions. Write in "
            "the same language the conversation uses (Vietnamese if the "
            "turns are Vietnamese)."
            f"{prev_block}\n\n"
            "New turns to absorb:\n"
            f"{turns_text}\n\n"
            "Return ONLY the updated summary text — no preamble, no "
            "markdown headings, no bullet lists unless natural."
        )
        try:
            text = llm_call(prompt)
        except Exception as e:
            logger.warning("rolling summary LLM call failed: %s", e)
            return ""
        return (text or "").strip()

    # --- day-rollover diary --------------------------------------------------

    def maybe_close_day(self, llm_call: Callable[[str], str]) -> None:
        """Check whether the local date has changed since the last call.
        If so, kick a background diary write for the just-closed day.

        Cheap (one date comparison + a lock acquire) — safe to call at
        the top of every ``decide()``."""
        today = _today_str()
        with self._lock:
            if today == self._last_day:
                return
            if self._diary_pending:
                return
            yesterday = self._last_day
            self._diary_pending = True
            self._last_day = today
            previous_rolling = self._rolling_text
            self._rolling_text = ""  # fresh window for the new day
            self._covered_until_ts = 0.0  # watermark resets with rolling text

        def run() -> None:
            try:
                if self._workspace.memory_has_entry_for(yesterday):
                    logger.info("diary: %s already in MEMORY.md — skip", yesterday)
                    return
                body = self._build_diary_body(yesterday, previous_rolling, llm_call)
                if body:
                    self._workspace.append_memory_entry(yesterday, body)
            except Exception as e:
                logger.warning("diary close-day failed: %s", e)
            finally:
                with self._lock:
                    self._diary_pending = False

        threading.Thread(target=run, daemon=True, name="brain-diary").start()

    def catch_up_unsummarized_days(self, llm_call: Callable[[str], str]) -> None:
        """Scan session files for past days that have data but no
        matching entry in MEMORY.md, and run the diary flow for each.

        Useful at startup when the service was off for a stretch:
        rolling summary in memory is empty for those days, but the raw
        session files still exist — we can still produce a diary from
        them. Runs sequentially in one background thread to avoid
        hammering the LLM provider."""
        if not self._workspace.session.enabled:
            return
        today = _today_str()
        candidates: list[str] = []
        for path in self._workspace.session.list_files(newest_first=False):
            day = path.stem  # YYYY-MM-DD
            if day >= today:
                continue
            if self._workspace.memory_has_entry_for(day):
                continue
            candidates.append(day)
        if not candidates:
            return

        def run() -> None:
            for day in candidates:
                try:
                    body = self._build_diary_body(day, "", llm_call)
                    if body:
                        self._workspace.append_memory_entry(day, body)
                except Exception as e:
                    logger.warning("diary catch-up for %s failed: %s", day, e)

        threading.Thread(
            target=run, daemon=True, name="brain-diary-catchup",
        ).start()
        logger.info("diary catch-up queued for %d day(s): %s",
                    len(candidates), candidates)

    def _build_diary_body(
        self,
        day: str,
        rolling_text: str,
        llm_call: Callable[[str], str],
    ) -> str:
        """Build the bullet-list diary entry for ``day`` from that day's
        session file + the rolling summary that was in memory when the
        day closed. Returns ``""`` if there isn't enough material."""
        session_path = self._workspace.session.file_for_date(day)
        raw_turns: list[dict] = []
        if session_path is not None and session_path.is_file():
            raw_turns = [
                r for r in self._workspace.session.read_file(session_path)
                if r.get("role") in ("user", "assistant")
                and (r.get("text") or "").strip()
            ]
        if not raw_turns and not rolling_text.strip():
            logger.info("diary: %s has no usable content — skip", day)
            return ""

        turns_excerpt = "\n".join(
            f"{r['role']}: {(r.get('text') or '').strip()}" for r in raw_turns
        )
        rolling_block = (
            f"\n\nRolling summary at end of day:\n{rolling_text}\n"
            if rolling_text.strip() else ""
        )
        prompt = (
            "Summarize one day of casual voice conversation between a "
            "user and a smart lamp companion into 3-6 short bullet "
            "points capturing what the user mentioned, recurring "
            f"themes, named entities, preferences, and unresolved "
            "questions. Write in the conversation's language "
            "(Vietnamese if the turns are Vietnamese). No introduction, "
            "no markdown headings — just bullet lines starting with "
            "'- '."
            f"\n\nDay: {day}"
            f"{rolling_block}"
            f"\n\nRaw conversation:\n{turns_excerpt}\n"
        )
        try:
            text = llm_call(prompt)
        except Exception as e:
            logger.warning("diary LLM call failed for %s: %s", day, e)
            return ""
        return (text or "").strip()
