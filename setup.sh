#!/usr/bin/env bash
set -euo pipefail

APP_NAME="MeshTAK"
INSTALL_DIR="/opt/meshtak"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_NAME="meshtak"
SERVICE_FILE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_FILE="/var/log/meshtak.log"

MESHTAK_IP_FILE="meshtak_ip.py"
MESHTAK_SERIAL_FILE="meshtak_serial.py"
ACTIVE_PY="meshtak.py"

bold() { echo -e "\033[1m$*\033[0m"; }
info() { echo -e "[INFO] $*"; }
ok()   { echo -e "[ OK ] $*"; }
warn() { echo -e "[WARN] $*"; }
fail() { echo -e "[FAIL] $*" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "Run this script as root."
}

choose_mode() {
  echo
  bold "Select Meshtastic connection mode"
  echo "  1) Serial (USB / UART)"
  echo "  2) IP (TCP)"

  while true; do
    read -rp "Choose mode [1/2]: " MODE
    case "$MODE" in
      1) CONNECTION_MODE="serial"; break ;;
      2) CONNECTION_MODE="ip"; break ;;
      *) warn "Enter 1 or 2." ;;
    esac
  done

  ok "Selected mode: ${CONNECTION_MODE}"
}

choose_tak_target() {
  echo
  bold "TAK server configuration"
  read -rp "TAK server IP/hostname: " TAK_HOST
  [[ -n "${TAK_HOST}" ]] || fail "TAK server IP/hostname is required."

  read -rp "TAK server port [8088]: " TAK_PORT
  TAK_PORT="${TAK_PORT:-8088}"

  ok "TAK target: ${TAK_HOST}:${TAK_PORT}"
}

detect_pkg_mgr() {
  if command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
  elif command -v apt-get >/dev/null 2>&1; then
    PKG_MGR="apt"
  else
    fail "Supported package manager not found"
  fi
  ok "Package manager: ${PKG_MGR}"
}

install_dependencies() {
  info "Installing OS dependencies..."

  if [[ "${PKG_MGR}" == "dnf" ]]; then
    dnf install -y python3 python3-pip python3-virtualenv python3-devel
  else
    apt-get update
    apt-get install -y python3 python3-pip python3-venv python3-full
  fi

  ok "Dependencies installed"
}

ensure_install_dir() {
  mkdir -p "${INSTALL_DIR}"
}

copy_repo_files() {
  cp -f ./*.py "${INSTALL_DIR}/" || true
  chmod 644 "${INSTALL_DIR}"/*.py 2>/dev/null || true
  ok "Files copied to ${INSTALL_DIR}"
}

apply_tak_target() {
  info "Applying TAK server settings to Python files..."

  for f in "${INSTALL_DIR}/${MESHTAK_IP_FILE}" "${INSTALL_DIR}/${MESHTAK_SERIAL_FILE}"; do
    [[ -f "$f" ]] || continue

    sed -i -E "s|^TAK_HOST *= *['\"].*['\"]|TAK_HOST = \"${TAK_HOST}\"|" "$f"
    sed -i -E "s|^TAK_PORT *= *[0-9]+|TAK_PORT = ${TAK_PORT}|" "$f"
  done

  ok "TAK settings applied"
}

setup_python_venv() {
  info "Setting up Python venv..."

  rm -rf "${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"

  "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
  "${VENV_DIR}/bin/pip" install meshtastic PyPubSub lxml

  ok "Venv ready"
}

detect_serial_candidates() {
  local candidates=()

  [[ -d /dev/serial/by-id ]] && \
    candidates+=(/dev/serial/by-id/*)

  candidates+=(/dev/ttyUSB* /dev/ttyACM* /dev/ttyS0)

  for dev in "${candidates[@]}"; do
    [[ -e "$dev" ]] && echo "$dev"
  done | awk '!seen[$0]++'
}

choose_serial_device() {
  mapfile -t devices < <(detect_serial_candidates)

  echo
  bold "Serial device selection"

  if [[ ${#devices[@]} -eq 0 ]]; then
    warn "No devices detected"
    read -rp "Enter manually [/dev/ttyS0]: " SERIAL_DEVICE
    SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyS0}"
    return
  fi

  local i=1
  for d in "${devices[@]}"; do
    echo "  $i) $d"
    ((i++))
  done
  echo "  m) manual entry"

  while true; do
    read -rp "Select device: " choice
    if [[ "$choice" == "m" ]]; then
      read -rp "Device [/dev/ttyS0]: " SERIAL_DEVICE
      SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyS0}"
      return
    elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >=1 && choice <= ${#devices[@]} )); then
      SERIAL_DEVICE="${devices[$((choice-1))]}"
      return
    else
      warn "Invalid choice"
    fi
  done
}

configure_mode() {

  if [[ "${CONNECTION_MODE}" == "serial" ]]; then
    cp "${INSTALL_DIR}/${MESHTAK_SERIAL_FILE}" "${INSTALL_DIR}/${ACTIVE_PY}"

    choose_serial_device
    info "Using serial: ${SERIAL_DEVICE}"

    sed -i -E "s|^MESHTASTIC_DEVICE *= *['\"].*['\"]|MESHTASTIC_DEVICE = \"${SERIAL_DEVICE}\"|" \
      "${INSTALL_DIR}/${ACTIVE_PY}"

  else
    cp "${INSTALL_DIR}/${MESHTAK_IP_FILE}" "${INSTALL_DIR}/${ACTIVE_PY}"

    read -rp "Meshtastic IP: " MESHTASTIC_HOST
    read -rp "Port [4403]: " MESHTASTIC_PORT
    MESHTASTIC_PORT="${MESHTASTIC_PORT:-4403}"

    sed -i -E "s|^MESHTASTIC_HOST *=.*|MESHTASTIC_HOST = \"${MESHTASTIC_HOST}\"|" \
      "${INSTALL_DIR}/${ACTIVE_PY}"

    sed -i -E "s|^MESHTASTIC_PORT *=.*|MESHTASTIC_PORT = ${MESHTASTIC_PORT}|" \
      "${INSTALL_DIR}/${ACTIVE_PY}"
  fi

  ok "Mode configured"
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
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/${ACTIVE_PY}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"

  ok "Service running"
}

show_status() {
  echo
  bold "Install complete"
  echo "Mode:          ${CONNECTION_MODE}"
  echo "TAK target:    ${TAK_HOST}:${TAK_PORT}"
  echo "Script:        ${INSTALL_DIR}/${ACTIVE_PY}"
  echo "Python:        ${VENV_DIR}/bin/python"
  echo
  echo "Logs:"
  echo "  journalctl -u meshtak -f"
}

main() {
  require_root
  choose_mode
  choose_tak_target
  detect_pkg_mgr
  install_dependencies
  ensure_install_dir
  copy_repo_files
  apply_tak_target
  setup_python_venv
  configure_mode
  install_service
  show_status
}

main "$@"
