#!/usr/bin/env bash
set -euo pipefail

APP_NAME="MeshTAK"
INSTALL_DIR="/opt/meshtak"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_NAME="meshtak"
SERVICE_FILE_SRC="meshtak.service"
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
  if [[ "${EUID}" -ne 0 ]]; then
    fail "Run this script as root."
  fi
}

detect_pkg_mgr() {
  if command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
  elif command -v apt-get >/dev/null 2>&1; then
    PKG_MGR="apt"
  else
    fail "Supported package manager not found (dnf or apt-get)."
  fi
  ok "Package manager detected: ${PKG_MGR}"
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

ensure_install_dir() {
  mkdir -p "${INSTALL_DIR}"
  ok "Install directory ready: ${INSTALL_DIR}"
}

copy_repo_files() {
  info "Copying repo files into ${INSTALL_DIR} ..."
  cp -f ./*.py "${INSTALL_DIR}/" || true
  cp -f "${SERVICE_FILE_SRC}" "${INSTALL_DIR}/" 2>/dev/null || true
  chmod 755 "${INSTALL_DIR}"
  chmod 644 "${INSTALL_DIR}"/*.py 2>/dev/null || true
  ok "Repo files copied"
}

setup_python_venv() {
  info "Creating Python virtual environment..."
  rm -rf "${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"

  "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
  "${VENV_DIR}/bin/pip" install meshtastic PyPubSub lxml

  ok "Virtual environment ready at ${VENV_DIR}"
}

detect_serial_candidates() {
  local candidates=()

  if [[ -d /dev/serial/by-id ]]; then
    while IFS= read -r -d '' dev; do
      candidates+=("$dev")
    done < <(find /dev/serial/by-id -maxdepth 1 -type l -print0 2>/dev/null | sort -z)
  fi

  for pattern in /dev/ttyUSB* /dev/ttyACM* /dev/ttyS0; do
    for dev in $pattern; do
      [[ -e "$dev" ]] && candidates+=("$dev")
    done
  done

  printf '%s\n' "${candidates[@]:-}" | awk 'NF && !seen[$0]++'
}

choose_serial_device() {
  local detected
  mapfile -t detected < <(detect_serial_candidates)

  echo
  bold "Serial device selection" >&2

  if [[ ${#detected[@]} -eq 0 ]]; then
    warn "No serial candidates auto-detected." >&2
    read -rp "Enter serial device manually [/dev/ttyS0]: " SERIAL_DEVICE >&2
    SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyS0}"
    printf '%s\n' "${SERIAL_DEVICE}"
    return
  fi

  echo "Detected serial candidates:" >&2
  local i=1
  for dev in "${detected[@]}"; do
    if [[ -L "$dev" ]]; then
      printf "  %d) %s -> %s\n" "$i" "$dev" "$(readlink -f "$dev")" >&2
    else
      printf "  %d) %s\n" "$i" "$dev" >&2
    fi
    ((i++))
  done
  echo "  m) Enter manually" >&2

  if [[ ${#detected[@]} -eq 1 ]]; then
    read -rp "Use detected device '${detected[0]}'? [Y/n]: " ans >&2
    ans="${ans:-Y}"
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      printf '%s\n' "${detected[0]}"
      return
    fi
  fi

  while true; do
    read -rp "Select device number or 'm': " choice >&2
    if [[ "$choice" == "m" || "$choice" == "M" ]]; then
      read -rp "Enter serial device manually [/dev/ttyS0]: " SERIAL_DEVICE >&2
      SERIAL_DEVICE="${SERIAL_DEVICE:-/dev/ttyS0}"
      printf '%s\n' "${SERIAL_DEVICE}"
      return
    elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#detected[@]} )); then
      printf '%s\n' "${detected[$((choice-1))]}"
      return
    else
      warn "Invalid selection." >&2
    fi
  done
}

configure_mode() {
  echo
  bold "Select Meshtastic connection mode"
  echo "  1) Serial"
  echo "  2) IP/TCP"

  local mode
  while true; do
    read -rp "Choose mode [1/2]: " mode
    case "$mode" in
      1)
        [[ -f "${INSTALL_DIR}/${MESHTAK_SERIAL_FILE}" ]] || fail "Missing ${MESHTAK_SERIAL_FILE} in current folder."
        cp -f "${INSTALL_DIR}/${MESHTAK_SERIAL_FILE}" "${INSTALL_DIR}/${ACTIVE_PY}"

        local serial_dev
        serial_dev="$(choose_serial_device)"
        info "Using serial device: ${serial_dev}"

        sed -i -E "s|^MESHTASTIC_DEVICE *= *['\"].*['\"]|MESHTASTIC_DEVICE = \"${serial_dev}\"|g" "${INSTALL_DIR}/${ACTIVE_PY}" \
          || fail "Failed to set MESHTASTIC_DEVICE in ${ACTIVE_PY}"

        ok "Serial mode configured"
        break
        ;;
      2)
        [[ -f "${INSTALL_DIR}/${MESHTAK_IP_FILE}" ]] || fail "Missing ${MESHTAK_IP_FILE} in current folder."
        cp -f "${INSTALL_DIR}/${MESHTAK_IP_FILE}" "${INSTALL_DIR}/${ACTIVE_PY}"

        read -rp "Enter Meshtastic IP/hostname: " mesh_host
        [[ -n "${mesh_host}" ]] || fail "Meshtastic IP/hostname is required for IP mode."

        sed -i -E "s|^MESHTASTIC_HOST *= *['\"].*['\"]|MESHTASTIC_HOST = \"${mesh_host}\"|g" "${INSTALL_DIR}/${ACTIVE_PY}" \
          || fail "Failed to set MESHTASTIC_HOST in ${ACTIVE_PY}"

        read -rp "Enter Meshtastic TCP port [4403]: " mesh_port
        mesh_port="${mesh_port:-4403}"
        sed -i -E "s|^MESHTASTIC_PORT *= *[0-9]+|MESHTASTIC_PORT = ${mesh_port}|g" "${INSTALL_DIR}/${ACTIVE_PY}" \
          || fail "Failed to set MESHTASTIC_PORT in ${ACTIVE_PY}"

        ok "IP mode configured"
        break
        ;;
      *)
        warn "Enter 1 or 2."
        ;;
    esac
  done
}

install_service() {
  info "Installing systemd service..."

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

  chmod 644 "${SERVICE_FILE_DST}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  ok "Service installed and restarted"
}

show_status() {
  echo
  bold "Done"
  echo "Active script: ${INSTALL_DIR}/${ACTIVE_PY}"
  echo "Venv python:    ${VENV_DIR}/bin/python"
  echo "Log file:       ${LOG_FILE}"
  echo
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
  echo
  echo "Follow logs with:"
  echo "  journalctl -u ${SERVICE_NAME} -f"
  echo "or"
  echo "  tail -f ${LOG_FILE}"
}

main() {
  require_root
  detect_pkg_mgr
  install_dependencies
  ensure_install_dir
  copy_repo_files
  setup_python_venv
  configure_mode
  install_service
  show_status
}

main "$@"
