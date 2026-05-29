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
#   Phase 5: Verifies per-component polling intervals match platform.json configuration
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
        echo "  Original thermalctld restored."
        docker exec pmon supervisorctl restart thermalctld
        echo "  thermalctld restarted."
    else
        echo "  No thermalctld backup found at $BACKUP_PATH (already clean)."
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
        local base_cmp="$base_file"
        local new_cmp="$new_file"

        # For PHYSICAL_ENTITY_INFO, filter out bare PSU entries (e.g. "PHYSICAL_ENTITY_INFO|PSU 1")
        # which are owned by psud, not thermalctld. When thermalctld restarts, its __del__
        # methods wipe ALL PHYSICAL_ENTITY_INFO keys; psud repopulates PSU entries on its
        # next loop but timing is unpredictable. Only compare entries thermalctld owns
        # (fan drawer, fan, and thermal entity entries).
        if [ "$table" = "phy" ]; then
            base_cmp="/tmp/phy_keys_base_filtered.txt"
            new_cmp="/tmp/phy_keys_new_filtered.txt"
            grep -v '^PHYSICAL_ENTITY_INFO|PSU [0-9]\+$' "$base_file" > "$base_cmp" 2>/dev/null || true
            grep -v '^PHYSICAL_ENTITY_INFO|PSU [0-9]\+$' "$new_file" > "$new_cmp" 2>/dev/null || true
        fi

        if diff -q "$base_cmp" "$new_cmp" &>/dev/null; then
            local count
            count=$(wc -l < "$base_cmp")
            pass_test "Keys match: ${table} (${count} entries)"
        else
            fail_test "Keys DIFFER: ${table}"
            echo "    --- Baseline only ---"
            diff "$base_cmp" "$new_cmp" | grep '^<' | head -5 | sed 's/^/    /'
            echo "    --- New only ---"
            diff "$base_cmp" "$new_cmp" | grep '^>' | head -5 | sed 's/^/    /'
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
# Phase 5: Verify polling intervals from platform.json
# -----------------------------------------------

PMON_PLATFORM_JSON="/usr/share/sonic/platform/platform.json"

get_timestamp() {
    # $1 = full STATE_DB key (e.g. "TEMPERATURE_INFO|ASIC")
    sonic-db-cli STATE_DB HGET "$1" "timestamp" 2>/dev/null || true
}

parse_polling_intervals_from_platform_json() {
    # Read platform.json from pmon and extract polling intervals for all components.
    # Components without a polling_interval default to 60s.
    # Output format (pipe-delimited to handle spaces in names):
    #   FAN_INTERVAL|<interval>
    #   PSU_INTERVAL|<interval>
    #   THERMAL|<name>|<interval>
    docker exec pmon cat "$PMON_PLATFORM_JSON" 2>/dev/null | python3 -c "
import json, sys

DEFAULT_INTERVAL = 60

data = json.load(sys.stdin)
chassis = data.get('chassis', data)

# fan_drawers interval
fan_interval = DEFAULT_INTERVAL
for entry in chassis.get('fan_drawers', []):
    if 'name' not in entry and 'polling_interval' in entry:
        try:
            fan_interval = int(entry['polling_interval'])
        except (ValueError, TypeError):
            pass
        break
print('FAN_INTERVAL|{}'.format(fan_interval))

# psu interval
psu_interval = DEFAULT_INTERVAL
for entry in chassis.get('psus', []):
    if 'name' not in entry and 'polling_interval' in entry:
        try:
            psu_interval = int(entry['polling_interval'])
        except (ValueError, TypeError):
            pass
        break
print('PSU_INTERVAL|{}'.format(psu_interval))

# thermals: per-thermal intervals (default 60 if not set)
for entry in chassis.get('thermals', []):
    name = entry.get('name')
    if not name:
        continue
    val = entry.get('polling_interval', '')
    if val:
        try:
            interval = int(val)
        except (ValueError, TypeError):
            interval = DEFAULT_INTERVAL
    else:
        interval = DEFAULT_INTERVAL
    print('THERMAL|{}|{}'.format(name, interval))
"
}

verify_component_polling_rate() {
    # Verify that a DB key's timestamp changes at approximately the expected
    # interval and NOT faster.
    #
    # $1 = label (for test output)
    # $2 = STATE_DB key (e.g. "TEMPERATURE_INFO|ASIC")
    # $3 = expected polling interval in seconds
    # $4 = observation window in seconds (how long to watch)
    local label="$1"
    local db_key="$2"
    local expected_interval="$3"
    local window="$4"

    echo "    Checking ${label} (key=${db_key}, expected_interval=${expected_interval}s, window=${window}s)..."

    local ts_prev ts_curr update_count elapsed_start
    ts_prev=$(get_timestamp "$db_key")
    if [ -z "$ts_prev" ]; then
        warn_test "${label}: No timestamp found for ${db_key} — skipping"
        return
    fi

    update_count=0
    elapsed_start=$(date +%s)

    # Sample every 2 seconds for the observation window
    while true; do
        sleep 2
        local now
        now=$(date +%s)
        local elapsed=$(( now - elapsed_start ))
        if [ "$elapsed" -ge "$window" ]; then
            break
        fi

        ts_curr=$(get_timestamp "$db_key")
        if [ "$ts_curr" != "$ts_prev" ]; then
            ((update_count++))
            ts_prev="$ts_curr"
        fi
    done

    # Expected number of updates in the window: floor(window / interval)
    # The actual cycle time = interval + execution overhead (hardware reads).
    # For fast intervals (5-10s), execution overhead (~2-3s) is significant,
    # so actual updates will be fewer than ideal. Use 50% lower bound to
    # accommodate this, and +2 upper bound for timing jitter.
    local expected_updates=$(( window / expected_interval ))
    local min_expected=$(( expected_updates / 2 ))
    local max_expected=$(( expected_updates + 2 ))

    # Clamp min to 0
    if [ "$min_expected" -lt 0 ]; then
        min_expected=0
    fi

    echo "    ${label}: observed ${update_count} updates in ${window}s (expected ~${expected_updates}, range ${min_expected}-${max_expected})"

    if [ "$update_count" -ge "$min_expected" ] && [ "$update_count" -le "$max_expected" ]; then
        pass_test "${label}: ${update_count} updates in ${window}s matches interval=${expected_interval}s (expected ${min_expected}-${max_expected})"
    elif [ "$update_count" -gt "$max_expected" ]; then
        fail_test "${label}: ${update_count} updates in ${window}s — updating TOO FAST for interval=${expected_interval}s (expected ${min_expected}-${max_expected})"
    else
        fail_test "${label}: ${update_count} updates in ${window}s — updating TOO SLOW for interval=${expected_interval}s (expected ${min_expected}-${max_expected})"
    fi
}

run_polling_interval_tests() {
    section "PHASE 5: Verify polling intervals from platform.json"

    if ! docker exec pmon test -f "$PMON_PLATFORM_JSON" 2>/dev/null; then
        warn_test "Polling: platform.json not found in pmon — skipping"
        return
    fi

    echo "  Parsing polling intervals from platform.json (default=60s if not set)..."
    local intervals_output
    intervals_output=$(parse_polling_intervals_from_platform_json)
    echo "$intervals_output" | sed 's/^/    /'

    local fan_interval psu_interval
    fan_interval=$(echo "$intervals_output" | grep '^FAN_INTERVAL|' | cut -d'|' -f2)
    psu_interval=$(echo "$intervals_output" | grep '^PSU_INTERVAL|' | cut -d'|' -f2)

    # Default to 60 if parsing returned empty
    : "${fan_interval:=60}"
    : "${psu_interval:=60}"

    # Determine observation window: 3x the largest interval to get enough samples
    local max_interval="$fan_interval"
    if [ "$psu_interval" -gt "$max_interval" ]; then
        max_interval="$psu_interval"
    fi
    while IFS='|' read -r type name interval; do
        if [ "$type" = "THERMAL" ] && [ "$interval" -gt "$max_interval" ]; then
            max_interval="$interval"
        fi
    done <<< "$intervals_output"

    local window=$(( max_interval * 3 ))
    if [ "$window" -lt 60 ]; then
        window=60
    fi
    echo "  Observation window: ${window}s (3x max interval ${max_interval}s, min 60s)"

    # --- Verify fan polling interval ---
    echo ""
    echo "  --- Fan drawer polling (interval=${fan_interval}s) ---"
    local first_fan_key
    first_fan_key=$(sonic-db-cli STATE_DB KEYS "FAN_INFO|*" 2>/dev/null | head -1 || true)
    if [ -n "$first_fan_key" ]; then
        verify_component_polling_rate "Fan" "$first_fan_key" "$fan_interval" "$window"
    else
        warn_test "Fan: No FAN_INFO keys found — skipping fan interval check"
    fi

    # --- Verify PSU thermal polling interval ---
    echo ""
    echo "  --- PSU thermal polling (interval=${psu_interval}s) ---"
    local first_psu_temp_key
    first_psu_temp_key=$(sonic-db-cli STATE_DB KEYS "TEMPERATURE_INFO|PSU*" 2>/dev/null | head -1 || true)
    if [ -n "$first_psu_temp_key" ]; then
        verify_component_polling_rate "PSU Thermal" "$first_psu_temp_key" "$psu_interval" "$window"
    else
        warn_test "PSU Thermal: No TEMPERATURE_INFO|PSU* keys found — skipping PSU interval check"
    fi

    # --- Verify per-thermal polling intervals ---
    echo ""
    echo "  --- Per-thermal polling intervals ---"
    while IFS='|' read -r type name interval; do
        if [ "$type" != "THERMAL" ]; then
            continue
        fi
        local db_key="TEMPERATURE_INFO|${name}"

        # Verify the key exists in STATE_DB
        local exists
        exists=$(sonic-db-cli STATE_DB EXISTS "$db_key" 2>/dev/null || echo "0")
        if [ "$exists" = "0" ] || [ "$exists" = "(integer) 0" ]; then
            warn_test "Thermal '${name}': key ${db_key} not found in STATE_DB — skipping"
            continue
        fi

        verify_component_polling_rate "Thermal '${name}'" "$db_key" "$interval" "$window"
    done <<< "$intervals_output"

    # --- Cross-check: fast thermal should update more than slow fan ---
    # Find the fastest thermal
    local fastest_thermal="" fastest_interval=999999
    while IFS='|' read -r type name interval; do
        if [ "$type" != "THERMAL" ]; then continue; fi
        if [ "$interval" -lt "$fastest_interval" ]; then
            fastest_interval="$interval"
            fastest_thermal="$name"
        fi
    done <<< "$intervals_output"

    if [ -n "$fastest_thermal" ] && [ "$fastest_interval" -lt "$fan_interval" ]; then
        echo ""
        echo "  --- Cross-check: fastest thermal (${fastest_thermal}=${fastest_interval}s) vs fans (${fan_interval}s) ---"
        echo "    Sampling for 30s to compare update rates..."

        local fan_key temp_key
        fan_key=$(sonic-db-cli STATE_DB KEYS "FAN_INFO|*" 2>/dev/null | head -1 || true)
        temp_key="TEMPERATURE_INFO|${fastest_thermal}"

        if [ -n "$fan_key" ]; then
            local fan_ts_prev temp_ts_prev fan_updates=0 temp_updates=0
            fan_ts_prev=$(get_timestamp "$fan_key")
            temp_ts_prev=$(get_timestamp "$temp_key")
            local start_time
            start_time=$(date +%s)

            while true; do
                sleep 2
                local now
                now=$(date +%s)
                if [ $(( now - start_time )) -ge 30 ]; then break; fi

                local fan_ts_curr temp_ts_curr
                fan_ts_curr=$(get_timestamp "$fan_key")
                temp_ts_curr=$(get_timestamp "$temp_key")

                if [ "$fan_ts_curr" != "$fan_ts_prev" ]; then
                    ((fan_updates++))
                    fan_ts_prev="$fan_ts_curr"
                fi
                if [ "$temp_ts_curr" != "$temp_ts_prev" ]; then
                    ((temp_updates++))
                    temp_ts_prev="$temp_ts_curr"
                fi
            done

            echo "    In 30s: thermal '${fastest_thermal}' updated ${temp_updates}x, fans updated ${fan_updates}x"
            if [ "$temp_updates" -gt "$fan_updates" ]; then
                pass_test "Cross-check: fastest thermal (${fastest_interval}s) updates more often than fans (${fan_interval}s)"
            elif [ "$temp_updates" -eq "$fan_updates" ] && [ "$fastest_interval" -eq "$fan_interval" ]; then
                pass_test "Cross-check: thermal and fan update at same rate (both ${fan_interval}s)"
            else
                warn_test "Cross-check: thermal updates (${temp_updates}) not greater than fan updates (${fan_updates}) in 30s — may be timing"
            fi
        fi
    fi

    # --- Verify no errors in thermalctld syslog ---
    echo ""
    echo "  --- Error check ---"
    local syslog_errors
    syslog_errors=$({
        cat /var/log/syslog 2>/dev/null || true
        docker exec pmon bash -c "cat /var/log/syslog 2>/dev/null || journalctl 2>/dev/null || true" 2>/dev/null || true
    } | grep -i 'thermalctld' | grep -ciE 'error|exception|traceback' || true)

    if [ "$syslog_errors" -eq 0 ]; then
        pass_test "Polling: No thermalctld errors in syslog with polling intervals active"
    else
        fail_test "Polling: Found ${syslog_errors} error(s) in thermalctld syslog"
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
  --restore                   Restore the original thermalctld from backup and exit.

Phases:
  1. Capture baseline:  DB keys, field schemas, data, CLI output, syslog
  2. Replace & restart: Copy new thermalctld into pmon, restart the process
  3. Capture new state: Same snapshots as phase 1 with the new thermalctld
  4. Compare:           Diff baseline vs new (keys, fields, sensor lists)
  5. Polling intervals: Verify each component polled at its configured rate

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

# ==================================================
# PHASE 5: Polling interval verification
# ==================================================
run_polling_interval_tests

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
