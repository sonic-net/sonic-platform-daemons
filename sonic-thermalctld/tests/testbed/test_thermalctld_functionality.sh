#!/bin/bash
#
# test_thermalctld_functionality.sh
#
# End-to-end verification script for thermalctld refactoring on a physical testbed.
#
# This script:
#   Phase 1: Captures baseline state with the ORIGINAL thermalctld
#   Phase 2: Replaces thermalctld inside pmon with the new version and restarts
#   Phase 3: Captures state with the NEW thermalctld
#   Phase 4: Compares baseline vs new to verify identical behavior
#   Cleanup: Restores original thermalctld on failure (or with --restore flag)
#
# Usage:
#   sudo bash test_thermalctld_functionality.sh <path_to_new_thermalctld>
#   sudo bash test_thermalctld_functionality.sh --restore   # restore original backup
#   sudo bash test_thermalctld_functionality.sh --help       # show detailed help
#
# The new thermalctld file must be placed alongside this script or passed as argument.
#

set -uo pipefail

# -----------------------------------------------
# Globals
# -----------------------------------------------
PASS=0
FAIL=0
WARN=0
THERMALCTLD_PATH_IN_PMON="/usr/local/bin/thermalctld"
BACKUP_PATH="/tmp/thermalctld.original.bak"
SNAPSHOT_DIR="/tmp/thermalctld_verify"
BASELINE_DIR="${SNAPSHOT_DIR}/baseline"
NEW_DIR="${SNAPSHOT_DIR}/new"
WAIT_AFTER_RESTART=90  # seconds to wait after restart for data to populate

# -----------------------------------------------
# Helpers
# -----------------------------------------------
pass_test() {
    echo "  [PASS] $1"
    ((PASS++))
}

fail_test() {
    echo "  [FAIL] $1"
    ((FAIL++))
}

warn_test() {
    echo "  [WARN] $1"
    ((WARN++))
}

section() {
    echo ""
    echo "========================================"
    echo "  $1"
    echo "========================================"
}

restore_original() {
    echo "  Restoring original thermalctld from backup..."
    if [ -f "$BACKUP_PATH" ]; then
        docker cp "$BACKUP_PATH" "pmon:${THERMALCTLD_PATH_IN_PMON}"
        docker exec pmon supervisorctl restart thermalctld
        echo "  Original thermalctld restored and restarted."
    else
        echo "  WARNING: No backup found at $BACKUP_PATH. Cannot restore."
    fi
}

dump_table_keys() {
    # $1 = table pattern, $2 = output file
    sonic-db-cli STATE_DB KEYS "$1" 2>/dev/null | sort > "$2"
}

dump_table_data() {
    # $1 = table pattern, $2 = output file
    local keys
    keys=$(sonic-db-cli STATE_DB KEYS "$1" 2>/dev/null | sort)
    > "$2"
    for key in $keys; do
        echo "--- $key ---" >> "$2"
        sonic-db-cli STATE_DB HGETALL "$key" 2>/dev/null >> "$2"
        echo "" >> "$2"
    done
}

dump_table_fields() {
    # $1 = table pattern, $2 = output file
    # Dumps key + sorted field names only (no values, since values like temperature change)
    local keys
    keys=$(sonic-db-cli STATE_DB KEYS "$1" 2>/dev/null | sort)
    > "$2"
    for key in $keys; do
        # HGETALL returns a Python dict string; extract just the keys (field names)
        fields=$(sonic-db-cli STATE_DB HGETALL "$key" 2>/dev/null \
            | grep -oP "'([^']+)'\s*:" | sed "s/'//g; s/://" | sort | tr '\n' ' ')
        echo "$key: $fields" >> "$2"
    done
}

capture_snapshot() {
    # $1 = output directory
    local dir="$1"
    mkdir -p "$dir"

    echo "  Capturing DB keys..."
    dump_table_keys "TEMPERATURE_INFO|*" "${dir}/temp_keys.txt"
    dump_table_keys "FAN_INFO|*" "${dir}/fan_keys.txt"
    dump_table_keys "FAN_DRAWER_INFO|*" "${dir}/drawer_keys.txt"
    dump_table_keys "PHYSICAL_ENTITY_INFO|*" "${dir}/phy_keys.txt"

    echo "  Capturing DB field names..."
    dump_table_fields "TEMPERATURE_INFO|*" "${dir}/temp_fields.txt"
    dump_table_fields "FAN_INFO|*" "${dir}/fan_fields.txt"
    dump_table_fields "FAN_DRAWER_INFO|*" "${dir}/drawer_fields.txt"

    echo "  Capturing full DB data..."
    dump_table_data "TEMPERATURE_INFO|*" "${dir}/temp_data.txt"
    dump_table_data "FAN_INFO|*" "${dir}/fan_data.txt"
    dump_table_data "FAN_DRAWER_INFO|*" "${dir}/drawer_data.txt"

    echo "  Capturing 'show platform temperature' CLI output..."
    show platform temperature > "${dir}/cli_temperature.txt" 2>/dev/null || true
    # Extract just sensor names (skip header lines, strip all dynamic values)
    tail -n +3 "${dir}/cli_temperature.txt" | awk 'NF>0 {print $1}' | sort > "${dir}/cli_temp_sensors.txt"
    echo "  CLI sensors captured: $(wc -l < "${dir}/cli_temp_sensors.txt") sensors"

    echo "  Capturing thermalctld syslog..."
    docker exec pmon bash -c "cat /var/log/syslog 2>/dev/null || journalctl 2>/dev/null || true" \
        | grep -i 'thermalctld' | tail -50 > "${dir}/syslog.txt" 2>/dev/null || true

    # Count entries
    echo "  TEMPERATURE_INFO keys: $(wc -l < "${dir}/temp_keys.txt")"
    echo "  FAN_INFO keys:         $(wc -l < "${dir}/fan_keys.txt")"
    echo "  FAN_DRAWER_INFO keys:  $(wc -l < "${dir}/drawer_keys.txt")"
    echo "  PHYSICAL_ENTITY keys:  $(wc -l < "${dir}/phy_keys.txt")"
}

verify_snapshot() {
    # $1 = snapshot directory, $2 = label (e.g. "Baseline" or "New")
    local dir="$1"
    local label="$2"

    local temp_count fan_count drawer_count phy_count
    temp_count=$(wc -l < "${dir}/temp_keys.txt" 2>/dev/null || echo 0)
    fan_count=$(wc -l < "${dir}/fan_keys.txt" 2>/dev/null || echo 0)
    drawer_count=$(wc -l < "${dir}/drawer_keys.txt" 2>/dev/null || echo 0)
    phy_count=$(wc -l < "${dir}/phy_keys.txt" 2>/dev/null || echo 0)

    if [ "$temp_count" -gt 0 ]; then
        pass_test "${label}: TEMPERATURE_INFO has ${temp_count} entries"
    else
        fail_test "${label}: TEMPERATURE_INFO is empty"
    fi

    if [ "$fan_count" -gt 0 ]; then
        pass_test "${label}: FAN_INFO has ${fan_count} entries"
    else
        fail_test "${label}: FAN_INFO is empty"
    fi

    if [ "$drawer_count" -gt 0 ]; then
        pass_test "${label}: FAN_DRAWER_INFO has ${drawer_count} entries"
    else
        warn_test "${label}: FAN_DRAWER_INFO is empty (may be expected)"
    fi

    if [ "$phy_count" -gt 0 ]; then
        pass_test "${label}: PHYSICAL_ENTITY_INFO has ${phy_count} entries"
    else
        warn_test "${label}: PHYSICAL_ENTITY_INFO is empty"
    fi

    # Check for errors in syslog snapshot
    local error_count
    error_count=$(grep -iE 'error|exception|traceback' "${dir}/syslog.txt" 2>/dev/null | wc -l)
    if [ "$error_count" -eq 0 ]; then
        pass_test "${label}: No thermalctld errors in syslog"
    else
        fail_test "${label}: Found ${error_count} error(s) in thermalctld syslog"
        grep -iE 'error|exception|traceback' "${dir}/syslog.txt" | head -5 | sed 's/^/    /'
    fi

    # Validate fields exist in first temp entry
    if [ "$temp_count" -gt 0 ]; then
        local first_key
        first_key=$(head -1 "${dir}/temp_keys.txt")
        for field in temperature high_threshold low_threshold warning_status timestamp is_replaceable; do
            if grep -q "$field" "${dir}/temp_data.txt"; then
                pass_test "${label}: TEMPERATURE_INFO has field '$field'"
            else
                fail_test "${label}: TEMPERATURE_INFO missing field '$field'"
            fi
        done
    fi

    # Validate fields exist in first fan entry
    if [ "$fan_count" -gt 0 ]; then
        for field in presence status speed speed_target direction drawer_name is_replaceable timestamp; do
            if grep -q "$field" "${dir}/fan_data.txt"; then
                pass_test "${label}: FAN_INFO has field '$field'"
            else
                fail_test "${label}: FAN_INFO missing field '$field'"
            fi
        done
    fi
}

check_data_refreshing() {
    # $1 = label
    local label="$1"

    echo "  Checking that data is being refreshed (waiting 70s)..."
    local first_temp_key first_fan_key ts1_temp ts1_fan ts2_temp ts2_fan

    first_temp_key=$(sonic-db-cli STATE_DB KEYS "TEMPERATURE_INFO|*" 2>/dev/null | head -1 || true)
    first_fan_key=$(sonic-db-cli STATE_DB KEYS "FAN_INFO|*" 2>/dev/null | head -1 || true)

    ts1_temp=""
    ts1_fan=""
    [ -n "$first_temp_key" ] && ts1_temp=$(sonic-db-cli STATE_DB HGET "$first_temp_key" "timestamp" 2>/dev/null || true)
    [ -n "$first_fan_key" ] && ts1_fan=$(sonic-db-cli STATE_DB HGET "$first_fan_key" "timestamp" 2>/dev/null || true)

    sleep 70

    ts2_temp=""
    ts2_fan=""
    [ -n "$first_temp_key" ] && ts2_temp=$(sonic-db-cli STATE_DB HGET "$first_temp_key" "timestamp" 2>/dev/null || true)
    [ -n "$first_fan_key" ] && ts2_fan=$(sonic-db-cli STATE_DB HGET "$first_fan_key" "timestamp" 2>/dev/null || true)

    if [ -n "$ts1_temp" ] && [ -n "$ts2_temp" ]; then
        if [ "$ts1_temp" != "$ts2_temp" ]; then
            pass_test "${label}: Temperature timestamp updated ($ts1_temp -> $ts2_temp)"
        else
            fail_test "${label}: Temperature timestamp did NOT update after 70s"
        fi
    fi

    if [ -n "$ts1_fan" ] && [ -n "$ts2_fan" ]; then
        if [ "$ts1_fan" != "$ts2_fan" ]; then
            pass_test "${label}: Fan timestamp updated ($ts1_fan -> $ts2_fan)"
        else
            fail_test "${label}: Fan timestamp did NOT update after 70s"
        fi
    fi
}

compare_snapshots() {
    section "PHASE 4: Compare baseline vs new"

    # Compare keys (must be identical)
    for table in temp fan drawer phy; do
        local base_file="${BASELINE_DIR}/${table}_keys.txt"
        local new_file="${NEW_DIR}/${table}_keys.txt"

        if diff -q "$base_file" "$new_file" &>/dev/null; then
            local count
            count=$(wc -l < "$base_file")
            pass_test "Keys match: ${table} (${count} entries)"
        else
            fail_test "Keys DIFFER: ${table}"
            echo "    --- Baseline only ---"
            diff "$base_file" "$new_file" | grep '^<' | head -5 | sed 's/^/    /'
            echo "    --- New only ---"
            diff "$base_file" "$new_file" | grep '^>' | head -5 | sed 's/^/    /'
        fi
    done

    # Compare field schemas (field names per key, not values)
    for table in temp fan drawer; do
        local base_file="${BASELINE_DIR}/${table}_fields.txt"
        local new_file="${NEW_DIR}/${table}_fields.txt"

        if diff -q "$base_file" "$new_file" &>/dev/null; then
            pass_test "Field schemas match: ${table}"
        else
            fail_test "Field schemas DIFFER: ${table}"
            diff "$base_file" "$new_file" | head -10 | sed 's/^/    /'
        fi
    done

    # Compare 'show platform temperature' CLI sensor list
    if [ -s "${BASELINE_DIR}/cli_temp_sensors.txt" ] && [ -s "${NEW_DIR}/cli_temp_sensors.txt" ]; then
        if diff -q "${BASELINE_DIR}/cli_temp_sensors.txt" "${NEW_DIR}/cli_temp_sensors.txt" &>/dev/null; then
            cli_count=$(wc -l < "${BASELINE_DIR}/cli_temp_sensors.txt")
            pass_test "'show platform temperature' sensor list matches (${cli_count} sensors)"
        else
            fail_test "'show platform temperature' sensor list DIFFERS"
            diff "${BASELINE_DIR}/cli_temp_sensors.txt" "${NEW_DIR}/cli_temp_sensors.txt" | head -10 | sed 's/^/    /'
        fi
    else
        warn_test "'show platform temperature' CLI output not available for comparison"
    fi
}

list_sensors() {
    local label="$1"
    local dir="$2"

    echo ""
    echo "  --- ${label} Temperature Sensors ---"
    if [ -s "${dir}/temp_data.txt" ]; then
        grep -E '^\-\-\- |temperature|high_threshold|warning_status' "${dir}/temp_data.txt" | head -40 | sed 's/^/  /'
    else
        echo "  (none)"
    fi

    echo ""
    echo "  --- ${label} Fans ---"
    if [ -s "${dir}/fan_data.txt" ]; then
        grep -E '^\-\-\- |presence|speed|status|direction' "${dir}/fan_data.txt" | head -40 | sed 's/^/  /'
    else
        echo "  (none)"
    fi
}

# -----------------------------------------------
# Usage
# -----------------------------------------------
usage() {
    cat <<EOF
Usage: sudo bash $0 [OPTIONS] [<path_to_new_thermalctld>]

End-to-end verification script for thermalctld refactoring on a physical SONiC testbed.

This script captures a baseline snapshot with the original thermalctld, replaces it
with a new version, captures a second snapshot, and compares the two to verify
identical behavior.

Arguments:
  <path_to_new_thermalctld>   Path to the new thermalctld script to test.
                              If omitted, looks for 'thermalctld' in the same
                              directory as this script.

Options:
  -h, --help                  Show this help message and exit.
  --restore                   Restore the original thermalctld from backup
                              ($BACKUP_PATH) and exit.

Phases:
  1. Capture baseline:  DB keys, field schemas, data, CLI output, syslog
  2. Replace & restart: Copy new thermalctld into pmon, restart the process
  3. Capture new state: Same snapshots as phase 1 with the new thermalctld
  4. Compare:           Diff baseline vs new (keys, fields, sensor lists)

Outputs:
  Snapshots are saved to:     $SNAPSHOT_DIR
  Original backup is saved to: $BACKUP_PATH

Examples:
  sudo bash $0 ./thermalctld
  sudo bash $0 /tmp/thermalctld_new
  sudo bash $0 --restore
EOF
    exit 0
}

# -----------------------------------------------
# Main
# -----------------------------------------------

# Handle --help / -h first (before root check so help is always available)
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
fi

# Pre-checks
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

if ! command -v sonic-db-cli &>/dev/null; then
    echo "ERROR: sonic-db-cli not found. Is this a SONiC device?"
    exit 1
fi

# Handle --restore flag
if [ "${1:-}" = "--restore" ]; then
    section "RESTORE: Restoring original thermalctld"
    restore_original
    exit 0
fi

# Determine path to new thermalctld
NEW_THERMALCTLD="${1:-}"
if [ -z "$NEW_THERMALCTLD" ]; then
    # Look for it alongside this script
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "${SCRIPT_DIR}/thermalctld" ]; then
        NEW_THERMALCTLD="${SCRIPT_DIR}/thermalctld"
    else
        echo "ERROR: No new thermalctld file specified."
        echo "Usage: sudo bash $0 <path_to_new_thermalctld>"
        echo "   or: place the new 'thermalctld' file in the same directory as this script."
        exit 1
    fi
fi

if [ ! -f "$NEW_THERMALCTLD" ]; then
    echo "ERROR: File not found: $NEW_THERMALCTLD"
    exit 1
fi

echo "New thermalctld: $NEW_THERMALCTLD"

# Verify pmon is running
SERVICE_STATUS=$(systemctl is-active pmon 2>/dev/null || true)
if [ "$SERVICE_STATUS" != "active" ]; then
    echo "ERROR: pmon container is not running (status: $SERVICE_STATUS). Cannot proceed."
    exit 1
fi

THERMALCTLD_PID=$(docker exec pmon pgrep -f 'thermalctld' 2>/dev/null || true)
if [ -z "$THERMALCTLD_PID" ]; then
    echo "ERROR: thermalctld process is not running inside pmon."
    exit 1
fi
echo "pmon is running, thermalctld PID: $THERMALCTLD_PID"

# Clean up old snapshots
rm -rf "$SNAPSHOT_DIR"
mkdir -p "$BASELINE_DIR" "$NEW_DIR"

# ==================================================
# PHASE 1: Capture baseline with original thermalctld
# ==================================================
section "PHASE 1: Capture baseline (original thermalctld)"

echo "  Backing up original thermalctld..."
docker cp "pmon:${THERMALCTLD_PATH_IN_PMON}" "$BACKUP_PATH"
if [ -f "$BACKUP_PATH" ]; then
    pass_test "Original thermalctld backed up to $BACKUP_PATH"
else
    fail_test "Failed to backup original thermalctld"
    exit 1
fi

echo ""
echo "  Capturing baseline DB snapshot..."
capture_snapshot "$BASELINE_DIR"

echo ""
echo "  Validating baseline..."
verify_snapshot "$BASELINE_DIR" "Baseline"

echo ""
check_data_refreshing "Baseline"

list_sensors "Baseline" "$BASELINE_DIR"

# ==================================================
# PHASE 2: Replace thermalctld and restart
# ==================================================
section "PHASE 2: Replace thermalctld with new version"

echo "  Copying new thermalctld into pmon container..."
docker cp "$NEW_THERMALCTLD" "pmon:${THERMALCTLD_PATH_IN_PMON}"

echo "  Restarting thermalctld inside pmon..."
docker exec pmon supervisorctl restart thermalctld
sleep 5

NEW_PID=$(docker exec pmon pgrep -f 'thermalctld' 2>/dev/null || true)
if [ -n "$NEW_PID" ]; then
    pass_test "thermalctld restarted successfully (new PID: $NEW_PID)"
else
    fail_test "thermalctld FAILED to start after replacement"
    echo "  Restoring original..."
    restore_original
    exit 1
fi

echo "  Waiting ${WAIT_AFTER_RESTART}s for data to populate..."
sleep "$WAIT_AFTER_RESTART"

# ==================================================
# PHASE 3: Capture state with new thermalctld
# ==================================================
section "PHASE 3: Capture state (new thermalctld)"

capture_snapshot "$NEW_DIR"

echo ""
echo "  Validating new thermalctld output..."
verify_snapshot "$NEW_DIR" "New"

echo ""
check_data_refreshing "New"

list_sensors "New" "$NEW_DIR"

# ==================================================
# PHASE 4: Compare
# ==================================================
compare_snapshots

# -----------------------------------------------
# Summary
# -----------------------------------------------
section "SUMMARY"

TOTAL=$((PASS + FAIL + WARN))
echo "  Total: $TOTAL  |  PASS: $PASS  |  FAIL: $FAIL  |  WARN: $WARN"
echo ""
echo "  Snapshots saved in: $SNAPSHOT_DIR"
echo "  Original backup:    $BACKUP_PATH"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "  RESULT: FAILED - $FAIL test(s) failed"
    echo ""
    echo "  To restore original thermalctld, run:"
    echo "    sudo bash $0 --restore"
    exit 1
else
    echo "  RESULT: PASSED - new thermalctld produces identical output"
    echo ""
    echo "  The new thermalctld is currently running."
    echo "  To restore original, run: sudo bash $0 --restore"
    exit 0
fi
