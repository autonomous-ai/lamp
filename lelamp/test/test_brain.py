"""
Unit tests for the brain package.

The Gemini Live session bridge needs a network connection to test against
the real API, so we only cover the parts that don't — namely the context
loader (SOUL.md + OpenClaw JSONL parsing + Lumi HTTP fallback).

Run from repo root:
    python -m pytest lelamp/test/test_brain.py -v
"""

import json
import threading

import pytest

from lelamp.service.brain.context_loader import BrainContext, Turn, load_context


# ----- context loader tests ------------------------------------------------


def test_context_loader_handles_missing_soul(tmp_path):
    ctx = load_context(
        workspace_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        lumi_base_url="http://127.0.0.1:1",  # guaranteed-unreachable
        include_history=False,
    )
    assert ctx.soul == ""
    assert ctx.recent_turns == []
    assert ctx.to_system_prompt_block() == ""


def test_context_loader_reads_soul(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Lumi.\n", encoding="utf-8")
    ctx = load_context(
        workspace_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        lumi_base_url="http://127.0.0.1:1",
        include_history=False,
    )
    block = ctx.to_system_prompt_block()
    assert "You are Lumi." in block
    assert "=== PERSONA" in block


def test_context_loader_reads_openclaw_jsonl(tmp_path):
    """Mirror OpenClaw's chat.history by reading the same JSONL it reads."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session_file = sessions / "abc.jsonl"
    (sessions / "sessions.json").write_text(
        json.dumps({"agent:main:main": {"sessionId": "abc", "sessionFile": str(session_file)}}),
        encoding="utf-8",
    )

    def msg(role, text, ts):
        return json.dumps(
            {
                "type": "message",
                "timestamp": ts,
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            }
        )

    session_file.write_text(
        "\n".join(
            [
                msg("user", "chào Lumi", 1),
                msg("assistant", "[laughs softly] chào bạn!", 2),
                msg("user", "[OpenClaw heartbeat poll]", 3),       # filtered
                msg("assistant", "HEARTBEAT_OK", 4),                # filtered
                msg("user", "bật đèn ngủ giúp", 5),
                msg("assistant", "Ok bật rồi.", 6),
                # toolResult role and non-text parts are skipped
                json.dumps({"type": "message", "timestamp": 7,
                            "message": {"role": "toolResult", "content": [{"type": "text", "text": "noop"}]}}),
                json.dumps({"type": "custom_message", "content": "skip me"}),
            ]
        ),
        encoding="utf-8",
    )

    ctx = load_context(
        workspace_dir=str(tmp_path),
        agents_dir=str(tmp_path),
        lumi_base_url="http://127.0.0.1:1",
        history_limit=10,
    )
    texts = [(t.role, t.text) for t in ctx.recent_turns]
    assert texts == [
        ("user", "chào Lumi"),
        ("assistant", "[laughs softly] chào bạn!"),
        ("user", "bật đèn ngủ giúp"),
        ("assistant", "Ok bật rồi."),
    ]


def test_context_loader_falls_back_to_lumi_when_jsonl_absent(tmp_path, monkeypatch):
    """If the OpenClaw workspace isn't visible (Mac dev box) the loader
    should still degrade to the Lumi HTTP endpoint."""
    import lelamp.service.brain.context_loader as cl

    def fake_recent(base, limit, timeout):
        return [Turn(role="user", text="from lumi http"), Turn(role="assistant", text="ok")]

    monkeypatch.setattr(cl, "_fetch_recent_turns", fake_recent)
    ctx = load_context(
        workspace_dir=str(tmp_path),
        agents_dir=str(tmp_path / "missing"),
        lumi_base_url="http://127.0.0.1:5000",
        history_limit=5,
    )
    assert [t.text for t in ctx.recent_turns] == ["from lumi http", "ok"]


def test_context_loader_respects_history_limit(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session_file = sessions / "x.jsonl"
    (sessions / "sessions.json").write_text(
        json.dumps({"agent:main:main": {"sessionId": "x", "sessionFile": str(session_file)}}),
        encoding="utf-8",
    )
    session_file.write_text(
        "\n".join(
            json.dumps(
                {"type": "message", "timestamp": i,
                 "message": {"role": "user" if i % 2 == 0 else "assistant",
                             "content": [{"type": "text", "text": f"turn {i}"}]}}
            )
            for i in range(50)
        ),
        encoding="utf-8",
    )
    ctx = load_context(
        workspace_dir=str(tmp_path), agents_dir=str(tmp_path),
        lumi_base_url="http://127.0.0.1:1", history_limit=5,
    )
    # Last 5 in chronological order
    assert [t.text for t in ctx.recent_turns] == ["turn 45", "turn 46", "turn 47", "turn 48", "turn 49"]


def test_brain_context_renders_turns():
    ctx = BrainContext(
        soul="persona",
        recent_turns=[
            Turn(role="user", text="hello"),
            Turn(role="assistant", text="hi there"),
        ],
    )
    block = ctx.to_system_prompt_block()
    assert "user: hello" in block
    assert "assistant: hi there" in block
