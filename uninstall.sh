#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="meshtak"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
INSTALL_DIR="/opt/meshtak"

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
rm -f "${SERVICE_FILE}"
systemctl daemon-reload

rm -rf "${INSTALL_DIR}"

echo "MeshTAK removed."
