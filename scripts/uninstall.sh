#!/usr/bin/env bash
#
# Meshpoint Uninstaller
#
# Removes the installed Meshpoint application, CLI, and systemd units.
# By default this keeps shared system components like the SX1302 HAL,
# the meshpoint system user, and Raspberry Pi boot config changes.
#
# Usage:
#   sudo ./scripts/uninstall.sh
#   sudo ./scripts/uninstall.sh --yes --purge-hal --purge-user
#
set -euo pipefail

MESHPOINT_DIR="/opt/meshpoint"
HAL_BUILD_DIR="/opt/sx1302_hal"
CLI_LINK="/usr/local/bin/meshpoint"
SERVICE_PATH="/etc/systemd/system/meshpoint.service"
WATCHDOG_PATH="/etc/systemd/system/network-watchdog.service"
SUDOERS_PATH="/etc/sudoers.d/meshpoint"
JOURNALD_PATH="/etc/systemd/journald.conf.d/meshpoint.conf"
UDEV_PATH="/etc/udev/rules.d/99-meshpoint-esp.rules"
LIBLORAGW_PATH="/usr/local/lib/libloragw.so"

ASSUME_YES=0
PURGE_HAL=0
PURGE_USER=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

usage() {
    cat <<'EOF'
Usage:
  sudo ./scripts/uninstall.sh [options]

Options:
  --yes         Skip the confirmation prompt
  --purge-hal   Also remove /opt/sx1302_hal and /usr/local/lib/libloragw.so
  --purge-user  Also remove the meshpoint system user
  --help        Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)
            ASSUME_YES=1
            ;;
        --purge-hal)
            PURGE_HAL=1
            ;;
        --purge-user)
            PURGE_USER=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "Unknown option: $1"
            ;;
    esac
    shift
done

if [[ $EUID -ne 0 ]]; then
    fail "This script must be run as root. Use: sudo ./scripts/uninstall.sh"
fi

echo ""
echo "This will remove the installed Meshpoint deployment:"
echo "  - ${MESHPOINT_DIR}"
echo "  - ${SERVICE_PATH}"
echo "  - ${WATCHDOG_PATH}"
echo "  - ${CLI_LINK}"
echo "  - ${SUDOERS_PATH}"
echo "  - ${JOURNALD_PATH}"
echo "  - ${UDEV_PATH}"
echo ""
echo "By default it keeps:"
echo "  - ${HAL_BUILD_DIR}"
echo "  - ${LIBLORAGW_PATH}"
echo "  - meshpoint system user"
echo "  - SPI/UART/I2C boot configuration changes"
echo ""

if [[ $ASSUME_YES -ne 1 ]]; then
    read -r -p "Proceed with uninstall? [y/N] " reply
    case "${reply}" in
        y|Y|yes|YES)
            ;;
        *)
            info "Uninstall cancelled."
            exit 0
            ;;
    esac
fi

info "Stopping systemd services..."
systemctl stop meshpoint 2>/dev/null || true
systemctl disable meshpoint 2>/dev/null || true
systemctl stop network-watchdog 2>/dev/null || true
systemctl disable network-watchdog 2>/dev/null || true

info "Removing systemd unit files..."
rm -f "${SERVICE_PATH}"
rm -f "${WATCHDOG_PATH}"
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

info "Removing CLI link and support files..."
rm -f "${CLI_LINK}"
rm -f "${SUDOERS_PATH}"
rm -f "${JOURNALD_PATH}"
rm -f "${UDEV_PATH}"

if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
fi

if systemctl list-unit-files systemd-journald.service >/dev/null 2>&1; then
    systemctl restart systemd-journald 2>/dev/null || true
fi

if [[ -d "${MESHPOINT_DIR}" ]]; then
    info "Removing installed application directory..."
    rm -rf "${MESHPOINT_DIR}"
else
    warn "Install directory not found: ${MESHPOINT_DIR}"
fi

if [[ $PURGE_HAL -eq 1 ]]; then
    info "Removing SX1302 HAL artifacts..."
    rm -rf "${HAL_BUILD_DIR}"
    rm -f "${LIBLORAGW_PATH}"
    ldconfig 2>/dev/null || true
else
    warn "Keeping HAL artifacts. Re-run with --purge-hal to remove them too."
fi

if [[ $PURGE_USER -eq 1 ]]; then
    if id -u meshpoint >/dev/null 2>&1; then
        info "Removing meshpoint system user..."
        userdel meshpoint 2>/dev/null || warn "Could not remove meshpoint user"
    fi
else
    warn "Keeping meshpoint system user. Re-run with --purge-user to remove it."
fi

echo ""
echo "==========================================="
echo "  Meshpoint uninstall complete"
echo "==========================================="
echo ""
echo "Kept by default:"
echo "  - SPI/UART/I2C boot settings"
echo "  - HAL build and libloragw unless --purge-hal was used"
echo "  - meshpoint user unless --purge-user was used"
echo ""
