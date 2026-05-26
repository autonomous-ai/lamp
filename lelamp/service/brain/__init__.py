"""
Brain — voice front-door between the classic STT pipeline and OpenClaw.

Two paths share this package:

  - ``brain/call/`` — half-cascade text router. STT produces a final
    transcript, a single chat-completion HTTP call decides chit-chat vs
    delegate, chit-chat replies stream sentence-by-sentence into
    TTSService. Default mode.
  - ``brain/live/`` — realtime audio router (Gemini Live / OpenAI
    Realtime). Raw mic audio streams to the provider, which does
    server-side VAD + STT + the same chit-chat/delegate decision. Kept
    so VAD latency can be A/B'd against the local RMS + Silero pipeline.

Shared modules sit at this level (``context_loader``, ``prompts``,
``summarizer``, ``workspace``) so both paths see the same prompt
prefix, the same memory, and write to the same workspace.

See docs/voice-brain.md for the architecture.
"""

from lelamp.service.brain.context_loader import BrainContext, Turn, load_context
from lelamp.service.brain.summarizer import RollingSummary
from lelamp.service.brain.workspace import BrainWorkspace

# Re-export the call-mode surface at the package root so existing
# callers (voice_service.py) don't need to change their imports.
from lelamp.service.brain.call.text_router import (
    TextBrain,
    TextBrainDecision,
    build_text_brain_from_env,
    is_disabled,
)

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
