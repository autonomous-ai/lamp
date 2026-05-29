# Brain Router Bench — 26/05/2026

So sánh latency giữa **brain trả thẳng** vs **OpenClaw xử lý** cho các turn chitchat-style, để quyết định router.

Nguồn dữ liệu (Pi 172.168.20.106):
- `/root/.brain/workspace/bench/2026-05-25.jsonl` — bench brain (63 turn)
- `/root/local/flow_events_2026-05-{21..26}.jsonl` — flow events OpenClaw (6 ngày)

## 1. Brain trả thẳng (chitchat local)

63 turn / 25-05, 100% OpenAI:

| Model | n | p50 | p90 | mean |
|---|---|---|---|---|
| gpt-4o-mini | 6 | 2.0s | 2.4s | 1.8s |
| gpt-5.5 | 57 | 4.6s | 9.1s | 4.9s |

→ **Chitchat brain ≈ 4-5s/turn** (gpt-4o-mini nhanh hơn ~2x nhưng ít mẫu).

## 2. Delegate decision time (brain nghĩ rồi route)

11 turn delegate / 25-05 — chỉ là thời gian LLM call để quyết định:

```
1.55  1.66  1.87  2.02  2.39  3.41  4.03  4.57  7.16  7.74  10.44 s
```

- p50: 3.4s · p90: 7.7s · mean: 4.3s
- Tỷ lệ thuận với prompt size: 4.5k token → 1.5-2.4s; 7-8k token → 4.5-10.4s.

## 3. OpenClaw xử chitchat (delegate path, no tool_call)

Lọc từ flow events 21-26/05: voice turn (`trace_id=lumi-chat-*`), có `agent_call`, không `tool_call`. Loại 3 outlier >30s (warmup/reconnect).

**152 turn sạch:**

| Đo từ user→ | p50 | p90 | p99 | max |
|---|---|---|---|---|
| reply_start (first_token/tts_send) | **7.5s** | 14.4s | 21.2s | 25.0s |

Cumulative: 60% ≤ 8s · 79% ≤ 10s · 92% ≤ 15s.

**Phân loại 152 turn:**

| Loại | n | % | p50 | mean |
|---|---|---|---|---|
| Pure chitchat (hello, mấy giờ, cám ơn) | 7 | 5% | 8.1s | 7.0s |
| Likely chitchat (≤6 từ, không lệnh) | 75 | 49% | 6.9s | 6.8s |
| Needs OC (đèn/nhạc/nhắc) | 26 | 17% | 8.2s | 8.7s |
| Ambiguous | 44 | 29% | 7.9s | 8.4s |

→ **54% (82/152)** là chitchat-like nhưng bị đẩy sang OpenClaw.

## 4. Apples-to-apples

| Path | p50 | p90 | tệ nhất |
|---|---|---|---|
| Brain chitchat | ~4-5s | ~8s | ~10s |
| OpenClaw chitchat (no tool) | ~7.5s | ~14.5s | ~25s |

→ **OpenClaw chậm gấp đôi brain cho chitchat thuần.**
→ Mỗi turn route sai phí ~**2-4s**. Trong 6 ngày qua: ~3 phút tổng.

## 5. Phát hiện phụ

1. **26 turn "needs OC" mà KHÔNG tool_call** — kiểu `"tắt nhạc đi"`, `"Con nhạc chill đi"`. Brain delegate đúng nhưng OpenClaw không gọi tool. Đây là **lỗi pipeline OpenClaw**, cần dump list để debug.
2. **3 outlier 33-143s** = session warmup/reconnect đầu phiên — không phản ánh latency thực.
3. **Bench writer chết từ 22/5 → 25/5** (file `/root/local/brain_bench/2026-05-22.jsonl` rồi nhảy sang path mới `/root/.brain/workspace/bench/`).

## Khuyến nghị

- **Giữ chitchat ở brain.** Chỉ delegate khi thật sự cần tool/memory/persona phức tạp.
- Cân nhắc dùng `gpt-4o-mini` cho router decision (2s vs 4.6s) — đánh đổi accuracy cần đo thêm.
- Đào 26 case "needs OC mà không tool_call" để fix pipeline OpenClaw.

---

## 6. Post-deploy validation (cùng ngày, sau 11:30)

Deploy 3 commit chạy thật trên Pi 172.168.20.106:

1. `99ffa76a` — OpenAI SSE streaming + `[DELEGATE]` prefix protocol (thay tool call)
2. `13e47a8d` — thêm field `first_sentence_s` vào bench (proxy TTFA)
3. `9a535e55` — doc này

Sau deploy commit 1 → 2, đổi env `LELAMP_OPENAI_TEXT_MODEL` từ `gpt-5.5` sang `gpt-4o-mini` để kiểm tra giả thuyết "TTFB của model thống trị latency".

### 6.1 Streaming + prefix hoạt động đúng

Bằng chứng từ journal (turn "Một bài đi", 11:33:25):

```
TTS queued for pre-synth (busy, queue depth=1): Một ngọn đèn thức khuya
TTS queued for pre-synth (busy, queue depth=2): Nghe mưa rơi rất nhẹ
TTS queued for pre-synth (busy, queue depth=3): Bàn tay ai chạm khẽ
TTS queued for pre-synth (busy, queue depth=4): Đêm bỗng mềm như mơ.
```

→ 4 câu thơ stream lần lượt vào TTS speak_queue trong khi LLM còn đang gen. Pattern gapless hoạt động hoàn hảo.

### 6.2 Bảng so sánh latency thực (27 turn post-deploy)

| Model | Decision | n | decide mean | decide p50 | first mean | first p50 |
|---|---|---|---|---|---|---|
| **gpt-5.5** | chitchat | 15 | 5.30s | 5.16s | 3.77s | 3.81s |
| **gpt-5.5** | delegate | 3 | 3.62s | 3.04s | – | – |
| **gpt-4o-mini** | chitchat | 5 | **2.19s** | **2.16s** | **1.65s** | **1.45s** |
| **gpt-4o-mini** | delegate | 4 | **1.24s** | **1.31s** | – | – |

→ Đổi model giảm **decide ~58 %**, **first_sentence ~56 %**, **delegate ~66 %**.

### 6.3 Best/worst case quan sát được

- **TTFA tốt nhất:** gpt-4o-mini "Làm một bài thơ đi" → first=**0.84s**, decide=1.55s. Loa kêu câu đầu trong khi LLM còn gen 2 câu sau.
- **TTFA tệ nhất (gpt-5.5):** "Đau lên miệng rồi..." → decide=7.95s. Prompt 7k token cộng reply dài.
- **Delegate nhanh nhất:** gpt-4o-mini "Nãy giờ tui mình nói gì" → decide=**0.92s** (chỉ cần emit `[DELEGATE]` marker, abort stream).

### 6.4 Phân tích "vì sao streaming một mình không đủ"

Với gpt-5.5, `first_sentence_s` ≈ `latency_s` (chênh chỉ 0.2–0.6s). Lý do:

- **TTFB (time-to-first-token):** ~3s cho prompt 7k token trên gpt-5.5
- **Token generation:** ~150–200 tok/s → 100–200 token output mất ~1s
- Streaming chỉ giúp **trong giai đoạn token gen**, không can thiệp TTFB

→ Nút thắt thật là TTFB của model. gpt-4o-mini có TTFB ~0.5–1s nên TTFA tụt rõ rệt.

### 6.5 Chất lượng reply gpt-4o-mini (subjective)

- Vẫn tự nhiên, đúng giọng persona, dùng voice marker `[chuckle]` / `[sigh]`
- Reply ngắn gọn hơn gpt-5.5 (~35 token vs ~160 token cho cùng câu hỏi) — phù hợp voice front-door
- Routing đúng 4/4 case delegate test (BTC, sở thích, thói quen, "nãy giờ mình nói gì")
- Không thấy hallucination về thông tin owner

### 6.6 Khuyến nghị cập nhật

- **Dùng `gpt-4o-mini` làm default** cho router. Default trong code đã là mini; trên Pi chỉ cần bỏ override `LELAMP_OPENAI_TEXT_MODEL=gpt-5.5` trong `/opt/lelamp/.env`.
- Nếu muốn dùng gpt-5.5 cho reply dài/phức tạp, cân nhắc **2-stage**: gpt-4o-mini quyết định + 1 câu opening → TTS ngay → gpt-5.5 gen phần sau ở background. Phức tạp hơn, chỉ làm nếu chất lượng mini không đủ.
- Verify OpenAI prompt-cache hit rate — nếu miss (cache TTL ~5–10 min) thì TTFB tăng. Có thể warm cache bằng heartbeat ping.

### 6.7 Bonus thử gpt-5-mini (KHÔNG dùng được)

Để tìm "smart hơn 4o-mini, nhanh hơn 5.5", thử `gpt-5-mini` ngày
26/5 chiều. Kết quả 4 turn liên tiếp:

| Turn | Decision | decide | first_sentence |
|---|---|---|---|
| "Khỏe khom" | chitchat | **13.40s** | 13.30s |
| "Mình rất khỏe nha" | chitchat | 10.50s | 10.38s |
| "Đèn đỏ đi" | delegate | 4.94s | – |
| "Hồi lại lần nữa nè..." | chitchat | 9.67s | 9.60s |

→ Chitchat mean **~11s**, gấp 5× gpt-4o-mini và gấp 2× gpt-5.5.

**Nguyên nhân:** gpt-5-mini là **reasoning model** (gpt-5 series có
internal thinking phase trước khi sinh token output). Dù response text
ngắn, TTFB bị inflate bởi hidden reasoning tokens. Loại model này
**không phù hợp cho voice front-door** — user phải đợi vài giây "im
lặng" trước khi loa kêu, cảm giác đèn bị treo.

Cùng họ với `o1`, `o3`, `o4-mini` — tránh hết cho voice. Reasoning model
tốt cho code/math, dở cho realtime UX.

**Còn lại để thử:** `gpt-4.1-mini` (không reasoning, smart hơn 4o-mini
một bậc, TTFB ~1-1.5s) là candidate hợp lý duy nhất giữa "fast" và
"smart" trong OpenAI lineup.

Tên model bịa cần tránh: `gpt-5.5-mini` không tồn tại (404), `gpt-4-mini`
không tồn tại. Phải dùng tên đúng từ docs OpenAI.

### 6.8 Fix gpt-5-mini bằng `reasoning_effort=minimal` (SWEET SPOT)

Đào docs OpenAI thấy gpt-5 series support param `reasoning_effort`:

- gpt-5 / gpt-5-mini: `minimal`, `low`, `medium` (default), `high`
- gpt-5.1+: thêm `none`

→ Default `medium` chính là thủ phạm — model burn 5-10s hidden thinking
tokens trước khi emit token đầu. Set `minimal` → tắt gần hết reasoning,
TTFB tụt drastically.

**Code change** (`text_router.py:_decide_openai`): đọc env
`LELAMP_OPENAI_REASONING_EFFORT`, nếu set thì append vào payload.
Optional — không set thì model dùng default (an toàn cho gpt-4o*,
gpt-4.1* không hỗ trợ field này).

**Kết quả sau khi set `LELAMP_OPENAI_REASONING_EFFORT=minimal` (4 turn):**

| Turn | Decision | decide | first_sentence |
|---|---|---|---|
| "Bồ mì ơi!" | chitchat | 3.76s | 2.15s |
| "Có việc gì" | chitchat | 2.71s | 2.24s |
| "Gửi trái nghe coi Khi" | chitchat | 3.16s | 3.15s |
| "Nãy giờ tụi mình nói chuyện gì" | chitchat ✅ | 2.66s | 2.54s |
| "Bật đèn vàng đi" | delegate | 1.98s | – |

→ Chitchat mean **3.07s** (vs 11.19s không tắt reasoning) = giảm
**3.6×**. TTFA mean **2.52s** (vs 11.09s) = giảm **4.4×**.

### 6.9 Bảng tổng hợp 3 model finalist

| Model | Config | decide | TTFA | delegate | Routing quality | Note |
|---|---|---|---|---|---|---|
| gpt-4o-mini | default | 2.2s | 1.6s | 1.2s | basic | Đôi khi nhầm "nãy giờ" thành delegate |
| **gpt-5-mini** | `reasoning_effort=minimal` | **3.1s** | **2.5s** | **2.0s** | **good** | **Sweet spot** ⭐ |
| gpt-5.5 | default (effort=medium) | 5.3s | 3.8s | 3.6s | best | Đắt + chậm cho voice |

**Recommend default:** `gpt-5-mini` + `reasoning_effort=minimal`.
- Smart hơn 4o-mini rõ (catch case "nãy giờ" mà 4o-mini miss)
- TTFA 2.5s vẫn cảm giác phản hồi tốt cho voice
- Cost $0.25/1M ≈ giữa mini ($0.15) và 5.5 ($1.25)

### 6.10 Live mode (Gemini Live, gemini-3.1-flash-live-preview) — TTFA win

Sau khi tách thành `brain/call/` + `brain/live/` + restore code Gemini
Live + LiveBrainRunner + ElevenLabs route, đo trên Pi 172.168.20.106:

**TTFA (Time-To-First-Audio) cho chit-chat:**

| Turn | brain.input → brain.tts.start | brain.input → brain.chitchat |
|---|---|---|
| "Mình hỏi bạn dậy chưa?" | **1.0s** ⭐ | 7.0s (full reply) |
| "Em biết làm thơ gì?" | ~1.5-2s | 11s |
| "Làm một bài thơ có hai câu" | 0.6s | 8s |

→ TTFA ~1-2s — **nhanh nhất trong các phương án đã test**:

| Mode | Model | TTFA | Note |
|---|---|---|---|
| Call mode | gpt-5.5 | 3-4s | TTFB cao |
| Call mode | gpt-4o-mini | 1.5-2s | Smart đủ |
| Call mode | gpt-5-mini + reasoning=minimal | 2-3s | Sweet spot quality |
| **Live mode** | **gemini-3.1-flash-live-preview** | **1-2s** ⭐ | Best TTFA |

**Lý do TTFA live thấp:**
- Server-side VAD của Gemini detect end-of-turn ~100-500ms (vs call mode `SILENCE_TIMEOUT=2.5s` local)
- Audio → text → reply trong cùng 1 session, không pass STT round trip riêng
- Streaming sentence vào ElevenLabs ngay khi câu đầu xong (same speak_queue pattern call mode)

**Session lifecycle:**
- Connect overhead: 0.7-1.0s (đôi khi 3s với mạng chậm)
- Session lifetime: 1-5 phút, sau đó GoAway → reconnect
- Auto-restart cũng xảy ra khi `[DELEGATE]` text marker detected (Gemini occasionally emits text instead of calling tool — sau khi đổi prompt sang `DECISION_RULES_LIVE` thì hiếm hơn)

### 6.11 Live mode — known regressions vs call

Quality KHÔNG bằng call mode dù TTFA nhanh hơn. Trade-offs:

1. **Over-delegate** — Gemini đôi khi gọi `delegate_to_lumi` cho cả
   greetings ("Hello", "Alo", "sách sách"). Mitigated bằng cách tighten
   `DELEGATE_TOOL_DESCRIPTION`: thêm explicit "DO NOT call this for
   greetings, smalltalk, mumbled input; when in doubt prefer chit-chat".
2. **Mis-hear Vietnamese** — Gemini Live's input transcription đôi khi
   sai từ đồng âm tiếng Việt rõ rệt hơn Deepgram nova-3. Mitigated một
   phần bằng `speech_config.language_code=vi-VN`, nhưng Developer API
   không support `language_codes` (input lock) → vẫn auto-detect.
3. **Loss of in-session memory** giữa các turn — nếu force-close session
   sau `[DELEGATE]` text marker, turn tiếp theo bắt đầu từ cold context;
   replies hơi generic/feature-list.
4. **No mid-session history sync** — `send_client_content` với
   `turn_complete=False` không support trên 3.x Live tier (chỉ 2.5
   native-audio support, mà model đó silent quá). OpenClaw turns
   (Telegram, web chat) landed mid-session sẽ invisible cho đến
   GoAway (~10-15 phút). Accept gap.
5. **Speaker recog cần audio buffer** — runner giờ accumulate rolling
   30s deque trên local mic (call mode style), pass vào
   `_identify_and_decorate` khi delegate fires. Default fallback
   `"Unknown Speaker: <transcript>"` đảm bảo OpenClaw nhận format
   đúng dù speaker server fail.

### 6.12 Live mode — knobs tunable

```bash
# Provider switch
LELAMP_BRAIN_MODE=live                                    # call | live
LELAMP_BRAIN_PROVIDER=gemini                              # gemini | openai

# Model
LELAMP_GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview    # default
LELAMP_GEMINI_LIVE_VOICE=Aoede                            # used for in-session memory only
LELAMP_GEMINI_LIVE_LANGUAGE=vi                            # → vi-VN

# VAD (Gemini native — KHÔNG có RMS threshold)
LELAMP_LIVE_VAD_SILENCE_MS=500                            # default 100, docs recommend 500-800
LELAMP_LIVE_VAD_START_SENSITIVITY=low                     # low | high
LELAMP_LIVE_VAD_END_SENSITIVITY=                          # low | high (unset = SDK default)
LELAMP_LIVE_VAD_PREFIX_PADDING_MS=                        # int ms (default 20)

# Echo gate
LELAMP_LIVE_POST_TTS_HOLDOFF_S=0.6                        # drop mic frames N s sau TTS
```

### 6.13 OpenAI Realtime (gpt-realtime-2) — match call quality, beat Gemini latency

Sau khi xác minh OpenAI Realtime hỗ trợ mid-session
``conversation.item.create`` (Gemini 3.1 Live không), switch live
provider sang OpenAI. Setup verified trên Pi 172.168.20.106:

```
LELAMP_BRAIN_MODE=live
LELAMP_BRAIN_PROVIDER=openai
LELAMP_OPENAI_REALTIME_MODEL=gpt-realtime-2
LELAMP_OPENAI_REALTIME_REASONING_EFFORT=minimal
LELAMP_OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
```

**Tại sao 4 knob này:**

| Knob | Value | Lý do |
|---|---|---|
| `gpt-realtime-2` | reasoning model | Instruction following + tool use mạnh hơn 1.5 |
| `reasoning.effort=minimal` | minimal | Giảm TTFB — reasoning model default `low` vẫn add thinking tokens |
| `gpt-4o-mini-transcribe` | thay `whisper-1` | `whisper-1` hay hallucinate câu "Hẹn gặp lại các bạn trong những video tiếp theo" từ silence; mini-transcribe stable hơn rõ |
| `mid-session sync` | `conversation.item.create` | Brain push OpenClaw turns mới mà không trigger reply (set không gọi `response.create`) |

**Đo thực tế (5 turn liên tiếp):**

| Turn | Input | Decision | brain.input → brain.tts.start (TTFA) | input → reply done |
|---|---|---|---|---|
| "Bằng làm được gì?" | chitchat | – | – | 2s |
| "tôi hỏi bạn làm được gì?" | chitchat | – | – | 3s |
| "làm một bài thơ đi" | chitchat | – | – | 3s |
| "Bạn đã làm được gì?" | chitchat | – | – | 3s |
| "Bật đèn vàng đi" (mis-heard "mật liền vàng đi") | delegate | – | – | 5s (delegate fire) |
| "Bật nhạc đi" / "Bật nhạc lên" | delegate | – | – | ~4s |

→ **Chitchat full reply ~2-3s** vs Gemini Live ~7s = **2-3× nhanh hơn** Gemini cho cùng tác vụ.
→ Delegate **~3-5s** — Realtime function tool fire qua tool_call event.

**Quality vs Gemini Live:**

| Metric | Gemini Live | OpenAI Realtime |
|---|---|---|
| Chitchat reply naturalness | OK, hơi feature-list khi cold | Smooth conversational |
| Delegate routing accuracy | Over-delegate "Hello" / "Alo" | Đúng "Bật đèn/nhạc", giữ "Hello" chitchat |
| Vietnamese transcription | Auto-detect, đôi khi nhầm | `gpt-4o-mini-transcribe` + `language=vi` lock — chính xác hơn rõ |
| Mid-session history sync | KHÔNG (3.1 reject) | ✅ Native support |
| Cost | Audio token rate Gemini | Audio token Realtime + reasoning |
| Self-talk loop (Whisper hallucination) | Không (Gemini không hallucinate outro) | Fixed bằng switch `whisper-1` → `gpt-4o-mini-transcribe` |

→ **OpenAI Realtime gpt-realtime-2 + reasoning=minimal + gpt-4o-mini-transcribe** là sweet spot cho live mode hiện tại.

### 6.14 Sentence-streamed TTS hoạt động giống call mode

Cả 2 provider live đều fire `on_text(delta, False)` per chunk + `on_text("", True)` final. Runner xử lý:
- Accumulate text → split sentence boundary
- Push từng câu vào `tts.speak_queue`
- ElevenLabs pre-synth queue depth 1-4 → gapless playback
- `brain.tts.start` log ở câu đầu để đo TTFA

Pattern: trên reply 3-câu, sẽ thấy:
```
brain.input  [live] '...'
brain.tts.start [live] '<câu 1>'
TTS queued for pre-synth (busy, queue depth=1): <câu 2>
TTS queued for pre-synth (busy, queue depth=2): <câu 3>
brain.chitchat [live] '<full reply>'
```

### 6.15 Known issues sau deploy

- **TTS proxy 400 cho voice `Huyen`** (id `foH7s9fX31wFFH2yqrFa`): nhiều câu reply không phát được audio do `campaign-api.autonomous.ai/.../elevenlabs/...` trả `400 Bad Request`. Streaming code không lỗi, lỗi ở proxy/voice config. Cần check voice ID hoặc switch về `Linh`.
- **"Một bài đi" bị nhầm thành chitchat** (gpt-5.5 viết thơ thay vì delegate music) — routing miss của DECISION_RULES. Cần thêm ví dụ "một bài" / "bài hát" / "mở nhạc" vào rule A, hoặc kỳ vọng catalog SKILL.md mô tả rõ hơn về music skill.
- **brain.context dump spam 42 dòng/turn** trong journal — filter cần regex `brain\.(input|decide|chitchat \[|delegate|e2e)` để loại `.context`, không dùng plain "brain" filter.
