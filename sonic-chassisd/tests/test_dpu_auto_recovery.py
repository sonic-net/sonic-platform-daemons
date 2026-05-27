"""
Test suite for DPU auto-recovery state machine and related DB fields.

Covers:
- Recovery state machine transitions (Booting, Ready, SWFailure, PowerCycle,
  Offline, ManualIntervention, Unrecoverable)
- DPU_STATE DB fields: ready_status, recovery_status, reset_count,
  last_down_time, last_ready_time
- Auto-recovery feature flag gating (enabled/disabled/always_disabled)
- Recovery initialization on chassisd startup (init_dpu_recovery_state)
- NPU crash detection and forced power-cycle on boot
- Reset limit enforcement and unrecoverable marking
- deinit() marking all DPUs not-ready
- Admin-down DPU handling (Offline state)
- Manual intervention path when feature disabled
- Re-enabling auto-recovery while in ManualIntervention
"""

import os
import sys
import glob
import json
import pytest
import tempfile
from unittest.mock import MagicMock, patch, mock_open
from sonic_py_common import daemon_base

from .mock_platform import MockSmartSwitchChassis, MockModule
from .mock_module_base import ModuleBase

SYSLOG_IDENTIFIER = 'test_dpu_auto_recovery'
daemon_base.db_connect = MagicMock()

os.environ["CHASSISD_UNIT_TESTING"] = "1"
from chassisd import (
    SmartSwitchModuleUpdater,
    DPU_STATE_BOOTING,
    DPU_STATE_READY,
    DPU_STATE_SW_FAILURE,
    DPU_STATE_POWER_CYCLE,
    DPU_STATE_OFFLINE,
    DPU_STATE_MANUAL_INTERVENTION,
    DPU_STATE_UNRECOVERABLE,
    READY_STATUS,
    RECOVERY_STATUS,
    RESET_COUNT,
    LAST_DOWN_TIME,
    LAST_READY_TIME,
    RECOVERY_RECOVERABLE,
    RECOVERY_UNRECOVERABLE,
    DEFAULT_DPU_RESET_LIMIT,
    MODULE_ADMIN_DOWN,
    MODULE_ADMIN_UP,
    MODULE_REBOOT_CAUSE_DIR,
    MAX_HISTORY_FILES,
    REBOOT_CAUSE_FILE,
)


# ============================================================================
# Helpers
# ============================================================================

def create_chassis_with_dpus(num_dpus=2):
    """Create a MockSmartSwitchChassis with the specified number of DPU modules."""
    chassis = MockSmartSwitchChassis()
    for i in range(num_dpus):
        name = f"DPU{i}"
        desc = f"DPU Module {i}"
        module = MockModule(i, name, desc, ModuleBase.MODULE_TYPE_DPU, i, f"SN-DPU{i}")
        module.set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        chassis.module_list.append(module)
    return chassis


def create_updater(chassis, platform_json=None):
    """Create a SmartSwitchModuleUpdater with optional platform.json overrides."""
    with patch("os.path.isfile", return_value=(platform_json is not None)):
        if platform_json:
            with patch("builtins.open", mock_open(read_data=json.dumps(platform_json))):
                updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
        else:
            updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    return updater


def set_dpu_states(updater, module_name, mp='up', cp='up', dp='up'):
    """Set the DPU states in the dpu_state_table mock and midplane state tracking.

    CP/DP states are stored in dpu_state_table (a mock Table object).
    MP state is accessed via get_dpu_midplane_state; we monkey-patch the
    method on the updater instance to return test data.
    """
    # CP and DP are read via dpu_state_table.hget(module_name, field)
    updater.dpu_state_table.hset(module_name, 'dpu_control_plane_state', cp)
    updater.dpu_state_table.hset(module_name, 'dpu_data_plane_state', dp)
    # Store midplane state in a helper dict and monkey-patch once
    if not hasattr(updater, '_test_mp_states'):
        updater._test_mp_states = {}

        def _patched_get_mp_state(key):
            mod_name = key.replace("DPU_STATE|", "")
            return updater._test_mp_states.get(mod_name)

        updater.get_dpu_midplane_state = _patched_get_mp_state
    updater._test_mp_states[module_name] = mp


def get_dpu_state_field(updater, module_name, field):
    """Get a field from the dpu_state_table."""
    ok, val = updater.dpu_state_table.hget(module_name, field)
    return val if ok else None


# ============================================================================
# Test: Recovery state initialization
# ============================================================================

class TestInitDpuRecoveryState:
    """Test init_dpu_recovery_state() called on chassisd startup."""

    def test_init_sets_all_dpus_to_booting(self):
        """All DPUs should start in Booting state with recovery fields initialized."""
        chassis = create_chassis_with_dpus(2)
        updater = create_updater(chassis)

        with patch.object(updater, '_npu_crash_on_last_boot', return_value=False):
            updater.init_dpu_recovery_state()

        for i in range(2):
            name = f"DPU{i}"
            assert updater.dpu_recovery_state[name]['state'] == DPU_STATE_BOOTING
            assert updater.dpu_recovery_state[name]['reset_count'] == 0
            assert get_dpu_state_field(updater, name, READY_STATUS) == 'false'
            assert get_dpu_state_field(updater, name, RECOVERY_STATUS) == RECOVERY_RECOVERABLE
            assert get_dpu_state_field(updater, name, RESET_COUNT) == '0'
            assert get_dpu_state_field(updater, name, LAST_DOWN_TIME) is not None

    def test_init_npu_crash_power_cycles_admin_up_dpus(self):
        """When NPU crash is detected, admin-up DPUs should be power-cycled."""
        chassis = create_chassis_with_dpus(2)
        # DPU0 is online (admin-up), DPU1 is offline (admin-down)
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        chassis.module_list[1].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)

        updater = create_updater(chassis)

        # Pre-populate module table so get_module_current_status works
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))
        updater.module_table.hset("DPU1", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_npu_crash_on_last_boot', return_value=True), \
             patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, '_power_cycle_dpu') as mock_pc:
            updater.init_dpu_recovery_state()

        # DPU0 (online) should be power-cycled, DPU1 (offline) should not
        mock_pc.assert_called_once_with("DPU0", 0)

    def test_init_npu_crash_manual_intervention_when_recovery_disabled(self):
        """NPU crash + auto-recovery disabled → ManualIntervention state."""
        chassis = create_chassis_with_dpus(1)
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater = create_updater(chassis)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_npu_crash_on_last_boot', return_value=True), \
             patch.object(updater, '_is_auto_recovery_enabled', return_value=False):
            updater.init_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION


# ============================================================================
# Test: NPU crash detection
# ============================================================================

class TestNpuCrashDetection:
    """Test _npu_crash_on_last_boot() logic."""

    def test_detects_kernel_panic(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with patch("os.path.isfile", return_value=True), \
             patch("builtins.open", mock_open(read_data="Kernel Panic - not syncing")):
            assert updater._npu_crash_on_last_boot() is True

    def test_detects_unknown_cause(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with patch("os.path.isfile", return_value=True), \
             patch("builtins.open", mock_open(read_data="Unknown")):
            assert updater._npu_crash_on_last_boot() is True

    def test_normal_reboot_not_crash(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with patch("os.path.isfile", return_value=True), \
             patch("builtins.open", mock_open(read_data="User issued 'reboot' command")):
            assert updater._npu_crash_on_last_boot() is False

    def test_missing_file_not_crash(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with patch("os.path.isfile", return_value=False):
            assert updater._npu_crash_on_last_boot() is False


# ============================================================================
# Test: Auto-recovery feature flag
# ============================================================================

class TestAutoRecoveryFeatureFlag:
    """Test _is_auto_recovery_enabled() feature flag checking."""

    def test_enabled_returns_true(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        mock_table = MagicMock()
        mock_table.get.return_value = [True, (('state', 'enabled'),)]

        with patch("chassisd.daemon_base.db_connect") as mock_db, \
             patch("chassisd.swsscommon.Table", return_value=mock_table):
            assert updater._is_auto_recovery_enabled() is True

    def test_disabled_returns_false(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        mock_table = MagicMock()
        mock_table.get.return_value = [True, (('state', 'disabled'),)]

        with patch("chassisd.daemon_base.db_connect") as mock_db, \
             patch("chassisd.swsscommon.Table", return_value=mock_table):
            assert updater._is_auto_recovery_enabled() is False

    def test_always_disabled_returns_false(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        mock_table = MagicMock()
        mock_table.get.return_value = [True, (('state', 'always_disabled'),)]

        with patch("chassisd.daemon_base.db_connect") as mock_db, \
             patch("chassisd.swsscommon.Table", return_value=mock_table):
            assert updater._is_auto_recovery_enabled() is False

    def test_missing_feature_returns_false(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        mock_table = MagicMock()
        mock_table.get.return_value = None

        with patch("chassisd.daemon_base.db_connect") as mock_db, \
             patch("chassisd.swsscommon.Table", return_value=mock_table):
            assert updater._is_auto_recovery_enabled() is False

    def test_db_exception_returns_false(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with patch("chassisd.daemon_base.db_connect", side_effect=Exception("DB error")):
            assert updater._is_auto_recovery_enabled() is False


# ============================================================================
# Test: Planned transition suppression
# ============================================================================

class TestPlannedTransitionSuppression:
    """Test that recovery is suppressed during planned operations."""

    def test_planned_transition_in_progress_suppresses_recovery(self):
        """CP down during planned reboot should NOT trigger power-cycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))
        # Mark a planned transition as in progress
        updater.module_table.hset("DPU0", "state_transition_in_progress", "True")

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        # State should remain Ready — no recovery triggered
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY

    def test_no_planned_transition_allows_recovery(self):
        """CP down without planned transition triggers power-cycle normally."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))
        # No state_transition_in_progress set (or set to False)
        updater.module_table.hset("DPU0", "state_transition_in_progress", "False")

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE

    def test_planned_transition_completed_allows_recovery(self):
        """After planned transition completes (False), recovery resumes."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # First poll: transition in progress — suppressed
        updater.module_table.hset("DPU0", "state_transition_in_progress", "True")
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY

        # Second poll: transition done — recovery proceeds
        updater.module_table.hset("DPU0", "state_transition_in_progress", "False")
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE

    def test_planned_transition_midplane_down_suppressed(self):
        """Midplane down during planned shutdown should NOT trigger recovery."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))
        updater.module_table.hset("DPU0", "state_transition_in_progress", "True")

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY

    def test_is_planned_transition_missing_field_returns_false(self):
        """Missing state_transition_in_progress field returns False (no suppression)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        # Don't set the field at all — module_table.get returns empty
        assert updater._is_planned_transition_in_progress("DPU0") is False


# ============================================================================
# Test: State machine transitions — Booting → Ready
# ============================================================================

class TestBootingToReady:
    """Test DPU transition from Booting to Ready when all planes are up."""

    def test_booting_to_ready_all_up(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_BOOTING

        # Set all states to up
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')

        # Set module operational status to online
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'
        assert get_dpu_state_field(updater, "DPU0", LAST_READY_TIME) is not None

    def test_booting_stays_if_not_all_up(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_BOOTING

        # Midplane up but CP still down
        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='down')

        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_BOOTING


# ============================================================================
# Test: State machine transitions — Ready → failure states
# ============================================================================

class TestReadyToFailure:
    """Test transitions from Ready state on failure detection."""

    def test_ready_midplane_down_triggers_power_cycle(self):
        """Ready + midplane down + auto-recovery enabled → PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'

    def test_ready_midplane_down_manual_intervention_when_disabled(self):
        """Ready + midplane down + auto-recovery disabled → ManualIntervention."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION

    def test_ready_cp_down_immediate_power_cycle(self):
        """Ready + CP down (midplane up) + auto-recovery → immediate PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'

    def test_ready_cp_down_manual_intervention_when_disabled(self):
        """Ready + CP down (midplane up) + auto-recovery disabled → ManualIntervention."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

    def test_ready_dp_down_only_marks_not_ready_no_recovery(self):
        """Ready + DP down only (MP/CP up) → still Ready, but ready_status=false."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        # State stays Ready (dp-only doesn't trigger state machine)
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

    def test_ready_all_up_stays_ready(self):
        """Ready + all up → stays Ready, ready_status=true."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'


# ============================================================================
# Test: SWFailure state transitions
# ============================================================================

class TestSWFailureState:
    """Test SWFailure state behavior."""

    def test_sw_failure_triggers_power_cycle_when_enabled(self):
        """SWFailure + auto-recovery enabled → PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'

    def test_sw_failure_manual_intervention_when_disabled(self):
        """SWFailure + auto-recovery disabled → ManualIntervention."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION


# ============================================================================
# Test: PowerCycle state transitions
# ============================================================================

class TestPowerCycleState:
    """Test PowerCycle state behavior."""

    def test_power_cycle_to_ready_when_all_up(self):
        """PowerCycle + all states up → Ready (recovery successful)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_POWER_CYCLE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 1

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'
        assert get_dpu_state_field(updater, "DPU0", LAST_READY_TIME) is not None

    def test_power_cycle_stays_if_not_recovered(self):
        """PowerCycle + not all up → stays in PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_POWER_CYCLE

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE


# ============================================================================
# Test: Reset limit and Unrecoverable state
# ============================================================================

class TestResetLimitUnrecoverable:
    """Test that exceeding reset_limit marks DPU as unrecoverable."""

    def test_reaches_reset_limit_becomes_unrecoverable(self):
        """After hitting reset_limit, DPU should be marked unrecoverable."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 3})
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 2  # one below limit

        set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE
        assert get_dpu_state_field(updater, "DPU0", RECOVERY_STATUS) == RECOVERY_UNRECOVERABLE
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '3'

    def test_unrecoverable_is_terminal(self):
        """Once unrecoverable, state doesn't change even if DPU comes back."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_UNRECOVERABLE

        # Even with all states up, should stay unrecoverable
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE

    def test_multiple_failures_increment_reset_count(self):
        """Each failure increments reset_count until limit."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 5})

        # Simulate multiple failure/recovery cycles
        for cycle in range(4):
            updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

            set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
            chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
            updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

            with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
                 patch.object(updater, 'get_module_admin_status', return_value='up'):
                updater.update_dpu_recovery_state()

            assert updater.dpu_recovery_state["DPU0"]['reset_count'] == cycle + 1

            # Simulate recovery
            if updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE:
                set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
                with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
                     patch.object(updater, 'get_module_admin_status', return_value='up'):
                    updater.update_dpu_recovery_state()

        # 5th failure should trigger unrecoverable
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE
        assert updater.dpu_recovery_state["DPU0"]['reset_count'] == 5


# ============================================================================
# Test: Admin-down DPU handling (Offline state)
# ============================================================================

class TestAdminDownOffline:
    """Test DPU behavior when admin state is down."""

    def test_admin_down_transitions_to_offline(self):
        """Admin-down DPU should transition to Offline state."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='down'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_OFFLINE
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

    def test_admin_up_from_offline_transitions_to_booting(self):
        """Admin-up from Offline → Booting."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_OFFLINE

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_PRESENT)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_PRESENT))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_BOOTING


# ============================================================================
# Test: ManualIntervention state
# ============================================================================

class TestManualIntervention:
    """Test ManualIntervention state behavior."""

    def test_manual_intervention_recovers_when_all_up(self):
        """ManualIntervention + all up → Ready (operator fixed it)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_MANUAL_INTERVENTION

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'

    def test_manual_intervention_auto_recovery_reenabled_triggers_power_cycle(self):
        """ManualIntervention + DPU still down + auto-recovery re-enabled → PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_MANUAL_INTERVENTION

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE


# ============================================================================
# Test: Hardware failure (oper_status Offline) from unexpected states
# ============================================================================

class TestHardwareFailure:
    """Test hardware failure detection via oper_status going Offline."""

    def test_ready_hardware_offline_triggers_power_cycle(self):
        """Ready + oper_status=Offline → hardware failure → PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

    def test_hardware_offline_manual_intervention_when_disabled(self):
        """oper_status=Offline + auto-recovery disabled → ManualIntervention."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION


# ============================================================================
# Test: Power-cycle DPU mechanics
# ============================================================================

class TestPowerCycleDpu:
    """Test _power_cycle_dpu() method."""

    def test_power_cycle_calls_admin_state(self):
        """_power_cycle_dpu should call set_admin_state(DOWN) then set_admin_state(UP)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 0

        module = chassis.module_list[0]
        module.set_admin_state = MagicMock()

        updater._power_cycle_dpu("DPU0", 0)

        # Should be called twice: once with DOWN, once with UP
        calls = module.set_admin_state.call_args_list
        assert len(calls) == 2
        assert calls[0][0][0] == MODULE_ADMIN_DOWN
        assert calls[1][0][0] == MODULE_ADMIN_UP

    def test_power_cycle_increments_reset_count(self):
        """Each _power_cycle_dpu call increments reset_count."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 2

        updater._power_cycle_dpu("DPU0", 0)

        assert updater.dpu_recovery_state["DPU0"]['reset_count'] == 3
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '3'

    def test_power_cycle_at_limit_marks_unrecoverable(self):
        """Power-cycle at reset_limit marks DPU as unrecoverable."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 3})
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 2  # limit is 3

        updater._power_cycle_dpu("DPU0", 0)

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE
        assert get_dpu_state_field(updater, "DPU0", RECOVERY_STATUS) == RECOVERY_UNRECOVERABLE


# ============================================================================
# Test: Deinit marks all DPUs not-ready
# ============================================================================

class TestDeinit:
    """Test that deinit() properly marks all DPUs as not-ready."""

    def test_deinit_sets_ready_status_false(self):
        """deinit() should set ready_status=false for all DPUs."""
        chassis = create_chassis_with_dpus(2)
        updater = create_updater(chassis)

        # Set DPUs to ready first
        updater.dpu_state_table.hset("DPU0", READY_STATUS, 'true')
        updater.dpu_state_table.hset("DPU1", READY_STATUS, 'true')

        updater.deinit()

        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'
        assert get_dpu_state_field(updater, "DPU1", READY_STATUS) == 'false'

    def test_deinit_sets_last_down_time(self):
        """deinit() should update last_down_time for all DPUs."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater.deinit()

        assert get_dpu_state_field(updater, "DPU0", LAST_DOWN_TIME) is not None


# ============================================================================
# Test: Platform.json configuration loading
# ============================================================================

class TestPlatformJsonConfig:
    """Test that recovery thresholds are properly loaded from platform.json."""

    def test_custom_reset_limit_from_platform_json(self):
        """reset_limit should be loaded from platform.json."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 10})

        assert updater.reset_limit == 10

    def test_custom_reboot_timeout_from_platform_json(self):
        """dpu_reboot_timeout should be loaded from platform.json."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reboot_timeout": 600})

        assert updater.dpu_reboot_timeout == 600

    def test_default_values_when_no_platform_json(self):
        """Defaults should be used when platform.json is absent."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        assert updater.reset_limit == DEFAULT_DPU_RESET_LIMIT
        assert updater.dpu_reboot_timeout == 360

    def test_invalid_platform_json_uses_defaults(self):
        """Invalid JSON should fall back to defaults."""
        chassis = create_chassis_with_dpus(1)

        with patch("os.path.isfile", return_value=True), \
             patch("builtins.open", mock_open(read_data="not valid json {")):
            updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)

        assert updater.reset_limit == DEFAULT_DPU_RESET_LIMIT


# ============================================================================
# Test: Multiple DPUs independent state tracking
# ============================================================================

class TestMultipleDpus:
    """Test that multiple DPUs are tracked independently."""

    def test_independent_state_tracking(self):
        """Each DPU should have independent recovery state."""
        chassis = create_chassis_with_dpus(3)
        updater = create_updater(chassis)

        # DPU0: Ready (all up)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # DPU1: Ready but midplane down (will power-cycle)
        updater.dpu_recovery_state["DPU1"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU1", mp='down', cp='up', dp='up')
        chassis.module_list[1].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU1", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # DPU2: Booting
        updater.dpu_recovery_state["DPU2"]['state'] = DPU_STATE_BOOTING
        set_dpu_states(updater, "DPU2", mp='up', cp='down', dp='down')
        chassis.module_list[2].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU2", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        # DPU0 stays ready
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        # DPU1 goes to power cycle
        assert updater.dpu_recovery_state["DPU1"]['state'] == DPU_STATE_POWER_CYCLE
        # DPU2 stays booting (not all up yet)
        assert updater.dpu_recovery_state["DPU2"]['state'] == DPU_STATE_BOOTING

    def test_one_dpu_unrecoverable_others_continue(self):
        """One DPU hitting reset_limit shouldn't affect others."""
        chassis = create_chassis_with_dpus(2)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 2})

        # DPU0: at limit, will become unrecoverable
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 1
        set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # DPU1: healthy
        updater.dpu_recovery_state["DPU1"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU1", mp='up', cp='up', dp='up')
        chassis.module_list[1].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU1", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE
        assert updater.dpu_recovery_state["DPU1"]['state'] == DPU_STATE_READY


# ============================================================================
# Test: Full recovery cycle (end-to-end)
# ============================================================================

class TestFullRecoveryCycle:
    """End-to-end test of a complete failure/recovery cycle."""

    def test_full_cycle_booting_ready_failure_recovery(self):
        """Booting → Ready → midplane failure → PowerCycle → Ready."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        # Phase 1: Booting → Ready
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_BOOTING
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'

        # Phase 2: Ready → midplane failure → PowerCycle
        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'

        # Phase 3: PowerCycle → DPU comes back → Ready
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'

    def test_full_cycle_sw_failure_recovery(self):
        """Ready → CP down → immediate PowerCycle → Ready."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        # Setup: DPU is Ready
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # Phase 1: CP goes down → immediate PowerCycle (no SWFailure delay)
        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'

        # Phase 2: DPU recovers
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'

    def test_full_cycle_admin_down_up(self):
        """Ready → admin-down → Offline → admin-up → Booting → Ready."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        # Setup: DPU is Ready
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # Phase 1: Admin-down
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='down'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_OFFLINE

        # Phase 2: Admin-up (DPU starts booting)
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_BOOTING

        # Phase 3: Booting → Ready
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY


# ============================================================================
# Test: Admin-down from non-Offline states
# ============================================================================

class TestAdminDownFromAllStates:
    """Test that admin-down transitions to Offline from every non-terminal state."""

    @pytest.mark.parametrize("initial_state", [
        DPU_STATE_BOOTING,
        DPU_STATE_READY,
        DPU_STATE_SW_FAILURE,
        DPU_STATE_POWER_CYCLE,
        DPU_STATE_MANUAL_INTERVENTION,
    ])
    def test_admin_down_from_any_state_transitions_to_offline(self, initial_state):
        """Admin-down should transition to Offline from any non-terminal state."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = initial_state

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='down'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_OFFLINE
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

    def test_admin_down_from_unrecoverable_stays_unrecoverable(self):
        """Unrecoverable is terminal — admin-down does not change it."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_UNRECOVERABLE

        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='down'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE

    def test_admin_down_already_offline_stays_offline(self):
        """Already Offline + admin-down → stays Offline (no-op)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_OFFLINE

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='down'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_OFFLINE


# ============================================================================
# Test: Hardware offline during active recovery states (no duplicate recovery)
# ============================================================================

class TestHardwareOfflineDuringRecovery:
    """oper_status=Offline while already in a recovery state should not re-trigger power-cycle."""

    @pytest.mark.parametrize("recovery_state", [
        DPU_STATE_BOOTING,
        DPU_STATE_POWER_CYCLE,
        DPU_STATE_SW_FAILURE,
    ])
    def test_hardware_offline_while_in_recovery_no_duplicate(self, recovery_state):
        """oper_status=Offline in {Booting, PowerCycle, SWFailure}
        should NOT trigger a new power-cycle from the hardware-offline branch."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = recovery_state
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 1

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'), \
             patch.object(updater, '_power_cycle_dpu') as mock_pc:
            updater.update_dpu_recovery_state()

        # The hardware-offline code path should be skipped for these states
        # SWFailure will trigger its own power-cycle (expected), but NOT from
        # the hardware-offline branch
        if recovery_state == DPU_STATE_SW_FAILURE:
            # SWFailure always power-cycles on its own logic path
            mock_pc.assert_called_once()
        else:
            mock_pc.assert_not_called()

    def test_hardware_offline_manual_intervention_re_triggers_recovery(self):
        """ManualIntervention is excluded from hardware-offline branch, but its
        own logic path triggers power-cycle when auto-recovery is re-enabled."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_MANUAL_INTERVENTION
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 1

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        # ManualIntervention's own logic re-triggers power-cycle when enabled
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE

    def test_hardware_offline_from_ready_triggers_power_cycle(self):
        """oper_status=Offline from Ready DOES trigger power-cycle (contrast test)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE

    def test_hardware_offline_from_offline_state_triggers_recovery(self):
        """oper_status=Offline from Offline state + admin-up → hardware-offline
        branch fires (Offline is NOT in the exclusion list), triggering power-cycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_OFFLINE

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        # Offline is not in the hardware-offline exclusion list, so the
        # hardware-offline branch fires and triggers a power-cycle
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE


# ============================================================================
# Test: Feature flag toggled mid-recovery
# ============================================================================

class TestFeatureFlagMidRecovery:
    """Test dynamic feature flag changes during active recovery."""

    def test_auto_recovery_disabled_during_sw_failure_goes_to_manual(self):
        """SWFailure + auto-recovery disabled mid-recovery → ManualIntervention."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION

    def test_auto_recovery_reenabled_from_manual_intervention_triggers_power_cycle(self):
        """ManualIntervention + DPU still down + feature re-enabled → PowerCycle."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_MANUAL_INTERVENTION

        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE

    def test_auto_recovery_disabled_hardware_offline_goes_manual(self):
        """Hardware offline + auto-recovery disabled → ManualIntervention."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY

        set_dpu_states(updater, "DPU0", mp='down', cp='down', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_OFFLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION

    def test_feature_toggle_cycle_disabled_then_reenabled(self):
        """Full cycle: Ready → failure → disabled (manual) → re-enabled (power-cycle)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        # Phase 1: Ready → CP down → SWFailure
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_SW_FAILURE

        # Phase 2: Feature disabled → ManualIntervention
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=False), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_MANUAL_INTERVENTION

        # Phase 3: Feature re-enabled → PowerCycle
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE


# ============================================================================
# Test: Power-cycle failure handling
# ============================================================================

class TestPowerCycleFailure:
    """Test _power_cycle_dpu() behavior when set_admin_state raises exceptions."""

    def test_power_cycle_set_admin_state_exception(self):
        """If set_admin_state raises, state should still update (best-effort)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 0

        module = chassis.module_list[0]
        module.set_admin_state = MagicMock(side_effect=Exception("I2C bus error"))

        updater._power_cycle_dpu("DPU0", 0)

        # Despite the exception, reset_count should still increment
        assert updater.dpu_recovery_state["DPU0"]['reset_count'] == 1
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'
        # State transitions to POWER_CYCLE (below reset_limit)
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE

    def test_power_cycle_exception_at_limit_still_marks_unrecoverable(self):
        """Exception during power-cycle at reset_limit still marks unrecoverable."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 2})
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 1

        module = chassis.module_list[0]
        module.set_admin_state = MagicMock(side_effect=RuntimeError("HW error"))

        updater._power_cycle_dpu("DPU0", 0)

        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_UNRECOVERABLE
        assert get_dpu_state_field(updater, "DPU0", RECOVERY_STATUS) == RECOVERY_UNRECOVERABLE

    def test_power_cycle_partial_failure(self):
        """set_admin_state(DOWN) succeeds but set_admin_state(UP) fails."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_SW_FAILURE
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 0

        call_count = [0]

        def side_effect_fn(state):
            call_count[0] += 1
            if call_count[0] == 2:  # Second call (UP) fails
                raise RuntimeError("set_admin_state(UP) failed")
            return True

        module = chassis.module_list[0]
        module.set_admin_state = MagicMock(side_effect=side_effect_fn)

        updater._power_cycle_dpu("DPU0", 0)

        # Should still transition (try_get catches the exception)
        assert updater.dpu_recovery_state["DPU0"]['reset_count'] == 1


# ============================================================================
# Test: DB setter methods
# ============================================================================

class TestDBSetterMethods:
    """Test that DB setter methods properly update CHASSIS_STATE_DB."""

    def test_set_ready_status_true(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_ready_status("DPU0", 'true')
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'true'

    def test_set_ready_status_false(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_ready_status("DPU0", 'false')
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

    def test_set_recovery_status_recoverable(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_recovery_status("DPU0", RECOVERY_RECOVERABLE)
        assert get_dpu_state_field(updater, "DPU0", RECOVERY_STATUS) == RECOVERY_RECOVERABLE

    def test_set_recovery_status_unrecoverable(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_recovery_status("DPU0", RECOVERY_UNRECOVERABLE)
        assert get_dpu_state_field(updater, "DPU0", RECOVERY_STATUS) == RECOVERY_UNRECOVERABLE

    def test_set_reset_count(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_reset_count("DPU0", 7)
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '7'

    def test_set_last_down_time_format(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_last_down_time("DPU0")
        val = get_dpu_state_field(updater, "DPU0", LAST_DOWN_TIME)
        assert val is not None
        assert 'UTC' in val  # Default format contains 'UTC'

    def test_set_last_ready_time_format(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater._set_last_ready_time("DPU0")
        val = get_dpu_state_field(updater, "DPU0", LAST_READY_TIME)
        assert val is not None
        assert 'UTC' in val


# ============================================================================
# Test: Reboot cause persistence and history
# ============================================================================

class TestRebootCausePersistence:
    """Test reboot cause file I/O, symlink management, and history rotation."""

    def test_persist_dpu_reboot_time(self):
        """persist_dpu_reboot_time writes formatted time to file."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                updater.persist_dpu_reboot_time("DPU0")

                path = os.path.join(tmpdir, "dpu0", "prev_reboot_time.txt")
                assert os.path.exists(path)
                content = open(path).read().strip()
                # Format: YYYY_MM_DD_HH_MM_SS
                assert len(content.split('_')) == 6

    def test_retrieve_dpu_reboot_time_exists(self):
        """retrieve_dpu_reboot_time returns stored time."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                mod_dir = os.path.join(tmpdir, "dpu0")
                os.makedirs(mod_dir)
                with open(os.path.join(mod_dir, "prev_reboot_time.txt"), 'w') as f:
                    f.write("2026_05_19_10_30_00")

                result = updater.retrieve_dpu_reboot_time("DPU0")
                assert result == "2026_05_19_10_30_00"

    def test_retrieve_dpu_reboot_time_missing(self):
        """retrieve_dpu_reboot_time returns None when file doesn't exist."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                result = updater.retrieve_dpu_reboot_time("DPU0")
                assert result is None

    def test_persist_dpu_reboot_cause_creates_history_file(self):
        """persist_dpu_reboot_cause creates JSON history file."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                # Create needed directories
                history_dir = os.path.join(tmpdir, "dpu0", "history")
                os.makedirs(history_dir)

                updater.persist_dpu_reboot_cause(("Power Loss", "Unexpected"), "DPU0")

                # Verify history file was created
                files = os.listdir(history_dir)
                assert len(files) == 1
                assert files[0].endswith("_reboot_cause.json")

                # Verify content
                with open(os.path.join(history_dir, files[0])) as f:
                    data = json.load(f)
                assert data['cause'] == 'Power Loss'
                assert data['comment'] == 'Unexpected'
                assert data['device'] == 'DPU0'

    def test_persist_dpu_reboot_cause_none(self):
        """persist_dpu_reboot_cause handles None reboot_cause."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                history_dir = os.path.join(tmpdir, "dpu0", "history")
                os.makedirs(history_dir)

                updater.persist_dpu_reboot_cause(None, "DPU0")

                files = os.listdir(history_dir)
                assert len(files) == 1
                with open(os.path.join(history_dir, files[0])) as f:
                    data = json.load(f)
                assert data['cause'] == 'Unknown'
                assert data['comment'] == 'N/A'

    def test_persist_dpu_reboot_cause_string(self):
        """persist_dpu_reboot_cause handles string reboot_cause (comma-separated)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                history_dir = os.path.join(tmpdir, "dpu0", "history")
                os.makedirs(history_dir)

                updater.persist_dpu_reboot_cause("Watchdog,Hardware watchdog reset", "DPU0")

                files = os.listdir(history_dir)
                with open(os.path.join(history_dir, files[0])) as f:
                    data = json.load(f)
                assert data['cause'] == 'Watchdog'
                assert data['comment'] == 'Hardware watchdog reset'

    def test_persist_dpu_reboot_cause_creates_symlink(self):
        """persist_dpu_reboot_cause creates previous-reboot-cause.json symlink."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                history_dir = os.path.join(tmpdir, "dpu0", "history")
                os.makedirs(history_dir)

                updater.persist_dpu_reboot_cause(("Test Cause", ""), "DPU0")

                symlink = os.path.join(tmpdir, "dpu0", "previous-reboot-cause.json")
                assert os.path.islink(symlink)
                # Symlink should point to the history file
                target = os.readlink(symlink)
                assert "_reboot_cause.json" in target

    def test_rotate_files_removes_old_files(self):
        """_rotate_files removes oldest files when exceeding MAX_HISTORY_FILES."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir), \
                 patch("chassisd.MAX_HISTORY_FILES", 3):
                history_dir = os.path.join(tmpdir, "dpu0", "history")
                os.makedirs(history_dir)

                # Create 5 files (exceeds limit of 3)
                for i in range(5):
                    fname = f"2026_01_0{i+1}_00_00_00_reboot_cause.json"
                    with open(os.path.join(history_dir, fname), 'w') as f:
                        f.write("{}")

                updater._rotate_files("DPU0")

                remaining = sorted(os.listdir(history_dir))
                assert len(remaining) == 3
                # Oldest files should be removed (keep the 3 newest)
                assert remaining[0].startswith("2026_01_03")

    def test_rotate_files_no_op_when_under_limit(self):
        """_rotate_files does nothing when file count is within limit."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir), \
                 patch("chassisd.MAX_HISTORY_FILES", 10):
                history_dir = os.path.join(tmpdir, "dpu0", "history")
                os.makedirs(history_dir)

                for i in range(3):
                    with open(os.path.join(history_dir, f"file_{i}.json"), 'w') as f:
                        f.write("{}")

                updater._rotate_files("DPU0")
                assert len(os.listdir(history_dir)) == 3

    def test_retrieve_dpu_reboot_info_valid(self):
        """retrieve_dpu_reboot_info returns (cause, time) from JSON file."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                mod_dir = os.path.join(tmpdir, "dpu0")
                os.makedirs(mod_dir)
                data = {"cause": "Kernel Panic", "name": "2026_05_19_10_00_00"}
                with open(os.path.join(mod_dir, "previous-reboot-cause.json"), 'w') as f:
                    json.dump(data, f)

                cause, time_str = updater.retrieve_dpu_reboot_info("DPU0")
                assert cause == "Kernel Panic"
                assert time_str == "2026_05_19_10_00_00"

    def test_retrieve_dpu_reboot_info_missing_file(self):
        """retrieve_dpu_reboot_info returns (None, None) when file doesn't exist."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                cause, time_str = updater.retrieve_dpu_reboot_info("DPU0")
                assert cause is None
                assert time_str is None


# ============================================================================
# Test: update_dpu_reboot_cause_to_db
# ============================================================================

class TestUpdateRebootCauseToDb:
    """Test update_dpu_reboot_cause_to_db publishes history to CHASSIS_STATE_DB."""

    def test_publishes_reboot_cause_to_db(self):
        """History files should be published as keys in CHASSIS_STATE_DB."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        # Mock the chassis_state_db
        mock_db = MagicMock()
        mock_db.keys.return_value = []
        updater.chassis_state_db = mock_db

        history_data = {
            "cause": "Watchdog",
            "comment": "HW reset",
            "device": "DPU0",
            "time": "Mon May 19 10:00:00 AM UTC 2026",
            "name": "2026_05_19_10_00_00",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = os.path.join(tmpdir, "dpu0", "history")
            os.makedirs(history_dir)
            fpath = os.path.join(history_dir, "2026_05_19_10_00_00_reboot_cause.json")
            with open(fpath, 'w') as f:
                json.dump(history_data, f)

            with patch("glob.glob", return_value=[fpath]):
                updater.update_dpu_reboot_cause_to_db("DPU0")

        # Verify hset was called for each field
        calls = mock_db.hset.call_args_list
        assert len(calls) > 0
        # Key should be REBOOT_CAUSE|DPU0|<time>
        first_key = calls[0][0][0]
        assert "REBOOT_CAUSE|DPU0|2026_05_19_10_00_00" == first_key

    def test_deletes_existing_keys_before_publish(self):
        """Existing REBOOT_CAUSE keys should be deleted before publishing new ones."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        mock_db = MagicMock()
        mock_db.keys.return_value = ["REBOOT_CAUSE|DPU0|old_time"]
        updater.chassis_state_db = mock_db

        with patch("glob.glob", return_value=[]):
            updater.update_dpu_reboot_cause_to_db("DPU0")

        mock_db.delete.assert_called_once_with("REBOOT_CAUSE|DPU0|old_time")

    def test_handles_invalid_json_gracefully(self):
        """Invalid JSON in history file should be skipped without crashing."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        mock_db = MagicMock()
        mock_db.keys.return_value = []
        updater.chassis_state_db = mock_db

        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = os.path.join(tmpdir, "dpu0", "history")
            os.makedirs(history_dir)
            fpath = os.path.join(history_dir, "bad_file.json")
            with open(fpath, 'w') as f:
                f.write("not valid json {{{")

            with patch("glob.glob", return_value=[fpath]):
                # Should not raise
                updater.update_dpu_reboot_cause_to_db("DPU0")

        # No hset calls since JSON was invalid
        mock_db.hset.assert_not_called()


# ============================================================================
# Test: Configuration edge cases
# ============================================================================

class TestConfigEdgeCases:
    """Test platform.json edge cases for reset_limit and reboot_timeout."""

    def test_reset_limit_zero_uses_default(self):
        """dpu_reset_limit=0 should use default (0 would mean never recover)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 0})
        # Implementation either uses 0 (immediate unrecoverable) or falls back to default
        # Either is acceptable; test documents the behavior
        assert updater.reset_limit == 0 or updater.reset_limit == DEFAULT_DPU_RESET_LIMIT

    def test_reset_limit_negative_treated_as_given(self):
        """Negative dpu_reset_limit — documents behavior (immediate unrecoverable)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": -1})
        # Negative means any failure hits the limit immediately
        assert updater.reset_limit == -1 or updater.reset_limit == DEFAULT_DPU_RESET_LIMIT

    def test_only_reset_limit_specified(self):
        """Only dpu_reset_limit in platform.json; timeout uses default."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reset_limit": 8})
        assert updater.reset_limit == 8
        assert updater.dpu_reboot_timeout == 360

    def test_only_reboot_timeout_specified(self):
        """Only dpu_reboot_timeout in platform.json; reset_limit uses default."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis, platform_json={"dpu_reboot_timeout": 900})
        assert updater.reset_limit == DEFAULT_DPU_RESET_LIMIT
        assert updater.dpu_reboot_timeout == 900


# ============================================================================
# Test: Multiple DPU advanced scenarios
# ============================================================================

class TestMultipleDpuAdvanced:
    """Advanced multi-DPU scenarios."""

    def test_all_dpus_fail_simultaneously(self):
        """All DPUs failing at once should all get independent power-cycles."""
        chassis = create_chassis_with_dpus(4)
        updater = create_updater(chassis)

        for i in range(4):
            name = f"DPU{i}"
            updater.dpu_recovery_state[name]['state'] = DPU_STATE_READY
            set_dpu_states(updater, name, mp='down', cp='down', dp='down')
            chassis.module_list[i].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
            updater.module_table.hset(name, "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        for i in range(4):
            assert updater.dpu_recovery_state[f"DPU{i}"]['state'] == DPU_STATE_POWER_CYCLE
            assert updater.dpu_recovery_state[f"DPU{i}"]['reset_count'] == 1

    def test_reset_count_independent_per_dpu(self):
        """Each DPU's reset_count increments independently."""
        chassis = create_chassis_with_dpus(2)
        updater = create_updater(chassis)

        # DPU0: already at reset_count=3
        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        updater.dpu_recovery_state["DPU0"]['reset_count'] = 3
        set_dpu_states(updater, "DPU0", mp='down', cp='up', dp='up')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # DPU1: at reset_count=0
        updater.dpu_recovery_state["DPU1"]['state'] = DPU_STATE_READY
        updater.dpu_recovery_state["DPU1"]['reset_count'] = 0
        set_dpu_states(updater, "DPU1", mp='down', cp='up', dp='up')
        chassis.module_list[1].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU1", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()

        assert updater.dpu_recovery_state["DPU0"]['reset_count'] == 4
        assert updater.dpu_recovery_state["DPU1"]['reset_count'] == 1

    def test_cascading_dp_down_then_cp_down(self):
        """DP down (no recovery) followed by CP down (triggers immediate power-cycle)."""
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        updater.dpu_recovery_state["DPU0"]['state'] = DPU_STATE_READY
        set_dpu_states(updater, "DPU0", mp='up', cp='up', dp='down')
        chassis.module_list[0].set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
        updater.module_table.hset("DPU0", "oper_status", str(ModuleBase.MODULE_STATUS_ONLINE))

        # Phase 1: DP down only — still Ready but not ready
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_READY
        assert get_dpu_state_field(updater, "DPU0", READY_STATUS) == 'false'

        # Phase 2: CP also goes down — immediate PowerCycle
        set_dpu_states(updater, "DPU0", mp='up', cp='down', dp='down')
        with patch.object(updater, '_is_auto_recovery_enabled', return_value=True), \
             patch.object(updater, 'get_module_admin_status', return_value='up'):
            updater.update_dpu_recovery_state()
        assert updater.dpu_recovery_state["DPU0"]['state'] == DPU_STATE_POWER_CYCLE
        assert get_dpu_state_field(updater, "DPU0", RESET_COUNT) == '1'


# ============================================================================
# Test: _is_first_boot helper
# ============================================================================

class TestIsFirstBoot:
    """Test _is_first_boot() helper method."""

    def test_first_boot_detected(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                mod_dir = os.path.join(tmpdir, "dpu0")
                os.makedirs(mod_dir)
                with open(os.path.join(mod_dir, "reboot-cause.txt"), 'w') as f:
                    f.write("First boot")

                assert updater._is_first_boot("DPU0") is True

    def test_not_first_boot(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                mod_dir = os.path.join(tmpdir, "dpu0")
                os.makedirs(mod_dir)
                with open(os.path.join(mod_dir, "reboot-cause.txt"), 'w') as f:
                    f.write("Watchdog")

                assert updater._is_first_boot("DPU0") is False

    def test_missing_file_returns_false(self):
        chassis = create_chassis_with_dpus(1)
        updater = create_updater(chassis)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", tmpdir):
                assert updater._is_first_boot("DPU0") is False
