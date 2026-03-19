#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/meshtak"
SERVICE_NAME="meshtak.service"
SYSTEMD_DIR="/etc/systemd/system"
PRESERVE_DIR="/home/tdcadmin/meshtak"

log() {
  echo "[+] $*"
}

warn() {
  echo "[!] $*"
}

fail() {
  echo "[-] $*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    fail "Run this script as root or with sudo."
  fi
}

remove_service() {
  if systemctl list-unit-files | grep -q "^${SERVICE_NAME}"; then
    log "Stopping and disabling service..."
    systemctl stop "${SERVICE_NAME}" || true
    systemctl disable "${SERVICE_NAME}" || true
  fi

  if [[ -f "${SYSTEMD_DIR}/${SERVICE_NAME}" ]]; then
    log "Removing systemd unit file..."
    rm -f "${SYSTEMD_DIR}/${SERVICE_NAME}"
    systemctl daemon-reload
  fi
}

remove_app_dir() {
  if [[ -d "${APP_DIR}" ]]; then
    log "Removing application directory ${APP_DIR}..."
    rm -rf "${APP_DIR}"
  fi
}

verify_preserved_data() {
  if [[ -d "${PRESERVE_DIR}" ]]; then
    log "Preserved directory confirmed: ${PRESERVE_DIR}"
  else
    warn "Expected preserved directory NOT found: ${PRESERVE_DIR}"
  fi
}

main() {
  require_root

  echo "======================================"
  echo "   Meshtak Uninstall Script"
  echo "======================================"
  echo
  echo "This will REMOVE:"
  echo "  - systemd service (${SERVICE_NAME})"
  echo "  - ${APP_DIR}"
  echo

  read -rp "Continue? [y/N]: " confirm
  if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    echo "Aborted."
    exit 0
  fi

  remove_service
  remove_app_dir
  verify_preserved_data

  echo
  log "Uninstall complete."
}

main "$@"
