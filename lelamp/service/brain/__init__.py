"""
Brain — half-cascade text router placed between the classic STT pipeline
and OpenClaw. STT produces a final transcript; the brain decides whether
to reply directly (chit-chat) or forward to OpenClaw (delegate) via a
single chat-completion HTTP call.

See docs/voice-brain.md for the architecture.
"""

from lelamp.service.brain.context_loader import BrainContext, Turn, load_context
from lelamp.service.brain.summarizer import RollingSummary
from lelamp.service.brain.text_router import (
    TextBrain,
    TextBrainDecision,
    build_text_brain_from_env,
    is_disabled,
)
from lelamp.service.brain.workspace import BrainWorkspace

__all__ = [
    "BrainContext",
    "BrainWorkspace",
    "RollingSummary",
    "Turn",
    "load_context",
    "TextBrain",
    "TextBrainDecision",
    "build_text_brain_from_env",
    "is_disabled",
]
