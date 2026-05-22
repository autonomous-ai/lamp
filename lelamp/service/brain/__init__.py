"""
Brain — voice-in gateway that decides between chit-chat (reply directly)
and task (delegate to OpenClaw via the existing /api/sensing/event flow).

See docs/voice-brain.md for the architecture.

Provider selection is data-driven via :mod:`lelamp.service.brain.factory`
— VoiceService reads ``LELAMP_BRAIN_PROVIDER`` and calls ``make_brain``.
No provider implementation (Gemini, OpenAI, …) is imported here so the
package stays cheap to import and machines without one of the vendor
SDKs still boot — the missing one only fails when explicitly selected.
"""

from lelamp.service.brain.audio_sink import PCMAudioSink
from lelamp.service.brain.base import Brain, BrainSession
from lelamp.service.brain.context_loader import BrainContext, Turn, load_context
from lelamp.service.brain.factory import (
    available_providers,
    is_disabled,
    make_brain,
    normalize,
)

__all__ = [
    "Brain",
    "BrainSession",
    "BrainContext",
    "Turn",
    "PCMAudioSink",
    "load_context",
    "make_brain",
    "available_providers",
    "is_disabled",
    "normalize",
]
