# Hermes backend integration — design notes

Branch: `feat/hermes`. Status: design only, no code committed yet.

## Goal

Thêm Hermes làm **backend agent thứ hai** cho Lumi, song song với OpenClaw, switch bằng config. Hermes dùng **HTTP + SSE** (OpenAI Responses API style) thay vì WebSocket.

Lumi đã có sẵn `domain.AgentGateway` interface (`lumi/domain/agent.go:30`) — comment nói rõ "abstracts an agentic runtime (OpenClaw, PicoClaw, etc.)". → Hermes implement đúng interface đó, không phải refactor lớn.

---

## 1. Backend selection

Thêm config field:

```jsonc
// config/config.json
{
  "agent_backend": "hermes",          // "openclaw" (default) | "hermes"
  "hermes_base_url": "http://127.0.0.1:8642",
  "hermes_api_key":  "change-me-local-dev",
  "hermes_conversation": "lumi-main", // named conversation cho mọi turn từ thiết bị
  "hermes_model": "hermes-agent",
  // ... các field cũ giữ nguyên
}
```

Tại `lamp/server/wire.go` — bind `domain.AgentGateway` provider:

```go
func ProvideAgentGateway(cfg *config.Config, ...) domain.AgentGateway {
    switch cfg.AgentBackend {
    case "hermes":
        return hermes.ProvideService(cfg, ...)
    default:
        return openclaw.ProvideService(cfg, ...)
    }
}
```

Mọi consumer (`server/server.go`, handlers) đã consume qua `domain.AgentGateway` interface — không cần đụng.

---

## 2. Protocol mapping (Hermes SSE → `domain.WSEvent`)

Lumi event handler (`handler_events.go:44`) đã switch theo `evt.Event` + `payload.Stream`. Hermes adapter dịch SSE thành event giả lập cùng shape:

| Hermes SSE event | → Lumi `domain.WSEvent` |
|---|---|
| `response.created` | `event:"agent", payload.stream:"lifecycle", phase:"start"`, gắn `runId = response.id`, `sessionKey` từ header `X-Hermes-Session-Id` |
| `response.output_item.added` (`type:"message"`) | bỏ qua — chờ delta |
| `response.output_text.delta` | `event:"agent", payload.stream:"assistant", delta:"..."` |
| `response.output_text.done` | bỏ qua (delta đã stream xong) |
| `response.output_item.added` (`type:"function_call"`) | `event:"agent", payload.stream:"tool", phase:"start", data.name=fn_name, data.args=arguments_json, data.toolCallId=call_id` |
| `response.output_item.done` (`type:"function_call_output"`) | `event:"agent", payload.stream:"tool", phase:"end", data.result=output_text, data.toolCallId=call_id` |
| `response.completed` | (a) `event:"chat", state:"final", role:"assistant", message=full_text`; (b) `event:"agent", payload.stream:"lifecycle", phase:"end", data.usage={...}` |
| `response.failed` | `event:"agent", payload.stream:"lifecycle", phase:"error", data.error=msg` |

→ Sau khi dịch, **đẩy vào cùng worker goroutine + handler** mà `internal/openclaw/service_ws.go` đang dùng. Handler ở `handler_events.go` không cần biết backend là gì.

### Thinking / reasoning

Hermes hiện chưa mô tả `response.reasoning.delta`. Nếu sau này có, map sang `stream:"thinking", delta:...`. Tạm thời bỏ qua, không emit stream thinking.

### Cron events

OpenClaw có `event:"cron"`. Hermes API không có cron native. → Không emit từ Hermes adapter; Lumi tự chạy scheduler nội bộ (đã có `lib/cron` chưa? — TODO check, nếu chưa thì bỏ `cronFireExpected` codepath khi `agent_backend == "hermes"`).

---

## 3. Session / conversation mapping

| OpenClaw | Hermes |
|---|---|
| `sessionKey` (UUID, server-persisted) | `X-Hermes-Session-Id` header (UUID, returned per response) |
| `mainSessionKey` | named `conversation: "lumi-main"` — server tự chain |
| `chat.send` với `sessionKey` | `POST /v1/responses` với `conversation: "lumi-main"` |
| `sessions.list` | không cần — single named conversation |
| `sessions.subscribe` | không cần — SSE theo từng request |
| `chat.history` | `GET /v1/responses/{id}` walk `previous_response_id` chain (nếu cần) |
| `sessions.compact` | spawn subprocess `hermes compact <session-uuid>` (xem §7) |

**State Lumi cần lưu** (`internal/hermes/conversation.go`):

```go
type ConversationState struct {
    name           string  // "lumi-main"
    sessionUUID    atomic.Value // string — captured from X-Hermes-Session-Id, dùng cho compact
    lastResponseID atomic.Value // string — debug/history walk only
}
```

`GetSessionKey()` trả `sessionUUID` (cho UI monitor hiển thị giống OpenClaw). `SetSessionKey()` no-op (Hermes tự quản).

**Multi-turn**: không cần `previous_response_id` thủ công — gửi `conversation` là đủ, Hermes auto-chain.

---

## 4. SSE consumer chi tiết

`internal/hermes/client.go`:

```go
func (c *Client) SendResponseStream(ctx context.Context, req SendRequest) error {
    body, _ := json.Marshal(req)  // {model, conversation, stream:true, input:[...]}
    httpReq, _ := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/v1/responses", bytes.NewReader(body))
    httpReq.Header.Set("Authorization", "Bearer "+c.apiKey)
    httpReq.Header.Set("Content-Type", "application/json")
    httpReq.Header.Set("Accept", "text/event-stream")

    resp, err := c.http.Do(httpReq)
    if err != nil { return err }
    defer resp.Body.Close()

    // Capture session UUID once
    if sk := resp.Header.Get("X-Hermes-Session-Id"); sk != "" {
        c.state.SetSessionUUID(sk)
    }

    scanner := bufio.NewScanner(resp.Body)
    scanner.Buffer(make([]byte, 0, 1<<20), 8<<20)  // 8MB max line (handle big tool outputs)
    var currentEvent string
    for scanner.Scan() {
        line := scanner.Text()
        switch {
        case strings.HasPrefix(line, "event: "):
            currentEvent = strings.TrimPrefix(line, "event: ")
        case strings.HasPrefix(line, "data: "):
            data := strings.TrimPrefix(line, "data: ")
            if data == "[DONE]" { return nil }
            c.translateAndDispatch(currentEvent, data)  // → domain.WSEvent → handler
        case line == "":
            currentEvent = ""
        }
    }
    return scanner.Err()
}
```

**`translateAndDispatch`** parse JSON theo bảng §2, build `domain.WSEvent`, đẩy vào `eventCh` (cùng channel với OpenClaw worker). Reuse được cả code dedup/flow.Log/monitorBus.

---

## 5. Image input mapping

OpenClaw `chat.send`:
```json
"attachments": [{"type":"image","mimeType":"image/jpeg","content":"<base64>"}]
```

Hermes `/v1/responses`:
```json
"input": [{
  "role":"user",
  "content":[
    {"type":"input_text","text":"<msg>"},
    {"type":"input_image","image_url":"data:image/jpeg;base64,<base64>"}
  ]
}]
```

`internal/hermes/service.go` `SendChatMessageWithImage()` build payload theo Hermes shape. Khi không có image → `"input": "<msg>"` (string shortcut, đỡ tốn token).

---

## 6. AgentGateway method mapping

| Method (interface) | Hermes impl |
|---|---|
| `Name()` | `"Hermes"` |
| `IsReady()` | poll `GET /health` mỗi 10s → atomic bool |
| `ConnectedAt()` | timestamp của health-check OK gần nhất |
| `AgentUptime()` | không có (Hermes `/health/detailed` có gì thì map; tạm trả 0) |
| `IsBusy()` / `SetBusy()` | giữ nguyên logic local (mark busy khi POST, clear khi `response.completed` SSE đến) |
| `QueuePendingEvent()` | reuse hoàn toàn code openclaw — chỉ là buffer local |
| `SendChatMessage()` | `POST /v1/responses` `stream:true` + conversation, return runID = response.id (chờ `response.created` để có id; idempotency key tự sinh) |
| `SendSystemChatMessage()` | giống `SendChatMessage` nhưng prefix `[system]` hoặc dùng `instructions` field |
| `SendChatMessageWithImage()` | xem §5 |
| `NextChatRunID()` | sinh local `chat-N` + `lumi-chat-N-<ts>` giống openclaw |
| `SendChatMessage*WithRun()` | gọi `SendChatMessage` với fixedRunID làm idempotency hint (Hermes không bắt buộc — Lumi vẫn dùng cho monitor) |
| `SendSlashCommandWithRun()` | giống — Hermes không có concept "deliver:false", coi tất cả là user input |
| `GetSessionKey()` | `state.sessionUUID.Load()` |
| `SetSessionKey()` | no-op (server-managed) |
| `SetupAgent()` | viết `hermes.yaml` hoặc `~/.hermes/config.json` + restart hermes service (xem §7) |
| `AddChannel()` | **không có channel plugin trong Hermes** — Lumi tự quản (xem §8) |
| `HasWhatsappSession()` / `PairWhatsapp()` | return false / channel báo `PairingStatusFailure("not supported on hermes")` |
| `ResetAgent()` | xoá `~/.hermes/` + restart |
| `RestartAgent()` | `systemctl restart hermes` |
| `RefreshModelsConfig()` | patch model trong hermes config + restart |
| `EnsureOnboarding()` | seed SOUL.md / IDENTITY.md vào thư mục instruction của Hermes (Hermes hỗ trợ `instructions` field hoặc system prompt file) |
| `FetchChatHistory()` | `GET /v1/responses/{last_id}` rồi walk `previous_response_id` chain, trả về array shape giống OpenClaw chat.history (cho `MatchPendingByMessage`) |
| `GetConfigJSON()` | đọc file hermes config |
| `StartWS(ctx, handler)` | **đổi semantic**: không có WS persistent, chỉ chạy goroutine health-poll + lưu handler ref để per-request SSE consumer biết route event vào đâu |
| `MarkGuardRun()` / `ConsumeGuardRun()` / `MarkBroadcastRun()` / … | reuse y nguyên openclaw code (chỉ là in-memory map runID → metadata) |
| `MarkPoseBucketRun()` / `Consume…` / `MarkWebChatRun()` / … | reuse |
| `SetPendingChatTrace()` / `RemovePendingChatTraceByRunID()` / `MatchPendingByMessage()` | reuse |
| `GetTelegramBotToken()` | đọc từ `config.TelegramBotToken` (config Lumi, không từ Hermes) |
| `GetTelegramTargets()` | **đổi nguồn**: Hermes không có `agents/main/sessions/sessions.json`. Lumi tự lưu danh sách chat target trong SQLite/JSONL local (`~/.lumi/telegram_targets.json`) |
| `Broadcast()` / `SendToUser*()` | giữ nguyên — đã chỉ là wrapper Telegram Bot API |
| `SendToLeLampTTS*()` / `StopTTS()` / `SetVolume()` / `StartLeLampVoice()` | giữ nguyên — chỉ POST sang LeLamp, không liên quan agent |
| `WatchIdentity()` | giữ nguyên (đọc IDENTITY.md local) |
| `StartSkillWatcher()` | reuse hoàn toàn — file SKILL.md trên disk vẫn watch & download. Notify agent qua `SendSystemChatMessage` (Hermes nhận hệt như OpenClaw) |
| `StartModelSync()` / `StartPrimaryModelWatch()` / `UpdatePrimaryModel()` | nhẹ hơn — Hermes chỉ có 1 model name, patch hermes config thay vì openclaw.json |
| `GetConfiguredChannel()` | trả `"telegram"` nếu `config.TelegramBotToken != ""`, nếu không `"channel"` |
| `CompactSession()` | xem §7 (subprocess) |
| `NewSession()` | tạo conversation mới: đổi `conversation` name (vd `lumi-main-2`) hoặc gọi Hermes API tạo conversation mới — TBD theo Hermes spec |
| `IsRecentOutboundChat()` | reuse — chỉ là cache string outbound gần đây |

---

## 7. Compaction — **skip ở giai đoạn 1**

Hermes giai đoạn này không có CLI compact. → `CompactSession()` là **no-op + log**:

```go
func (s *Service) CompactSession(sessionKey string) error {
    slog.Info("hermes compact: not supported, no-op", "session", sessionKey)
    return nil
}
```

Hệ quả: conversation `lumi-main` sẽ dài dần. Mitigation tạm thời — dùng `NewSession()` (đổi conversation name, vd `lumi-main-<unixDay>`) khi user gọi `/reset` hoặc khi reach một ngưỡng turn. Để follow-up phase 2 khi Hermes có CLI/API compact.

---

## 8. Channels (Telegram) — **Lumi tự chạy bot, parity với OpenClaw**

OpenClaw có plugin channel chạy **bên trong daemon** — user nhắn Telegram → daemon nhận → đẩy vào session → Lumi observe qua WS. Hermes không có plugin layer.

→ **Lumi tự host Telegram bot** khi `agent_backend == "hermes"`, mục tiêu UX parity với OpenClaw:

**Receive path** (mới, `internal/telegram/receiver.go`, ~250 LOC):
- Long-poll `getUpdates` mỗi vài giây, dùng `config.TelegramBotToken`.
- Filter theo `config.TelegramUserID` (whitelist DM) hoặc bot mention trong group.
- Mỗi message hợp lệ → `agentGateway.SendChatMessage(msg)` (hoặc `SendChatMessageWithImage` nếu user gửi ảnh — tải qua `getFile`).
- Lưu chat target vào `~/.lumi/telegram_targets.json` (auto-populate khi user nhắn lần đầu) → reply path đọc từ file này.

**Send path** (reuse):
- `internal/openclaw/telegram_sender.go` đã có sẵn Bot API client — **promote ra `internal/telegram/sender.go`** (đổi package) để cả 2 backend share.
- `Broadcast()`, `SendToUser()`, `SendToUserWithMedia()` không cần đổi semantic.

**Reply routing**:
- Khi Hermes trả `event:"chat", state:"final"`, handler kiểm tra runID có phải originated từ Telegram không (đánh dấu qua `MarkBroadcastRun` hoặc map riêng `telegramRunOrigin[runID] = chatID` khi receive).
- Nếu có → `SendToUser(chatID, replyText)` thay vì TTS.

**`GetTelegramTargets()`**:
- Hermes impl đọc từ `~/.lumi/telegram_targets.json` (Lumi-owned store) thay vì `agents/main/sessions/sessions.json` của OpenClaw.

→ **Giai đoạn 1 bắt buộc làm** vì user muốn parity với OpenClaw.

---

## 9. Skills

Skill loading hiện tại:
- `skill_watcher.go` download SKILL.md vào `~/.openclaw/skills/{name}/SKILL.md`
- Notify agent qua `SendSystemChatMessage("[system] skills updated...")`

Với Hermes:
- Download vào `~/.hermes/skills/{name}/SKILL.md` (hoặc thư mục Hermes quy định).
- Notify giống y — Hermes nhận `[system]` message và tự re-read disk.
- Yêu cầu: Hermes phải hỗ trợ "đọc file từ instructions dir" hoặc agent đủ thông minh đọc file qua tool `read`. Cần xác nhận với Hermes spec.

→ **Open question**: cơ chế load skill của Hermes thế nào? Nếu Hermes chỉ nhận `instructions` field static (1 string lúc create conversation), phải nhồi toàn bộ skill content vào đó → mất khả năng hot-reload. Cần check.

---

## 10. Lifecycle / process management — **Local trên Pi**

Hermes chạy local: `http://127.0.0.1:8642`.

- Binary `hermes` cài sẵn trên Pi (qua pip/npm/apt — install path ngoài scope Lumi).
- Service unit: `systemctl restart hermes` (giả định systemd unit tên `hermes`).
- Config Hermes tại `~/.hermes/config.{yaml,json}` — Lumi ghi vào đây từ `SetupAgent()`.
- `ProvideService` start health-poller goroutine, không exec.
- `RestartAgent()` → `exec systemctl restart hermes` (giống `restartOpenclawGateway` ở `service_gateway.go:65-72`).

**Config fields cố định** (không cần `hermes_managed_locally` flag):

```jsonc
{
  "hermes_base_url":     "http://127.0.0.1:8642",
  "hermes_api_key":      "change-me-local-dev",
  "hermes_conversation": "lumi-main",
  "hermes_model":        "hermes-agent"
}
```

---

## 11. File layout

### New files

```
lamp/internal/hermes/
├── service.go         (~180 LOC)  — AgentGateway impl, state, ProvideService
├── client.go          (~220 LOC)  — HTTP client, SSE consumer, retry
├── sse_translator.go  (~180 LOC)  — SSE event → domain.WSEvent
├── conversation.go    (~80 LOC)   — conversation/sessionUUID/responseID state
├── health.go          (~60 LOC)   — health poller goroutine
├── compact.go         (~40 LOC)   — subprocess wrapper
├── setup.go           (~150 LOC)  — write hermes config, restart, ResetAgent
├── runs.go            (~250 LOC)  — copy of openclaw/service_runs.go (Mark*/Consume*Run, PendingChatTrace)
└── wire.go            (~20 LOC)
```

**Note về `runs.go`**: tất cả `Mark*Run` / `Consume*Run` / `*PendingChatTrace` chỉ là in-memory map + mutex, không gắn gì với OpenClaw. Hai lựa chọn:

1. Copy file `runs.go` sang hermes/ (simple, redundant).
2. **Refactor**: tách thành package `internal/agent/runs/` shared cho cả 2 backend. Trong session đầu, dùng (1) cho nhanh; (2) là follow-up cleanup.

### Modified files

- `lamp/server/config/config.go` — thêm 5 field Hermes.
- `lamp/server/wire.go` — `ProvideAgentGateway` switch case.
- `lamp/internal/hermes/wire.go` — new wire set.
- Có thể cần: `lamp/server/config/config.go` thêm getter/setter.

### Unchanged

- `lamp/server/agent/delivery/http/handler_events.go` (đã switch theo `evt.Event`)
- `lamp/server/agent/delivery/http/handler_hw.go` (`[HW:/...]` markers độc lập backend)
- LeLamp, sensing, monitor SSE, LED, network, OTA, web UI
- `lamp/internal/openclaw/` (không đụng — vẫn là backend default)

---

## 12. System prompt cho Hermes

OpenClaw có `SOUL.md` + `KNOWLEDGE.md` + skill files được agent đọc qua tool. Hermes cần được "dạy" cùng nội dung **và** quy ước `[HW:/...]` marker.

Trong `EnsureOnboarding()`:
1. Concat `SOUL.md` + identity + danh sách marker syntax thành 1 string.
2. Gửi qua `instructions` field của `POST /v1/responses` mỗi turn, hoặc set vào hermes config làm system prompt mặc định.

Marker reference (Lumi parser ở `handler_hw.go:64-86`):
```
[HW:/led/solid:{"color":"red"}]
[HW:/scene:{"name":"sunset"}]
[HW:/emotion:{"type":"happy"}]
[HW:/servo/track:{"x":0.5,"y":0.5}]
[HW:/audio/play:{"file":"chime.mp3"}]
[HW:/buddy/exec/<action>:{...}]
[HW:/wellbeing/log:{...}]
[HW:/broadcast]  [HW:/speak]  [HW:/dm:{"telegram_id":"..."}]
```

---

## 13. Backwards compatibility

- `agent_backend` không set → mặc định `"openclaw"`, behavior cũ nguyên vẹn.
- Pi đã setup OpenClaw rồi → đổi config sang `"hermes"` + restart Lumi → openclaw daemon vẫn chạy nhưng Lumi không connect; tốn RAM nhưng không vỡ. Có thể `systemctl stop openclaw` tay.
- Web UI setup hiện đang ép OpenClaw flow. Cần thêm bước chọn backend hoặc giả định Hermes đã setup ngoài Lumi (giai đoạn 1).

---

## 14. Test plan giai đoạn 1

1. Build Lumi với hermes backend, chạy local Mac.
2. Mock Hermes server (Python `fastapi` ~100 LOC) trả SSE stub khi POST `/v1/responses`.
3. Verify:
   - WS handler nhận event với đúng `event`/`stream`.
   - `[HW:/emotion:...]` marker fire HTTP về LeLamp (mock).
   - Flow Monitor SSE hiển thị 1 turn từ start → tool → assistant → end.
4. Smoke test compact subprocess (mock binary `hermes` shell script `echo ok`).
5. Switch backend qua config, không rebuild — verify hot-reload via `runConfigChangeListener` (hoặc bỏ qua, yêu cầu restart).

---

## 15. Open questions — đã chuyển xuống §17/§18 cùng quyết định

---

## 16. Implementation order (đề xuất)

| Bước | Việc | Đầu ra |
|---|---|---|
| 1 | Tạo skeleton `internal/hermes/` với stub đầy đủ interface (return nil/empty) | Build pass, wire DI chạy |
| 2 | `client.go` — POST `/v1/responses` non-streaming, log raw response | Manual curl-equivalent từ Go |
| 3 | Switch sang `stream:true` + SSE parser, log từng event | Confirm parsing đúng spec |
| 4 | `sse_translator.go` — map sang `domain.WSEvent`, inject vào handler | Flow Monitor thấy 1 turn full |
| 5 | `SendChatMessageWithImage` | Voice + vision flow chạy |
| 6 | `EnsureOnboarding` + system prompt với `[HW:/...]` reference | Agent emit marker đúng |
| 7 | `StartSkillWatcher` reuse + Hermes load skill | Hot-reload skill |
| 8 | `CompactSession` subprocess | Long-session OK |
| 9 | `runs.go` copy/refactor | Guard run, broadcast, pose bucket hoạt động |
| 10 | Telegram receive trong Lumi (nếu cần channel) | Multi-channel parity |

---

## 17. Quyết định đã chốt

- [x] `hermes.md` ở repo root.
- [x] Backend selection qua `config.json` field `agent_backend: "openclaw" | "hermes"`.
- [x] Hermes chạy **local trên Pi** tại `http://127.0.0.1:8642`.
- [x] Telegram receive trong Lumi **làm ngay giai đoạn 1** — parity với OpenClaw UX.
- [x] `CompactSession()` **no-op** (Hermes chưa có CLI compact). Dùng `NewSession()` (đổi conversation name) làm workaround khi cần reset.
- [x] Chỉ dùng `conversation` name; **không** track `previous_response_id` chain. `FetchChatHistory()` trả empty hoặc cache local Lumi (xem §18).
z