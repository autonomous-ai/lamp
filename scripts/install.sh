#!/bin/bash
# Run this on the Pi to download and execute the latest setup from CDN.
# Usage: curl -fsSL https://storage.googleapis.com/s3-autonomous-upgrade-3/lumi/install.sh | sudo bash
set -euo pipefail
curl -fsSL -H "Cache-Control: no-cache" -H "Pragma: no-cache" \
  -o /tmp/setup.sh \
  "https://cdn.autonomous.ai/lumi/setup.sh"
chmod +x /tmp/setup.sh
bash /tmp/setup.sh

# Install lumi-mode — switch between production and developer mode.
# Embedded here so install.sh is the single entry point and no sibling
# files are needed when setup.sh is fetched from CDN.
cat > /usr/local/bin/lumi-mode << 'LUMIMODE'
#!/usr/bin/env bash
# lumi-mode — switch Lumi between production and developer mode.
#
# Usage (run as root on the Pi):
#   sudo lumi-mode production
#   sudo lumi-mode developer
#   lumi-mode              # print current mode

set -euo pipefail

LELAMP_ENV="/opt/lelamp/.env"
NGINX_CONF="/etc/nginx/conf.d/lumi.conf"

_current_mode() {
    grep -i "^LELAMP_MODE=" "$LELAMP_ENV" 2>/dev/null \
        | cut -d= -f2 | tr -d '[:space:]' \
        || echo "production"
}

_set_env_mode() {
    local mode="$1"
    if grep -q "^LELAMP_MODE=" "$LELAMP_ENV" 2>/dev/null; then
        sed -i "s/^LELAMP_MODE=.*/LELAMP_MODE=${mode}/" "$LELAMP_ENV"
    else
        echo "LELAMP_MODE=${mode}" >> "$LELAMP_ENV"
    fi
}

_nginx_set_web_block() {
    local mode="$1"
    python3 - "$NGINX_CONF" "$mode" <<'PYEOF'
import sys

path, mode = sys.argv[1], sys.argv[2]
with open(path) as f:
    content = f.read()

OPEN   = "  location / {\n    try_files $uri /index.html;\n  }"
CLOSED = "  location / {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n    try_files $uri /index.html;\n  }"

if mode == "production":
    content = content.replace(OPEN, CLOSED)
else:
    content = content.replace(CLOSED, OPEN)

with open(path, "w") as f:
    f.write(content)
PYEOF
}

MODE="${1:-}"

if [ -z "$MODE" ]; then
    echo "current mode: $(_current_mode)"
    exit 0
fi

if [ "$MODE" != "production" ] && [ "$MODE" != "developer" ]; then
    echo "Usage: lumi-mode <production|developer>"
    exit 1
fi

if [ "$(id -u)" != "0" ]; then
    echo "error: must run as root (sudo lumi-mode $MODE)"
    exit 1
fi

_set_env_mode "$MODE"
_nginx_set_web_block "$MODE"

nginx -t
nginx -s reload
systemctl restart lumi-lelamp lumi

echo "lumi-mode: switched to $MODE"
LUMIMODE
chmod +x /usr/local/bin/lumi-mode
