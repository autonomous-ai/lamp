"""
Brain — voice-in gateway that decides between chit-chat (reply directly)
and task (delegate to OpenClaw via the existing /api/sensing/event flow).

See docs/voice-brain.md for the architecture.
"""

from lelamp.service.brain.audio_sink import PCMAudioSink
from lelamp.service.brain.base import Brain, BrainSession
from lelamp.service.brain.context_loader import BrainContext, Turn, load_context

__all__ = [
    "Brain",
    "BrainSession",
    "BrainContext",
    "Turn",
    "PCMAudioSink",
    "load_context",
]

# GeminiLiveBrain imports google-genai lazily; we don't top-level export it
# so simply importing `lelamp.service.brain` stays cheap and works on
# machines without the SDK installed.
