# Voice Brain (router half-cascade text)

Lớp routing tuỳ chọn, đặt **sau** pipeline STT hiện tại và **trước**
OpenClaw. STT (Deepgram nova-3) vẫn lo phần audio→text; brain quyết
định cho mỗi câu user nói:

```
mic ─► VAD ─► STT (Deepgram) ─► final transcript ─┬─► brain.decide ─┬─► chit-chat reply ─► TTSService ─► loa
                                                                     └─► delegate ────────► OpenClaw (/api/sensing/event)
```

**Vì sao:** OpenClaw tốn token + latency cho mọi câu, kể cả "hello"
hay "cảm ơn". Brain bắt mấy câu chit-chat đơn giản, chỉ escalate sang
OpenClaw mấy thứ thật sự cần (điều khiển thiết bị, nhắc nhở, lookup,
nhạc, bất cứ gì cần memory / sensing / skill).

Brain là **opt-in**. Với `LELAMP_BRAIN_PROVIDER=none` (mặc định, hoặc
không set) thì pipeline STT → OpenClaw chạy nguyên như cũ.

---

## 1. Providers

| Provider | Env value | API | Default model |
| --- | --- | --- | --- |
| Google Gemini | `gemini` | `generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | `gemini-2.5-flash` |
| OpenAI        | `openai` | `api.openai.com/v1/chat/completions` | `gpt-4o-mini` |

Cả 2 provider gọi qua **HTTP thuần** (`requests`). Không dùng vendor
SDK — message array được build tay nên mình kiểm soát hoàn toàn wire
shape, history merge, và prompt cache prefix.

`LELAMP_BRAIN_PROVIDER` chấp nhận:

| Value | Nghĩa |
| --- | --- |
| unset / `none` / `off` / `classic` / `disabled` | tắt brain, transcript STT gửi thẳng OpenClaw |
| `gemini` | route qua Gemini chat completion |
| `openai` | route qua OpenAI chat completion |
| giá trị khác | log unknown, brain tắt |

---

## 2. Cơ chế quyết định

Cả 2 provider dùng chung prompt (`DECISION_RULES` trong
`lelamp/service/brain/prompts.py`) và cùng 1 function declaration
`delegate_to_lumi`. Model chọn đúng 1 trong:

| Nhánh | Trigger | lelamp làm gì |
| --- | --- | --- |
| **(A) Chit-chat** | Model trả về plain text | Text vào `TTSService.speak_queue(...)` — cùng giọng ElevenLabs/OpenAI với task reply. Không gửi gì cho OpenClaw. |
| **(B) Delegate** | Model gọi `delegate_to_lumi(transcript=…)` | Transcript được forward qua `POST /api/sensing/event` y như pipeline STT cổ điển. OpenClaw chạy turn bình thường. |
| **(error)** | HTTP error / parse error / response rỗng | Fall through xuống Lumi như delegate. Safe default — input của user không bao giờ bị silently drop. |

`DECISION_RULES` cố tình giữ ngắn + chỉ viết bằng tiếng Anh (reply vẫn
ra ngôn ngữ user qua language hint — xem §4). Prompt dài làm tăng
first-token latency và khó hit prompt cache window.

---

## 3. Bật brain mode

```bash
# Shared
export LELAMP_BRAIN_PROVIDER=gemini       # hoặc openai, hoặc none

# Gemini
export GEMINI_API_KEY=...                 # hoặc GOOGLE_API_KEY
export LELAMP_GEMINI_TEXT_MODEL=gemini-2.5-flash

# OpenAI
export OPENAI_API_KEY=...
export LELAMP_OPENAI_TEXT_MODEL=gpt-4o-mini

# HTTP timeout (shared)
export LELAMP_BRAIN_HTTP_TIMEOUT=15       # giây, mặc định 15

# Session memory persistence (chit-chat history)
export LELAMP_BRAIN_SESSION_LOG=/root/local/brain/session.jsonl  # hoặc /dev/null để tắt
export LELAMP_BRAIN_SESSION_HISTORY_MAX=10                       # số turn chit-chat tối đa (default 10)

# Context cho static system prompt (chia sẻ với OpenClaw)
export OPENCLAW_WORKSPACE=/root/.openclaw/workspace      # cho IDENTITY/USER/MEMORY/KNOWLEDGE/SOUL .md
export OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main   # cho sessions/sessions.json (history)
export OPENCLAW_SESSION_KEY=agent:main:main              # session nào để mirror
export LUMI_BASE_URL=http://127.0.0.1:5000               # fallback history source khi JSONL không có

# Cap size cho static block (tail-trim nếu vượt)
export LELAMP_BRAIN_USER_MD_MAX=3000
export LELAMP_BRAIN_MEMORY_MD_MAX=5000
export LELAMP_BRAIN_KNOWLEDGE_MD_MAX=2000
export LELAMP_BRAIN_MEMORY_FILES_KEEP=3
```

`VoiceService.__init__` đọc `LELAMP_BRAIN_PROVIDER` 1 lần khi khởi
động. Nếu fail (thiếu key, provider lạ) thì log warning và giữ
nguyên pipeline STT cổ điển — production luôn an toàn.

Không cần cài thêm dep Python nào — `requests` đã có sẵn trong project.

---

## 4. Bố cục package

```
lelamp/service/brain/
  __init__.py        — public exports (TextBrain, build_text_brain_from_env, load_context, …)
  prompts.py         — DECISION_RULES, DELEGATE_TOOL_*, language_hint(), resolve_stt_language()
  context_loader.py  — đọc IDENTITY / USER / MEMORY / KNOWLEDGE / SOUL + OpenClaw session JSONL
  text_router.py     — class TextBrain với 2 backend HTTP `gemini` / `openai`
lelamp/test/test_brain.py — unit test cho context loader
```

### Context brain nhận được

**Static** system prompt — build 1 lần ở startup, cache trong suốt
process lifetime để mọi request dùng lại đúng cùng 1 prefix byte-stable
(prompt cache hit từ call #2 trở đi):

1. `DECISION_RULES` — routing rules (tiếng Anh, ngắn).
2. Language hint — ví dụ `LANGUAGE: The user is speaking Vietnamese. …`
   khi `stt_language` được set trong lumi config.
3. **IDENTITY** — `IDENTITY.md` đầy đủ (given name + species + traits).
4. **OWNER / USER PROFILE** — `USER.md` đầy đủ (name, preferences, timezone).
5. **LONG-TERM MEMORY** — `workspace/memory/*.md` (concat
   `MEMORY_FILES_KEEP` file mới nhất, tail-trim) hoặc fallback `MEMORY.md`.
6. **KNOWLEDGE** — `KNOWLEDGE.md` (mistakes agent đã học không lặp lại).
7. **PERSONA** — `SOUL.md` (giọng văn).

Thiếu file nào thì silent skip; brain vẫn boot mà không có file đó.

### History brain nhận được (per call)

Build lại mỗi lần `decide()` — pass qua array `contents` / `messages`,
**không** nhúng vào system prompt, để prefix cache giữ byte-stable.

`_merge_history()` interleave 2 nguồn rồi sort theo timestamp:

- **OpenClaw session JSONL** (luồng delegate) — đọc trực tiếp từ
  `$OPENCLAW_AGENTS_DIR/sessions/<sessionFile>` (resolve qua
  `sessions.json` index bằng `OPENCLAW_SESSION_KEY`). Cùng data mà
  `chat.history` WS RPC trả về. Hardware command, sensing tag, context
  blob, date header, `NO_REPLY`, heartbeat token đều bị strip — brain
  chỉ thấy text hội thoại.
- **In-process chit-chat log** (mấy turn chit-chat brain tự xử,
  không bao giờ tới OpenClaw) — giữ trong `_session_history` + persist
  JSONL ở `LELAMP_BRAIN_SESSION_LOG` để restart service không quên
  history cũ.

Cả 2 nguồn đều stamp `ts` Unix epoch; `merged.sort(key=...)` ra timeline
chronological. Cap ở `2 * LELAMP_BRAIN_SESSION_HISTORY_MAX` entry tổng
(giữ newest tail) để OpenClaw log dài không làm phình prompt.

---

## 5. Tích hợp với VoiceService

`VoiceService._stream_session` chạy pipeline STT cổ điển. Khi STT
finalize transcript (`final_text`) và event type = `voice` (speech
thường — wake-word event vẫn route riêng):

```python
decision = self._text_brain.decide(final_text, speaker=user)
if decision.decision == "chitchat" and decision.reply:
    self._tts.speak_queue(decision.reply)            # nhánh A
elif decision.decision == "delegate":
    self._send_to_lumi(final_msg, event_type="voice")  # nhánh B
else:  # decision.decision == "error"
    self._send_to_lumi(final_msg, event_type="voice")  # safe fallback
```

Speaker recognition, SER, wake-word router chạy trước brain — brain chỉ
thấy final transcript, không bao giờ thấy raw audio.

---

## 6. Prompt caching

Static system instruction (mọi thứ trong §4 trừ per-call history) build
1 lần trong `TextBrain.__init__` và cache cho suốt process lifetime.
Đọc lại `IDENTITY.md` / `USER.md` / `MEMORY.md` / etc. sau khi sửa
phải restart service — chấp nhận được với loại file này.

Vì sao quan trọng: với prefix byte-stable, cả 2 provider đều kick prompt
cache từ call #2:

- **OpenAI**: tự động cho mọi prefix ≥ 1024 token; cached input
  tính ~50 % rate bình thường.
- **Gemini**: implicit cache trên cùng prefix, discount tương đương.

Nếu cached prefix đổi (ví dụ sửa `MEMORY.md` rồi restart) thì call kế
tiếp trả full price; mấy call sau cache lại.

---

## 7. Hạn chế / follow-up

- **Không streaming reply** — `chat.completions` trả full reply trong
  1 HTTP response, nên chit-chat reply về 1 cục. Với reply 1–2 câu
  ngắn thì perceived latency chủ yếu do model generation time, không
  phải round-trip; streaming chỉ tiết kiệm tí mà nhân đôi parse
  complexity.
- **OpenClaw history read-only** — brain đọc JSONL của OpenClaw nhưng
  không write back. Chit-chat reply nằm trong JSONL riêng của brain
  (`LELAMP_BRAIN_SESSION_LOG`); không xuất hiện trong OpenClaw chat
  history hay Flow Monitor. Trade-off cross-process visibility.
- **Không barge-in** — khi TTSService đang phát chit-chat reply thì
  mic gate đang on; user không thể ngắt giữa chừng.
- **Static prompt = process-lifetime** — edit IDENTITY / USER / MEMORY
  / KNOWLEDGE / SOUL phải restart lelamp mới ăn vào brain. Luồng
  OpenClaw delegate thì pick-up ngay — chỉ fast path chit-chat ôm
  snapshot.
