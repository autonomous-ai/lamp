# Voice Brain (Gemini Live) — dev notes & findings

Captured during the 2026-05-21 dev session on `lumi-9314` (100.102.110.29).
Living notes, not formal docs — the canonical reference is
`docs/voice-brain.md`.

## Brain vs Classic STT — at a glance

| Aspect | Classic STT → OpenClaw | Brain (fallback ElevenLabs) | Brain (native Aoede) |
| --- | --- | --- | --- |
| Chit-chat latency | slow (full pipeline) | faster | fastest |
| Task latency | faster | +1 hop slower | +1 hop slower |
| OpenClaw cost (Claude Opus) | every turn | task only | task only |
| Gemini Live cost | none | every turn | every turn |
| ElevenLabs cost | every reply | every reply | task reply only |
| Voice consistency across paths | ✅ Linh everywhere | ✅ Linh everywhere | ❌ Aoede vs Linh |
| Speaker recognition | ✅ per-utterance buffer | ❌ rolling 30 s buffer, often unknown | ❌ same |
| SER (speech emotion) | ✅ | ❌ | ❌ |
| Wake word filter | ✅ "hey lumi" | ❌ all audio streams | ❌ |
| VAD | client multi-layer (RMS + WebRTC + Silero) | server neural | server neural |
| TV/music vs speech | OK | better (neural) | better |
| Multilingual auto-detect | limited | ✅ | ✅ |
| Cross-session memory | OpenClaw owns | lost on reconnect (no `session_resumption` yet) | lost |
| Pi CPU load | high (Silero + STT WS) | low (just streams audio) | low |
| OpenClaw load | ~100 % | ~50 % (chit-chat skipped) | ~50 % |
| Log filter "who said it" | ✅ `Speaker - <name>:` | ❌ `[unknown]` (no voiceprint on this device) | ❌ |

**When to use which**

- Demo / companion focus on chit-chat → brain (either mode).
- Production with proactive sensing (SER, habit, wake word) → classic STT.
- Lowest latency → brain native.
- Single consistent voice → brain fallback OR classic.

## Issues hit during dev (chronological)

### 1. Audio device conflict on Pi
`PCMAudioSink` originally opened its own `sd.OutputStream` →
`PaErrorCode -9985 Device unavailable` because TTSService already holds
the seeed/wm8960 card. Fixed by switching the sink to:
1. `aplay` subprocess (respects ALSA dmix; works on `lumi-9314`).
2. `sounddevice` fallback (Mac dev box).
Briefly tried multiplexing onto TTSService's `_stream` via a new
`write_int16_pcm()` method; reverted on request to keep `voice/` un-touched.

### 2. Session close after every turn
Initial impression "Gemini server closes sessions" was wrong.
`session.receive()` is a **per-turn** async generator — it ends at
`turn_complete` even though the WebSocket is still open. Fix in
`gemini_live.py`: wrap the `async for response in session.receive()`
in `while not self._closed`. Sessions now survive across many turns
(verified 5 + minutes idle, multiple turns each session).

### 3. Token cost dominated by re-billed prompt
Gemini Live re-evaluates (and re-bills) the full
`system_instruction` + accumulated session turns on every model turn.
Even with the brain doing nothing new, each reply pulls
~ 3 500 tokens of prompt re-eval. Adding cumulative `brain.usage`
logging — grep `brain.usage` in `journalctl -u lumi-lelamp`.

### 4. Bias toward `delegate_to_lumi`
Symptoms: harmless utterances ("yo", "Bạn tên là gì?", "phim",
"de copiar") got delegated instead of chit-chat.
Root causes, in order of impact:

- **OpenClaw history pollution.** The 20 turns from
  `agents/main/sessions/<id>.jsonl` are full of `[HW:/...]`,
  `[sensing:...]`, `[wellbeing_context: {...}]` — Gemini saw the
  conversation as "every input is a task". Fixed by stripping that
  plumbing in `_clean_openclaw_text` (`context_loader.py`).
- **Long prompt overwhelming the model.** Added a verbose
  "FORBIDDEN" section that pushed the prompt past the chit-chat
  default. Trimmed back to a tight 3-paragraph prompt; bias dropped.
- **SOUL.md conflicting rules.** SOUL says "always emit `/emotion`
  before speaking" and lists `[sensing:*]` → skill mappings — the
  brain followed those instructions and leaked `/emotion`, `[HW:]`,
  `/emotion joyful intense=0.7` into its spoken reply. Mitigation:
  added an explicit override paragraph in `DECISION_RULES` telling
  the brain that SOUL describes the *full* Lumi system and that the
  brain itself must stay plain-prose.

### 5. "Nãy giờ tụi mình nói chuyện gì?" → delegated
Gemini interpreted this as a lookup task. Within the same session it
actually had the conversation in context (proven on the next turn).
Added an explicit example to `DECISION_RULES`:
"questions about our conversation → answer from your own memory,
never delegate".

### 6. Speaker recognition returns `unknown`
No voiceprint enrolled on `lumi-9314` for current speakers (Gray, Leo).
The brain reads the 30 s rolling buffer and calls
`_identify_and_decorate`, but the API returns no match. Easy enrol
fix when needed; not a bug in the brain wiring.

### 7. Ambient audio triggering brain turns
Raw mic mode + Gemini server VAD bắt cả TV/người khác nói trong phòng
(`'de copiar'`, `'Yani en derin yulam'`, `'야.'`). Brain mostly
chit-chats them (with the cleaned-up prompt) but it still burns tokens
on noise. Mitigations on the table (none shipped): add a client-side
RMS gate, restore the WebRTC pre-filter, or wire a wake-word check.

### 8. `language_code` was hard-coded
Brain hard-coded `vi-VN`. Now reads `stt_language` from lumi
`config.json` (the same field classic STT uses), via the new
`_resolve_language` helper in `gemini_live.py`. Short codes map to
BCP-47 (`vi → vi-VN`, `ko → ko-KR`, …). `auto`/empty leaves it unset
so Gemini auto-detects. Tested mixing VI / EN / KR replies cleanly.

### 9. `/voice/` blast-radius rule
Gray (boss-mandated): brain feature must keep changes inside
`lelamp/service/brain/`. `lelamp/service/voice/voice_service.py` is the
only `voice/` file modified — the previously added
`TTSService.write_int16_pcm` was reverted.

## Outstanding follow-ups

| Item | Priority | Note |
| --- | --- | --- |
| Enrol Gray / Leo voiceprints on `lumi-9314` | medium | so logs show `[gray]` instead of `[unknown]` |
| Trim SOUL.md down to a core 500-token voice persona for brain | medium | saves ~ 1 500 tokens / turn — biggest single saving |
| `session_resumption` (handle + `GoAway`) | high | brain currently loses Gemini-side context on every reset (~ 10 min) |
| `context_window_compression` | low | for sessions > 15 min |
| Persist brain chit-chat turns somewhere (file or `/api/monitor/event`) | medium | so the next session can load them as history |
| Client-side ambient filter (RMS / wake word) | medium | reduce token spend on TV / people-not-talking-to-Lumi |
| Slash-pattern leak (`/emotion joyful intense:0.7`) still appears occasionally | low | cosmetic; prompt override mostly catches it |
| Verify decision quality with real audio test suite (not just live mic) | low | currently ad-hoc speaks |
