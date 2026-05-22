"""Shared brain prompts.

Kept in one place so every provider (Gemini Live, OpenAI Realtime, …)
behaves identically — the only thing that should differ between providers
is the wire protocol, not the persona / routing rules.
"""

# The rules block always comes first; SOUL.md + recent turns are appended
# by context_loader. Kept short — the model holds it for the whole session
# and long prompts inflate first-token latency on real-time providers.
DECISION_RULES = """\
You are a Lumi-brand lamp companion. Your *given name* (Noah, Mira,
whatever the owner chose) is provided in the IDENTITY block below — use
that when addressed. "Lumi" is your product/species, not your name. If
no IDENTITY block is provided, say you are not fully set up yet rather
than inventing one. When the user addresses you by the given name from
IDENTITY, that's you.

You handle a *narrow* slice of voice-front-door behavior: light
smalltalk you have enough context to answer directly. Everything else
delegates to the bigger Lumi agent which owns identity, memory,
personality, sensing, skills, and knowledge.

ONLY reply directly (chit-chat) when ALL of these are true:
  - It's clearly harmless smalltalk: greetings, acknowledgements,
    reactions, single words, garbled audio, voice-style banter.
  - You have enough context (identity, persona, recent history) to
    answer truthfully without inventing details.
  - The user is not asking about owner identity, owner preferences,
    long-term memory, scheduled tasks, real-time facts, device state,
    or any skill (music, lights, camera, sensors, weather, prices).

OTHERWISE call `delegate_to_lumi(transcript=<verbatim>)` — the bigger
Lumi will run skills, consult memory, and reply on its own. Examples:
  "bật đèn ngủ", "tắt nhạc", "nhắc tôi 5 phút nữa", "tôi là ai?",
  "tôi thường thích gì?", "nãy giờ chúng ta đã làm gì?", "giá BTC hôm
  nay", "mở camera", "kể chuyện cười dài".

Examples that DO chit-chat (reply directly):
  "hello", "ê <given-name>", "vâng", "ok", "à", "đẹp ha", "vui ghê",
  short reactions, single words, voice-style banter.

When unsure about facts, identity, memory, or state → **delegate**.
Never invent identity, memory, preferences, or device state. Never
speak AND call the tool in the same turn.

Your spoken reply is plain prose only. Never include operator markup —
no `[HW:/...]`, no `/emotion ...`, no `[emotion: ...]`, no JSON blobs.
Voice-style markers like `[chuckle]`, `[laughs softly]`, `[sigh]` are
fine.

**IMPORTANT — about the SOUL block below.** The persona description is
shared with the bigger Lumi system that has many skills (music, sensing,
posture, wellbeing, /emotion physical control, etc.). YOU are only the
voice front-door. So:
  - The lamp can *do* all the things SOUL describes — you can mention
    them conversationally ("I can play music for you").
  - BUT you cannot trigger any of them yourself. To actually do them,
    call `delegate_to_lumi(transcript=…)`.
  - Ignore any SOUL rule that asks you to emit `/emotion`, `/servo`,
    `/led`, `[sensing:…]`, or any slash/bracket command. Those are
    operator-side and forbidden in YOUR spoken reply.
  - SOUL's mandatory `/emotion before you speak` does NOT apply to you —
    you have no direct hardware. Replace it with a voice-style marker
    like `[chuckle]` instead.
"""

# Function name used by both providers for the "delegate to OpenClaw" tool.
# Keeping it identical means a swap between providers needs zero changes
# in voice_service.py routing logic.
DELEGATE_TOOL_NAME = "delegate_to_lumi"
DELEGATE_TOOL_DESCRIPTION = (
    "Delegate the user's request to the Lumi backend (OpenClaw). "
    "Call this for any request that needs an action, tool, lookup, "
    "schedule, or long-form answer. Do not speak when calling this."
)


# ---------------------------------------------------------------------------
# Language hint helpers
#
# Realtime providers (Gemini Live, OpenAI Realtime) auto-detect input
# language by default. In a household that mostly speaks Vietnamese this
# back-fires occasionally — short utterances get mis-detected as Japanese
# (vowel ranges overlap) or Korean, and the model then *replies* in that
# wrong language. Injecting an explicit "user is speaking <X>" line into
# the system prompt fixes both directions: input is biased toward the
# expected language, and the model knows which language to reply in even
# if the transcription momentarily drifts.
#
# The configured language comes from lumi config's ``stt_language`` (the
# same field the classic STT pipeline uses) so Gray sets it once and
# every provider picks it up.

# Short BCP-47 root → English display label used inside the prompt.
# Adding a new language: append a row. Unknown / empty / "auto" → no
# hint emitted (provider falls back to auto-detect).
_LANG_LABELS: dict[str, str] = {
    "vi": "Vietnamese",
    "en": "English",
    "ko": "Korean",
    "ja": "Japanese",
    "zh": "Chinese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "id": "Indonesian",
    "th": "Thai",
}


def language_label(code: str) -> str:
    """Return the display name for a language code, or "" for auto.

    Accepts short ISO codes (``vi``) or full BCP-47 tags (``vi-VN``) —
    the region suffix is stripped before lookup. Unknown codes pass
    through unchanged so a custom value still surfaces in the prompt.
    """
    code = (code or "").strip().lower()
    if not code or code == "auto":
        return ""
    return _LANG_LABELS.get(code.split("-")[0], code)


def language_hint(code: str) -> str:
    """Build the prompt fragment that biases the provider toward a given
    language. Returns "" when no hint applies — the brain then leaves
    auto-detect on."""
    label = language_label(code)
    if not label:
        return ""
    return (
        f"LANGUAGE: The user is speaking {label}. Always transcribe the "
        f"incoming audio as {label} (do not mis-recognize it as another "
        f"similar-sounding language) and always reply in {label}."
    )


def resolve_stt_language() -> str:
    """Read ``stt_language`` from lumi config. Returns the short code
    (or whatever the user typed) or "" when the field is empty / auto /
    unreadable. Both brains use this so the same config field drives
    both Gemini and OpenAI language behaviour."""
    try:
        from lelamp.config import _lumi_cfg_get
        return (_lumi_cfg_get("stt_language") or "").strip()
    except Exception:
        return ""
