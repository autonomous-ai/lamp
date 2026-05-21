# Voice Brain (Gemini Live)

Optional routing layer placed **in front of** the existing STT → OpenClaw
pipeline. Replaces the always-go-through-OpenClaw flow with:

```
mic ─► VAD ─► Gemini Live ─┬─► tool_call delegate_to_lumi ─► OpenClaw (classic flow)
                            └─► native audio out ──────────► speaker (chit-chat)
```

**Why:** OpenClaw pays a token + latency cost on every utterance, even for
"hello" or "thanks". The brain short-circuits casual chit-chat with a single
Gemini Live call that streams a spoken reply directly. Only requests that
need a tool, action, or long answer escalate to OpenClaw.

The brain is **opt-in**. With `LELAMP_BRAIN=classic` (default) lelamp is
byte-for-byte identical to the pre-brain build.

---

## 1. How it decides

Gemini Live sees the system prompt (`DECISION_RULES` in
`lelamp/service/brain/gemini_live.py`) plus the user's audio and decides
per-turn:

| Branch | Trigger | What lelamp does |
| --- | --- | --- |
| **(A) Chit-chat** | Model speaks audio | `PCMAudioSink` plays Gemini's 24 kHz PCM directly out the speaker. Nothing is sent to OpenClaw. |
| **(B) Task** | Model calls `delegate_to_lumi(transcript=…)` | The transcript is forwarded to Lumi exactly the way an STT final transcript would be: `POST /api/sensing/event`. OpenClaw then runs its normal turn. |

If the model is unsure the system prompt biases it toward calling
`delegate_to_lumi` — false-negatives (chit-chat that escalates) are cheaper
than false-positives (task answered by Gemini without skills).

---

## 2. Enabling brain mode

```bash
export LELAMP_BRAIN=gemini_live
export GEMINI_API_KEY=...                # required
export LELAMP_BRAIN_TTS=native           # native | fallback (default native)

# Optional overrides:
export LELAMP_GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
export LELAMP_GEMINI_LIVE_VOICE=Aoede
export LELAMP_GEMINI_LIVE_LANGUAGE=vi-VN
export OPENCLAW_WORKSPACE=/root/.openclaw/workspace   # for SOUL.md
export OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main # for sessions/sessions.json
export OPENCLAW_SESSION_KEY=agent:main:main            # which session's history to mirror
export LUMI_BASE_URL=http://127.0.0.1:5000             # fallback history source
```

When the brain is enabled, **the classic STT pipeline is bypassed entirely**.
Every mic frame goes straight to Gemini Live, which owns VAD, turn
detection, classification, and reply. No fallback to per-utterance STT
sessions — that path was tried and dropped because the silence-timeout
kept cutting Gemini's reply off mid-stream.

Speaker recognition, SER, and wake-word filtering are **not run** in
brain mode (they need per-session audio buffers that this loop doesn't
keep). If you need them, disable the brain (`LELAMP_BRAIN=classic`).

### `LELAMP_BRAIN_TTS`

| Value | What happens |
| --- | --- |
| `native` (default) | Brain requests `response_modalities=[AUDIO]`. Gemini Live streams 24 kHz PCM back; PCMAudioSink plays it directly via aplay (or sounddevice on Mac). Lowest latency, but the chit-chat voice differs from task replies. |
| `fallback` | Brain requests `response_modalities=[AUDIO]` plus `output_audio_transcription`. The audio chunks are dropped; the transcribed text accumulates until `turn_complete` and is handed to `TTSService.speak_queue` — same ElevenLabs/OpenAI voice as task replies. Costs the Gemini audio synth you discard + the TTS synth you actually play, but keeps a single consistent voice. |

`VoiceService.__init__` reads `LELAMP_BRAIN` once at startup. On any
failure (no key, no SDK, sink can't open) it logs a warning and falls back
to the classic STT path — production stays safe.

Install the SDK:

```bash
pip install google-genai
# or, with uv (used in lelamp):
uv pip install google-genai
```

---

## 3. Package layout

```
lelamp/service/brain/
  __init__.py        — public exports (Brain, BrainSession, BrainContext, …)
  base.py            — Brain / BrainSession abstract interfaces
  context_loader.py  — reads SOUL.md + OpenClaw session JSONL (chat.history mirror)
  audio_sink.py      — PCMAudioSink — aplay subprocess primary, sounddevice fallback
  gemini_live.py     — GeminiLiveBrain / GeminiLiveSession (async↔sync bridge)
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
  `[OpenClaw heartbeat poll]` / `HEARTBEAT_OK` plumbing, and take the
  last `history_limit` turns in chronological order.
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

Any source that's unreachable is silently skipped — the brain still boots,
it just answers without that piece of context.

---

## 4. Standalone demo (Mac)

```bash
cd lelamp
export GEMINI_API_KEY=...
python -m lelamp.brain_demo
```

Opens the default mic and speaker, prints `[lumi] …` text deltas (when
present) and `>>> [TASK → would POST to Lumi] '…'` lines whenever the brain
escalates a turn. No Lumi server or OpenClaw is required — useful for
trying voices / phrasing without redeploying to the device.

When `OPENCLAW_WORKSPACE` is unset the demo uses a built-in dev persona so
the brain still sounds like Lumi on a clean machine.

---

## 5. Integration with VoiceService

`VoiceService.__init__` reads `LELAMP_BRAIN`. If it is `gemini_live` and
the SDK + key are healthy, it builds a `GeminiLiveBrain` + `PCMAudioSink`
and stores them on `self._brain` / `self._brain_sink`. `_loop()` then
forks to `_continuous_brain_loop()` instead of the classic VAD loop.

`_continuous_brain_loop` keeps a single mic open and a single brain
session open for as long as the service runs. Each mic frame is sent
straight to Gemini Live. Three callbacks are wired:

- `on_delegate(transcript)` → `_send_to_lumi(transcript, "voice")`
- `on_audio_chunk(pcm)`     → `_brain_sink.push(pcm)` (native mode only)
- `on_text(text, is_final)` → buffer, then `TTSService.speak_queue(text)`
  on turn complete (fallback mode only)

When the brain session ends (delegate fires, Gemini disconnects, idle
timeout, …) the loop opens a fresh one and continues.

`_tts_is_speaking()` is extended to also return `True` while the brain
sink is draining, so the existing TTS-echo mic gate covers brain audio
too. A second RMS-based reverb gate keeps the mic muted until measured
room noise drops below `LELAMP_ECHO_RMS_FLOOR`.

---

## 6. Audio formats

| Direction | Format |
| --- | --- |
| Mic → brain | PCM int16 LE mono, 16 kHz (re-uses VoiceService's resampled output) |
| Brain → speaker | PCM int16 LE mono, 24 kHz (Gemini Live native) |

`PCMAudioSink` opens a single backend per session (aplay subprocess
preferred, sounddevice fallback). On the Pi where PortAudio holds the
seeed/wm8960 card exclusively for TTSService, set
`LELAMP_BRAIN_OUTPUT_ALSA` to a dmix/plug device so aplay can share the
output.

---

## 7. Known limitations / follow-ups

- **Voice mismatch (native mode)** — chit-chat uses a Gemini voice
  (`Aoede` default), task replies come back through ElevenLabs/OpenAI,
  so the voice changes by branch. Mitigation: set
  `LELAMP_BRAIN_TTS=fallback` to keep one consistent voice (trade-off:
  higher latency on the chit-chat reply).
- **No interruption mid-reply** — while Gemini is speaking the mic gate
  is active, so a user can't barge in. Lifting the gate would require
  echo cancellation aware of the brain sink's playback buffer.
- **No speaker recognition / SER / wake-word in brain mode** — those
  pipelines need per-utterance audio buffers that this single-session
  loop doesn't keep. To recover them, either fall back to classic STT
  (`LELAMP_BRAIN=classic`) or wire a parallel per-turn buffer into
  `_continuous_brain_loop` keyed off Gemini Live's `turn_complete` /
  `interrupted` events.
- **History is read-only** — the brain reads recent turns from the
  OpenClaw session JSONL but never writes back. Chit-chat replies
  don't appear in the Flow Monitor. Adding an explicit POST when the
  brain handles a turn would close the gap.
- **No memory between sessions** — stateless by design for MVP. Add a
  short-term turn buffer in `GeminiLiveBrain` if conversational continuity
  becomes important.
