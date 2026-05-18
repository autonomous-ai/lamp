# Security Test Checklist

Branch: `feat/security` — PR #69

Pi IP (Tailscale): `100.119.50.21`  
Run these from any machine on the team that has Tailscale access.

---

## Results legend

| Symbol | Meaning |
|--------|---------|
| ✅ PASS | Response matches expected — secure |
| ❌ FAIL | Response does NOT match — something is wrong |

---

## F1 — LeLamp direct port blocked (not exposed to LAN)

LeLamp binds to `127.0.0.1:5001`, never reachable directly from outside.

```bash
curl -v --connect-timeout 5 http://100.119.50.21:5001/hw/health
```

**Expected:** `curl: (7) Failed to connect` or connection timeout  
**FAIL if:** returns any HTTP response

---

## F2 — nginx `/hw/` accessible in developer mode

`LELAMP_MODE=developer` → middleware disabled, nginx does NOT block `/hw/`.

```bash
curl -s -o /dev/null -w "%{http_code}" http://100.119.50.21/hw/health
```

**Expected:** `200`  
**FAIL if:** returns `403` or connection refused

---

## F3 — LeLamp local-only middleware (production mode simulation)

In production mode, any non-localhost caller gets 403 from Python middleware.  
To test without changing the Pi, hit `/hw/` and check response header — in developer mode it passes through.

```bash
curl -s http://100.119.50.21/hw/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('status:', d.get('status'))"
```

**Expected:** `status: 1` (request passes through in developer mode)  
**Note:** Switch `LELAMP_MODE=production` in Pi `.env` + restart to test the block path

---

## F5a — `POST /api/system/exec` blocked (RCE endpoint)

This endpoint must NEVER be reachable from outside localhost.

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://100.119.50.21/api/system/exec \
  -H "Content-Type: application/json" \
  -d '{"cmd":"ls"}'
```

**Expected:** `403`  
**FAIL if:** returns `200` or any other non-403 (means RCE is open 💀)

---

## F5b — `WS /api/system/shell` reachable (intentional — Web UI terminal)

Shell WebSocket must still be reachable from LAN for the xterm.js terminal in the Web UI.

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Upgrade: websocket" \
  -H "Connection: Upgrade" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  http://100.119.50.21/api/system/shell
```

**Expected:** `101` (WebSocket upgrade accepted)  
**FAIL if:** returns `403` (would break the Web UI terminal)

---

## F6 — `/gw/` OpenClaw UI loads but WebSocket requires HTTPS

UI HTML loads, but WebSocket connection requires a secure context (HTTPS or localhost).

```bash
curl -s -o /dev/null -w "%{http_code}" http://100.119.50.21/gw/
```

**Expected:** `200` (HTML loads)  
**Note:** Connecting via browser over HTTP will show "requires HTTPS or localhost secure context" — this is expected browser security behavior, not a bug.

---

## F7 — DL Backend requires API key (RunPod only)

Replace `<POD_ID>` with the actual RunPod pod ID.

```bash
# Without key — must be rejected
curl -s -o /dev/null -w "%{http_code}" \
  https://<POD_ID>-8888.proxy.runpod.net/api/dl/health

# With correct key — must work
curl -s -o /dev/null -w "%{http_code}" \
  -H "X-API-Key: <your-DL_API_KEY>" \
  https://<POD_ID>-8888.proxy.runpod.net/api/dl/health
```

**Expected (no key):** `401`  
**Expected (with key):** `200`  
**FAIL if:** no-key request returns `200` (means DL_API_KEY was not set at startup)

---

## Quick all-in-one script

Copy-paste to run all Pi tests at once:

```bash
PI=100.119.50.21

echo "=== F1: LeLamp direct port ==="
curl -v --connect-timeout 5 http://$PI:5001/hw/health 2>&1 | grep -E "connect|HTTP" | head -3

echo ""
echo "=== F2: /hw/ accessible (developer mode) ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' http://$PI/hw/health) — expect 200"

echo ""
echo "=== F5a: /api/system/exec blocked ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' -X POST http://$PI/api/system/exec -H 'Content-Type: application/json' -d '{"cmd":"ls"}') — expect 403"

echo ""
echo "=== F5b: /api/system/shell WebSocket reachable ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' -H 'Upgrade: websocket' -H 'Connection: Upgrade' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H 'Sec-WebSocket-Version: 13' http://$PI/api/system/shell) — expect 101"

echo ""
echo "=== F6: /gw/ loads ==="
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' http://$PI/gw/) — expect 200"
```
