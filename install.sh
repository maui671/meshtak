#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="/opt/meshtak"
VENV_DIR="$BASE_DIR/venv"
CERT_DIR="$BASE_DIR/certs"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/logs"
CONFIG_FILE="$BASE_DIR/config.json"
SERVICE_FILE="/etc/systemd/system/meshtak.service"
INSTALL_LOG="/var/log/meshtak-install.log"
RUN_USER="tdcadmin"
RUN_GROUP="tdcadmin"
WEB_HOST="0.0.0.0"
WEB_PORT="9443"
CERT_PATH="$CERT_DIR/meshtak.crt"
KEY_PATH="$CERT_DIR/meshtak.key"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONNECTION_TYPE="serial"
SERIAL_PORT="/dev/ttyACM0"
TCP_HOST=""
TAK_ENABLED="false"
TAK_HOST=""
TAK_PORT="8088"
TAK_PROTOCOL="tcp"
TAK_TLS="false"
CHANNELS_JSON='[]'

log() {
  mkdir -p "$(dirname "$INSTALL_LOG")"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$INSTALL_LOG"
}

fail() {
  log "ERROR: $*"
  exit 1
}

require_root() {
  [[ "$EUID" -eq 0 ]] || fail "Run install.sh as root."
}

ensure_user() {
  id "$RUN_USER" >/dev/null 2>&1 || fail "User $RUN_USER does not exist."
}

prompt_yes_no() {
  local prompt="$1" default="${2:-y}" answer
  while true; do
    if [[ "$default" == "y" ]]; then
      read -r -p "$prompt [Y/n]: " answer
      answer="${answer:-y}"
    else
      read -r -p "$prompt [y/N]: " answer
      answer="${answer:-n}"
    fi
    case "${answer,,}" in
      y|yes) echo y; return ;;
      n|no) echo n; return ;;
    esac
    echo "Please answer y or n."
  done
}

detect_serial_devices() {
  local path
  for path in /dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*; do
    [[ -e "$path" ]] && echo "$path"
  done | awk '!seen[$0]++'
}

choose_connection_type() {
  local choice
  echo
  echo "--- Meshtastic Connection ---"
  echo "1) Serial"
  echo "2) IP"
  while true; do
    read -r -p "Choose connection type [1-2] [1]: " choice
    choice="${choice:-1}"
    case "$choice" in
      1) CONNECTION_TYPE="serial"; return ;;
      2) CONNECTION_TYPE="tcp"; return ;;
    esac
    echo "Invalid selection."
  done
}

choose_serial_device() {
  local devices i choice
  mapfile -t devices < <(detect_serial_devices)
  echo
  echo "--- Serial Device ---"
  if (( ${#devices[@]} == 0 )); then
    read -r -p "Enter serial device path [/dev/ttyACM0]: " SERIAL_PORT
    SERIAL_PORT="${SERIAL_PORT:-/dev/ttyACM0}"
    return
  fi
  i=1
  for dev in "${devices[@]}"; do
    echo "$i) $dev"
    ((i++))
  done
  echo "$i) Enter device path manually"
  while true; do
    read -r -p "Choose serial device [1-$i]: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#devices[@]} )); then
      SERIAL_PORT="${devices[$((choice - 1))]}"
      return
    elif [[ "$choice" == "$i" ]]; then
      read -r -p "Enter serial device path [/dev/ttyACM0]: " SERIAL_PORT
      SERIAL_PORT="${SERIAL_PORT:-/dev/ttyACM0}"
      return
    fi
    echo "Invalid selection."
  done
}

ask_ip_host() {
  echo
  echo "--- Meshtastic TCP ---"
  read -r -p "Enter Meshtastic device IP/hostname: " TCP_HOST
  TCP_HOST="${TCP_HOST:-192.168.1.100}"
}

ask_questions() {
  choose_connection_type
  if [[ "$CONNECTION_TYPE" == "serial" ]]; then
    choose_serial_device
  else
    ask_ip_host
  fi

  echo
  echo "--- Web UI ---"
  read -r -p "Web UI port [9443]: " WEB_PORT
  WEB_PORT="${WEB_PORT:-9443}"

  echo
  echo "--- TAK ---"
  if [[ "$(prompt_yes_no 'Enable TAK forwarding?' 'n')" == "y" ]]; then
    TAK_ENABLED="true"
    read -r -p "TAK host/IP: " TAK_HOST
    TAK_HOST="${TAK_HOST:-127.0.0.1}"
    read -r -p "TAK port [8088]: " TAK_PORT
    TAK_PORT="${TAK_PORT:-8088}"
    read -r -p "TAK protocol tcp or udp [tcp]: " TAK_PROTOCOL
    TAK_PROTOCOL="${TAK_PROTOCOL:-tcp}"
    TAK_PROTOCOL="${TAK_PROTOCOL,,}"
    [[ "$TAK_PROTOCOL" =~ ^(tcp|udp)$ ]] || TAK_PROTOCOL="tcp"
    if [[ "$TAK_PROTOCOL" == "tcp" ]] && [[ "$(prompt_yes_no 'Use TLS for TAK?' 'n')" == "y" ]]; then
      TAK_TLS="true"
    fi
  fi

  echo
  echo "--- Optional pinned channels ---"
  echo "Enter JSON for pinned channels or leave blank. Example: [{\"name\":\"Ops\",\"index\":1,\"pinned\":true}]"
  read -r -p "Channels JSON []: " CHANNELS_JSON
  CHANNELS_JSON="${CHANNELS_JSON:-[]}"
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  log "Updating apt cache"
  apt-get update -y
  log "Installing required packages"
  apt-get install -y python3 python3-venv python3-pip python3-dev build-essential git openssl ca-certificates jq unzip
}

prepare_dirs() {
  log "Creating runtime directories"
  mkdir -p "$BASE_DIR" "$CERT_DIR" "$DATA_DIR" "$LOG_DIR"
  cp -a "$SCRIPT_DIR"/. "$BASE_DIR"/
  chown -R "$RUN_USER:$RUN_GROUP" "$BASE_DIR"
}

create_venv() {
  log "Creating virtualenv"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip wheel
  "$VENV_DIR/bin/pip" install -r "$BASE_DIR/requirements.txt"
}

generate_cert() {
  if [[ -f "$CERT_PATH" && -f "$KEY_PATH" ]]; then
    log "Existing web certificate found. Keeping it."
    return
  fi
  log "Generating self-signed Web UI certificate"
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$KEY_PATH" \
    -out "$CERT_PATH" \
    -subj "/CN=$(hostname -f 2>/dev/null || hostname)"
  chown "$RUN_USER:$RUN_GROUP" "$CERT_PATH" "$KEY_PATH"
  chmod 640 "$CERT_PATH" "$KEY_PATH"
}

write_config() {
  log "Writing config to $CONFIG_FILE"
  cat > "$CONFIG_FILE" <<EOF
{
  "connection": {
    "type": "$CONNECTION_TYPE",
    "port": "$SERIAL_PORT",
    "host": "$TCP_HOST"
  },
  "tak": {
    "enabled": $TAK_ENABLED,
    "host": "$TAK_HOST",
    "port": $TAK_PORT,
    "protocol": "$TAK_PROTOCOL",
    "tls": $TAK_TLS,
    "ca_cert": "",
    "client_cert": "",
    "client_key": ""
  },
  "web": {
    "host": "$WEB_HOST",
    "port": $WEB_PORT,
    "tls_cert": "$CERT_PATH",
    "tls_key": "$KEY_PATH"
  },
  "channels": $CHANNELS_JSON,
  "cot": {
    "type": "a-f-G-U-C",
    "team": "Orange",
    "role": "RTO"
  }
}
EOF
  chown "$RUN_USER:$RUN_GROUP" "$CONFIG_FILE"
  chmod 640 "$CONFIG_FILE"
}

write_service() {
  log "Writing systemd service"
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MeshTAK
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
EOF
}

open_firewall() {
  if command -v ufw >/dev/null 2>&1; then
    ufw allow "$WEB_PORT/tcp" >/dev/null 2>&1 || true
    log "Opened UFW port $WEB_PORT/tcp"
  fi
}

start_service() {
  log "Enabling and starting service"
  systemctl daemon-reload
  systemctl enable meshtak.service
  systemctl restart meshtak.service
}

main() {
  require_root
  ensure_user
  ask_questions
  install_packages
  prepare_dirs
  create_venv
  generate_cert
  write_config
  write_service
  open_firewall
  start_service
  log "Install complete. Web UI should be reachable on https://$(hostname -I | awk '{print $1}'):$WEB_PORT"
}

main "$@"
