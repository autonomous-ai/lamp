"""
Brain context loader — mirrors what OpenClaw sees when it answers a turn,
minus skills (skills = task = falls through to OpenClaw anyway).

Sources (each best-effort, falls through silently on failure):
    SOUL.md          — persona block. File path: $OPENCLAW_WORKSPACE/SOUL.md
                       (default /root/.openclaw/workspace/SOUL.md on Pi).
    Session history  — OpenClaw's own per-session JSONL. Same data the
                       `chat.history` WS RPC would return, just read straight
                       from the source of truth at
                       $OPENCLAW_AGENTS_DIR/main/sessions/<id>.jsonl
                       where <id> comes from sessions.json indexed by
                       sessionKey (default `agent:main:main`).
    Lumi /api/agent  — last-resort fallback when the workspace isn't visible
                       (e.g. on a Mac dev box where Pi files are absent).

If everything is missing the loader returns empty strings — the brain still
boots, it just answers without persona / history.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import requests

logger = logging.getLogger("lelamp.brain.context")

DEFAULT_WORKSPACE = "/root/.openclaw"
DEFAULT_WORKSPACE_SUBDIR = "workspace"     # where SOUL.md lives
DEFAULT_AGENTS_SUBDIR = "agents/main"      # where sessions/ lives
DEFAULT_SESSION_KEY = "agent:main:main"
DEFAULT_LUMI_BASE = "http://127.0.0.1:5000"
DEFAULT_HISTORY_LIMIT = 20
DEFAULT_HTTP_TIMEOUT = 1.5

# Tokens we strip from history because they're plumbing noise — the brain
# doesn't need to "see" them and including them blows up the prompt.
_HEARTBEAT_TOKENS = (
    "[OpenClaw heartbeat poll]",
    "HEARTBEAT_OK",
)

# Strip OpenClaw plumbing from history text. Without this the chit-chat
# brain sees a stream of `[HW:/emotion:{...}]`, `[sensing:presence.enter]`,
# `[context: {...}]` etc. — model interprets the conversation as "every
# user turn is a task" and over-delegates simple greetings.
_PLUMBING_PATTERNS = [
    # Hardware commands the agent emits: [HW:/path:{json}] possibly with
    # nested braces. Match up to the matching closing bracket greedily.
    re.compile(r"\[HW:/[^\]]*\]"),
    # Multi-key context blobs: [context: ...], [wellbeing_context: {...}],
    # [emotion_context: {...}], [presence_context: {...}].
    re.compile(r"\[[a-z_]*context:[^\]]*\]", re.IGNORECASE),
    # Sensing / emotion / activity / speech_emotion / ambient / voice tags
    # in the leading-bracket form: [sensing:presence.enter], [emotion]…
    re.compile(
        r"\[(?:sensing|emotion|activity|speech_emotion|ambient|user|voice|mood/log|music-suggestion/log|wellbeing/log)[^\]]*\]",
        re.IGNORECASE,
    ),
    # Date/time headers: "[Thu 2026-05-21 12:00 GMT+7]".
    re.compile(r"\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}[^\]]*\]"),
    # Operator notes Gemini doesn't need: "[No crons to cancel...]" etc.
    re.compile(r"\[No crons to cancel[^\]]*\]"),
    # Bare "NO_REPLY" used by the agent to signal "stay silent".
    re.compile(r"\bNO_REPLY\b"),
]


def _clean_openclaw_text(text: str) -> str:
    """Strip OpenClaw plumbing so the brain sees only conversational text.

    Returns the cleaned string, or "" if nothing meaningful is left.
    Preserves emotional/tone markers like ``[chuckle]``, ``[sigh]`` —
    those help the brain mirror voice character.
    """
    cleaned = text
    for pat in _PLUMBING_PATTERNS:
        cleaned = pat.sub("", cleaned)
    # Collapse whitespace + drop blank lines
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())
    return cleaned.strip()


@dataclass
class Turn:
    """One previous chat turn (user or assistant)."""

    role: str        # "user" | "assistant"
    text: str
    time: str = ""   # ISO8601 if available


@dataclass
class BrainContext:
    identity: str = ""               # full IDENTITY.md (given name, species, traits)
    identity_name: str = ""          # just the parsed given name (Noah, etc.) for quick checks
    user_profile: str = ""           # full USER.md (owner — name, preferences, timezone, …)
    memory: str = ""                 # curated long-term memory — OpenClaw workspace/memory/*.md (newest tail) or MEMORY.md
    brain_memory: str = ""           # brain-curated per-day diary entries — brain workspace/MEMORY.md
    knowledge: str = ""              # KNOWLEDGE.md — mistakes the agent learned to not repeat
    soul: str = ""                   # full SOUL.md (persona narrative)
    skills_catalog: str = ""         # `name: description` lines pulled from workspace/skills/*/SKILL.md (OpenClaw's authoritative skill list — used as the brain's delegate trigger)
    recent_turns: List[Turn] = field(default_factory=list)
    workspace_dir: str = ""

    def to_system_prompt_block(self) -> str:
        """Render context as a single block suitable for prepending to the
        brain's system instruction. Empty sections are skipped silently.

        Order reflects the review-notes recommendation:
          1. IDENTITY  — who *I* am (given name first so model never invents).
          2. OWNER     — who the *user* is (USER.md).
          3. MEMORY    — long-term curated facts.
          4. KNOWLEDGE — mistakes / corrections the agent has learned.
          5. PERSONA   — narrative tone (SOUL.md).
          6. RECENT    — recent conversation turns (last so they don't
                          shadow persona / identity).
        """
        parts: list[str] = []
        if self.identity.strip():
            parts.append("=== IDENTITY (IDENTITY.md) ===\n" + self.identity.strip())
        if self.user_profile.strip():
            parts.append(
                "=== OWNER / USER PROFILE (USER.md) ===\n" + self.user_profile.strip()
            )
        if self.memory.strip() or self.brain_memory.strip():
            # OpenClaw-curated memory first, brain's per-day chit-chat
            # diary appended underneath as a labelled sub-section. Two
            # separators are intentional — they keep the boundary visible
            # so the model treats "things Lumi the agent decided to
            # remember" and "things the voice front door has been
            # hearing in chit-chat" as separate tracks, while still
            # living in the same MEMORY block.
            memory_parts: list[str] = []
            if self.memory.strip():
                memory_parts.append(self.memory.strip())
            if self.brain_memory.strip():
                memory_parts.append(
                    "--- BRAIN CHIT-CHAT SUMMARIES ---\n"
                    + self.brain_memory.strip()
                )
            parts.append(
                "=== LONG-TERM MEMORY ===\n" + "\n\n".join(memory_parts)
            )
        if self.knowledge.strip():
            parts.append(
                "=== KNOWLEDGE (KNOWLEDGE.md — mistakes to avoid) ===\n"
                + self.knowledge.strip()
            )
        if self.skills_catalog.strip():
            # OpenClaw's skill catalog — list of `name: description` lines.
            # This is the authoritative source for "what Lumi can do that
            # the brain can't simulate" — the DECISION_RULES leans on this
            # block as the delegate trigger, so when OpenClaw adds a new
            # skill the brain picks it up on next restart with zero prompt
            # changes.
            parts.append(
                "=== OPENCLAW SKILLS (delegate when the user wants any of these) ===\n"
                + self.skills_catalog.strip()
            )
        if self.soul.strip():
            parts.append("=== PERSONA (SOUL.md) ===\n" + self.soul.strip())
        if self.recent_turns:
            # Prefix each turn with [HH:MM] so the model can answer
            # "what did I say 5 minutes ago" / "anh nói gì nãy" with
            # actual temporal grounding. OpenAI Realtime has no
            # message-level timestamp field (verified against their
            # SDK schema 2026-05-27), so embedding in text is the
            # only path. We also emit a CURRENT TIME line right
            # before the conversation so the model has the reference
            # clock to compute deltas against.
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            convo_lines: list[str] = []
            for t in self.recent_turns:
                if not t.text.strip():
                    continue
                clock = _fmt_clock(t.time)
                prefix = f"[{clock}] " if clock else ""
                convo_lines.append(f"{prefix}{t.role}: {t.text}")
            convo = "\n".join(convo_lines)
            if convo:
                parts.append(
                    f"=== CURRENT TIME ===\n{now_str} (server clock — use to "
                    f"compute how long ago each [HH:MM] turn happened)\n\n"
                    f"=== RECENT CONVERSATION ===\n{convo}"
                )
        return "\n\n".join(parts)


def _read_identity(workspace_dir: str) -> tuple[str, str]:
    """Read IDENTITY.md from the workspace.

    Returns ``(raw_text, parsed_name)``. ``raw_text`` is the full file
    contents (used as the prompt block); ``parsed_name`` extracts the
    ``**Name:**`` line the same way ``app_state._read_agent_name`` does
    so the brain's prompt block exposes the canonical given name
    (Noah, etc.) without each provider re-parsing.

    Returns ``("", "")`` when the file is missing or unreadable —
    callers should treat empty as "no identity injected".
    """
    path = Path(workspace_dir) / "IDENTITY.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info("IDENTITY.md not found at %s — brain runs without given name", path)
        return "", ""
    except OSError as e:
        logger.warning("Could not read IDENTITY.md at %s: %s", path, e)
        return "", ""

    name = ""
    for line in raw.splitlines():
        lower = line.lower()
        idx = lower.find("**name:**")
        if idx >= 0:
            # Strip the marker + anything after em-dash / en-dash / hyphen
            # so "**Name:** Noah — a curious lamp" yields "Noah".
            tail = line[idx + len("**name:**"):].strip()
            for sep in ("—", "–", "-"):
                tail = tail.split(sep)[0].strip()
            if tail:
                name = tail
                break
    return raw, name


def _read_soul(workspace_dir: str) -> str:
    path = Path(workspace_dir) / "SOUL.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info("SOUL.md not found at %s — brain will run without persona", path)
        return ""
    except OSError as e:
        logger.warning("Could not read SOUL.md at %s: %s", path, e)
        return ""


# Maximum description chars retained per skill — keeps the catalog block
# bounded even if a skill author writes a thesis in its frontmatter.
# Skills with longer descriptions are truncated with an ellipsis; the
# brain only needs enough to know whether to delegate, not the full
# workflow (that's OpenClaw's job).
_SKILL_DESC_MAX_CHARS = int(os.environ.get("LELAMP_BRAIN_SKILL_DESC_MAX", "400"))


def _parse_skill_frontmatter(text: str) -> tuple[str, str]:
    """Extract ``(name, description)`` from a SKILL.md's YAML
    frontmatter. Returns ``("", "")`` when the file has no frontmatter
    or the fields are missing. We parse manually instead of pulling in
    PyYAML — the frontmatter shape is fixed (two flat string fields),
    not worth a dependency."""
    if not text.startswith("---"):
        return "", ""
    end = text.find("\n---", 3)
    if end < 0:
        return "", ""
    fm = text[3:end].strip()
    name = ""
    description = ""
    current: Optional[str] = None  # which field we're currently appending to
    buffer: list[str] = []
    for line in fm.splitlines():
        # New key starts when the line begins with `<key>:` at column 0.
        stripped_left = line.lstrip()
        is_indented = stripped_left != line
        head = line.split(":", 1)
        if not is_indented and len(head) == 2 and head[0].strip() in ("name", "description"):
            # Flush any pending field, then start the new one.
            if current == "name":
                name = " ".join(buffer).strip()
            elif current == "description":
                description = " ".join(buffer).strip()
            current = head[0].strip()
            buffer = [head[1].strip()]
        elif current and is_indented:
            # Continuation line for a folded multi-line value.
            buffer.append(stripped_left)
    if current == "name":
        name = " ".join(buffer).strip()
    elif current == "description":
        description = " ".join(buffer).strip()
    return name, description


def _read_skills_catalog(workspace_dir: str) -> str:
    """Scan ``<workspace>/skills/*/SKILL.md`` and return a newline-
    separated catalog of ``- name: description`` lines for injection
    into the brain's static system prompt.

    The catalog is the authoritative "what OpenClaw can do that the
    brain can't simulate" list. When OpenClaw adds a new skill, the
    brain picks it up on the next process restart with zero prompt
    changes. Skills without parseable frontmatter are skipped silently.

    Returns ``""`` when the skills dir is missing or empty — brain
    falls back to the static DECISION_RULES bucket list in that case.
    """
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.is_dir():
        logger.info("skills dir not found at %s — brain runs without skill catalog", skills_dir)
        return ""
    entries: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Could not read %s: %s", skill_md, e)
            continue
        name, description = _parse_skill_frontmatter(text)
        if not name:
            # Fall back to the parent folder name when frontmatter is
            # absent — better than dropping the skill silently.
            name = skill_md.parent.name
        if not description:
            continue
        if len(description) > _SKILL_DESC_MAX_CHARS:
            description = description[: _SKILL_DESC_MAX_CHARS - 1].rstrip() + "…"
        entries.append(f"- {name}: {description}")
    if not entries:
        logger.info("no skill entries found under %s", skills_dir)
        return ""
    logger.info("skills catalog loaded from %s (%d skills)", skills_dir, len(entries))
    return "\n".join(entries)


# Per-block size cap so a curated MEMORY.md that grows over time
# doesn't quietly balloon the brain prompt to 50K tokens. Drops the
# OLDEST half if exceeded — assumes append-style writes where the tail
# is the freshest (most relevant) information. Tune via env if the
# behaviour ever surprises.
_USER_MD_MAX_CHARS = int(os.environ.get("LELAMP_BRAIN_USER_MD_MAX", "3000"))
_MEMORY_MD_MAX_CHARS = int(os.environ.get("LELAMP_BRAIN_MEMORY_MD_MAX", "5000"))
_KNOWLEDGE_MD_MAX_CHARS = int(os.environ.get("LELAMP_BRAIN_KNOWLEDGE_MD_MAX", "2000"))
# When memory lives as workspace/memory/*.md timestamped files (the
# OpenClaw default), how many of the newest files to concatenate. 3 is
# typically enough to cover the latest day's snapshots without bloating
# the prompt.
_MEMORY_FILES_KEEP = int(os.environ.get("LELAMP_BRAIN_MEMORY_FILES_KEEP", "3"))


def _read_capped(path: Path, max_chars: int) -> str:
    """Read a markdown context file, return its (possibly tail-trimmed)
    contents. Files past the cap keep their tail (newest entries) so the
    truncation doesn't drop recent updates."""
    text = path.read_text(encoding="utf-8")
    if len(text) <= max_chars:
        return text
    # Keep the tail — the assumption is curated context files grow by
    # appending new facts at the bottom.
    return "... (older entries truncated) ...\n" + text[-max_chars:]


def _read_user(workspace_dir: str) -> str:
    """Read USER.md from the workspace. Returns "" when the file is
    missing — the brain just runs without owner context. We keep the
    file even when its template fields are unfilled (Name / Pronouns /
    Timezone all `_`) so the model sees the *shape* of the missing
    profile and can ask the user to introduce themselves, instead of
    inventing details from session history."""
    path = Path(workspace_dir) / "USER.md"
    try:
        return _read_capped(path, _USER_MD_MAX_CHARS)
    except FileNotFoundError:
        logger.info("USER.md not found at %s — brain runs without owner context", path)
        return ""
    except OSError as e:
        logger.warning("Could not read USER.md at %s: %s", path, e)
        return ""


def _read_memory(workspace_dir: str) -> str:
    """Read curated long-term memory from the workspace.

    Two layouts are supported, in priority order:

      1. ``workspace/memory/*.md`` — the OpenClaw default. Each file is
         a memory snapshot timestamped in its name (e.g.
         ``2026-05-21-0929.md``). We concatenate the last few files
         (newest tail = chronologically latest = freshest memory) and
         tail-trim the whole thing to ``_MEMORY_MD_MAX_CHARS``.
      2. ``workspace/MEMORY.md`` — a single curated file if the deploy
         maintains memory that way. Tail-trimmed.

    Missing both → return ``""`` (brain runs without long-term memory).
    """
    memory_dir = Path(workspace_dir) / "memory"
    if memory_dir.is_dir():
        files = sorted(memory_dir.glob("*.md"))
        if files:
            recent = files[-_MEMORY_FILES_KEEP:]
            chunks: list[str] = []
            for f in recent:
                try:
                    chunks.append(f"--- {f.name} ---\n" + f.read_text(encoding="utf-8"))
                except OSError as e:
                    logger.warning("Could not read memory file %s: %s", f, e)
            combined = "\n\n".join(chunks)
            if len(combined) > _MEMORY_MD_MAX_CHARS:
                combined = "... (older entries truncated) ...\n" + combined[-_MEMORY_MD_MAX_CHARS:]
            return combined

    # Fallback: single MEMORY.md
    path = Path(workspace_dir) / "MEMORY.md"
    try:
        return _read_capped(path, _MEMORY_MD_MAX_CHARS)
    except FileNotFoundError:
        logger.info(
            "no memory/ dir and no MEMORY.md at %s — brain runs without long-term memory",
            workspace_dir,
        )
        return ""
    except OSError as e:
        logger.warning("Could not read MEMORY.md at %s: %s", path, e)
        return ""


def _read_knowledge(workspace_dir: str) -> str:
    """Read KNOWLEDGE.md from the workspace — the OpenClaw "mistakes to
    not repeat" log. Typically very short (the default template is just
    a comment line). When the workspace agent has populated it with
    real lessons-learned, this gives the brain a chance to avoid the
    same misstep when answering chit-chat."""
    path = Path(workspace_dir) / "KNOWLEDGE.md"
    try:
        text = path.read_text(encoding="utf-8")
        if len(text) > _KNOWLEDGE_MD_MAX_CHARS:
            text = "... (older entries truncated) ...\n" + text[-_KNOWLEDGE_MD_MAX_CHARS:]
        return text
    except FileNotFoundError:
        logger.info("KNOWLEDGE.md not found at %s — brain runs without it", path)
        return ""
    except OSError as e:
        logger.warning("Could not read KNOWLEDGE.md at %s: %s", path, e)
        return ""


def _flatten_content_text(content: Any) -> str:
    """OpenClaw's `message.content` is either a string or a list of parts
    `{type: "text"|"thinking"|"toolCall"|..., text|name: ...}`. We keep only
    `type=="text"` parts; thinking/toolCall noise hurts the chit-chat
    prompt without adding signal."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") != "text":
            continue
        text = (p.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _read_openclaw_history(
    agents_dir: str, session_key: str, limit: int
) -> List["Turn"]:
    """Read OpenClaw's main-session JSONL — same source the `chat.history`
    WS RPC reads. Returns last `limit` user/assistant turns with the
    heartbeat plumbing filtered out."""
    sessions_dir = Path(agents_dir) / "sessions"
    index_path = sessions_dir / "sessions.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("OpenClaw sessions index not found at %s", index_path)
        return []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read OpenClaw sessions index %s: %s", index_path, e)
        return []

    entry = index.get(session_key)
    if not isinstance(entry, dict):
        logger.info("Session %r not in OpenClaw index %s", session_key, index_path)
        return []

    session_file = entry.get("sessionFile")
    if not session_file:
        logger.info("Session %r in index has no sessionFile", session_key)
        return []

    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        logger.info("Session file %s missing — likely just rotated", session_file)
        return []
    except OSError as e:
        logger.warning("Could not read session file %s: %s", session_file, e)
        return []

    turns: List[Turn] = []
    # OpenClaw JSONL grows append-only; scan back-to-front so we can stop
    # as soon as we have enough turns even on a multi-MB file.
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "message":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _flatten_content_text(msg.get("content"))
        if not text:
            continue
        if any(token in text for token in _HEARTBEAT_TOKENS):
            continue
        text = _clean_openclaw_text(text)
        if not text:
            continue
        turns.append(Turn(role=role, text=text, time=str(obj.get("timestamp", ""))))
        if len(turns) >= limit:
            break

    turns.reverse()  # back to chronological order
    return turns


def _fetch_recent_turns(
    lumi_base: str, limit: int, timeout: float
) -> List[Turn]:
    """Hit Lumi's /api/agent/recent and condense the MonitorEvent stream into
    a list of (role, text) turns. We keep only user inputs and assistant
    responses — sensing/control events are noise for chit-chat context."""
    url = f"{lumi_base.rstrip('/')}/api/agent/recent"
    try:
        resp = requests.get(url, params={"last": limit * 4}, timeout=timeout)
    except requests.RequestException as e:
        logger.info("Could not fetch recent turns from %s: %s", url, e)
        return []
    if resp.status_code != 200:
        logger.info("recent turns endpoint returned %d — skipping history", resp.status_code)
        return []
    try:
        events = resp.json()
    except ValueError:
        logger.warning("recent turns endpoint did not return JSON")
        return []

    turns: List[Turn] = []
    for ev in events if isinstance(events, list) else []:
        etype = (ev.get("type") or "").lower()
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        if etype in ("chat_input", "user_input", "voice_input", "sensing_input"):
            turns.append(Turn(role="user", text=summary, time=ev.get("time", "")))
        elif etype in ("chat_response", "assistant_reply", "agent_reply"):
            turns.append(Turn(role="assistant", text=summary, time=ev.get("time", "")))
    return turns[-limit:]


_DATE_JSONL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def _turn_epoch(time_field: Any) -> float:
    """Best-effort conversion of a Turn-style timestamp field to epoch
    seconds. Accepts ISO 8601 strings (with or without ``Z``), numeric
    strings, raw numbers (seconds or millis — auto-detected by
    magnitude), or empty. Unparseable / empty values sort to 0 (oldest)
    so they don't accidentally float to the top of a sorted history."""
    if time_field is None or time_field == "":
        return 0.0
    if isinstance(time_field, (int, float)):
        v = float(time_field)
        return v / 1000.0 if v > 1e11 else v
    s = str(time_field).strip()
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except (ValueError, TypeError):
        return 0.0


def _fmt_clock(time_field: Any) -> str:
    """Format a Turn-style timestamp as ``HH:MM`` in the server's local
    timezone. Returns ``""`` when the field is unparseable so callers
    can decide to either skip the prefix or fall back to a relative
    label like ``[?]``. Pairs with the CURRENT TIME block in
    ``BrainContext.to_system_prompt_block`` — same clock on both sides
    so the model can compute deltas without timezone math."""
    epoch = _turn_epoch(time_field)
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M")
    except (OverflowError, ValueError):
        return ""


def _load_extra_session_turns(
    session_dir: Optional[str], limit: int,
) -> List["Turn"]:
    """Read tail records from a brain workspace's ``session/`` dir
    (the daily JSONL layout that BrainWorkspace writes). Used to seed
    live brain sessions with chit-chat history from previous sessions
    so a GoAway / idle-close cycle doesn't drop the conversation
    memory on the floor.

    Record shape matches what ``TextBrain._append_session_turn`` and
    ``LiveBrainRunner`` write::

        {"role": "user"|"assistant", "text": "...", "ts": <epoch_seconds>}

    Walks newest → oldest dated files until ``limit`` records are
    collected, returns chronological order. Malformed / non-conforming
    lines are silently skipped."""
    if not session_dir or limit <= 0:
        return []
    dir_path = Path(session_dir)
    if not dir_path.is_dir():
        return []
    files = sorted(
        (p for p in dir_path.glob("*.jsonl") if _DATE_JSONL_RE.match(p.name)),
        reverse=True,
    )
    collected_rev: List[Turn] = []
    for p in files:
        if len(collected_rev) >= limit:
            break
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            logger.debug("extra session read %s failed: %s", p, e)
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role")
            if role not in ("user", "assistant"):
                continue
            text = (obj.get("text") or "").strip()
            if not text:
                continue
            # ``ts`` (epoch) is the canonical brain-side stamp;
            # ``time`` would be the OpenClaw-side ISO string. Accept
            # either so this loader works against any JSONL that
            # follows the {role,text,time-ish} shape.
            ts_field = obj.get("ts", obj.get("time", ""))
            collected_rev.append(Turn(role=role, text=text, time=str(ts_field)))
            if len(collected_rev) >= limit:
                break
    collected_rev.reverse()
    return collected_rev


def _merge_turns_by_time(
    primary: List["Turn"], extra: List["Turn"], limit: int,
) -> List["Turn"]:
    """Merge two Turn lists chronologically, cap at ``limit`` (keep the
    newest tail). Stable sort: when timestamps tie, ``primary``
    (OpenClaw) wins position over ``extra`` (brain session) so the
    canonical user-facing history stays consistent."""
    if not extra:
        return primary[-limit:] if limit > 0 else primary
    if not primary:
        return extra[-limit:] if limit > 0 else extra
    # Attach origin tag for stable tiebreak (primary < extra).
    tagged: list[tuple[float, int, Turn]] = []
    for t in primary:
        tagged.append((_turn_epoch(t.time), 0, t))
    for t in extra:
        tagged.append((_turn_epoch(t.time), 1, t))
    tagged.sort(key=lambda x: (x[0], x[1]))
    merged = [t for _, _, t in tagged]
    if limit > 0 and len(merged) > limit:
        merged = merged[-limit:]
    return merged


def _resolve_paths(
    workspace_dir: Optional[str],
    agents_dir: Optional[str],
) -> tuple[str, str]:
    """Resolve the two on-device paths the loader needs.

    `OPENCLAW_WORKSPACE` historically pointed at `…/workspace` (where
    SOUL.md lives), but OpenClaw's sessions live under
    `<openclaw_root>/agents/main/sessions`. We accept either layout —
    if the user points OPENCLAW_WORKSPACE at the workspace subdir we
    derive the agents dir from its parent.
    """
    if workspace_dir:
        ws = workspace_dir
    else:
        env_ws = os.environ.get("OPENCLAW_WORKSPACE")
        ws = env_ws or f"{DEFAULT_WORKSPACE}/{DEFAULT_WORKSPACE_SUBDIR}"

    if agents_dir:
        ad = agents_dir
    else:
        env_ad = os.environ.get("OPENCLAW_AGENTS_DIR")
        if env_ad:
            ad = env_ad
        else:
            # ws is .../workspace → openclaw root is its parent
            ws_path = Path(ws)
            root = ws_path.parent if ws_path.name == DEFAULT_WORKSPACE_SUBDIR else Path(DEFAULT_WORKSPACE)
            ad = str(root / DEFAULT_AGENTS_SUBDIR)
    return ws, ad


def load_context(
    workspace_dir: Optional[str] = None,
    agents_dir: Optional[str] = None,
    session_key: Optional[str] = None,
    lumi_base_url: Optional[str] = None,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    include_history: bool = True,
    extra_session_dir: Optional[str] = None,
) -> BrainContext:
    """Load brain context. All sources are best-effort — failures degrade
    gracefully to an empty section.

    History resolution order:
        1. OpenClaw session JSONL (exact mirror of chat.history)
        2. Lumi /api/agent/recent (only used if #1 returned nothing — for
           dev machines without the workspace mounted)

    If ``extra_session_dir`` is provided, it should point at a
    ``BrainWorkspace.session.dir`` (the per-mode JSONL directory). Any
    chit-chat turns found there are merged chronologically with the
    OpenClaw history before the limit is applied. Used by live brain
    sessions to inherit their own previous-session chit-chat across
    GoAway / idle-close cycles without polluting OpenClaw's history.
    """
    workspace_dir, agents_dir = _resolve_paths(workspace_dir, agents_dir)
    session_key = session_key or os.environ.get("OPENCLAW_SESSION_KEY") or DEFAULT_SESSION_KEY
    lumi_base_url = lumi_base_url or os.environ.get("LUMI_BASE_URL") or DEFAULT_LUMI_BASE

    identity, identity_name = _read_identity(workspace_dir)
    user_profile = _read_user(workspace_dir)
    memory = _read_memory(workspace_dir)
    soul = _read_soul(workspace_dir)
    skills_catalog = _read_skills_catalog(workspace_dir)

    turns: List[Turn] = []
    history_source = "none"
    extra_turns_n = 0
    if include_history:
        turns = _read_openclaw_history(agents_dir, session_key, history_limit)
        if turns:
            history_source = "openclaw_jsonl"
        else:
            turns = _fetch_recent_turns(lumi_base_url, history_limit, timeout)
            if turns:
                history_source = "lumi_recent"
        if extra_session_dir:
            extra = _load_extra_session_turns(extra_session_dir, history_limit)
            extra_turns_n = len(extra)
            if extra:
                turns = _merge_turns_by_time(turns, extra, history_limit)
                if history_source == "none":
                    history_source = "extra_session_only"
                else:
                    history_source = f"{history_source}+extra_session"

    logger.info(
        "Brain context loaded — identity=%r (%d) user=%d memory=%d soul=%d skills=%d turns=%d "
        "(extra_session=%d) history_source=%s session_key=%s",
        identity_name or "(no IDENTITY.md)", len(identity),
        len(user_profile), len(memory), len(soul),
        skills_catalog.count("\n") + 1 if skills_catalog else 0,
        len(turns), extra_turns_n,
        history_source, session_key,
    )
    return BrainContext(
        identity=identity,
        identity_name=identity_name,
        user_profile=user_profile,
        memory=memory,
        soul=soul,
        skills_catalog=skills_catalog,
        recent_turns=turns,
        workspace_dir=workspace_dir,
    )
