# brain/live/ — realtime audio brain (Gemini Live / OpenAI Realtime)

Voice front-door alternative to `brain/call/` (half-cascade text router).
Where call mode does **STT → text router → TTS**, live mode streams raw
mic audio straight into a realtime LLM session that handles VAD + STT +
the chit-chat/delegate decision server-side. Reply transcript is routed
through ElevenLabs (same `TTSService.speak_queue` as call mode) so the
user keeps one voice across both paths.

Run mode is picked at startup:

```bash
LELAMP_BRAIN_MODE=live          # call (default) | live
LELAMP_BRAIN_PROVIDER=gemini    # gemini | openai
```

## Why this exists

- The call-mode pipeline waits up to `LELAMP_SILENCE_TIMEOUT_S` (default
  2.5s) of silence before deciding the user finished speaking. Gemini
  Live's server VAD typically detects end-of-turn in 100-500ms — about
  2s saved per turn on the perceived latency budget.
- A/B is the whole point — both modes share `prompts.py`,
  `context_loader.py`, `summarizer.py`, `workspace.py`, so the routing
  policy + memory layout stay identical and only the transport differs.

## Model choice: `gemini-2.5-flash-live-preview`

Default is **`gemini-2.5-flash-live-preview`**, not the newer 3.1. The
reason is one specific API behaviour:

| Model | `send_client_content` mid-session? |
|---|---|
| `gemini-3.1-flash-live-preview` | initial seeding only — after the first turn callers must use `send_realtime_input(text=…)` which the model treats as a user message |
| `gemini-2.5-flash-live-preview` | supported throughout the conversation, including `turn_complete=False` (add context without triggering generation) |

We need to inject history mid-session — see next section — so 2.5 is
the only viable model on the Developer API tier. Swap back to 3.x once
Google ships the same `send_client_content` flexibility there.

Override via env:

```bash
LELAMP_GEMINI_LIVE_MODEL=gemini-2.5-flash-live-preview  # default
```

## History strategy: session-level (with a known stale gap)

A Gemini Live session is long-lived — typically 10-15 minutes before
the server emits `GoAway` and forces a reconnect. Within that window
Gemini "remembers" everything spoken on the wire.

Ideally the brain would re-inject OpenClaw history on every voice
turn so Telegram DMs, web chat, and other-voice sessions that landed
during this session would be visible to the live brain. Two routes
were explored on 2026-05-26 and **both rejected**:

### Rejected: mid-session `send_client_content` injection

`send_client_content(turns=[new_turns], turn_complete=False)` looked
ideal — adds context without triggering a reply. Per the public Live
API docs it works "throughout the conversation" on Gemini 2.5 Flash
Live. On the Developer API the only 2.5 Live model actually exposed
is `gemini-2.5-flash-native-audio-latest`, which behaves like an
audio-out model: silent responses (response=0 tokens) for the first
1-2 turns of every session, opaque transcription. Unusable for the
voice front-door. Gemini 3.x Flash Live restricts
`send_client_content` to **initial seeding only** — mid-session
calls are rejected. So mid-session injection is not viable on either
model.

### Rejected: per-turn session restart

Close the session after every `turn_complete` → outer mic loop
reopens with a fresh `load_context()`. Cost was acceptable (~0.7-1s
connect during the natural silence after Lumi replies). Quality
regression was not: Gemini lost its in-session conversation memory
turn-over-turn, so replies turned into generic "I can do X, Y, Z"
feature-list dumps from the SOUL + SKILLS blocks in the system
prompt. User-facing "is it saying weird things?" — yes.

### Current: load once per session, accept the gap

- `load_context()` runs at session start and bakes
  IDENTITY + USER + MEMORY + KNOWLEDGE + SOUL + SKILLS + the last
  ~20 OpenClaw turns into the system instruction.
- Within the 10-15 minute Gemini Live session, the in-session
  conversation memory carries everything spoken — Gemini follows
  the dialogue naturally, no extra plumbing needed.
- On `GoAway`, the runner opens a fresh session, `load_context()`
  re-reads OpenClaw JSONL, and the brain catches up on anything
  that happened during the old session — including Telegram / web /
  other-voice turns.

**Known gap:** OpenClaw turns that land *during* a live session stay
invisible to the live brain until the next GoAway. For a household
that voice-chats and Telegrams in parallel within the same 10-min
window, brain will not quote them in voice replies. Acceptable for
now — sessions are short, the gap window is small.

The delegate-text path is the one exception: when Gemini emits the
literal `[DELEGATE]` token (the shared DECISION_RULES marker, which
Gemini occasionally outputs instead of calling the function tool),
the runner **does** force-close the session after delegating. Reason:
once `[DELEGATE]` is in Gemini's dialog history it spirals into
`response=0` for every subsequent turn. Force-close + reopen is the
only known recovery.

## File layout

```
brain/live/
  __init__.py         — package marker
  README.md           — this file
  base.py             — Brain / BrainSession ABCs (audio in, text out,
                        delegate callback, error callback)
  factory.py          — make_brain("gemini"|"openai") — lazy import of
                        the SDK so a Pi without google-genai installed
                        still boots in call mode
  gemini_live.py      — GeminiLiveBrain + GeminiLiveSession.
                        Owns the asyncio loop, WS lifecycle, tool-call
                        + transcription handling, session resumption.
  openai_realtime.py  — OpenAIRealtimeBrain + OpenAIRealtimeSession.
                        Same shape as gemini, different wire protocol.
  audio_sink.py       — legacy PCM sink (used when reply was played as
                        provider audio). Kept for reference; the live
                        runner routes everything through ElevenLabs
                        now, so this module is unused at runtime.
  runner.py           — LiveBrainRunner: owns the mic, drives one
                        session at a time, splits reply transcript
                        into sentences, pushes each into
                        TTSService.speak_queue. Also tracks the last
                        OpenClaw turn it has injected and re-syncs on
                        turn_complete.
```

## VAD knobs (Gemini Live native)

Gemini Live exposes a small VAD config — no RMS / energy threshold.
The SDK's own defaults (silence_duration_ms=100, start_sensitivity=HIGH)
are too twitchy: every fragmented pause is a free `response.create`
that bills audio output. We override with tighter in-code defaults
out of the box:

```bash
LELAMP_LIVE_VAD_SILENCE_MS=800           # our default 800 (SDK: 100). Set "sdk" to use SDK default.
LELAMP_LIVE_VAD_START_SENSITIVITY=low    # our default low (SDK: high). Set "sdk" to use SDK default.
LELAMP_LIVE_VAD_END_SENSITIVITY=         # low | high (unset → SDK default)
LELAMP_LIVE_VAD_PREFIX_PADDING_MS=       # int ms (unset → SDK default 20)
```

Unset env → tightened default. Env value = override. Env value = literal
`sdk` → skip the field so the server uses its own default (escape
hatch for A/B testing).

The runner does **not** do any client-side RMS filtering on the audio
sent to Gemini — Gemini's VAD sees the stream and decides for itself.
The local VAD in `voice_service.py` gates audio frames BEFORE they
reach the WS (separate path; see voice_service `_make_live_vad_check`).

## Cost / always-on knobs

The lamp typically stays powered for whole-day use, so the runner has
two gates that keep idle billing bounded:

1. **Local VAD frame gate** (`is_speech_callback` from voice_service).
   Silence frames are dropped before `session.send_audio`, so audio-in
   tokens only accrue while someone is actually talking. No env knob —
   the chain (RMS → WebRTC → Silero) is wired in voice_service.py.

2. **Idle-close session gate.** If the active WS sees no positive VAD
   verdict for `LELAMP_LIVE_IDLE_CLOSE_S` (default 90s) AND TTS is
   quiet AND no reply is being generated, the runner closes the
   session and goes back to "mic open, no session." The next speech
   frame opens a fresh one (~0.7-1s connect, billed to that turn's
   perceived latency). Saves the GoAway/reconnect cycle of full-prompt
   re-injection during quiet hours — ~100-150 reconnects/day → ~one
   per real conversation.

   ```bash
   LELAMP_LIVE_IDLE_CLOSE_S=90              # default 90; 0 = always-on (legacy)
   ```

   Tune: raise to 180-300 if natural conversation pauses are long and
   the 1s wake delay on the next sentence is noticeable; lower to
   30-60 in a noisy environment where the local VAD may already be
   the dominant savings.

3. **Response-firing gate** (`_should_skip_response`). Even when a
   transcript arrives, hallucination / echo / too-short cases skip
   `request_response()` so we don't pay for output audio + text on
   noise. Provider-side VAD also won't auto-commit pure-silence frames
   because the local gate kept them off the wire.

## TTS path

Hard-wired to ElevenLabs via `TTSService.speak_queue`. The previous
`LELAMP_BRAIN_TTS=native|fallback` env was removed; companion
deployments always want one consistent voice across call and live
modes, no toggle needed.

**Gemini Live (3.x Developer API):** the provider still emits audio
chunks regardless of caller intent — there's no text-only response
modality on this tier. The runner drops every chunk and uses
`output_audio_transcription` for the words. Wasted output tokens
billed; accept until the API grows a text modality.

**OpenAI Realtime (GA, gpt-realtime-2 and later):** supports
`output_modalities=["text"]`, which we set by default. The server only
generates text (no audio gen → ~4× cheaper per output token at GA
pricing), and the runner pipes that text into ElevenLabs verbatim via
`response.output_text.*` events. Override with
`LELAMP_LIVE_OPENAI_OUTPUT_MODALITY=audio` if a downstream wants the
raw provider audio (none currently does).

## Known gaps / follow-ups

- **History sync** lives in `runner.py` only — `factory.py` and the
  provider classes don't know about it. If we add a third provider
  later, the sync logic needs to move into the BrainSession ABC or a
  decorator.
- **TTS proxy 400** for voice IDs that the campaign-api ElevenLabs
  proxy doesn't recognise is a separate config bug, not a live-mode
  bug. Same failure in call mode.
- **`audio_sink.py`** is dead code. Delete once we're sure live mode
  never wants to play raw provider audio.
- **OpenAI Realtime** path is restored but not tested in this iteration.
  GA shape might have drifted; touch up before flipping
  `LELAMP_BRAIN_PROVIDER=openai`.
