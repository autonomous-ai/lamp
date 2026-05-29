# Voice Brain Review Notes — `feat/voice-brain-gemini-live`

Date: 2026-05-22 UTC
Repo: `ai-lamp-openclaw`
Branch: `origin/feat/voice-brain-gemini-live`

## TL;DR

Branch hiện tại làm được realtime “front-door” cho voice, nhưng chitchat path chưa production-ready vì đang tạo một personality brain riêng mỏng context. Nếu để Gemini Live / GPT Realtime tự trả lời chitchat như hiện tại, Lumi dễ thành generic voice assistant thay vì agentic companion.

Product direction nên là:

- Realtime brain chỉ làm audio/latency/classification layer.
- Mọi output phát ra loa Lumi phải đi qua `TTSService` / Autonomous proxy TTS.
- Brain chitchat chỉ được tự trả lời khi có đủ identity/persona/memory context cần thiết.
- Nếu thiếu context hoặc câu hỏi liên quan identity/memory/state/knowledge thì delegate về main OpenClaw agent.

## What the branch currently adds

### New brain package

`lelamp/service/brain/*`

- `base.py` — shared `Brain`, `BrainSession` interfaces.
- `factory.py` — provider registry via `LELAMP_BRAIN_PROVIDER`.
- `gemini_live.py` — Gemini Live implementation.
- `openai_realtime.py` — OpenAI Realtime implementation.
- `prompts.py` — shared decision prompt and `delegate_to_lumi` tool description.
- `context_loader.py` — loads `SOUL.md` and recent OpenClaw session history.
- `audio_sink.py` — plays realtime PCM output via `aplay`, fallback `sounddevice`.

### VoiceService integration

When `LELAMP_BRAIN_PROVIDER` is enabled, `VoiceService` bypasses classic STT and streams mic frames to realtime provider.

Callbacks:

- `on_delegate(transcript)` → `_send_to_lumi(transcript, "voice")`
- `on_audio_chunk(pcm)` → direct speaker playback in native mode
- `on_text(text, is_final)` → accumulates reply text and can send it to `TTSService.speak_queue()` in fallback mode

### Intended routing

```
mic → realtime brain
  → chitchat: brain replies
  → task/action/lookup: delegate_to_lumi → OpenClaw main agent
```

## Main issues found

## 1. TTS path is wrong for product behavior

Current runtime default in `VoiceService` is effectively:

```env
LELAMP_BRAIN_TTS=native
```

Native mode means:

- Gemini/OpenAI realtime generates voice audio directly.
- PCM chunks are played through `PCMAudioSink` / `aplay`.
- It does not go through Autonomous proxy TTS.
- Voice can differ from normal Lumi task replies.

Product requirement discussed:

- All spoken output from Lumi should go through `TTSService` / Autonomous proxy TTS.
- Chitchat should not use Gemini/OpenAI realtime provider voice.

Required change:

- Remove or ignore `native` in Lumi runtime path.
- Always route chitchat text through `TTSService.speak_queue()`.
- Keep native only for standalone demo/dev benchmark if needed.
- Update docs/env to not present native as deploy option.

Correct chitchat flow:

```
user: "hello noah"
Gemini/GPT realtime: produce text only / text transcript of reply
VoiceService: TTSService.speak_queue(reply)
TTSService: Autonomous proxy TTS
speaker: proxy voice
```

## 2. Identity is incomplete / wrong

Current prompt hardcodes:

```text
You are Lumi — a smart, warm voice assistant living in a lamp.
```

This treats “Lumi” as the assistant name, but product model is different:

- `Lumi` = species/product identity.
- Given name = name owner assigned, e.g. `Noah`.

Main runtime already has identity logic:

- `_read_agent_name()` reads `IDENTITY.md`.
- Wake words are generated from that name.

But brain context loader does not read `IDENTITY.md`.

Consequence:

- User says: “hello Noah”
- Brain may guess Noah is itself, but it does not know from system context.
- Worse, prompt says it is Lumi, so it may answer “I’m Lumi” instead of “I’m Noah.”

Required change:

Inject explicit identity into brain system prompt:

```text
Your given name is Noah.
Your product/species is Lumi.
When the user says “Noah”, they are addressing you.
Do not say your name is Lumi unless asked what kind of device/species you are.
```

Identity source should be same as main agent/wake word source:

- `IDENTITY.md`, or
- owner setup config if that is canonical.

## 3. User/owner profile is missing

Brain currently does not load:

- `USER.md`
- owner profile
- user folder metadata
- face/speaker identity context

Brain mode also bypasses classic speaker recognition / SER / wake-word logic, so it often cannot know who is speaking unless recent history happens to mention it.

Consequence:

Casual questions like:

- “tôi là ai?”
- “anh thích gì?”
- “mình nói chuyện hôm qua về gì?”

may be answered from weak context or hallucinated.

Required change:

Add a compact owner/user context block, for example:

```text
=== OWNER / USER CONTEXT ===
Primary owner: <name>
Preferred language: <language>
Known relationship/context: <short curated summary>
```

Do not dump sensitive raw files blindly. Use curated/safe summary where possible.

## 4. Long-term memory is missing

Brain currently loads:

- `SOUL.md`
- last ~20 session turns

Brain does not load:

- `MEMORY.md`
- memory summaries
- habit summaries
- wellbeing summaries
- knowledge/personality facts beyond SOUL.md and recent history

Consequence:

Chitchat feels generic. It cannot reliably answer questions that depend on continuity.

Required change:

Inject a compact memory summary into brain context, e.g.:

```text
=== LONG-TERM MEMORY SUMMARY ===
<curated short facts relevant to casual conversation>
```

Avoid full raw memory if too large or sensitive. Prefer a compact generated/cached `brain_context.md` or equivalent.

## 5. Wellbeing / sensing / smooth context is stripped

`context_loader._clean_openclaw_text()` strips patterns like:

- `[wellbeing_context: ...]`
- `[sensing: ...]`
- `[emotion_context: ...]`
- `[presence_context: ...]`

This is good for removing noisy operator markup, but it means the realtime brain does not see state that helps smooth companion behavior.

Required change:

Do not rely on raw bracket tags in recent history. Instead inject a clean state summary:

```text
=== CURRENT COMPANION STATE ===
Presence: owner nearby / unknown / away
Mood/wellbeing hints: <short safe summary>
Recent activity: <short summary>
Do not mention this unless natural.
```

## 6. Routing prompt is too permissive

Current prompt says:

```text
When unsure → CHIT-CHAT.
```

Given the brain has thin context, this is dangerous. It encourages the realtime provider to answer questions it does not really have enough context for.

Required change:

New routing policy should be closer to:

```text
Only answer directly when it is clearly harmless smalltalk and you have enough identity/context.
If the user asks about actions, tools, external facts, schedules, memory, owner profile, wellbeing, preferences, or anything you are unsure about, delegate_to_lumi.
Never invent identity, memory, preferences, or state.
```

## 7. Chitchat test cases should be added before merge

Minimum tests / manual QA:

1. Given name

- Setup: `IDENTITY.md` name = Noah
- User: “bạn tên gì?”
- Expected: says given name Noah; may mention it is a Lumi lamp if relevant.
- Should not answer generic “mình là Lumi.”

2. Addressing by given name

- User: “hello Noah”
- Expected: recognizes Noah as itself.

3. Product/species

- User: “bạn là gì?”
- Expected: explains it is a Lumi lamp / companion, not confuses given name.

4. Owner identity

- User: “tôi là ai?”
- Expected: answer only if owner/user context is injected; otherwise delegate or say it is not sure.

5. Recent history

- User: “nãy giờ mình nói gì?”
- Expected: uses recent session history accurately, no hallucination.

6. Long-term memory

- User: “tôi thường thích gì?”
- Expected: uses memory summary if present; otherwise delegate / say not sure.

7. TTS path

- User: “hello Noah”
- Expected: audio output goes through `TTSService` / Autonomous proxy TTS, not Gemini/OpenAI realtime voice.

8. Unsafe uncertainty

- User asks ambiguous state/current/knowledge question.
- Expected: delegate to Lumi, not free-answer with thin context.

## Recommended architecture

Better design:

```
OpenClaw/main agent owns identity, memory, personality, wellbeing, and knowledge.
Realtime brain owns low-latency audio turn handling and narrow smalltalk only.
```

Before each realtime session, build a compact `BrainContext` from the same canonical sources as main agent:

- Identity / given name
- Species/product name
- Persona summary
- Owner/user summary
- Long-term memory summary
- Recent conversation summary
- Current wellbeing/presence/habit summary if available
- Strict routing/delegation rules

Then realtime provider can answer only within that injected context.

## Suggested code-level changes

### `context_loader.py`

Extend `BrainContext` fields:

```python
@dataclass
class BrainContext:
    identity: str = ""
    owner_profile: str = ""
    memory_summary: str = ""
    companion_state: str = ""
    soul: str = ""
    recent_turns: list[Turn] = field(default_factory=list)
```

Add readers:

- `_read_identity(workspace_dir)` from `IDENTITY.md`
- `_read_user_profile(workspace_dir)` from `USER.md` or safe summary
- `_read_memory_summary(workspace_dir)` from curated memory or generated summary
- `_read_companion_state(...)` from wellbeing/habit/presence summary source

Render blocks in `to_system_prompt_block()` in this order:

1. Identity
2. Owner/user
3. Memory summary
4. Companion state
5. Persona
6. Recent conversation

### `prompts.py`

Remove hardcoded “You are Lumi” as given name.

Use wording like:

```text
You are a Lumi lamp companion. Your exact given name is provided in the IDENTITY block below.
Never invent your name. If no given name is provided, say you are not fully set up yet.
```

Change routing from “When unsure → CHIT-CHAT” to “When unsure about facts/context/state → delegate.”

### `voice_service.py`

For Lumi runtime:

- Remove native mode from production path.
- Do not initialize/use `PCMAudioSink` for normal chitchat playback.
- Always send final brain text to `TTSService.speak_queue()`.
- If no TTSService is available, fail closed to classic STT or delegate; do not force native.

### Docs

Update docs to state:

- Native realtime voice is demo/dev only.
- Production Lumi voice always goes through Autonomous proxy TTS.
- Realtime brain is not a second agent; it is a latency/classification layer with compact context.

## Final review verdict

Do not merge as production behavior yet.

Branch is useful as a prototype for realtime voice routing, but chitchat currently lacks the canonical identity/memory/context that makes Lumi feel like the same agent. Without these fixes, it behaves like a generic voice assistant with a different voice path.
