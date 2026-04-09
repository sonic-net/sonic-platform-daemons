"""
    Unit tests for bmcctld daemon.

    Tests cover all major event handlers and the initial power-on sequence
"""

import os
import sys
import threading
from imp import load_source
from unittest import mock
from unittest.mock import MagicMock, patch, call

import pytest

# --------------------------------------------------------------------------
# Path setup - mocked_libs MUST be inserted before any sonic_py_common import
# so that swsscommon resolves to the mock package, not the real one.
# --------------------------------------------------------------------------

tests_path = os.path.dirname(os.path.abspath(__file__))
mocked_libs_path = os.path.join(tests_path, 'mocked_libs')
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, 'scripts')

sys.path.insert(0, mocked_libs_path)
sys.path.insert(0, modules_path)

# Verify we are using the mocked swsscommon package
import swsscommon as _swsscommon_pkg  # noqa: E402
assert os.path.samefile(
    _swsscommon_pkg.__path__[0],
    os.path.join(mocked_libs_path, 'swsscommon')
), "swsscommon mock not loaded from mocked_libs!"

os.environ["BMCCTLD_UNIT_TESTING"] = "1"

from sonic_py_common import daemon_base  # noqa: E402
daemon_base.db_connect = MagicMock()

load_source('bmcctld', os.path.join(scripts_path, 'bmcctld'))
import bmcctld  # noqa: E402  (loaded via load_source above)

from .mock_platform import MockChassis, MockModule
from .mock_swsscommon import Table, FieldValuePairs

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def silence_logs(monkeypatch):
    """Suppress all syslog calls during tests."""
    for cls in [
        bmcctld.SwitchHostController,
        bmcctld.PolicyReader,
        bmcctld.CriticalEventChecker,
        bmcctld.GracefulShutdownHandler,
        bmcctld.BmcEventHandler,
        bmcctld.BmcctldDaemon,
    ]:
        monkeypatch.setattr(cls, 'log_info', MagicMock())
        monkeypatch.setattr(cls, 'log_notice', MagicMock())
        monkeypatch.setattr(cls, 'log_warning', MagicMock())
        monkeypatch.setattr(cls, 'log_error', MagicMock())
        monkeypatch.setattr(cls, 'log_debug', MagicMock())


@pytest.fixture
def chassis():
    return MockChassis()


@pytest.fixture
def controller(chassis):
    ctrl = bmcctld.SwitchHostController(chassis)
    ctrl.host_state_table = Table(None, bmcctld.HOST_STATE_TABLE)
    return ctrl


@pytest.fixture
def policy_reader():
    pr = bmcctld.PolicyReader()
    return pr


@pytest.fixture
def critical_event_checker():
    lc = bmcctld.CriticalEventChecker()
    lc._system_leak_table = Table(None, bmcctld.SYSTEM_LEAK_STATUS_TABLE)
    lc._rack_alert_table = Table(None, bmcctld.RACK_MANAGER_ALERT_TABLE)
    return lc


@pytest.fixture
def graceful_shutdown(controller, policy_reader):
    gs = bmcctld.GracefulShutdownHandler(controller, policy_reader)
    return gs


@pytest.fixture
def event_handler(policy_reader, critical_event_checker):
    stop_event = threading.Event()
    stop_event.set()  # Prevent blocking in tests
    action_queue = __import__('queue').Queue()
    eh = bmcctld.BmcEventHandler(action_queue, policy_reader, critical_event_checker, stop_event)
    # Replace live DB tables with in-memory mocks
    eh._cmd_table = Table(None, bmcctld.RACK_MANAGER_COMMAND_TABLE)
    return eh


# --------------------------------------------------------------------------
# Helper: set a Table entry by directly populating mock_dict
# --------------------------------------------------------------------------

def _set_table_entry(table, key, fields):
    table.mock_dict[key] = fields


# --------------------------------------------------------------------------
# Tests: SwitchHostController
# --------------------------------------------------------------------------

class TestSwitchHostController:

    def test_get_oper_status_online(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        assert controller.get_oper_status() == bmcctld.SWITCH_HOST_ONLINE

    def test_get_oper_status_offline(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        assert controller.get_oper_status() == bmcctld.SWITCH_HOST_OFFLINE

    def test_power_on_calls_set_admin_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        result = controller.power_on()
        assert result is True
        assert chassis.switch_host.get_admin_state() is True

    def test_power_off_calls_set_admin_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        result = controller.power_off()
        assert result is True
        assert chassis.switch_host.get_admin_state() is False

    def test_power_cycle_calls_do_power_cycle(self, chassis, controller):
        result = controller.power_cycle()
        assert result is True
        assert chassis.switch_host.power_cycle_called is True

    def test_power_on_updates_host_state(self, chassis, controller):
        controller.power_on()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        assert result[0] is True
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_updates_host_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        assert result[0] is True
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    def test_power_on_writes_transitional_status(self, chassis, controller):
        """STATE_DB shows POWERING_ON before the platform set_admin_state call."""
        captured = {}
        original = chassis.switch_host.set_admin_state
        def interceptor(up):
            result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
            if result and result[0]:
                captured.update(dict(result[1]))
            original(up)
        chassis.switch_host.set_admin_state = interceptor
        controller.power_on()
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) == bmcctld.SWITCH_HOST_POWERING_ON
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.POWER_STATE_ON
        # Final entry must reflect confirmed ONLINE
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_writes_transitional_status(self, chassis, controller):
        """STATE_DB shows POWERING_OFF before the platform set_admin_state call."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        captured = {}
        original = chassis.switch_host.set_admin_state
        def interceptor(up):
            result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
            if result and result[0]:
                captured.update(dict(result[1]))
            original(up)
        chassis.switch_host.set_admin_state = interceptor
        controller.power_off()
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) == bmcctld.SWITCH_HOST_POWERING_OFF
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.POWER_STATE_OFF
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    def test_power_cycle_writes_transitional_status(self, chassis, controller):
        """STATE_DB shows POWER_CYCLING before the platform do_power_cycle call."""
        captured = {}
        original = chassis.switch_host.do_power_cycle
        def interceptor():
            result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
            if result and result[0]:
                captured.update(dict(result[1]))
            original()
        chassis.switch_host.do_power_cycle = interceptor
        controller.power_cycle()
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) == bmcctld.SWITCH_HOST_POWER_CYCLING
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.POWER_STATE_CYCLE
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_get_db_power_state(self, chassis, controller):
        """get_db_power_state returns the value stored by the last _update_host_state call."""
        controller.power_on()
        assert controller.get_db_power_state() == bmcctld.POWER_STATE_ON

    def test_power_on_rolls_back_state_on_exception(self, chassis, controller):
        """If set_admin_state raises, STATE_DB is restored to the pre-call snapshot."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.power_off()  # seed a known prior state (OFFLINE / POWER_OFF)
        chassis.switch_host.set_admin_state = MagicMock(side_effect=RuntimeError("hw fault"))
        result = controller.power_on()
        assert result is False
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_power_off_rolls_back_state_on_exception(self, chassis, controller):
        """If set_admin_state raises, STATE_DB is restored to the pre-call snapshot."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_on()  # seed a known prior state (ONLINE / POWER_ON)
        chassis.switch_host.set_admin_state = MagicMock(side_effect=RuntimeError("hw fault"))
        result = controller.power_off()
        assert result is False
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON

    def test_get_switch_host_module_by_type(self, chassis):
        """If a module explicitly returns MODULE_TYPE_SWITCH_HOST it is selected."""
        ctrl = bmcctld.SwitchHostController(chassis)
        mod = ctrl._get_switch_host_module()
        assert mod is chassis.switch_host

    def test_get_switch_host_module_fallback_index_1(self):
        """Without a SWITCH_HOST type, fall back to module at index 1."""
        ch = MockChassis()
        # Change types so type-based lookup fails
        ch._module_list[1].module_type = "UNKNOWN"
        ctrl = bmcctld.SwitchHostController(ch)
        ctrl._switch_host_module = None  # force re-lookup
        mod = ctrl._get_switch_host_module()
        assert mod is ch._module_list[1]

    def test_refresh_host_state_preserves_power_state(self, chassis, controller):
        controller.power_on()
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.refresh_host_state()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        state = dict(result[1])
        # power state must still be POWER_ON
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_refresh_host_state_infers_power_on_when_not_available(self, chassis, controller):
        # No prior power state recorded — host is ONLINE, so infer POWER_ON
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.refresh_host_state()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_refresh_host_state_infers_power_off_when_not_available(self, chassis, controller):
        # No prior power state recorded — host is OFFLINE, so infer POWER_OFF
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.refresh_host_state()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    # -- _verify_oper_status tests --

    def test_verify_oper_status_matches_immediately(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        with patch('time.sleep') as mock_sleep:
            result = controller._verify_oper_status(bmcctld.SWITCH_HOST_ONLINE, 30, "test")
        assert result is True
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('time.time')
    def test_verify_oper_status_timeout(self, mock_time, mock_sleep, chassis, controller):
        # Simulate: deadline set at t=0+30=30, first loop check t=0 (<30), sleep,
        # second loop check t=31 (>=30) → exit without match
        mock_time.side_effect = [0, 0, 31, 31]
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        result = controller._verify_oper_status(bmcctld.SWITCH_HOST_ONLINE, 30, "test")
        assert result is False
        mock_sleep.assert_called_once_with(bmcctld.POWER_VERIFY_POLL_INTERVAL_SECS)

    def test_power_on_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(controller, '_verify_oper_status', return_value=False):
            result = controller.power_on()
        assert result is False
        assert chassis.switch_host.get_admin_state() is True  # API was still called

    def test_power_off_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(controller, '_verify_oper_status', return_value=False):
            result = controller.power_off()
        assert result is False
        assert chassis.switch_host.get_admin_state() is False  # API was still called

    def test_power_cycle_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(controller, '_verify_oper_status', return_value=False):
            result = controller.power_cycle()
        assert result is False
        assert chassis.switch_host.power_cycle_called is True  # API was still called

    def test_power_cycle_uses_double_timeout(self, chassis, controller):
        with patch.object(controller, '_verify_oper_status', return_value=True) as mock_verify:
            controller.power_cycle()
        mock_verify.assert_called_once_with(
            bmcctld.SWITCH_HOST_ONLINE,
            bmcctld.POWER_VERIFY_TIMEOUT_SECS * 2,
            "power_cycle"
        )


# --------------------------------------------------------------------------
# Tests: PolicyReader
# --------------------------------------------------------------------------

class TestPolicyReader:

    def _make_table_returning(self, key, fields):
        tbl = Table(None, "TEST")
        _set_table_entry(tbl, key, fields)
        return tbl

    def test_get_power_on_delay_default(self, policy_reader):
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            assert policy_reader.get_power_on_delay() == bmcctld.DEFAULT_POWER_ON_DELAY_SECS

    def test_get_power_on_delay_custom(self, policy_reader):
        tbl = Table(None, bmcctld.SWITCH_HOST_POWER_ON_DELAY_TABLE)
        _set_table_entry(tbl, "default", {bmcctld.FIELD_POWER_ON_DELAY: "60"})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_power_on_delay() == 60.0

    def test_get_graceful_shutdown_timeout_default(self, policy_reader):
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            assert policy_reader.get_graceful_shutdown_timeout() == bmcctld.DEFAULT_SHUTDOWN_DELAY_SECS

    def test_get_graceful_shutdown_timeout_zero(self, policy_reader):
        tbl = Table(None, bmcctld.SWITCH_HOST_SHUTDOWN_TIMEOUT_TABLE)
        _set_table_entry(tbl, "default", {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "0"})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_graceful_shutdown_timeout() == 0.0

    def test_get_leak_control_policy_defaults(self, policy_reader):
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            policy = policy_reader.get_leak_control_policy()
        assert policy["system_leak_policy"] == "enabled"
        assert policy["system_critical_leak_action"] == bmcctld.ACTION_POWER_OFF
        assert policy["system_minor_leak_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["rack_mgr_leak_policy"] == "enabled"
        assert policy["rack_mgr_critical_alert_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["rack_mgr_minor_alert_action"] == bmcctld.ACTION_SYSLOG_ONLY

    def test_get_leak_control_policy_custom(self, policy_reader):
        tbl = Table(None, bmcctld.LEAK_CONTROL_POLICY_TABLE)
        _set_table_entry(tbl, bmcctld.LEAK_CONTROL_POLICY_TABLE, {
            "system_critical_leak_action": bmcctld.ACTION_GRACEFUL_SHUTDOWN,
            "rack_mgr_critical_alert_action": bmcctld.ACTION_POWER_OFF,
        })
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            policy = policy_reader.get_leak_control_policy()
        assert policy["system_critical_leak_action"] == bmcctld.ACTION_GRACEFUL_SHUTDOWN
        assert policy["rack_mgr_critical_alert_action"] == bmcctld.ACTION_POWER_OFF


# --------------------------------------------------------------------------
# Tests: CriticalEventChecker
# --------------------------------------------------------------------------

class TestCriticalEventChecker:

    def test_no_critical_system_leak(self, critical_event_checker):
        tbl = Table(None, bmcctld.SYSTEM_LEAK_STATUS_TABLE)
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_system_leak() is False

    def test_has_critical_system_leak(self, critical_event_checker):
        tbl = Table(None, bmcctld.SYSTEM_LEAK_STATUS_TABLE)
        _set_table_entry(tbl, bmcctld.SYSTEM_LEAK_STATUS_KEY,
                         {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_system_leak() is True

    def test_minor_system_leak_not_critical(self, critical_event_checker):
        tbl = Table(None, bmcctld.SYSTEM_LEAK_STATUS_TABLE)
        _set_table_entry(tbl, bmcctld.SYSTEM_LEAK_STATUS_KEY,
                         {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_MINOR})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_system_leak() is False

    def test_has_critical_rack_mgr_alert(self, critical_event_checker):
        tbl = Table(None, bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_temperature",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_rack_mgr_alert() is True

    def test_no_critical_rack_mgr_alert(self, critical_event_checker):
        tbl = Table(None, bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_temperature",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MINOR})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_rack_mgr_alert() is False

    def test_rack_level_leak_critical_via_leak_field(self, critical_event_checker):
        """Rack_level_leak uses 'leak' field, not 'severity'."""
        tbl = Table(None, bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Rack_level_leak",
                         {bmcctld.FIELD_LEAK: bmcctld.ALERT_SEVERITY_CRITICAL})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_rack_mgr_alert() is True

    def test_has_any_critical_event_system(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=True)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=False)
        assert critical_event_checker.has_any_critical_event() is True

    def test_has_any_critical_event_rack_mgr(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=False)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=True)
        assert critical_event_checker.has_any_critical_event() is True

    def test_no_critical_events(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=False)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=False)
        assert critical_event_checker.has_any_critical_event() is False


# --------------------------------------------------------------------------
# Tests: GracefulShutdownHandler
# --------------------------------------------------------------------------

class TestGracefulShutdownHandler:

    def test_powering_off_state_set_before_gnoi(self, graceful_shutdown, chassis):
        """STATE_DB shows POWERING_OFF before gNOI shutdown is issued."""
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        captured = {}
        original = graceful_shutdown.controller._update_host_state
        def capture_first(power_state, device_status=None):
            if not captured:
                captured['device_status'] = device_status
            return original(power_state, device_status)
        graceful_shutdown.controller._update_host_state = capture_first
        graceful_shutdown._issue_gnoi_shutdown = MagicMock(return_value=False)
        graceful_shutdown.execute()
        assert captured.get('device_status') == bmcctld.SWITCH_HOST_POWERING_OFF

    def test_shutdown_delay_zero_skips_gnoi(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=0)
        graceful_shutdown.execute()
        # set_admin_state(False) must be called on the Switch-Host module
        assert chassis.switch_host.get_admin_state() is False

    def test_gnoi_fails_triggers_power_off(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        graceful_shutdown._issue_gnoi_shutdown = MagicMock(return_value=False)
        graceful_shutdown.execute()
        assert chassis.switch_host.get_admin_state() is False

    def test_gnoi_success_and_host_goes_offline_still_calls_power_off(self, graceful_shutdown, chassis):
        """Even after graceful OFFLINE, power_off is always issued to remove power."""
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        graceful_shutdown._issue_gnoi_shutdown = MagicMock(return_value=True)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        result = graceful_shutdown.execute()
        assert result is True
        assert chassis.switch_host.get_admin_state() is False

    def test_gnoi_timeout_triggers_power_off(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        graceful_shutdown._issue_gnoi_shutdown = MagicMock(return_value=True)
        # Simulate timeout: host never goes OFFLINE within graceful_shutdown_timeout
        with patch.object(graceful_shutdown.controller, '_verify_oper_status', return_value=False):
            graceful_shutdown.execute()
        assert chassis.switch_host.get_admin_state() is False

    def test_get_switch_host_addr_default(self, graceful_shutdown):
        with patch('builtins.open', side_effect=FileNotFoundError):
            addr = graceful_shutdown._get_switch_host_addr()
        assert addr == bmcctld.DEFAULT_SWITCH_HOST_ADDR

    def test_get_switch_host_addr_from_bmc_json(self, graceful_shutdown, tmp_path):
        import json
        bmc_json = tmp_path / "bmc.json"
        bmc_json.write_text(json.dumps({"bmc_if_addr": "10.0.0.1"}))
        with patch.object(bmcctld, 'BMC_JSON_PATHS', [str(bmc_json)]):
            addr = graceful_shutdown._get_switch_host_addr()
        assert addr == "10.0.0.1"


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - Rack Manager commands
# --------------------------------------------------------------------------

class TestBmcEventHandlerRackMgrCommands:

    def _cmd_fvs(self, command, status=bmcctld.CMD_STATUS_PENDING):
        return {bmcctld.FIELD_COMMAND: command, bmcctld.FIELD_STATUS: status}

    def test_power_off_command_enqueues_power_off(self, event_handler):
        event_handler._handle_rack_mgr_command("CMD_1", self._cmd_fvs(bmcctld.CMD_POWER_OFF))
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert item.on_complete is not None

    def test_power_on_command_no_leak_enqueues_power_on(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        event_handler._handle_rack_mgr_command("CMD_2", self._cmd_fvs(bmcctld.CMD_POWER_ON))
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.on_complete is not None

    def test_power_on_command_blocked_by_critical_leak(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
        event_handler._handle_rack_mgr_command("CMD_3", self._cmd_fvs(bmcctld.CMD_POWER_ON))
        assert event_handler.action_queue.empty()

    def test_power_cycle_command_enqueues_power_cycle(self, event_handler):
        event_handler._handle_rack_mgr_command("CMD_4", self._cmd_fvs(bmcctld.CMD_POWER_CYCLE))
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_CYCLE

    def test_already_processed_command_is_skipped(self, event_handler):
        event_handler._handle_rack_mgr_command(
            "CMD_5", self._cmd_fvs(bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_DONE))
        assert event_handler.action_queue.empty()

    def test_unknown_command_is_logged(self, event_handler):
        event_handler._handle_rack_mgr_command("CMD_6", self._cmd_fvs("INVALID_CMD"))
        assert event_handler.action_queue.empty()


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - Chassis module admin state
# --------------------------------------------------------------------------

class TestBmcEventHandlerChassisModule:

    def test_admin_down_triggers_graceful_shutdown(self, event_handler):
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_admin_up_powers_on_when_no_leak(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_ON

    def test_admin_up_blocked_by_critical_leak(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        assert event_handler.action_queue.empty()


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - System leak events
# --------------------------------------------------------------------------

class TestBmcEventHandlerSystemLeak:

    def _make_policy(self, **kwargs):
        policy = {
            "system_leak_policy": "enabled",
            "system_critical_leak_action": bmcctld.ACTION_POWER_OFF,
            "system_minor_leak_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_leak_policy": "enabled",
            "rack_mgr_critical_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_minor_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
        }
        policy.update(kwargs)
        return policy

    def test_critical_system_leak_power_off(self, event_handler, chassis):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_critical_leak_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL},
        )
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_OFF

    def test_critical_system_leak_graceful_shutdown(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_critical_leak_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL},
        )
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_critical_system_leak_syslog_only(self, event_handler, chassis):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_critical_leak_action=bmcctld.ACTION_SYSLOG_ONLY)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_minor_system_leak_syslog_only_by_default(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy()
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_MINOR},
        )
        assert event_handler.action_queue.empty()

    def test_system_leak_policy_disabled_skips_action(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_leak_policy="disabled")
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_wrong_key_is_ignored(self, event_handler):
        event_handler._handle_system_leak(
            "wrong-key",
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_leak_cleared_no_action(self, event_handler):
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: ""},  # cleared
        )
        assert event_handler.action_queue.empty()


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - Rack Manager alerts
# --------------------------------------------------------------------------

class TestBmcEventHandlerRackMgrAlerts:

    def _make_policy(self, **kwargs):
        policy = {
            "system_leak_policy": "enabled",
            "system_critical_leak_action": bmcctld.ACTION_POWER_OFF,
            "system_minor_leak_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_leak_policy": "enabled",
            "rack_mgr_critical_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_minor_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
        }
        policy.update(kwargs)
        return policy

    def test_critical_rack_alert_syslog_only_by_default(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy()
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_temperature",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_critical_rack_alert_power_off_when_configured(self, event_handler, chassis):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_critical_alert_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_rack_mgr_alert(
            "Rack_level_leak",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_OFF

    def test_minor_rack_alert_syslog_only_by_default(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy()
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_flow_rate",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MINOR},
        )
        assert event_handler.action_queue.empty()

    def test_rack_mgr_leak_policy_disabled_skips_action(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_leak_policy="disabled")
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_pressure",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_rack_level_leak_uses_leak_field(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_critical_alert_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_rack_mgr_alert(
            "Rack_level_leak",
            {bmcctld.FIELD_LEAK: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        item = event_handler.action_queue.get_nowait()
        assert item.action == bmcctld.ACTION_POWER_OFF

    def test_normal_severity_no_action(self, event_handler):
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_temperature",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_NORMAL},
        )
        assert event_handler.action_queue.empty()


# --------------------------------------------------------------------------
# Tests: BmcctldDaemon - action loop
# --------------------------------------------------------------------------

class TestBmcctldDaemonActionLoop:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
            daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        return daemon

    def test_execute_graceful_shutdown(self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.graceful_shutdown.execute = MagicMock(return_value=True)
        item = bmcctld.ActionItem(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "test")
        daemon._execute_action_item(item)
        daemon.graceful_shutdown.execute.assert_called_once()

    def test_execute_power_off(self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller.power_off = MagicMock(return_value=True)
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "test")
        daemon._execute_action_item(item)
        daemon.controller.power_off.assert_called_once()

    def test_execute_power_on(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=True)
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "test")
        daemon._execute_action_item(item)
        daemon.controller.power_on.assert_called_once()

    def test_execute_power_cycle(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_cycle = MagicMock(return_value=True)
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_CYCLE, "test")
        daemon._execute_action_item(item)
        daemon.controller.power_cycle.assert_called_once()

    def test_on_complete_called_with_success(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "test", on_complete=callback)
        daemon._execute_action_item(item)
        callback.assert_called_once_with(True)

    def test_on_complete_called_with_failure(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=False)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "test", on_complete=callback)
        daemon._execute_action_item(item)
        callback.assert_called_once_with(False)

    def test_action_loop_processes_queued_items(self, chassis):
        daemon = self._make_daemon(chassis)
        # power_on mock must also flip oper_status so the subsequent power_off
        # idempotency check sees ONLINE and proceeds instead of skipping.
        def fake_power_on():
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
            return True
        daemon.controller.power_on = MagicMock(side_effect=fake_power_on)
        daemon.controller.power_off = MagicMock(return_value=True)
        daemon.action_queue.put(bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "evt1"))
        daemon.action_queue.put(bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "evt2"))

        # Stop the loop after both items are executed
        original = daemon._execute_action_item
        calls = [0]
        def counting_execute(item):
            original(item)
            calls[0] += 1
            if calls[0] >= 2:
                daemon.stop_event.set()
        daemon._execute_action_item = counting_execute

        daemon._run_action_loop()
        daemon.controller.power_on.assert_called_once()
        daemon.controller.power_off.assert_called_once()

    # -- Idempotency skip tests --

    def test_execute_power_off_skipped_when_already_offline(self, chassis):
        """power_off is not issued when host is already OFFLINE; on_complete(True) fired."""
        # chassis.switch_host starts OFFLINE by default
        daemon = self._make_daemon(chassis)
        daemon.controller.power_off = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "dup-event", on_complete=callback)
        daemon._execute_action_item(item)
        daemon.controller.power_off.assert_not_called()
        callback.assert_called_once_with(True)

    def test_execute_graceful_shutdown_skipped_when_already_offline(self, chassis):
        """graceful_shutdown is not issued when host is already OFFLINE."""
        daemon = self._make_daemon(chassis)
        daemon.graceful_shutdown.execute = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "dup-event", on_complete=callback)
        daemon._execute_action_item(item)
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(True)

    def test_execute_power_on_skipped_when_already_online(self, chassis):
        """power_on is not issued when host is already ONLINE; on_complete(True) fired."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "dup-event", on_complete=callback)
        daemon._execute_action_item(item)
        daemon.controller.power_on.assert_not_called()
        callback.assert_called_once_with(True)

    def test_execute_power_cycle_not_skipped_when_online(self, chassis):
        """power_cycle always executes regardless of current oper_status."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_cycle = MagicMock(return_value=True)
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_CYCLE, "test")
        daemon._execute_action_item(item)
        daemon.controller.power_cycle.assert_called_once()

    def test_execute_power_off_skipped_when_powering_off_in_progress(self, chassis):
        """power_off is skipped when STATE_DB shows POWERING_OFF (already in progress)."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        # Simulate a power_off already in progress by writing transitional state to DB
        daemon.controller._update_host_state(bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_POWERING_OFF)
        daemon.controller.power_off = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "dup-leak-event", on_complete=callback)
        daemon._execute_action_item(item)
        daemon.controller.power_off.assert_not_called()
        callback.assert_called_once_with(True)

    def test_execute_graceful_shutdown_skipped_when_powering_off_in_progress(self, chassis):
        """graceful_shutdown is skipped when STATE_DB shows POWERING_OFF."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_POWERING_OFF)
        daemon.graceful_shutdown.execute = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "dup-cmd", on_complete=callback)
        daemon._execute_action_item(item)
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(True)

    def test_execute_power_on_skipped_when_powering_on_in_progress(self, chassis):
        """power_on is skipped when STATE_DB shows POWERING_ON (already in progress)."""
        # Host is OFFLINE on platform but DB shows POWERING_ON (race: just started)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_POWERING_ON)
        daemon.controller.power_on = MagicMock(return_value=True)
        callback = MagicMock()
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "dup-on-event", on_complete=callback)
        daemon._execute_action_item(item)
        daemon.controller.power_on.assert_not_called()
        callback.assert_called_once_with(True)


# --------------------------------------------------------------------------
# Tests: BmcctldDaemon - initial power-on sequence
# --------------------------------------------------------------------------

class TestBmcctldDaemonInitialSequence:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
            daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        return daemon

    def test_powers_on_when_no_leak_and_host_offline(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._initial_power_on_sequence()
        daemon.controller.power_on.assert_called_once()

    def test_skips_power_on_if_critical_leak_present(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
        daemon.controller.power_on = MagicMock()
        daemon._initial_power_on_sequence()
        daemon.controller.power_on.assert_not_called()

    def test_refreshes_state_when_already_online(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock()
        daemon.controller.refresh_host_state = MagicMock()
        daemon._initial_power_on_sequence()
        daemon.controller.power_on.assert_not_called()
        daemon.controller.refresh_host_state.assert_called_once()

    def test_stop_event_during_boot_delay_skips_sequence(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock()
        daemon.stop_event.set()  # Signal stop before delay expires
        daemon._initial_power_on_sequence()
        daemon.controller.power_on.assert_not_called()

    def test_boot_delay_processes_queued_actions(self, chassis):
        """Action items queued by the event thread during the boot delay are executed."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        # Use a tiny non-zero delay so the queue-drain loop runs at least once
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0.1)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_off = MagicMock(return_value=True)
        # Simulate a POWER_OFF arriving from Rack Manager during boot delay
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.action_queue.put(bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "RACK_MGR_BOOT_DELAY"))
        daemon._initial_power_on_sequence()
        # The POWER_OFF must have been consumed from the queue during the delay
        assert daemon.action_queue.empty()
        daemon.controller.power_off.assert_called_once()

    def test_rack_mgr_power_off_during_boot_delay_skips_auto_power_on(self, chassis):
        """If Rack Manager POWER_OFF cmd executed during boot delay, automatic power-on is skipped."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._rack_mgr_power_cmd_executed = MagicMock(return_value=True)
        daemon._initial_power_on_sequence()
        daemon.controller.power_on.assert_not_called()

    def test_rack_mgr_power_on_during_boot_delay_skips_auto_power_on(self, chassis):
        """If Rack Manager POWER_ON cmd executed during boot delay, automatic power-on is skipped."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._rack_mgr_power_cmd_executed = MagicMock(return_value=True)
        daemon._initial_power_on_sequence()
        daemon.controller.power_on.assert_not_called()

    def test_rack_mgr_power_cmd_executed_detects_done_power_off(self, chassis):
        """_rack_mgr_power_cmd_executed returns True when POWER_OFF is DONE in RACK_MANAGER_COMMAND."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon.event_handler.state_db, bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_OFF),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_DONE),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    def test_rack_mgr_power_cmd_executed_detects_in_progress_power_on(self, chassis):
        """_rack_mgr_power_cmd_executed returns True when POWER_ON is IN_PROGRESS."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon.event_handler.state_db, bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_ON),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_IN_PROGRESS),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    def test_rack_mgr_power_cmd_executed_ignores_power_cycle(self, chassis):
        """_rack_mgr_power_cmd_executed returns False for POWER_CYCLE (not POWER_ON/OFF/GRACEFUL_SHUT)."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon.event_handler.state_db, bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_CYCLE),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_DONE),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is False

    def test_rack_mgr_power_cmd_executed_detects_graceful_shut(self, chassis):
        """_rack_mgr_power_cmd_executed returns True when GRACEFUL_SHUT is DONE."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon.event_handler.state_db, bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_GRACEFUL_SHUT),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_DONE),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    def test_rack_mgr_power_cmd_executed_ignores_pending(self, chassis):
        """_rack_mgr_power_cmd_executed returns False when command is still PENDING."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon.event_handler.state_db, bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_OFF),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_PENDING),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is False

    def test_rack_mgr_power_cmd_executed_empty_table(self, chassis):
        """_rack_mgr_power_cmd_executed returns False when no commands exist."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon.event_handler.state_db, bmcctld.RACK_MANAGER_COMMAND_TABLE)
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is False


class TestBmcctldDaemonRun:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
        return daemon

    def test_run_not_liquid_cooled_powers_on_immediately(self, chassis):
        """Non-liquid-cooled: power_on is called immediately, no boot delay or leak checks."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock()
        with patch('bmcctld.is_liquid_cooled', return_value=False):
            result = daemon.run()
        assert result is False
        daemon.controller.power_on.assert_called_once()
        daemon._run_action_loop.assert_called_once()

    def test_run_not_liquid_cooled_skips_initial_sequence_but_starts_event_thread(self, chassis):
        """Non-liquid-cooled: event thread starts (for CLI admin cmds), but no boot sequence."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._run_action_loop = MagicMock()
        daemon._initial_power_on_sequence = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock()
        with patch('bmcctld.is_liquid_cooled', return_value=False):
            daemon.run()
        daemon._initial_power_on_sequence.assert_not_called()
        daemon.event_handler.run_event_loop.assert_called_once()

    def test_run_daemon_restart_skips_boot_sequence(self, chassis):
        """Daemon restart: Switch-Host already ONLINE — skip boot sequence entirely."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock()
        daemon.controller.power_on = MagicMock()
        daemon.controller.refresh_host_state = MagicMock()
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock()
        with patch('bmcctld.is_liquid_cooled', return_value=True):
            result = daemon.run()
        assert result is False
        daemon._initial_power_on_sequence.assert_not_called()
        daemon.controller.power_on.assert_not_called()
        daemon.controller.refresh_host_state.assert_called_once()
        daemon._run_action_loop.assert_called_once()

    def test_run_liquid_cooled_runs_full_sequence(self, chassis):
        """Liquid-cooled: event thread and initial power-on sequence are both invoked."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock()
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock()
        # Set stop_event so _run_action_loop returns without looping
        daemon._initial_power_on_sequence.side_effect = lambda: daemon.stop_event.set()
        with patch('bmcctld.is_liquid_cooled', return_value=True):
            result = daemon.run()
        assert result is False
        daemon._initial_power_on_sequence.assert_called_once()

