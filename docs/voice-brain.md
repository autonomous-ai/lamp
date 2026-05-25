# Voice Brain (half-cascade text router)

Optional routing layer placed **after** the existing STT pipeline and
**before** OpenClaw. STT (Deepgram nova-3) still does the audio→text
work; the brain decides per-utterance whether to:

```
mic ─► VAD ─► STT (Deepgram) ─► final transcript ─┬─► brain.decide ─┬─► chit-chat reply ─► TTSService ─► speaker
                                                                     └─► delegate ────────► OpenClaw (/api/sensing/event)
```

**Why:** OpenClaw pays a token + latency cost on every utterance, even
for "hello" or "cảm ơn". The brain short-circuits casual chit-chat with
one cheap chat-completion call and only escalates real tasks (device
control, reminders, lookups, music, anything needing memory / sensing /
skills) to OpenClaw.

The brain is **opt-in**. With `LELAMP_BRAIN_PROVIDER=none` (default, or
unset) the STT → OpenClaw path runs unchanged.

---

## 1. Providers

| Provider | Env value | API | Default model |
| --- | --- | --- | --- |
| Google Gemini | `gemini` | `generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | `gemini-2.5-flash` |
| OpenAI        | `openai` | `api.openai.com/v1/chat/completions` | `gpt-4o-mini` |

Both providers are reached via **raw HTTP** (`requests`). There is no
vendor SDK dependency — the message array is built explicitly so we own
the wire shape, the history merge, and the prompt cache prefix.

`LELAMP_BRAIN_PROVIDER` accepts:

| Value | Meaning |
| --- | --- |
| unset / `none` / `off` / `classic` / `disabled` | brain disabled, STT transcript goes straight to OpenClaw |
| `gemini` | route through Gemini chat completion |
| `openai` | route through OpenAI chat completion |
| anything else | logged as unknown, brain disabled |

---

## 2. How it decides

Both providers see the same prompt (`DECISION_RULES` in
`lelamp/service/brain/prompts.py`) and the same `delegate_to_lumi`
function declaration. The model picks exactly one of:

| Branch | Trigger | What lelamp does |
| --- | --- | --- |
| **(A) Chit-chat** | Model returns plain text | The text is passed to `TTSService.speak_queue(...)` — same ElevenLabs/OpenAI voice as task replies. Nothing is sent to OpenClaw. |
| **(B) Delegate** | Model calls `delegate_to_lumi(transcript=…)` | The transcript is forwarded to Lumi via `POST /api/sensing/event` exactly the way the classic STT path forwards it. OpenClaw runs its normal turn. |
| **(error)** | HTTP error / parse error / empty response | Falls through to Lumi as a delegate. Safe default — user input is never silently dropped. |

`DECISION_RULES` is intentionally short and English-only (replies still
come back in the user's language via the language hint — see §4). Long
prompts inflate first-token latency and make the prompt cache window
harder to hit.

---

## 3. Enabling brain mode

```bash
# Shared
export LELAMP_BRAIN_PROVIDER=gemini       # or openai, or none

# Gemini
export GEMINI_API_KEY=...                 # or GOOGLE_API_KEY
export LELAMP_GEMINI_TEXT_MODEL=gemini-2.5-flash

# OpenAI
export OPENAI_API_KEY=...
export LELAMP_OPENAI_TEXT_MODEL=gpt-4o-mini

# Shared HTTP knob
export LELAMP_BRAIN_HTTP_TIMEOUT=15       # seconds, default 15

# Session memory persistence (chit-chat history)
export LELAMP_BRAIN_SESSION_LOG=/root/local/brain/session.jsonl  # or /dev/null to disable
export LELAMP_BRAIN_SESSION_HISTORY_MAX=10                       # max chit-chat turns kept in-memory (default 10)

# Context the static system prompt loads (shared with OpenClaw)
export OPENCLAW_WORKSPACE=/root/.openclaw/workspace      # for IDENTITY.md / USER.md / MEMORY.md / KNOWLEDGE.md / SOUL.md
export OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main   # for sessions/sessions.json (history)
export OPENCLAW_SESSION_KEY=agent:main:main              # which session's history to mirror
export LUMI_BASE_URL=http://127.0.0.1:5000               # fallback history source when JSONL is absent

# Static-block size caps (tail-trim if exceeded)
export LELAMP_BRAIN_USER_MD_MAX=3000
export LELAMP_BRAIN_MEMORY_MD_MAX=5000
export LELAMP_BRAIN_KNOWLEDGE_MD_MAX=2000
export LELAMP_BRAIN_MEMORY_FILES_KEEP=3
```

`VoiceService.__init__` reads `LELAMP_BRAIN_PROVIDER` once at startup.
On any failure (missing key, unknown provider) it logs a warning and
leaves the classic STT path intact — production stays safe.

No extra Python deps required — `requests` is already a project dep.

---

## 4. Package layout

```
lelamp/service/brain/
  __init__.py        — public exports (TextBrain, build_text_brain_from_env, load_context, …)
  prompts.py         — DECISION_RULES, DELEGATE_TOOL_*, language_hint(), resolve_stt_language()
  context_loader.py  — reads IDENTITY / USER / MEMORY / KNOWLEDGE / SOUL + OpenClaw session JSONL
  text_router.py     — TextBrain class with `gemini` and `openai` HTTP backends
lelamp/test/test_brain.py — context loader unit tests
```

### Context the brain receives

The **static** system prompt — built once at startup, cached for the
process lifetime so every request reuses the same byte-stable prefix
(prompt cache hits from call #2 onward):

1. `DECISION_RULES` — routing rules (English, short).
2. Language hint — e.g. `LANGUAGE: The user is speaking Vietnamese. …`
   when `stt_language` is set in lumi config.
3. **IDENTITY** — full `IDENTITY.md` (given name + species + traits).
4. **OWNER / USER PROFILE** — full `USER.md` (name, preferences, timezone).
5. **LONG-TERM MEMORY** — `workspace/memory/*.md` (newest `MEMORY_FILES_KEEP`
   files concatenated, tail-trimmed) or fallback `MEMORY.md`.
6. **KNOWLEDGE** — `KNOWLEDGE.md` (mistakes the agent learned not to repeat).
7. **PERSONA** — `SOUL.md` (narrative tone).

Missing files are skipped silently; the brain still boots without them.

### History the brain receives (per call)

Built fresh on every `decide()` call — passed as the `contents` /
`messages` array, *not* in the system prompt, so the cached prefix
stays byte-stable.

`_merge_history()` interleaves two sources and sorts them by timestamp:

- **OpenClaw session JSONL** (delegate flow) — read straight from
  `$OPENCLAW_AGENTS_DIR/sessions/<sessionFile>` (resolved via
  `sessions.json` indexed by `OPENCLAW_SESSION_KEY`). Same data the
  `chat.history` WS RPC would return. Hardware commands, sensing tags,
  context blobs, date headers, `NO_REPLY`, heartbeat tokens etc. are
  stripped — the brain only sees conversational text.
- **In-process chit-chat log** (chit-chat the brain handled itself,
  which never reaches OpenClaw) — kept in `_session_history` and
  persisted as JSONL at `LELAMP_BRAIN_SESSION_LOG` so a service restart
  reloads recent turns instead of forgetting them.

Both sources stamp each entry with a Unix epoch `ts`; `merged.sort(key=...)`
produces one chronological timeline. Capped at
`2 * LELAMP_BRAIN_SESSION_HISTORY_MAX` total entries (newest tail) so a
long OpenClaw log can't blow up the prompt.

---

## 5. Integration with VoiceService

`VoiceService._stream_session` runs the classic STT pipeline. When STT
finalises a transcript (`final_text`) and the event type is `voice`
(plain speech — wake-word events still route their own way):

```python
decision = self._text_brain.decide(final_text, speaker=user)
if decision.decision == "chitchat" and decision.reply:
    self._tts.speak_queue(decision.reply)            # branch A
elif decision.decision == "delegate":
    self._send_to_lumi(final_msg, event_type="voice")  # branch B
else:  # decision.decision == "error"
    self._send_to_lumi(final_msg, event_type="voice")  # safe fallback
```

Speaker recognition, SER, and the wake-word router run unchanged before
the brain — the brain only sees the final transcript, never the raw
audio.

---

## 6. Prompt caching

The static system instruction (everything in §4 above except the
per-call history) is built once in `TextBrain.__init__` and cached for
the lifetime of the process. Re-reading `IDENTITY.md` / `USER.md` /
`MEMORY.md` / etc. after an edit requires a service restart — fine for
the kind of files involved.

Why this matters: with a byte-stable prefix, both providers' prompt
caches kick in from call #2:

- **OpenAI**: automatic for any prefix ≥ 1024 tokens; cached input
  bills at ~50 % of normal rate.
- **Gemini**: implicit cache on the same prefix, comparable discount.

If the cached prefix changes (e.g. you edit `MEMORY.md` and restart)
the next call pays full price; subsequent calls cache again.

---

## 7. Known limitations / follow-ups

- **No streaming reply** — `chat.completions` returns the full reply in
  one HTTP response, so chit-chat replies arrive in one chunk. For
  typical 1–2 sentence replies the perceived latency is dominated by
  the model's generation time, not the round-trip; streaming would
  shave a fraction of a second but doubles the parsing complexity.
- **History reads are read-only for OpenClaw** — the brain reads
  OpenClaw's JSONL but never writes back. Chit-chat replies live in
  the brain's own JSONL (`LELAMP_BRAIN_SESSION_LOG`); they don't show
  up in the OpenClaw chat history or the Flow Monitor. Cross-process
  visibility is the trade-off.
- **No barge-in** — while TTSService is speaking the chit-chat reply
  the mic gate is active; the user can't interrupt mid-reply.
- **Static prompt is process-lifetime** — edits to IDENTITY / USER /
  MEMORY / KNOWLEDGE / SOUL require a lelamp restart to take effect in
  the brain. The OpenClaw delegate path picks up changes immediately —
  only the chit-chat fast path holds the snapshot.
