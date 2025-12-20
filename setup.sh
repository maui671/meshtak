#!/bin/bash
set -e

APP_DIR="/opt/meshtak"
VENV_DIR="$APP_DIR/venv"
SERVICE_FILE="meshtak.service"

echo "[+] Detecting package manager..."

if command -v apt >/dev/null 2>&1; then
    PKG_MGR="apt"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
else
    echo "[-] Unsupported distro: neither apt nor dnf found"
    exit 1
fi

echo "[+] Using package manager: $PKG_MGR"

# -------------------------------
# Install system dependencies
# -------------------------------
if [ "$PKG_MGR" = "apt" ]; then
    apt update
    apt install -y \
        python3 \
        python3-venv \
        python3-pip \
        git \
        ca-certificates
else
    dnf install -y \
        python3 \
        python3-pip \
        git \
        ca-certificates

    # RHEL-based distros usually need this separately
    python3 -m ensurepip || true
fi

# -------------------------------
# Python virtual environment
# -------------------------------
echo "[+] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"

echo "[+] Activating virtual environment..."
source "$VENV_DIR/bin/activate"

echo "[+] Upgrading pip..."
pip install --upgrade pip

echo "[+] Installing Python dependencies..."
pip install \
    meshtastic \
    PyPubSub

# -------------------------------
# Permissions & service install
# -------------------------------
echo "[+] Making meshtak.py executable..."
chmod +x "$APP_DIR/meshtak.py"

echo "[+] Installing systemd service..."
cp "$APP_DIR/$SERVICE_FILE" /etc/systemd/system/meshtak.service

echo "[+] Reloading systemd..."
systemctl daemon-reexec
systemctl daemon-reload

echo "[+] Enabling service..."
systemctl enable meshtak.service

echo "[+] Starting service..."
systemctl restart meshtak.service

echo "[+] Installation complete."
echo "Use: journalctl -u meshtak -f"
