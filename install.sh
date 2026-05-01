#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/meshtak
CONFIG_DIR=/etc/meshtak
JSON_CONFIG=${CONFIG_DIR}/config.json
YAML_CONFIG=${APP_DIR}/config/local.yaml
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

# Defaults
WEB_PORT=443
DEVICE_NAME="MeshTAK"

RADIO_BACKEND="heltec_serial"

ACTIVE_CONN_TYPE=serial
ACTIVE_SERIAL_PORT=/dev/ttyACM0
ACTIVE_TCP_HOST=""
ACTIVE_TCP_PORT=4403
ACTIVE_BLE_ADDRESS=""
ACTIVE_BLE_PIN=""

M1_ENABLED=false
M1_SPI_DEV=/dev/spidev0.0

TAK_ENABLED=false
TAK_HOST=127.0.0.1
TAK_PORT=8088
TAK_PROTOCOL=udp

log(){ echo "[$(date '+%F %T')] $*"; }
require_root(){ [[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }; }

prompt_yes_no(){
    local p="$1" d="${2:-y}" a
    while true; do
        if [[ "$d" == y ]]; then read -r -p "$p [Y/n]: " a; a="${a:-y}"
        else read -r -p "$p [y/N]: " a; a="${a:-n}"; fi
        case "${a,,}" in y|yes) echo y; return;; n|no) echo n; return;; esac
    done
}

detect_serial(){
    for p in /dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*; do
        [[ -e "$p" ]] && echo "$p"
    done | awk '!seen[$0]++'
}

choose_serial(){
    mapfile -t devs < <(detect_serial)
    echo
    echo "--- Heltec Serial Device ---"

    if (( ${#devs[@]} == 0 )); then
        read -r -p "Enter device [/dev/ttyACM0]: " ACTIVE_SERIAL_PORT
        ACTIVE_SERIAL_PORT="${ACTIVE_SERIAL_PORT:-/dev/ttyACM0}"
        return
    fi

    local i=1
    for d in "${devs[@]}"; do
        echo "  ${i}) ${d}"
        ((i++))
    done

    while true; do
        read -r -p "Select device [1-${#devs[@]}]: " c
        if [[ "$c" =~ ^[0-9]+$ ]] && (( c>=1 && c<=${#devs[@]} )); then
            ACTIVE_SERIAL_PORT="${devs[$((c-1))]}"
            return
        fi
    done
}

ask_questions(){
    echo
    echo "========================================"
    echo " MeshTAK install configuration"
    echo "========================================"

    read -r -p "Web UI port [443]: " WEB_PORT
    WEB_PORT="${WEB_PORT:-443}"

    echo
    echo "--- Radio Backend ---"
    echo "  1) Heltec Serial"
    echo "  2) Heltec TCP/IP"
    echo "  3) Heltec BLE"
    echo "  4) SenseCAP M1 WM1303"

    read -r -p "Select backend [1-4]: " b
    b="${b:-1}"

    case "$b" in
        1)
            RADIO_BACKEND="heltec_serial"
            ACTIVE_CONN_TYPE=serial
            choose_serial
            ;;
        2)
            RADIO_BACKEND="heltec_tcp"
            ACTIVE_CONN_TYPE=tcp
            read -r -p "Meshtastic IP: " ACTIVE_TCP_HOST
            read -r -p "Port [4403]: " ACTIVE_TCP_PORT
            ACTIVE_TCP_PORT="${ACTIVE_TCP_PORT:-4403}"
            ;;
        3)
            RADIO_BACKEND="heltec_ble"
            ACTIVE_CONN_TYPE=ble
            read -r -p "BLE MAC: " ACTIVE_BLE_ADDRESS
            ;;
        4)
            RADIO_BACKEND="sensecap_m1"
            M1_ENABLED=true
            read -r -p "SPI device [/dev/spidev0.0]: " M1_SPI_DEV
            M1_SPI_DEV="${M1_SPI_DEV:-/dev/spidev0.0}"
            ;;
    esac

    echo
    if [[ "$(prompt_yes_no 'Enable TAK forwarding?' 'y')" == y ]]; then
        TAK_ENABLED=true
        read -r -p "TAK host [127.0.0.1]: " TAK_HOST
        TAK_HOST="${TAK_HOST:-127.0.0.1}"
    fi
}

install_packages(){
    apt-get update -y
    apt-get install -y \
        python3 python3-venv python3-pip \
        git build-essential \
        libffi-dev libsqlite3-dev \
        i2c-tools bluez bluetooth rfkill \
        ufw rsync
}

install_m1_hal(){
    [[ "$M1_ENABLED" == true ]] || return 0

    if [[ -f /usr/local/lib/libloragw.so ]]; then
        log "SX1302 HAL already installed"
        return
    fi

    bash "$SCRIPT_DIR/scripts/install_libloragw.sh"
}

prepare_dirs(){
    mkdir -p "$APP_DIR" "$CONFIG_DIR" "$CERT_DIR"
}

copy_app(){
    rsync -a --delete "$SCRIPT_DIR/" "$APP_DIR/"
}

create_venv(){
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
}

generate_cert(){
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 3650 \
        -subj "/CN=$(hostname)"
}

write_config(){
cat > "$JSON_CONFIG" <<EOF
{
  "radio": {
    "backend": "$RADIO_BACKEND",
    "heltec": {
      "type": "$ACTIVE_CONN_TYPE",
      "serial_port": "$ACTIVE_SERIAL_PORT",
      "host": "$ACTIVE_TCP_HOST",
      "port": $ACTIVE_TCP_PORT,
      "ble_address": "$ACTIVE_BLE_ADDRESS"
    },
    "sensecap_m1": {
      "enabled": $M1_ENABLED,
      "spi_device": "$M1_SPI_DEV",
      "tx_enabled": false
    }
  },
  "tak": {
    "enabled": $TAK_ENABLED,
    "host": "$TAK_HOST",
    "port": $TAK_PORT,
    "protocol": "$TAK_PROTOCOL"
  },
  "web": {
    "port": $WEB_PORT,
    "tls_cert": "$CERT_FILE",
    "tls_key": "$KEY_FILE"
  }
}
EOF
}

write_service(){
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MeshTAK
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/run_server.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
}

main(){
    require_root
    ask_questions
    install_packages
    install_m1_hal
    prepare_dirs
    copy_app
    create_venv
    generate_cert
    write_config
    write_service

    systemctl daemon-reload
    systemctl enable meshtak
    systemctl restart meshtak

    echo "Done."
}

main "$@"
