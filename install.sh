#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=/opt/meshtak
CONFIG_DIR=/etc/meshtak
JSON_CONFIG=${CONFIG_DIR}/config.json
YAML_CONFIG=${APP_DIR}/config/local.yaml
PACKAGE_RECORD=${CONFIG_DIR}/installed_packages.txt
SERVICE_FILE=/etc/systemd/system/meshtak.service
ENV_FILE=/etc/default/meshtak
RUN_USER=tdcadmin
RUN_GROUP=tdcadmin
VENV_DIR=${APP_DIR}/venv
CERT_DIR=${APP_DIR}/certs
CERT_FILE=${CERT_DIR}/meshtak.crt
KEY_FILE=${CERT_DIR}/meshtak.key
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HAL_BUILD_DIR=/opt/sx1302_hal
WEB_PORT=443
ACTIVE_ENABLED=true
ACTIVE_CONN_TYPE=serial
ACTIVE_SERIAL_PORT=/dev/ttyACM0
ACTIVE_TCP_HOST=""
ACTIVE_TCP_PORT=4403
TAK_ENABLED=false
TAK_HOST=127.0.0.1
TAK_PORT=8088
TAK_PROTOCOL=udp
TAK_TLS=false
ENABLE_SPI_I2C=true
OPEN_UFW=true
PASSIVE_ENABLED=true
SPI_DEV=/dev/spidev0.0
DEVICE_NAME="MeshTAK"
TAK_CA_CERT=""; TAK_CLIENT_CERT=""; TAK_CLIENT_KEY=""
log(){ echo "[$(date '+%F %T')] $*"; }
require_root(){ [[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }; }
prompt_yes_no(){ local p="$1" d="${2:-y}" a; while true; do if [[ "$d" == y ]]; then read -r -p "$p [Y/n]: " a; a="${a:-y}"; else read -r -p "$p [y/N]: " a; a="${a:-n}"; fi; case "${a,,}" in y|yes) echo y; return;; n|no) echo n; return;; esac; done; }
detect_serial(){ for p in /dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*; do [[ -e "$p" ]] && echo "$p"; done | awk '!seen[$0]++'; }
choose_serial(){ mapfile -t devs < <(detect_serial); echo; echo "--- Heltec / Active Meshtastic Serial Device ---"; if (( ${#devs[@]} == 0 )); then read -r -p "Enter device path [/dev/ttyACM0]: " ACTIVE_SERIAL_PORT; ACTIVE_SERIAL_PORT="${ACTIVE_SERIAL_PORT:-/dev/ttyACM0}"; return; fi; local i=1; for d in "${devs[@]}"; do echo "  ${i}) ${d}"; ((i++)); done; echo "  ${i}) Enter device path manually"; while true; do read -r -p "Choose serial device [1-${i}]: " c; if [[ "$c" =~ ^[0-9]+$ ]] && (( c>=1 && c<=${#devs[@]} )); then ACTIVE_SERIAL_PORT="${devs[$((c-1))]}"; return; elif [[ "$c" == "$i" ]]; then read -r -p "Enter serial device path [/dev/ttyACM0]: " ACTIVE_SERIAL_PORT; ACTIVE_SERIAL_PORT="${ACTIVE_SERIAL_PORT:-/dev/ttyACM0}"; return; fi; done; }
ask_questions(){
 echo; echo "========================================"; echo " MeshTAK install configuration"; echo "========================================"; echo;
 read -r -p "Web UI port [443]: " WEB_PORT; WEB_PORT="${WEB_PORT:-443}";
 read -r -p "Device name [MeshTAK]: " DEVICE_NAME; DEVICE_NAME="${DEVICE_NAME:-MeshTAK}";
 echo; if [[ "$(prompt_yes_no 'Enable passive WM1303 collector?' 'y')" == y ]]; then PASSIVE_ENABLED=true; else PASSIVE_ENABLED=false; fi
 if [[ "$PASSIVE_ENABLED" == true ]]; then read -r -p "WM1303 SPI device [/dev/spidev0.0]: " SPI_DEV; SPI_DEV="${SPI_DEV:-/dev/spidev0.0}"; fi
 echo; if [[ "$(prompt_yes_no 'Enable active Meshtastic messaging radio (Heltec)?' 'y')" == y ]]; then ACTIVE_ENABLED=true; echo; echo "--- Active Meshtastic Connection Type ---"; echo "  1) Serial"; echo "  2) IP"; while true; do read -r -p "Choose connection type [1-2] [1]: " choice; choice="${choice:-1}"; case "$choice" in 1) ACTIVE_CONN_TYPE=serial; choose_serial; break;; 2) ACTIVE_CONN_TYPE=tcp; read -r -p "Meshtastic device IP/hostname: " ACTIVE_TCP_HOST; ACTIVE_TCP_HOST="${ACTIVE_TCP_HOST:-192.168.1.100}"; read -r -p "Meshtastic TCP port [4403]: " ACTIVE_TCP_PORT; ACTIVE_TCP_PORT="${ACTIVE_TCP_PORT:-4403}"; break;; esac; done; else ACTIVE_ENABLED=false; fi
 echo; if [[ "$(prompt_yes_no 'Enable TAK forwarding?' 'y')" == y ]]; then TAK_ENABLED=true; read -r -p "TAK host/IP [127.0.0.1]: " TAK_HOST; TAK_HOST="${TAK_HOST:-127.0.0.1}"; read -r -p "TAK port [8088]: " TAK_PORT; TAK_PORT="${TAK_PORT:-8088}"; read -r -p "TAK protocol tcp or udp [udp]: " TAK_PROTOCOL; TAK_PROTOCOL="${TAK_PROTOCOL:-udp}"; TAK_PROTOCOL="${TAK_PROTOCOL,,}"; [[ "$TAK_PROTOCOL" =~ ^(tcp|udp)$ ]] || TAK_PROTOCOL=udp; if [[ "$TAK_PROTOCOL" == tcp ]]; then if [[ "$(prompt_yes_no 'Use TLS for TAK TCP?' 'n')" == y ]]; then TAK_TLS=true; read -r -p "TAK CA cert path (optional): " TAK_CA_CERT; read -r -p "TAK client cert path (optional): " TAK_CLIENT_CERT; read -r -p "TAK client key path (optional): " TAK_CLIENT_KEY; fi; fi; fi
 echo; if [[ "$(prompt_yes_no 'Enable SPI/I2C on Raspberry Pi if raspi-config is present?' 'y')" == y ]]; then ENABLE_SPI_I2C=true; else ENABLE_SPI_I2C=false; fi
 OPEN_UFW=true
}
install_packages(){
  local packages=(
    python3
    python3-venv
    python3-pip
    python3-dev
    build-essential
    git
    rsync
    jq
    openssl
    ca-certificates
    unzip
    libsqlite3-dev
    libffi-dev
    pkg-config
    i2c-tools
    ufw
  )
  local newly_installed=()

  mkdir -p "$CONFIG_DIR"
  for pkg in "${packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
      newly_installed+=("$pkg")
    fi
  done

  apt-get update -y
  apt-get install -y "${packages[@]}"

  if (( ${#newly_installed[@]} > 0 )); then
    printf '%s\n' "${newly_installed[@]}" > "$PACKAGE_RECORD"
  else
    : > "$PACKAGE_RECORD"
  fi
}
enable_pi_interfaces(){ [[ "$ENABLE_SPI_I2C" == true ]] || return 0; if command -v raspi-config >/dev/null 2>&1; then raspi-config nonint do_spi 0 || true; raspi-config nonint do_i2c 0 || true; fi; }
install_passive_dependencies(){
  [[ "$PASSIVE_ENABLED" == true ]] || return 0
  if [[ -f /usr/local/lib/libloragw.so ]]; then
    log "libloragw.so already installed"
    return 0
  fi

  echo
  echo "Passive WM1303 collector requested. Building patched SX1302 HAL for libloragw.so."
  if HAL_BUILD_DIR="$HAL_BUILD_DIR" bash "$SCRIPT_DIR/scripts/install_libloragw.sh"; then
    return 0
  fi

  echo
  echo "WARNING: libloragw.so build failed. Passive WM1303 capture cannot start without it."
  if [[ -t 0 ]] && [[ "$(prompt_yes_no 'Disable passive WM1303 collector for this install and continue?' 'y')" == y ]]; then
      PASSIVE_ENABLED=false
  elif [[ ! -t 0 ]]; then
    echo "Non-interactive install: disabling passive WM1303 collector so the service can start."
    PASSIVE_ENABLED=false
  else
    echo "Leaving passive WM1303 enabled. The app will start in degraded mode until libloragw is installed."
  fi
}
ensure_groups(){ usermod -aG dialout "$RUN_USER" || true; usermod -aG spi "$RUN_USER" || true; usermod -aG i2c "$RUN_USER" || true; usermod -aG gpio "$RUN_USER" || true; }
prepare_dirs(){ mkdir -p "$APP_DIR" "$CONFIG_DIR" "$CERT_DIR" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/config"; }
copy_app(){ rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'venv' --exclude '*.pyc' --exclude '*.pyo' "$SCRIPT_DIR/" "$APP_DIR/"; mkdir -p "$CERT_DIR" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/config"; }
create_venv(){ rm -rf "$VENV_DIR"; python3 -m venv "$VENV_DIR"; "$VENV_DIR/bin/pip" install --upgrade pip wheel setuptools; "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"; }
generate_web_cert(){ mkdir -p "$CERT_DIR"; if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -keyout "$KEY_FILE" -out "$CERT_FILE" -subj "/C=US/ST=GA/L=Atlanta/O=MeshTAK/CN=$(hostname -f 2>/dev/null || hostname)"; chmod 600 "$KEY_FILE"; chmod 644 "$CERT_FILE"; fi; }
write_json_config(){ cat > "$JSON_CONFIG" <<EOF
{
  "meshtastic_active": {
    "enabled": ${ACTIVE_ENABLED},
    "connection": {"type": "${ACTIVE_CONN_TYPE}", "serial_port": "${ACTIVE_SERIAL_PORT}", "host": "${ACTIVE_TCP_HOST}", "port": ${ACTIVE_TCP_PORT}}
  },
  "tak": {"enabled": ${TAK_ENABLED}, "host": "${TAK_HOST}", "port": ${TAK_PORT}, "protocol": "${TAK_PROTOCOL}", "tls": ${TAK_TLS}, "ca_cert": "${TAK_CA_CERT}", "client_cert": "${TAK_CLIENT_CERT}", "client_key": "${TAK_CLIENT_KEY}"},
  "web": {"host": "0.0.0.0", "port": ${WEB_PORT}, "tls_cert": "${CERT_FILE}", "tls_key": "${KEY_FILE}"},
  "channels": [{"name": "Broadcast", "index": 0, "pinned": true}],
  "cot": {"type": "a-f-G-U-C", "team": "Orange", "role": "RTO"},
  "identity_policy": {"prefer_meshtastic_name_if_same_node_id": true, "allow_passive_only_tak_publish": true}
}
EOF
}
write_yaml_config(){ cat > "$YAML_CONFIG" <<EOF
radio:
  region: "US"
  frequency_mhz: 906.875
  spreading_factor: 11
  bandwidth_khz: 250.0
  coding_rate: "4/8"
  sync_word: 0x2B
  preamble_length: 16
  tx_power_dbm: 22
meshtastic:
  default_key_b64: "AQ=="
  channel_keys: {}
meshcore:
  default_key_b64: ""
  channel_keys: {}
capture:
  sources:
$( if [[ "$PASSIVE_ENABLED" == true ]]; then echo '    - "concentrator"'; fi )
  serial_port: null
  serial_baud: 115200
  concentrator_spi_device: "${SPI_DEV}"
  meshcore_usb:
    serial_port: null
    baud_rate: 115200
    auto_detect: false
storage:
  database_path: "data/concentrator.db"
  max_packets_retained: 100000
  cleanup_interval_seconds: 3600
dashboard:
  host: "0.0.0.0"
  port: ${WEB_PORT}
  static_dir: "frontend"
upstream:
  enabled: false
  url: ""
  reconnect_interval_seconds: 10
  buffer_max_size: 5000
  auth_token: null
device:
  device_name: "${DEVICE_NAME}"
  latitude: 0.0
  longitude: 0.0
  altitude: 25
  hardware_description: "WM1303 + Raspberry Pi 4"
  firmware_version: "1.0.0"
relay:
  enabled: false
  serial_port: null
  serial_baud: 115200
  max_relay_per_minute: 20
  burst_size: 5
  min_relay_rssi: -110.0
  max_relay_rssi: -50.0
mqtt:
  enabled: false
  broker: "mqtt.meshtastic.org"
  port: 1883
  username: "meshdev"
  password: "large4cats"
  topic_root: "msh"
  region: "US"
  publish_channels: ["LongFast"]
  publish_json: false
  location_precision: "exact"
  homeassistant_discovery: false
EOF
}
write_env_file(){ cat > "$ENV_FILE" <<EOF
PYTHONUNBUFFERED=1
CONCENTRATOR_CONFIG=${YAML_CONFIG}
EOF
}
write_service(){ cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MeshTAK Integrated Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/run_server.py
Restart=always
RestartSec=5
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal
SyslogIdentifier=meshtak

[Install]
WantedBy=multi-user.target
EOF
}
set_permissions(){ chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR" "$CONFIG_DIR"; }
reload_and_start(){ systemctl daemon-reload; systemctl enable meshtak.service; systemctl restart meshtak.service; }
open_firewall(){ [[ "$OPEN_UFW" == true ]] || return 0; command -v ufw >/dev/null 2>&1 && ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true; }
main(){ require_root; ask_questions; install_packages; enable_pi_interfaces; install_passive_dependencies; ensure_groups; prepare_dirs; copy_app; create_venv; generate_web_cert; write_json_config; write_yaml_config; write_env_file; write_service; set_permissions; reload_and_start; open_firewall; echo; systemctl status meshtak; echo ; echo "https://$(hostname -I | awk '{print $1}'):${WEB_PORT}"; }
main "$@"
