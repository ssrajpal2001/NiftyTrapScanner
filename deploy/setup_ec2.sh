#!/bin/bash
# =============================================================================
# setup_ec2.sh — One-time EC2 provisioning for NiftyTrapScanner
# Ubuntu 22.04 LTS
# Run as: bash deploy/setup_ec2.sh
# =============================================================================
set -e

APP_DIR="/opt/trapscanner"
REPO_URL="https://github.com/ssrajpal2001/NiftyTrapScanner.git"
BRANCH="phase2/ltf-entry-engine"
SERVICE_USER="ubuntu"

echo "=== [1/6] System update ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl

echo "=== [2/6] Clone / pull repo ==="
if [ -d "$APP_DIR" ]; then
    echo "Repo exists — pulling latest..."
    cd "$APP_DIR"
    sudo -u "$SERVICE_USER" git fetch origin
    sudo -u "$SERVICE_USER" git checkout "$BRANCH"
    sudo -u "$SERVICE_USER" git pull origin "$BRANCH"
else
    sudo git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
    sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
fi

echo "=== [3/6] Python venv + dependencies ==="
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "=== [4/6] Create directories ==="
mkdir -p "$APP_DIR/logs"
mkdir -p "$APP_DIR/.streamlit"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/logs"

echo "=== [5/6] Copy config (if not present) ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ">>> EDIT $APP_DIR/.env and add your UPSTOX_TOKEN <<<"
fi

echo "=== [6/6] Install systemd services ==="
sudo cp "$APP_DIR/deploy/trapscanner-live.service"  /etc/systemd/system/
sudo cp "$APP_DIR/deploy/trapscanner-phase2.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trapscanner-live
sudo systemctl enable trapscanner-phase2

echo ""
echo "============================================================"
echo " Setup complete!"
echo " 1. Edit /opt/trapscanner/.env  — add UPSTOX_TOKEN"
echo " 2. sudo systemctl start trapscanner-live"
echo " 3. sudo systemctl start trapscanner-phase2"
echo " 4. Live Tracker : http://<EC2-IP>:8501"
echo " 5. Phase 2 UI   : http://<EC2-IP>:8502"
echo "============================================================"
