#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/meshtak
CONFIG_DIR=/etc/meshtak
JSON_CONFIG=${CONFIG_DIR}/config.json
PACKAGE_RECORD=${CONFIG_DIR}/installed_packages.txt
SERVICE_FILE=/etc/systemd/system/meshtak.service
ENV_FILE=/etc/default/meshtak
HAL_BUILD_DIR=/opt/sx1302_hal
LIBLORAGW_FILE=/usr/local/lib/libloragw.so
SOURCE_DIR=/home/tdcadmin/meshtak
WEB_PORT_DEFAULT=443

PROJECT_PACKAGES=(
  python3-venv
  python3-pip
  python3-dev
  build-essential
  rsync
  jq
  unzip
  libffi-dev
  libjq1
  libonig5
  libsqlite3-dev
  pkg-config
  i2c-tools
  bluez
  bluetooth
  rfkill
  ufw
)

prompt_yes_no(){
  local p="$1" d="${2:-n}" a
  while true; do
    if [[ "$d" == y ]]; then
      read -r -p "$p [Y/n]: " a
      a="${a:-y}"
    else
      read -r -p "$p [y/N]: " a
      a="${a:-n}"
    fi
    case "${a,,}" in
      y|yes) echo y; return;;
      n|no) echo n; return;;
    esac
  done
}

require_root(){
  [[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }
}

remove_runtime(){
  systemctl stop meshtak.service 2>/dev/null || true
  systemctl disable meshtak.service 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$ENV_FILE"
  systemctl daemon-reload
  systemctl reset-failed meshtak.service 2>/dev/null || true

  rm -rf "$APP_DIR" "$CONFIG_DIR" "$HAL_BUILD_DIR"
  rm -f "$LIBLORAGW_FILE"
  ldconfig 2>/dev/null || true
}

load_recorded_packages(){
  if [[ -f "$PACKAGE_RECORD" ]]; then
    mapfile -t RECORDED_PACKAGES < <(grep -E '^[A-Za-z0-9+._-]+$' "$PACKAGE_RECORD" || true)
  else
    RECORDED_PACKAGES=()
  fi
}

remove_firewall_rule(){
  local port="${1:-$WEB_PORT_DEFAULT}"
  if command -v ufw >/dev/null 2>&1; then
    ufw delete allow "${port}/tcp" >/dev/null 2>&1 || true
  fi
}

detect_web_port(){
  local port=""
  if [[ -f "$JSON_CONFIG" ]]; then
    port="$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' "$JSON_CONFIG" | head -n 1 | grep -oE '[0-9]+' || true)"
  fi
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    port="$WEB_PORT_DEFAULT"
  fi
  echo "$port"
}

purge_project_packages(){
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get not found; skipping package purge."
    return 0
  fi

  local packages=("${RECORDED_PACKAGES[@]}")
  if (( ${#packages[@]} == 0 )); then
    packages=("${PROJECT_PACKAGES[@]}")
  fi

  local filtered_packages=()
  local package
  for package in "${packages[@]}"; do
    [[ "$package" == "git" ]] && continue
    filtered_packages+=("$package")
  done
  packages=("${filtered_packages[@]}")

  if (( ${#packages[@]} == 0 )); then
    echo "No recorded MeshTAK-installed apt packages to purge."
    return 0
  fi

  echo "Purging apt packages: ${packages[*]}"
  apt-get purge -y "${packages[@]}" || true
  apt-get autoremove -y --purge || true
}

main(){
  require_root

  echo "This will completely remove the installed MeshTAK runtime:"
  echo "  - ${APP_DIR}"
  echo "  - ${CONFIG_DIR}"
  echo "  - ${SERVICE_FILE}"
  echo "  - ${ENV_FILE}"
  echo "  - ${HAL_BUILD_DIR}"
  echo "  - ${LIBLORAGW_FILE}"
  echo "  - Web UI UFW allow rule"
  echo
  echo "It will NOT remove or modify ${SOURCE_DIR}."
  echo
  load_recorded_packages
  if (( ${#RECORDED_PACKAGES[@]} > 0 )); then
    echo "Recorded MeshTAK-installed apt packages: ${RECORDED_PACKAGES[*]}"
  else
    echo "No installed package record found; fallback purge targets: ${PROJECT_PACKAGES[*]}"
  fi
  echo "Package purge note: package removal can affect other software if those packages are shared."
  echo

  [[ "$(prompt_yes_no 'Continue with full MeshTAK uninstall?' 'n')" == y ]] || exit 0

  local web_port
  web_port="$(detect_web_port)"

  if [[ "$(prompt_yes_no 'Also purge MeshTAK apt packages?' 'y')" == y ]]; then
    purge_project_packages
  fi

  remove_runtime
  remove_firewall_rule "$web_port"

  echo
  echo "MeshTAK installed runtime removed. Source folder left untouched: ${SOURCE_DIR}"
}

main "$@"
