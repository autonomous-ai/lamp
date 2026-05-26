"""Brain provider factory.

Single source of truth for which providers exist. To add a provider:
add a row to ``_PROVIDERS`` mapping env value → (module, class). To
retire a provider: delete the row + delete the module file. Nothing
else in the codebase needs to change — VoiceService talks to the
abstract :class:`Brain` interface only.

Provider value space (used by ``LELAMP_BRAIN_PROVIDER``):

    none / unset / classic   → brain disabled, classic STT pipeline.
    gemini                   → Google Gemini Live realtime API.
    openai                   → OpenAI Realtime API.

Vendor-name keys follow the LiteLLM / LangChain / OpenRouter convention
so adding a third real-time provider later (e.g. ``anthropic``) doesn't
require renaming anything.
"""

import importlib
import logging
from typing import Optional

from lelamp.service.brain.live.base import Brain

logger = logging.getLogger("lelamp.brain")

# env value → (module_path, class_name).
# Each module is imported lazily so machines that have only one of the
# vendor SDKs installed still boot — the missing one just fails to load
# when selected and falls back to classic STT.
_PROVIDERS: dict[str, tuple[str, str]] = {
    "gemini": ("lelamp.service.brain.gemini_live",     "GeminiLiveBrain"),
    "openai": ("lelamp.service.brain.openai_realtime", "OpenAIRealtimeBrain"),
}

# Values that mean "no brain, use classic STT". Anything outside this set
# and outside ``_PROVIDERS`` is treated as a typo and logged.
_DISABLED_VALUES = frozenset({"", "none", "off", "classic", "disabled"})


def normalize(provider: Optional[str]) -> str:
    """Return the canonical env value: lowercased + stripped, or "" for None."""
    return (provider or "").strip().lower()


def is_disabled(provider: Optional[str]) -> bool:
    """True when this env value means "skip the brain entirely"."""
    return normalize(provider) in _DISABLED_VALUES


def available_providers() -> list[str]:
    """List of provider keys accepted by ``make_brain``. Useful for log
    messages when the user passes an unknown value."""
    return list(_PROVIDERS)


def make_brain(provider: str, **kwargs) -> Optional[Brain]:
    """Instantiate a brain by provider name.

    Returns ``None`` for unknown providers OR when the provider's SDK
    fails to import — the caller is expected to log + fall back.
    """
    key = normalize(provider)
    spec = _PROVIDERS.get(key)
    if spec is None:
        return None
    module_path, class_name = spec
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
    except (ImportError, AttributeError) as e:
        logger.warning("brain provider %r unavailable: %s", provider, e)
        return None
    return cls(**kwargs)
