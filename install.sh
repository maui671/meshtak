#!/usr/bin/env bash

set -Eeuo pipefail

BASE_DIR="/opt/meshtak"
VENV_DIR="$BASE_DIR/venv"
DATA_DIR="$BASE_DIR/data"
STATIC_DIR="$BASE_DIR/static"
TEMPLATE_DIR="$BASE_DIR/templates"
CERT_DIR="$BASE_DIR/certs"
LOG_DIR="$BASE_DIR/logs"
CONFIG_FILE="$BASE_DIR/config.json"
SERVICE_FILE="/etc/systemd/system/meshtak.service"
INSTALL_LOG="/var/log/meshtak-install.log"

RUN_USER="tdcadmin"
RUN_GROUP="tdcadmin"

WEB_HOST="0.0.0.0"
WEB_PORT="8443"
CERT_PATH="$CERT_DIR/meshtak.crt"
KEY_PATH="$CERT_DIR/meshtak.key"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"

CONNECTION_TYPE=""
SERIAL_PORT=""
TCP_HOST=""
TAK_ENABLED="false"
TAK_HOST=""
TAK_PORT="8088"
TAK_PROTOCOL="udp"
TAK_TLS="false"
TAK_VERIFY_SERVER="false"
TAK_CA_CERT="$CERT_DIR/tak-ca.pem"
TAK_CLIENT_CERT="$CERT_DIR/tak-client.crt"
TAK_CLIENT_KEY="$CERT_DIR/tak-client.key"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$INSTALL_LOG"
}

fail() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "$INSTALL_LOG" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    fail "This installer must be run as root."
  fi
}

ensure_user() {
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    fail "User $RUN_USER does not exist. Create it first."
  fi
}

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local answer

  while true; do
    if [[ "$default" == "y" ]]; then
      read -r -p "$prompt [Y/n]: " answer
      answer="${answer:-y}"
    else
      read -r -p "$prompt [y/N]: " answer
      answer="${answer:-n}"
    fi

    case "${answer,,}" in
      y|yes) echo "y"; return 0 ;;
      n|no) echo "n"; return 0 ;;
      *) echo "Please answer y or n." ;;
    esac
  done
}

detect_serial_devices() {
  local devices=()
  local path

  for path in /dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*; do
    [[ -e "$path" ]] || continue
    devices+=("$path")
  done

  printf '%s\n' "${devices[@]}" | awk '!seen[$0]++'
}

choose_connection_type() {
  local choice=""

  echo
  echo "--- Meshtastic Connection Type ---"
  echo "1) Serial"
  echo "2) IP"

  while true; do
    read -r -p "Choose connection type [1-2]: " choice
    case "$choice" in
      1)
        CONNECTION_TYPE="serial"
        return 0
        ;;
      2)
        CONNECTION_TYPE="tcp"
        return 0
        ;;
      *)
        echo "Invalid selection. Enter 1 for Serial or 2 for IP."
        ;;
    esac
  done
}

choose_serial_device() {
  local serial_devices=()
  local i=1
  local choice=""

  mapfile -t serial_devices < <(detect_serial_devices)

  echo
  echo "--- Serial Device Selection ---"

  if (( ${#serial_devices[@]} == 0 )); then
    echo "No serial devices auto-detected."
    read -r -p "Enter serial device path manually [/dev/ttyACM0]: " SERIAL_PORT
    SERIAL_PORT="${SERIAL_PORT:-/dev/ttyACM0}"
    return 0
  fi

  for dev in "${serial_devices[@]}"; do
    echo "$i) $dev"
    ((i++))
  done
  echo "$i) Enter device path manually"

  while true; do
    read -r -p "Choose serial device [1-$i]: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
      if (( choice >= 1 && choice <= ${#serial_devices[@]} )); then
        SERIAL_PORT="${serial_devices[$((choice - 1))]}"
        return 0
      elif (( choice == i )); then
        read -r -p "Enter serial device path manually [/dev/ttyACM0]: " SERIAL_PORT
        SERIAL_PORT="${SERIAL_PORT:-/dev/ttyACM0}"
        return 0
      fi
    fi
    echo "Invalid selection."
  done
}

ask_ip_host() {
  echo
  echo "--- Meshtastic IP Configuration ---"
  read -r -p "Enter Meshtastic device IP or hostname [192.168.1.100]: " TCP_HOST
  TCP_HOST="${TCP_HOST:-192.168.1.100}"
}

ask_tak_questions() {
  echo
  echo "--- TAK Configuration ---"

  local tak_choice
  tak_choice="$(prompt_yes_no "Enable TAK forwarding?" "n")"
  if [[ "$tak_choice" != "y" ]]; then
    TAK_ENABLED="false"
    TAK_HOST=""
    TAK_PORT="8088"
    TAK_PROTOCOL="udp"
    TAK_TLS="false"
    TAK_VERIFY_SERVER="false"
    return 0
  fi

  TAK_ENABLED="true"

  read -r -p "TAK host [127.0.0.1]: " TAK_HOST
  TAK_HOST="${TAK_HOST:-127.0.0.1}"

  read -r -p "TAK port [8088]: " TAK_PORT
  TAK_PORT="${TAK_PORT:-8088}"

  while true; do
    read -r -p "TAK ingest transport [udp/tcp] [udp]: " TAK_PROTOCOL
    TAK_PROTOCOL="${TAK_PROTOCOL:-udp}"
    TAK_PROTOCOL="${TAK_PROTOCOL,,}"
    case "$TAK_PROTOCOL" in
      udp|tcp) break ;;
      *) echo "Please enter udp or tcp." ;;
    esac
  done

  if [[ "$TAK_PROTOCOL" == "tcp" ]]; then
    local tls_choice
    tls_choice="$(prompt_yes_no "Use TLS for TAK TCP?" "n")"
    if [[ "$tls_choice" == "y" ]]; then
      TAK_TLS="true"
      local verify_choice
      verify_choice="$(prompt_yes_no "Verify TAK server certificate?" "n")"
      if [[ "$verify_choice" == "y" ]]; then
        TAK_VERIFY_SERVER="true"
      else
        TAK_VERIFY_SERVER="false"
      fi
    else
      TAK_TLS="false"
      TAK_VERIFY_SERVER="false"
    fi
  else
    TAK_TLS="false"
    TAK_VERIFY_SERVER="false"
  fi
}

ask_questions() {
  log "Starting installer prompts"

  choose_connection_type

  if [[ "$CONNECTION_TYPE" == "serial" ]]; then
    choose_serial_device
  else
    ask_ip_host
  fi

  ask_tak_questions

  echo
  log "Prompt summary:"
  log "  Connection type: $CONNECTION_TYPE"
  if [[ "$CONNECTION_TYPE" == "serial" ]]; then
    log "  Serial port: $SERIAL_PORT"
  else
    log "  TCP host: $TCP_HOST"
  fi
  log "  TAK enabled: $TAK_ENABLED"
  log "  TAK host: ${TAK_HOST:-<disabled>}"
  log "  TAK port: $TAK_PORT"
  log "  TAK protocol: $TAK_PROTOCOL"
  log "  TAK TLS: $TAK_TLS"
  log "  TAK verify server: $TAK_VERIFY_SERVER"
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive

  log "Updating apt cache"
  apt-get update -y

  log "Installing required packages"
  apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    build-essential \
    openssl \
    ufw \
    rsync \
    ca-certificates
}

create_directories() {
  log "Creating MeshTAK directories"
  mkdir -p \
    "$BASE_DIR" \
    "$DATA_DIR" \
    "$STATIC_DIR" \
    "$TEMPLATE_DIR" \
    "$CERT_DIR" \
    "$LOG_DIR"
}

copy_project_files() {
  log "Copying project files into $BASE_DIR"
  rsync -a --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'venv' \
    --exclude 'data' \
    --exclude 'certs' \
    --exclude 'logs' \
    "$SOURCE_DIR"/ "$BASE_DIR"/
}

create_venv() {
  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating Python virtual environment"
    python3 -m venv "$VENV_DIR"
  else
    log "Using existing Python virtual environment"
  fi

  log "Upgrading pip/setuptools/wheel"
  "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel

  if [[ ! -f "$BASE_DIR/requirements.txt" ]]; then
    fail "requirements.txt not found in $BASE_DIR"
  fi

  log "Installing Python requirements"
  "$VENV_DIR/bin/pip" install -r "$BASE_DIR/requirements.txt"
}

write_config() {
  log "Writing config file to $CONFIG_FILE"

  if [[ "$CONNECTION_TYPE" == "serial" ]]; then
    cat > "$CONFIG_FILE" <<EOF_JSON
{
  "connection": {
    "type": "serial",
    "port": "$SERIAL_PORT"
  },
  "tak": {
    "enabled": $TAK_ENABLED,
    "host": "$TAK_HOST",
    "port": $TAK_PORT,
    "protocol": "$TAK_PROTOCOL",
    "tls": $TAK_TLS,
    "verify_server": $TAK_VERIFY_SERVER,
    "ca_cert": "$TAK_CA_CERT",
    "client_cert": "$TAK_CLIENT_CERT",
    "client_key": "$TAK_CLIENT_KEY"
  },
  "cot": {
    "type": "a-f-G-U-C",
    "team": "Orange",
    "role": "RTO"
  },
  "web": {
    "host": "$WEB_HOST",
    "port": $WEB_PORT,
    "tls_cert": "$CERT_PATH",
    "tls_key": "$KEY_PATH"
  }
}
EOF_JSON
  else
    cat > "$CONFIG_FILE" <<EOF_JSON
{
  "connection": {
    "type": "tcp",
    "host": "$TCP_HOST"
  },
  "tak": {
    "enabled": $TAK_ENABLED,
    "host": "$TAK_HOST",
    "port": $TAK_PORT,
    "protocol": "$TAK_PROTOCOL",
    "tls": $TAK_TLS,
    "verify_server": $TAK_VERIFY_SERVER,
    "ca_cert": "$TAK_CA_CERT",
    "client_cert": "$TAK_CLIENT_CERT",
    "client_key": "$TAK_CLIENT_KEY"
  },
  "cot": {
    "type": "a-f-G-U-C",
    "team": "Orange",
    "role": "RTO"
  },
  "web": {
    "host": "$WEB_HOST",
    "port": $WEB_PORT,
    "tls_cert": "$CERT_PATH",
    "tls_key": "$KEY_PATH"
  }
}
EOF_JSON
  fi
}

generate_cert() {
  if [[ -f "$CERT_PATH" && -f "$KEY_PATH" ]]; then
    log "Existing TLS certificate found, leaving in place"
    return 0
  fi

  log "Generating self-signed TLS certificate"
  openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout "$KEY_PATH" \
    -out "$CERT_PATH" \
    -subj "/C=US/ST=GA/L=Local/O=MeshTAK/CN=meshtak"
}

prepare_runtime_files() {
  log "Preparing runtime files"
  touch "$LOG_DIR/meshtak.log"
  touch "$LOG_DIR/webui.log"
  touch "$LOG_DIR/wrapper.log"
}

set_permissions() {
  log "Setting ownership and permissions"
  chown -R "$RUN_USER:$RUN_GROUP" "$BASE_DIR"

  chmod 755 "$BASE_DIR"
  chmod 755 "$DATA_DIR" "$STATIC_DIR" "$TEMPLATE_DIR" "$CERT_DIR" "$LOG_DIR"
  chmod 600 "$KEY_PATH"
  chmod 644 "$CERT_PATH" "$CONFIG_FILE"
  [[ -f "$TAK_CLIENT_KEY" ]] && chmod 600 "$TAK_CLIENT_KEY"
  [[ -f "$TAK_CLIENT_CERT" ]] && chmod 644 "$TAK_CLIENT_CERT"
  [[ -f "$TAK_CA_CERT" ]] && chmod 644 "$TAK_CA_CERT"
  chmod 664 "$LOG_DIR/meshtak.log" "$LOG_DIR/webui.log" "$LOG_DIR/wrapper.log"
}

ensure_serial_access() {
  if getent group dialout >/dev/null 2>&1; then
    if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "dialout"; then
      log "User $RUN_USER is already in dialout"
    else
      log "Adding $RUN_USER to dialout for serial access"
      usermod -aG dialout "$RUN_USER"
    fi
  else
    log "Group dialout not present, skipping serial group membership"
  fi
}

write_systemd_service() {
  log "Writing systemd service file"
  cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=MeshTAK Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$BASE_DIR
ExecStart=$VENV_DIR/bin/python $BASE_DIR/meshtak_wrapper.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF_SERVICE
}

configure_firewall() {
  log "Opening HTTPS Web UI port 8443/tcp in UFW"
  ufw allow 8443/tcp >/dev/null 2>&1 || true
}

start_service() {
  log "Reloading systemd"
  systemctl daemon-reload

  log "Enabling meshtak service"
  systemctl enable meshtak >/dev/null

  log "Restarting meshtak service"
  systemctl restart meshtak

  log "Service status:"
  systemctl --no-pager --full status meshtak || true
}

main() {
  require_root
  ensure_user

  mkdir -p "$(dirname "$INSTALL_LOG")"
  touch "$INSTALL_LOG"
  chmod 644 "$INSTALL_LOG"

  log "=== START MeshTAK install ==="
  log "Source dir: $SOURCE_DIR"
  log "Target dir: $BASE_DIR"

  ask_questions
  install_packages
  create_directories
  copy_project_files
  create_venv
  write_config
  generate_cert
  prepare_runtime_files
  set_permissions
  ensure_serial_access
  write_systemd_service
  configure_firewall
  start_service

  log "=== MeshTAK install complete ==="
  echo
  echo "MeshTAK install complete."
  echo "Web UI: https://<raspberry-pi-ip>:8443"
  echo "Config: $CONFIG_FILE"
  echo "Service: systemctl status meshtak"
  echo "Logs: $LOG_DIR"
}

main "$@"
