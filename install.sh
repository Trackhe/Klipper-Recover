#!/bin/bash
# Install print_recover into an existing Klipper + Moonraker (Mainsail/Fluidd) setup.
# Pattern follows community extras / Klipper-CustomShape-Bed.
set -e

KLIPPER_PATH="${HOME}/klipper"
KLIPPER_SERVICE_NAME=klipper
MOONRAKER_CONFIG_DIR="${HOME}/printer_data/config"
UNINSTALL=0

if [ ! -d "${MOONRAKER_CONFIG_DIR}" ]; then
    echo "\"${MOONRAKER_CONFIG_DIR}\" does not exist. Falling back to ${HOME}/klipper_config."
    MOONRAKER_CONFIG_DIR="${HOME}/klipper_config"
fi

usage() {
    echo "Usage: $0 [-k <klipper path>] [-s <klipper service>] [-c <moonraker config dir>] [-u]"
    echo "  -k  Klipper path          (default: ~/klipper)"
    echo "  -s  Klipper systemd name  (default: klipper)"
    echo "  -c  Moonraker config dir  (default: ~/printer_data/config)"
    echo "  -u  Uninstall (remove symlink)"
    exit 1
}

while getopts "k:s:c:uh" arg; do
    case $arg in
        k) KLIPPER_PATH=$OPTARG ;;
        s) KLIPPER_SERVICE_NAME=$OPTARG ;;
        c) MOONRAKER_CONFIG_DIR=$OPTARG ;;
        u) UNINSTALL=1 ;;
        h) usage ;;
        *) usage ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRCDIR="${SCRIPT_DIR}/src"
REPO_DIR="${SCRIPT_DIR}"
MODULE_NAME="print_recover"
UPDATE_SECTION="update_manager ${MODULE_NAME}"

verify_ready() {
    if [ "$EUID" -eq 0 ]; then
        echo "[ERROR] Do not run this script as root."
        exit 1
    fi
}

check_klipper() {
    if ! sudo systemctl list-units --full -all -t service --no-legend 2>/dev/null \
        | grep -Fq "${KLIPPER_SERVICE_NAME}.service"; then
        echo "[ERROR] Klipper service '${KLIPPER_SERVICE_NAME}' not found. Use -s to set the name."
        exit 1
    fi
    echo "Klipper service found: ${KLIPPER_SERVICE_NAME}"
}

check_folders() {
    if [ ! -d "${KLIPPER_PATH}/klippy/extras/" ]; then
        echo "[ERROR] Klipper extras not found at ${KLIPPER_PATH}/klippy/extras/"
        exit 1
    fi
    echo "Klipper installation: ${KLIPPER_PATH}"

    if [ ! -f "${MOONRAKER_CONFIG_DIR}/moonraker.conf" ]; then
        echo "[ERROR] moonraker.conf not found in ${MOONRAKER_CONFIG_DIR}"
        exit 1
    fi
    echo "Moonraker config: ${MOONRAKER_CONFIG_DIR}"

    if [ ! -f "${SRCDIR}/${MODULE_NAME}.py" ]; then
        echo "[ERROR] ${SRCDIR}/${MODULE_NAME}.py missing"
        exit 1
    fi
}

stop_klipper() {
    echo -n "Stopping Klipper... "
    sudo systemctl stop "${KLIPPER_SERVICE_NAME}"
    echo "[OK]"
}

start_klipper() {
    echo -n "Starting Klipper... "
    sudo systemctl start "${KLIPPER_SERVICE_NAME}"
    echo "[OK]"
}

restart_moonraker() {
    echo -n "Restarting Moonraker... "
    sudo systemctl restart moonraker
    echo "[OK]"
}

link_extension() {
    echo -n "Linking ${MODULE_NAME}.py into Klipper extras... "
    ln -sf "${SRCDIR}/${MODULE_NAME}.py" \
        "${KLIPPER_PATH}/klippy/extras/${MODULE_NAME}.py"
    echo "[OK]"
}

unlink_extension() {
    TARGET="${KLIPPER_PATH}/klippy/extras/${MODULE_NAME}.py"
    if [ -e "${TARGET}" ] || [ -L "${TARGET}" ]; then
        echo -n "Removing ${TARGET}... "
        rm -f "${TARGET}"
        echo "[OK]"
    else
        echo "${TARGET} not found — nothing to remove."
    fi
}

detect_origin() {
    ORIGIN="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"
    if [ -z "${ORIGIN}" ]; then
        ORIGIN="https://github.com/Trackhe/Klipper-Recover.git"
    fi
}

add_updater() {
    detect_origin
    CONF="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    echo -n "Adding [update_manager ${MODULE_NAME}] to moonraker.conf... "

    if grep -q "^\[update_manager ${MODULE_NAME}\]" "${CONF}"; then
        echo "[SKIPPED] already present"
        return
    fi

    {
        echo ""
        echo "# ${MODULE_NAME} — managed by install.sh"
        echo "[update_manager ${MODULE_NAME}]"
        echo "type: git_repo"
        echo "path: ${REPO_DIR}"
        echo "origin: ${ORIGIN}"
        echo "primary_branch: main"
        echo "is_system_service: False"
        echo "managed_services: klipper"
        echo "info_tags:"
        echo "  desc=Klipper print recovery (disconnect / power-loss resume)"
        echo ""
    } >> "${CONF}"

    echo "[OK]"
    restart_moonraker
}

print_next_steps() {
    echo ""
    echo "Installation complete."
    echo ""
    echo "Next steps:"
    echo "  1. Copy example config:"
    echo "       cp ${REPO_DIR}/config/print_recover.cfg.example \\"
    echo "          ${MOONRAKER_CONFIG_DIR}/print_recover.cfg"
    echo "       cp ${REPO_DIR}/config/print_recover_ratos.cfg \\"
    echo "          ${MOONRAKER_CONFIG_DIR}/print_recover_ratos.cfg"
    echo "  2. In printer.cfg add:"
    echo "       [include print_recover.cfg]"
    echo "       [include print_recover_ratos.cfg]   # RatOS"
    echo "  3. Ensure [virtual_sdcard] is configured (required for resume)."
    echo "  4. Slicer layer-change G-code:"
    echo "       RECOVER_LAYER LAYER=[layer_num] Z=[layer_z]"
    echo "  5. FIRMWARE_RESTART, then test: RECOVER_STATUS"
    echo ""
}

uninstall_msg() {
    echo ""
    echo "Symlink removed. Also:"
    echo "  - Remove [include print_recover.cfg] / [print_recover] from printer.cfg"
    echo "  - Remove the [update_manager ${MODULE_NAME}] block from moonraker.conf"
}

verify_ready
check_klipper
check_folders
stop_klipper

if [ "${UNINSTALL}" -eq 0 ]; then
    link_extension
    add_updater
    start_klipper
    print_next_steps
else
    unlink_extension
    start_klipper
    uninstall_msg
fi
