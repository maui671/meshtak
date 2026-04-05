#!/usr/bin/env bash
set -euo pipefail

APP_NAME="MeshTAK"
INSTALL_DIR="/opt/meshtak"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_NAME="meshtak"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="/etc/default/${SERVICE_NAME}"
LOG_FILE="/var/log/meshtak.log"
INSTALL_LOG="/var/log/meshtak-install.log"
RUN_USER="tdcadmin"
RUN_GROUP="tdcadmin"

WEB_HOST="0.0.0.0"
WEB_PORT="8443"
NODE_PRUNE_SECONDS="86400"

CERT_DIR="${INSTALL_DIR}/certs"
CERT_FILE="${CERT_DIR}/meshtak.crt"
KEY_FILE="${CERT_DIR}/meshtak.key"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONNECTION_MODE=""
SERIAL_DEVICE=""
MESHTASTIC_HOST=""
MESHTASTIC_PORT="4403"
TAK_HOST=""
TAK_PORT="8088"

exec > >(tee -a "${INSTALL_LOG}") 2>&1

bold() { echo -e "\033[1m$*\033[0m"; }
info() { echo -e "[INFO] $*"; }
ok()   { echo -e "[ OK ] $*"; }
warn() { echo -e "[WARN] $*"; }
fail() { echo -e "[FAIL] $*" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "Run this script as root."
}

ensure_repo_files() {
  local required=(
    "requirements.txt"
    "meshtak.py"
    "meshtak_wrapper.py"
    "webui.py"
    "node_store.py"
    "templates/index.html"
    "static/app.js"
    "static/styles.css"
  )

  for f in "${required[@]}"; do
    [[ -f "${REPO_ROOT}/${f}" ]] || fail "Missing required file in repo: ${f}"
  done

  ok "Repo layout verified"
}

ensure_run_user() {
  if id -u "${RUN_USER}" >/dev/null 2>&1; then
    ok "Runtime user exists: ${RUN_USER}"
    return
  fi

  info "Creating runtime user ${RUN_USER}"
  useradd -m -s /bin/bash "${RUN_USER}"
  ok "Created runtime user ${RUN_USER}"
}

detect_pkg_mgr() {
  if command -v apt-get >/dev/null 2>&1; then
    PKG_MGR="apt"
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
  else
    fail "No supported package manager found."
  fi
  ok "Package manager detected: ${PKG_MGR}"
}

install_dependencies() {
  info "Installing OS dependencies"

  if [[ "${PKG_MGR}" == "apt" ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y \
      python3 python3-pip python3-venv python3-full \
      openssl ca-certificates curl \
      avahi-utils usbutils
  else
    dnf install -y \
      python3 python3-pip python3-virtualenv \
      openssl ca-certificates curl \
      avahi nmap-ncat usbutils
  fi

  ok "Dependencies installed"
}

add_runtime_user_groups() {
  for grp in dialout tty uucp plugdev; do
    if getent group "${grp}" >/dev/null 2>&1; then
      usermod -aG "${grp}" "${RUN_USER}" || true
    fi
  done
  ok "Runtime user group memberships updated"
}

detect_serial_candidates() {
  local candidates=()

  if [[ -d /dev/serial/by-id ]]; then
    while IFS= read -r -d '' dev; do
      candidates+=("${dev}")
    done < <(find /dev/serial/by-id -maxdepth 1 -type l -print0 2>/dev/null || true)
  fi

  while IFS= read -r -d '' dev; do
    candidates+=("${dev}")
  done < <(find /dev -maxdepth 1 \( -name 'ttyACM*' -o -name 'ttyUSB*' -o -name 'ttyAMA*' -o -name 'ttyS*' \) -print0 2>/dev/null || true)

  printf '%s\n' "${candidates[@]}" | awk 'NF && !seen[$0]++'
}

prompt_connection_mode() {
  echo
  bold "Meshtastic connection mode"
  echo "  1) Serial"
  echo "  2) IP"

  while true; do
    read -rp "Choose mode [1/2]: " choice
    case "${choice}" in
      1) CONNECTION_MODE="serial"; break ;;
      2) CONNECTION_MODE="ip"; break ;;
      *) warn "Enter 1 or 2." ;;
    esac
  done

  ok "Selected mode: ${CONNECTION_MODE}"
}

prompt_serial_device() {
  mapfile -t devices < <(detect_serial_candidates)

  echo
  bold "Serial device selection"

  if [[ ${#devices[@]} -eq 0 ]]; then
    warn "No serial candidates were auto-detected."
    read -rp "Enter serial device [/dev/ttyACM0]: " SERIAL_DEVICE
    SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyACM0}"
    ok "Selected serial device: ${SERIAL_DEVICE}"
    return
  fi

  local i=1
  for d in "${devices[@]}"; do
    echo "  ${i}) ${d}"
    ((i++))
  done
  echo "  m) manual entry"

  while true; do
    read -rp "Select device: " choice
    if [[ "${choice}" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#devices[@]} )); then
      SERIAL_DEVICE="${devices[$((choice-1))]}"
      break
    elif [[ "${choice}" == "m" || "${choice}" == "M" ]]; then
      read -rp "Enter serial device [/dev/ttyACM0]: " SERIAL_DEVICE
      SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyACM0}"
      break
    else
      warn "Invalid choice."
    fi
  done

  ok "Selected serial device: ${SERIAL_DEVICE}"
}

prompt_ip_target() {
  echo
  bold "Meshtastic IP configuration"

  while true; do
    read -rp "Meshtastic IP/hostname: " MESHTASTIC_HOST
    [[ -n "${MESHTASTIC_HOST}" ]] && break
    warn "Meshtastic IP/hostname is required."
  done

  read -rp "Meshtastic TCP port [4403]: " MESHTASTIC_PORT
  MESHTASTIC_PORT="${MESHTASTIC_PORT:-4403}"
  [[ "${MESHTASTIC_PORT}" =~ ^[0-9]+$ ]] || fail "Meshtastic TCP port must be numeric."

  ok "Meshtastic target: ${MESHTASTIC_HOST}:${MESHTASTIC_PORT}"
}

prompt_tak_target() {
  echo
  bold "TAK target"

  while true; do
    read -rp "TAK server IP/hostname: " TAK_HOST
    [[ -n "${TAK_HOST}" ]] && break
    warn "TAK server IP/hostname is required."
  done

  read -rp "TAK server port [8088]: " TAK_PORT
  TAK_PORT="${TAK_PORT:-8088}"
  [[ "${TAK_PORT}" =~ ^[0-9]+$ ]] || fail "TAK port must be numeric."

  ok "TAK target: ${TAK_HOST}:${TAK_PORT}"
}

show_summary() {
  echo
  bold "Install summary"
  echo "  Install dir: ${INSTALL_DIR}"
  echo "  Runtime user: ${RUN_USER}"
  echo "  Service: ${SERVICE_NAME}"
  echo "  Web UI: https://<this-host>:${WEB_PORT}"
  echo "  Mode: ${CONNECTION_MODE}"
  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    echo "  Serial device: ${SERIAL_DEVICE}"
  else
    echo "  Meshtastic target: ${MESHTASTIC_HOST}:${MESHTASTIC_PORT}"
  fi
  echo "  TAK target: ${TAK_HOST}:${TAK_PORT}"
  echo
}

prepare_install_tree() {
  info "Preparing install tree"

  mkdir -p "${INSTALL_DIR}"
  mkdir -p "${INSTALL_DIR}/templates"
  mkdir -p "${INSTALL_DIR}/static"
  mkdir -p "${CERT_DIR}"

  ok "Install tree prepared"
}

copy_project_files() {
  info "Copying project files"

  install -m 0644 "${REPO_ROOT}/meshtak.py" "${INSTALL_DIR}/meshtak.py"
  install -m 0644 "${REPO_ROOT}/meshtak_wrapper.py" "${INSTALL_DIR}/meshtak_wrapper.py"
  install -m 0644 "${REPO_ROOT}/webui.py" "${INSTALL_DIR}/webui.py"
  install -m 0644 "${REPO_ROOT}/node_store.py" "${INSTALL_DIR}/node_store.py"
  install -m 0644 "${REPO_ROOT}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
  install -m 0644 "${REPO_ROOT}/templates/index.html" "${INSTALL_DIR}/templates/index.html"
  install -m 0644 "${REPO_ROOT}/static/app.js" "${INSTALL_DIR}/static/app.js"
  install -m 0644 "${REPO_ROOT}/static/styles.css" "${INSTALL_DIR}/static/styles.css"

  touch "${INSTALL_DIR}/nodes.json"
  touch "${INSTALL_DIR}/message_queue.json"
  touch "${LOG_FILE}"

  ok "Project files copied"
}

configure_python_venv() {
  info "Creating Python virtual environment"

  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools
  "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

  ok "Python environment ready"
}

patch_meshtak_runtime() {
  info "Applying runtime settings to meshtak.py"

  python3 - <<PY
from pathlib import Path
import re

path = Path("${INSTALL_DIR}/meshtak.py")
text = path.read_text(encoding="utf-8")

replacements = {
    r'^CONNECTION_MODE = .*?$': 'CONNECTION_MODE = "${CONNECTION_MODE}"',
    r'^MESHTASTIC_DEVICE = .*?$': 'MESHTASTIC_DEVICE = "${SERIAL_DEVICE}"',
    r'^MESHTASTIC_HOST = .*?$': 'MESHTASTIC_HOST = "${MESHTASTIC_HOST}"',
    r'^MESHTASTIC_PORT = .*?$': 'MESHTASTIC_PORT = ${MESHTASTIC_PORT}',
    r'^TAK_HOST = .*?$': 'TAK_HOST = "${TAK_HOST}"',
    r'^TAK_PORT = .*?$': 'TAK_PORT = ${TAK_PORT}',
    r'^LOG_FILE_PATH = .*?$': 'LOG_FILE_PATH = "${LOG_FILE}"',
}

for pattern, repl in replacements.items():
    text, count = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if count == 0:
        raise SystemExit(f"Failed to patch setting with pattern: {pattern}")

path.write_text(text, encoding="utf-8")
PY

  ok "meshtak.py patched"
}

write_environment_file() {
  info "Writing environment file"

  cat > "${ENV_FILE}" <<EOF
MESHTAK_LOG_FILE=${LOG_FILE}
MESHTAK_SERVICE_NAME=${SERVICE_NAME}
MESHTAK_WEB_HOST=${WEB_HOST}
MESHTAK_WEB_PORT=${WEB_PORT}
MESHTAK_CERT_FILE=${CERT_FILE}
MESHTAK_KEY_FILE=${KEY_FILE}
MESHTAK_NODE_PRUNE_SECONDS=${NODE_PRUNE_SECONDS}
EOF

  ok "Environment file written: ${ENV_FILE}"
}

generate_tls_cert() {
  info "Generating self-signed TLS certificate"

  local cn
  cn="$(hostname -f 2>/dev/null || hostname)"

  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "${KEY_FILE}" \
    -out "${CERT_FILE}" \
    -days 3650 \
    -subj "/C=US/ST=Georgia/L=Robins_AFB/O=MeshTAK/OU=MeshTAK/CN=${cn}" >/dev/null 2>&1

  ok "TLS certificate created"
}

write_systemd_service() {
  info "Writing systemd unit"

  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=MeshTAK integrated bridge and HTTPS UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/meshtak_wrapper.py
Restart=always
RestartSec=5
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF

  ok "Systemd unit written"
}

set_permissions() {
  info "Setting ownership and permissions"

  chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}"
  chown "${RUN_USER}:${RUN_GROUP}" "${LOG_FILE}"
  chown root:root "${SERVICE_FILE}" "${ENV_FILE}"
  chmod 0644 "${SERVICE_FILE}" "${ENV_FILE}"
  chmod 0644 "${CERT_FILE}"
  chmod 0600 "${KEY_FILE}"

  ok "Permissions applied"
}

open_firewall_port() {
  if command -v ufw >/dev/null 2>&1; then
    ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
    ok "UFW updated for ${WEB_PORT}/tcp"
    return
  fi

  if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${WEB_PORT}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    ok "firewalld updated for ${WEB_PORT}/tcp"
    return
  fi

  warn "No supported firewall manager detected. Port ${WEB_PORT}/tcp was not opened automatically."
}

enable_and_start_service() {
  info "Enabling and starting ${SERVICE_NAME}"

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null
  systemctl restart "${SERVICE_NAME}"

  sleep 3

  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    ok "Service is active"
  else
    warn "Service did not come up cleanly. Showing status:"
    systemctl --no-pager --full status "${SERVICE_NAME}" || true
    fail "MeshTAK service failed to start"
  fi
}

print_completion() {
  local host_ip=""
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

  echo
  bold "Install complete"
  echo "  Service: ${SERVICE_NAME}"
  echo "  Log: ${LOG_FILE}"
  echo "  UI: https://${host_ip:-<host-ip>}:${WEB_PORT}"
  echo "  Install dir: ${INSTALL_DIR}"
  echo
  echo "Useful commands:"
  echo "  systemctl status ${SERVICE_NAME}"
  echo "  journalctl -u ${SERVICE_NAME} -f"
  echo "  tail -f ${LOG_FILE}"
  echo
}

main() {
  require_root
  ensure_repo_files
  ensure_run_user
  detect_pkg_mgr

  echo
  bold "=== ${APP_NAME} install ==="

  prompt_connection_mode
  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    prompt_serial_device
    MESHTASTIC_HOST=""
    MESHTASTIC_PORT="4403"
  else
    prompt_ip_target
    SERIAL_DEVICE=""
  fi
  prompt_tak_target
  show_summary

  install_dependencies
  add_runtime_user_groups
  prepare_install_tree
  copy_project_files
  configure_python_venv
  patch_meshtak_runtime
  write_environment_file
  generate_tls_cert
  write_systemd_service
  set_permissions
  open_firewall_port
  enable_and_start_service
  print_completion
}

main "$@"
