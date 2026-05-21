# Voice Brain (Gemini Live)

Lớp định tuyến tuỳ chọn đặt **trước** pipeline STT → OpenClaw hiện có.
Thay vì mọi câu nói đều đẩy qua OpenClaw, brain mới chia làm 2 nhánh:

```
mic ─► VAD ─► Gemini Live ─┬─► tool_call delegate_to_lumi ─► OpenClaw (flow cũ)
                            └─► audio out trực tiếp ───────► loa (chit-chat)
```

**Vì sao cần:** OpenClaw tốn token + latency cho mọi lượt nói, kể cả
"chào", "cảm ơn". Brain "ngắn mạch" các câu chit-chat thường, dùng đúng
một cuộc gọi Gemini Live trả lời bằng giọng. Chỉ những yêu cầu cần tool,
hành động, hay câu trả lời dài mới được đẩy lên OpenClaw.

Brain là **opt-in**. Với `LELAMP_BRAIN=classic` (mặc định) lelamp giữ
nguyên hành vi như bản trước khi có brain.

---

## 1. Cách brain quyết định

Gemini Live nhận system prompt (`DECISION_RULES` trong
`lelamp/service/brain/gemini_live.py`) cộng audio người dùng và tự phân
loại từng lượt:

| Nhánh | Khi nào kích hoạt | lelamp làm gì |
| --- | --- | --- |
| **(A) Trò chuyện** | Model nói lại bằng giọng | `PCMAudioSink` phát PCM 24 kHz của Gemini thẳng ra loa. Không gửi gì sang OpenClaw. |
| **(B) Nhiệm vụ** | Model gọi `delegate_to_lumi(transcript=…)` | Transcript được forward sang Lumi y hệt như STT final: `POST /api/sensing/event`. OpenClaw xử lý tiếp như bình thường. |

Khi không chắc, system prompt yêu cầu model nghiêng về `delegate_to_lumi`
— thà chuyển nhầm còn hơn trả lời sai vì thiếu skill.

---

## 2. Bật brain mode

```bash
export LELAMP_BRAIN=gemini_live
export GEMINI_API_KEY=...                # bắt buộc

export LELAMP_BRAIN_TTS=native           # native | fallback (mặc định native)

# Tuỳ chọn:
export LELAMP_GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
export LELAMP_GEMINI_LIVE_VOICE=Aoede
export LELAMP_GEMINI_LIVE_LANGUAGE=vi-VN
export OPENCLAW_WORKSPACE=/root/.openclaw/workspace
export OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main
export OPENCLAW_SESSION_KEY=agent:main:main
export LUMI_BASE_URL=http://127.0.0.1:5000
```

Khi bật brain, **pipeline STT cổ điển bị bypass hoàn toàn**. Mọi frame
mic đi thẳng sang Gemini Live; Gemini lo VAD, turn detection, phân loại,
sinh câu trả lời. Không còn nhánh "STT-shaped fallback per-utterance" —
đã thử rồi nhưng silence-timeout của VoiceService cứ cắt giọng Gemini
giữa chừng.

Speaker recognition, SER, wake-word filter **không chạy** trong brain
mode (cần buffer audio per-session mà loop này không giữ). Cần lại
thì set `LELAMP_BRAIN=classic` để dùng STT cũ.

### `LELAMP_BRAIN_TTS`

| Giá trị | Ý nghĩa |
| --- | --- |
| `native` (mặc định) | Brain xin `response_modalities=[AUDIO]`. Gemini Live trả PCM 24 kHz, PCMAudioSink phát thẳng qua aplay (sounddevice fallback trên Mac). Latency thấp nhất nhưng giọng khác task reply. |
| `fallback` | Brain xin `response_modalities=[AUDIO]` + `output_audio_transcription`. Audio chunks bị drop, text transcribe được buffer rồi đưa cho `TTSService.speak_queue` khi `turn_complete` — giọng giống hệt task reply (ElevenLabs/OpenAI), nhưng tốn cả Gemini audio (vứt) + TTS synth (phát). |

`VoiceService.__init__` đọc `LELAMP_BRAIN` một lần duy nhất khi khởi động.
Bất cứ lỗi nào (không có key, chưa cài SDK, không mở được loa) đều log
warning và **fallback về STT classic** — production luôn an toàn.

Cài SDK:

```bash
pip install google-genai
# hoặc dùng uv (lelamp đang dùng):
uv pip install google-genai
```

---

## 3. Cấu trúc package

```
lelamp/service/brain/
  __init__.py        — public exports (Brain, BrainSession, BrainContext, …)
  base.py            — abstract Brain / BrainSession
  context_loader.py  — đọc SOUL.md + JSONL session main (mirror chat.history)
  audio_sink.py      — PCMAudioSink — aplay subprocess primary, sounddevice fallback
  gemini_live.py     — GeminiLiveBrain / GeminiLiveSession (bridge sync↔async)
lelamp/brain_demo.py — script demo standalone Mac/Linux (không cần Lumi)
lelamp/test/test_brain.py — unit test cho context loader
```

### Context brain nhận được

Giống y context của OpenClaw, **không có skills** (skills = nhiệm vụ =
thuộc về nhánh B rồi):

- `SOUL.md` — khối nhân vật từ `$OPENCLAW_WORKSPACE/SOUL.md`
- **History session main** — đọc thẳng từ JSONL của OpenClaw, đúng cái
  nguồn mà `chat.history` WS RPC cũng đọc. Path lấy qua
  `$OPENCLAW_AGENTS_DIR/sessions/sessions.json` →
  `<sessionFile>` cho sessionKey `agent:main:main` (override bằng
  `OPENCLAW_SESSION_KEY`). Quét ngược từ cuối file, chỉ giữ `role` ∈
  {`user`, `assistant`} với part `type == "text"`, bỏ noise
  `[OpenClaw heartbeat poll]` / `HEARTBEAT_OK`, rồi lấy `history_limit`
  lượt cuối theo đúng thứ tự thời gian.
- **Fallback:** nếu workspace không truy cập được (vd đang dev trên Mac
  chưa có file Pi), loader chuyển sang
  `GET {LUMI_BASE_URL}/api/agent/recent` — log monitor của Lumi. Độ
  chi tiết thấp hơn JSONL nhưng đủ để chit-chat khi dev.

Path resolve theo 2 dạng:
- `OPENCLAW_WORKSPACE=/root/.openclaw/workspace` (nơi `SOUL.md` ở);
  loader tự suy ra `agents/main` từ cha của nó.
- `OPENCLAW_AGENTS_DIR=/root/.openclaw/agents/main` để override
  thẳng nếu layout khác.

Nguồn nào không lấy được thì bỏ qua im lặng — brain vẫn chạy, chỉ là không
có phần đó trong prompt.

---

## 4. Chạy thử trên Mac

```bash
cd lelamp
export GEMINI_API_KEY=...
python -m lelamp.brain_demo
```

Mở mic + loa mặc định, in `[lumi] …` cho text deltas (nếu có) và
`>>> [TASK → would POST to Lumi] '…'` mỗi lần brain escalate một câu sang
flow task. Không cần Lumi server hay OpenClaw — tiện thử voice / câu chữ
mà không phải redeploy lên thiết bị.

Nếu chưa set `OPENCLAW_WORKSPACE`, demo dùng persona dev built-in để brain
vẫn nói "đúng giọng Lumi" trên máy sạch.

---

## 5. Tích hợp với VoiceService

`VoiceService.__init__` đọc `LELAMP_BRAIN`. Nếu là `gemini_live` và SDK
+ key OK, nó dựng `GeminiLiveBrain` + `PCMAudioSink` rồi gắn vào
`self._brain` / `self._brain_sink`. `_loop()` sau đó fork qua
`_continuous_brain_loop()` thay vì VAD loop cũ.

`_continuous_brain_loop` mở 1 mic duy nhất + 1 brain session duy nhất
suốt thời gian service chạy. Mỗi mic frame được gửi thẳng tới Gemini
Live. 3 callbacks wire trong loop:

- `on_delegate(transcript)` → `_send_to_lumi(transcript, "voice")`
- `on_audio_chunk(pcm)`     → `_brain_sink.push(pcm)` (chỉ native mode)
- `on_text(text, is_final)` → buffer, rồi `TTSService.speak_queue(text)`
  khi `turn_complete` (chỉ fallback mode)

Khi brain session kết thúc (delegate fire, Gemini disconnect, idle
timeout…) loop mở session mới rồi tiếp tục.

`_tts_is_speaking()` được mở rộng để trả `True` khi brain sink đang
phát audio, để mic gate chống echo TTS cũng chống echo voice của
brain. Lớp 2 là RMS-based reverb gate — mic im cho đến khi RMS đo được
< `LELAMP_ECHO_RMS_FLOOR`.

---

## 6. Định dạng audio

| Chiều | Format |
| --- | --- |
| Mic → brain | PCM int16 LE mono, 16 kHz (tận dụng resample sẵn có của VoiceService) |
| Brain → loa | PCM int16 LE mono, 24 kHz (Gemini Live native) |

`PCMAudioSink` mở 1 backend duy nhất cho cả session (aplay subprocess
ưu tiên, sounddevice là fallback). Trên Pi chỗ PortAudio giữ độc quyền
card seeed/wm8960 cho TTSService, đặt `LELAMP_BRAIN_OUTPUT_ALSA` về 1
device dmix/plug để aplay share output.

---

## 7. Hạn chế đã biết / follow-up

- **Khác giọng giữa hai nhánh (native mode)** — chit-chat dùng giọng
  Gemini (`Aoede` default), task quay về TTS ElevenLabs/OpenAI. Khắc
  phục: set `LELAMP_BRAIN_TTS=fallback` để 1 giọng duy nhất (đánh đổi
  latency cao hơn cho chit-chat).
- **Không ngắt lời giữa chừng** — khi brain đang nói thì mic bị gate,
  user chưa "đè" lên được. Muốn cho ngắt thì phải tích hợp echo-cancel
  biết buffer phát của brain.
- **Mất speaker recognition / SER / wake-word trong brain mode** — các
  pipeline đó cần buffer audio per-utterance, loop 1-session-liên-tục
  không giữ. Cần lại thì hoặc về STT classic
  (`LELAMP_BRAIN=classic`), hoặc wire 1 buffer per-turn vào
  `_continuous_brain_loop` đọc `turn_complete` / `interrupted` từ Gemini.
- **History chỉ đọc** — brain đọc lịch sử Lumi nhưng không ghi ngược lại.
  Chit-chat reply không hiện trong Flow Monitor. Nếu cần audit thì sau
  POST một event tổng hợp khi brain xử lý xong lượt.
- **Không nhớ giữa các session** — stateless cho MVP. Khi cần liền mạch
  hội thoại mới gắn thêm buffer turn ngắn hạn trong `GeminiLiveBrain`.
