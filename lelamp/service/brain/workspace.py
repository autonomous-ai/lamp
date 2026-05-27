"""
Brain workspace — file-side state for the half-cascade text brain,
mirroring the layout OpenClaw uses for its own workspace.

Layout (rooted at ``LELAMP_BRAIN_WORKSPACE``):

    <workspace>/<subdir>/
      MEMORY.md                     long-term curated memory (per-day diary
                                    appended by the summarizer; also loaded
                                    into the brain's static system prompt
                                    alongside OpenClaw's MEMORY.md)
      session/YYYY-MM-DD.jsonl      daily chit-chat log, 30d retention
      bench/YYYY-MM-DD.jsonl        daily decide() bench log, 7d retention

Each brain mode owns its own subdir so chit-chat / bench data stays
isolated across providers:

    <workspace>/call/                 — call mode (text router)
    <workspace>/live-gemini/          — live mode + Gemini Live
    <workspace>/live-openai/          — live mode + OpenAI Realtime

This is full layout-A isolation: switching mode mid-day means the new
mode starts with no chit-chat memory of what was said in another
mode's workspace. OpenClaw's IDENTITY/USER/MEMORY/SOUL/SKILLS stay
shared via ``OPENCLAW_WORKSPACE`` (separate filesystem tree), so the
user-side memory is never fragmented — only the brain's own
per-provider diary / chit-chat log is.

Per-subdir env overrides for absolute paths (rarely needed):
``LELAMP_BRAIN_WORKSPACE_CALL``, ``LELAMP_BRAIN_WORKSPACE_LIVE_GEMINI``,
``LELAMP_BRAIN_WORKSPACE_LIVE_OPENAI``.

Two parallel concerns live here:

  - :class:`_DailyJsonl` — append-only JSONL writer that rotates per local
    day and prunes files older than ``retention_days`` on init. Day
    boundary is detected on every write (so a long-running process
    crosses midnight cleanly without restart).

  - :class:`BrainWorkspace` — facade that resolves the workspace root from
    env, owns the two ``_DailyJsonl`` instances, and exposes
    ``MEMORY.md`` read/append. Owning these in one object keeps
    text_router.py readable.

Legacy migration:

  - Pre-1.x stored a single flat ``session.jsonl`` / ``bench.jsonl`` under
    ``/root/local/brain``. On init we move those into the new daily
    layout (best-effort, mtime → date bucket).
  - Pre-subdir layout stored ``session/``, ``bench/``, ``MEMORY.md``
    directly under ``LELAMP_BRAIN_WORKSPACE``. When ``subdir="call"`` is
    requested (the new home for the call-mode brain), we move those
    legacy paths into ``<root>/call/`` on first init — one-shot,
    idempotent (only runs when the target subdir doesn't already
    contain the file/dir).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lelamp.brain.workspace")

# Default workspace root — mirrors OpenClaw's /root/.openclaw/workspace
# style. Override via LELAMP_BRAIN_WORKSPACE.
_DEFAULT_WORKSPACE_ROOT = "/root/.brain/workspace"

_DEFAULT_SESSION_RETENTION_DAYS = 30
_DEFAULT_BENCH_RETENTION_DAYS = 7

# Pre-1.x layout — flat files under /root/local/brain. Used by the
# one-time migration on first run after upgrade.
_LEGACY_SESSION_FILE_DEFAULT = "/root/local/brain/session.jsonl"
_LEGACY_BENCH_FILE_DEFAULT = "/root/local/brain/bench.jsonl"

_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def _today_str() -> str:
    """Local-date string used for daily file names. Local TZ on purpose:
    the user thinks in local days; UTC would split conversations across
    files at weird times."""
    return datetime.now().strftime("%Y-%m-%d")


class _DailyJsonl:
    """Append-only JSONL writer with per-day file rotation and retention.

    Thread-safe writes (single lock per instance — voice loop is
    sequential but the summarizer thread may also append, e.g. a
    ``role: "summary"`` record).
    """

    def __init__(self, dir_path: Path, retention_days: int, label: str):
        self._dir = dir_path
        self._retention_days = max(0, retention_days)
        self._label = label
        self._lock = threading.Lock()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                "brain workspace %s dir %s unavailable: %s — disabled",
                label, self._dir, e,
            )
            self._dir = None  # type: ignore[assignment]
            return
        pruned = self._cleanup_expired()
        if pruned:
            logger.info(
                "brain workspace %s: pruned %d file(s) older than %d days",
                label, pruned, self._retention_days,
            )

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    @property
    def dir(self) -> Optional[Path]:
        return self._dir

    def today_path(self) -> Optional[Path]:
        if self._dir is None:
            return None
        return self._dir / f"{_today_str()}.jsonl"

    def write(self, record: dict) -> None:
        """Append one JSONL line to today's file. Best-effort — silent
        on disk errors so observability never breaks routing."""
        if self._dir is None:
            return
        path = self.today_path()
        if path is None:
            return
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as e:
            logger.debug("brain workspace %s write failed: %s", self._label, e)

    def list_files(self, newest_first: bool = True) -> list[Path]:
        """Return all dated JSONL files. Non-conforming filenames are
        skipped silently."""
        if self._dir is None:
            return []
        files = [
            p for p in self._dir.glob("*.jsonl")
            if _DATE_FILE_RE.match(p.name)
        ]
        files.sort(reverse=newest_first)
        return files

    def read_file(self, path: Path) -> list[dict]:
        """Parse one dated JSONL file into records. Silently drops
        malformed lines."""
        records: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning("brain workspace %s read %s failed: %s",
                           self._label, path, e)
        return records

    def load_tail_records(self, max_entries: int) -> list[dict]:
        """Walk newest → oldest dated files, accumulate up to
        ``max_entries`` records. Returned in chronological order."""
        if self._dir is None or max_entries <= 0:
            return []
        collected_rev: list[dict] = []
        for p in self.list_files(newest_first=True):
            if len(collected_rev) >= max_entries:
                break
            for record in reversed(self.read_file(p)):
                collected_rev.append(record)
                if len(collected_rev) >= max_entries:
                    break
        collected_rev.reverse()
        return collected_rev

    def file_for_date(self, day: str) -> Optional[Path]:
        """Return the path for a specific date string (YYYY-MM-DD), or
        ``None`` when the workspace is disabled."""
        if self._dir is None:
            return None
        return self._dir / f"{day}.jsonl"

    def _cleanup_expired(self) -> int:
        if self._dir is None or self._retention_days <= 0:
            return 0
        cutoff = date.today() - timedelta(days=self._retention_days)
        deleted = 0
        for p in self.list_files(newest_first=False):
            m = _DATE_FILE_RE.match(p.name)
            if not m:
                continue
            try:
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
        return deleted


class BrainWorkspace:
    """Filesystem-side state for one TextBrain instance.

    Resolves the workspace root from env, owns the two daily JSONL
    writers, and exposes ``MEMORY.md`` read/append. Treat as a single
    holder for "where do files live"; the brain's behavior logic stays
    in text_router.py and summarizer.py.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        session_retention_days: Optional[int] = None,
        bench_retention_days: Optional[int] = None,
        subdir: Optional[str] = None,
    ):
        # ``subdir`` namespaces a workspace under the shared root so
        # each brain mode (call / live-gemini / live-openai) owns an
        # isolated session+bench+MEMORY.md set. None = use the shared
        # root directly (pre-subdir layout — keep for callers that
        # haven't migrated; new code should always pass a subdir).
        self._subdir = subdir
        self._root: Optional[Path] = self._resolve_root(root, subdir)
        if self._root is None:
            self.session = _DailyJsonl.__new__(_DailyJsonl)
            self.session._dir = None  # type: ignore[attr-defined]
            self.bench = _DailyJsonl.__new__(_DailyJsonl)
            self.bench._dir = None  # type: ignore[attr-defined]
            self._memory_path: Optional[Path] = None
            self._memory_lock = threading.Lock()
            return

        # When a subdir is requested, hoist any legacy data sitting at
        # the shared root into the subdir before the daily writers
        # touch it. Only the call subdir owns legacy data; other
        # subdirs are new layout so the migration is a no-op for them.
        if subdir:
            self._migrate_root_to_subdir(subdir)

        session_retention = (
            session_retention_days
            if session_retention_days is not None
            else int(os.environ.get(
                "LELAMP_BRAIN_SESSION_RETENTION_DAYS",
                _DEFAULT_SESSION_RETENTION_DAYS,
            ))
        )
        bench_retention = (
            bench_retention_days
            if bench_retention_days is not None
            else int(os.environ.get(
                "LELAMP_BRAIN_BENCH_RETENTION_DAYS",
                _DEFAULT_BENCH_RETENTION_DAYS,
            ))
        )
        self.session = _DailyJsonl(
            self._root / "session", session_retention, f"session[{subdir or 'root'}]",
        )
        self.bench = _DailyJsonl(
            self._root / "bench", bench_retention, f"bench[{subdir or 'root'}]",
        )
        self._memory_path = self._root / "MEMORY.md"
        self._memory_lock = threading.Lock()

        self._migrate_legacy_files()

        logger.info(
            "brain workspace ready at %s (subdir=%s, session retention=%dd, bench retention=%dd)",
            self._root, subdir or "(root)", session_retention, bench_retention,
        )

    # Per-subdir env override knobs. Allow an operator to point one
    # mode's workspace at a completely different filesystem location
    # (e.g. a separate disk for OpenAI's bench data) without touching
    # the shared root env.
    _SUBDIR_ENV_OVERRIDE = {
        "call":         "LELAMP_BRAIN_WORKSPACE_CALL",
        "live-gemini":  "LELAMP_BRAIN_WORKSPACE_LIVE_GEMINI",
        "live-openai":  "LELAMP_BRAIN_WORKSPACE_LIVE_OPENAI",
    }

    @classmethod
    def _resolve_root(
        cls,
        explicit: Optional[Path],
        subdir: Optional[str],
    ) -> Optional[Path]:
        # Explicit Path argument wins absolutely — used by tests + code
        # paths that already computed the right directory.
        if explicit is not None:
            raw = str(explicit)
            final = Path(raw)
        else:
            # Per-subdir absolute-path override wins next, so an
            # operator can park one mode's workspace on a different
            # disk without touching LELAMP_BRAIN_WORKSPACE.
            override_env = cls._SUBDIR_ENV_OVERRIDE.get(subdir or "") if subdir else None
            override_raw = os.environ.get(override_env, "").strip() if override_env else ""
            if override_raw:
                raw = override_raw
                final = Path(raw)
            else:
                raw = os.environ.get("LELAMP_BRAIN_WORKSPACE", _DEFAULT_WORKSPACE_ROOT).strip()
                if subdir:
                    final = Path(raw) / subdir
                else:
                    final = Path(raw)
        if not raw or raw.lower() in ("off", "none", "disabled", "/dev/null"):
            logger.info("brain workspace disabled (LELAMP_BRAIN_WORKSPACE=%r)", raw)
            return None
        try:
            final.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("brain workspace root %s unavailable: %s — disabled", final, e)
            return None
        return final

    def _migrate_root_to_subdir(self, subdir: str) -> None:
        """One-shot migration for the pre-subdir layout.

        Pre-1.x stored ``session/``, ``bench/``, ``MEMORY.md`` directly
        under the shared root. The first time we boot with ``subdir``
        in use, hoist those into ``<root>/<subdir>/`` so historical
        chit-chat / bench / diary survives the layout change.

        Only safe for the subdir that historically owned the data
        (i.e. ``call`` — the only mode that wrote here before). For
        other subdirs we no-op; their workspaces start empty.

        Idempotent: skips any file/dir that already exists at the
        destination so a second run is a no-op.
        """
        if self._root is None or subdir != "call":
            return
        # ``self._root`` here is ``<shared_root>/call`` (or the per-
        # subdir override). The legacy location is the parent — but
        # ONLY when we resolved via the default root + subdir join,
        # not when the override env was used (those point at an
        # arbitrary path with no legacy ancestor to scan).
        shared_root_raw = os.environ.get("LELAMP_BRAIN_WORKSPACE", _DEFAULT_WORKSPACE_ROOT).strip()
        override_env = self._SUBDIR_ENV_OVERRIDE.get(subdir, "")
        if override_env and os.environ.get(override_env, "").strip():
            return  # operator points override at custom path — no legacy ancestor
        shared_root = Path(shared_root_raw)
        if not shared_root.is_dir() or shared_root == self._root:
            return
        for name in ("session", "bench"):
            src = shared_root / name
            dst = self._root / name
            if src.is_dir() and not dst.exists():
                try:
                    src.rename(dst)
                    logger.info(
                        "brain workspace: hoisted legacy %s → %s", src, dst,
                    )
                except OSError as e:
                    logger.warning(
                        "brain workspace: legacy hoist %s → %s failed: %s",
                        src, dst, e,
                    )
        legacy_memory = shared_root / "MEMORY.md"
        target_memory = self._root / "MEMORY.md"
        if legacy_memory.is_file() and not target_memory.exists():
            try:
                legacy_memory.rename(target_memory)
                logger.info(
                    "brain workspace: hoisted legacy %s → %s",
                    legacy_memory, target_memory,
                )
            except OSError as e:
                logger.warning(
                    "brain workspace: MEMORY.md hoist %s → %s failed: %s",
                    legacy_memory, target_memory, e,
                )
        return path

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Optional[Path]:
        return self._root

    # --- MEMORY.md -----------------------------------------------------------

    def read_memory_md(self) -> str:
        """Return the full text of brain MEMORY.md, or ``""`` when the
        workspace is disabled / file missing. No tail-trim here — the
        caller (context_loader) applies the shared cap on the merged
        memory block."""
        if self._memory_path is None or not self._memory_path.is_file():
            return ""
        try:
            with self._memory_lock:
                return self._memory_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("brain MEMORY.md read failed: %s", e)
            return ""

    def memory_has_entry_for(self, day: str) -> bool:
        """True when MEMORY.md already contains an ``## <day>`` heading —
        used by the diary close-day flow to skip days that were already
        summarized in a previous run."""
        text = self.read_memory_md()
        if not text:
            return False
        return f"## {day}" in text

    def append_memory_entry(self, day: str, body: str) -> None:
        """Append a dated diary entry to MEMORY.md. Format:

            ## YYYY-MM-DD
            <body>

        Idempotent: if a heading for ``day`` already exists the call is
        a no-op (we don't want duplicate entries when the summarizer
        catches up across a long restart)."""
        if self._memory_path is None:
            return
        body = (body or "").strip()
        if not body:
            return
        if self.memory_has_entry_for(day):
            logger.info("brain MEMORY.md already has entry for %s — skip", day)
            return
        block = f"\n## {day}\n{body}\n"
        try:
            with self._memory_lock:
                with open(self._memory_path, "a", encoding="utf-8") as f:
                    f.write(block)
            logger.info("brain MEMORY.md appended diary for %s (%d chars)",
                        day, len(body))
        except OSError as e:
            logger.warning("brain MEMORY.md append failed: %s", e)

    # --- one-time legacy migration ------------------------------------------

    def _migrate_legacy_files(self) -> None:
        """Move pre-rotation flat files into the daily layout. Runs once
        per file (we rename the legacy file as we migrate, so re-runs are
        no-ops). Date bucket is derived from file mtime — close enough
        for a one-time historical move."""
        if self._root is None:
            return
        for env_key, default, target in (
            ("LELAMP_BRAIN_SESSION_LOG", _LEGACY_SESSION_FILE_DEFAULT, self.session),
            ("LELAMP_BRAIN_BENCH_LOG", _LEGACY_BENCH_FILE_DEFAULT, self.bench),
        ):
            legacy_raw = os.environ.get(env_key, default).strip()
            if not legacy_raw or legacy_raw.lower() in ("off", "none", "disabled", "/dev/null"):
                continue
            legacy = Path(legacy_raw)
            if not legacy.is_file() or target.dir is None:
                continue
            try:
                mtime = datetime.fromtimestamp(legacy.stat().st_mtime).strftime("%Y-%m-%d")
                dest = target.dir / f"{mtime}.jsonl"
                if dest.exists():
                    # Don't clobber an existing daily file — append instead.
                    with open(legacy, "r", encoding="utf-8", errors="replace") as src, \
                         open(dest, "a", encoding="utf-8") as dst:
                        shutil.copyfileobj(src, dst)
                    legacy.unlink()
                    logger.info(
                        "brain workspace: appended legacy %s into existing %s",
                        legacy, dest,
                    )
                else:
                    legacy.rename(dest)
                    logger.info(
                        "brain workspace: migrated legacy %s → %s",
                        legacy, dest,
                    )
            except OSError as e:
                logger.warning(
                    "brain workspace: legacy migration of %s failed: %s",
                    legacy, e,
                )
