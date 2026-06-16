#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Hetzner CX22 — NiftyTrapScanner one-shot setup script
#
# Run as root on a fresh Ubuntu 24.04 server:
#   curl -fsSL https://raw.githubusercontent.com/ssrajpal2001/NiftyTrapScanner/phase2/ltf-entry-engine/deploy/hetzner_setup.sh | bash
#
# Or copy the file and run:
#   chmod +x hetzner_setup.sh && bash hetzner_setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="https://github.com/ssrajpal2001/NiftyTrapScanner.git"
REPO_BRANCH="phase2/ltf-entry-engine"
APP_DIR="/opt/trapscanner"
APP_USER="trapscanner"
SERVICE_NAME="trapscanner"
PORT=8501

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║        NiftyTrapScanner — Hetzner Setup                 ║"
echo "║  Repo : ssrajpal2001/NiftyTrapScanner                   ║"
echo "║  Branch: phase2/ltf-entry-engine                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "▶ [1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git curl ufw \
    libssl-dev libffi-dev

# ── 2. Create dedicated user ──────────────────────────────────────────────────
echo "▶ [2/7] Creating app user '$APP_USER'..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

# ── 3. Clone / update repo ────────────────────────────────────────────────────
echo "▶ [3/7] Cloning repo ($REPO_BRANCH)..."
if [ -d "$APP_DIR/.git" ]; then
    echo "  Repo exists — pulling latest..."
    git -C "$APP_DIR" fetch origin
    git -C "$APP_DIR" checkout "$REPO_BRANCH"
    git -C "$APP_DIR" pull origin "$REPO_BRANCH"
else
    git clone --branch "$REPO_BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 4. Python venv + dependencies ────────────────────────────────────────────
echo "▶ [4/7] Creating virtual environment and installing packages..."
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ── 5. Create .env file ───────────────────────────────────────────────────────
echo "▶ [5/7] Setting up .env file..."
ENV_FILE="$APP_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'ENVEOF'
# ── Upstox ───────────────────────────────────────────────────────────────────
# Refresh daily before market open (9:00 AM IST)
UPSTOX_TOKEN=

# ── Angel One SmartAPI ───────────────────────────────────────────────────────
# Regenerate API key from: https://smartapi.angelbroking.com/
ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=
ENVEOF
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  Created $ENV_FILE — fill in your credentials before starting."
else
    echo "  .env already exists — skipping (credentials preserved)."
fi

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "▶ [6/7] Installing systemd service '$SERVICE_NAME'..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SVCEOF
[Unit]
Description=NiftyTrapScanner Live Tracker (Streamlit)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/streamlit run live_tracker.py \\
    --server.port ${PORT} \\
    --server.address 0.0.0.0 \\
    --server.headless true \\
    --server.fileWatcherType none \\
    --browser.gatherUsageStats false
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 7. Firewall (UFW) ─────────────────────────────────────────────────────────
echo "▶ [7/7] Configuring firewall..."
ufw allow ssh        comment "SSH"
ufw allow "$PORT"/tcp comment "Streamlit"
ufw --force enable

# ── Done ──────────────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║                    SETUP COMPLETE                       ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  App directory : $APP_DIR"
echo "  Config file   : $ENV_FILE"
echo "  Service       : systemctl {start|stop|restart|status} $SERVICE_NAME"
echo "  Logs          : journalctl -u $SERVICE_NAME -f"
echo "  URL           : http://${SERVER_IP}:${PORT}"
echo ""
echo "  ── NEXT STEPS ──────────────────────────────────────────"
echo "  1. Fill in credentials:"
echo "     nano $ENV_FILE"
echo ""
echo "  2. Start the app:"
echo "     systemctl start $SERVICE_NAME"
echo ""
echo "  3. Check it's running:"
echo "     systemctl status $SERVICE_NAME"
echo "     curl -s http://localhost:$PORT | head -5"
echo ""
echo "  4. Open in browser:"
echo "     http://${SERVER_IP}:${PORT}"
echo ""
echo "  ── DAILY TOKEN UPDATE ──────────────────────────────────"
echo "  Every morning before 9:00 AM, update Upstox token:"
echo "     sed -i 's/^UPSTOX_TOKEN=.*/UPSTOX_TOKEN=YOUR_NEW_TOKEN/' $ENV_FILE"
echo "     systemctl restart $SERVICE_NAME"
echo ""
