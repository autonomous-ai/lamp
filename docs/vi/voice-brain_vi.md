# Voice Brain (realtime providers)

Lớp định tuyến tuỳ chọn đặt **trước** pipeline STT → OpenClaw hiện có.
Thay vì mọi câu nói đều đẩy qua OpenClaw, brain mới chia làm 2 nhánh:

```
mic ─► TTS echo gate ─► <provider> (server VAD) ─┬─► tool_call delegate_to_lumi ─► OpenClaw (flow cũ)
                           └─► audio out trực tiếp ───────► loa (chit-chat)
```

**Vì sao cần:** OpenClaw tốn token + latency cho mọi lượt nói, kể cả
"chào", "cảm ơn". Brain "ngắn mạch" các câu chit-chat thường, dùng đúng
một cuộc gọi realtime trả lời bằng giọng. Chỉ yêu cầu cần tool, hành
động, hay câu trả lời dài mới được đẩy lên OpenClaw.

Brain là **opt-in**. Với `LELAMP_BRAIN_PROVIDER=none` (mặc định, hoặc
không set) lelamp giữ nguyên hành vi như bản trước khi có brain.

---

## 1. Provider

| Provider | Giá trị env | API | Model mặc định | SDK |
| --- | --- | --- | --- | --- |
| Google Gemini Live | `gemini` | `google.genai` `live.connect` | `gemini-3.1-flash-live-preview` | `google-genai>=0.7.0` |
| OpenAI Realtime    | `openai` | `openai.beta.realtime.connect` | `gpt-realtime` | `openai>=1.40.0` |

Tên provider theo convention LiteLLM / LangChain / OpenRouter (vendor
name, không phải product name). Sau khi benchmark muốn loại provider
nào thì xoá file module + xoá 1 dòng trong
`lelamp/service/brain/factory.py:_PROVIDERS` — không phải sửa file
nào khác.

`LELAMP_BRAIN_PROVIDER` chấp nhận:

| Giá trị | Ý nghĩa |
| --- | --- |
| chưa set / `none` / `off` / `classic` / `disabled` | brain tắt, chạy STT classic |
| `gemini` | Gemini Live realtime |
| `openai` | OpenAI Realtime |
| khác | log unknown rồi fallback STT classic |

---

## 2. Cách brain quyết định

Provider được chọn nhận cùng một system prompt (`DECISION_RULES` trong
`lelamp/service/brain/prompts.py`) cộng audio người dùng và tự phân
loại từng lượt:

| Nhánh | Khi nào kích hoạt | lelamp làm gì |
| --- | --- | --- |
| **(A) Trò chuyện** | Model nói lại bằng giọng | `PCMAudioSink` phát PCM 24 kHz thẳng ra loa. Không gửi gì sang OpenClaw. |
| **(B) Nhiệm vụ** | Model gọi `delegate_to_lumi(transcript=…)` | Transcript được forward sang Lumi y hệt như STT final: `POST /api/sensing/event`. OpenClaw xử lý tiếp như bình thường. |

Dùng chung 1 prompt giữa các provider giúp chất lượng quyết định so
sánh được — điểm khác duy nhất giữa các provider là wire protocol.

---

## 3. Bật brain mode

```bash
# Chung
export LELAMP_BRAIN_PROVIDER=gemini       # hoặc openai, hoặc none
export LELAMP_BRAIN_TTS=native            # native | fallback (mặc định native)

# Gemini Live
export GEMINI_API_KEY=...                 # bắt buộc khi provider=gemini
export LELAMP_GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
export LELAMP_GEMINI_LIVE_VOICE=Aoede
export LELAMP_GEMINI_LIVE_LANGUAGE=vi-VN  # rỗng/auto → để Gemini tự detect

# OpenAI Realtime
export OPENAI_API_KEY=...                 # bắt buộc khi provider=openai
export LELAMP_OPENAI_REALTIME_MODEL=gpt-realtime
export LELAMP_OPENAI_REALTIME_VOICE=alloy

# Context (chung cho cả 2)
export OPENCLAW_WORKSPACE=/root/.openclaw/workspace
export OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main
export OPENCLAW_SESSION_KEY=agent:main:main
export LUMI_BASE_URL=http://127.0.0.1:5000
```

Khi bật brain, **pipeline STT cổ điển bị bypass hoàn toàn**. Mọi
frame mic được đẩy thẳng sang provider, provider tự chạy server VAD
để quyết turn boundary — client KHÔNG còn lớp VAD nào nữa. Phần
gating duy nhất còn lại ở client là TTS echo gate (drop frame khi
chính Lumi đang phát) và reverb-decay gate (đợi RMS xuống dưới
`ECHO_RMS_FLOOR` sau khi loa tắt), để brain không tự nghe chính
nó.

Không còn nhánh "STT-shaped fallback per-utterance" — đã thử nhưng
silence-timeout của VoiceService cứ cắt giọng giữa chừng.

Speaker recognition, SER, wake-word filter **không chạy** trong brain
mode (cần buffer audio per-session mà loop này không giữ). Cần lại
thì set `LELAMP_BRAIN_PROVIDER=none` để dùng STT cũ.

### `LELAMP_BRAIN_TTS`

| Giá trị | Ý nghĩa |
| --- | --- |
| `native` (mặc định) | Phát PCM 24 kHz của provider thẳng qua aplay (sounddevice fallback trên Mac). Latency thấp nhất nhưng giọng khác task reply. |
| `fallback` | Audio chunks của provider bị drop, text transcribe được buffer rồi đưa cho `TTSService.speak_queue` khi turn-complete — giọng giống hệt task reply (ElevenLabs/OpenAI), nhưng tốn cả realtime audio (vứt) + TTS synth (phát). |

`VoiceService.__init__` đọc `LELAMP_BRAIN_PROVIDER` một lần khi khởi
động. Bất cứ lỗi nào (không có key, chưa cài SDK, mở loa fail, tên
provider không hợp lệ) đều log warning và **fallback về STT classic**
— production luôn an toàn.

Cài SDK:

```bash
pip install google-genai "openai[realtime]"
# hoặc dùng uv (lelamp đang dùng):
uv pip install google-genai "openai[realtime]"
```

Extra `[realtime]` kéo về dep `websockets` mà `client.realtime.connect`
cần. Nếu thiếu, brain log `"You need to install openai[realtime]…"` và
fallback STT classic.

(Chỉ cần SDK của provider đang chọn; cài cả 2 nếu muốn A/B test bằng
cách đổi `LELAMP_BRAIN_PROVIDER`.)

---

## 4. Cấu trúc package

```
lelamp/service/brain/
  __init__.py        — public exports + re-export factory.make_brain
  base.py            — abstract Brain / BrainSession
  prompts.py         — DECISION_RULES + DELEGATE_TOOL_* dùng chung cho mọi provider
  factory.py         — registry provider (LELAMP_BRAIN_PROVIDER → Brain class)
  context_loader.py  — đọc SOUL.md + JSONL session main (mirror chat.history)
  audio_sink.py      — PCMAudioSink — aplay subprocess primary, sounddevice fallback
  gemini_live.py     — GeminiLiveBrain / GeminiLiveSession (google-genai)
  openai_realtime.py — OpenAIRealtimeBrain / OpenAIRealtimeSession (openai SDK)
lelamp/brain_demo.py — script demo standalone Mac/Linux (không cần Lumi)
lelamp/test/test_brain.py — unit test cho context loader
```

### Context brain nhận được

Giống y context của OpenClaw, **không có skills** (skills = nhiệm vụ =
thuộc về nhánh B rồi):

- `SOUL.md` — khối nhân vật từ `$OPENCLAW_WORKSPACE/SOUL.md`
- **History session main** — đọc thẳng từ JSONL của OpenClaw, đúng
  nguồn `chat.history` WS RPC cũng đọc. Path lấy qua
  `$OPENCLAW_AGENTS_DIR/sessions/sessions.json` →
  `<sessionFile>` cho sessionKey `agent:main:main` (override bằng
  `OPENCLAW_SESSION_KEY`). Quét ngược từ cuối file, chỉ giữ `role` ∈
  {`user`, `assistant`} với part `type == "text"`, bỏ noise
  `[OpenClaw heartbeat poll]` / `HEARTBEAT_OK`, strip
  `[HW:/...]`, `[sensing:…]`, `[wellbeing_context: {...}]`, date
  headers, rồi lấy `history_limit` lượt cuối theo thứ tự thời gian.
- **Fallback:** nếu workspace không truy cập được (vd đang dev trên
  Mac), loader chuyển sang `GET {LUMI_BASE_URL}/api/agent/recent`
  — log monitor của Lumi. Độ chi tiết thấp hơn JSONL nhưng đủ để
  chit-chat khi dev.

Path resolve theo 2 dạng:
- `OPENCLAW_WORKSPACE=/root/.openclaw/workspace` (nơi `SOUL.md` ở);
  loader tự suy ra `agents/main` từ cha của nó.
- `OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main` để override
  thẳng nếu layout khác.

Nguồn nào không lấy được thì bỏ qua im lặng — brain vẫn chạy, chỉ là
thiếu phần đó trong prompt.

---

## 5. Chạy thử trên Mac

```bash
cd lelamp
export LELAMP_BRAIN_PROVIDER=gemini   # hoặc openai
export GEMINI_API_KEY=...             # (hoặc OPENAI_API_KEY)
python -m lelamp.brain_demo
```

Mở mic + loa mặc định, in `[lumi] …` cho text deltas (nếu có) và
`>>> [TASK → would POST to Lumi] '…'` mỗi lần brain escalate một câu
sang flow task. Không cần Lumi server hay OpenClaw — tiện thử voice /
câu chữ mà không phải redeploy lên thiết bị.

Nếu chưa set `OPENCLAW_WORKSPACE`, demo dùng persona dev built-in để
brain vẫn nói "đúng giọng Lumi" trên máy sạch.

---

## 6. Tích hợp với VoiceService

`VoiceService.__init__` đọc `LELAMP_BRAIN_PROVIDER`. Nếu giá trị là
một provider hợp lệ AND SDK + key OK, nó gọi
`brain.make_brain(provider, …)` lấy về 1 instance `Brain` và mở
`PCMAudioSink` đặt vào `self._brain` / `self._brain_sink`. `_loop()`
sau đó fork qua `_continuous_brain_loop()` thay vì VAD loop cũ.

`_continuous_brain_loop` mở 1 mic duy nhất + 1 brain session duy nhất
suốt thời gian service chạy. Mỗi mic frame được gửi thẳng tới
realtime API của provider. 3 callbacks wire trong loop:

- `on_delegate(transcript)` → `_send_to_lumi(transcript, "voice")`
- `on_audio_chunk(pcm)`     → `_brain_sink.push(pcm)` (chỉ native mode)
- `on_text(text, is_final)` → buffer, rồi `TTSService.speak_queue(text)`
  khi `turn_complete` (chỉ fallback mode)

Khi brain session kết thúc (delegate fire, provider disconnect, idle
timeout…) loop mở session mới rồi tiếp tục.

`_tts_is_speaking()` được mở rộng để trả `True` khi brain sink đang
phát audio, để mic gate chống echo TTS cũng chống echo voice của
brain. Lớp 2 là RMS-based reverb gate — mic im cho đến khi RMS đo
được < `LELAMP_ECHO_RMS_FLOOR`.

---

## 7. Định dạng audio

| Chiều | Format | Ghi chú |
| --- | --- | --- |
| Mic → brain | PCM int16 LE mono, **16 kHz** | output resample sẵn của VoiceService |
| Mic → Gemini Live | 16 kHz, gửi nguyên | Gemini Live nhận 16 kHz `audio/pcm` |
| Mic → OpenAI Realtime | **24 kHz**, polyphase resample trong `openai_realtime.py` | OpenAI Realtime cần 24 kHz `pcm16` |
| Brain → loa | PCM int16 LE mono, **24 kHz** | Cả 2 provider đều stream 24 kHz — khớp `PCMAudioSink` mặc định |

`PCMAudioSink` mở 1 backend duy nhất cho cả session (aplay subprocess
ưu tiên, sounddevice là fallback). Trên Pi chỗ PortAudio giữ độc quyền
card seeed/wm8960 cho TTSService, đặt `LELAMP_BRAIN_OUTPUT_ALSA` về 1
device dmix/plug để aplay share output.

---

## 8. Thêm provider mới

Muốn thêm (vd) `anthropic`:

1. Tạo `lelamp/service/brain/anthropic_realtime.py` implement `Brain`
   + `BrainSession`. Dùng lại `prompts.DECISION_RULES`,
   `prompts.DELEGATE_TOOL_NAME`, `prompts.DELEGATE_TOOL_DESCRIPTION`,
   và `context_loader.load_context` để persona / routing / context
   giống y các provider khác.
2. Thêm 1 dòng vào `lelamp/service/brain/factory.py:_PROVIDERS`:
   ```python
   "anthropic": ("lelamp.service.brain.anthropic_realtime", "AnthropicRealtimeBrain"),
   ```
3. Document env var trong file này. Không cần sửa gì khác.

Retire provider thì làm ngược lại — xoá module file + xoá dòng đó.

---

## 9. Hạn chế đã biết / follow-up

- **Khác giọng giữa hai nhánh (native mode)** — chit-chat dùng giọng
  provider (`Aoede` cho Gemini, `alloy` cho OpenAI), task quay về TTS
  ElevenLabs/OpenAI. Khắc phục: set `LELAMP_BRAIN_TTS=fallback` để 1
  giọng duy nhất (đánh đổi latency cao hơn cho chit-chat).
- **Không ngắt lời giữa chừng** — khi brain đang nói thì mic bị gate,
  user chưa "đè" lên được. Muốn ngắt thì phải tích hợp echo-cancel
  biết buffer phát của brain.
- **Mất speaker recognition / SER / wake-word trong brain mode** —
  các pipeline đó cần buffer audio per-utterance, loop
  1-session-liên-tục không giữ. Cần lại thì hoặc về STT classic
  (`LELAMP_BRAIN_PROVIDER=none`), hoặc wire 1 buffer per-turn vào
  `_continuous_brain_loop` đọc `turn_complete` / `response.done` từ
  provider.
- **History chỉ đọc** — brain đọc lịch sử Lumi nhưng không ghi ngược.
  Chit-chat reply không hiện trong Flow Monitor. Nếu cần audit thì
  sau POST một event tổng hợp khi brain xử lý xong lượt.
- **Không nhớ giữa các session** — stateless cho MVP. Khi cần liền
  mạch hội thoại mới gắn thêm `session_resumption` (Gemini) hoặc
  `conversation.item.retrieve` replay (OpenAI).
