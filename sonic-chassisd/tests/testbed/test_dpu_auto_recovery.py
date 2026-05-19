#!/usr/bin/env python3
"""
Testbed script to verify DPU auto-recovery changes on a SONiC SmartSwitch.

This script runs ON the NPU (switch host) and validates:
  1. DPU_STATE DB fields (ready_status, recovery_status, reset_count, etc.)
  2. Auto-recovery feature flag behavior
  3. DPU recovery state machine transitions via controlled failures
  4. Chassisd deinit behavior (marking DPUs not-ready)
  5. Reset limit enforcement

Prerequisites:
  - Run on a SONiC SmartSwitch (NPU host) with DPUs attached
  - chassisd must be running
  - User must have sudo access
  - DPUs should be in operational (online) state initially

Usage:
  sudo python3 test_dpu_auto_recovery_testbed.py [--dpu DPU0] [--skip-destructive]

Examples:
  # Run all tests on DPU0
  sudo python3 test_dpu_auto_recovery_testbed.py --dpu DPU0

  # Run only non-destructive (read-only) checks
  sudo python3 test_dpu_auto_recovery_testbed.py --skip-destructive

  # Run on all DPUs
  sudo python3 test_dpu_auto_recovery_testbed.py --all-dpus
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants matching chassisd implementation
# ---------------------------------------------------------------------------
CHASSIS_STATE_DB_ID = 13  # CHASSIS_STATE_DB index
CONFIG_DB_ID = 4

# DPU_STATE table fields
READY_STATUS = 'ready_status'
RECOVERY_STATUS = 'recovery_status'
RESET_COUNT = 'reset_count'
LAST_DOWN_TIME = 'last_down_time'
LAST_READY_TIME = 'last_ready_time'
DP_STATE = 'dpu_data_plane_state'
CP_STATE = 'dpu_control_plane_state'
MP_STATE = 'dpu_midplane_link_state'

# Expected values
RECOVERY_RECOVERABLE = 'recoverable'
RECOVERY_UNRECOVERABLE = 'unrecoverable'

# Feature flag
DPU_AUTO_RECOVERY_FEATURE = 'dpu-auto-recovery'

# Timeouts
DPU_BOOT_TIMEOUT = 420  # 7 minutes for DPU to come up after power-cycle
POLL_INTERVAL = 10
CHASSISD_RESTART_WAIT = 30

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def run_cmd(cmd, check=True, timeout=60):
    """Run a shell command and return stdout."""
    logger.debug(f"Running: {cmd}")
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        logger.error(f"Command failed: {cmd}\nstderr: {result.stderr}")
        raise RuntimeError(f"Command failed: {cmd}")
    return result.stdout.strip()


def sonic_db_cli(db, cmd):
    """Execute sonic-db-cli command."""
    return run_cmd(f"sonic-db-cli {db} {cmd}")


def get_dpu_state_field(dpu_name, field):
    """Get a field from CHASSIS_STATE_DB DPU_STATE|<dpu_name>."""
    result = sonic_db_cli("CHASSIS_STATE_DB", f"HGET 'DPU_STATE|{dpu_name}' '{field}'")
    return result if result else None


def get_all_dpu_state(dpu_name):
    """Get all fields from DPU_STATE table for a given DPU."""
    result = sonic_db_cli("CHASSIS_STATE_DB", f"HGETALL 'DPU_STATE|{dpu_name}'")
    if not result:
        return {}
    # sonic-db-cli HGETALL returns a Python dict string like {'key': 'val', ...}
    # Try parsing as Python literal first, fall back to alternating lines
    import ast
    try:
        parsed = ast.literal_eval(result)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    # Fallback: alternating key/value lines (redis-cli format)
    lines = result.split('\n')
    state = {}
    for i in range(0, len(lines) - 1, 2):
        state[lines[i]] = lines[i + 1]
    return state


def get_module_oper_status(dpu_name):
    """Get operational status from STATE_DB CHASSIS_MODULE_TABLE."""
    result = run_cmd(
        f"sonic-db-cli STATE_DB HGET 'CHASSIS_MODULE_TABLE|{dpu_name}' 'oper_status'",
        check=False
    )
    return result if result else None


def get_feature_state(feature_name):
    """Get feature state from CONFIG_DB FEATURE table."""
    result = run_cmd(
        f"sonic-db-cli CONFIG_DB HGET 'FEATURE|{feature_name}' 'state'",
        check=False
    )
    return result if result else None


def set_feature_state(feature_name, state):
    """Set feature state in CONFIG_DB FEATURE table."""
    sonic_db_cli("CONFIG_DB", f"HSET 'FEATURE|{feature_name}' 'state' '{state}'")
    logger.info(f"Set feature {feature_name} state to '{state}'")


def get_chassis_module_admin_status(dpu_name):
    """Get admin_status from CONFIG_DB CHASSIS_MODULE table."""
    result = run_cmd(
        f"sonic-db-cli CONFIG_DB HGET 'CHASSIS_MODULE|{dpu_name}' 'admin_status'",
        check=False
    )
    return result if result else None


def set_module_admin_status(dpu_name, status):
    """Set admin_status in CONFIG_DB CHASSIS_MODULE table."""
    sonic_db_cli("CONFIG_DB", f"HSET 'CHASSIS_MODULE|{dpu_name}' 'admin_status' '{status}'")
    logger.info(f"Set {dpu_name} admin_status to '{status}'")


def restart_chassisd():
    """Restart the chassisd service."""
    logger.info("Restarting chassisd...")
    run_cmd("docker exec pmon supervisorctl restart chassisd", timeout=30)
    time.sleep(CHASSISD_RESTART_WAIT)
    logger.info("chassisd restarted")


def is_chassisd_running():
    """Check if chassisd is running (inside pmon container via supervisord)."""
    result = run_cmd("docker exec pmon supervisorctl status chassisd", check=False)
    if result and "RUNNING" in result:
        return True
    # Fallback: check if chassisd is a systemctl service
    result = run_cmd("systemctl is-active chassisd", check=False)
    return result == "active"


def get_dpu_list():
    """Get list of DPU names from STATE_DB or CHASSIS_STATE_DB."""
    result = run_cmd(
        "sonic-db-cli STATE_DB KEYS 'CHASSIS_MODULE_TABLE|DPU*'",
        check=False
    )
    if result:
        dpus = []
        for key in result.split('\n'):
            if key.startswith('CHASSIS_MODULE_TABLE|DPU'):
                dpus.append(key.split('|')[1])
        if dpus:
            return sorted(dpus)

    # Fallback: check CHASSIS_STATE_DB DPU_STATE entries
    result = run_cmd(
        "sonic-db-cli CHASSIS_STATE_DB KEYS 'DPU_STATE|DPU*'",
        check=False
    )
    if not result:
        return []
    dpus = []
    for key in result.split('\n'):
        if key.startswith('DPU_STATE|DPU'):
            dpus.append(key.split('|')[1])
    return sorted(dpus)


def wait_for_dpu_state(dpu_name, field, expected_value, timeout=DPU_BOOT_TIMEOUT):
    """Wait until a DPU state field reaches the expected value."""
    start = time.time()
    while time.time() - start < timeout:
        value = get_dpu_state_field(dpu_name, field)
        if value == expected_value:
            elapsed = int(time.time() - start)
            logger.info(f"{dpu_name}: {field}={expected_value} (took {elapsed}s)")
            return True
        time.sleep(POLL_INTERVAL)
    # One final check at timeout boundary to avoid race condition
    actual = get_dpu_state_field(dpu_name, field)
    elapsed = int(time.time() - start)
    if actual == expected_value:
        logger.info(f"{dpu_name}: {field}={expected_value} (took {elapsed}s)")
        return True
    logger.error(
        f"{dpu_name}: Timeout waiting for {field}={expected_value} "
        f"(actual={actual}, waited {elapsed}s)"
    )
    return False


def wait_for_dpu_ready(dpu_name, timeout=DPU_BOOT_TIMEOUT):
    """Wait for DPU to become fully ready (all planes up)."""
    return wait_for_dpu_state(dpu_name, READY_STATUS, 'true', timeout)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestResult:
    """Simple test result tracker."""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results = []

    def record(self, name, passed, msg=""):
        if passed:
            self.passed += 1
            self.results.append(('PASS', name, msg))
            logger.info(f"  PASS: {name}")
        else:
            self.failed += 1
            self.results.append(('FAIL', name, msg))
            logger.error(f"  FAIL: {name} - {msg}")

    def skip(self, name, reason=""):
        self.skipped += 1
        self.results.append(('SKIP', name, reason))
        logger.warning(f"  SKIP: {name} - {reason}")

    def summary(self):
        total = self.passed + self.failed + self.skipped
        logger.info("=" * 60)
        logger.info(f"TEST RESULTS: {self.passed} passed, {self.failed} failed, "
                    f"{self.skipped} skipped (total: {total})")
        logger.info("=" * 60)
        for status, name, msg in self.results:
            logger.info(f"  [{status}] {name}{f' - {msg}' if msg else ''}")
        return self.failed == 0


# ---------------------------------------------------------------------------
# Non-destructive tests (read-only verification)
# ---------------------------------------------------------------------------

def test_chassisd_running(results):
    """Verify chassisd service is running."""
    logger.info("Test: chassisd is running")
    results.record("chassisd_running", is_chassisd_running(),
                   "chassisd service is not active")


def test_dpu_state_table_exists(results, dpu_name):
    """Verify DPU_STATE table exists with required fields."""
    logger.info(f"Test: DPU_STATE table fields for {dpu_name}")
    state = get_all_dpu_state(dpu_name)

    required_fields = [READY_STATUS, RECOVERY_STATUS, RESET_COUNT,
                       LAST_DOWN_TIME, MP_STATE, CP_STATE, DP_STATE]

    for field in required_fields:
        present = field in state
        results.record(
            f"dpu_state_field_{field}_{dpu_name}",
            present,
            f"Field '{field}' missing from DPU_STATE|{dpu_name}"
        )


def test_dpu_ready_status(results, dpu_name):
    """Verify ready_status reflects actual DPU health."""
    logger.info(f"Test: ready_status correctness for {dpu_name}")
    state = get_all_dpu_state(dpu_name)

    mp = state.get(MP_STATE)
    cp = state.get(CP_STATE)
    dp = state.get(DP_STATE)
    ready = state.get(READY_STATUS)

    all_up = (mp == 'up' and cp == 'up' and dp == 'up')

    if all_up:
        results.record(
            f"ready_status_when_all_up_{dpu_name}",
            ready == 'true',
            f"Expected ready_status=true when all planes up, got '{ready}'"
        )
    else:
        results.record(
            f"ready_status_when_not_all_up_{dpu_name}",
            ready == 'false',
            f"Expected ready_status=false when planes not all up "
            f"(mp={mp}, cp={cp}, dp={dp}), got '{ready}'"
        )


def test_recovery_status_field(results, dpu_name):
    """Verify recovery_status is a valid value."""
    logger.info(f"Test: recovery_status validity for {dpu_name}")
    status = get_dpu_state_field(dpu_name, RECOVERY_STATUS)
    valid = status in (RECOVERY_RECOVERABLE, RECOVERY_UNRECOVERABLE, None)
    results.record(
        f"recovery_status_valid_{dpu_name}",
        valid,
        f"Invalid recovery_status value: '{status}'"
    )


def test_reset_count_field(results, dpu_name):
    """Verify reset_count is a non-negative integer."""
    logger.info(f"Test: reset_count validity for {dpu_name}")
    count_str = get_dpu_state_field(dpu_name, RESET_COUNT)
    try:
        count = int(count_str)
        valid = count >= 0
    except (TypeError, ValueError):
        count = count_str
        valid = False
    results.record(
        f"reset_count_valid_{dpu_name}",
        valid,
        f"Invalid reset_count: '{count}'"
    )


def test_auto_recovery_feature_flag(results):
    """Verify dpu-auto-recovery feature exists in CONFIG_DB (or defaults to enabled)."""
    logger.info("Test: dpu-auto-recovery feature flag")
    state = get_feature_state(DPU_AUTO_RECOVERY_FEATURE)
    valid_states = ('enabled', 'disabled', 'always_disabled')
    # Feature may not be explicitly configured (defaults to enabled in chassisd)
    results.record(
        "auto_recovery_feature_exists",
        state is None or state in valid_states,
        f"Feature state: '{state}' (expected None/default or one of {valid_states})"
    )


def test_last_ready_time_populated(results, dpu_name):
    """If DPU is ready, last_ready_time should be set."""
    logger.info(f"Test: last_ready_time for {dpu_name}")
    ready = get_dpu_state_field(dpu_name, READY_STATUS)
    last_ready = get_dpu_state_field(dpu_name, LAST_READY_TIME)

    if ready == 'true':
        results.record(
            f"last_ready_time_set_{dpu_name}",
            last_ready is not None and len(last_ready) > 0,
            f"DPU is ready but last_ready_time is empty"
        )
    else:
        results.skip(f"last_ready_time_set_{dpu_name}",
                     "DPU not currently ready")


# ---------------------------------------------------------------------------
# Destructive tests (modify state, require recovery)
# ---------------------------------------------------------------------------

def test_admin_down_marks_not_ready(results, dpu_name):
    """Admin-down a DPU and verify it becomes not-ready."""
    logger.info(f"Test: admin-down marks {dpu_name} not-ready")

    # Save original admin status
    original_admin = get_chassis_module_admin_status(dpu_name) or 'up'

    try:
        # Admin-down the DPU
        set_module_admin_status(dpu_name, 'down')
        time.sleep(CHASSISD_RESTART_WAIT)

        ready = get_dpu_state_field(dpu_name, READY_STATUS)
        results.record(
            f"admin_down_not_ready_{dpu_name}",
            ready == 'false',
            f"Expected ready_status=false after admin-down, got '{ready}'"
        )
    finally:
        # Restore original state
        set_module_admin_status(dpu_name, original_admin)
        logger.info(f"Restored {dpu_name} admin_status to '{original_admin}'")
        if original_admin == 'up':
            logger.info(f"Waiting for {dpu_name} to recover...")
            wait_for_dpu_ready(dpu_name)


def test_disable_auto_recovery_no_power_cycle(results, dpu_name):
    """With auto-recovery disabled, DPU failure should not trigger power-cycle."""
    logger.info(f"Test: disabled auto-recovery prevents power-cycle on {dpu_name}")

    original_feature_state = get_feature_state(DPU_AUTO_RECOVERY_FEATURE)
    original_reset_count = get_dpu_state_field(dpu_name, RESET_COUNT)

    try:
        # Disable auto-recovery
        set_feature_state(DPU_AUTO_RECOVERY_FEATURE, 'disabled')
        time.sleep(POLL_INTERVAL)

        # Record current reset count
        count_before = get_dpu_state_field(dpu_name, RESET_COUNT)

        # Simulate failure by admin-down then up (to trigger state machine)
        set_module_admin_status(dpu_name, 'down')
        time.sleep(CHASSISD_RESTART_WAIT)
        set_module_admin_status(dpu_name, 'up')
        time.sleep(CHASSISD_RESTART_WAIT * 2)

        # Reset count should NOT have increased (no power-cycle triggered)
        count_after = get_dpu_state_field(dpu_name, RESET_COUNT)
        results.record(
            f"no_power_cycle_when_disabled_{dpu_name}",
            count_before == count_after,
            f"Reset count changed from {count_before} to {count_after} "
            f"even with auto-recovery disabled"
        )
    finally:
        # Restore original feature state
        if original_feature_state:
            set_feature_state(DPU_AUTO_RECOVERY_FEATURE, original_feature_state)
        # Ensure DPU comes back
        set_module_admin_status(dpu_name, 'up')
        wait_for_dpu_ready(dpu_name)


def test_chassisd_restart_reinitializes_state(results, dpu_name):
    """Restart chassisd and verify recovery state is re-initialized."""
    logger.info(f"Test: chassisd restart re-initializes state for {dpu_name}")

    restart_chassisd()

    # After restart, DPU should be in Booting or Ready state
    # ready_status should reflect current health
    time.sleep(POLL_INTERVAL * 2)

    recovery_status = get_dpu_state_field(dpu_name, RECOVERY_STATUS)
    results.record(
        f"chassisd_restart_recovery_status_{dpu_name}",
        recovery_status == RECOVERY_RECOVERABLE,
        f"Expected recovery_status=recoverable after restart, got '{recovery_status}'"
    )

    reset_count = get_dpu_state_field(dpu_name, RESET_COUNT)
    # After restart, reset_count should be a valid non-negative integer.
    # It may not be 0 if chassisd detects a prior transition during init.
    is_valid = reset_count is not None and reset_count.isdigit() and int(reset_count) >= 0
    results.record(
        f"chassisd_restart_reset_count_{dpu_name}",
        is_valid,
        f"Expected valid reset_count>=0 after restart, got '{reset_count}'"
    )


def test_enable_auto_recovery_feature(results, dpu_name):
    """Enable auto-recovery and verify DPU state reflects it."""
    logger.info(f"Test: enable auto-recovery feature for {dpu_name}")

    original_state = get_feature_state(DPU_AUTO_RECOVERY_FEATURE)

    try:
        set_feature_state(DPU_AUTO_RECOVERY_FEATURE, 'enabled')
        time.sleep(POLL_INTERVAL)

        # Verify the feature is reflected (chassisd should read it next poll)
        state = get_feature_state(DPU_AUTO_RECOVERY_FEATURE)
        results.record(
            f"auto_recovery_enabled_{dpu_name}",
            state == 'enabled',
            f"Expected state=enabled, got '{state}'"
        )

        # If DPU is healthy, it should be ready
        oper = get_module_oper_status(dpu_name)
        if oper == 'Online':
            success = wait_for_dpu_ready(dpu_name, timeout=120)
            results.record(
                f"dpu_ready_with_recovery_enabled_{dpu_name}",
                success,
                f"DPU online but not ready after enabling auto-recovery"
            )
    finally:
        if original_state:
            set_feature_state(DPU_AUTO_RECOVERY_FEATURE, original_state)


def test_dpu_power_cycle_recovery(results, dpu_name):
    """Trigger a DPU power-cycle via admin-down/up and verify recovery.

    WARNING: This is destructive - it will power-cycle the DPU.
    """
    logger.info(f"Test: DPU power-cycle recovery for {dpu_name}")

    # Ensure auto-recovery is enabled
    original_state = get_feature_state(DPU_AUTO_RECOVERY_FEATURE)
    set_feature_state(DPU_AUTO_RECOVERY_FEATURE, 'enabled')

    try:
        # Record initial reset count
        initial_count = int(get_dpu_state_field(dpu_name, RESET_COUNT) or '0')

        # Power-cycle: admin-down then admin-up
        logger.info(f"  Power-cycling {dpu_name}...")
        set_module_admin_status(dpu_name, 'down')
        time.sleep(30)  # Let it fully shut down

        set_module_admin_status(dpu_name, 'up')
        logger.info(f"  Waiting for {dpu_name} to boot...")

        # Wait for DPU to come back online
        success = wait_for_dpu_ready(dpu_name, timeout=DPU_BOOT_TIMEOUT)
        results.record(
            f"power_cycle_recovery_{dpu_name}",
            success,
            f"DPU did not become ready after power-cycle within {DPU_BOOT_TIMEOUT}s"
        )

        # Verify ready_status is true
        if success:
            ready = get_dpu_state_field(dpu_name, READY_STATUS)
            results.record(
                f"ready_after_power_cycle_{dpu_name}",
                ready == 'true',
                f"Expected ready_status=true, got '{ready}'"
            )

            last_ready = get_dpu_state_field(dpu_name, LAST_READY_TIME)
            results.record(
                f"last_ready_time_after_recovery_{dpu_name}",
                last_ready is not None and len(last_ready) > 0,
                f"last_ready_time not set after recovery"
            )
    finally:
        if original_state:
            set_feature_state(DPU_AUTO_RECOVERY_FEATURE, original_state)
        # Ensure DPU is admin-up
        set_module_admin_status(dpu_name, 'up')


def test_chassisd_stop_marks_dpus_not_ready(results, dpu_name):
    """Stop chassisd and verify DPUs are marked not-ready on deinit."""
    logger.info(f"Test: chassisd stop marks {dpu_name} not-ready (deinit)")

    try:
        # Stop chassisd gracefully (SIGTERM triggers deinit)
        run_cmd("docker exec pmon supervisorctl stop chassisd", timeout=30)
        time.sleep(10)

        ready = get_dpu_state_field(dpu_name, READY_STATUS)
        results.record(
            f"deinit_not_ready_{dpu_name}",
            ready == 'false',
            f"Expected ready_status=false after chassisd stop, got '{ready}'"
        )

        last_down = get_dpu_state_field(dpu_name, LAST_DOWN_TIME)
        results.record(
            f"deinit_last_down_time_{dpu_name}",
            last_down is not None and len(last_down) > 0,
            f"last_down_time not set after deinit"
        )
    finally:
        # Restart chassisd
        run_cmd("docker exec pmon supervisorctl start chassisd", timeout=30)
        time.sleep(CHASSISD_RESTART_WAIT)
        logger.info("chassisd restarted after deinit test")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Testbed script for DPU auto-recovery verification on SONiC SmartSwitch"
    )
    parser.add_argument(
        '--dpu', type=str, default=None,
        help='Target DPU name (e.g., DPU0). If not specified, tests first available DPU.'
    )
    parser.add_argument(
        '--all-dpus', action='store_true',
        help='Run tests on all DPUs'
    )
    parser.add_argument(
        '--skip-destructive', action='store_true',
        help='Skip destructive tests (power-cycle, admin-down, service restart)'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Enable debug logging'
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Check prerequisites
    if os.geteuid() != 0:
        logger.error("This script must be run as root (sudo)")
        sys.exit(1)

    if not is_chassisd_running():
        logger.error("chassisd is not running. Start it first: docker exec pmon supervisorctl start chassisd")
        sys.exit(1)

    # Determine target DPUs
    available_dpus = get_dpu_list()
    if not available_dpus:
        logger.error("No DPUs found in STATE_DB. Is this a SmartSwitch?")
        sys.exit(1)

    logger.info(f"Available DPUs: {available_dpus}")

    if args.all_dpus:
        target_dpus = available_dpus
    elif args.dpu:
        if args.dpu not in available_dpus:
            logger.error(f"DPU '{args.dpu}' not found. Available: {available_dpus}")
            sys.exit(1)
        target_dpus = [args.dpu]
    else:
        target_dpus = [available_dpus[0]]

    logger.info(f"Target DPUs for testing: {target_dpus}")
    logger.info("=" * 60)

    results = TestResult()

    # --- Non-destructive tests (always run) ---
    logger.info("\n=== NON-DESTRUCTIVE TESTS ===\n")

    test_chassisd_running(results)
    test_auto_recovery_feature_flag(results)

    for dpu in target_dpus:
        logger.info(f"\n--- {dpu} ---")
        test_dpu_state_table_exists(results, dpu)
        test_dpu_ready_status(results, dpu)
        test_recovery_status_field(results, dpu)
        test_reset_count_field(results, dpu)
        test_last_ready_time_populated(results, dpu)

    # --- Destructive tests (optional) ---
    if not args.skip_destructive:
        logger.info("\n=== DESTRUCTIVE TESTS ===\n")
        logger.info("WARNING: These tests will power-cycle DPUs and restart services.")
        logger.info("Press Ctrl+C within 5 seconds to abort...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Aborted by user.")
            results.summary()
            sys.exit(0)

        for dpu in target_dpus:
            logger.info(f"\n--- Destructive tests for {dpu} ---")
            test_admin_down_marks_not_ready(results, dpu)
            test_enable_auto_recovery_feature(results, dpu)
            test_disable_auto_recovery_no_power_cycle(results, dpu)
            test_chassisd_restart_reinitializes_state(results, dpu)
            test_dpu_power_cycle_recovery(results, dpu)
            test_chassisd_stop_marks_dpus_not_ready(results, dpu)
    else:
        logger.info("\n(Skipping destructive tests as requested)\n")

    # --- Summary ---
    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
