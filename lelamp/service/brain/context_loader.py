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
    soul: str = ""                   # full SOUL.md (persona narrative)
    recent_turns: List[Turn] = field(default_factory=list)
    workspace_dir: str = ""

    def to_system_prompt_block(self) -> str:
        """Render context as a single block suitable for prepending to the
        brain's system instruction. Empty sections are skipped silently.

        Order is intentional and reflects the review-notes recommendation:
        identity FIRST so the model never invents a name; persona NEXT so
        tone matches; recent turns LAST so they don't shadow the persona.
        """
        parts: list[str] = []
        if self.identity.strip():
            parts.append("=== IDENTITY (IDENTITY.md) ===\n" + self.identity.strip())
        if self.soul.strip():
            parts.append("=== PERSONA (SOUL.md) ===\n" + self.soul.strip())
        if self.recent_turns:
            convo = "\n".join(
                f"{t.role}: {t.text}" for t in self.recent_turns if t.text.strip()
            )
            if convo:
                parts.append("=== RECENT CONVERSATION ===\n" + convo)
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
) -> BrainContext:
    """Load brain context. All sources are best-effort — failures degrade
    gracefully to an empty section.

    History resolution order:
        1. OpenClaw session JSONL (exact mirror of chat.history)
        2. Lumi /api/agent/recent (only used if #1 returned nothing — for
           dev machines without the workspace mounted)
    """
    workspace_dir, agents_dir = _resolve_paths(workspace_dir, agents_dir)
    session_key = session_key or os.environ.get("OPENCLAW_SESSION_KEY") or DEFAULT_SESSION_KEY
    lumi_base_url = lumi_base_url or os.environ.get("LUMI_BASE_URL") or DEFAULT_LUMI_BASE

    identity, identity_name = _read_identity(workspace_dir)
    soul = _read_soul(workspace_dir)

    turns: List[Turn] = []
    history_source = "none"
    if include_history:
        turns = _read_openclaw_history(agents_dir, session_key, history_limit)
        if turns:
            history_source = "openclaw_jsonl"
        else:
            turns = _fetch_recent_turns(lumi_base_url, history_limit, timeout)
            if turns:
                history_source = "lumi_recent"

    logger.info(
        "Brain context loaded — identity=%r (%d chars), soul=%d chars, turns=%d, "
        "history_source=%s, session_key=%s, workspace=%s, agents=%s",
        identity_name or "(no IDENTITY.md)", len(identity),
        len(soul), len(turns), history_source, session_key, workspace_dir, agents_dir,
    )
    return BrainContext(
        identity=identity,
        identity_name=identity_name,
        soul=soul,
        recent_turns=turns,
        workspace_dir=workspace_dir,
    )
