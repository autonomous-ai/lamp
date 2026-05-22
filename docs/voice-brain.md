# Voice Brain (realtime providers)

Optional routing layer placed **in front of** the existing STT → OpenClaw
pipeline. Replaces the always-go-through-OpenClaw flow with:

```
mic ─► VAD ─► <provider> ─┬─► tool_call delegate_to_lumi ─► OpenClaw (classic flow)
                           └─► native audio out ──────────► speaker (chit-chat)
```

**Why:** OpenClaw pays a token + latency cost on every utterance, even for
"hello" or "thanks". The brain short-circuits casual chit-chat with a
single realtime API call that streams a spoken reply directly. Only
requests that need a tool, action, or long answer escalate to OpenClaw.

The brain is **opt-in**. With `LELAMP_BRAIN_PROVIDER=none` (default, or
unset) lelamp is byte-for-byte identical to the pre-brain build.

---

## 1. Providers

| Provider | Env value | API | Default model | SDK |
| --- | --- | --- | --- | --- |
| Google Gemini Live | `gemini` | `google.genai` `live.connect` | `gemini-3.1-flash-live-preview` | `google-genai>=0.7.0` |
| OpenAI Realtime    | `openai` | `openai.beta.realtime.connect` | `gpt-realtime` | `openai>=1.40.0` |

Provider keys follow the LiteLLM / LangChain / OpenRouter convention
(vendor name, not product name). To benchmark and retire one later, you
delete that provider's module file and remove its row from
`lelamp/service/brain/factory.py:_PROVIDERS` — no other code changes
are required.

`LELAMP_BRAIN_PROVIDER` accepts:

| Value | Meaning |
| --- | --- |
| unset / `none` / `off` / `classic` / `disabled` | brain disabled, classic STT pipeline |
| `gemini` | Gemini Live realtime |
| `openai` | OpenAI Realtime |
| anything else | logged as unknown, brain disabled (falls back to classic) |

---

## 2. How it decides

The chosen provider sees the **same** system prompt (`DECISION_RULES` in
`lelamp/service/brain/prompts.py`) plus the user's audio and decides per
turn:

| Branch | Trigger | What lelamp does |
| --- | --- | --- |
| **(A) Chit-chat** | Model speaks audio | `PCMAudioSink` plays 24 kHz PCM directly out the speaker. Nothing is sent to OpenClaw. |
| **(B) Task** | Model calls `delegate_to_lumi(transcript=…)` | The transcript is forwarded to Lumi exactly the way an STT final transcript would be: `POST /api/sensing/event`. OpenClaw then runs its normal turn. |

Sharing one prompt across providers keeps decision quality comparable —
the only intentional difference between providers is the wire protocol.

---

## 3. Enabling brain mode

```bash
# Shared
export LELAMP_BRAIN_PROVIDER=gemini       # or openai, or none
export LELAMP_BRAIN_TTS=native            # native | fallback (default native)

# Gemini Live
export GEMINI_API_KEY=...                 # required for provider=gemini
export LELAMP_GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
export LELAMP_GEMINI_LIVE_VOICE=Aoede
export LELAMP_GEMINI_LIVE_LANGUAGE=vi-VN  # empty/auto → auto-detect

# OpenAI Realtime
export OPENAI_API_KEY=...                 # required for provider=openai
export LELAMP_OPENAI_REALTIME_MODEL=gpt-realtime
export LELAMP_OPENAI_REALTIME_VOICE=alloy

# Context (shared by both providers)
export OPENCLAW_WORKSPACE=/root/.openclaw/workspace   # for SOUL.md
export OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main # for sessions/sessions.json
export OPENCLAW_SESSION_KEY=agent:main:main            # which session's history to mirror
export LUMI_BASE_URL=http://127.0.0.1:5000             # fallback history source
```

When the brain is enabled, **the classic STT pipeline is bypassed
entirely**. Every mic frame goes straight to the provider, which owns
VAD, turn detection, classification, and reply. No fallback to
per-utterance STT sessions — that path was tried and dropped because
the silence-timeout kept cutting realtime replies off mid-stream.

Speaker recognition, SER, and wake-word filtering are **not run** in
brain mode (they need per-session audio buffers that this loop doesn't
keep). If you need them, disable the brain
(`LELAMP_BRAIN_PROVIDER=none`).

### `LELAMP_BRAIN_TTS`

| Value | What happens |
| --- | --- |
| `native` (default) | Brain plays the provider's PCM audio directly via aplay (or sounddevice on Mac). Lowest latency, but the chit-chat voice differs from task replies. |
| `fallback` | The provider's audio chunks are dropped; the transcribed reply text accumulates until turn-complete and is handed to `TTSService.speak_queue` — same ElevenLabs/OpenAI voice as task replies. Costs the realtime audio synth you discard + the TTS synth you actually play, but keeps a single consistent voice. |

`VoiceService.__init__` reads `LELAMP_BRAIN_PROVIDER` once at startup.
On any failure (no key, no SDK, sink can't open, unknown provider name)
it logs a warning and falls back to the classic STT path — production
stays safe.

Install the SDKs:

```bash
pip install google-genai "openai[realtime]"
# or, with uv (used in lelamp):
uv pip install google-genai "openai[realtime]"
```

The `[realtime]` extra pulls in the `websockets` dep required by
`client.realtime.connect`. Without it the brain logs
`"You need to install openai[realtime] to use this method"` and falls
back to classic STT.

(Only the SDK for the selected provider is actually required; install
both if you want to A/B test by flipping `LELAMP_BRAIN_PROVIDER`.)

---

## 4. Package layout

```
lelamp/service/brain/
  __init__.py        — public exports + re-exports of factory.make_brain
  base.py            — Brain / BrainSession abstract interfaces
  prompts.py         — DECISION_RULES + DELEGATE_TOOL_* shared by all providers
  factory.py         — provider registry (LELAMP_BRAIN_PROVIDER → Brain class)
  context_loader.py  — reads SOUL.md + OpenClaw session JSONL (chat.history mirror)
  audio_sink.py      — PCMAudioSink — aplay subprocess primary, sounddevice fallback
  gemini_live.py     — GeminiLiveBrain / GeminiLiveSession  (google-genai)
  openai_realtime.py — OpenAIRealtimeBrain / OpenAIRealtimeSession (openai SDK)
lelamp/brain_demo.py — standalone Mac/Linux demo (no Lumi, no VoiceService)
lelamp/test/test_brain.py — context loader unit tests
```

### Context the brain receives

Mirrors what OpenClaw sees, **minus skills** (skills = task = belongs in
branch B anyway):

- `SOUL.md` — persona block from `$OPENCLAW_WORKSPACE/SOUL.md`
- **Session main history** — read straight from OpenClaw's own JSONL,
  the same source the `chat.history` WS RPC reads from. Path is resolved
  via `$OPENCLAW_AGENTS_DIR/sessions/sessions.json` →
  `<sessionFile>` for sessionKey `agent:main:main` (override via
  `OPENCLAW_SESSION_KEY`). We scan back-to-front, keep only `role` ∈
  {`user`, `assistant`} parts with `type == "text"`, drop the
  `[OpenClaw heartbeat poll]` / `HEARTBEAT_OK` plumbing, strip
  `[HW:/...]`, `[sensing:…]`, `[wellbeing_context: {...}]`, date headers
  and similar operator markup, and take the last `history_limit` turns
  in chronological order.
- **Fallback:** if the workspace isn't visible (e.g. running on a Mac
  dev box without Pi files mounted) the loader falls back to
  `GET {LUMI_BASE_URL}/api/agent/recent` — Lumi's monitor log of
  recent flow events. Lower fidelity than the JSONL, but enough to keep
  chit-chat going during development.

Path resolution accepts either shape:
- `OPENCLAW_WORKSPACE=/root/.openclaw/workspace` (where `SOUL.md` lives);
  the loader derives `agents/main` from its parent.
- `OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main` to override
  explicitly if your layout differs.

Any source that's unreachable is silently skipped — the brain still
boots, it just answers without that piece of context.

---

## 5. Standalone demo (Mac)

```bash
cd lelamp
export LELAMP_BRAIN_PROVIDER=gemini   # or openai
export GEMINI_API_KEY=...             # (or OPENAI_API_KEY)
python -m lelamp.brain_demo
```

Opens the default mic and speaker, prints `[lumi] …` text deltas (when
present) and `>>> [TASK → would POST to Lumi] '…'` lines whenever the
brain escalates a turn. No Lumi server or OpenClaw is required — useful
for trying voices / phrasing without redeploying to the device.

When `OPENCLAW_WORKSPACE` is unset the demo uses a built-in dev persona
so the brain still sounds like Lumi on a clean machine.

---

## 6. Integration with VoiceService

`VoiceService.__init__` reads `LELAMP_BRAIN_PROVIDER`. If the value is a
known provider AND the SDK + key are healthy, it asks
`brain.make_brain(provider, …)` for a `Brain` instance and wires a
`PCMAudioSink` next to it on `self._brain` / `self._brain_sink`.
`_loop()` then forks to `_continuous_brain_loop()` instead of the
classic VAD loop.

`_continuous_brain_loop` keeps a single mic open and a single brain
session open for as long as the service runs. Each mic frame is sent
straight to the provider's realtime API. Three callbacks are wired:

- `on_delegate(transcript)` → `_send_to_lumi(transcript, "voice")`
- `on_audio_chunk(pcm)`     → `_brain_sink.push(pcm)` (native mode only)
- `on_text(text, is_final)` → buffer, then `TTSService.speak_queue(text)`
  on turn complete (fallback mode only)

When the brain session ends (delegate fires, the provider disconnects,
idle timeout, …) the loop opens a fresh one and continues.

`_tts_is_speaking()` is extended to also return `True` while the brain
sink is draining, so the existing TTS-echo mic gate covers brain audio
too. A second RMS-based reverb gate keeps the mic muted until measured
room noise drops below `LELAMP_ECHO_RMS_FLOOR`.

---

## 7. Audio formats

| Direction | Format | Notes |
| --- | --- | --- |
| Mic → brain | PCM int16 LE mono, **16 kHz** | VoiceService's resampled output |
| Mic → Gemini Live | 16 kHz, sent as-is | Gemini Live accepts 16 kHz `audio/pcm` |
| Mic → OpenAI Realtime | **24 kHz**, polyphase-resampled in `openai_realtime.py` | OpenAI Realtime expects 24 kHz `pcm16` |
| Brain → speaker | PCM int16 LE mono, **24 kHz** | Both providers stream 24 kHz — matches `PCMAudioSink` default |

`PCMAudioSink` opens a single backend per session (aplay subprocess
preferred, sounddevice fallback). On the Pi where PortAudio holds the
seeed/wm8960 card exclusively for TTSService, set
`LELAMP_BRAIN_OUTPUT_ALSA` to a dmix/plug device so aplay can share the
output.

---

## 8. Adding a new provider

To bring in (say) `anthropic`:

1. Create `lelamp/service/brain/anthropic_realtime.py` implementing
   `Brain` and `BrainSession`. Reuse `prompts.DECISION_RULES`,
   `prompts.DELEGATE_TOOL_NAME`, `prompts.DELEGATE_TOOL_DESCRIPTION`,
   and `context_loader.load_context` so the persona / routing / context
   shape stays identical to the others.
2. Add one row to `lelamp/service/brain/factory.py:_PROVIDERS`:
   ```python
   "anthropic": ("lelamp.service.brain.anthropic_realtime", "AnthropicRealtimeBrain"),
   ```
3. Document the env vars in this file. Nothing else needs to change.

Retiring a provider is the inverse — delete the module file and that
row.

---

## 9. Known limitations / follow-ups

- **Voice mismatch (native mode)** — chit-chat uses a provider voice
  (`Aoede` for Gemini, `alloy` for OpenAI), task replies come back
  through ElevenLabs/OpenAI TTS, so the voice changes by branch.
  Mitigation: set `LELAMP_BRAIN_TTS=fallback` to keep one consistent
  voice (trade-off: higher latency on the chit-chat reply).
- **No interruption mid-reply** — while the brain is speaking the mic
  gate is active, so a user can't barge in. Lifting the gate would
  require echo cancellation aware of the brain sink's playback buffer.
- **No speaker recognition / SER / wake-word in brain mode** — those
  pipelines need per-utterance audio buffers that this single-session
  loop doesn't keep. To recover them, either fall back to classic STT
  (`LELAMP_BRAIN_PROVIDER=none`) or wire a parallel per-turn buffer
  into `_continuous_brain_loop` keyed off the provider's
  `turn_complete` / `response.done` events.
- **History is read-only** — the brain reads recent turns from the
  OpenClaw session JSONL but never writes back. Chit-chat replies
  don't appear in the Flow Monitor. Adding an explicit POST when the
  brain handles a turn would close the gap.
- **No memory between sessions** — stateless by design for MVP. Add
  `session_resumption` (Gemini) or `conversation.item.retrieve` replay
  (OpenAI) if conversational continuity becomes important.
