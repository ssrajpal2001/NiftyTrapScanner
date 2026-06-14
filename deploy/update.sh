#!/bin/bash
# Quick update + restart — run this every day before market opens
# Usage: bash deploy/update.sh
set -e

APP_DIR="/opt/trapscanner"
BRANCH="phase2/ltf-entry-engine"

echo "=== Pulling latest code ==="
cd "$APP_DIR"
git fetch origin
git pull origin "$BRANCH"

echo "=== Updating dependencies ==="
.venv/bin/pip install -r requirements.txt -q

echo "=== Restarting services ==="
sudo systemctl restart trapscanner-live
sudo systemctl restart trapscanner-phase2

echo "=== Status ==="
sudo systemctl status trapscanner-live  --no-pager -l
sudo systemctl status trapscanner-phase2 --no-pager -l

echo ""
echo "Live Tracker : http://$(curl -s ifconfig.me):8501"
echo "Phase 2 UI   : http://$(curl -s ifconfig.me):8502"
