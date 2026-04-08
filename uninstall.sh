#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=/opt/meshtak
CONFIG_DIR=/etc/meshtak
SERVICE_FILE=/etc/systemd/system/meshtak.service
ENV_FILE=/etc/default/meshtak
prompt_yes_no(){ local p="$1" d="${2:-n}" a; while true; do if [[ "$d" == y ]]; then read -r -p "$p [Y/n]: " a; a="${a:-y}"; else read -r -p "$p [y/N]: " a; a="${a:-n}"; fi; case "${a,,}" in y|yes) echo y; return;; n|no) echo n; return;; esac; done; }
[[ $EUID -eq 0 ]] || { echo 'Run as root'; exit 1; }
echo "This will remove /opt/meshtak, /etc/meshtak, and the meshtak service."
echo "It will NOT remove your source repo."
[[ "$(prompt_yes_no 'Continue with uninstall?' 'n')" == y ]] || exit 0
systemctl stop meshtak.service 2>/dev/null || true
systemctl disable meshtak.service 2>/dev/null || true
rm -f "$SERVICE_FILE" "$ENV_FILE"
systemctl daemon-reload
rm -rf "$APP_DIR" "$CONFIG_DIR"
echo 'MeshTAK runtime removed.'
