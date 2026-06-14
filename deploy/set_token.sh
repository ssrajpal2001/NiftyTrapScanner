#!/bin/bash
# Update the daily Upstox token and restart services
# Usage: bash deploy/set_token.sh "eyJ0eX..."

TOKEN="$1"
if [ -z "$TOKEN" ]; then
    echo "Usage: bash deploy/set_token.sh <your_token>"
    exit 1
fi

ENV_FILE="/opt/trapscanner/.env"

# Replace or add UPSTOX_TOKEN in .env
if grep -q "^UPSTOX_TOKEN=" "$ENV_FILE"; then
    sed -i "s|^UPSTOX_TOKEN=.*|UPSTOX_TOKEN=$TOKEN|" "$ENV_FILE"
else
    echo "UPSTOX_TOKEN=$TOKEN" >> "$ENV_FILE"
fi

echo "Token updated in $ENV_FILE"

# Restart services to pick up new token
sudo systemctl restart trapscanner-live
sudo systemctl restart trapscanner-phase2

echo "Services restarted. Token is live."
