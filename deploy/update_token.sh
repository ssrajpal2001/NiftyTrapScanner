#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# update_token.sh — Update Upstox token and restart the app
# Run every morning before 9:00 AM IST:
#   bash /opt/trapscanner/deploy/update_token.sh YOUR_NEW_TOKEN
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV_FILE="/opt/trapscanner/.env"
SERVICE="trapscanner"

if [ $# -eq 0 ]; then
    echo "Usage: $0 <new_upstox_token>"
    echo "Example: $0 eyJ0eXAiOiJKV1Qi..."
    exit 1
fi

NEW_TOKEN="$1"

# Validate token looks like a JWT (starts with eyJ)
if [[ "$NEW_TOKEN" != eyJ* ]]; then
    echo "WARNING: Token doesn't look like a JWT (expected to start with 'eyJ'). Continuing anyway..."
fi

echo "Updating UPSTOX_TOKEN in $ENV_FILE..."
if grep -q "^UPSTOX_TOKEN=" "$ENV_FILE"; then
    sed -i "s|^UPSTOX_TOKEN=.*|UPSTOX_TOKEN=${NEW_TOKEN}|" "$ENV_FILE"
else
    echo "UPSTOX_TOKEN=${NEW_TOKEN}" >> "$ENV_FILE"
fi

echo "Restarting $SERVICE..."
systemctl restart "$SERVICE"

sleep 3
STATUS=$(systemctl is-active "$SERVICE" 2>/dev/null || echo "unknown")
echo "Service status: $STATUS"

if [ "$STATUS" = "active" ]; then
    echo "✓ Token updated and app restarted successfully."
else
    echo "✗ App may not have started — check: journalctl -u $SERVICE -n 30"
fi
