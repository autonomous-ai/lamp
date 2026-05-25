# Tổng quan hệ thống

Hệ thống là đèn AI thông minh phân tán: Pi đóng vai trò device, RunPod chạy GPU, Mac/Claude Desktop là
  endpoint của người dùng. Dưới đây là chức năng từng phần:

  ---
  1. dlbackend/ — Deep Learning Backend (Python, FastAPI)
  
  Vai trò: Lớp perception GPU-accelerated, deploy trên RunPod/GPU server.

  - Stack: Python 3.10+, FastAPI, ONNX Runtime, PyTorch, opencv, numpy
  - Entry: src/server.py, config qua Pydantic ở src/config.py
  - Module chính (trong src/core/):
    - action/ — Action recognition từ video (walking, reading, typing…)
    - emotion/ — 7 cảm xúc RAF-DB từ face crop
    - ser/ — Speech Emotion Recognition (angry/happy/sad từ WAV)
    - audio_recognition/ — Speaker enroll & voice biometrics
    - faces/, persondetector/, perception/ — Phát hiện người, face embedding
    - crypto/ — RSA + AES-256-GCM cho lbserver/ (load balancer encryption proxy)
  - API: POST /api/dl/emotion-recognize, /api/dl/ser/recognize, WS /api/dl/action-analysis/ws,
  /api/dl/audio-recognizer/*
  - Luồng: LeLamp gửi frames/audio mỗi ~2s → dlbackend trả class + confidence → Lumi định tuyến.

  ---
  2. lumi/ — Lumi Server (Go, Gin) — Bộ điều phối trung tâm
  
  Vai trò: HTTP API server chính chạy trên Pi (port :5000), orchestrator giữa OpenClaw ↔ LeLamp ↔ dlbackend.

  - Stack: Go 1.24, Gin, Gorilla WebSocket, PAHO MQTT, Google Wire DI, go-gpiocdev
  - Entry: cmd/lamp/main.go (API), cmd/bootstrap/main.go (OTA worker)
  - Module nội bộ (internal/):
    - agent/, openclaw/ — OpenClaw WS gateway client
    - device/, network/ — WiFi/LLM/MQTT setup, first-boot
    - sensing/, intent/ — Nhận perception event, match local intent (~50ms) hoặc forward lên LLM
    - buddy/ — BLE pairing với claude-desktop-buddy
    - ambient/, monitor/ — Habit tracking, presence prediction
    - statusled/, resetbutton/ — GPIO feedback (LED, nút reset 10s = factory reset)
    - ota/, beclient/ — Bootstrap OTA + backend client
  - API tiêu biểu: POST /api/device/setup, POST /api/sensing/event, WS /api/openclaw/*, POST /api/guard/*, GET 
  /api/system/*
  - Response format: {status, data, message} chuẩn (xem server/serializers/)

  ---
  3. lumi-buddy/ — Companion app macOS (Swift) cho remote computer use
  
  Vai trò: App menubar trên Mac của user, kết nối tới Lumi lamp qua WiFi để thực thi lệnh trên máy Mac.

  - Stack: Swift 5.9 + SPM, native macOS 13+, zero dependencies
  - Cấu trúc: macos/ (Swift app), mock-lamp/ (Go mock server), docs/, Makefile
  - Phases:
    - 1A — Scaffold menubar (NSStatusItem, no Dock icon) — hiện tại
    - 1B — mDNS discovery tìm lamp trên LAN
    - 1C — 6-digit pairing, lưu token Keychain
    - 1D — WebSocket persistent client + exponential backoff reconnect
    - 1E — Command executors: open_app, close_app, open_url, type_text, key_combo, notification, ping
  - Bảo mật: Accessibility prompt, audit log tại ~/Library/Application Support/LumiBuddy/audit.log
  - Ví dụ flow: User nói với đèn "Mở Chrome vào Gmail" → OpenClaw skill → Lumi WS → buddy thực thi open_app +
  open_url trên Mac.

  ---
  4. claude-desktop-buddy/ — BLE bridge giữa Claude Desktop và Lumi
  
  Vai trò: Service Go chạy trên Pi (port :5002), kết nối Bluetooth LE tới Claude Desktop trên Mac. Khác với
  lumi-buddy: cái này chạy ở device, dùng BLE thay vì WiFi.
  
  - Stack: Go, BlueZ DBus (BLE server), HTTP server (cho OpenClaw skill)
  - File chính:
    - main.go, protocol.go — Heartbeat/Event/Command/TimeSync codecs
    - ble.go — BlueZ DBus, advertising, Nordic UART service
    - state.go — FSM 6 trạng thái: sleep/idle/busy/attention/heart/celebrate
    - bridge.go — Relay state → LeLamp (LED effect, emotion, TTS)
    - narrator.go — TTS đa ngôn ngữ (i18n: en, vi)
    - agent.go — Xử lý approve/deny tool call (timeout 30s)
    - transfer.go — Folder-push chunked qua BLE
    - httpserver.go — API cho OpenClaw skill: GET /status, POST /approve, POST /deny
  - Version: v1.0.45 (production)
  - Ví dụ flow: Claude Desktop chờ user approve tool call → buddy → LED chớp cam + TTS "chờ phê duyệt" → user
  nói "approve" → Lumi voice → POST /approve → BLE → Claude Desktop tiếp tục.
  
  ---
  5. hardware/ — Tài liệu phần cứng (không phải code)

  Đây là schematic & BOM, không có firmware. Chứa:
  - components.md — BOM: Pi 5 / OrangePi 4 Pro, 5× servo STS3215, vòng LED WS2812 64-pixel, camera USB, 2 mic, 4
   touch pad TTP223, amp PAM8610, loa 3W 
  - wiring.md — Sơ đồ pin-by-pin (source of truth, ~256 dòng) — khớp với code lelamp/service/
  - power.md — Rail 12V/5A → buck MP2482 → 5V, tổng ~60W
  - assembly.md — Hướng dẫn lắp
  - cad/ — STEP/STL (gitignored, lưu Mega.nz)

  ---
  6. imager/ — Trình build OS image cho Pi 5

  Docker-based golden image builder, sản phẩm: file .img 8GB flash ra SD card.

  - Dockerfile — Ubuntu 24.04 + btrfs-progs + qemu-user-static
  - build.sh — Script chính, 2 phase:
    - BASE (~20 phút, cached): Tải Raspberry Pi OS Lite arm64, format Btrfs subvolumes @ và @factory, cài
  SSH/WiFi/wpa_supplicant, tạo user system/12345, cài services (bootstrap, lumi, lelamp, openclaw), PulseAudio
  AEC, Node.js 22
    - OVERLAY (~1 phút, lặp nhanh): Copy base.img, tải binary từ GCS, sync Python app với uv
  - Makefile — Targets: make build, make flash, make check-sd
  - Boot flow: kernel → firstrun-wifi → btrfs-resize-once → device-ap-mode → nginx serve setup UI tại

  - Dockerfile — Ubuntu 24.04 + btrfs-progs + qemu-user-static
  - build.sh — Script chính, 2 phase:
    - BASE (~20 phút, cached): Tải Raspberry Pi OS Lite arm64, format Btrfs subvolumes @ và @factory, cài
  SSH/WiFi/wpa_supplicant, tạo user system/12345, cài services (bootstrap, lumi, lelamp, openclaw), PulseAudio
  AEC, Node.js 22
    - OVERLAY (~1 phút, lặp nhanh): Copy base.img, tải binary từ GCS, sync Python app với uv
  - Makefile — Targets: make build, make flash, make check-sd
  - Boot flow: kernel → firstrun-wifi → btrfs-resize-once → device-ap-mode → nginx serve setup UI tại
  192.168.100.1
  - Factory reset: fr-snapshot (snapshot @factory), fr-rollback (revert)

  - build.sh — Script chính, 2 phase:
    - BASE (~20 phút, cached): Tải Raspberry Pi OS Lite arm64, format Btrfs subvolumes @ và @factory, cài
  SSH/WiFi/wpa_supplicant, tạo user system/12345, cài services (bootstrap, lumi, lelamp, openclaw), PulseAudio
  AEC, Node.js 22
    - OVERLAY (~1 phút, lặp nhanh): Copy base.img, tải binary từ GCS, sync Python app với uv
  - Makefile — Targets: make build, make flash, make check-sd
  - Boot flow: kernel → firstrun-wifi → btrfs-resize-once → device-ap-mode → nginx serve setup UI tại
  192.168.100.1
  - Factory reset: fr-snapshot (snapshot @factory), fr-rollback (revert)

    - OVERLAY (~1 phút, lặp nhanh): Copy base.img, tải binary từ GCS, sync Python app với uv
  - Makefile — Targets: make build, make flash, make check-sd
  - Boot flow: kernel → firstrun-wifi → btrfs-resize-once → device-ap-mode → nginx serve setup UI tại
  192.168.100.1
  - Factory reset: fr-snapshot (snapshot @factory), fr-rollback (revert)

  192.168.100.1
  - Factory reset: fr-snapshot (snapshot @factory), fr-rollback (revert)


  ---
  7. scripts/ — CI/CD & deployment (21 scripts)

  Tự động hoá build → push GCS → Pi OTA tự cập nhật.

  ┌─────────────┬─────────────────────────────────────────────────────────────┬─────────────────────────────┐
  │    Nhóm     │                      Script tiêu biểu                       │          Mục đích           │
  ├─────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────┤
  │             │                                                             │ Cài systemd services,       │
  │ Setup OS    │ setup.sh (~1300 dòng), setup-ap.sh                          │ hostapd/dnsmasq, nginx      │
  │             │                                                             │ proxy                       │
  ├─────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────┤
  │ Install     │ install.sh, install-openclaw.sh                             │ Wrapper tải về từ CDN       │
  ├─────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────┤
  │ Upload      │ upload-lumi.sh, upload-lelamp.sh, upload-openclaw.sh,       │ Auto-bump version, zip,     │
  │ binaries →  │ upload-web.sh, upload-skills.sh                             │ push GCS, cập nhật          │
  │ GCS         │                                                             │ metadata.json               │
  ├─────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────┤
  │ Upload      │ upload-bootstrap.sh, upload-hooks.sh, upload-setup.sh,      │ Bootstrap, git hooks, setup │
  │ infra       │ upload-cad.sh                                               │  scripts                    │
  ├─────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────┤
  │ Patch /     │ patch-nginx-gw.sh, patch-nginx-hw-timeout.sh,               │ Tweak nginx, hardening,     │
  │ Config      │ patch-security.sh, add-network.sh,                          │ WiFi, migrate paths         │
  │             │ setup-claude-desktop-buddy.sh, migrate-openclaw-path.sh     │                             │
  └─────────────┴─────────────────────────────────────────────────────────────┴─────────────────────────────┘

  OTA cycle: Sửa code → ./scripts/upload-lumi.sh → metadata.json cập nhật trên GCS → bootstrap-worker trên Pi
  phát hiện version mới → pull binary → restart service.

  ---
  Tóm tắt kiến trúc

  [User Mac]                           [Lumi Device / Pi]                       [Cloud]
  ─────────                            ──────────────────                       ───────
  lumi-buddy (Swift, WiFi WS) ───►  lumi/ (Go :5000)  ◄──── MQTT/HTTP ──── GCS OTA
  Claude Desktop ─ BLE ─►  claude-desktop-buddy (Go :5002, BLE)              metadata.json
                                         │
                                         ▼ HTTP
                                    lelamp/ (Python, hardware drivers :5001)
                                         │
                                         ▼ HTTP/WS
                                    dlbackend/ (RunPod GPU, Python FastAPI)
  
  hardware/ = schematic; imager/ = OS builder; scripts/ = build+OTA pipeline.


# Openclaw
 Tin tốt: hệ thống đã có interface trừu tượng domain.AgentGateway, nên có thể "swap implementation" mà không
  phải viết lại toàn bộ. 
 
  ---
  A. Folder CHÍNH cần sửa

  lumi/internal/openclaw/ — Đây là folder chính

  Đây là implementation cụ thể của agent OpenClaw, 17 file Go. Bạn có 2 hướng:
  - Cách 1 (sạch): Tạo folder mới lumi/internal/<agent-mới>/ implement đầy đủ interface domain.AgentGateway, giữ
   nguyên openclaw/ để fallback
  - Cách 2 (gọn): Rename internal/openclaw/ → internal/agent-adapter/ và viết lại nội bộ

  Các file lõi trong folder này:

  ┌─────────────────────────────────────────┬────────────────────────────────────────────────────────┐
  │                  File                   │                       Chức năng                        │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service.go                              │ Struct Service — implementation chính của AgentGateway │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_ws.go                           │ WebSocket client + reconnect tới agent gateway         │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_chat.go                         │ SendChatMessage, SendChatMessageWithImage              │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_events.go                       │ Parse event stream (lifecycle/tool/assistant/thinking) │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_setup.go                        │ Đọc/ghi openclaw.json config                           │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_telegram.go, telegram_sender.go │ Tích hợp Telegram                                      │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_lelamp.go                       │ Gọi TTS/command sang LeLamp                            │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ service_identity.go                     │ Watch IDENTITY.md cho wake word                        │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ onboarding.go                           │ Seed SOUL.md, tải skills từ CDN                        │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ skill_watcher.go                        │ Poll OTA metadata skill version                        │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ sync.go, tunables.go                    │ Sync model list, tuning                                │
  ├─────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ wire.go                                 │ Wire DI provider                                       │
  └─────────────────────────────────────────┴────────────────────────────────────────────────────────┘

  lumi/domain/openclaw.go — Định nghĩa event format
  
  Các struct WSEvent, AgentPayload, ChatPayload, TokenUsage + helper ToolName(), ToolArguments(), ResultText()
  đang khớp format wire của OpenClaw. Nếu agent mới có schema khác → cần adapter dịch event sang struct này,
  hoặc viết lại file này.

  lumi/domain/agent.go — Interface chính (giữ nguyên)

  ~60 method định nghĩa contract giữa server và agent. Agent mới chỉ cần implement đủ interface.

  lumi/internal/agent/factory.go — Điểm switch agent
  
  Hiện tại:
  switch cfg.AgentRuntime {
  default:
      return openclaw.ProvideService(...)
  }   
  Chỉ cần thêm case mới ("my-agent": return myagent.ProvideService(...)) và đổi default. Đây là điểm chuyển đổi
  gọn nhất.

  ---
  B. File phụ thuộc cần cập nhật (liệt kê đầy đủ)

  1. Config & runtime (lumi/)

  - lumi/server/config/config.go — field AgentRuntime (line ~63), OpenclawConfigDir (line ~66, default
  /root/.openclaw), migration code line ~190-200
  - config/config.json — agent_runtime, openclaw_config_dir, telegram_bot_token
  - lumi/server/server.go line ~979-1102 — log source openclaw, openclaw-service, resolveOpenclawLog(), fallback
   /tmp/openclaw/

  2. HTTP handlers (lumi/server/agent/delivery/http/)
  
  Phần lớn đã dùng interface, nhưng vài file phụ thuộc format event:
  - handler.go — orchestrator (event routing, TTS buffer, runID map)
  - handler_events.go — parse stream theo agentPayload struct (phụ thuộc format)
  - handler_text.go — POST /api/agent/text
  - handler_hw.go — POST /api/agent/hw
  - handler_api_compaction.go, handler_api_flow.go, handler_api_monitor.go — endpoint phụ

  3. Resources & skills (lumi/resources/, lumi/internal/openclaw/resources/)

  - lumi/resources/openclaw-skills/ — 22 skills (camera, display, guard, habit, led-control, mood, music, scene,
   sensing, servo-control, voice, wellbeing…). Mỗi skill có SKILL.md + handlers. Plain markdown, agent mới có
  thể tái sử dụng nếu hiểu được skill prompt.
  - lumi/resources/openclaw-hooks/ — emotion-acknowledge/, turn-gate/. Hook trên message:preprocessed — chỉ giữ
  nếu agent mới có hook concept.
  - lumi/internal/openclaw/resources/SOUL.md — personality, embed qua //go:embed. Dùng lại tốt.

  4. LeLamp (Python)  

  - lelamp/app_state.py:
    - line ~88: LELAMP_SNAPSHOT_DIR = "/root/.openclaw/media/lumi-snapshots"
    - line ~100: OPENCLAW_WORKSPACE = "/root/.openclaw/workspace"

  5. Scripts (scripts/)
  
  - scripts/install-openclaw.sh — cài Node.js 22, npm install -g openclaw@latest, tạo /root/.openclaw, viết
  /etc/systemd/system/openclaw.service. Phải thay hoàn toàn.
  - scripts/upload-openclaw.sh — upload skills lên workspace agent.
  - scripts/migrate-openclaw-path.sh — có thể xoá.
  - scripts/setup.sh line ~571-681 — gọi install openclaw + viết upstream nginx.
  - scripts/patch-nginx-gw.sh line ~17-27 — upstream openclaw { server 127.0.0.1:18789; }.
  - scripts/upload-skills.sh — đường dẫn upload skill OTA.

  6. Imager / OS image (imager/)

  - imager/build.sh:
    - line ~46-48: stage stage_nodejs_openclaw
    - line ~944-955: nginx proxy /gw/ → :18789, upstream openclaw { server 127.0.0.1:18789; }

  7. Web UI (lumi/web/src/pages/monitor/)

  - GwConfig.tsx line ~18-41 — reference openclaw.json
  - LogsSection.tsx line ~6-11, 341, 346 — log dropdown default "openclaw"
  - TurnBadge.tsx line ~149, 266 — label "openclaw uuid"
  - FlowDiagram.tsx line ~430, 477, 547, 602 — pipeline label
  - ChatSection.tsx line ~494, 912, 1921 — flow docs reference
  - index.tsx line ~238 — polling status

  8. claude-desktop-buddy (Go)

  - claude-desktop-buddy/skill/SKILL.md — skill này được OpenClaw đọc; nếu agent mới đọc được format SKILL.md
  thì giữ, không thì viết lại.
  - claude-desktop-buddy/httpserver.go — GET /status, POST /approve, POST /deny đang được OpenClaw skill curl.
  Endpoint vẫn dùng được, chỉ cần agent mới biết URL.
  
  9. lumi-buddy (Swift)

  Hầu như không phụ thuộc OpenClaw — chỉ gọi WebSocket Lumi. Không phải sửa trừ khi tên route đổi.
  
  ---
  C. Documentation cần update
  
  Tất cả đều có 2 phiên bản (en + vi):
  - CLAUDE.md — bảng mapping doc, tên agent
  - README.md — overview
  - docs/openclaw-compaction.md + docs/vi/...
  - docs/debug/openclaw-selfreplay.md
  - docs/overview.md, docs/lamp-server.md, docs/flow-monitor.md — kiến trúc, sequence diagram
  - docs/mqtt.md — dispatch flow
  - Bất cứ chỗ nào nhắc :18789, /root/.openclaw, openclaw.json, openclaw.service

  ---
  D. Đánh giá Abstraction (mức độ dễ swap)
 
  ┌─────────────────────────────────────────────────────────┬───────────────────────────────────────────────┐
  │                        Khía cạnh                        │                  Trạng thái                   │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ Interface AgentGateway                                  │ ✅ Tốt — 60 method, đã abstracted             │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ HTTP handler layer                                      │ ✅ Hầu hết qua interface                      │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ Factory pattern                                         │ ✅ Có sẵn switch theo cfg.AgentRuntime        │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ Event format (WSEvent/AgentPayload)                     │ ❌ Hardcoded OpenClaw — cần adapter           │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ Config file format (openclaw.json)                      │ ❌ Hardcoded — cần migrate                    │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ Skill structure                                         │ ⚠️  Plain markdown OK, path coupled            │
  │ (~/.openclaw/workspace/skills/*/SKILL.md)               │                                               │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ Telegram/TTS built-in                                   │ ⚠️  Nếu agent mới không có thì phải tự         │
  │                                                         │ implement                                     │
  ├─────────────────────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ nginx upstream + systemd                                │ ❌ Hardcoded port :18789, service name        │
  └─────────────────────────────────────────────────────────┴───────────────────────────────────────────────┘
  
  ---
  E. Lộ trình đề xuất (theo thứ tự)
  
  1. Tạo package mới: lumi/internal/myagent/ implement domain.AgentGateway (copy structure từ openclaw/)
  2. Viết adapter event trong myagent/events.go: parse format mới → domain.WSEvent/AgentPayload
  3. Thêm case factory: lumi/internal/agent/factory.go switch theo AgentRuntime
  4. Generalize config: đổi OpenclawConfigDir → AgentWorkspaceDir, thêm field AgentPort
  5. Viết install script mới: scripts/install-myagent.sh, systemd unit, nginx upstream
  6. Update imager build.sh: stage cài agent mới + nginx upstream
  7. Migrate skills: copy resources/openclaw-skills/ → format mới (giữ SOUL.md/SKILL.md nếu được)
  8. Update Web UI labels: monitor/*.tsx (tìm-thay openclaw → agent)
  9. Cập nhật LeLamp paths trong lelamp/app_state.py
  10. Update docs (en + vi) theo bảng mapping CLAUDE.md

  Độ khó tổng thể: ~7/10. Khó vì event format và Telegram tightly-coupled, dễ vì interface đã có sẵn.