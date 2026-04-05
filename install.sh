#!/usr/bin/env bash
set -euo pipefail

APP_NAME="MeshTAK"
INSTALL_DIR="/opt/meshtak"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_NAME="meshtak"
SERVICE_FILE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_FILE="/var/log/meshtak.log"
INSTALL_LOG="/var/log/meshtak-install.log"
NODE_STORE_FILE="${INSTALL_DIR}/nodes.json"

MESHTAK_IP_FILE="meshtak_ip.py"
MESHTAK_SERIAL_FILE="meshtak_serial.py"
ACTIVE_PY="meshtak.py"
WRAPPER_PY="meshtak_wrapper.py"
WEBUI_PY="webui.py"
NODE_STORE_PY="node_store.py"

CONNECTION_MODE=""
SERIAL_DEVICE=""
MESHTASTIC_HOST=""
MESHTASTIC_PORT="4403"
TAK_HOST=""
TAK_PORT="8088"
PKG_MGR=""

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
  [[ -f "./requirements.txt" ]] || fail "requirements.txt not found in repo root."
  [[ -f "./${MESHTAK_SERIAL_FILE}" ]] || fail "${MESHTAK_SERIAL_FILE} not found in repo root."
  [[ -f "./${MESHTAK_IP_FILE}" ]] || fail "${MESHTAK_IP_FILE} not found in repo root."
  [[ -f "./${WRAPPER_PY}" ]] || fail "${WRAPPER_PY} not found in repo root."
  [[ -f "./${WEBUI_PY}" ]] || fail "${WEBUI_PY} not found in repo root."
  [[ -f "./${NODE_STORE_PY}" ]] || fail "${NODE_STORE_PY} not found in repo root."
  [[ -f "./templates/index.html" ]] || fail "templates/index.html not found."
  [[ -f "./static/app.js" ]] || fail "static/app.js not found."
}

detect_pkg_mgr() {
  if command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
  elif command -v apt-get >/dev/null 2>&1; then
    PKG_MGR="apt"
  else
    fail "Supported package manager not found."
  fi
  ok "Package manager detected: ${PKG_MGR}"
}

detect_serial_candidates() {
  local candidates=()

  if [[ -d /dev/serial/by-id ]]; then
    while IFS= read -r -d '' dev; do
      candidates+=("$dev")
    done < <(find /dev/serial/by-id -maxdepth 1 -type l -print0 2>/dev/null || true)
  fi

  while IFS= read -r -d '' dev; do
    candidates+=("$dev")
  done < <(find /dev -maxdepth 1 \( -name 'ttyACM*' -o -name 'ttyUSB*' -o -name 'ttyAMA*' -o -name 'ttyS*' \) -print0 2>/dev/null || true)

  printf '%s\n' "${candidates[@]}" | awk 'NF && !seen[$0]++'
}

choose_mode() {
  echo
  bold "Select Meshtastic connection mode"
  echo "  1) Serial (USB / UART)"
  echo "  2) IP (TCP)"

  while true; do
    read -rp "Choose mode [1/2]: " mode_choice
    case "${mode_choice}" in
      1) CONNECTION_MODE="serial"; break ;;
      2) CONNECTION_MODE="ip"; break ;;
      *) warn "Enter 1 or 2." ;;
    esac
  done

  ok "Selected mode: ${CONNECTION_MODE}"
}

choose_serial_device() {
  mapfile -t devices < <(detect_serial_candidates)

  echo
  bold "Serial device selection"

  if [[ ${#devices[@]} -eq 0 ]]; then
    warn "No serial devices auto-detected."
    while true; do
      read -rp "Enter serial device manually [/dev/ttyACM0]: " SERIAL_DEVICE
      SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyACM0}"
      [[ -n "${SERIAL_DEVICE}" ]] && break
    done
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
    if [[ "${choice}" == "m" || "${choice}" == "M" ]]; then
      read -rp "Enter serial device [/dev/ttyACM0]: " SERIAL_DEVICE
      SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyACM0}"
      [[ -n "${SERIAL_DEVICE}" ]] || continue
      break
    elif [[ "${choice}" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#devices[@]} )); then
      SERIAL_DEVICE="${devices[$((choice-1))]}"
      break
    else
      warn "Invalid choice."
    fi
  done

  ok "Selected serial device: ${SERIAL_DEVICE}"
}

choose_ip_details() {
  echo
  bold "Meshtastic IP configuration"

  while true; do
    read -rp "Meshtastic IP/hostname: " MESHTASTIC_HOST
    [[ -n "${MESHTASTIC_HOST}" ]] && break
    warn "Meshtastic IP/hostname is required."
  done

  read -rp "Meshtastic port [4403]: " MESHTASTIC_PORT
  MESHTASTIC_PORT="${MESHTASTIC_PORT:-4403}"
  [[ "${MESHTASTIC_PORT}" =~ ^[0-9]+$ ]] || fail "Meshtastic port must be numeric."

  ok "Meshtastic target: ${MESHTASTIC_HOST}:${MESHTASTIC_PORT}"
}

choose_tak_target() {
  echo
  bold "TAK server configuration"

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
  echo "  Mode: ${CONNECTION_MODE}"
  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    echo "  Serial device: ${SERIAL_DEVICE}"
  else
    echo "  Meshtastic target: ${MESHTASTIC_HOST}:${MESHTASTIC_PORT}"
  fi
  echo "  TAK target: ${TAK_HOST}:${TAK_PORT}"
  echo "  Install dir: ${INSTALL_DIR}"
  echo "  Service: ${SERVICE_NAME}"
  echo
}

install_dependencies() {
  info "Installing OS dependencies..."

  if [[ "${PKG_MGR}" == "dnf" ]]; then
    dnf install -y python3 python3-pip python3-virtualenv python3-devel
  else
    apt-get update
    apt-get install -y python3 python3-pip python3-venv python3-full
  fi

  ok "OS dependencies installed"
}

prepare_install_dir() {
  info "Preparing install directory..."
  mkdir -p "${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR:?}/"*
  mkdir -p "${INSTALL_DIR}/templates"
  mkdir -p "${INSTALL_DIR}/static"
  ok "Install directory ready"
}

copy_repo_files() {
  info "Copying repo files..."

  cp -f ./*.py "${INSTALL_DIR}/" 2>/dev/null || true
  cp -f ./requirements.txt "${INSTALL_DIR}/requirements.txt"
  cp -f ./templates/index.html "${INSTALL_DIR}/templates/index.html"
  cp -f ./static/app.js "${INSTALL_DIR}/static/app.js"

  chmod 644 "${INSTALL_DIR}"/*.py 2>/dev/null || true
  chmod 644 "${INSTALL_DIR}/requirements.txt"
  chmod 644 "${INSTALL_DIR}/templates/index.html"
  chmod 644 "${INSTALL_DIR}/static/app.js"

  ok "Repo files copied"
}

patch_python_files() {
  info "Applying runtime settings to source files..."

  for f in "${INSTALL_DIR}/${MESHTAK_IP_FILE}" "${INSTALL_DIR}/${MESHTAK_SERIAL_FILE}"; do
    [[ -f "${f}" ]] || continue
    sed -i -E "s|^TAK_HOST *= *['\"].*['\"]|TAK_HOST = \"${TAK_HOST}\"|" "${f}"
    sed -i -E "s|^TAK_PORT *= *[0-9]+|TAK_PORT = ${TAK_PORT}|" "${f}"
    sed -i -E "s|^LOG_FILE_PATH *= *['\"].*['\"]|LOG_FILE_PATH = \"${LOG_FILE}\"|" "${f}"
  done

  if [[ -f "${INSTALL_DIR}/${NODE_STORE_PY}" ]]; then
    sed -i -E "s|^STORE_FILE *= *['\"].*['\"]|STORE_FILE = \"${NODE_STORE_FILE}\"|" \
      "${INSTALL_DIR}/${NODE_STORE_PY}"
  fi

  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    cp -f "${INSTALL_DIR}/${MESHTAK_SERIAL_FILE}" "${INSTALL_DIR}/${ACTIVE_PY}"
    sed -i -E "s|^MESHTASTIC_DEVICE *= *['\"].*['\"]|MESHTASTIC_DEVICE = \"${SERIAL_DEVICE}\"|" \
      "${INSTALL_DIR}/${ACTIVE_PY}"
  else
    cp -f "${INSTALL_DIR}/${MESHTAK_IP_FILE}" "${INSTALL_DIR}/${ACTIVE_PY}"
    sed -i -E "s|^MESHTASTIC_HOST *=.*|MESHTASTIC_HOST = \"${MESHTASTIC_HOST}\"|" \
      "${INSTALL_DIR}/${ACTIVE_PY}"
    sed -i -E "s|^MESHTASTIC_PORT *=.*|MESHTASTIC_PORT = ${MESHTASTIC_PORT}|" \
      "${INSTALL_DIR}/${ACTIVE_PY}"
  fi

  chmod 644 "${INSTALL_DIR}/${ACTIVE_PY}"
  ok "Active runtime script prepared: ${INSTALL_DIR}/${ACTIVE_PY}"
}

setup_python_venv() {
  info "Creating Python virtual environment..."

  rm -rf "${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"

  "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
  "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

  ok "Python environment ready"
}

ensure_runtime_permissions() {
  info "Setting runtime permissions..."

  mkdir -p "$(dirname "${LOG_FILE}")"
  touch "${LOG_FILE}"
  chmod 664 "${LOG_FILE}" || true

  mkdir -p "${INSTALL_DIR}"
  if [[ ! -f "${NODE_STORE_FILE}" ]]; then
    echo "{}" > "${NODE_STORE_FILE}"
  fi
  chmod 664 "${NODE_STORE_FILE}" || true

  if id tdcadmin >/dev/null 2>&1; then
    if getent group dialout >/dev/null 2>&1; then
      usermod -aG dialout tdcadmin 2>/dev/null || true
    fi
    if getent group uucp >/dev/null 2>&1; then
      usermod -aG uucp tdcadmin 2>/dev/null || true
    fi
  fi

  ok "Runtime permissions set"
}

install_service() {
  info "Creating systemd service..."

  cat > "${SERVICE_FILE_DST}" <<EOF
[Unit]
Description=MeshTAK Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/${WRAPPER_PY}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"

  ok "Systemd service installed and started"
}

show_status() {
  local host_ip
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  host_ip="${host_ip:-localhost}"

  echo
  bold "MeshTAK install complete"
  echo "Mode:            ${CONNECTION_MODE}"
  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    echo "Serial device:   ${SERIAL_DEVICE}"
  else
    echo "Meshtastic IP:   ${MESHTASTIC_HOST}:${MESHTASTIC_PORT}"
  fi
  echo "TAK target:      ${TAK_HOST}:${TAK_PORT}"
  echo "Active script:   ${INSTALL_DIR}/${ACTIVE_PY}"
  echo "Wrapper:         ${INSTALL_DIR}/${WRAPPER_PY}"
  echo "Node store:      ${NODE_STORE_FILE}"
  echo "Web UI:          http://${host_ip}:8420"
  echo "Python venv:     ${VENV_DIR}"
  echo "Install log:     ${INSTALL_LOG}"
  echo "Runtime log:     ${LOG_FILE}"
  echo
  echo "Operational commands:"
  echo "  systemctl status meshtak --no-pager"
  echo "  journalctl -u meshtak -f"
  echo "  tail -f ${LOG_FILE}"
  echo "  cat ${NODE_STORE_FILE}"
  echo
  systemctl status meshtak --no-pager || true
}

main() {
  require_root
  ensure_repo_files
  detect_pkg_mgr

  choose_mode
  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    choose_serial_device
  else
    choose_ip_details
  fi
  choose_tak_target
  show_summary

  install_dependencies
  prepare_install_dir
  copy_repo_files
  patch_python_files
  setup_python_venv
  ensure_runtime_permissions
  install_service
  show_status
}

main "$@"
