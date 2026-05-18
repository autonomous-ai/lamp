#!/usr/bin/env bash
# lumi-mode — switch Lumi device between production and developer mode.
#
# Production: nginx blocks all external access (web UI + API); LeLamp
#             enforces local-only middleware.
# Developer:  nginx allows LAN/Tailscale access; middleware disabled.
#
# Usage (run as root on the Pi):
#   sudo lumi-mode production
#   sudo lumi-mode developer
#   lumi-mode          # prints current mode

set -euo pipefail

LELAMP_ENV="/opt/lelamp/.env"
ACCESS_WEB="/etc/nginx/conf.d/lumi-access-web.conf"
ACCESS_API="/etc/nginx/conf.d/lumi-access-api.conf"
DENY_BLOCK="allow 127.0.0.1;\nallow ::1;\ndeny all;"

_current_mode() {
    grep -i "^LELAMP_MODE=" "$LELAMP_ENV" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || echo "production"
}

_set_lelamp_mode() {
    local mode="$1"
    if grep -q "^LELAMP_MODE=" "$LELAMP_ENV" 2>/dev/null; then
        sed -i "s/^LELAMP_MODE=.*/LELAMP_MODE=${mode}/" "$LELAMP_ENV"
    else
        echo "LELAMP_MODE=${mode}" >> "$LELAMP_ENV"
    fi
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

if [ "$MODE" = "production" ]; then
    printf "%b\n" "$DENY_BLOCK" > "$ACCESS_WEB"
    printf "%b\n" "$DENY_BLOCK" > "$ACCESS_API"
else
    # Empty files — nginx include of empty file = no restrictions.
    : > "$ACCESS_WEB"
    : > "$ACCESS_API"
fi

_set_lelamp_mode "$MODE"

nginx -t
nginx -s reload
systemctl restart lumi-lelamp

echo "lumi-mode: switched to $MODE"
