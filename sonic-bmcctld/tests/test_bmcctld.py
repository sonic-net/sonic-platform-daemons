"""
    Unit tests for bmcctld daemon.

    Tests cover all major event handlers, the three-lane action queue and the
    main-thread dispatch (inline power calls, the graceful thread and its
    queue-derived cancellation), and the startup / stop sequences.
"""

import os
import sys
import copy
import threading
import time
import importlib.util
import importlib.machinery
import builtins
from types import SimpleNamespace

def load_source(module_name, module_path):
    loader = importlib.machinery.SourceFileLoader(module_name, module_path)
    spec = importlib.util.spec_from_file_location(module_name, module_path, loader=loader)
    if module_name in sys.modules:
        module = sys.modules[module_name]
    else:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

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
daemon_base.db_connect = MagicMock(side_effect=lambda db_name: db_name)

load_source('bmcctld', os.path.join(scripts_path, 'bmcctld'))
import bmcctld  # noqa: E402  (loaded via load_source above)

from .mock_platform import MockChassis, MockModule
from .mock_swsscommon import Table, FieldValuePairs

TEST_REQUEST_ID = "12345678-1234-1234-1234-123456789abc"
TEST_UUID1 = "3f2b1c8a-1234-1abc-8def-0123456789ab"
TEST_UUID4 = "3f2b1c8a-1234-4abc-8def-0123456789ab"


@pytest.mark.parametrize("dependency", [
    "grpc", "sonic_grpc.gnoi.client", "sonic_grpc.gnoi",
])
def test_missing_required_gnoi_import_fails_loading(dependency, monkeypatch):
    original_import = builtins.__import__

    def import_without_dependency(name, *args, **kwargs):
        if name == dependency:
            raise ImportError("missing required dependency: " + name)
        return original_import(name, *args, **kwargs)

    module_name = "bmcctld_missing_dependency"
    loader = importlib.machinery.SourceFileLoader(
        module_name, os.path.join(scripts_path, "bmcctld"))
    spec = importlib.util.spec_from_loader(module_name, loader)
    monkeypatch.setitem(sys.modules, module_name, importlib.util.module_from_spec(spec))
    with patch("builtins.__import__", side_effect=import_without_dependency):
        with pytest.raises(ImportError, match="missing required dependency"):
            load_source(module_name, os.path.join(scripts_path, "bmcctld"))


def _make_item(action, event_desc="test", callback=None, startup=False):
    return bmcctld.ActionItem(action, event_desc, on_complete=callback, startup=startup)


def _with_callback(item, callback):
    """A copy of a queued item with another completion callback."""
    item = copy.copy(item)
    item.on_complete = callback
    return item


def _make_operation(action, event_desc="test", callback=None, startup=False):
    """An operation whose cancel is a plain event; daemon-level tests exercise _OpCancel."""
    item = _make_item(action, event_desc, callback, startup)
    return bmcctld.Operation(item, TEST_REQUEST_ID, threading.Event())


def _dequeue_item(action_queue):
    return action_queue.get(timeout=0)


def _make_daemon(chassis, admin_status=None, power_on_delay=0):
    with patch('sonic_platform.platform.Platform') as mock_platform:
        mock_platform.return_value.get_chassis.return_value = chassis
        daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
    if admin_status is not None:
        daemon.policy_reader.get_switch_host_admin_status = MagicMock(
            return_value=admin_status)
    daemon.policy_reader.get_power_on_delay = MagicMock(return_value=power_on_delay)
    return daemon


def _host_state(daemon_or_controller):
    controller = getattr(daemon_or_controller, "controller", daemon_or_controller)
    return dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])


def _without_credentials(graceful_shutdown):
    """Point the operation's credential paths at files that do not exist: loading
    them fails and the leg is not_qualified, whatever the test host has."""
    graceful_shutdown.policy_reader.get_gnoi_cert_paths = MagicMock(return_value={
        field: "/nonexistent/bmc-link/{}.cer".format(field)
        for field in bmcctld.DEFAULT_GNOI_CERT_PATHS})

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_database_mocks():
    bmcctld.swsscommon.reset_mock_db()
    daemon_base.db_connect.reset_mock()


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


@pytest.fixture(autouse=True)
def fast_main_thread_timing(monkeypatch):
    """Keep the main thread's join slices and stop windows short in tests."""
    monkeypatch.setattr(bmcctld, "GRACEFUL_JOIN_SLICE_SECS", 0.02)
    monkeypatch.setattr(bmcctld, "HANDSHAKE_JOIN_SECS", 0.1)


@pytest.fixture
def chassis():
    return MockChassis()


@pytest.fixture
def controller(chassis):
    return bmcctld.SwitchHostController(chassis)


@pytest.fixture
def policy_reader():
    pr = bmcctld.PolicyReader()
    return pr


@pytest.fixture
def critical_event_checker(policy_reader):
    lc = bmcctld.CriticalEventChecker(policy_reader)
    return lc


@pytest.fixture
def graceful_shutdown(controller, policy_reader):
    gs = bmcctld.GracefulShutdownHandler(controller, policy_reader, threading.Event())
    return gs


@pytest.fixture
def event_handler(controller, policy_reader, critical_event_checker):
    eh = bmcctld.BmcEventHandler(
        bmcctld.ActionQueue(), policy_reader,
        critical_event_checker, threading.Event(), controller)
    return eh


# --------------------------------------------------------------------------
# Helper: set a Table entry by directly populating mock_dict
# --------------------------------------------------------------------------

def _set_table_entry(table, key, fields):
    table.mock_dict[key] = fields


def _leak_policy(**overrides):
    """A LEAK_CONTROL_POLICY row with every action syslog_only unless overridden."""
    policy = {
        "system_leak_policy": "enabled", "rack_mgr_leak_policy": "enabled",
        "system_critical_leak_action": "syslog_only", "system_major_leak_action": "syslog_only",
        "system_minor_leak_action": "syslog_only",
        "rack_mgr_critical_alert_action": "syslog_only", "rack_mgr_major_alert_action": "syslog_only",
        "rack_mgr_minor_alert_action": "syslog_only",
    }
    policy.update(overrides)
    return policy


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
        result = controller.power_on(threading.Event())
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert chassis.switch_host.get_admin_state() is True

    def test_power_off_calls_set_admin_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        result = controller.power_off(threading.Event())
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert chassis.switch_host.get_admin_state() is False

    def test_power_cycle_calls_do_power_cycle(self, chassis, controller):
        result = controller.power_cycle(threading.Event())
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert chassis.switch_host.power_cycle_called is True

    def test_power_on_updates_host_state(self, chassis, controller):
        controller.power_on(threading.Event())
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        assert result[0] is True
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_updates_host_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off(threading.Event())
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        assert result[0] is True
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    def test_power_on_writes_transitional_status(self, chassis, controller):
        """STATE_DB device_power_state shows POWERING_ON before the platform set_admin_state call."""
        captured = {}
        original = chassis.switch_host.set_admin_state
        def interceptor(up):
            result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
            if result and result[0]:
                captured.update(dict(result[1]))
            original(up)
        chassis.switch_host.set_admin_state = interceptor
        controller.power_on(threading.Event())
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.SWITCH_HOST_POWERING_ON
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) in (bmcctld.SWITCH_HOST_ONLINE, bmcctld.SWITCH_HOST_OFFLINE)
        # Final entry must reflect POWER_ON and confirmed ONLINE
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_writes_transitional_status(self, chassis, controller):
        """STATE_DB device_power_state shows POWERING_OFF before the platform set_admin_state call."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        captured = {}
        original = chassis.switch_host.set_admin_state
        def interceptor(up):
            result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
            if result and result[0]:
                captured.update(dict(result[1]))
            original(up)
        chassis.switch_host.set_admin_state = interceptor
        controller.power_off(threading.Event())
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.SWITCH_HOST_POWERING_OFF
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) in (bmcctld.SWITCH_HOST_ONLINE, bmcctld.SWITCH_HOST_OFFLINE)
        # Final entry must reflect POWER_OFF and confirmed OFFLINE
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    def test_power_cycle_writes_transitional_status(self, chassis, controller):
        """STATE_DB device_power_state shows POWER_CYCLING before the platform do_power_cycle call."""
        captured = {}
        original = chassis.switch_host.do_power_cycle
        def interceptor():
            result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
            if result and result[0]:
                captured.update(dict(result[1]))
            original()
        chassis.switch_host.do_power_cycle = interceptor
        controller.power_cycle(threading.Event())
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.SWITCH_HOST_POWER_CYCLING
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) in (bmcctld.SWITCH_HOST_ONLINE, bmcctld.SWITCH_HOST_OFFLINE)
        # Final entry must reflect POWER_CYCLE and confirmed ONLINE
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_CYCLE
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_get_db_power_state(self, chassis, controller):
        """get_db_power_state returns the value stored by the last _update_host_state call."""
        controller.power_on(threading.Event())
        assert controller.get_db_power_state() == bmcctld.POWER_STATE_ON

    def test_power_on_leaves_transition_on_issued_call_exception(self, chassis, controller):
        """If set_admin_state raises, the raise may have taken effect: POWERING_ON stays."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.power_off(threading.Event())
        chassis.switch_host.set_admin_state = MagicMock(side_effect=RuntimeError("hw fault"))
        result = controller.power_on(threading.Event())
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWERING_ON

    def test_power_on_restores_state_when_raise_was_never_issued(self, chassis, controller):
        """An exception before the platform call restores the pre-call snapshot."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.power_off(threading.Event())
        chassis.switch_host.set_admin_state = MagicMock()
        real_update = controller._update_host_state
        writes = []

        def failing_transitional_write(power_state, device_status=None):
            writes.append(power_state)
            if power_state == bmcctld.SWITCH_HOST_POWERING_ON:
                raise RuntimeError("write failed")
            return real_update(power_state, device_status)

        controller._update_host_state = failing_transitional_write
        result = controller.power_on(threading.Event())
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        chassis.switch_host.set_admin_state.assert_not_called()
        assert writes == [bmcctld.SWITCH_HOST_POWERING_ON, bmcctld.POWER_STATE_OFF]
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_power_off_leaves_transition_on_exception(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_on(threading.Event())
        chassis.switch_host.set_admin_state = MagicMock(side_effect=RuntimeError("hw fault"))
        result = controller.power_off(threading.Event())
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWERING_OFF

    def test_get_switch_host_module_by_type(self, chassis):
        """If a module explicitly returns MODULE_TYPE_SWITCH_HOST it is selected."""
        ctrl = bmcctld.SwitchHostController(chassis)
        mod = ctrl._get_switch_host_module()
        assert mod is chassis.switch_host

    def test_get_switch_host_module_fallback_index_1(self):
        """Without a SWITCH-HOST type, fall back to module at index 1."""
        ch = MockChassis()
        # Change types so type-based lookup fails
        ch._module_list[1].module_type = "UNKNOWN"
        ctrl = bmcctld.SwitchHostController(ch)
        ctrl._switch_host_module = None  # force re-lookup
        mod = ctrl._get_switch_host_module()
        assert mod is ch._module_list[1]

    def test_init_host_state_infers_power_on_when_not_available(self, chassis, controller):
        # No prior power state recorded — host is ONLINE, so infer POWER_ON
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.init_host_state()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_init_host_state_infers_power_off_when_not_available(self, chassis, controller):
        # No prior power state recorded — host is OFFLINE, so infer POWER_OFF
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.init_host_state()
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    @pytest.mark.parametrize("stale_state", [
        bmcctld.SWITCH_HOST_POWERING_ON,
        bmcctld.SWITCH_HOST_POWER_CYCLING,
        bmcctld.POWER_STATE_ON,
        bmcctld.POWER_STATE_CYCLE,
        bmcctld.SWITCH_HOST_POWERING_OFF,
        bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
    ])
    def test_init_host_state_keeps_other_labels_while_offline(self, chassis, controller, stale_state):
        """OFFLINE settles to POWERED_OFF only over a recorded power-off or no record: any other
        label was written before a platform call and is no evidence that power was removed, and
        the guard trusts POWERED_OFF."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller._update_host_state(stale_state)
        controller.init_host_state()
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == stale_state
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE

    def test_init_host_state_keeps_a_recorded_power_off_while_offline(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller._update_host_state(bmcctld.POWER_STATE_OFF)
        controller.init_host_state()
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_init_host_state_keeps_recorded_label_when_status_is_unknown(
            self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller._update_host_state(bmcctld.POWER_STATE_ON)
        chassis.switch_host.set_oper_status("Fault")
        controller.init_host_state()
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == "FAULT"      # written as read

    def test_init_host_state_resolves_transitional_label_when_online(
            self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_ON)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.init_host_state()
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON

    @pytest.mark.parametrize(
        "death_point,stale_state,live_status,op_result,stale_reason", [
        ("before_host_accept", bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
         MockModule.MODULE_STATUS_ONLINE, "-", "-"),
        ("after_accept_before_watchdog", bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
         MockModule.MODULE_STATUS_ONLINE, "-", "-"),
        ("after_watchdog_arm", bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
         MockModule.MODULE_STATUS_ONLINE, "-", "-"),
        ("before_transitional_write", bmcctld.POWER_STATE_ON,
         MockModule.MODULE_STATUS_ONLINE, None,
         bmcctld.OP_REASON_UNCLASSIFIED),
        ("before_terminal_record", bmcctld.SWITCH_HOST_POWERING_OFF,
         MockModule.MODULE_STATUS_OFFLINE, "-", "-"),
    ])
    @pytest.mark.parametrize("request_id", [TEST_UUID1, TEST_UUID4, TEST_REQUEST_ID])
    def test_stale_operation_is_abandoned_on_startup(
            self, chassis, death_point, stale_state, live_status, op_result,
            stale_reason, request_id):
        # The three death points of the design's handshake leave the same BMC-side state: the
        # host's progress is invisible to a restarted daemon, which is the point of the record.
        retained_state = Table("STATE_DB", bmcctld.HOST_STATE_TABLE)
        fields = [
            (bmcctld.FIELD_DEVICE_POWER_STATE, stale_state),
            (bmcctld.FIELD_DEVICE_STATUS, bmcctld.SWITCH_HOST_ONLINE),
            (bmcctld.FIELD_OP_REQUEST_ID, request_id),
            (bmcctld.FIELD_OP_TRIGGER, death_point),
            (bmcctld.FIELD_OP_REASON, stale_reason),
        ]
        if op_result is not None:
            fields.append((bmcctld.FIELD_OP_RESULT, op_result))
        retained_state.set(
            bmcctld.HOST_STATE_KEY, FieldValuePairs(fields))

        chassis.switch_host.set_oper_status(live_status)
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        chassis.set_liquid_cooled(False)
        with patch('sonic_platform.platform.Platform') as mock_platform:
            mock_platform.return_value.get_chassis.return_value = chassis
            fresh_daemon = bmcctld.BmcctldDaemon(
                bmcctld.SYSLOG_IDENTIFIER)
        fresh_daemon.policy_reader.get_switch_host_admin_status = MagicMock(
            return_value=bmcctld.ADMIN_DOWN)
        fresh_daemon.event_handler.run_event_loop = MagicMock()
        fresh_daemon._run_action_loop = MagicMock()

        assert fresh_daemon.run() is False

        state = dict(retained_state.get(bmcctld.HOST_STATE_KEY)[1])
        if live_status == MockModule.MODULE_STATUS_ONLINE:
            expected_power_state = bmcctld.POWER_STATE_ON
        elif stale_state == bmcctld.POWER_STATE_OFF:
            expected_power_state = bmcctld.POWER_STATE_OFF
        else:
            expected_power_state = stale_state
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == expected_power_state
        assert state[bmcctld.FIELD_DEVICE_STATUS] == str(live_status).upper()
        assert state[bmcctld.FIELD_OP_REQUEST_ID] == request_id
        assert state[bmcctld.FIELD_OP_TRIGGER] == death_point
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
        assert state[bmcctld.FIELD_OP_REASON] == "-"
        hint = ("; the host may still be powered: a power-off must precede the next power-on"
                if stale_state in (bmcctld.SWITCH_HOST_POWERING_OFF,
                                   bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN) else "")
        fresh_daemon.controller.log_warning.assert_called_once_with(
            "OP_ABANDONED request_id={} stale_state={}{}".format(request_id, stale_state, hint))
        assert fresh_daemon.action_queue.empty()
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    @pytest.mark.parametrize("request_id", ["-", ""])
    def test_invalid_request_id_is_not_abandoned(
            self, chassis, controller, request_id):
        controller.host_state_table.set(
            bmcctld.HOST_STATE_KEY, FieldValuePairs([
                (bmcctld.FIELD_DEVICE_POWER_STATE,
                 bmcctld.SWITCH_HOST_POWERING_OFF),
                (bmcctld.FIELD_OP_REQUEST_ID, request_id),
                (bmcctld.FIELD_OP_RESULT, "-"),
            ]))
        controller.init_host_state()
        state = dict(controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == "-"
        controller.log_warning.assert_not_called()

    def test_terminal_operation_is_not_rewritten(self, controller):
        controller.host_state_table.set(
            bmcctld.HOST_STATE_KEY, FieldValuePairs([
                (bmcctld.FIELD_DEVICE_POWER_STATE,
                 bmcctld.SWITCH_HOST_POWERING_OFF),
                (bmcctld.FIELD_OP_REQUEST_ID, TEST_UUID4),
                (bmcctld.FIELD_OP_RESULT, bmcctld.OP_RESULT_POWER_OFF_FAILED),
                (bmcctld.FIELD_OP_REASON, "-"),
            ]))
        controller.init_host_state()
        state = dict(controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED
        controller.log_warning.assert_not_called()

    # -- _verify_oper_status tests --

    def test_verify_oper_status_matches_immediately(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        with patch('time.sleep') as mock_sleep:
            result = controller._verify_oper_status(bmcctld.SWITCH_HOST_ONLINE, 30, "test", threading.Event())
        assert result == bmcctld.PowerCallResult.CONFIRMED
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('time.monotonic')
    def test_verify_oper_status_timeout(self, mock_monotonic, mock_sleep, chassis, controller):
        # Simulate: deadline set at t=0+30=30, first loop check t=0 (<30), sleep,
        # second loop check t=31 (>=30) → exit without match
        mock_monotonic.side_effect = [0, 0, 31, 31]
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        cancel = MagicMock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = False
        result = controller._verify_oper_status(bmcctld.SWITCH_HOST_ONLINE, 30, "test", cancel)
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        cancel.wait.assert_called_once_with(bmcctld.POWER_VERIFY_POLL_INTERVAL_SECS)
        mock_sleep.assert_not_called()

    def test_power_on_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_on(threading.Event())
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        assert chassis.switch_host.get_admin_state() is True  # API was still called

    def test_power_off_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_off(threading.Event())
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        assert chassis.switch_host.get_admin_state() is False  # API was still called

    def test_power_cycle_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_cycle(threading.Event())
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        assert chassis.switch_host.power_cycle_called is True  # API was still called

    def test_power_cycle_uses_double_timeout(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.CONFIRMED) as mock_verify:
            controller.power_cycle(threading.Event())
        mock_verify.assert_called_once_with(
            bmcctld.SWITCH_HOST_ONLINE,
            bmcctld.POWER_VERIFY_TIMEOUT_SECS * 2,
            "power_cycle",
            mock.ANY,
        )

    @pytest.mark.parametrize(
        "action, method_name",
        [
            (bmcctld.ACTION_POWER_OFF, "power_off"),
            (bmcctld.ACTION_POWER_ON, "power_on"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
        ],
    )
    def test_cancel_before_wrapper_entry_issues_no_platform_call(
            self, action, method_name, chassis, controller):
        operation = _make_operation(action)
        operation.cancel.set()
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        result = getattr(controller, method_name)(operation.cancel)
        assert result == bmcctld.PowerCallResult.CANCELLED
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    @pytest.mark.parametrize(
        "action, method_name",
        [
            (bmcctld.ACTION_POWER_OFF, "power_off"),
            (bmcctld.ACTION_POWER_ON, "power_on"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
        ],
    )
    def test_cancel_at_commit_point_issues_no_platform_call_and_rolls_back(
            self, action, method_name, chassis, controller):
        """A stop that lands between the transitional write and the platform call
        issues nothing and restores the previous label."""
        if action == bmcctld.ACTION_POWER_OFF:
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
            controller._update_host_state(bmcctld.POWER_STATE_ON)
        else:
            controller._update_host_state(bmcctld.POWER_STATE_OFF)
        before = _host_state(controller)
        operation = _make_operation(action)
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        checker = MagicMock()
        checker.is_power_on_blocked.side_effect = lambda: operation.cancel.set() or False
        controller.critical_event_checker = checker
        if action == bmcctld.ACTION_POWER_OFF:
            # No gate on a removal: the cancel lands during the transitional write.
            real_update = controller._update_host_state

            def cancel_during_transition(power_state, device_status=None):
                real_update(power_state, device_status)
                if power_state == bmcctld.SWITCH_HOST_POWERING_OFF:
                    operation.cancel.set()

            controller._update_host_state = cancel_during_transition
        result = getattr(controller, method_name)(operation.cancel)
        assert result == bmcctld.PowerCallResult.CANCELLED
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()
        after = _host_state(controller)
        assert after[bmcctld.FIELD_DEVICE_POWER_STATE] == before[bmcctld.FIELD_DEVICE_POWER_STATE]
        assert after[bmcctld.FIELD_DEVICE_STATUS] == before[bmcctld.FIELD_DEVICE_STATUS]

    @pytest.mark.parametrize(
        "action, method_name",
        [
            (bmcctld.ACTION_POWER_ON, "power_on"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
        ],
    )
    def test_raise_is_refused_by_the_gate_reread_at_the_commit_point(
            self, action, method_name, chassis, controller):
        checker = MagicMock()
        checker.is_power_on_blocked.return_value = True
        controller.critical_event_checker = checker
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        operation = _make_operation(action)
        result = getattr(controller, method_name)(operation.cancel)
        assert result == bmcctld.PowerCallResult.REFUSED_LEAK
        checker.is_power_on_blocked.assert_called_once()
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    def test_a_refused_raise_puts_a_transitional_prior_label_back(self, chassis, controller):
        """A stale POWERING_OFF (an interrupted power-off) survives a refused raise: the
        startup hint that a power-off must precede the next power-on depends on it."""
        checker = MagicMock()
        checker.is_power_on_blocked.return_value = True
        controller.critical_event_checker = checker
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_OFF)
        chassis.switch_host.set_admin_state = MagicMock()
        result = controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON).cancel)
        assert result == bmcctld.PowerCallResult.REFUSED_LEAK
        chassis.switch_host.set_admin_state.assert_not_called()
        assert controller.get_db_power_state() == bmcctld.SWITCH_HOST_POWERING_OFF

    @pytest.mark.parametrize(
        "action, method_name",
        [
            (bmcctld.ACTION_POWER_ON, "power_on"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
        ],
    )
    def test_gate_reread_at_the_commit_point_refuses_through_the_policy(
            self, action, method_name, chassis, controller, critical_event_checker):
        """The under-lock re-read uses the real policy-aware predicate: a MAJOR
        Rack Manager alert whose configured action removes power refuses the raise,
        with nothing mocked on the checker."""
        critical_event_checker.policy_reader.get_leak_control_policy = MagicMock(
            return_value=_leak_policy(rack_mgr_major_alert_action=bmcctld.ACTION_POWER_OFF))
        alerts = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(alerts, "Inlet_liquid_flow_rate",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR})
        controller.critical_event_checker = critical_event_checker
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        operation = _make_operation(action)
        result = getattr(controller, method_name)(operation.cancel)
        assert result == bmcctld.PowerCallResult.REFUSED_LEAK
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    def test_verify_wait_is_cancelled_without_poll_interval_delay(
            self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        cancel = MagicMock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = True
        with patch('bmcctld.time.sleep') as sleep:
            result = controller._verify_oper_status(
                bmcctld.SWITCH_HOST_ONLINE, 60, "cancel-test", cancel)
        assert result == bmcctld.PowerCallResult.CANCELLED
        cancel.wait.assert_called_once()
        sleep.assert_not_called()

    @pytest.mark.parametrize(
        "action, method_name, platform_method",
        [
            (bmcctld.ACTION_POWER_OFF, "power_off", "set_admin_state"),
            (bmcctld.ACTION_POWER_ON, "power_on", "set_admin_state"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle", "do_power_cycle"),
        ],
    )
    def test_cancel_during_issued_call_wins_over_call_exception(
            self, action, method_name, platform_method, chassis, controller):
        operation = _make_operation(action)

        def cancel_then_raise(*_args):
            operation.cancel.set()
            raise RuntimeError("platform call failed after cancellation")

        setattr(chassis.switch_host, platform_method, cancel_then_raise)
        result = getattr(controller, method_name)(operation.cancel)

        assert result == bmcctld.PowerCallResult.CANCELLED

    def test_power_on_timeout_leaves_transition(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        operation = _make_operation(bmcctld.ACTION_POWER_ON)
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_on(operation.cancel)
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        state = dict(controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_cycle_timeout_leaves_transition(self, chassis, controller):
        operation = _make_operation(bmcctld.ACTION_POWER_CYCLE)
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_cycle(operation.cancel)
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        state = dict(controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWER_CYCLING


# --------------------------------------------------------------------------
# Tests: PolicyReader
# --------------------------------------------------------------------------

class TestPolicyReader:

    def _make_table_returning(self, key, fields):
        tbl = Table(None, "TEST")
        _set_table_entry(tbl, key, fields)
        return tbl

    def test_get_power_on_delay_default(self, policy_reader):
        """When no CHASSIS_MODULE|SWITCH-HOST entry exists, power_on_delay defaults to 0."""
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            assert policy_reader.get_power_on_delay() == bmcctld.DEFAULT_POWER_ON_DELAY_SECS

    def test_get_power_on_delay_custom(self, policy_reader):
        tbl = Table(None, bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(tbl, bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_POWER_ON_DELAY: "60"})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_power_on_delay() == 60

    def test_get_graceful_shutdown_timeout_default(self, policy_reader):
        """A missing module row uses the graceful timeout default."""
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            assert policy_reader.get_graceful_shutdown_timeout() == 120

    @pytest.mark.parametrize("fields", [
        {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "invalid"},
        {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: ""},
        {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: None},
        {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "-5"},
    ], ids=["missing-field", "invalid", "empty", "null", "negative"])
    def test_get_graceful_shutdown_timeout_fallback(self, policy_reader, fields):
        with patch.object(policy_reader, "_get_chassis_module_entry", return_value=fields):
            assert policy_reader.get_graceful_shutdown_timeout() == 120

    @pytest.mark.parametrize("seconds", [0, 30, 120, 300])
    def test_get_graceful_shutdown_timeout_preserves_explicit_value(self, policy_reader, seconds):
        fields = {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: str(seconds)}
        with patch.object(policy_reader, "_get_chassis_module_entry", return_value=fields):
            assert policy_reader.get_graceful_shutdown_timeout() == seconds

    def test_get_graceful_shutdown_timeout_zero(self, policy_reader):
        tbl = Table(None, bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(tbl, bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "0"})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_graceful_shutdown_timeout() == 0

    @pytest.mark.parametrize("field", [None, "ca_crt", "client_crt", "client_key"])
    def test_certificate_defaults_and_partial_overrides(self, field, policy_reader):
        table = Table("CONFIG_DB", "GNMI_CLIENT")
        expected = {
            "ca_crt": "/etc/sonic/credential/gnmi_root.cer",
            "client_crt": "/etc/sonic/credential/gnmi_client.cer",
            "client_key": "/etc/sonic/credential/gnmi_client.key",
        }
        if field:
            _set_table_entry(table, "certs", {field: "/etc/sonic/custom.pem"})
            expected[field] = "/etc/sonic/custom.pem"
        with patch.object(Table, "set") as write_row:
            paths = policy_reader.get_gnoi_cert_paths()
            assert paths == expected
            paths["ca_crt"] = "modified local copy"
            assert policy_reader.get_gnoi_cert_paths() == expected
        write_row.assert_not_called()

    def test_certificate_reader_projects_only_known_fields(self, policy_reader):
        table = Table("CONFIG_DB", "GNMI_CLIENT")
        _set_table_entry(table, "certs", {"ca_crt": "", "extra": "not a path"})
        paths = policy_reader.get_gnoi_cert_paths()
        assert set(paths) == {"ca_crt", "client_crt", "client_key"}
        assert paths["ca_crt"] == ""

    def test_chassis_module_entry_all_fields(self, policy_reader):
        """All three fields coexist in CHASSIS_MODULE|SWITCH-HOST."""
        tbl = Table(None, bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(tbl, bmcctld.SWITCH_HOST_MODULE_KEY, {
            bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP,
            bmcctld.FIELD_POWER_ON_DELAY: "300",
            bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "90",
        })
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_switch_host_admin_status() == bmcctld.ADMIN_UP
            assert policy_reader.get_power_on_delay() == 300
            assert policy_reader.get_graceful_shutdown_timeout() == 90

    def test_get_switch_host_admin_status_default(self, policy_reader):
        """When no CHASSIS_MODULE entry exists, admin_status defaults to 'down'."""
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            assert policy_reader.get_switch_host_admin_status() == bmcctld.ADMIN_DOWN

    def test_get_switch_host_admin_status_up(self, policy_reader):
        tbl = Table(None, bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(tbl, bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_switch_host_admin_status() == bmcctld.ADMIN_UP

    def test_get_switch_host_admin_status_down(self, policy_reader):
        tbl = Table(None, bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(tbl, bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert policy_reader.get_switch_host_admin_status() == bmcctld.ADMIN_DOWN

    def test_get_leak_control_policy_defaults(self, policy_reader):
        with patch.object(bmcctld.swsscommon, 'Table', return_value=Table(None, "T")):
            policy = policy_reader.get_leak_control_policy()
        assert policy["system_leak_policy"] == "enabled"
        assert policy["system_critical_leak_action"] == bmcctld.ACTION_POWER_OFF
        assert policy["system_major_leak_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["system_minor_leak_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["rack_mgr_leak_policy"] == "enabled"
        assert policy["rack_mgr_critical_alert_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["rack_mgr_major_alert_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["rack_mgr_minor_alert_action"] == bmcctld.ACTION_SYSLOG_ONLY

    def test_get_leak_control_policy_custom(self, policy_reader):
        tbl = Table(None, bmcctld.LEAK_CONTROL_POLICY_TABLE)
        _set_table_entry(tbl, "policy", {
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
                         {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_system_leak() is True

    def test_minor_system_leak_not_critical(self, critical_event_checker):
        tbl = Table(None, bmcctld.SYSTEM_LEAK_STATUS_TABLE)
        _set_table_entry(tbl, bmcctld.SYSTEM_LEAK_STATUS_KEY,
                         {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MINOR})
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

    def test_is_power_on_blocked_system(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=True)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=False)
        assert critical_event_checker.is_power_on_blocked() is True

    def test_is_power_on_blocked_rack_mgr(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=False)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=True)
        assert critical_event_checker.is_power_on_blocked() is True

    def test_no_critical_events(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=False)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=False)
        assert critical_event_checker.is_power_on_blocked() is False

    def _power_down_checker(self, checker, policy, system_status="", rack_alerts=None):
        """Configure the checker with a policy, system leak status and rack alerts."""
        full_policy = {
            "system_leak_policy": "enabled", "rack_mgr_leak_policy": "enabled",
            "system_critical_leak_action": "syslog_only", "system_major_leak_action": "syslog_only",
            "system_minor_leak_action": "syslog_only",
            "rack_mgr_critical_alert_action": "syslog_only", "rack_mgr_major_alert_action": "syslog_only",
            "rack_mgr_minor_alert_action": "syslog_only",
        }
        full_policy.update(policy)
        checker.policy_reader.get_leak_control_policy = MagicMock(return_value=full_policy)
        checker.get_system_leak_status = MagicMock(return_value=system_status)
        tbl = Table(None, bmcctld.RACK_MANAGER_ALERT_TABLE)
        for key, severity in (rack_alerts or {}).items():
            _set_table_entry(tbl, key, {bmcctld.FIELD_SEVERITY: severity})
        return tbl

    @pytest.mark.parametrize("status,policy,expected", [
        ("MAJOR", {"system_major_leak_action": "power_off"}, True),
        ("MAJOR", {"system_major_leak_action": "graceful_shutdown"}, True),
        ("MAJOR", {"system_minor_leak_action": "power_off"}, True),
        ("MINOR", {"system_minor_leak_action": "power_off"}, True),
        ("MINOR", {"system_major_leak_action": "power_off"}, False),
        ("MAJOR", {}, False),
        ("MINOR", {}, False),
        ("None", {"system_major_leak_action": "power_off"}, False),
        ("", {"system_major_leak_action": "power_off"}, False),
        ("MAJOR", {"system_major_leak_action": "power_off", "system_leak_policy": "disabled"}, False),
    ])
    def test_power_down_event_system(self, critical_event_checker, status, policy, expected):
        tbl = self._power_down_checker(critical_event_checker, policy, system_status=status)
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_power_down_event() is expected

    @pytest.mark.parametrize("alerts,policy,expected", [
        ({"Inlet_liquid_flow_rate": "MAJOR"}, {"rack_mgr_major_alert_action": "power_off"}, True),
        ({"Inlet_liquid_flow_rate": "MINOR"}, {"rack_mgr_minor_alert_action": "graceful_shutdown"}, True),
        ({"Inlet_liquid_flow_rate": "MAJOR"}, {"rack_mgr_minor_alert_action": "power_off"}, True),
        ({"Inlet_liquid_flow_rate": "MINOR"}, {"rack_mgr_major_alert_action": "power_off"}, False),
        ({"Inlet_liquid_flow_rate": "MAJOR"}, {}, False),
        ({"Inlet_liquid_flow_rate": "MINOR", "Inlet_liquid_pressure": "MAJOR"},
         {"rack_mgr_major_alert_action": "power_off"}, True),
        ({}, {"rack_mgr_major_alert_action": "power_off"}, False),
        ({"Inlet_liquid_flow_rate": "MAJOR"},
         {"rack_mgr_major_alert_action": "power_off", "rack_mgr_leak_policy": "disabled"}, False),
    ])
    def test_power_down_event_rack_mgr(self, critical_event_checker, alerts, policy, expected):
        tbl = self._power_down_checker(critical_event_checker, policy, rack_alerts=alerts)
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_power_down_event() is expected

    def test_power_down_event_reads_the_rack_level_leak_field(self, critical_event_checker):
        """Rack_level_leak reports through its `leak` field, not `severity`."""
        tbl = self._power_down_checker(critical_event_checker, {"rack_mgr_major_alert_action": "power_off"})
        _set_table_entry(tbl, "Rack_level_leak", {bmcctld.FIELD_LEAK: bmcctld.ALERT_SEVERITY_MAJOR})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_power_down_event() is True
            assert critical_event_checker.is_power_on_blocked() is True

    def test_is_power_on_blocked_ignores_a_critical_system_leak_under_a_disabled_policy(
            self, critical_event_checker):
        self._power_down_checker(critical_event_checker, {"system_leak_policy": "disabled"},
                                 system_status=bmcctld.SystemLeakStatus.CRITICAL)
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=True)
        assert critical_event_checker.is_power_on_blocked() is False

    def test_is_power_on_blocked_ignores_a_critical_rack_alert_under_a_disabled_policy(
            self, critical_event_checker):
        tbl = self._power_down_checker(critical_event_checker, {"rack_mgr_leak_policy": "disabled"},
                                       rack_alerts={"Inlet_liquid_flow_rate": bmcctld.ALERT_SEVERITY_CRITICAL})
        with patch.object(bmcctld.swsscommon, 'Table', return_value=tbl):
            assert critical_event_checker.has_critical_rack_mgr_alert() is True
            assert critical_event_checker.is_power_on_blocked() is False

    def test_is_power_on_blocked_power_down_action(self, critical_event_checker):
        critical_event_checker.has_critical_system_leak = MagicMock(return_value=False)
        critical_event_checker.has_critical_rack_mgr_alert = MagicMock(return_value=False)
        critical_event_checker.has_power_down_event = MagicMock(return_value=True)
        critical_event_checker.policy_reader.get_leak_control_policy = MagicMock(
            return_value={"system_leak_policy": "enabled", "rack_mgr_leak_policy": "enabled"})
        assert critical_event_checker.is_power_on_blocked() is True



# --------------------------------------------------------------------------
# Tests: GracefulShutdownHandler
# --------------------------------------------------------------------------

class TestGracefulShutdownHandler:

    def test_powering_off_state_set_before_gnoi(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        captured = {}
        original = graceful_shutdown.controller._update_host_state
        def capture_first(power_state, device_status=None):
            if not captured:
                captured['power_state'] = power_state
            return original(power_state, device_status)
        graceful_shutdown.controller._update_host_state = capture_first
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "failed [bmc-req:{}]".format(TEST_REQUEST_ID), status=2)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        graceful_shutdown.execute(
            _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
            MagicMock(return_value=requester))
        assert captured.get('power_state') == bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN

    def test_shutdown_delay_zero_skips_gnoi(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=0)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        factory = MagicMock()
        outcome = graceful_shutdown.execute(
            _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN), factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            True,
        )
        factory.assert_not_called()
        graceful_shutdown.controller.power_off.assert_called_once()

    def test_gnoi_fails_triggers_power_off(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        requester = MagicMock()
        requester.send_halt.side_effect = bmcctld.GnoiRpcError("unreachable")
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        outcome = graceful_shutdown.execute(
            _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
            MagicMock(return_value=requester))
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_RPC_FAILURE,
            True,
        )
        graceful_shutdown.controller.power_off.assert_called_once()

    def test_gnoi_success_and_host_goes_offline_still_calls_power_off(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "complete [bmc-req:{}]".format(TEST_REQUEST_ID))
        states = []
        update_state = graceful_shutdown.controller._update_host_state

        def record_state(power_state, device_status=None):
            states.append(power_state)
            return update_state(power_state, device_status)

        graceful_shutdown.controller._update_host_state = MagicMock(
            side_effect=record_state)
        outcome = graceful_shutdown.execute(
            _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
            MagicMock(return_value=requester))
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        assert states == [
            bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
            bmcctld.SWITCH_HOST_POWERING_OFF,
            bmcctld.POWER_STATE_OFF,
        ]
        assert chassis.switch_host.get_admin_state() is False
        requester.close.assert_called_once()

    def test_gnoi_timeout_triggers_power_off(self, graceful_shutdown, chassis):
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=1)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        with patch.object(operation.cancel, 'wait', side_effect=lambda _delay: False), \
                patch('bmcctld.time.monotonic', side_effect=[0, 0, 0, 0, 2]):
            outcome = graceful_shutdown.execute(
                operation, MagicMock(return_value=requester))
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_DEADLINE,
            True,
        )
        graceful_shutdown.controller.power_off.assert_called_once()

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

    def test_get_switch_host_gnoi_port_default(self, graceful_shutdown):
        with patch('builtins.open', side_effect=FileNotFoundError):
            port = graceful_shutdown._get_switch_host_gnoi_port()
        assert port == 8080

    def test_get_switch_host_gnoi_port_from_bmc_json(self, graceful_shutdown, tmp_path):
        import json
        bmc_json = tmp_path / "bmc.json"
        bmc_json.write_text(json.dumps({"switch_host_gnmi_port": 8443}))
        with patch.object(bmcctld, 'BMC_JSON_PATHS', [str(bmc_json)]):
            port = graceful_shutdown._get_switch_host_gnoi_port()
        assert port == 8443

    def test_bmc_json_that_is_not_an_object_is_skipped(self, graceful_shutdown, tmp_path):
        """A bmc.json whose top level is not an object is skipped like a broken one;
        it must not reach the handshake as an AttributeError."""
        bmc_json = tmp_path / "bmc.json"
        bmc_json.write_text("[1, 2]")
        with patch.object(bmcctld, 'BMC_JSON_PATHS', [str(bmc_json)]):
            assert graceful_shutdown._get_switch_host_addr() == bmcctld.DEFAULT_SWITCH_HOST_ADDR
            assert graceful_shutdown._get_switch_host_gnoi_port() == bmcctld.DEFAULT_SWITCH_HOST_GNMI_PORT

    @pytest.mark.parametrize("first_data,expected_addr,expected_port", [
        ({"bmc_if_addr": "10.0.0.1"}, "10.0.0.1", 8443),
        ({"switch_host_gnmi_port": 9443}, "10.0.0.2", 9443),
        ({"bmc_if_addr": "", "switch_host_gnmi_port": None}, "10.0.0.2", 8443),
        ({"bmc_if_addr": "", "switch_host_gnmi_port": 0}, "10.0.0.2", 0),
    ])
    def test_bmc_link_settings_keep_independent_precedence(
            self, graceful_shutdown, tmp_path, first_data, expected_addr,
            expected_port):
        import json
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        broken = tmp_path / "broken.json"
        first.write_text(json.dumps(first_data))
        second.write_text(json.dumps({
            "bmc_if_addr": "10.0.0.2", "switch_host_gnmi_port": 8443,
        }))
        broken.write_text("invalid json")
        with patch.object(bmcctld, "BMC_JSON_PATHS", [
                str(tmp_path / "missing.json"), str(broken), str(first), str(second)]):
            assert graceful_shutdown._get_switch_host_addr() == expected_addr
            assert graceful_shutdown._get_switch_host_gnoi_port() == expected_port

    @pytest.mark.parametrize("graceful,reason", [
        (True, "-"), (False, bmcctld.OP_REASON_DEADLINE),
        (False, bmcctld.OP_REASON_PREEMPTED),
    ])
    @pytest.mark.parametrize("power_result", [
        bmcctld.PowerCallResult.CANCELLED,
        bmcctld.PowerCallResult.NOT_CONFIRMED,
    ])
    def test_shutdown_off_failure_outcomes(
            self, graceful_shutdown, graceful, reason, power_result):
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        graceful_shutdown._run_shutdown_leg = MagicMock(return_value=(graceful, reason))
        graceful_shutdown.controller.power_off = MagicMock(return_value=power_result)
        graceful_shutdown.controller.power_on = MagicMock()
        graceful_shutdown._event_log = MagicMock()

        outcome = graceful_shutdown.execute(operation, MagicMock())

        if reason == bmcctld.OP_REASON_PREEMPTED:
            expected_result = bmcctld.OP_RESULT_PREEMPTED
            graceful_shutdown.controller.power_off.assert_not_called()
        else:
            expected_result = (bmcctld.OP_RESULT_PREEMPTED
                               if power_result == bmcctld.PowerCallResult.CANCELLED
                               else bmcctld.OP_RESULT_POWER_OFF_FAILED)
            graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
        assert outcome == (expected_result, reason, False)
        graceful_shutdown._run_shutdown_leg.assert_called_once()
        graceful_shutdown.controller.power_on.assert_not_called()
        if expected_result == bmcctld.OP_RESULT_POWER_OFF_FAILED:
            graceful_shutdown._event_log.log_error.assert_called_once_with(
                "POWER_OFF_FAILED request_id={}".format(operation.request_id))
        else:
            graceful_shutdown._event_log.log_error.assert_not_called()


# --------------------------------------------------------------------------
# Tests: graceful qualification, report classification, and gNOI transport
# --------------------------------------------------------------------------

OUR_REQUEST_ID = "3f2b1c8a-1234-4abc-8def-0123456789ab"
OTHER_REQUEST_ID = "7a25d48e-9876-4fed-8cba-fedcba987654"


def _report(reason, active=False, method=3, status=1, message=""):
    return SimpleNamespace(
        reason=reason,
        active=active,
        method=method,
        status=SimpleNamespace(status=status, message=message),
    )


class TestReportClassifier:

    @pytest.mark.parametrize(
        "resp, expected",
        [
            (_report("done [bmc-req:{}]".format(OUR_REQUEST_ID)),
             ("graceful", None)),
            (_report("active [bmc-req:{}]".format(OUR_REQUEST_ID), active=True),
             ("keep_waiting", None)),
            (_report("failed [bmc-req:{}]".format(OUR_REQUEST_ID), status=2),
             ("forced", bmcctld.OP_REASON_CHECK_FAILED)),
            (_report("failed [bmc-req:{}]".format(OUR_REQUEST_ID), status=2,
                     message="backend failure"),
             ("forced", bmcctld.OP_REASON_BACKEND_ANSWERED)),
            (_report("done [bmc-req:{}]".format(OUR_REQUEST_ID),
                     message="backend answer"),
             ("forced", bmcctld.OP_REASON_BACKEND_ANSWERED)),
            (_report("done [bmc-req:{}]".format(OTHER_REQUEST_ID)),
             ("keep_waiting", None)),
            (_report("done [bmc-req:{}0]".format(OUR_REQUEST_ID)),
             ("keep_waiting", None)),
            (_report("done [bmc-req:{}]".format(OUR_REQUEST_ID[:-1])),
             ("keep_waiting", None)),
            (_report("done without a tag"), ("keep_waiting", None)),
            (_report("done [bmc-req:{0}] [bmc-req:{0}]".format(OUR_REQUEST_ID)),
             ("keep_waiting", None)),
            (_report("done [bmc-req:{}]".format(OUR_REQUEST_ID), method=1),
             ("forced", bmcctld.OP_REASON_CHECK_FAILED)),
        ],
        ids=[
            "graceful", "active", "host-failure", "backend-failure",
            "backend-success", "foreign", "longer-id", "shorter-id",
            "untagged", "duplicate-tag", "wrong-method",
        ],
    )
    def test_report_attribution(self, resp, expected):
        assert bmcctld.classify_report(resp, OUR_REQUEST_ID) == expected


class FakeGrpcError(Exception):
    pass


class TestGnoiRequester:

    def _make_requester(self, monkeypatch, tmp_path):
        cert_dir = tmp_path / "certs"
        cert_dir.mkdir()
        (cert_dir / "ca.cer").write_bytes(b"ca")
        (cert_dir / "client.key").write_bytes(b"key")
        (cert_dir / "client.cer").write_bytes(b"cert")

        grpc_api = SimpleNamespace(
            RpcError=FakeGrpcError,
            ssl_channel_credentials=MagicMock(return_value="credentials"),
        )
        proto_api = SimpleNamespace(
            HALT=3,
            RebootStatus=SimpleNamespace(STATUS_SUCCESS=1),
            RebootRequest=MagicMock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            RebootStatusRequest=MagicMock(return_value="status-request"),
        )
        client = MagicMock()
        client_factory = MagicMock(return_value=client)

        monkeypatch.setattr(bmcctld, "grpc", grpc_api)
        monkeypatch.setattr(bmcctld, "system_pb2", proto_api)
        monkeypatch.setattr(bmcctld, "GnoiClient", client_factory)

        requester = bmcctld.GnoiRequester(
            "169.254.100.2", 8080,
            {"ca_crt": str(cert_dir / "ca.cer"),
             "client_crt": str(cert_dir / "client.cer"),
             "client_key": str(cert_dir / "client.key")},
            bmcctld.GNOI_SERVER_NAME)
        return requester, grpc_api, proto_api, client_factory, client, cert_dir

    @pytest.mark.parametrize("error", [RuntimeError("client closed"), ValueError("channel state")])
    def test_a_closed_client_failing_an_rpc_is_an_rpc_failure(self, error, monkeypatch, tmp_path):
        """GnoiClient raises RuntimeError once close() ran under it (a cancellation) and grpc
        raises ValueError in places; both are normalised like an RpcError."""
        requester, _, _, _, client, _ = self._make_requester(monkeypatch, tmp_path)
        requester.open()
        client.system.Reboot.side_effect = error
        client.system.RebootStatus.side_effect = error
        with pytest.raises(bmcctld.GnoiRpcError):
            requester.send_halt("message", 30)
        with pytest.raises(bmcctld.GnoiRpcError):
            requester.poll_status(10)

    def test_close_normalizes_any_channel_failure(self):
        requester = bmcctld.GnoiRequester(
            "169.254.100.2", 8080, dict(bmcctld.DEFAULT_GNOI_CERT_PATHS),
            bmcctld.GNOI_SERVER_NAME)
        client = MagicMock()
        client.__exit__.side_effect = RuntimeError("channel teardown failed")
        requester._client = client
        with pytest.raises(bmcctld.GnoiRpcError, match="channel teardown failed"):
            requester.close()
        assert requester._client is None
        requester.close()
        client.__exit__.assert_called_once()

    def test_requester_uses_exact_mtls_and_rpc_shapes(self, monkeypatch, tmp_path):
        requester, grpc_api, proto_api, client_factory, client, _ = \
            self._make_requester(monkeypatch, tmp_path)

        requester.open()
        grpc_api.ssl_channel_credentials.assert_called_once_with(
            root_certificates=b"ca", private_key=b"key", certificate_chain=b"cert")
        client_factory.assert_called_once_with(
            "169.254.100.2:8080",
            options=(("grpc.ssl_target_name_override", "switch-host.bmc-link.sonic"),),
            credentials="credentials",
        )
        client.__enter__.assert_called_once_with()

        requester.send_halt("BMC pre-shutdown request [bmc-req:{}]".format(OUR_REQUEST_ID), 30)
        proto_api.RebootRequest.assert_called_once_with(
            method=3,
            message="BMC pre-shutdown request [bmc-req:{}]".format(OUR_REQUEST_ID),
        )
        reboot_request = client.system.Reboot.call_args.args[0]
        assert reboot_request.method == 3
        assert reboot_request.message.endswith("[bmc-req:{}]".format(OUR_REQUEST_ID))
        client.system.Reboot.assert_called_once_with(reboot_request, timeout=30)

        requester.poll_status(10)
        proto_api.RebootStatusRequest.assert_called_once_with()
        client.system.RebootStatus.assert_called_once_with("status-request", timeout=10)

        requester.close()
        client.__exit__.assert_called_once_with(None, None, None)

    @pytest.mark.parametrize(
        "failure_point",
        ["cert-read", "credentials", "client-open", "send", "poll", "close"],
    )
    def test_transport_failures_are_normalized_once(
            self, failure_point, monkeypatch, tmp_path):
        requester, grpc_api, _, client_factory, client, cert_dir = \
            self._make_requester(monkeypatch, tmp_path)

        if failure_point == "cert-read":
            (cert_dir / "ca.cer").unlink()
        elif failure_point == "credentials":
            grpc_api.ssl_channel_credentials.side_effect = ValueError("bad PEM")
        elif failure_point == "client-open":
            client.__enter__.side_effect = FakeGrpcError("channel failed")
        else:
            requester.open()
            if failure_point == "send":
                client.system.Reboot.side_effect = FakeGrpcError("send failed")
            elif failure_point == "poll":
                client.system.RebootStatus.side_effect = FakeGrpcError("poll failed")
            else:
                client.__exit__.side_effect = FakeGrpcError("close failed")

        with pytest.raises(bmcctld.GnoiRpcError):
            if failure_point in ("cert-read", "credentials", "client-open"):
                requester.open()
            elif failure_point == "send":
                requester.send_halt("message", 30)
            elif failure_point == "poll":
                requester.poll_status(10)
            else:
                requester.close()

        if failure_point == "credentials":
            assert grpc_api.ssl_channel_credentials.call_count == 1
        elif failure_point == "client-open":
            assert client.__enter__.call_count == 1
        elif failure_point == "send":
            assert client.system.Reboot.call_count == 1
        elif failure_point == "poll":
            assert client.system.RebootStatus.call_count == 1
        elif failure_point == "close":
            assert client.__exit__.call_count == 1
        else:
            client_factory.assert_not_called()


class TestConfiguredCertificatePaths:

    @staticmethod
    def _watch_reads():
        """Patch Table.get for every Table the daemon builds; the reads still reach the data."""
        return patch.object(Table, "get", autospec=True, side_effect=Table.get)

    @staticmethod
    def _cert_reads(read_row):
        return [c.args[1] for c in read_row.call_args_list
                if c.args[0].table_name == bmcctld.GNMI_CLIENT_TABLE]

    def _setup(self, monkeypatch, tmp_path, graceful_shutdown, chassis):
        _, grpc_api, _, _, client, _ = TestGnoiRequester()._make_requester(
            monkeypatch, tmp_path)
        paths = {
            "ca_crt": str(tmp_path / "trust" / "root.pem"),
            "client_crt": str(tmp_path / "identity" / "certificate.pem"),
            "client_key": str(tmp_path / "identity" / "private.key"),
        }
        for field, path in paths.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(field.encode())
        table = Table("CONFIG_DB", "GNMI_CLIENT")
        _set_table_entry(table, "certs", paths)
        timeout_table = Table("CONFIG_DB", bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(timeout_table, bmcctld.SWITCH_HOST_MODULE_KEY,
                         {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "10"})
        graceful_shutdown._get_switch_host_addr = MagicMock(return_value="169.254.100.2")
        graceful_shutdown._get_switch_host_gnoi_port = MagicMock(return_value=8080)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        graceful_shutdown.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        client.system.RebootStatus.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        return paths, table, grpc_api, client

    def test_configured_files_reach_tls_through_operation(
            self, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, table, grpc_api, client = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        operation.cancel.wait = MagicMock(return_value=False)
        with self._watch_reads() as read_row, \
                patch("builtins.open", wraps=builtins.open) as read_file:
            outcome = graceful_shutdown.execute(operation, bmcctld.GnoiRequester)

        assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        assert self._cert_reads(read_row) == ["certs"]
        assert read_file.call_args_list == [
            call(paths["ca_crt"], "rb"), call(paths["client_key"], "rb"),
            call(paths["client_crt"], "rb"),
        ]
        grpc_api.ssl_channel_credentials.assert_called_once_with(
            root_certificates=b"ca_crt", private_key=b"client_key",
            certificate_chain=b"client_crt")
        client.system.Reboot.assert_called_once()
        client.__exit__.assert_called_once_with(None, None, None)
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
        graceful_shutdown.controller.power_on.assert_not_called()

    def test_credential_paths_are_resolved_once_per_operation(
            self, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, table, grpc_api, _ = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        replacement = tmp_path / "replacement.pem"
        replacement.write_bytes(b"new-ca")

        class ChangingRequester(bmcctld.GnoiRequester):
            """Changes the configuration once the paths have been resolved."""

            def open(self):
                table.set("certs", FieldValuePairs([
                    ("ca_crt", str(replacement)), ("extra", "ignored")]))
                return super().open()

        with self._watch_reads() as read_row:
            for _ in range(2):
                outcome = graceful_shutdown.execute(
                    _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
                    ChangingRequester)
                assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        assert self._cert_reads(read_row) == ["certs", "certs"]
        assert grpc_api.ssl_channel_credentials.call_args_list == [
            call(root_certificates=b"ca_crt", private_key=b"client_key",
                 certificate_chain=b"client_crt"),
            call(root_certificates=b"new-ca", private_key=b"client_key",
                 certificate_chain=b"client_crt"),
        ]

    @pytest.mark.parametrize("field", ["ca_crt", "client_crt", "client_key"])
    @pytest.mark.parametrize("failure", ["empty", "relative", "missing", "empty-file"])
    def test_invalid_selected_path_does_not_fall_back(
            self, field, failure, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, table, grpc_api, client = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        monkeypatch.setattr(bmcctld, "DEFAULT_GNOI_CERT_PATHS", dict(paths))
        empty_file = tmp_path / "empty.pem"
        empty_file.touch()
        invalid = {"empty": "", "relative": "relative.pem",
                   "missing": str(tmp_path / "missing.pem"),
                   "empty-file": str(empty_file)}[failure]
        _set_table_entry(table, "certs", {field: invalid})
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        assert graceful_shutdown.execute(operation, bmcctld.GnoiRequester) == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_NOT_QUALIFIED, True)
        grpc_api.ssl_channel_credentials.assert_not_called()
        client.system.Reboot.assert_not_called()
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
        assert field in graceful_shutdown.log_warning.call_args.args[0]

    @pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
    def test_selected_file_read_failure_is_not_qualified(
            self, failure, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, _, grpc_api, client = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        with patch("builtins.open", side_effect=failure("file changed")) as read_file:
            outcome = graceful_shutdown.execute(
                _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
                bmcctld.GnoiRequester)
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_FORCED,
                           bmcctld.OP_REASON_NOT_QUALIFIED, True)
        read_file.assert_called_once_with(paths["ca_crt"], "rb")
        grpc_api.ssl_channel_credentials.assert_not_called()
        client.system.Reboot.assert_not_called()

    @pytest.mark.parametrize("power_result", [
        bmcctld.PowerCallResult.CONFIRMED, bmcctld.PowerCallResult.NOT_CONFIRMED,
    ])
    @pytest.mark.parametrize("cancelled", [False, True])
    def test_db_read_error_forces_the_leg(
            self, power_result, cancelled,
            monkeypatch, tmp_path, graceful_shutdown, chassis):
        self._setup(monkeypatch, tmp_path, graceful_shutdown, chassis)
        real_get = Table.get

        def failing_cert_read(table, key):
            if table.table_name == bmcctld.GNMI_CLIENT_TABLE:
                raise RuntimeError("DB unavailable")
            return real_get(table, key)

        controller = graceful_shutdown.controller
        controller.power_off.return_value = power_result
        controller._update_host_state = MagicMock()
        factory = MagicMock()
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        if cancelled:
            operation.cancel.set()
        with patch.object(Table, "get", autospec=True, side_effect=failing_cert_read) as read_row:
            outcome = graceful_shutdown.execute(operation, factory)
        factory.assert_not_called()
        controller._update_host_state.assert_not_called()
        controller.power_on.assert_not_called()
        if cancelled:
            # Cancelled before the leg started: nothing is read, nothing fails.
            read_row.assert_not_called()
            controller.power_off.assert_not_called()
            assert outcome[0] == bmcctld.OP_RESULT_PREEMPTED
        else:
            assert self._cert_reads(read_row) == ["certs"]
            controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
            expected = (bmcctld.OP_RESULT_SUCCESS_FORCED
                        if power_result == bmcctld.PowerCallResult.CONFIRMED
                        else bmcctld.OP_RESULT_POWER_OFF_FAILED)
            assert outcome == (
                expected, bmcctld.OP_REASON_UNCLASSIFIED,
                expected == bmcctld.OP_RESULT_SUCCESS_FORCED)

class FakeClock:
    """A clock the test drives. With auto_advance every read also moves it a little, so a
    loop that waits on it ends instead of hanging when the code under test regresses."""

    def __init__(self, now=0.0, auto_advance=0.0):
        self.now = now
        self.auto_advance = auto_advance

    def __call__(self):
        self.now += self.auto_advance
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestGracefulShutdownOperation:

    @pytest.mark.parametrize("config", ["fresh", "missing-field", "stored-zero"])
    def test_default_and_stored_timeout_drive_graceful_leg(
            self, config, graceful_shutdown, chassis):
        controller = graceful_shutdown.controller
        table = controller.chassis_module_config_table
        if config == "stored-zero":
            table.set(bmcctld.SWITCH_HOST_MODULE_KEY, [
                (bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT, "0")])
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        if config == "missing-field":
            table.hdel(bmcctld.SWITCH_HOST_MODULE_KEY,
                       bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)
        factory = MagicMock(return_value=requester)
        clock = FakeClock()
        operation.cancel.wait = MagicMock(
            side_effect=lambda delay: clock.advance(delay) or False)
        with patch("bmcctld.time.monotonic", side_effect=clock):
            outcome = graceful_shutdown.execute(operation, factory)

        if config == "stored-zero":
            expected_reason = bmcctld.OP_REASON_TIMEOUT_ZERO
            factory.assert_not_called()
            expected_wait = 0
        else:
            expected_reason = bmcctld.OP_REASON_DEADLINE
            factory.assert_called_once()
            requester.send_halt.assert_called_once_with(
                "BMC pre-shutdown request [bmc-req:{}]".format(TEST_REQUEST_ID),
                timeout_secs=30)
            assert requester.poll_status.call_count == 120
            requester.close.assert_called_once()
            expected_wait = 120
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_FORCED, expected_reason, True)
        controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
        controller.power_on.assert_not_called()
        assert clock.now == expected_wait

    @pytest.mark.parametrize("fails_at, expected_reason", [
        (10.0, bmcctld.OP_REASON_DEADLINE),         # the poll's timeout was the time left
        (9.9, bmcctld.OP_REASON_RPC_FAILURE),       # the host failed the poll before the deadline
    ])
    def test_poll_ended_by_the_deadline_records_deadline(
            self, fails_at, expected_reason, graceful_shutdown, chassis):
        """A report that stays active runs to the deadline. When the deadline ends the
        last poll in flight the reason is still deadline, not rpc_failure."""
        operation, requester, factory = self._setup(graceful_shutdown, chassis, timeout=10)
        clock = FakeClock()
        operation.cancel.wait = MagicMock(
            side_effect=lambda delay: clock.advance(delay) or False)

        def poll_status(timeout_secs):
            if timeout_secs < 0.4:          # each poll takes 0.4 s; this one has 0.2 s left
                clock.now = fails_at
                raise bmcctld.GnoiRpcError("poll did not complete")
            clock.advance(0.4)
            return _report("active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)

        requester.poll_status.side_effect = poll_status
        with patch("bmcctld.time.monotonic", side_effect=clock):
            outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_FORCED, expected_reason, True)
        assert requester.poll_status.call_args.kwargs["timeout_secs"] == pytest.approx(0.2)

    def _setup(self, graceful_shutdown, chassis, timeout=10,
               power_result=None):
        if power_result is None:
            power_result = bmcctld.PowerCallResult.CONFIRMED
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = \
            MagicMock(return_value=timeout)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=power_result)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        requester = MagicMock()
        factory = MagicMock(return_value=requester)
        return operation, requester, factory

    def test_operation_start_race_without_removal_evidence_issues_power_off(
            self, graceful_shutdown, chassis):
        # The host reads off when the leg starts, but nothing bmcctld recorded says
        # its power was removed: OFFLINE alone is not that evidence (the host may be
        # booting), so the HALT is attempted and the platform call is made, as the
        # inline power_off would.
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        requester.send_halt.assert_called_once()
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)

    def test_already_off_host_with_removal_evidence_still_powers_off_once(
            self, graceful_shutdown, chassis):
        """The leg makes no already-off decision of its own: the dispatch guard
        completed the requests whose power was known to be off before they reached
        the handler, so a request that did reach it gets the HALT and the platform call."""
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        graceful_shutdown.controller._update_host_state(
            bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        requester.send_halt.assert_called_once()
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)

    def test_booting_host_is_powered_off_and_not_recorded_as_off(
            self, graceful_shutdown, chassis):
        # An earlier power_on was issued but never confirmed: the host reads OFFLINE
        # while it boots. The leg must remove its power and must not write
        # POWERED_OFF on its own, which would let the guard skip a later power_off.
        operation, requester, factory = self._setup(graceful_shutdown, chassis, timeout=0)
        graceful_shutdown.controller._update_host_state(
            bmcctld.SWITCH_HOST_POWERING_ON, bmcctld.SWITCH_HOST_OFFLINE)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            True,
        )
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
        assert _host_state(graceful_shutdown.controller)[
            bmcctld.FIELD_DEVICE_POWER_STATE] != bmcctld.POWER_STATE_OFF

    def test_host_halted_by_this_leg_is_powered_off_despite_a_stale_off_label(
            self, graceful_shutdown, chassis):
        # A stale POWERED_OFF label next to a running host: the leg HALTs the host,
        # which then reads OFFLINE but is still powered. The evidence rule applies
        # only to a host that already read off when the leg started.
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        graceful_shutdown.controller._update_host_state(
            bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_ONLINE)

        def halt_then_report(timeout_secs):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return _report("done [bmc-req:{}]".format(operation.request_id))

        requester.poll_status.side_effect = halt_then_report
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        requester.send_halt.assert_called_once()
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)

    def test_not_qualified_leg_still_powers_off_an_off_host_once(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.open.side_effect = bmcctld.GnoiCredentialError("client_crt: missing")
        graceful_shutdown.controller._update_host_state(
            bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_NOT_QUALIFIED,
            True,
        )
        requester.send_halt.assert_not_called()
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)

    def test_timeout_zero_precedes_credential_loading(
            self, graceful_shutdown, chassis):
        """A forced-only configuration sends nothing and loads nothing; its reason is
        timeout_zero whatever the credentials look like."""
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=0)
        requester.open.side_effect = bmcctld.GnoiCredentialError("client_crt: missing")
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            True,
        )
        factory.assert_not_called()
        graceful_shutdown.controller.power_off.assert_called_once()

    @pytest.mark.parametrize(
        "response, expected_reason",
        [
            (_report("failed [bmc-req:{}]".format(TEST_REQUEST_ID), status=2),
             bmcctld.OP_REASON_CHECK_FAILED),
            (_report("busy [bmc-req:{}]".format(TEST_REQUEST_ID), status=2,
                     message="Previous reboot is ongoing"),
             bmcctld.OP_REASON_BACKEND_ANSWERED),
            (_report("backend [bmc-req:{}]".format(TEST_REQUEST_ID),
                     message="backend answered"),
             bmcctld.OP_REASON_BACKEND_ANSWERED),
        ],
        ids=["host-check-failed", "host-busy", "backend-success-shape"],
    )
    def test_tagged_terminal_reports_force_with_exact_reason(
            self, response, expected_reason, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = response
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, expected_reason, True)
        requester.poll_status.assert_called_once()

    def test_poll_rpc_failure_forces_without_retry(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.side_effect = bmcctld.GnoiRpcError("poll failed")
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_RPC_FAILURE,
            True,
        )
        requester.poll_status.assert_called_once()
        requester.close.assert_called_once()

    @pytest.mark.parametrize(
        "response",
        [
            _report("foreign [bmc-req:{}]".format(OTHER_REQUEST_ID)),
            _report("untagged terminal"),
            _report("active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True),
            _report("two [bmc-req:{0}] [bmc-req:{0}]".format(TEST_REQUEST_ID)),
            _report("done [bmc-req:{}]".format(TEST_REQUEST_ID[:-1])),
            _report("done [bmc-req:{}0]".format(TEST_REQUEST_ID)),
        ],
        ids=["foreign", "untagged", "active", "duplicate-tag", "shorter-id", "longer-id"],
    )
    def test_unattributed_reports_wait_to_deadline(
            self, response, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=1)
        requester.poll_status.return_value = response
        clock = FakeClock()
        operation.cancel.wait = MagicMock(
            side_effect=lambda delay: clock.advance(delay) or False)
        with patch('bmcctld.time.monotonic', side_effect=clock):
            outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_DEADLINE,
            True,
        )
        requester.poll_status.assert_called_once()

    def test_poll_consuming_budget_starts_no_sleep_or_second_poll(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=1)
        clock = FakeClock()
        def poll_status(timeout_secs):
            assert timeout_secs == 1
            clock.now = 1
            return _report(
                "active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)
        requester.poll_status.side_effect = poll_status
        operation.cancel.wait = MagicMock()
        with patch('bmcctld.time.monotonic', side_effect=clock):
            outcome = graceful_shutdown.execute(operation, factory)
        assert outcome[1] == bmcctld.OP_REASON_DEADLINE
        requester.poll_status.assert_called_once()
        operation.cancel.wait.assert_not_called()

    def test_budget_consumed_during_classification_starts_no_sleep(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=1)
        clock = FakeClock()
        requester.poll_status.return_value = _report("foreign")
        operation.cancel.wait = MagicMock()
        def classify(_response, _request_id):
            clock.now = 1
            return "keep_waiting", None
        with patch('bmcctld.time.monotonic', side_effect=clock), \
                patch('bmcctld.classify_report', side_effect=classify):
            outcome = graceful_shutdown.execute(operation, factory)
        assert outcome[1] == bmcctld.OP_REASON_DEADLINE
        operation.cancel.wait.assert_not_called()

    @pytest.mark.parametrize(
        "report_time, expected_result, expected_reason",
        [
            (0.9, bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-"),
            (1.1, bmcctld.OP_RESULT_SUCCESS_FORCED,
             bmcctld.OP_REASON_DEADLINE),
        ],
        ids=["before-deadline", "after-deadline"],
    )
    def test_deadline_precedes_late_terminal_report(
            self, report_time, expected_result, expected_reason,
            graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=1)
        clock = FakeClock()
        def poll_status(timeout_secs):
            assert timeout_secs == 1
            clock.now = report_time
            return _report("done [bmc-req:{}]".format(TEST_REQUEST_ID))
        requester.poll_status.side_effect = poll_status
        with patch('bmcctld.time.monotonic', side_effect=clock):
            outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (expected_result, expected_reason, True)

    def test_cancel_before_open_sends_nothing(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        operation.cancel.set()
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        factory.assert_not_called()
        requester.open.assert_not_called()
        requester.send_halt.assert_not_called()
        graceful_shutdown.controller.power_off.assert_not_called()

    def test_cancel_during_open_sends_nothing(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.open.side_effect = operation.cancel.set
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome[0] == bmcctld.OP_RESULT_PREEMPTED
        requester.send_halt.assert_not_called()
        requester.poll_status.assert_not_called()
        requester.close.assert_called_once()
        graceful_shutdown.controller.power_off.assert_not_called()

    def test_cancel_inside_poll_allows_only_that_rpc(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        def poll_status(timeout_secs):
            operation.cancel.set()
            return _report(
                "active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)
        requester.poll_status.side_effect = poll_status
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome[0] == bmcctld.OP_RESULT_PREEMPTED
        requester.poll_status.assert_called_once()
        graceful_shutdown.controller.power_off.assert_not_called()

    @pytest.mark.parametrize(
        "poll_result",
        ["success", "failure", "error"],
    )
    def test_cancelled_poll_result_is_not_consumed(
            self, poll_result, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)

        def poll_status(timeout_secs):
            operation.cancel.set()
            if poll_result == "error":
                raise bmcctld.GnoiRpcError("poll failed")
            return _report(
                "done [bmc-req:{}]".format(TEST_REQUEST_ID),
                status=1 if poll_result == "success" else 2,
            )

        requester.poll_status.side_effect = poll_status

        outcome = graceful_shutdown.execute(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        requester.poll_status.assert_called_once()
        graceful_shutdown.controller.power_off.assert_not_called()

    def test_cancelled_power_off_preserves_completed_graceful_leg(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))

        def cancel_power_off(_cancel):
            operation.cancel.set()
            return bmcctld.PowerCallResult.CANCELLED

        graceful_shutdown.controller.power_off.side_effect = cancel_power_off

        outcome = graceful_shutdown.execute(operation, factory)

        assert outcome == (bmcctld.OP_RESULT_PREEMPTED, "-", False)
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)

    def test_power_off_failure_keeps_leg_reason(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis,
            power_result=bmcctld.PowerCallResult.NOT_CONFIRMED)
        requester.poll_status.return_value = _report(
            "failed [bmc-req:{}]".format(TEST_REQUEST_ID), status=2)
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_POWER_OFF_FAILED,
            bmcctld.OP_REASON_CHECK_FAILED,
            False,
        )

    def test_poll_error_of_a_cancelled_leg_after_the_deadline_stays_preempted(
            self, graceful_shutdown, chassis):
        operation, requester, factory = TestGracefulShutdownOperation()._setup(
            graceful_shutdown, chassis, timeout=10)
        clock = FakeClock()

        def poll_status(timeout_secs):
            clock.now = 10.0
            operation.cancel.set()
            raise bmcctld.GnoiRpcError("channel closed")

        requester.poll_status.side_effect = poll_status
        with patch("bmcctld.time.monotonic", side_effect=clock):
            outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (bmcctld.OP_RESULT_PREEMPTED, bmcctld.OP_REASON_PREEMPTED, False)
        graceful_shutdown.controller.power_off.assert_not_called()

    # ---- I14: the HALT RPC cannot outlive the handshake deadline

    def test_classifier_exception_forces_the_leg_and_closes_the_requester(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        with patch('bmcctld.classify_report', side_effect=RuntimeError("bug")):
            outcome = graceful_shutdown.execute(operation, factory)
        requester.close.assert_called_once()
        graceful_shutdown.controller.power_off.assert_called_once_with(graceful_shutdown.stop_event)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_UNCLASSIFIED, True)


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - Rack Manager commands
# --------------------------------------------------------------------------

class TestBmcEventHandlerRackMgrCommands:

    def _cmd_fvs(self, command, status=bmcctld.CMD_STATUS_PENDING):
        return {bmcctld.FIELD_COMMAND: command, bmcctld.FIELD_STATUS: status}

    def test_power_off_command_enqueues_power_off(self, event_handler):
        event_handler._handle_rack_mgr_command("CMD_1", self._cmd_fvs(bmcctld.CMD_POWER_OFF))
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert item.on_complete is not None

    def test_power_on_command_no_leak_enqueues_power_on(self, event_handler):
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        event_handler._handle_rack_mgr_command("CMD_2", self._cmd_fvs(bmcctld.CMD_POWER_ON))
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.on_complete is not None

    def test_power_on_command_blocked_by_critical_leak(self, event_handler):
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=True)
        event_handler._set_cmd_status = MagicMock()
        event_handler._handle_rack_mgr_command("CMD_3", self._cmd_fvs(bmcctld.CMD_POWER_ON))
        assert event_handler.action_queue.empty()
        event_handler._set_cmd_status.assert_called_with(
            "CMD_3", bmcctld.CMD_STATUS_FAILED, "POWER_DOWN_POLICY_CONFIGURED_EVENT_ACTIVE")

    def test_power_cycle_command_enqueues_power_cycle(self, event_handler):
        event_handler._handle_rack_mgr_command("CMD_4", self._cmd_fvs(bmcctld.CMD_POWER_CYCLE))
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_CYCLE

    def test_power_cycle_command_is_admitted_while_power_on_is_blocked(self, event_handler):
        """Only POWER_ON is refused at admission; the gate in front of the platform
        call refuses a POWER_CYCLE."""
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=True)
        event_handler._handle_rack_mgr_command(
            "CMD_4B", self._cmd_fvs(bmcctld.CMD_POWER_CYCLE))
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_CYCLE
        event_handler.critical_event_checker.is_power_on_blocked.assert_not_called()

    def test_already_processed_command_is_skipped(self, event_handler):
        event_handler._set_cmd_status = MagicMock()
        event_handler._handle_rack_mgr_command(
            "CMD_5", self._cmd_fvs(bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_DONE))
        assert event_handler.action_queue.empty()
        event_handler._set_cmd_status.assert_not_called()

    def test_in_progress_echo_of_an_admitted_command_is_skipped(self, event_handler):
        """The daemon's own IN_PROGRESS write comes back through the subscription."""
        event_handler._set_cmd_status = MagicMock()
        event_handler._handle_rack_mgr_command(
            "CMD_5", self._cmd_fvs(bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_IN_PROGRESS))
        assert event_handler.action_queue.empty()
        event_handler._set_cmd_status.assert_not_called()

    @pytest.mark.parametrize("command", ["INVALID_CMD", "GRACEFUL_RESTART"])
    def test_unknown_command_is_logged(self, event_handler, command):
        event_handler._set_cmd_status = MagicMock(wraps=event_handler._set_cmd_status)
        event_handler._handle_rack_mgr_command("CMD_6", self._cmd_fvs(command))
        assert event_handler.action_queue.empty()
        assert event_handler._set_cmd_status.call_args_list == [
            call("CMD_6", bmcctld.CMD_STATUS_IN_PROGRESS),
            call("CMD_6", bmcctld.CMD_STATUS_FAILED, "UNKNOWN_COMMAND"),
        ]

    def test_command_callbacks_preserve_their_own_rows(self, event_handler):
        commands = [
            (bmcctld.CMD_POWER_ON, bmcctld.ACTION_POWER_ON),
            (bmcctld.CMD_POWER_OFF, bmcctld.ACTION_POWER_OFF),
            (bmcctld.CMD_POWER_CYCLE, bmcctld.ACTION_POWER_CYCLE),
            (bmcctld.CMD_GRACEFUL_SHUT, bmcctld.ACTION_GRACEFUL_SHUTDOWN),
        ]
        leak_check = MagicMock(return_value=False)
        event_handler.critical_event_checker.is_power_on_blocked = leak_check
        table = _rm_table()
        callbacks = []
        for index, (command, action) in enumerate(commands):
            key = "CMD_{}".format(index)
            fields = self._cmd_fvs(command)
            fields["requester"] = "test-{}".format(index)
            fields["request_tag"] = "request-{}".format(index)
            table.set(key, FieldValuePairs(list(fields.items())))
            event_handler._handle_rack_mgr_command(key, fields)
            item = _dequeue_item(event_handler.action_queue)
            assert item.action == action
            assert item.event_desc == "RACK_MGR_CMD:" + command
            assert dict(table.get(key)[1])[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_IN_PROGRESS
            callbacks.append((item.on_complete, key, fields, index))
        # Only POWER_ON consults the gate at admission.
        assert leak_check.call_count == 1

        for callback, key, fields, index in reversed(callbacks):
            success = index % 2 == 0
            callback(success)
            row = dict(table.get(key)[1])
            assert row[bmcctld.FIELD_STATUS] == (
                bmcctld.CMD_STATUS_DONE if success else bmcctld.CMD_STATUS_FAILED)
            assert row[bmcctld.FIELD_RESULT] == ("SUCCESS" if success else "ERROR")
            for field in (bmcctld.FIELD_COMMAND, "request_tag", "requester"):
                assert row[field] == fields[field]
        # every callback wrote its own row and no other
        assert sorted(table.getKeys()) == ["CMD_0", "CMD_1", "CMD_2", "CMD_3"]


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - Chassis module admin state
# --------------------------------------------------------------------------

class TestBmcEventHandlerChassisModule:

    def test_admin_down_triggers_graceful_shutdown_when_online(self, event_handler, controller):
        # host is ONLINE → admin_down should enqueue graceful_shutdown
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_ONLINE})
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_admin_down_is_admitted_when_already_offline(
            self, event_handler, controller):
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_admin_up_powers_on_when_no_leak(self, event_handler, controller):
        # host is OFFLINE → admin_up should enqueue power_on
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON

    def test_admin_up_is_admitted_when_already_online(self, event_handler, controller):
        """The handler never reads the host state: a redundant admin_up is queued
        and the main thread's guard completes it without a platform call."""
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_ONLINE})
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON

    def test_admin_up_blocked_by_critical_leak(self, event_handler, controller):
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=True)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        assert event_handler.action_queue.empty()

    def test_empty_admin_status_does_not_poison_dedup(self, event_handler, controller):
        """Regression: an empty admin_status must NOT update the dedup map,
        so a subsequent real admin_status (e.g. 'up') is not silently dropped."""
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)

        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: ""},
        )
        assert event_handler.action_queue.empty()
        assert bmcctld.SWITCH_HOST_MODULE_KEY not in event_handler._last_chassis_module_admin_status

        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON

    def test_seed_chassis_module_admin_status_ignores_config_replay(self, event_handler, controller):
        """CONFIG replay after seed must not enqueue ACTION_POWER_ON."""
        event_handler.policy_reader._get_chassis_module_entry = MagicMock(
            return_value={bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})

        event_handler.seed_chassis_module_admin_status()
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        assert event_handler.action_queue.empty()

    def test_seed_chassis_module_admin_status_noop_when_config_missing(self, event_handler):
        event_handler.policy_reader._get_chassis_module_entry = MagicMock(return_value={})
        event_handler.seed_chassis_module_admin_status()
        assert event_handler._last_chassis_module_admin_status == {}


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - System leak events
# --------------------------------------------------------------------------

class TestBmcEventHandlerSystemLeak:

    def _make_policy(self, **kwargs):
        policy = {
            "system_leak_policy": "enabled",
            "system_critical_leak_action": bmcctld.ACTION_POWER_OFF,
            "system_major_leak_action": bmcctld.ACTION_SYSLOG_ONLY,
            "system_minor_leak_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_leak_policy": "enabled",
            "rack_mgr_critical_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_major_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
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
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF

    def test_critical_rack_alert_graceful_is_dispatched_as_critical_power_off(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(
                rack_mgr_critical_alert_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_rack_mgr_alert(
            "Rack_level_leak",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert item.event_desc == "RACK_MGR_CRITICAL_EVENT key=Rack_level_leak"

    def test_critical_system_leak_graceful_shutdown(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_critical_leak_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._event_log = MagicMock()
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
        # A critical trigger reaches the power domain only as a power-off.
        assert item.action == bmcctld.ACTION_POWER_OFF
        event_handler._event_log.log_notice.assert_any_call(
            "Event 'CRITICAL_SYSTEM_LEAK' is critical: configured action "
            "'graceful_shutdown' is dispatched as 'power_off'")

    def test_major_system_leak_graceful_shutdown_is_not_remapped(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_major_leak_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_critical_system_leak_syslog_only(self, event_handler, chassis):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_critical_leak_action=bmcctld.ACTION_SYSLOG_ONLY)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_minor_system_leak_syslog_only_by_default(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy()
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MINOR},
        )
        assert event_handler.action_queue.empty()

    def test_major_system_leak_syslog_only_by_default(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy()
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR},
        )
        assert event_handler.action_queue.empty()

    def test_major_system_leak_graceful_shutdown_when_configured(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_major_leak_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_major_system_leak_power_off_is_urgent_like_any_power_off(self, event_handler):
        """The urgent lane holds power_off requests whatever raised them: a MAJOR
        leak configured to power_off is served before anything in the normal lane."""
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_major_leak_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR},
        )
        assert event_handler.action_queue.pending_through(bmcctld.ActionQueue.LANE_POWER_OFF)
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert item.event_desc == "MAJOR_SYSTEM_LEAK"

    def test_major_system_leak_policy_disabled_skips_action(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_leak_policy="disabled",
                                           system_major_leak_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR},
        )
        assert event_handler.action_queue.empty()

    def test_system_leak_policy_disabled_skips_action(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_leak_policy="disabled")
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_wrong_key_is_ignored(self, event_handler):
        event_handler._handle_system_leak(
            "wrong-key",
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL},
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
            "system_major_leak_action": bmcctld.ACTION_SYSLOG_ONLY,
            "system_minor_leak_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_leak_policy": "enabled",
            "rack_mgr_critical_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
            "rack_mgr_major_alert_action": bmcctld.ACTION_SYSLOG_ONLY,
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
        item = _dequeue_item(event_handler.action_queue)
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

    def test_major_rack_alert_syslog_only_by_default(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy()
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_flow_rate",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR},
        )
        assert event_handler.action_queue.empty()

    def test_major_rack_alert_uses_major_action_when_configured(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_major_alert_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_flow_rate",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_republished_rack_alert_of_the_same_severity_dispatches_once(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_major_alert_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        for _ in range(3):
            event_handler._handle_rack_mgr_alert(
                "Inlet_liquid_flow_rate", {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_GRACEFUL_SHUTDOWN
        assert event_handler.action_queue.empty()

    def test_critical_leak_is_dispatched_with_the_default_action_when_the_policy_is_unreadable(
            self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(side_effect=RuntimeError("redis down"))
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.CRITICAL})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_OFF
        event_handler.log_error.assert_called()

    def test_rack_alert_handled_with_the_defaults_is_dispatched_again_once_the_policy_reads(
            self, event_handler):
        """A rack alert that arrives while the policy is unreadable gets the default
        action (syslog_only). It is not latched: the next publication of the same
        severity gets the configured action, and only that one is latched."""
        configured = self._make_policy(rack_mgr_critical_alert_action=bmcctld.ACTION_POWER_OFF)
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            side_effect=[RuntimeError("redis down"), configured, configured])
        alert = {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL}
        event_handler._handle_rack_mgr_alert("Rack_level_leak", alert)
        assert event_handler.action_queue.empty()
        event_handler._handle_rack_mgr_alert("Rack_level_leak", alert)
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_OFF
        event_handler._handle_rack_mgr_alert("Rack_level_leak", alert)
        assert event_handler.action_queue.empty()

    def test_rack_alert_clear_seen_without_the_policy_does_not_hide_the_next_alert(self, event_handler):
        """CRITICAL, then NORMAL while the policy is unreadable, then CRITICAL again:
        the second CRITICAL is a new alert and is dispatched."""
        configured = self._make_policy(rack_mgr_critical_alert_action=bmcctld.ACTION_POWER_OFF)
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            side_effect=[configured, RuntimeError("redis down"), configured])
        critical = {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL}
        event_handler._handle_rack_mgr_alert("Rack_level_leak", critical)
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_OFF
        event_handler._handle_rack_mgr_alert(
            "Rack_level_leak", {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_NORMAL})
        assert event_handler.action_queue.empty()
        event_handler._handle_rack_mgr_alert("Rack_level_leak", critical)
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_OFF

    @pytest.mark.parametrize("severity, policy_key, trigger", [
        (bmcctld.ALERT_SEVERITY_MAJOR, "rack_mgr_major_alert_action",
         "RACK_MGR_MAJOR_EVENT key=Inlet_liquid_flow_rate"),
        (bmcctld.ALERT_SEVERITY_MINOR, "rack_mgr_minor_alert_action",
         "RACK_MGR_MINOR_EVENT key=Inlet_liquid_flow_rate"),
    ])
    def test_non_critical_rack_alert_graceful_shutdown_is_not_remapped(
            self, event_handler, severity, policy_key, trigger):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=_leak_policy(**{policy_key: bmcctld.ACTION_GRACEFUL_SHUTDOWN}))
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_flow_rate", {bmcctld.FIELD_SEVERITY: severity})
        item = _dequeue_item(event_handler.action_queue)
        assert (item.action, item.event_desc) == (bmcctld.ACTION_GRACEFUL_SHUTDOWN, trigger)

    @pytest.mark.parametrize("action, queued", [
        ("power_cycle", False), ("reboot", False), ("", False), (bmcctld.ACTION_POWER_ON, True)])
    def test_policy_action_outside_the_known_set_is_not_queued(self, event_handler, action, queued):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=_leak_policy(system_major_leak_action=action))
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR})
        assert event_handler.action_queue.empty() is not queued
        if not queued:
            assert "Unknown action" in event_handler.log_warning.call_args.args[0]

    # ---- I03: the abandoned flag alone keeps a woken handshake thread from sending the HALT

    def test_event_loop_stops_the_daemon_after_repeated_failures(self, event_handler, monkeypatch):
        monkeypatch.setattr(bmcctld, "EVENT_LOOP_FAILURE_LIMIT", 3)
        monkeypatch.setattr(bmcctld.time, "sleep", MagicMock())
        event_handler.sel.select = MagicMock(side_effect=RuntimeError("subscription gone"))
        event_handler.run_event_loop()
        assert event_handler.stop_event.is_set() is True
        assert event_handler.sel.select.call_count == 3

    def test_major_rack_alert_power_off_is_urgent_like_any_power_off(self, event_handler):
        """A MAJOR Rack Manager alert configured to power_off also lands in the urgent lane."""
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_major_alert_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_flow_rate",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR},
        )
        assert event_handler.action_queue.pending_through(bmcctld.ActionQueue.LANE_POWER_OFF)
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF

    def test_major_rack_alert_logged_to_event_log(self, event_handler):
        event_handler._event_log = MagicMock()
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_flow_rate",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR},
        )
        msgs = [c[0][0] for c in event_handler._event_log.log_notice.call_args_list]
        assert any("MAJOR RACK MGR ALERT" in m for m in msgs)

    def test_rack_mgr_leak_policy_disabled_skips_action(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_leak_policy="disabled")
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_pressure",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        assert event_handler.action_queue.empty()

    def test_rack_mgr_leak_policy_disabled_does_not_poison_dedup(self, event_handler):
        """Regression: a CRITICAL arriving while policy=disabled must NOT be
        recorded in the dedup map, so the same severity dispatches normally
        once the policy is re-enabled."""
        # Phase 1: policy=disabled, CRITICAL arrives -> no action, no dedup mutation
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_leak_policy="disabled")
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_pressure",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        assert event_handler.action_queue.empty()
        assert "Inlet_liquid_pressure" not in event_handler._last_rack_mgr_alert_severity

        # Phase 2: policy re-enabled, SAME CRITICAL re-arrives -> action MUST dispatch
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_critical_alert_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_pressure",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert event_handler._last_rack_mgr_alert_severity["Inlet_liquid_pressure"] == \
            bmcctld.ALERT_SEVERITY_CRITICAL

    def test_rack_level_leak_uses_leak_field(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(rack_mgr_critical_alert_action=bmcctld.ACTION_POWER_OFF)
        )
        event_handler._handle_rack_mgr_alert(
            "Rack_level_leak",
            {bmcctld.FIELD_LEAK: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF

    def test_normal_severity_no_action(self, event_handler):
        event_handler._handle_rack_mgr_alert(
            "Inlet_liquid_temperature",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_NORMAL},
        )
        assert event_handler.action_queue.empty()


# --------------------------------------------------------------------------
# Tests: BmcEventHandler - Rack Manager telemetry (RACK_MANAGER_DATA)
# --------------------------------------------------------------------------

class TestBmcEventHandlerRackMgrData:

    def test_data_subscriber_registered(self, event_handler):
        """RACK_MANAGER_DATA is subscribed and routed to _handle_rack_mgr_data."""
        fd = event_handler.rack_mgr_data_sub.getFd()
        assert fd in event_handler._subscribers
        assert event_handler._subscribers[fd][1] == event_handler._handle_rack_mgr_data

    def test_normal_telemetry_clears_active_alert(self, event_handler):
        """NORMAL telemetry deletes the matching RACK_MANAGER_ALERT row and resets dedup."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_temperature",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL})
        event_handler._last_rack_mgr_alert_severity["Inlet_liquid_temperature"] = \
            bmcctld.ALERT_SEVERITY_CRITICAL
        event_handler._handle_rack_mgr_data(
            "Inlet_liquid_temperature",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_NORMAL},
        )
        assert "Inlet_liquid_temperature" not in tbl.mock_dict
        assert "Inlet_liquid_temperature" not in event_handler._last_rack_mgr_alert_severity

    def test_normal_telemetry_no_active_alert_is_noop(self, event_handler):
        """NORMAL telemetry with no active alert does nothing (no error, no delete)."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        event_handler._handle_rack_mgr_data(
            "Inlet_liquid_pressure",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_NORMAL},
        )
        assert tbl.mock_dict == {}

    def test_non_normal_telemetry_leaves_alert(self, event_handler):
        """Non-NORMAL telemetry never clears an alert (alerts are raised via ALERT table)."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_flow_rate",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL})
        event_handler._last_rack_mgr_alert_severity["Inlet_liquid_flow_rate"] = \
            bmcctld.ALERT_SEVERITY_CRITICAL
        event_handler._handle_rack_mgr_data(
            "Inlet_liquid_flow_rate",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MINOR},
        )
        assert "Inlet_liquid_flow_rate" in tbl.mock_dict
        assert event_handler._last_rack_mgr_alert_severity["Inlet_liquid_flow_rate"] == \
            bmcctld.ALERT_SEVERITY_CRITICAL

    def test_rack_level_leak_telemetry_clears_via_leak_field(self, event_handler):
        """Rack_level_leak telemetry reports NORMAL via the 'leak' field and clears the alert."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Rack_level_leak",
                         {bmcctld.FIELD_LEAK: bmcctld.ALERT_SEVERITY_CRITICAL})
        event_handler._handle_rack_mgr_data(
            "Rack_level_leak",
            {bmcctld.FIELD_LEAK: bmcctld.ALERT_SEVERITY_NORMAL},
        )
        assert "Rack_level_leak" not in tbl.mock_dict

    def test_mixed_case_normal_clears(self, event_handler):
        """A mixed-case 'Normal' telemetry severity still clears the alert."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_pressure",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL})
        event_handler._handle_rack_mgr_data(
            "Inlet_liquid_pressure",
            {bmcctld.FIELD_SEVERITY: "Normal"},
        )
        assert "Inlet_liquid_pressure" not in tbl.mock_dict

    def test_unknown_telemetry_key_ignored(self, event_handler):
        """Telemetry under a key outside ALERT_KEYS is ignored (no table access, no error)."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_temperature",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL})
        event_handler._handle_rack_mgr_data(
            "Some_unexpected_key",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_NORMAL},
        )
        # Unrelated active alert must be untouched.
        assert "Inlet_liquid_temperature" in tbl.mock_dict

    def test_missing_fields_telemetry_does_not_clear_alert(self, event_handler):
        """Malformed telemetry (no severity and no leak field) must not clear an alert."""
        tbl = Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE)
        _set_table_entry(tbl, "Inlet_liquid_temperature",
                         {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL})
        event_handler._handle_rack_mgr_data("Inlet_liquid_temperature", {})
        assert "Inlet_liquid_temperature" in tbl.mock_dict


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Tests: ActionQueue and _OpCancel
# --------------------------------------------------------------------------

class TestActionQueue:

    def test_lanes_are_served_power_off_then_shutdown_then_the_rest(self):
        q = bmcctld.ActionQueue()
        q.put(_make_item(bmcctld.ACTION_POWER_ON, "first"))
        q.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "second"))
        q.put(_make_item(bmcctld.ACTION_POWER_OFF, "third"))
        assert q.pending_through(bmcctld.ActionQueue.LANE_POWER_OFF) is True
        served = [q.get(0).event_desc for _ in range(3)]
        assert served == ["third", "second", "first"]
        assert q.get(0) is None
        assert q.empty()

    def test_each_lane_keeps_admission_order(self):
        q = bmcctld.ActionQueue()
        for desc, action in [("a", bmcctld.ACTION_POWER_OFF), ("b", bmcctld.ACTION_POWER_ON),
                             ("c", bmcctld.ACTION_POWER_OFF), ("d", bmcctld.ACTION_POWER_CYCLE)]:
            q.put(_make_item(action, desc))
        assert [q.get(0).event_desc for _ in range(4)] == ["a", "c", "b", "d"]

    @pytest.mark.parametrize("action, lane", [
        (bmcctld.ACTION_POWER_OFF, bmcctld.ActionQueue.LANE_POWER_OFF),
        (bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.ActionQueue.LANE_SHUTDOWN),
        (bmcctld.ACTION_POWER_ON, bmcctld.ActionQueue.LANE_NORMAL),
        (bmcctld.ACTION_POWER_CYCLE, bmcctld.ActionQueue.LANE_NORMAL),
    ])
    def test_each_action_has_its_lane(self, action, lane):
        assert bmcctld.ActionQueue.lane_of(action) == lane
        q = bmcctld.ActionQueue()
        q.put(_make_item(action))
        assert q.empty() is False
        assert q.pending_through(bmcctld.ActionQueue.LANE_POWER_OFF) is (lane == bmcctld.ActionQueue.LANE_POWER_OFF)
        assert q.pending_through(bmcctld.ActionQueue.LANE_SHUTDOWN) is (lane <= bmcctld.ActionQueue.LANE_SHUTDOWN)
        assert q.pending_through(bmcctld.ActionQueue.LANE_NORMAL) is True

    def test_wait_pending_through_wakes_on_a_matching_put_only(self):
        q = bmcctld.ActionQueue()
        results = {}

        def waiter(name, lane):
            started = time.monotonic()
            results[name] = (q.wait_pending_through(lane, 1.5), time.monotonic() - started)

        threads = [threading.Thread(target=waiter, args=("off", bmcctld.ActionQueue.LANE_POWER_OFF)),
                   threading.Thread(target=waiter, args=("shut", bmcctld.ActionQueue.LANE_SHUTDOWN))]
        for thread in threads:
            thread.start()
        time.sleep(0.05)
        q.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN))
        for thread in threads:
            thread.join(3)
        assert results["shut"][0] is True and results["shut"][1] < 1
        assert results["off"][0] is False

    def test_wait_pending_through_returns_at_once_when_already_pending(self):
        q = bmcctld.ActionQueue()
        q.put(_make_item(bmcctld.ACTION_POWER_OFF))
        started = time.monotonic()
        assert q.wait_pending_through(bmcctld.ActionQueue.LANE_POWER_OFF, 5) is True
        assert time.monotonic() - started < 1

    def test_get_returns_none_after_the_timeout_when_empty(self):
        q = bmcctld.ActionQueue()
        started = time.monotonic()
        assert q.get(0.05) is None
        assert time.monotonic() - started >= 0.04

    def test_put_wakes_a_waiter_at_once(self):
        q = bmcctld.ActionQueue()
        waited = []

        def waiter():
            started = time.monotonic()
            q.wait_pending_through(bmcctld.ActionQueue.LANE_NORMAL, 5)
            waited.append(time.monotonic() - started)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        q.put(_make_item(bmcctld.ACTION_POWER_OFF))
        thread.join(2)
        assert not thread.is_alive()
        assert waited and waited[0] < 1

    def test_drain_returns_the_lanes_in_order_and_empties_the_queue(self):
        q = bmcctld.ActionQueue()
        q.put(_make_item(bmcctld.ACTION_POWER_ON, "n1"))
        q.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "s1"))
        q.put(_make_item(bmcctld.ACTION_POWER_OFF, "u1"))
        assert [item.event_desc for item in q.drain()] == ["u1", "s1", "n1"]
        assert q.empty()
        assert q.pending_through(bmcctld.ActionQueue.LANE_POWER_OFF) is False

class TestOpCancel:

    def test_a_power_off_operation_consults_the_stop_flag_alone(self, chassis):
        daemon = _make_daemon(chassis)
        off = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_OFF), "id-off")
        on = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_ON), "id-on")
        assert off.cancel is daemon.stop_event
        assert isinstance(on.cancel, bmcctld._OpCancel)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF))
        assert off.cancel.is_set() is False
        assert on.cancel.is_set() is True
        daemon.stop_event.set()
        assert off.cancel.is_set() is True

    @pytest.mark.parametrize("action, phase", [
        (bmcctld.ACTION_POWER_OFF, bmcctld.PHASE_REMOVING),
        (bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.PHASE_REMOVING),
        (bmcctld.ACTION_POWER_ON, bmcctld.PHASE_RAISING),
        (bmcctld.ACTION_POWER_CYCLE, bmcctld.PHASE_RAISING),
    ])
    def test_an_operation_starts_in_the_phase_of_its_action(self, action, phase):
        assert _make_operation(action).phase == phase

    def test_a_removing_operation_yields_to_a_queued_power_off_only(self, chassis):
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN), "id").cancel
        assert cancel.is_set() is False
        daemon.action_queue.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON))
        assert cancel.is_set() is False
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF))
        assert cancel.is_set() is True
        assert daemon.action_queue.get(0).action == bmcctld.ACTION_POWER_OFF
        assert cancel.is_set() is False

    def test_a_raising_operation_yields_to_a_queued_removal(self, chassis):
        """A queued graceful_shutdown ends the waits of a raise; a queued raise does not."""
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_ON), "id").cancel
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_CYCLE))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON))
        assert cancel.is_set() is False
        daemon.action_queue.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN))
        assert cancel.is_set() is True
        assert daemon.action_queue.get(0).action == bmcctld.ACTION_GRACEFUL_SHUTDOWN
        assert cancel.is_set() is False
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF))
        assert cancel.is_set() is True

    def test_the_view_fires_on_daemon_stop(self, chassis):
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_ON), "id").cancel
        daemon.stop_event.set()
        assert cancel.is_set() is True
        started = time.monotonic()
        assert cancel.wait(5) is True
        assert time.monotonic() - started < 0.5

    def test_a_stop_during_the_wait_ends_it_within_a_slice(self, chassis):
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_ON), "id").cancel
        timer = threading.Timer(0.2, daemon.stop_event.set)
        timer.start()
        started = time.monotonic()
        try:
            assert cancel.wait(5) is True
            assert time.monotonic() - started < 1.5
        finally:
            timer.join(2)

    def test_stop_flag_wait_returns_on_set_and_on_timeout(self):
        flag = bmcctld._StopFlag()
        started = time.monotonic()
        assert flag.wait(0.3) is False
        assert 0.25 <= time.monotonic() - started < 1.0
        timer = threading.Timer(0.2, flag.set)
        timer.start()
        started = time.monotonic()
        try:
            assert flag.wait(5) is True
            assert time.monotonic() - started < 1.0
        finally:
            timer.join(2)

    @pytest.mark.parametrize("queued", [bmcctld.ACTION_POWER_OFF, bmcctld.ACTION_GRACEFUL_SHUTDOWN])
    def test_wait_returns_true_as_soon_as_a_removal_is_queued(self, chassis, queued):
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_ON), "id").cancel
        result = []
        started = time.monotonic()
        thread = threading.Thread(target=lambda: result.append(cancel.wait(5)))
        thread.start()
        time.sleep(0.05)
        daemon.action_queue.put(_make_item(queued))
        thread.join(2)
        assert result == [True]
        assert time.monotonic() - started < 1

    def test_wait_returns_false_when_nothing_happens(self, chassis):
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_POWER_ON), "id").cancel
        started = time.monotonic()
        assert cancel.wait(0.1) is False
        assert time.monotonic() - started >= 0.09

    def test_wait_of_a_removing_operation_ignores_a_queued_shutdown_or_raise(self, chassis):
        daemon = _make_daemon(chassis)
        cancel = daemon._make_operation(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN), "id").cancel
        threading.Timer(0.05, lambda: daemon.action_queue.put(
            _make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN))).start()
        threading.Timer(0.1, lambda: daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON))).start()
        started = time.monotonic()
        assert cancel.wait(0.4) is False
        assert time.monotonic() - started >= 0.35


# --------------------------------------------------------------------------
# Tests: BmcctldDaemon - dispatch, guard, records, supersession, startup drop
# --------------------------------------------------------------------------

def _rm_table():
    return Table("STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE)


def _rm_row(daemon, key):
    result = _rm_table().get(key)
    return dict(result[1]) if result and result[0] else None


def _set_rm_row(key, command, status, result=None):
    fields = [(bmcctld.FIELD_COMMAND, command), (bmcctld.FIELD_STATUS, status)]
    if result is not None:
        fields.append((bmcctld.FIELD_RESULT, result))
    _rm_table().set(key, FieldValuePairs(fields))


class TestBmcctldDaemonDispatch:

    @pytest.mark.parametrize("action, method", [
        (bmcctld.ACTION_POWER_OFF, "power_off"),
        (bmcctld.ACTION_POWER_ON, "power_on"),
        (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
    ])
    def test_inline_actions_run_on_the_calling_thread(self, chassis, action, method):
        chassis.switch_host.set_oper_status(
            MockModule.MODULE_STATUS_ONLINE if action == bmcctld.ACTION_POWER_OFF
            else MockModule.MODULE_STATUS_OFFLINE)
        daemon = _make_daemon(chassis)
        threads = []

        def record(cancel):
            threads.append(threading.current_thread())
            if action == bmcctld.ACTION_POWER_OFF:
                assert cancel is daemon.stop_event
            else:
                assert isinstance(cancel, bmcctld._OpCancel)
            return bmcctld.PowerCallResult.CONFIRMED

        setattr(daemon.controller, method, MagicMock(side_effect=record))
        callback = MagicMock()
        daemon._dispatch(_make_item(action, "evt", callback=callback))
        assert threads == [threading.current_thread()]
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS
        assert state[bmcctld.FIELD_OP_REASON] == "-"
        assert state[bmcctld.FIELD_OP_TRIGGER] == "evt"

    def test_graceful_shutdown_runs_in_the_handler_on_the_main_thread(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        threads = []

        def record(operation, factory):
            threads.append(threading.current_thread().name)
            assert factory is daemon.gnoi_requester_factory
            return bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True

        daemon.graceful_shutdown.execute = MagicMock(side_effect=record)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "evt", callback=callback))
        assert threads == [threading.current_thread().name]
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_GRACEFUL
        assert not [t for t in threading.enumerate() if t.name == "bmcctld-graceful"]

    @pytest.mark.parametrize("action", ["graceful_restart", "", None])
    def test_unknown_action_is_refused_before_any_record_is_opened(self, chassis, action):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.graceful_shutdown.execute = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(action, "evt", callback=callback))
        callback.assert_called_once_with(False)
        daemon.graceful_shutdown.execute.assert_not_called()
        assert chassis.switch_host.get_oper_status() == MockModule.MODULE_STATUS_ONLINE
        assert _host_state(daemon) == {}
        assert "Unknown action" in daemon.log_warning.call_args.args[0]

    def test_run_operation_does_not_run_an_unknown_action_as_a_shutdown(self, chassis):
        """The dispatch check refuses an unknown action; past it, the operation fails
        without a handshake and without a power call."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.graceful_shutdown.execute = MagicMock()
        callback = MagicMock()
        daemon._run_operation(_make_item("graceful_restart", "evt", callback=callback))
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(False)
        assert chassis.switch_host.get_oper_status() == MockModule.MODULE_STATUS_ONLINE
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_POWER_OFF_FAILED

    @pytest.mark.parametrize("unreadable", ["admin_status", "rack_rows"])
    def test_startup_power_on_is_dropped_when_its_preconditions_cannot_be_read(
            self, chassis, unreadable):
        daemon = _make_daemon(chassis, admin_status=bmcctld.ADMIN_UP)
        daemon._event_log = MagicMock()
        if unreadable == "admin_status":
            daemon.policy_reader.get_switch_host_admin_status = MagicMock(
                side_effect=RuntimeError("redis down"))
        else:
            daemon._rm_removal_recorded = MagicMock(side_effect=RuntimeError("redis down"))
        daemon.controller.power_on = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "STARTUP", startup=True))
        daemon.controller.power_on.assert_not_called()
        assert _host_state(daemon) == {}
        assert daemon._event_log.log_notice.call_args.args[0].startswith(
            "STARTUP power_on dropped: its preconditions could not be read")

    # ---- D19: a failed terminal record does not change what the requester is told

    def test_failed_terminal_record_still_reports_the_outcome(self, chassis):
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        daemon.controller.write_operation_terminal = MagicMock(side_effect=RuntimeError("db down"))
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "evt", callback=callback))
        callback.assert_called_once_with(True)
        assert any(c.args[0].startswith("OP_DONE action=power_on result=SUCCESS")
                   for c in daemon._event_log.log_notice.call_args_list)

    # ---- B13: an unknown reading never skips a power-on either

    @pytest.mark.parametrize("action", [bmcctld.ACTION_POWER_OFF, bmcctld.ACTION_GRACEFUL_SHUTDOWN])
    def test_stale_powered_off_label_never_skips_a_removal_while_the_host_reads_on(
            self, chassis, action):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE)
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.graceful_shutdown.execute = MagicMock(
            return_value=(bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_TIMEOUT_ZERO, True))
        daemon._dispatch(_make_item(action, "leak"))
        assert daemon.controller.power_off.call_count + daemon.graceful_shutdown.execute.call_count == 1

    # ---- A01: a cancel seen at entry reads and writes nothing

    def test_operation_start_reopens_the_record(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.write_operation_terminal(
            TEST_UUID1, "old", bmcctld.OP_RESULT_SUCCESS, bmcctld.OP_REASON_ALREADY_OFF)
        seen = {}

        def power_on(cancel):
            seen.update(_host_state(daemon))
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_on = MagicMock(side_effect=power_on)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "new"))
        assert (seen[bmcctld.FIELD_OP_RESULT], seen[bmcctld.FIELD_OP_REASON]) == ("-", "-")
        assert seen[bmcctld.FIELD_OP_TRIGGER] == "new"
        assert seen[bmcctld.FIELD_OP_REQUEST_ID] != TEST_UUID1

    # ---- B14: the guard-skip detail of the OP_DONE line

    def test_guard_skip_is_logged_with_its_detail(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event"))
        assert daemon._event_log.log_notice.call_args.args[0].endswith(
            "trigger=dup-event detail=guard_skip")

    # ---- A30: no Switch-Host module

    def test_failed_raise_reports_failure_to_the_requester(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.NOT_CONFIRMED)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "evt", callback=callback))
        callback.assert_called_once_with(False)
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_POWER_ON_FAILED

    def test_leak_refusal_is_logged_with_the_block_reason(self, chassis):
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.REFUSED_LEAK)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "evt", callback=callback))
        callback.assert_called_once_with(False)
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_OFF_LEAK_BLOCKED
        assert daemon._event_log.log_notice.call_args.args[0].endswith(
            "detail=" + bmcctld.POWER_ON_BLOCK_REASON)

    def test_records_name_the_request_in_start_and_done(self, chassis):
        import uuid
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(
            bmcctld.ACTION_POWER_OFF, "RACK_MGR_CMD:POWER_OFF"))
        state = _host_state(daemon)
        request_id = state[bmcctld.FIELD_OP_REQUEST_ID]
        assert str(uuid.UUID(request_id)) == request_id
        notices = [c.args[0] for c in daemon._event_log.log_notice.call_args_list]
        assert "OP_START action=power_off request_id={} trigger=RACK_MGR_CMD:POWER_OFF".format(
            request_id) in notices
        assert ("OP_DONE action=power_off result=SUCCESS reason=- request_id={} "
                "trigger=RACK_MGR_CMD:POWER_OFF detail=SUCCESS".format(request_id)) in notices

    def test_failed_start_record_does_not_misattribute_the_terminal_record(self, chassis):
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        daemon.controller.write_operation_start = MagicMock(side_effect=RuntimeError("db down"))
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "evt"))
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS
        assert state[bmcctld.FIELD_OP_TRIGGER] == "evt"
        done = [c.args[0] for c in daemon._event_log.log_notice.call_args_list
                if c.args[0].startswith("OP_DONE")]
        assert len(done) == 1
        assert "request_id={}".format(state[bmcctld.FIELD_OP_REQUEST_ID]) in done[0]
        daemon.log_error.assert_called()

    def test_callback_failure_is_logged_and_does_not_propagate(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        callback = MagicMock(side_effect=RuntimeError("row gone"))
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "evt", callback=callback))
        callback.assert_called_once()
        message = daemon.log_error.call_args.args[0]
        assert message.startswith("CALLBACK_FAILED request_id=")
        assert message == "CALLBACK_FAILED request_id={}".format(_host_state(daemon)[bmcctld.FIELD_OP_REQUEST_ID])

    # -- lane order --

    def test_action_loop_serves_the_power_off_lane_first(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        calls = []

        def power_off(cancel):
            calls.append("off")
            return bmcctld.PowerCallResult.CONFIRMED

        def execute(operation, factory):
            calls.append("graceful")
            daemon.stop_event.set()
            return bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_TIMEOUT_ZERO, True

        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.graceful_shutdown.execute = MagicMock(side_effect=execute)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "first"))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "second"))
        daemon._run_action_loop()
        assert calls == ["off", "graceful"]
        assert daemon.action_queue.empty()

    @pytest.mark.parametrize("raise_action, method", [
        (bmcctld.ACTION_POWER_ON, "power_on"),
        (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
    ])
    def test_raise_admitted_before_a_power_off_that_ran_first_runs_after_it(
            self, chassis, raise_action, method):
        """Requests run in admission order within their lanes: a raise admitted before a
        removal runs once the removal is done."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)

        def power_off(cancel):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.controller.power_cycle = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        callback = MagicMock()
        daemon.action_queue.put(_make_item(raise_action, "raise", callback=callback))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "removal"))

        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the power_off lane is served first
        daemon._dispatch(_dequeue_item(daemon.action_queue))

        daemon.controller.power_off.assert_called_once()
        getattr(daemon.controller, method).assert_called_once()
        assert callback.call_args[0][0] is True
        assert _host_state(daemon)[bmcctld.FIELD_OP_TRIGGER] == "raise"
        assert daemon.action_queue.empty()

    def test_raise_queued_behind_a_power_off_runs_after_it(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)

        def power_off(cancel):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "removal"))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "raise", callback=callback))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon.controller.power_off.assert_called_once()
        daemon.controller.power_on.assert_called_once()
        callback.assert_called_once_with(True)

    def test_graceful_shutdown_is_served_before_the_raises(self, chassis):
        """The shutdown lane is served before the normal lane; the raises then run in their
        own order."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)

        def execute(operation, factory):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_TIMEOUT_ZERO, True

        daemon.graceful_shutdown.execute = MagicMock(side_effect=execute)
        daemon.controller.power_cycle = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        cycle_callback = MagicMock()
        on_callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_CYCLE, "cycle", callback=cycle_callback))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut"))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "during", callback=on_callback))
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # shut: its lane is served first
        daemon.controller.power_cycle.assert_not_called()
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # cycle
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # power_on
        daemon.graceful_shutdown.execute.assert_called_once()
        daemon.controller.power_cycle.assert_called_once()
        daemon.controller.power_on.assert_called_once()
        cycle_callback.assert_called_once_with(True)
        on_callback.assert_called_once_with(True)

    @pytest.mark.parametrize("status, policy", [
        (bmcctld.SystemLeakStatus.MAJOR, {"system_major_leak_action": bmcctld.ACTION_GRACEFUL_SHUTDOWN}),
        (bmcctld.SystemLeakStatus.MAJOR, {"system_major_leak_action": bmcctld.ACTION_POWER_OFF}),
        (bmcctld.SystemLeakStatus.MINOR, {"system_minor_leak_action": bmcctld.ACTION_GRACEFUL_SHUTDOWN}),
        (bmcctld.SystemLeakStatus.MINOR, {"system_minor_leak_action": bmcctld.ACTION_POWER_OFF}),
    ])
    def test_raise_queued_before_a_leak_with_a_power_down_policy_is_refused_by_the_gate(
            self, chassis, status, policy):
        """A raise admitted before a MAJOR or MINOR leak whose configured action removes power
        runs after the leak's removal, reaches the gate while the leak is still active and is
        refused there: the host stays off without any ordering rule."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.policy_reader.get_leak_control_policy = MagicMock(return_value=_leak_policy(**policy))

        def execute(operation, factory):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            daemon.controller._update_host_state(bmcctld.POWER_STATE_OFF)
            return bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_TIMEOUT_ZERO, True

        daemon.graceful_shutdown.execute = MagicMock(side_effect=execute)
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=lambda up: chassis.switch_host.set_oper_status(
                MockModule.MODULE_STATUS_ONLINE if up else MockModule.MODULE_STATUS_OFFLINE))
        callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "raise", callback=callback))
        # the leak arrives after the raise was admitted
        Table("STATE_DB", bmcctld.SYSTEM_LEAK_STATUS_TABLE).set(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            FieldValuePairs([(bmcctld.FIELD_DEVICE_LEAK_STATUS, status)]))
        daemon.event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY, {bmcctld.FIELD_DEVICE_LEAK_STATUS: status})

        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the leak's removal, served first
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the raise

        chassis.switch_host.set_admin_state.assert_called_once_with(False) \
            if "power_off" in policy.values() else daemon.graceful_shutdown.execute.assert_called_once()
        assert all(c.args != (True,) for c in chassis.switch_host.set_admin_state.call_args_list)
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_OFF_LEAK_BLOCKED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert daemon.action_queue.empty()

    def test_raise_queued_before_a_rack_alert_with_a_power_down_policy_is_refused_by_the_gate(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.policy_reader.get_leak_control_policy = MagicMock(
            return_value=_leak_policy(rack_mgr_major_alert_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN))

        def execute(operation, factory):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_TIMEOUT_ZERO, True

        daemon.graceful_shutdown.execute = MagicMock(side_effect=execute)
        chassis.switch_host.set_admin_state = MagicMock()
        callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "raise", callback=callback))
        alert = {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_MAJOR}
        _set_table_entry(Table("STATE_DB", bmcctld.RACK_MANAGER_ALERT_TABLE), "Inlet_liquid_flow_rate", alert)
        daemon.event_handler._handle_rack_mgr_alert("Inlet_liquid_flow_rate", alert)

        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the alert's graceful shutdown
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the raise

        daemon.graceful_shutdown.execute.assert_called_once()
        chassis.switch_host.set_admin_state.assert_not_called()
        callback.assert_called_once_with(False)
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_OFF_LEAK_BLOCKED

    def test_rack_power_cycle_under_a_blocking_event_is_refused_before_the_platform_call(self, chassis):
        """POWER_CYCLE is not refused at admission: the gate in front of the platform call
        refuses it, the host is not cycled and the row is closed FAILED/ERROR."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.policy_reader.get_leak_control_policy = MagicMock(
            return_value=_leak_policy(system_major_leak_action=bmcctld.ACTION_POWER_OFF))
        Table("STATE_DB", bmcctld.SYSTEM_LEAK_STATUS_TABLE).set(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            FieldValuePairs([(bmcctld.FIELD_DEVICE_LEAK_STATUS, bmcctld.SystemLeakStatus.MAJOR)]))
        chassis.switch_host.do_power_cycle = MagicMock()
        daemon.event_handler._handle_rack_mgr_command("CMD_CYCLE", {
            bmcctld.FIELD_COMMAND: bmcctld.CMD_POWER_CYCLE,
            bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING})
        assert _rm_row(daemon, "CMD_CYCLE")[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_IN_PROGRESS

        daemon._dispatch(_dequeue_item(daemon.action_queue))

        chassis.switch_host.do_power_cycle.assert_not_called()
        row = _rm_row(daemon, "CMD_CYCLE")
        assert (row[bmcctld.FIELD_STATUS], row[bmcctld.FIELD_RESULT]) == (
            bmcctld.CMD_STATUS_FAILED, "ERROR")
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_OFF_LEAK_BLOCKED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON

    def test_raise_queued_before_a_leak_runs_once_the_leak_has_cleared(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.policy_reader.get_leak_control_policy = MagicMock(
            return_value=_leak_policy(system_major_leak_action=bmcctld.ACTION_POWER_OFF))
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=lambda up: chassis.switch_host.set_oper_status(
                MockModule.MODULE_STATUS_ONLINE if up else MockModule.MODULE_STATUS_OFFLINE))
        callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "raise", callback=callback))
        leaks = Table("STATE_DB", bmcctld.SYSTEM_LEAK_STATUS_TABLE)
        leaks.set(bmcctld.SYSTEM_LEAK_STATUS_KEY,
                  FieldValuePairs([(bmcctld.FIELD_DEVICE_LEAK_STATUS, bmcctld.SystemLeakStatus.MAJOR)]))
        daemon.event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SystemLeakStatus.MAJOR})
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the leak's power_off
        leaks.set(bmcctld.SYSTEM_LEAK_STATUS_KEY,
                  FieldValuePairs([(bmcctld.FIELD_DEVICE_LEAK_STATUS, bmcctld.SystemLeakStatus.NONE)]))
        daemon._dispatch(_dequeue_item(daemon.action_queue))   # the raise, with the leak gone
        assert [c.args for c in chassis.switch_host.set_admin_state.call_args_list] == [(False,), (True,)]
        callback.assert_called_once_with(True)

    def test_raise_admitted_after_a_removal_completes_runs(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)

        def power_off(cancel):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "removal"))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "fresh"))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon.controller.power_off.assert_called_once()
        daemon.controller.power_on.assert_called_once()

    def test_raise_admitted_during_a_power_off_verification_runs_after_it(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        callback = MagicMock()

        def power_off(cancel):
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "during", callback=callback))
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "removal"))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon.controller.power_on.assert_called_once()
        callback.assert_called_once_with(True)

    # -- the live guard --

    def test_power_off_is_skipped_when_the_host_reads_off_and_is_recorded_off(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.controller.power_off = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_OFF, "dup-event", callback=callback))
        daemon.controller.power_off.assert_not_called()
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_ALREADY_OFF
        assert state[bmcctld.FIELD_OP_TRIGGER] == "dup-event"

    def test_graceful_shutdown_skip_records_already_off(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.graceful_shutdown.execute = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "dup-event", callback=callback))
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_ALREADY_OFF

    def test_power_on_is_skipped_when_the_host_reads_on(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.power_on = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event", callback=callback))
        daemon.controller.power_on.assert_not_called()
        callback.assert_called_once_with(True)

    def test_guard_skip_uses_the_guard_reading(self, chassis):
        """The record settles under the one reading the guard decided on. A status
        that changes right after the guard cannot turn a skip into a SUCCESS
        recorded under a contradicting status, and the guard reads only once."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.power_on = MagicMock()
        real_read = daemon.controller.get_oper_status

        def read_then_flip():
            status = real_read()
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return status

        daemon.controller.get_oper_status = MagicMock(side_effect=read_then_flip)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event"))
        daemon.controller.power_on.assert_not_called()
        assert daemon.controller.get_oper_status.call_count == 1
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_guard_skip_logs_op_start(self, chassis):
        """A guard skip is an operation like any other: OP_START, then OP_DONE."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event"))
        request_id = _host_state(daemon)[bmcctld.FIELD_OP_REQUEST_ID]
        notices = [c.args[0] for c in daemon._event_log.log_notice.call_args_list]
        assert "OP_START action=power_on request_id={} trigger=dup-event".format(
            request_id) in notices
        assert any(n.startswith("OP_DONE action=power_on result=SUCCESS reason=- request_id={} "
                                .format(request_id)) for n in notices)

    def test_power_on_guard_skip_keeps_settled_power_cycle_label(self, chassis):
        """sonic-mgmt's Redfish tests expect POWER_CYCLE to survive ResetType=On."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.POWER_STATE_CYCLE, bmcctld.SWITCH_HOST_ONLINE)
        daemon.controller.power_on = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event"))
        daemon.controller.power_on.assert_not_called()
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_CYCLE
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_on_guard_skip_repairs_a_stale_status_under_a_settled_label(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.POWER_STATE_CYCLE, bmcctld.SWITCH_HOST_OFFLINE)
        daemon.controller.power_on = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event"))
        daemon.controller.power_on.assert_not_called()
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_CYCLE
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE
        module = dict(daemon.controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)[1])
        assert module[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_on_guard_skip_settles_a_transitional_label(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_ON, bmcctld.SWITCH_HOST_ONLINE)
        daemon.controller.power_on = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "dup-event"))
        daemon.controller.power_on.assert_not_called()
        assert _host_state(daemon)[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON

    def test_power_cycle_is_never_guarded(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.power_cycle = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_CYCLE, "test"))
        daemon.controller.power_cycle.assert_called_once()

    @pytest.mark.parametrize("label", [
        bmcctld.SWITCH_HOST_POWERING_OFF, bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
        bmcctld.SWITCH_HOST_POWERING_ON, bmcctld.POWER_STATE_ON,
    ])
    @pytest.mark.parametrize("action", [bmcctld.ACTION_POWER_OFF, bmcctld.ACTION_GRACEFUL_SHUTDOWN])
    def test_a_recorded_transition_never_skips_a_removal_while_the_host_reads_on(
            self, chassis, label, action):
        """Only live evidence counts: a stale in-progress label is not a reason to
        leave a powered host on."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(label)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.graceful_shutdown.execute = MagicMock(
            return_value=(bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_TIMEOUT_ZERO, True))
        daemon._dispatch(_make_item(action, "dup"))
        assert daemon.controller.power_off.call_count + daemon.graceful_shutdown.execute.call_count == 1

    def test_a_recorded_powering_on_does_not_skip_a_power_on_while_the_host_reads_off(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_ON)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "retry"))
        daemon.controller.power_on.assert_called_once()

    @pytest.mark.parametrize("oper_status", [
        MockModule.MODULE_STATUS_ONLINE, MockModule.MODULE_STATUS_OFFLINE,
    ])
    def test_failed_power_off_residue_is_not_guarded(self, oper_status, chassis):
        chassis.switch_host.set_oper_status(oper_status)
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_OFF)
        daemon.controller.write_operation_terminal(
            TEST_REQUEST_ID, "failed", bmcctld.OP_RESULT_POWER_OFF_FAILED, "-")
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_OFF, "retry"))
        daemon.controller.power_off.assert_called_once()

    def test_unknown_oper_status_never_skips(self, chassis):
        """A reading that is neither ONLINE nor OFFLINE is unknown; unknown never
        proves a request already satisfied, so the request runs."""
        chassis.switch_host.set_oper_status("Fault")
        daemon = _make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE)
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))
        daemon.controller.power_off.assert_called_once()

    # -- the automatic startup power-on --

    def test_startup_power_on_runs_when_nothing_removed_power(self, chassis):
        daemon = _make_daemon(chassis, admin_status=bmcctld.ADMIN_UP)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "STARTUP", startup=True))
        daemon.controller.power_on.assert_called_once()
        assert _host_state(daemon)[bmcctld.FIELD_OP_TRIGGER] == "STARTUP"

    def test_startup_power_on_is_dropped_when_admin_status_is_no_longer_up(self, chassis):
        daemon = _make_daemon(chassis, admin_status=bmcctld.ADMIN_DOWN)
        daemon._event_log = MagicMock()
        daemon.controller.power_on = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "STARTUP", startup=True))
        daemon.controller.power_on.assert_not_called()
        daemon._event_log.log_notice.assert_any_call("STARTUP power_on dropped: admin_status='down'")

    @pytest.mark.parametrize("command", [bmcctld.CMD_POWER_OFF, bmcctld.CMD_GRACEFUL_SHUT])
    def test_startup_power_on_is_dropped_when_a_rack_manager_removal_is_recorded(self, chassis, command):
        daemon = _make_daemon(chassis, admin_status=bmcctld.ADMIN_UP)
        _set_rm_row("CMD_OLD", command, bmcctld.CMD_STATUS_DONE, "SUCCESS")
        daemon.controller.power_on = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "STARTUP", startup=True))
        daemon.controller.power_on.assert_not_called()

    def test_a_dropped_startup_power_on_leaves_no_operation_record(self, chassis):
        daemon = _make_daemon(chassis, admin_status=bmcctld.ADMIN_UP)
        _set_rm_row("CMD_OLD", bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_DONE, "SUCCESS")
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "STARTUP", startup=True))
        row = daemon.controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        assert not (row and row[0] and bmcctld.FIELD_OP_REQUEST_ID in dict(row[1]))

    def test_an_operator_power_on_is_not_dropped_by_a_recorded_removal(self, chassis):
        daemon = _make_daemon(chassis, admin_status=bmcctld.ADMIN_UP)
        _set_rm_row("CMD_OLD", bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_DONE, "SUCCESS")
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "CHASSIS_MODULE:ADMIN_UP"))
        daemon.controller.power_on.assert_called_once()
# --------------------------------------------------------------------------
# Tests: the graceful thread and queue-derived cancellation
# --------------------------------------------------------------------------

class BlockingRequester:
    """A gNOI channel whose status RPC blocks until the channel is closed.

    on_first_poll runs on the graceful thread before the first block, standing
    in for the event thread (or the signal handler) acting while the handshake
    is in flight.
    """

    def __init__(self, on_first_poll=None):
        self.closed = threading.Event()
        self.opened_on = None
        self.halts = []
        self.polls = 0
        self.on_first_poll = on_first_poll

    def open(self):
        self.opened_on = threading.current_thread().name

    def send_halt(self, message, timeout_secs):
        self.halts.append(message)

    def poll_status(self, timeout_secs):
        self.polls += 1
        if self.polls == 1 and self.on_first_poll is not None:
            self.on_first_poll()
        if self.closed.wait(timeout_secs):
            raise bmcctld.GnoiRpcError("channel closed")
        return _report("active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)

    def close(self):
        self.closed.set()


class PollingRequester(BlockingRequester):
    """Answers 'active' until the done_after-th poll, then the tagged completion."""

    def __init__(self, on_first_poll=None, done_after=2):
        super().__init__(on_first_poll)
        self.done_after = done_after
        self.request_id = None

    def send_halt(self, message, timeout_secs):
        super().send_halt(message, timeout_secs)
        self.request_id = message.split("[bmc-req:")[1].rstrip("]")

    def poll_status(self, timeout_secs):
        self.polls += 1
        if self.polls == 1 and self.on_first_poll is not None:
            self.on_first_poll()
        if self.closed.is_set():
            raise bmcctld.GnoiRpcError("channel closed")
        if self.polls >= self.done_after:
            return _report("done [bmc-req:{}]".format(self.request_id))
        return _report("active [bmc-req:{}]".format(self.request_id), active=True)


def _graceful_daemon(chassis, timeout=60):
    daemon = _make_daemon(chassis)
    chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
    daemon.controller.init_host_state()
    daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=timeout)
    daemon._event_log = MagicMock()
    daemon.graceful_shutdown._event_log = daemon._event_log
    return daemon


def _notice_waiter(daemon, prefix):
    """An event set when the daemon logs a notice starting with prefix."""
    seen = threading.Event()

    def on_notice(message):
        if message.startswith(prefix):
            seen.set()

    daemon._event_log.log_notice.side_effect = on_notice
    return seen


def _graceful_threads():
    return [t for t in threading.enumerate() if t.name == "bmcctld-graceful"]


class TestGracefulThread:

    def test_power_off_queued_during_the_handshake_preempts_it_and_then_runs(self, chassis):
        daemon = _graceful_daemon(chassis)
        off_callback = MagicMock()

        def queue_power_off():
            daemon.action_queue.put(_make_item(
                bmcctld.ACTION_POWER_OFF, "CRITICAL_SYSTEM_LEAK", callback=off_callback))

        requester = BlockingRequester(on_first_poll=queue_power_off)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()

        daemon._dispatch(_make_item(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "CHASSIS_MODULE:ADMIN_DOWN",
            callback=callback))

        assert requester.opened_on == "bmcctld-graceful"
        assert requester.closed.is_set()
        assert len(requester.halts) == 1
        set_admin_state.assert_not_called()
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_PREEMPTED
        # The interrupted operation's label stands until the power-off replaces it.
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN
        daemon._event_log.log_notice.assert_any_call(
            "Graceful handshake cancelled request_id={} cause=power_off".format(
                state[bmcctld.FIELD_OP_REQUEST_ID]))
        assert not _graceful_threads()
        assert daemon.action_queue.pending_through(bmcctld.ActionQueue.LANE_POWER_OFF)

        daemon._dispatch(_dequeue_item(daemon.action_queue))

        set_admin_state.assert_called_once_with(False)
        off_callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE
        assert state[bmcctld.FIELD_OP_TRIGGER] == "CRITICAL_SYSTEM_LEAK"

    def test_requests_other_than_power_off_do_not_disturb_a_running_graceful(self, chassis):
        daemon = _graceful_daemon(chassis)

        def queue_later_requests():
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "later"))
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_CYCLE, "later2"))

        requester = PollingRequester(on_first_poll=queue_later_requests, done_after=2)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        callback.assert_called_once_with(True)
        assert requester.polls == 2
        notices = [c.args[0] for c in daemon._event_log.log_notice.call_args_list
                   if c.args[0].startswith("Graceful handshake")]
        assert notices == []
        assert _host_state(daemon)[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert [item.event_desc for item in daemon.action_queue.drain()] == ["later", "later2"]

    def test_stop_during_the_handshake_abandons_it_and_closes_the_channel(self, chassis):
        daemon = _graceful_daemon(chassis)

        def stop_daemon():
            daemon.stop_event.set()
            daemon.action_queue.notify_all()

        requester = BlockingRequester(on_first_poll=stop_daemon)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()

        daemon._dispatch(_make_item(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))

        assert requester.closed.is_set()
        set_admin_state.assert_not_called()
        callback.assert_not_called()
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
        assert state[bmcctld.FIELD_OP_REASON] == "-"
        daemon._event_log.log_notice.assert_any_call(
            "Graceful handshake cancelled request_id={} cause=stop".format(state[bmcctld.FIELD_OP_REQUEST_ID]))
        assert not _graceful_threads()

    def test_a_power_off_arriving_inside_the_platform_call_does_not_undo_a_confirmed_removal(
            self, chassis):
        daemon = _graceful_daemon(chassis, timeout=0)
        off_callback = MagicMock()
        real_set_admin_state = chassis.switch_host.set_admin_state

        def set_admin_state(up):
            daemon.action_queue.put(_make_item(
                bmcctld.ACTION_POWER_OFF, "CRITICAL_SYSTEM_LEAK", callback=off_callback))
            real_set_admin_state(up)

        chassis.switch_host.set_admin_state = MagicMock(side_effect=set_admin_state)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))

        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_TIMEOUT_ZERO
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

        daemon._dispatch(_dequeue_item(daemon.action_queue))
        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        off_callback.assert_called_once_with(True)

    def test_graceful_dequeued_after_a_power_off_was_queued_starts_no_thread(self, chassis):
        daemon = _graceful_daemon(chassis)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))
        daemon.gnoi_requester_factory = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        daemon.gnoi_requester_factory.assert_not_called()
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_PREEMPTED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert not _graceful_threads()

    def test_graceful_dequeued_after_daemon_stop_is_abandoned_without_a_thread(self, chassis):
        daemon = _graceful_daemon(chassis)
        daemon.stop_event.set()
        daemon.gnoi_requester_factory = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        daemon.gnoi_requester_factory.assert_not_called()
        assert not _graceful_threads()
        callback.assert_not_called()
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED

    def test_graceful_leg_exception_is_forced_and_power_is_removed_on_the_main_thread(self, chassis):
        daemon = _graceful_daemon(chassis)
        daemon.graceful_shutdown._halt_and_poll = MagicMock(side_effect=RuntimeError("boom"))
        threads = []
        real_power_off = daemon.controller.power_off

        def record(cancel):
            threads.append(threading.current_thread().name)
            assert cancel is daemon.stop_event
            return real_power_off(cancel)

        daemon.controller.power_off = record
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        assert threads == [threading.current_thread().name]
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_graceful_body_exception_outside_the_leg_makes_no_power_call(self, chassis):
        daemon = _graceful_daemon(chassis)
        daemon.graceful_shutdown.execute = MagicMock(side_effect=RuntimeError("boom"))
        daemon.controller.power_off = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        daemon.controller.power_off.assert_not_called()
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_POWER_OFF_FAILED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED

    def test_graceful_body_exception_after_stop_issues_no_power_off(self, chassis):
        daemon = _graceful_daemon(chassis)

        def fail_after_stop(operation, factory):
            daemon.stop_event.set()
            raise RuntimeError("boom")

        daemon.graceful_shutdown.execute = fail_after_stop
        daemon.controller.power_off = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        daemon.controller.power_off.assert_not_called()
        callback.assert_not_called()

    def test_preemption_notice_is_logged_once_and_the_channel_closed_once(self, chassis):
        daemon = _graceful_daemon(chassis)
        closes = []

        class CountingRequester(BlockingRequester):
            def close(self):
                closes.append(threading.current_thread().name)
                super().close()

        def queue_power_off():
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))

        requester = CountingRequester(on_first_poll=queue_power_off)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut"))
        notices = [c.args[0] for c in daemon._event_log.log_notice.call_args_list
                   if c.args[0].startswith("Graceful handshake cancelled")]
        assert len(notices) == 1
        # Closed by the main thread on preemption and by the graceful thread's cleanup.
        assert sorted(closes) == sorted([threading.current_thread().name, "bmcctld-graceful"])


    def test_handshake_runs_on_the_graceful_thread_and_power_calls_on_main(self, chassis):
        daemon = _graceful_daemon(chassis)
        requester = PollingRequester(done_after=1)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        threads = []
        real_set_admin_state = chassis.switch_host.set_admin_state

        def set_admin_state(up):
            threads.append(threading.current_thread().name)
            real_set_admin_state(up)

        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        assert requester.opened_on == "bmcctld-graceful"
        assert threads == [threading.current_thread().name]
        callback.assert_called_once_with(True)
        assert _host_state(daemon)[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert not _graceful_threads()

    def test_pre_handshake_failure_still_removes_power(self, chassis):
        """A failure before the handshake, a Redis read here, is a forced leg like any
        other failure of the leg: power is removed."""
        daemon = _graceful_daemon(chassis)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(side_effect=RuntimeError("redis"))
        daemon.gnoi_requester_factory = MagicMock()
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        daemon.gnoi_requester_factory.assert_not_called()
        set_admin_state.assert_called_once_with(False)
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_thread_start_failure_still_removes_power(self, chassis):
        daemon = _graceful_daemon(chassis)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        thread = MagicMock()
        thread.start.side_effect = RuntimeError("can't start new thread")
        with patch("bmcctld.threading.Thread", return_value=thread):
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        set_admin_state.assert_called_once_with(False)
        callback.assert_called_once_with(True)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_handshake_thread_that_outlives_the_join_bound_does_not_block_power_off(self, chassis):
        """A handshake thread that ignores the closed channel is given up on after
        HANDSHAKE_JOIN_SECS; the operation ends PREEMPTED and the queued power-off runs."""
        daemon = _graceful_daemon(chassis)
        release = threading.Event()
        off_callback = MagicMock()

        def queue_power_off():
            daemon.action_queue.put(_make_item(
                bmcctld.ACTION_POWER_OFF, "CRITICAL_SYSTEM_LEAK", callback=off_callback))

        requester = WedgedRequester(release, on_first_poll=queue_power_off)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        created = _capture_operations(daemon)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        try:
            started = time.monotonic()
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
            assert time.monotonic() - started < 3
            assert created[0].abandoned is True
            callback.assert_called_once_with(False)
            set_admin_state.assert_not_called()
            assert _graceful_threads()
            daemon.graceful_shutdown.log_error.assert_called()

            daemon._dispatch(_dequeue_item(daemon.action_queue))
            set_admin_state.assert_called_once_with(False)
            off_callback.assert_called_once_with(True)
        finally:
            release.set()
            for thread in _graceful_threads():
                thread.join(3)

    def test_graceful_shutdown_queued_during_a_handshake_waits_for_it(self, chassis):
        """Only a power_off ends a handshake: a GRACEFUL_SHUT queued while another one's
        handshake runs lets it complete, its power-off follows, and the queued shutdown
        then finds the host off."""
        daemon = _graceful_daemon(chassis)
        shut_callback = MagicMock()

        def queue_shutdown():
            daemon.action_queue.put(_make_item(
                bmcctld.ACTION_GRACEFUL_SHUTDOWN, "MAJOR_SYSTEM_LEAK", callback=shut_callback))

        requester = PollingRequester(on_first_poll=queue_shutdown, done_after=2)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        assert len(requester.halts) == 1
        assert requester.polls >= 2
        set_admin_state.assert_called_once_with(False)
        callback.assert_called_once_with(True)
        assert _host_state(daemon)[bmcctld.FIELD_OP_REASON] == "-"
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        shut_callback.assert_called_once_with(True)
        assert set_admin_state.call_count == 1

    def test_handshake_that_never_returns_hits_the_hard_limit_and_power_off_follows(self, chassis):
        """Without a cancellation the wait ends at the handshake's hard limit: the leg
        is forced and this operation removes power itself."""
        daemon = _graceful_daemon(chassis, timeout=1)
        release = threading.Event()
        clock = FakeClock(auto_advance=0.01)

        def jump_past_the_hard_limit():
            # deadline (1 s) + the join allowance + 1 s: the old limit added the 30 s RPC
            # timeout on top and would not have ended the wait here
            clock.advance(1 + bmcctld.HANDSHAKE_JOIN_SECS + 1)

        requester = WedgedRequester(release, on_first_poll=jump_past_the_hard_limit)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        try:
            with patch("bmcctld.time.monotonic", side_effect=clock):
                daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
            set_admin_state.assert_called_once_with(False)
            callback.assert_called_once_with(True)
            state = _host_state(daemon)
            assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
            assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED
            assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        finally:
            release.set()
            for thread in _graceful_threads():
                thread.join(3)
        assert len(requester.halts) == 1

    def test_abandoned_handshake_sends_no_halt_when_it_finally_returns(self, chassis):
        """A thread stuck before the channel exists cannot be closed; once abandoned it
        must not send the HALT when it wakes, the host may be someone else's by then."""
        daemon = _graceful_daemon(chassis)
        release = threading.Event()

        def queue_power_off():
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))

        requester = WedgedOpenRequester(release, on_open=queue_power_off)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        callback = MagicMock()
        try:
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
            callback.assert_called_once_with(False)
            assert _graceful_threads()
        finally:
            release.set()
            for thread in _graceful_threads():
                thread.join(3)
        assert requester.halts == []
        assert not _graceful_threads()

    def test_abandoned_handshake_sends_no_halt_once_the_power_off_has_left_the_queue(self, chassis):
        daemon = _graceful_daemon(chassis)
        release = threading.Event()
        requester = WedgedOpenRequester(release, on_open=lambda: daemon.action_queue.put(
            _make_item(bmcctld.ACTION_POWER_OFF, "leak")))
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        try:
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut"))
            daemon._dispatch(_dequeue_item(daemon.action_queue))     # the power_off; nothing is queued now
            assert daemon.action_queue.empty()
            assert _graceful_threads()
        finally:
            release.set()
            for thread in _graceful_threads():
                thread.join(3)
        assert requester.halts == []

    # ---- I06: the deadline rule applies to rpc_failure only

    def test_second_graceful_request_while_a_handshake_lingers_is_forced(self, chassis):
        daemon = _graceful_daemon(chassis)
        release = threading.Event()

        def queue_power_off():
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))

        requester = WedgedRequester(release, on_first_poll=queue_power_off)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        try:
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut"))
            daemon._dispatch(_dequeue_item(daemon.action_queue))     # the power_off
            assert _graceful_threads()
            daemon.gnoi_requester_factory = MagicMock()
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
            callback = MagicMock()
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "again", callback=callback))
            daemon.gnoi_requester_factory.assert_not_called()
            callback.assert_called_once_with(True)
            state = _host_state(daemon)
            assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED
            assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
            assert [c.args[0] for c in set_admin_state.call_args_list] == [False, False]
        finally:
            release.set()
            for thread in _graceful_threads():
                thread.join(3)


class WedgedRequester(BlockingRequester):
    """A channel whose status RPC ignores close(): it returns only when released."""

    def __init__(self, release, on_first_poll=None):
        super().__init__(on_first_poll)
        self.release = release

    def poll_status(self, timeout_secs):
        self.polls += 1
        if self.polls == 1 and self.on_first_poll is not None:
            self.on_first_poll()
        self.release.wait(10)
        raise bmcctld.GnoiRpcError("released")


class WedgedOpenRequester(BlockingRequester):
    """A channel whose open() hangs until released; nothing exists to close meanwhile."""

    def __init__(self, release, on_open=None):
        super().__init__()
        self.release = release
        self.on_open = on_open

    def open(self):
        super().open()
        if self.on_open is not None:
            self.on_open()
        self.release.wait(10)


# --------------------------------------------------------------------------
# Tests: inline operations and queue-derived cancellation
# --------------------------------------------------------------------------

class TestInlinePreemption:

    def test_power_off_queued_during_power_on_verification_cancels_the_raise(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()

        def set_admin_state(up):
            chassis.switch_host._admin_state = up     # the host does not come up
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "CRITICAL_SYSTEM_LEAK"))

        chassis.switch_host.set_admin_state = set_admin_state
        daemon._event_log = MagicMock()
        callback = MagicMock()
        with patch("bmcctld.time.sleep") as sleep:
            daemon._dispatch(_make_item(
                bmcctld.ACTION_POWER_ON, "RACK_MGR_CMD:POWER_ON", callback=callback))
        sleep.assert_not_called()
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED
        # An issued raise keeps its transitional label: power may still come up.
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWERING_ON

        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=lambda up: MockModule.set_admin_state(chassis.switch_host, up))
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_graceful_shutdown_queued_during_power_on_verification_cancels_the_raise(self, chassis):
        """A queued graceful_shutdown outranks a raise the way a power_off does."""
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()

        def set_admin_state(up):
            chassis.switch_host._admin_state = up     # the host does not come up
            daemon.action_queue.put(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "MAJOR_SYSTEM_LEAK"))

        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        with patch("bmcctld.time.sleep") as sleep:
            daemon._dispatch(_make_item(
                bmcctld.ACTION_POWER_ON, "RACK_MGR_CMD:POWER_ON", callback=callback))
        sleep.assert_not_called()
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWERING_ON
        assert daemon.action_queue.get(0).action == bmcctld.ACTION_GRACEFUL_SHUTDOWN

    def test_power_off_queued_during_power_cycle_verification_cancels_it(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()

        def do_power_cycle():
            chassis.switch_host._oper_status = MockModule.MODULE_STATUS_OFFLINE
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))

        chassis.switch_host.do_power_cycle = do_power_cycle
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_CYCLE, "cycle", callback=callback))
        callback.assert_called_once_with(False)
        assert _host_state(daemon)[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWER_CYCLING

    def test_power_off_verification_is_not_cancelled_by_another_queued_power_off(
            self, chassis, monkeypatch):
        monkeypatch.setattr(bmcctld, "POWER_VERIFY_POLL_INTERVAL_SECS", 0.01)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        reads = {"n": 0}

        def set_admin_state(up):
            chassis.switch_host._admin_state = up
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "second"))

        def slow_power_down():
            reads["n"] += 1
            return (MockModule.MODULE_STATUS_OFFLINE if reads["n"] > 3
                    else MockModule.MODULE_STATUS_ONLINE)

        chassis.switch_host.set_admin_state = set_admin_state
        chassis.switch_host.get_oper_status = slow_power_down
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_OFF, "first", callback=callback))
        callback.assert_called_once_with(True)
        assert _host_state(daemon)[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

        second_callback = MagicMock()
        item = _dequeue_item(daemon.action_queue)
        daemon._dispatch(_with_callback(item, second_callback))
        second_callback.assert_called_once_with(True)

    def test_power_call_lines_and_guard_detail_are_the_published_strings(self, chassis):
        """The lab tests parse these; a change here changes them too."""
        assert bmcctld.GUARD_SKIP_DETAIL == "guard_skip"
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up"))
        daemon.controller.log_notice.assert_any_call("Switch-Host power_on issued; verifying oper_status...")

    @pytest.mark.parametrize("verified, result, success", [
        (bmcctld.PowerCallResult.NOT_CONFIRMED, bmcctld.OP_RESULT_POWER_ON_FAILED, False),
        (bmcctld.PowerCallResult.CONFIRMED, bmcctld.OP_RESULT_SUCCESS, True),
    ])
    def test_platform_false_return_still_verifies(self, chassis, verified, result, success):
        """A platform call that returns False may still have taken effect: the verification decides."""
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        chassis.switch_host.set_admin_state = MagicMock(return_value=False)
        daemon.controller._verify_oper_status = MagicMock(return_value=verified)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up", callback=callback))
        daemon.controller._verify_oper_status.assert_called_once()
        callback.assert_called_once_with(success)
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == result

    def test_precommit_exception_with_a_queued_power_off_rolls_the_label_back(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()

        def gate_fails_after_a_power_off_arrives():
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))
            raise RuntimeError("redis read failed")

        daemon.controller.critical_event_checker.is_power_on_blocked = MagicMock(
            side_effect=gate_fails_after_a_power_off_arrives)
        chassis.switch_host.set_admin_state = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up", callback=callback))
        callback.assert_called_once_with(False)
        chassis.switch_host.set_admin_state.assert_not_called()
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        # The queued power_off then finds nothing to do.
        daemon.controller.power_off = MagicMock()
        off_callback = MagicMock()
        item = _dequeue_item(daemon.action_queue)
        daemon._dispatch(_with_callback(item, off_callback))
        daemon.controller.power_off.assert_not_called()
        off_callback.assert_called_once_with(True)

    def test_power_cycle_rollback_without_a_prior_label_leaves_the_transitional_one(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.critical_event_checker.is_power_on_blocked = MagicMock(return_value=True)
        chassis.switch_host.do_power_cycle = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_CYCLE, "cycle"))
        chassis.switch_host.do_power_cycle.assert_not_called()
        state = _host_state(daemon)
        # No prior label to restore: the transitional label stays until the next
        # operation or the startup convergence settles it.
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWER_CYCLING
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_cycle_transitional_write_failure_is_fail_closed(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.controller._update_host_state = MagicMock(side_effect=RuntimeError("write failed"))
        chassis.switch_host.do_power_cycle = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_CYCLE, "cycle", callback=callback))
        chassis.switch_host.do_power_cycle.assert_not_called()
        callback.assert_called_once_with(False)

    def test_signal_handler_takes_no_lock(self, chassis):
        """The handler runs on the main thread, possibly while it holds the queue lock."""
        import signal as _signal
        daemon = _make_daemon(chassis)
        with daemon.action_queue._lock:
            daemon.signal_handler(_signal.SIGTERM, None)
        assert daemon.stop_event.is_set()

    def test_guard_read_failure_runs_the_request(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()
        daemon.controller.get_db_power_state = MagicMock(side_effect=RuntimeError("redis down"))
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_OFF, "leak", callback=callback))
        daemon.controller.power_off.assert_called_once()
        callback.assert_called_once_with(True)

    def test_dispatch_failure_closes_the_request_and_keeps_the_loop_alive(self, chassis):
        daemon = _make_daemon(chassis)
        daemon._guard_should_skip = MagicMock(side_effect=RuntimeError("unexpected"))
        callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "up", callback=callback))
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "stop"))
        calls = {"n": 0}

        def guard(item):
            calls["n"] += 1
            if calls["n"] == 2:
                daemon.stop_event.set()
            raise RuntimeError("unexpected")

        daemon._guard_should_skip = MagicMock(side_effect=guard)
        daemon._run_action_loop()
        assert calls["n"] == 2
        callback.assert_called_once_with(False)
        assert daemon.log_error.call_count >= 2

    def test_cancelled_raise_over_a_booting_host_keeps_its_transitional_label(self, chassis):
        """A live OFFLINE reading never turns POWERING_ON into POWERED_OFF."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = _make_daemon(chassis)
        # An earlier power_on was issued but never confirmed: the host is booting.
        daemon.controller._update_host_state(
            bmcctld.SWITCH_HOST_POWERING_ON, bmcctld.SWITCH_HOST_OFFLINE)

        def power_off_arrives_during_the_gate_read():
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))
            return False

        daemon.controller.critical_event_checker.is_power_on_blocked = MagicMock(
            side_effect=power_off_arrives_during_the_gate_read)
        chassis.switch_host.set_admin_state = MagicMock()
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up", callback=callback))
        callback.assert_called_once_with(False)
        chassis.switch_host.set_admin_state.assert_not_called()
        assert _host_state(daemon)[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_ON
        # The queued power_off is therefore not guard-skipped: the booting host loses power.
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        chassis.switch_host.set_admin_state.assert_called_once_with(False)

    def test_stop_during_inline_verification_records_abandoned(self, chassis):
        daemon = _make_daemon(chassis)
        daemon.controller.init_host_state()

        def set_admin_state(up):
            chassis.switch_host._admin_state = up
            daemon.stop_event.set()

        chassis.switch_host.set_admin_state = set_admin_state
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up", callback=callback))
        callback.assert_not_called()
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
# --------------------------------------------------------------------------
# Tests: exception recovery on the main thread
# --------------------------------------------------------------------------

def _capture_operations(daemon):
    created = []
    real_make = daemon._make_operation

    def make(item, request_id):
        operation = real_make(item, request_id)
        created.append(operation)
        return operation

    daemon._make_operation = make
    return created


class TestOperationExceptionRecovery:

    def _make_daemon(self, chassis):
        daemon = _make_daemon(chassis)
        daemon._event_log = MagicMock()
        return daemon

    @pytest.mark.parametrize("scenario", ["graceful", "rpc_failure", "deadline", "classifier_error"])
    @pytest.mark.parametrize("close_error", [False, True])
    def test_channel_close_preserves_the_outcome(
            self, chassis, scenario, close_error, monkeypatch):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        chassis.switch_host.set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.do_power_cycle = MagicMock()
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=1)
        clock = FakeClock()
        monkeypatch.setattr(bmcctld._OpCancel, "wait",
                            lambda self, timeout: clock.advance(timeout) or self.is_set())
        created = _capture_operations(daemon)
        requester = MagicMock()

        def poll_status(timeout_secs):
            return _report("done [bmc-req:{}]".format(created[0].request_id),
                           active=scenario == "deadline")

        requester.poll_status.side_effect = poll_status
        if scenario == "rpc_failure":
            requester.poll_status.side_effect = bmcctld.GnoiRpcError("poll failed")
        if close_error:
            requester.close.side_effect = bmcctld.GnoiRpcError("close failed")
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        callback = MagicMock()

        with patch('bmcctld.time.monotonic', side_effect=clock), \
                patch('bmcctld.classify_report', wraps=bmcctld.classify_report) as classify:
            if scenario == "classifier_error":
                classify.side_effect = RuntimeError("classification failed")
            daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "test", callback=callback))

        reason = {
            "graceful": "-", "rpc_failure": bmcctld.OP_REASON_RPC_FAILURE,
            "deadline": bmcctld.OP_REASON_DEADLINE,
            "classifier_error": bmcctld.OP_REASON_UNCLASSIFIED,
        }[scenario]
        result = (bmcctld.OP_RESULT_SUCCESS_GRACEFUL if scenario == "graceful" else
                  bmcctld.OP_RESULT_SUCCESS_FORCED)
        state = _host_state(daemon)
        assert (state[bmcctld.FIELD_OP_RESULT], state[bmcctld.FIELD_OP_REASON]) == (result, reason)
        callback.assert_called_once_with(True)
        assert chassis.switch_host.set_admin_state.call_args_list == [call(False)]
        chassis.switch_host.do_power_cycle.assert_not_called()
        daemon.gnoi_requester_factory.assert_called_once()
        requester.open.assert_called_once()
        requester.send_halt.assert_called_once()
        requester.poll_status.assert_called_once()
        requester.close.assert_called_once()
        assert not created[0].cancel.is_set()

    @pytest.mark.parametrize(
        "command, action, expected_outcome, expected_cmd_status, expected_state",
        [
            (bmcctld.CMD_GRACEFUL_SHUT,
             bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             (bmcctld.OP_RESULT_SUCCESS_FORCED,
              bmcctld.OP_REASON_NOT_QUALIFIED, True),
             bmcctld.CMD_STATUS_DONE,
             bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN),
            (bmcctld.CMD_POWER_OFF,
             bmcctld.ACTION_POWER_OFF,
             (bmcctld.OP_RESULT_SUCCESS, "-", True),
             bmcctld.CMD_STATUS_DONE,
             bmcctld.POWER_STATE_ON),
        ],
    )
    def test_downward_bookkeeping_failure_does_not_block_power_removal(
            self, command, action, expected_outcome, expected_cmd_status,
            expected_state, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        _without_credentials(daemon.graceful_shutdown)
        command_key = "bookkeeping-failure"
        daemon.event_handler._handle_rack_mgr_command(command_key, {
            bmcctld.FIELD_COMMAND: command,
            bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING,
        })

        update_host_state = daemon.controller._update_host_state

        def fail_downward_state_writes(power_state, device_status=None):
            if power_state in (
                    bmcctld.SWITCH_HOST_POWERING_OFF,
                    bmcctld.POWER_STATE_OFF):
                raise RuntimeError("power-state write failed")
            return update_host_state(power_state, device_status)

        daemon.controller.get_db_power_state = MagicMock(
            side_effect=RuntimeError("power-state read failed"))
        daemon.controller.get_db_device_status = MagicMock(
            side_effect=RuntimeError("device-status read failed"))
        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_downward_state_writes)
        set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=set_admin_state)
        created = _capture_operations(daemon)

        daemon._dispatch(_dequeue_item(daemon.action_queue))

        operation = created[0]
        assert operation.item.action == action
        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False)]
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_OFFLINE
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == expected_outcome[0]
        assert state[bmcctld.FIELD_OP_REASON] == expected_outcome[1]
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == expected_state
        command_row = _rm_row(daemon, command_key)
        assert command_row[bmcctld.FIELD_STATUS] == expected_cmd_status
        assert command_row[bmcctld.FIELD_RESULT] == "SUCCESS"

    @pytest.mark.parametrize(
        "action, expected_reason",
        [
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             bmcctld.OP_REASON_NOT_QUALIFIED),
            (bmcctld.ACTION_POWER_OFF, "-"),
        ],
    )
    def test_downward_platform_exception_is_not_retried(
            self, action, expected_reason, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        _without_credentials(daemon.graceful_shutdown)
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=RuntimeError("platform call failed"))

        daemon._dispatch(_make_item(action, "test"))

        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = _host_state(daemon)
        assert (state[bmcctld.FIELD_OP_RESULT], state[bmcctld.FIELD_OP_REASON]) == (
            bmcctld.OP_RESULT_POWER_OFF_FAILED, expected_reason)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_OFF

    @pytest.mark.parametrize(
        "failure_point",
        ["prior-state-read", "transitional-write", "critical-state-read"],
    )
    def test_power_on_bookkeeping_failure_remains_fail_closed(
            self, failure_point, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE)
        chassis.switch_host.set_admin_state = MagicMock()

        if failure_point == "prior-state-read":
            daemon.controller.get_db_power_state = MagicMock(
                side_effect=RuntimeError("power-state read failed"))
        elif failure_point == "transitional-write":
            update_host_state = daemon.controller._update_host_state

            def fail_powering_on_write(power_state, device_status=None):
                if power_state == bmcctld.SWITCH_HOST_POWERING_ON:
                    raise RuntimeError("power-state write failed")
                return update_host_state(power_state, device_status)

            daemon.controller._update_host_state = MagicMock(
                side_effect=fail_powering_on_write)
        else:
            daemon.controller.critical_event_checker.is_power_on_blocked = \
                MagicMock(side_effect=RuntimeError(
                    "critical-state read failed"))

        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up"))

        chassis.switch_host.set_admin_state.assert_not_called()
        state = _host_state(daemon)
        assert (state[bmcctld.FIELD_OP_RESULT], state[bmcctld.FIELD_OP_REASON]) == (
            bmcctld.OP_RESULT_POWER_ON_FAILED, "-")
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == \
            bmcctld.SWITCH_HOST_OFFLINE
        if failure_point == "transitional-write":
            assert daemon.controller._update_host_state.call_args_list == [
                call(bmcctld.SWITCH_HOST_POWERING_ON),
                call(bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE),
            ]

    @pytest.mark.parametrize(
        "command, action, expected_reason, expected_state",
        [
            (bmcctld.CMD_GRACEFUL_SHUT,
             bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             bmcctld.OP_REASON_NOT_QUALIFIED,
             bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN),
            (bmcctld.CMD_POWER_OFF,
             bmcctld.ACTION_POWER_OFF, "-", bmcctld.POWER_STATE_ON),
        ],
    )
    def test_bookkeeping_and_platform_failures_issue_one_downward_call(
            self, command, action, expected_reason, expected_state, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        _without_credentials(daemon.graceful_shutdown)
        command_key = "bookkeeping-and-platform-failure"
        daemon.event_handler._handle_rack_mgr_command(command_key, {
            bmcctld.FIELD_COMMAND: command,
            bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING,
        })

        update_host_state = daemon.controller._update_host_state

        def fail_downward_state_writes(power_state, device_status=None):
            if power_state in (
                    bmcctld.SWITCH_HOST_POWERING_OFF,
                    bmcctld.POWER_STATE_OFF):
                raise RuntimeError("power-state write failed")
            return update_host_state(power_state, device_status)

        daemon.controller.get_db_power_state = MagicMock(
            side_effect=RuntimeError("power-state read failed"))
        daemon.controller.get_db_device_status = MagicMock(
            side_effect=RuntimeError("device-status read failed"))
        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_downward_state_writes)
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=RuntimeError("platform call failed"))
        created = _capture_operations(daemon)

        daemon._dispatch(_dequeue_item(daemon.action_queue))

        operation = created[0]
        assert operation.item.action == action
        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED
        assert state[bmcctld.FIELD_OP_REASON] == expected_reason
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == expected_state
        command_row = _rm_row(daemon, command_key)
        assert command_row[bmcctld.FIELD_STATUS] == \
            bmcctld.CMD_STATUS_FAILED
        assert command_row[bmcctld.FIELD_RESULT] == "ERROR"

    @pytest.mark.parametrize(
        "action, initial_status, final_state",
        [
            (bmcctld.ACTION_POWER_OFF, MockModule.MODULE_STATUS_ONLINE, bmcctld.POWER_STATE_OFF),
            (bmcctld.ACTION_POWER_ON, MockModule.MODULE_STATUS_OFFLINE, bmcctld.POWER_STATE_ON),
            (bmcctld.ACTION_POWER_CYCLE, MockModule.MODULE_STATUS_ONLINE, bmcctld.POWER_STATE_CYCLE),
        ],
    )
    def test_confirmed_call_with_a_failing_final_label_write_records_success(
            self, action, initial_status, final_state, chassis):
        """The label write after a confirmed call catches its own failure (I9): the
        physical result stands and the operation is SUCCESS."""
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(initial_status)
        update_state = daemon.controller._update_host_state

        def fail_final_state(power_state, device_status=None):
            if power_state == final_state:
                raise RuntimeError("final state write failed")
            return update_state(power_state, device_status)

        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_final_state)
        callback = MagicMock()
        daemon._dispatch(_make_item(action, "test", callback=callback))

        callback.assert_called_once_with(True)
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_graceful_shutdown_transitional_write_failure_preserves_reason(
            self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        _without_credentials(daemon.graceful_shutdown)

        update_state = daemon.controller._update_host_state
        failed_once = [False]

        def fail_first_powering_off(power_state, device_status=None):
            if power_state == bmcctld.SWITCH_HOST_POWERING_OFF and \
                    not failed_once[0]:
                failed_once[0] = True
                raise RuntimeError("transitional state write failed")
            return update_state(power_state, device_status)

        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_first_powering_off)
        set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=set_admin_state)

        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut"))

        assert failed_once[0] is True
        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = _host_state(daemon)
        assert (state[bmcctld.FIELD_OP_RESULT], state[bmcctld.FIELD_OP_REASON]) == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_NOT_QUALIFIED)

    def test_leg_exception_with_a_failing_final_write_still_leaves_an_outcome(self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=1)
        daemon.graceful_shutdown._halt_and_poll = MagicMock(
            side_effect=RuntimeError("handshake code failed"))
        update_state = daemon.controller._update_host_state

        def fail_final_state(power_state, device_status=None):
            if power_state == bmcctld.POWER_STATE_OFF:
                raise RuntimeError("final state write failed")
            return update_state(power_state, device_status)

        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_final_state)
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut"))

        assert chassis.switch_host.get_admin_state() is False
        state = _host_state(daemon)
        assert (state[bmcctld.FIELD_OP_RESULT], state[bmcctld.FIELD_OP_REASON]) == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_UNCLASSIFIED)

    def test_exception_while_a_power_off_is_queued_is_a_preemption(self, chassis):
        """An operation whose own code raises while a POWER_OFF waits behind it is
        recorded PREEMPTED, not failed; the power-off runs next."""
        daemon = self._make_daemon(chassis)
        daemon.controller.init_host_state()

        def queue_a_power_off_then_raise(cancel):
            daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "leak"))
            raise RuntimeError("boom")

        daemon.controller.power_on = MagicMock(side_effect=queue_a_power_off_then_raise)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_ON, "up", callback=callback))
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_PREEMPTED

    def test_power_off_exception_after_the_handshake_is_power_off_failed(self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller.init_host_state()
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=0)
        daemon.controller.power_off = MagicMock(side_effect=RuntimeError("boom"))
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shut", callback=callback))
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_POWER_OFF_FAILED
        # the leg's reason, as when the power-off returns unconfirmed
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_TIMEOUT_ZERO
        daemon.controller.power_off.assert_called_once()

    @pytest.mark.parametrize("action, method", [
        (bmcctld.ACTION_POWER_ON, "power_on"), (bmcctld.ACTION_POWER_CYCLE, "power_cycle")])
    def test_raise_exception_is_power_on_failed(self, chassis, action, method):
        daemon = self._make_daemon(chassis)
        daemon.controller.init_host_state()
        setattr(daemon.controller, method, MagicMock(side_effect=RuntimeError("boom")))
        callback = MagicMock()
        daemon._dispatch(_make_item(action, "raise", callback=callback))
        callback.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_POWER_ON_FAILED
        assert state[bmcctld.FIELD_OP_REASON] == "-"
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        getattr(daemon.controller, method).assert_called_once()

    def test_exception_after_stop_is_abandoned(self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller.init_host_state()

        def stop_then_raise(cancel):
            daemon.stop_event.set()
            raise RuntimeError("boom")

        daemon.controller.power_off = MagicMock(side_effect=stop_then_raise)
        callback = MagicMock()
        daemon._dispatch(_make_item(bmcctld.ACTION_POWER_OFF, "off", callback=callback))
        callback.assert_not_called()
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED


# --------------------------------------------------------------------------
# Tests: BmcctldDaemon - initial power-on sequence
# --------------------------------------------------------------------------

class TestBmcctldDaemonInitialSequence:

    def _make_daemon(self, chassis):
        return _make_daemon(chassis, admin_status=bmcctld.ADMIN_UP)

    def test_skips_power_on_when_admin_status_down(self, chassis):
        """When admin_status=down (default), Switch-Host must not be powered on at startup."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_switch_host_admin_status = MagicMock(return_value=bmcctld.ADMIN_DOWN)
        daemon.controller.power_on = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_queues_the_startup_power_on_when_the_host_is_off(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon._initial_power_on_sequence()
        item = _dequeue_item(daemon.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.event_desc == "STARTUP"
        assert item.startup is True

    def test_startup_power_on_logs_the_line_sonic_mgmt_parses(self, chassis):
        """sonic-mgmt test_bmcctld_power_on_delay reads this exact event-log line
        (and its journal timestamp) on liquid-cooled systems."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon._event_log = MagicMock()
        daemon._initial_power_on_sequence()
        daemon._event_log.log_notice.assert_any_call(
            "STARTUP: Switch-Host OFFLINE — issuing power_on")

    def test_skips_power_on_if_a_blocking_event_is_active(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=True)
        daemon.controller.power_on = MagicMock()
        daemon._event_log = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()
        daemon._event_log.log_warning.assert_any_call(
            "STARTUP: Switch-Host power_on BLOCKED ({}: active alert blocks power-on)".format(
                bmcctld.POWER_ON_BLOCK_REASON))

    def test_refreshes_state_when_already_online(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock()
        daemon.controller.init_host_state = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()
        daemon.controller.init_host_state.assert_called_once()

    def test_boot_delay_skipped_when_system_uptime_exceeds_delay(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        with patch('time.clock_gettime', return_value=600):
            t0 = time.monotonic()
            daemon._initial_power_on_sequence()
            elapsed = time.monotonic() - t0
        assert elapsed < 1.0, "boot delay should have been skipped (elapsed={:.2f}s)".format(elapsed)
        assert _dequeue_item(daemon.action_queue).action == bmcctld.ACTION_POWER_ON

    def test_stop_event_during_boot_delay_skips_sequence(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock()
        daemon.stop_event.set()
        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_boot_delay_dispatches_queued_requests_inline(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=1)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        threads = []

        def power_off(cancel):
            threads.append(threading.current_thread())
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "RACK_MGR_BOOT_DELAY"))
        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        assert threads == [threading.current_thread()]

    def test_boot_delay_runs_a_slow_removal_to_completion_before_the_startup_tail(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=1)
        daemon.controller.init_host_state = MagicMock()

        def unconfirmed_power_off(up):
            assert up is False
            chassis.switch_host._admin_state = up

        def slow_verify(_expected, _timeout, _context, cancel=None):
            time.sleep(0.3)
            return bmcctld.PowerCallResult.NOT_CONFIRMED

        chassis.switch_host.set_admin_state = MagicMock(side_effect=unconfirmed_power_off)
        daemon.controller._verify_oper_status = MagicMock(side_effect=slow_verify)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "boot-delay"))

        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()

        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.SWITCH_HOST_POWERING_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_POWER_OFF_FAILED
        # The host still reads ONLINE, so the tail only refreshes the record.
        daemon.controller.init_host_state.assert_called_once()
        assert daemon.action_queue.empty()

    def test_startup_power_on_queued_after_a_boot_delay_removal_is_dropped_at_dispatch(self, chassis):
        """A Rack Manager removal served during the boot delay leaves its row behind;
        the startup power-on queued after the delay sees it and is dropped."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=1)
        daemon._event_log = MagicMock()
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.controller.power_on = MagicMock()
        _set_rm_row("CMD_1", bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_IN_PROGRESS)
        daemon.action_queue.put(_make_item(
            bmcctld.ACTION_POWER_OFF, "RACK_MGR_CMD:POWER_OFF"))
        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()
        daemon.controller.power_off.assert_called_once()
        item = _dequeue_item(daemon.action_queue)
        assert item.startup is True
        daemon._dispatch(item)
        daemon.controller.power_on.assert_not_called()
        daemon._event_log.log_notice.assert_any_call(
            "STARTUP power_on dropped: a Rack Manager power removal is recorded")

    def test_startup_power_on_runs_after_a_leak_power_off_in_the_boot_delay_whose_event_cleared(
            self, chassis):
        """A leak-triggered power-off during the boot delay leaves no Rack Manager row.
        Once the event has cleared, the startup power-on brings the host back: the
        startup scan looks only at Rack Manager rows."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=1)
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_OFF, "CRITICAL_SYSTEM_LEAK"))
        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()
        daemon.controller.power_off.assert_called_once()
        daemon._dispatch(_dequeue_item(daemon.action_queue))
        daemon.controller.power_on.assert_called_once()

    @pytest.mark.parametrize("command, status, result, expected", [
        (bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_DONE, "SUCCESS", True),
        (bmcctld.CMD_GRACEFUL_SHUT, bmcctld.CMD_STATUS_DONE, "SUCCESS", True),
        (bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_FAILED, bmcctld.OP_RESULT_ABANDONED, True),
        (bmcctld.CMD_GRACEFUL_SHUT, bmcctld.CMD_STATUS_FAILED, bmcctld.OP_RESULT_ABANDONED, True),
        (bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_FAILED, "ERROR", False),
        (bmcctld.CMD_GRACEFUL_SHUT, bmcctld.CMD_STATUS_FAILED, "ERROR", False),
        (bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_IN_PROGRESS, None, True),
        (bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_PENDING, None, True),
        (bmcctld.CMD_GRACEFUL_SHUT, "", None, True),
        (bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_DONE, "SUCCESS", False),
        (bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_IN_PROGRESS, None, False),
        (bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_PENDING, None, False),
        (bmcctld.CMD_POWER_CYCLE, bmcctld.CMD_STATUS_DONE, "SUCCESS", False),
        (bmcctld.CMD_POWER_CYCLE, bmcctld.CMD_STATUS_IN_PROGRESS, None, False),
        (bmcctld.CMD_POWER_CYCLE, bmcctld.CMD_STATUS_PENDING, None, False),
    ])
    def test_rm_removal_recorded_counts_present_removal_rows(
            self, chassis, command, status, result, expected):
        daemon = self._make_daemon(chassis)
        _set_rm_row("CMD_1", command, status, result)
        assert daemon._rm_removal_recorded() is expected

    def test_sweep_leaves_failed_and_statusless_rows_alone(self, chassis):
        daemon = _make_daemon(chassis)
        _set_rm_row("CMD_FAILED", bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_FAILED, "ERROR")
        _rm_table().set("CMD_NEW", FieldValuePairs([(bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_ON)]))
        daemon._sweep_stale_rm_rows()
        failed = _rm_row(daemon, "CMD_FAILED")
        assert (failed[bmcctld.FIELD_STATUS], failed[bmcctld.FIELD_RESULT]) == (
            bmcctld.CMD_STATUS_FAILED, "ERROR")
        assert _rm_row(daemon, "CMD_NEW") == {bmcctld.FIELD_COMMAND: bmcctld.CMD_POWER_ON}
        assert daemon._rm_removal_recorded() is False

    # ---- D16: an automatic raise whose preconditions cannot be read is not taken

    def test_rm_removal_recorded_is_false_on_an_empty_table(self, chassis):
        daemon = self._make_daemon(chassis)
        assert daemon._rm_removal_recorded() is False

    def test_cold_boot_applies_power_on_delay(self, chassis):
        chassis.set_reboot_cause(bmcctld.ChassisBase.REBOOT_CAUSE_POWER_LOSS)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=5)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon.stop_event.set()
        daemon._initial_power_on_sequence()
        daemon.policy_reader.get_power_on_delay.assert_called_once()

    def test_warm_boot_skips_power_on_delay(self, chassis):
        chassis.set_reboot_cause(chassis.REBOOT_CAUSE_NON_HARDWARE)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon._initial_power_on_sequence()
        daemon.policy_reader.get_power_on_delay.assert_not_called()
        assert _dequeue_item(daemon.action_queue).action == bmcctld.ACTION_POWER_ON

    def test_reboot_cause_exception_falls_back_to_cold_boot(self, chassis):
        chassis.get_reboot_cause = MagicMock(side_effect=RuntimeError("platform API not available"))
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=5)
        daemon.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        daemon.stop_event.set()
        daemon._initial_power_on_sequence()
        daemon.policy_reader.get_power_on_delay.assert_called_once()


# --------------------------------------------------------------------------
# Tests: BmcctldDaemon - run(), startup order and stop order
# --------------------------------------------------------------------------

class TestBmcctldDaemonRun:

    def _make_daemon(self, chassis, admin_status=bmcctld.ADMIN_UP):
        daemon = _make_daemon(chassis, admin_status=admin_status)
        daemon._event_log = MagicMock()
        return daemon

    def _idle_event_loop(self, daemon, before_idle=None):
        """An event-loop stand-in: optional admissions, then it waits for stop."""
        def run_event_loop():
            if before_idle is not None:
                before_idle()
            daemon.stop_event.wait(5)
        return run_event_loop

    def _drain_instead_of_loop(self, daemon):
        drained = []
        daemon._run_action_loop = MagicMock(side_effect=lambda: drained.extend(daemon.action_queue.drain()))
        return drained

    def test_run_not_liquid_cooled_queues_the_startup_power_on(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(False)
        daemon = self._make_daemon(chassis)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        drained = self._drain_instead_of_loop(daemon)
        assert daemon.run() is False
        assert [(i.action, i.event_desc, i.startup) for i in drained] == [
            (bmcctld.ACTION_POWER_ON, "STARTUP", True)]
        daemon._run_action_loop.assert_called_once()
        daemon._event_log.log_notice.assert_any_call(
            "STARTUP: non-liquid-cooled system — queueing power_on")
        assert not [t for t in threading.enumerate() if t.name == "bmcctld-events"]

    def test_run_not_liquid_cooled_skips_power_on_when_admin_down(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(False)
        daemon = self._make_daemon(chassis, admin_status=bmcctld.ADMIN_DOWN)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        daemon.controller.power_on = MagicMock()
        drained = self._drain_instead_of_loop(daemon)
        daemon.run()
        assert drained == []
        daemon.controller.power_on.assert_not_called()
        daemon._run_action_loop.assert_called_once()

    def test_run_not_liquid_cooled_starts_the_event_thread_without_the_boot_sequence(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(False)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock(side_effect=self._idle_event_loop(daemon))
        self._drain_instead_of_loop(daemon)
        daemon.run()
        daemon._initial_power_on_sequence.assert_not_called()
        daemon.event_handler.run_event_loop.assert_called_once()

    def test_run_daemon_restart_skips_boot_sequence(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        chassis.set_liquid_cooled(True)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock()
        daemon.controller.power_on = MagicMock()
        daemon.controller.init_host_state = MagicMock()
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        drained = self._drain_instead_of_loop(daemon)
        assert daemon.run() is False
        daemon._initial_power_on_sequence.assert_not_called()
        assert drained == []
        daemon.controller.power_on.assert_not_called()
        daemon.controller.init_host_state.assert_called_once()
        daemon._run_action_loop.assert_called_once()
        daemon._event_log.log_notice.assert_any_call(
            "STARTUP: Switch-Host already ONLINE (daemon restart); skipping boot sequence")

    def test_run_liquid_cooled_runs_the_boot_sequence(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(True)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock(side_effect=daemon.stop_event.set)
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        assert daemon.run() is False
        daemon._initial_power_on_sequence.assert_called_once()
        daemon._run_action_loop.assert_not_called()

    def test_run_prepares_records_before_the_event_thread_starts(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        chassis.set_liquid_cooled(True)
        daemon = self._make_daemon(chassis)
        order = []
        daemon.controller.init_host_state = MagicMock(side_effect=lambda: order.append("init_host_state"))
        daemon.event_handler.seed_chassis_module_admin_status = MagicMock(side_effect=lambda: order.append("seed"))
        daemon._sweep_stale_rm_rows = MagicMock(side_effect=lambda: order.append("sweep"))
        idle = self._idle_event_loop(daemon)
        daemon.event_handler.run_event_loop = lambda: (order.append("event_loop"), idle())
        self._drain_instead_of_loop(daemon)
        daemon.run()
        assert order == ["init_host_state", "seed", "sweep", "event_loop"]

    def test_run_sweeps_rows_a_previous_run_left_in_progress(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        _set_rm_row("CMD_OLD", bmcctld.CMD_GRACEFUL_SHUT, bmcctld.CMD_STATUS_IN_PROGRESS)
        _set_rm_row("CMD_DONE", bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_DONE, "SUCCESS")
        _set_rm_row("CMD_NEW", bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_PENDING)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        self._drain_instead_of_loop(daemon)
        daemon.run()
        old = _rm_row(daemon, "CMD_OLD")
        assert (old[bmcctld.FIELD_STATUS], old[bmcctld.FIELD_RESULT]) == (
            bmcctld.CMD_STATUS_FAILED, bmcctld.OP_RESULT_ABANDONED)
        assert _rm_row(daemon, "CMD_DONE")[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_DONE
        assert _rm_row(daemon, "CMD_NEW")[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_PENDING

    def test_run_drops_the_startup_power_on_behind_a_pending_removal_row(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(False)
        daemon = self._make_daemon(chassis)
        # A Rack Manager power-off written while the daemon was down: still PENDING
        # in Redis, replayed by the event thread at some point after the start. The
        # startup power-on is dropped whether its dispatch comes before (the scan
        # sees the PENDING row) or after (the row is still there) that replay.
        _set_rm_row("CMD_PENDING", bmcctld.CMD_POWER_OFF, bmcctld.CMD_STATUS_PENDING, None)

        def replay_pending_command():
            daemon.event_handler._handle_rack_mgr_command("CMD_PENDING", {
                bmcctld.FIELD_COMMAND: bmcctld.CMD_POWER_OFF,
                bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING,
            })

        daemon.event_handler.run_event_loop = self._idle_event_loop(
            daemon, before_idle=replay_pending_command)
        daemon.controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        order = []
        real_dispatch = daemon._dispatch

        def dispatch(item):
            order.append(item.event_desc)
            real_dispatch(item)
            if len(order) == 2:
                daemon.stop_event.set()

        daemon._dispatch = dispatch
        assert daemon.run() is False
        assert sorted(order) == ["RACK_MGR_CMD:POWER_OFF", "STARTUP"]
        # The host already reads off, so the replayed removal is a guard skip; the
        # automatic power-on is dropped, not refused.
        daemon.controller.power_off.assert_not_called()
        daemon.controller.power_on.assert_not_called()
        row = _rm_row(daemon, "CMD_PENDING")
        assert (row[bmcctld.FIELD_STATUS], row[bmcctld.FIELD_RESULT]) == (
            bmcctld.CMD_STATUS_DONE, "SUCCESS")
        daemon._event_log.log_notice.assert_any_call(
            "STARTUP power_on dropped: a Rack Manager power removal is recorded")

    def test_run_does_not_wait_for_the_subscription_replay(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(False)
        daemon = self._make_daemon(chassis)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        drained = self._drain_instead_of_loop(daemon)
        started = time.monotonic()
        daemon.run()
        assert time.monotonic() - started < 2.0
        assert [i.event_desc for i in drained] == ["STARTUP"]

    def test_run_keeps_the_startup_power_on_ahead_of_a_pending_raise_row(self, chassis):
        # Only a Rack Manager removal holds the automatic power-on back;
        # a pending cycle runs after it and may cycle the host once more.
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(False)
        daemon = self._make_daemon(chassis)
        _set_rm_row("CMD_CYCLE", bmcctld.CMD_POWER_CYCLE, bmcctld.CMD_STATUS_PENDING, None)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        drained = self._drain_instead_of_loop(daemon)
        daemon.run()
        assert [i.event_desc for i in drained] == ["STARTUP"]

    def test_stop_during_a_graceful_abandons_it_and_drains_the_queue(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=60)

        def stop_daemon():
            daemon.stop_event.set()
            daemon.action_queue.notify_all()

        requester = BlockingRequester(on_first_poll=stop_daemon)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state
        graceful_callback, raise_callback = MagicMock(), MagicMock()
        daemon.action_queue.put(_make_item(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "RACK_MGR_CMD:GRACEFUL_SHUT",
            callback=graceful_callback))
        daemon.action_queue.put(_make_item(
            bmcctld.ACTION_POWER_ON, "RACK_MGR_CMD:POWER_ON",
            callback=raise_callback))

        assert daemon.run() is False

        assert requester.closed.is_set()
        set_admin_state.assert_not_called()
        graceful_callback.assert_not_called()
        raise_callback.assert_not_called()
        state = _host_state(daemon)
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
        assert state[bmcctld.FIELD_OP_TRIGGER] == "RACK_MGR_CMD:GRACEFUL_SHUT"
        daemon._event_log.log_notice.assert_any_call(
            "Request abandoned at daemon stop: action=power_on trigger=RACK_MGR_CMD:POWER_ON (not started)")
        assert daemon.action_queue.empty()
        assert not [t for t in threading.enumerate()
                    if t.name in ("bmcctld-events", "bmcctld-graceful")]

    def test_run_cleans_up_when_the_action_loop_raises(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        daemon._run_action_loop = MagicMock(side_effect=RuntimeError("action loop failed"))
        callback = MagicMock()
        daemon.action_queue.put(_make_item(bmcctld.ACTION_POWER_ON, "late", callback=callback))
        with pytest.raises(RuntimeError, match="action loop failed"):
            daemon.run()
        assert daemon.stop_event.is_set()
        callback.assert_not_called()
        assert not [t for t in threading.enumerate() if t.name == "bmcctld-events"]

    @pytest.mark.parametrize("failure", ["init", "seed"])
    def test_failure_before_the_event_thread_starts_no_thread(self, chassis, failure):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.event_handler.run_event_loop = MagicMock()
        daemon._run_action_loop = MagicMock()
        target = {
            "init": (daemon.controller, "init_host_state"),
            "seed": (daemon.event_handler, "seed_chassis_module_admin_status"),
        }[failure]
        with patch.object(*target, side_effect=RuntimeError("startup failed")):
            with pytest.raises(RuntimeError, match="startup failed"):
                daemon.run()
        daemon.event_handler.run_event_loop.assert_not_called()
        daemon._run_action_loop.assert_not_called()

    def test_main_returns_the_exit_code_after_run(self, chassis, monkeypatch):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.event_handler.run_event_loop = self._idle_event_loop(daemon)
        daemon.stop_event.set()
        monkeypatch.setattr(bmcctld, "BmcctldDaemon", MagicMock(return_value=daemon))
        assert bmcctld.main() == bmcctld.exit_code

    def test_stop_leaves_rack_rows_in_progress_and_the_next_start_sweeps_them(self, chassis):
        """A daemon stop writes no Rack Manager row. The running command, a queued one
        and one admitted after the stop stay IN_PROGRESS; the next start closes them as
        FAILED/ABANDONED, so an abandoned removal still holds the automatic power-on back."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=60)
        keys = ("CMD_RUNNING", "CMD_QUEUED", "CMD_LATE")

        def admit(key, command):
            _set_rm_row(key, command, bmcctld.CMD_STATUS_PENDING)
            daemon.event_handler._handle_rack_mgr_command(key, {
                bmcctld.FIELD_COMMAND: command,
                bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING})

        def stop_daemon():
            daemon.stop_event.set()
            daemon.action_queue.notify_all()
            admit("CMD_LATE", bmcctld.CMD_POWER_CYCLE)

        def admit_two_commands():
            admit("CMD_RUNNING", bmcctld.CMD_GRACEFUL_SHUT)
            admit("CMD_QUEUED", bmcctld.CMD_POWER_ON)

        requester = BlockingRequester(on_first_poll=stop_daemon)
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        daemon.event_handler.run_event_loop = self._idle_event_loop(
            daemon, before_idle=admit_two_commands)
        set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.set_admin_state = set_admin_state

        assert daemon.run() is False

        set_admin_state.assert_not_called()
        assert daemon.action_queue.empty()
        assert _host_state(daemon)[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
        for key in keys:
            assert _rm_row(daemon, key)[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_IN_PROGRESS

        restarted = self._make_daemon(chassis)
        restarted._sweep_stale_rm_rows()
        for key in keys:
            row = _rm_row(restarted, key)
            assert (row[bmcctld.FIELD_STATUS], row[bmcctld.FIELD_RESULT]) == (
                bmcctld.CMD_STATUS_FAILED, bmcctld.OP_RESULT_ABANDONED)
        assert restarted._rm_removal_recorded() is True


# --------------------------------------------------------------------------
# Tests: the event loop and the admin_status latch
# --------------------------------------------------------------------------

class TestEventLoop:

    def _wire(self, event_handler, states, handler):
        table = MagicMock()
        table.pop.return_value = ("KEY", "SET", [("f", "v")])
        event_handler._subscribers = {7: (table, handler)}
        selectable = MagicMock()
        selectable.getFd.return_value = 7
        other = MagicMock()
        other.getFd.return_value = 99
        sequence = iter(states)

        def select(timeout):
            state = next(sequence)
            if state == "stop":
                event_handler.stop_event.set()
                return bmcctld.swsscommon.Select.TIMEOUT, None
            if state == "timeout":
                return bmcctld.swsscommon.Select.TIMEOUT, None
            if state == "unknown":
                return bmcctld.swsscommon.Select.OBJECT, other
            return bmcctld.swsscommon.Select.OBJECT, selectable

        event_handler.sel = MagicMock()
        event_handler.sel.select.side_effect = select
        return table

    def test_one_failing_iteration_is_logged_and_the_loop_continues(self, event_handler):
        handler = MagicMock(side_effect=[RuntimeError("boom"), None])
        self._wire(event_handler, ["object", "object", "stop"], handler)
        event_handler.run_event_loop()
        assert handler.call_count == 2
        event_handler.log_error.assert_called_once()
        assert "boom" in event_handler.log_error.call_args.args[0]

    def test_idle_select_is_skipped(self, event_handler):
        handler = MagicMock()
        self._wire(event_handler, ["object", "timeout", "object", "stop"], handler)
        event_handler.run_event_loop()
        assert handler.call_count == 2

    def test_unknown_fd_and_non_set_operations_are_ignored(self, event_handler):
        handler = MagicMock()
        table = self._wire(event_handler, ["unknown", "object", "stop"], handler)
        table.pop.return_value = ("KEY", "DEL", [])
        event_handler.run_event_loop()
        handler.assert_not_called()
        event_handler.log_warning.assert_called_once()


class TestAdminStatusLatch:

    def test_mirror_write_failure_does_not_cost_the_request(self, event_handler, controller):
        controller.update_chassis_module_admin_status = MagicMock(
            side_effect=RuntimeError("redis down"))
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_GRACEFUL_SHUTDOWN
        event_handler.log_error.assert_called()

    def test_gate_read_failure_leaves_the_latch_free_to_retry(self, event_handler):
        gate = MagicMock(side_effect=[RuntimeError("redis down"), False])
        event_handler.critical_event_checker.is_power_on_blocked = gate
        with pytest.raises(RuntimeError):
            event_handler._handle_chassis_module(
                bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        assert event_handler.action_queue.empty()
        # The same value published again is not deduplicated away: the latch
        # records a value only once its request was queued or refused.
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_ON
        assert gate.call_count == 2

    def test_blocked_admin_up_still_updates_the_latch(self, event_handler, controller):
        gate = MagicMock(return_value=True)
        event_handler.critical_event_checker.is_power_on_blocked = gate
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        assert event_handler.action_queue.empty()
        assert gate.call_count == 1
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        assert gate.call_count == 1
        assert event_handler.action_queue.empty()
        gate.return_value = False
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_GRACEFUL_SHUTDOWN
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_ON

    def test_republishing_the_row_with_other_fields_is_not_a_transition(self, event_handler):
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY, {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP})
        assert _dequeue_item(event_handler.action_queue).action == bmcctld.ACTION_POWER_ON
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP, bmcctld.FIELD_POWER_ON_DELAY: "30"})
        assert event_handler.action_queue.empty()


# Tests: ChassisModuleInfo — CHASSIS_MODULE_TABLE STATE_DB integration
# --------------------------------------------------------------------------

class TestChassisModuleInfo:

    def test_initialize_chassis_module_all_fields(self, chassis, controller):
        """initialize_chassis_module writes all expected fields to CHASSIS_MODULE_TABLE."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_NAME_FIELD] == "SWITCH-HOST"
        assert info[bmcctld.CHASSIS_MODULE_INFO_DESC_FIELD] == "Switch Host Module"
        assert info[bmcctld.CHASSIS_MODULE_INFO_SERIAL_FIELD] == "MOCK-SERIAL-1"
        assert info[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_UP
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_OFFLINE

    def test_initialize_chassis_module_oper_status_online(self, chassis, controller):
        """oper_status reflects live module state at initialization time."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_ONLINE

    def test_initialize_chassis_module_admin_down(self, chassis, controller):
        """admin_status=down is stored when module is initially down."""
        controller.initialize_chassis_module(bmcctld.ADMIN_DOWN)
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_DOWN

    def test_initialize_chassis_module_no_module(self, controller):
        """When module not found, initialize logs an error and does not write the table."""
        controller._get_switch_host_module = MagicMock(return_value=None)
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is False

    def test_power_on_mirrors_oper_status_online(self, chassis, controller):
        """power_on updates oper_status=ONLINE in CHASSIS_MODULE_TABLE."""
        controller.power_on(threading.Event())
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_mirrors_oper_status_offline(self, chassis, controller):
        """power_off updates oper_status=OFFLINE in CHASSIS_MODULE_TABLE."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off(threading.Event())
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_OFFLINE

    def test_init_host_state_mirrors_oper_status(self, chassis, controller):
        """init_host_state also updates oper_status in CHASSIS_MODULE_TABLE."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.init_host_state()
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_ONLINE

    def test_initialize_then_power_off_preserves_static_fields(self, chassis, controller):
        """oper_status update via power_off merges into entry; static fields are preserved."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        # Now power off — oper_status should update but name/serial/etc. must survive
        controller.power_off(threading.Event())
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_NAME_FIELD] == "SWITCH-HOST"
        assert info[bmcctld.CHASSIS_MODULE_INFO_SERIAL_FIELD] == "MOCK-SERIAL-1"
        assert info[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_UP
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_OFFLINE

    def test_admin_status_updated_on_chassis_module_event(self, event_handler, controller):
        """CHASSIS_MODULE admin_status event mirrors admin_status to CHASSIS_MODULE_TABLE."""
        # Initialize the table first so merging has something to merge into
        controller.initialize_chassis_module(bmcctld.ADMIN_DOWN)
        # Simulate an admin_up event while host is OFFLINE and a critical leak blocks power-on
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler.critical_event_checker.is_power_on_blocked = MagicMock(return_value=True)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_UP

    def test_admin_down_event_mirrors_admin_status(self, event_handler, controller):
        """CHASSIS_MODULE admin_down event mirrors admin_status=down to CHASSIS_MODULE_TABLE."""
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_ONLINE})
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_DOWN

    def test_daemon_init_calls_initialize_chassis_module(self, chassis):
        """BmcctldDaemon.__init__ populates CHASSIS_MODULE_TABLE at startup."""
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
        result = daemon.controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert bmcctld.CHASSIS_MODULE_INFO_NAME_FIELD in info
        assert bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD in info
        assert bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD in info

    def test_initialize_chassis_module_seeds_config_db_defaults(self, chassis, controller):
        """When CONFIG_DB CHASSIS_MODULE|SWITCH-HOST is absent, seed defaults using passed admin_status."""
        controller.chassis_module_config_table._del(bmcctld.SWITCH_HOST_MODULE_KEY)
        controller.initialize_chassis_module(bmcctld.ADMIN_DOWN)
        result = controller.chassis_module_config_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        cfg = dict(result[1])
        assert cfg[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_DOWN
        assert cfg[bmcctld.FIELD_POWER_ON_DELAY] == str(bmcctld.DEFAULT_POWER_ON_DELAY_SECS)
        assert cfg[bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT] == "120"

    @pytest.mark.parametrize("seconds", ["0", "90", "300"])
    def test_initialize_chassis_module_preserves_operator_config(self, chassis, controller, seconds):
        """Existing operator CONFIG_DB entry must not be clobbered by daemon startup."""
        controller.chassis_module_config_table.set(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            FieldValuePairs([
                (bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD, bmcctld.ADMIN_UP),
                (bmcctld.FIELD_POWER_ON_DELAY, "45"),
                (bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT, seconds),
            ]),
        )
        controller.initialize_chassis_module(bmcctld.ADMIN_DOWN)
        result = controller.chassis_module_config_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        cfg = dict(result[1])
        assert cfg[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_UP
        assert cfg[bmcctld.FIELD_POWER_ON_DELAY] == "45"
        assert cfg[bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT] == seconds

    def test_daemon_init_air_cooled_defaults_admin_up(self, chassis):
        """Air-cooled boxes default CONFIG_DB admin_status=up when no operator entry exists."""
        chassis.set_liquid_cooled(False)
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
        result = daemon.controller.chassis_module_config_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        cfg = dict(result[1])
        assert cfg[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_UP

    def test_daemon_init_liquid_cooled_defaults_admin_down(self, chassis):
        """Liquid-cooled boxes default CONFIG_DB admin_status=down when no operator entry exists."""
        chassis.set_liquid_cooled(True)
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
        result = daemon.controller.chassis_module_config_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        cfg = dict(result[1])
        assert cfg[bmcctld.CHASSIS_MODULE_INFO_ADMIN_STATUS_FIELD] == bmcctld.ADMIN_DOWN


# --------------------------------------------------------------------------
# Tests: EventLogger file target
# --------------------------------------------------------------------------

class TestEventLogger:
    """EventLogger must not tee to a container-local, unrotated file."""

    def test_no_default_log_file_constant(self):
        """The /var/log/syslog tee target is gone."""
        assert not hasattr(bmcctld, 'DEFAULT_LOG_FILE')
        assert bmcctld.CRITICAL_EVENT_LOG_FILE == "/host/bmc/event.log"

    def test_syslog_only_logger_has_no_file_handler(self):
        """Omitting log_file leaves a non-propagating logger with no file target.

        Only handlers[0] is inspected: pytest's logging plugin appends its own
        capture handlers to every logger.
        """
        import logging as _logging
        el = bmcctld.EventLogger(bmcctld.SYSLOG_IDENTIFIER)
        assert el._file_logger.propagate is False
        assert isinstance(el._file_logger.handlers[0], _logging.NullHandler)

    def test_syslog_only_logger_still_logs(self):
        """Log calls on a syslog-only instance do not raise."""
        el = bmcctld.EventLogger(bmcctld.SYSLOG_IDENTIFIER)
        el.log_debug("d")
        el.log_info("i")
        el.log_notice("n")
        el.log_warning("w")
        el.log_error("e")

    def test_module_loggers_target_expected_files(self):
        """_daemon_logger is syslog-only; _event_logger keeps the host file."""
        assert bmcctld._daemon_logger._file_logger.name.endswith("syslog_only")
        assert bmcctld._event_logger._file_logger.name.endswith("event_log")


class TestDatabaseThreadOwnership:

    def test_no_table_is_used_by_two_threads(self, monkeypatch, chassis):
        """A swsscommon.Table is not thread-safe. The event thread, the main thread and
        a third one use the same daemon objects at the same time; every Table must be
        used by one thread only. A connector is shared: it only builds Tables."""
        store = {}
        created = []
        errors = []
        admission_thread_name = threading.current_thread().name
        release = threading.Event()
        event_write_started = threading.Event()
        worker_write_started = threading.Event()
        stop_event = None

        def table_data(db_name, table_name):
            return store.setdefault((db_name, table_name), {})

        table_data("STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE)["CMD_THREAD"] = {
            bmcctld.FIELD_COMMAND: bmcctld.CMD_POWER_OFF,
            bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING,
        }
        table_data("CONFIG_DB", bmcctld.CHASSIS_MODULE_TABLE)[
            bmcctld.SWITCH_HOST_MODULE_KEY] = {
                bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP,
                bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "10",
            }
        table_data("STATE_DB", bmcctld.SYSTEM_LEAK_STATUS_TABLE)[
            bmcctld.SYSTEM_LEAK_STATUS_KEY] = {
                bmcctld.FIELD_DEVICE_LEAK_STATUS: "NORMAL",
            }

        class OwnedObject:
            """Bound to the thread that first uses it; creation may be elsewhere."""

            def __init__(self, kind, retain=True):
                self.kind = kind
                self.owner_name = threading.current_thread().name
                self.user_id = None
                self.user_name = None
                self._active = threading.Lock()
                if retain:
                    created.append(self)

            def check_owner(self):
                if self.user_id is None:
                    self.user_id = threading.get_ident()
                    self.user_name = threading.current_thread().name
                assert threading.get_ident() == self.user_id, (
                    "{} created by {} first used by {} now used by {}".format(
                        self.kind, self.owner_name, self.user_name,
                        threading.current_thread().name))

            def enter(self):
                self.check_owner()
                assert self._active.acquire(False), (
                    "overlapping use of {} owned by {}".format(
                        self.kind, self.owner_name))

            def leave(self):
                self._active.release()

        class Connection:
            def __init__(self, db_name):
                self.db_name = db_name

        class StrictTable(OwnedObject):
            def __init__(self, db, table_name, kind="table"):
                self.db_name = db.db_name
                self.table_name = table_name
                self._waited = False
                super().__init__("{} {}".format(table_name, kind))

            def _maybe_wait(self, method):
                if self._waited or method != "set":
                    return
                role = threading.current_thread().name
                if role == "db-owner-event" and \
                        self.table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE:
                    started = event_write_started
                elif role == "db-owner-worker" and \
                        self.table_name == bmcctld.HOST_STATE_TABLE:
                    started = worker_write_started
                else:
                    return
                self._waited = True
                started.set()
                assert release.wait(3), "timed out waiting for concurrent DB access"

            def set(self, key, fvs):
                self.enter()
                try:
                    self._maybe_wait("set")
                    values = fvs.fv_dict if hasattr(fvs, "fv_dict") else dict(fvs)
                    table_data(self.db_name, self.table_name).setdefault(
                        key, {}).update(values)
                finally:
                    self.leave()

            def get(self, key):
                self.enter()
                try:
                    value = table_data(self.db_name, self.table_name).get(key)
                    return [value is not None, list(value.items()) if value else []]
                finally:
                    self.leave()

            def getKeys(self):
                self.enter()
                try:
                    return list(table_data(self.db_name, self.table_name))
                finally:
                    self.leave()

        class StrictSubscriber(StrictTable):
            def __init__(self, db, table_name):
                super().__init__(db, table_name, "subscriber")

            def getFd(self):
                # A file descriptor is a plain integer; reading it is not DB use.
                return id(self)

            def pop(self):
                self.enter()
                try:
                    rows = table_data(self.db_name, self.table_name)
                    key = next(iter(rows))
                    return key, "SET", list(rows[key].items())
                finally:
                    self.leave()

        class StrictSelect(OwnedObject):
            OBJECT = 0
            TIMEOUT = 1

            def __init__(self):
                self.selectables = []
                self.event_index = 0
                super().__init__("select")

            def addSelectable(self, selectable):
                # Registration happens where the selector is built; use starts
                # with select().
                self.selectables.append(selectable)

            def select(self, timeout=-1, interrupt_on_signal=False):
                self.enter()
                try:
                    event_tables = (
                        bmcctld.RACK_MANAGER_COMMAND_TABLE,
                        bmcctld.CHASSIS_MODULE_TABLE,
                    )
                    if self.event_index == len(event_tables):
                        return self.TIMEOUT, None
                    table_name = event_tables[self.event_index]
                    self.event_index += 1
                    selected = next(
                        item for item in self.selectables
                        if item.table_name == table_name)
                    if self.event_index == len(event_tables):
                        stop_event.set()
                    return self.OBJECT, SelectedObject(selected)
                finally:
                    self.leave()

        class SelectedObject(OwnedObject):
            def __init__(self, subscriber):
                self.fd = subscriber.getFd()
                super().__init__("selected object")

            def getFd(self):
                self.check_owner()
                return self.fd

        monkeypatch.setattr(
            bmcctld.daemon_base, "db_connect", lambda db_name: Connection(db_name))
        monkeypatch.setattr(
            bmcctld.swsscommon, "Table",
            lambda db, table_name: StrictTable(db, table_name))
        monkeypatch.setattr(
            bmcctld.swsscommon, "SubscriberStateTable", StrictSubscriber)
        monkeypatch.setattr(bmcctld.swsscommon, "Select", StrictSelect)

        with patch('sonic_platform.platform.Platform') as mock_platform:
            mock_platform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
        controller = daemon.controller
        policy_reader = daemon.policy_reader
        critical_checker = daemon.critical_event_checker
        action_queue = daemon.action_queue
        event_handler = daemon.event_handler
        stop_event = daemon.stop_event

        def capture_errors(function):
            try:
                function()
            except Exception as error:
                errors.append((threading.current_thread().name, error))

        def event_access():
            event_handler.run_event_loop()
            controller.get_db_device_status()
            policy_reader.get_graceful_shutdown_timeout()
            policy_reader.get_gnoi_cert_paths()
            critical_checker.get_system_leak_status()
            critical_checker.is_power_on_blocked()

        def worker_access():
            controller._update_host_state(
                bmcctld.SWITCH_HOST_POWERING_OFF,
                bmcctld.SWITCH_HOST_OFFLINE)
            policy_reader.get_graceful_shutdown_timeout()
            policy_reader.get_gnoi_cert_paths()
            critical_checker.get_system_leak_status()
            critical_checker.is_power_on_blocked()

        event_thread = threading.Thread(
            target=capture_errors, args=(event_access,), name="db-owner-event")
        worker_thread = threading.Thread(
            target=capture_errors, args=(worker_access,), name="db-owner-worker")
        event_thread.start()
        worker_thread.start()

        both_started = event_write_started.wait(2) and worker_write_started.wait(2)
        if both_started:
            controller.write_operation_start(TEST_UUID4, "test")
            policy_reader.get_graceful_shutdown_timeout()
            policy_reader.get_gnoi_cert_paths()
            critical_checker.get_system_leak_status()
            critical_checker.is_power_on_blocked()
        release.set()
        event_thread.join(3)
        worker_thread.join(3)

        assert both_started, errors
        assert not event_thread.is_alive()
        assert not worker_thread.is_alive()
        assert errors == []

        item = _dequeue_item(action_queue)
        item.on_complete(True)
        command = table_data(
            "STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE)["CMD_THREAD"]
        assert command[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_DONE

        # Each thread read or wrote these rows, and each through Tables of its own:
        # a second user of one Table fails check_owner() above.
        tables = [obj for obj in created
                  if isinstance(obj, StrictTable) and not isinstance(obj, StrictSubscriber)]
        roles = {admission_thread_name, "db-owner-event", "db-owner-worker"}
        for table_name in (bmcctld.HOST_STATE_TABLE, bmcctld.GNMI_CLIENT_TABLE,
                           bmcctld.SYSTEM_LEAK_STATUS_TABLE):
            assert {obj.user_name for obj in tables
                    if obj.table_name == table_name} == roles
        assert {obj.user_name for obj in tables
                if obj.table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE} == {
            admission_thread_name, "db-owner-event"}
