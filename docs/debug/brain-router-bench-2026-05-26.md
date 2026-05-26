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
