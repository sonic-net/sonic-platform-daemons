#!/bin/bash
#
# replace_psud_in_pmon.sh
#
# Replace the psud script inside the running pmon docker on a test device
# and restart the daemon. The original psud is backed up so it can be restored.
#
# Usage:
#   sudo bash replace_psud_in_pmon.sh <path_to_new_psud>
#   sudo bash replace_psud_in_pmon.sh --restore
#   sudo bash replace_psud_in_pmon.sh --help
#
# Notes:
#   - Must be run as root on the SONiC device.
#   - Operates on /usr/local/bin/psud inside the pmon container.
#   - Backup is kept at /tmp/psud.original.bak.
#

set -euo pipefail

PSUD_PATH_IN_PMON="/usr/local/bin/psud"
BACKUP_PATH="/tmp/psud.original.bak"
CONTAINER="pmon"

usage() {
    cat <<EOF
Usage:
  sudo bash $(basename "$0") <path_to_new_psud>
  sudo bash $(basename "$0") --restore
  sudo bash $(basename "$0") --help

Replace ${PSUD_PATH_IN_PMON} inside the ${CONTAINER} container with the
provided file and restart the psud daemon. The previous version is
saved to ${BACKUP_PATH} so it can be restored with --restore.
EOF
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERROR: must be run as root (use sudo)" >&2
        exit 1
    fi
}

require_container() {
    if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
        echo "ERROR: ${CONTAINER} container is not running" >&2
        exit 1
    fi
}

backup_original() {
    if [ ! -f "${BACKUP_PATH}" ]; then
        echo "  Backing up original psud to ${BACKUP_PATH}"
        docker cp "${CONTAINER}:${PSUD_PATH_IN_PMON}" "${BACKUP_PATH}"
    else
        echo "  Backup already exists at ${BACKUP_PATH} (keeping it)"
    fi
}

restart_psud() {
    echo "  Restarting psud in ${CONTAINER}..."
    docker exec "${CONTAINER}" supervisorctl restart psud
    sleep 3
    docker exec "${CONTAINER}" supervisorctl status psud
}

restore_original() {
    require_root
    require_container
    if [ ! -f "${BACKUP_PATH}" ]; then
        echo "ERROR: no backup found at ${BACKUP_PATH}" >&2
        exit 1
    fi
    echo "  Restoring original psud from ${BACKUP_PATH}"
    docker cp "${BACKUP_PATH}" "${CONTAINER}:${PSUD_PATH_IN_PMON}"
    docker exec "${CONTAINER}" chmod +x "${PSUD_PATH_IN_PMON}"
    restart_psud
    echo "Done. Original psud restored."
}

replace_psud() {
    local src="$1"
    require_root
    require_container

    if [ ! -f "${src}" ]; then
        echo "ERROR: source file not found: ${src}" >&2
        exit 1
    fi

    backup_original

    echo "  Copying ${src} -> ${CONTAINER}:${PSUD_PATH_IN_PMON}"
    docker cp "${src}" "${CONTAINER}:${PSUD_PATH_IN_PMON}"
    docker exec "${CONTAINER}" chmod +x "${PSUD_PATH_IN_PMON}"

    restart_psud
    echo "Done. New psud installed. To revert: sudo bash $(basename "$0") --restore"
}

main() {
    if [ $# -lt 1 ]; then
        usage
        exit 1
    fi

    case "$1" in
        -h|--help)
            usage
            ;;
        --restore)
            restore_original
            ;;
        *)
            replace_psud "$1"
            ;;
    esac
}

main "$@"
