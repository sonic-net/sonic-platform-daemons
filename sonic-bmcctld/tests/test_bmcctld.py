"""
    Unit tests for bmcctld daemon.

    Tests cover all major event handlers and the initial power-on sequence
"""

import os
import sys
import itertools
import queue
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


def _make_operation(action, event_desc="test", priority=None, callback=None,
                    rack_cmd_key=None):
    if priority is None:
        priority = bmcctld.action_priority(action)
    item = bmcctld.ActionItem(
        action, event_desc, priority,
        on_complete=callback, rack_cmd_key=rack_cmd_key)
    return bmcctld.Operation(
        item, TEST_REQUEST_ID,
        bmcctld.OperationRunner.INITIAL_STAGES[action])


def _dequeue_item(action_queue):
    return action_queue.get_nowait()[2]

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
    gs = bmcctld.GracefulShutdownHandler(controller, policy_reader)
    return gs


@pytest.fixture
def event_handler(controller, policy_reader, critical_event_checker):
    stop_event = threading.Event()
    stop_event.set()  # Prevent blocking in tests
    action_queue = queue.PriorityQueue()
    action_sequence = itertools.count()
    eh = bmcctld.BmcEventHandler(
        action_queue, action_sequence, policy_reader,
        critical_event_checker, stop_event, controller)
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
        result = controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert chassis.switch_host.get_admin_state() is True

    def test_power_off_calls_set_admin_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        result = controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert chassis.switch_host.get_admin_state() is False

    def test_power_cycle_calls_do_power_cycle(self, chassis, controller):
        result = controller.power_cycle(_make_operation(bmcctld.ACTION_POWER_CYCLE))
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert chassis.switch_host.power_cycle_called is True

    def test_power_on_updates_host_state(self, chassis, controller):
        controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        result = controller.host_state_table.get(bmcctld.HOST_STATE_KEY)
        assert result[0] is True
        state = dict(result[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_updates_host_state(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
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
        controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
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
        controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
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
        controller.power_cycle(_make_operation(bmcctld.ACTION_POWER_CYCLE))
        assert captured.get(bmcctld.FIELD_DEVICE_POWER_STATE) == bmcctld.SWITCH_HOST_POWER_CYCLING
        assert captured.get(bmcctld.FIELD_DEVICE_STATUS) in (bmcctld.SWITCH_HOST_ONLINE, bmcctld.SWITCH_HOST_OFFLINE)
        # Final entry must reflect POWER_CYCLE and confirmed ONLINE
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_CYCLE
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_ONLINE

    def test_get_db_power_state(self, chassis, controller):
        """get_db_power_state returns the value stored by the last _update_host_state call."""
        controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        assert controller.get_db_power_state() == bmcctld.POWER_STATE_ON

    def test_power_on_rolls_back_state_on_exception(self, chassis, controller):
        """If set_admin_state raises, STATE_DB is restored to the pre-call snapshot."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
        chassis.switch_host.set_admin_state = MagicMock(side_effect=RuntimeError("hw fault"))
        result = controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        state = dict(controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_STATUS] == bmcctld.SWITCH_HOST_OFFLINE
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF

    def test_power_off_leaves_transition_on_exception(self, chassis, controller):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        chassis.switch_host.set_admin_state = MagicMock(side_effect=RuntimeError("hw fault"))
        result = controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
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
    def test_dth_stale_operation_is_abandoned_on_startup(
            self, chassis, death_point, stale_state, live_status, op_result,
            stale_reason, request_id):
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
        fresh_daemon.event_handler.run_event_loop = MagicMock(
            side_effect=fresh_daemon.event_handler._subscription_ready.set)
        fresh_daemon._run_action_loop = MagicMock()

        assert fresh_daemon.run() is False

        state = dict(retained_state.get(bmcctld.HOST_STATE_KEY)[1])
        expected_power_state = (
            bmcctld.POWER_STATE_ON
            if live_status == MockModule.MODULE_STATUS_ONLINE
            else bmcctld.POWER_STATE_OFF)
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == expected_power_state
        assert state[bmcctld.FIELD_DEVICE_STATUS] == str(live_status).upper()
        assert state[bmcctld.FIELD_OP_REQUEST_ID] == request_id
        assert state[bmcctld.FIELD_OP_TRIGGER] == death_point
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
        assert state[bmcctld.FIELD_OP_REASON] == "-"
        fresh_daemon.controller.log_warning.assert_called_once_with(
            "OP_ABANDONED request_id={} stale_state={}".format(
                request_id, stale_state))
        assert fresh_daemon.operation_runner.current is None
        assert fresh_daemon.action_queue.empty()
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    @pytest.mark.parametrize("request_id", [
        "-",
        "",
        TEST_UUID4.upper(),
        "not-a-uuid",
    ])
    def test_dth_invalid_request_id_is_not_abandoned(
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

    def test_dth_terminal_operation_is_not_rewritten(self, controller):
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
            result = controller._verify_oper_status(bmcctld.SWITCH_HOST_ONLINE, 30, "test")
        assert result == bmcctld.PowerCallResult.CONFIRMED
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('time.monotonic')
    def test_verify_oper_status_timeout(self, mock_monotonic, mock_sleep, chassis, controller):
        # Simulate: deadline set at t=0+30=30, first loop check t=0 (<30), sleep,
        # second loop check t=31 (>=30) → exit without match
        mock_monotonic.side_effect = [0, 0, 31, 31]
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        result = controller._verify_oper_status(bmcctld.SWITCH_HOST_ONLINE, 30, "test")
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        mock_sleep.assert_called_once_with(bmcctld.POWER_VERIFY_POLL_INTERVAL_SECS)

    def test_power_on_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        assert chassis.switch_host.get_admin_state() is True  # API was still called

    def test_power_off_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        assert chassis.switch_host.get_admin_state() is False  # API was still called

    def test_power_cycle_returns_false_when_verify_fails(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_cycle(_make_operation(bmcctld.ACTION_POWER_CYCLE))
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        assert chassis.switch_host.power_cycle_called is True  # API was still called

    def test_power_cycle_uses_double_timeout(self, chassis, controller):
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.CONFIRMED) as mock_verify:
            operation = _make_operation(bmcctld.ACTION_POWER_CYCLE)
            controller.power_cycle(operation)
        mock_verify.assert_called_once_with(
            bmcctld.SWITCH_HOST_ONLINE,
            bmcctld.POWER_VERIFY_TIMEOUT_SECS * 2,
            "power_cycle",
            None,
        )

    @pytest.mark.parametrize(
        "action, method_name",
        [
            (bmcctld.ACTION_POWER_OFF, "power_off"),
            (bmcctld.ACTION_POWER_ON, "power_on"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
        ],
    )
    def test_p_cancel_before_wrapper_entry_issues_no_platform_call(
            self, action, method_name, chassis, controller):
        operation = _make_operation(action)
        operation.cancel.set()
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        result = getattr(controller, method_name)(operation, operation.cancel)
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
    def test_p_cancel_under_lock_issues_no_platform_call(
            self, action, method_name, chassis, controller):
        operation = _make_operation(action)
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()

        class CancelOnEnter:
            def __enter__(self):
                operation.cancel.set()
            def __exit__(self, exc_type, exc_value, tb):
                return False

        controller._power_lock = CancelOnEnter()
        result = getattr(controller, method_name)(operation, operation.cancel)
        assert result == bmcctld.PowerCallResult.CANCELLED
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    @pytest.mark.parametrize(
        "action, method_name",
        [
            (bmcctld.ACTION_POWER_ON, "power_on"),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle"),
        ],
    )
    def test_s_raise_is_refused_on_under_lock_critical_reread(
            self, action, method_name, chassis, controller):
        checker = MagicMock()
        checker.has_any_critical_event.return_value = True
        controller.set_critical_event_checker(checker)
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        operation = _make_operation(action)
        result = getattr(controller, method_name)(operation, operation.cancel)
        assert result == bmcctld.PowerCallResult.REFUSED_LEAK
        checker.has_any_critical_event.assert_called_once()
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    def test_p_verify_wait_is_cancelled_without_poll_interval_delay(
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
    def test_p_cancel_during_issued_call_wins_over_call_exception(
            self, action, method_name, platform_method, chassis, controller):
        operation = _make_operation(action)

        def cancel_then_raise(*_args):
            operation.cancel.set()
            raise RuntimeError("platform call failed after cancellation")

        setattr(chassis.switch_host, platform_method, cancel_then_raise)
        result = getattr(controller, method_name)(operation, operation.cancel)

        assert result == bmcctld.PowerCallResult.CANCELLED

    @pytest.mark.parametrize(
        "action, method_name, issued_stage, confirmed_stage",
        [
            (bmcctld.ACTION_POWER_OFF, "power_off",
             bmcctld.STAGE_POWER_OFF_ISSUED,
             bmcctld.STAGE_POWER_OFF_CONFIRMED),
            (bmcctld.ACTION_POWER_ON, "power_on",
             bmcctld.STAGE_POWER_ON_ISSUED,
             bmcctld.STAGE_POWER_ON_CONFIRMED),
            (bmcctld.ACTION_POWER_CYCLE, "power_cycle",
             bmcctld.STAGE_POWER_CYCLE_ISSUED,
             bmcctld.STAGE_POWER_CYCLE_CONFIRMED),
        ],
    )
    def test_q_stage_is_issued_during_call_and_confirmed_after_verify(
            self, action, method_name, issued_stage, confirmed_stage,
            chassis, controller):
        operation = _make_operation(action)
        observed = []
        if action == bmcctld.ACTION_POWER_CYCLE:
            original = chassis.switch_host.do_power_cycle
            def platform_call():
                observed.append(operation.stage)
                original()
            chassis.switch_host.do_power_cycle = platform_call
        else:
            original = chassis.switch_host.set_admin_state
            def platform_call(up):
                observed.append(operation.stage)
                original(up)
            chassis.switch_host.set_admin_state = platform_call
            if action == bmcctld.ACTION_POWER_OFF:
                chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        result = getattr(controller, method_name)(operation, operation.cancel)
        assert result == bmcctld.PowerCallResult.CONFIRMED
        assert observed == [issued_stage]
        assert operation.stage == confirmed_stage

    def test_w_power_on_timeout_keeps_existing_final_state_behavior(
            self, chassis, controller):
        operation = _make_operation(bmcctld.ACTION_POWER_ON)
        with patch.object(
                controller, '_verify_oper_status',
                return_value=bmcctld.PowerCallResult.NOT_CONFIRMED):
            result = controller.power_on(operation, operation.cancel)
        assert result == bmcctld.PowerCallResult.NOT_CONFIRMED
        state = dict(controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_ON


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
    ], ids=["missing-field", "invalid", "empty", "null"])
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
        table = policy_reader._thread_database.table("CONFIG_DB", "BMC_GNOI")
        expected = {
            "ca_crt": "/etc/sonic/bmc-link/ca.crt",
            "client_crt": "/etc/sonic/bmc-link/client.crt",
            "client_key": "/etc/sonic/bmc-link/client.key",
        }
        if field:
            _set_table_entry(table, "certs", {field: "/etc/sonic/custom.pem"})
            expected[field] = "/etc/sonic/custom.pem"
        with patch.object(table, "set") as write_row:
            paths = policy_reader.get_gnoi_cert_paths()
            assert paths == expected
            paths["ca_crt"] = "modified local copy"
            assert policy_reader.get_gnoi_cert_paths() == expected
        write_row.assert_not_called()

    def test_certificate_reader_projects_only_known_fields(self, policy_reader):
        table = policy_reader._thread_database.table("CONFIG_DB", "BMC_GNOI")
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
        assert policy["system_minor_leak_action"] == bmcctld.ACTION_SYSLOG_ONLY
        assert policy["rack_mgr_leak_policy"] == "enabled"
        assert policy["rack_mgr_critical_alert_action"] == bmcctld.ACTION_SYSLOG_ONLY
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
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=10)
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
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
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
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
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
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
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
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
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
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

    @pytest.mark.parametrize("method", ["execute", "execute_restart"])
    @pytest.mark.parametrize("graceful,reason", [
        (True, None), (False, bmcctld.OP_REASON_DEADLINE),
        (False, bmcctld.OP_REASON_PREEMPTED),
    ])
    @pytest.mark.parametrize("power_result", [
        bmcctld.PowerCallResult.CANCELLED,
        bmcctld.PowerCallResult.NOT_CONFIRMED,
    ])
    def test_shutdown_and_restart_share_off_failure_outcomes(
            self, graceful_shutdown, method, graceful, reason, power_result):
        action = (bmcctld.ACTION_GRACEFUL_RESTART if method == "execute_restart"
                  else bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        operation = _make_operation(action)
        graceful_shutdown._run_shutdown_leg = MagicMock(return_value=(graceful, reason))
        graceful_shutdown.controller.power_off = MagicMock(return_value=power_result)
        graceful_shutdown.controller.power_on = MagicMock()
        graceful_shutdown._event_log = MagicMock()

        outcome = getattr(graceful_shutdown, method)(operation, MagicMock())

        if reason == bmcctld.OP_REASON_PREEMPTED:
            expected_result = bmcctld.OP_RESULT_PREEMPTED
            graceful_shutdown.controller.power_off.assert_not_called()
        else:
            expected_result = (bmcctld.OP_RESULT_PREEMPTED
                               if power_result == bmcctld.PowerCallResult.CANCELLED
                               else bmcctld.OP_RESULT_POWER_OFF_FAILED)
            graceful_shutdown.controller.power_off.assert_called_once_with(
                operation, operation.cancel)
        assert outcome == (expected_result, reason or "-", False)
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
    def test_n_report_attribution(self, resp, expected):
        assert bmcctld.classify_report(resp, OUR_REQUEST_ID) == expected


class TestGracefulQualification:

    def _configure_qualified(self, monkeypatch, tmp_path, graceful_shutdown):
        cert_dir = tmp_path / "certs"
        cert_dir.mkdir()
        cert_paths = {field: str(cert_dir / os.path.basename(default))
                      for field, default in bmcctld.DEFAULT_GNOI_CERT_PATHS.items()}
        for path in cert_paths.values():
            with open(path, "wb") as stream:
                stream.write(b"certificate-data")

        monkeypatch.setattr(bmcctld.device_info, "is_switch_bmc", lambda: True)
        return cert_paths

    def test_g_qualification_accepts_only_complete_positive_gate(
            self, monkeypatch, tmp_path, graceful_shutdown):
        cert_paths = self._configure_qualified(monkeypatch, tmp_path, graceful_shutdown)
        with patch("builtins.open") as direct_open:
            assert graceful_shutdown._is_graceful_qualified(cert_paths) is True
        direct_open.assert_not_called()

    @pytest.mark.parametrize(
        "failure",
        ["wrong-role", "missing-cert", "empty-cert", "cert-race"],
    )
    def test_g_qualification_rejects_incomplete_gate(
            self, failure, monkeypatch, tmp_path, graceful_shutdown):
        cert_paths = self._configure_qualified(
            monkeypatch, tmp_path, graceful_shutdown)

        if failure == "wrong-role":
            monkeypatch.setattr(bmcctld.device_info, "is_switch_bmc", lambda: False)
        elif failure == "missing-cert":
            os.unlink(cert_paths["client_crt"])
        elif failure == "empty-cert":
            with open(cert_paths["client_crt"], "wb"):
                pass
        else:
            monkeypatch.setattr(bmcctld.os.path, "isfile", lambda _path: True)
            monkeypatch.setattr(bmcctld.os.path, "getsize",
                                MagicMock(side_effect=FileNotFoundError))

        assert graceful_shutdown._is_graceful_qualified(cert_paths) is False

    def test_g_qualification_identity_exception_is_logged(
            self, monkeypatch, tmp_path, graceful_shutdown):
        cert_paths = self._configure_qualified(
            monkeypatch, tmp_path, graceful_shutdown)
        failure = RuntimeError("unexpected identity failure")
        monkeypatch.setattr(
            bmcctld.device_info, "is_switch_bmc",
            MagicMock(side_effect=failure))
        graceful_shutdown.log_error = MagicMock()

        assert graceful_shutdown._is_graceful_qualified(cert_paths) is False
        graceful_shutdown.log_error.assert_called_once()
        assert str(failure) in graceful_shutdown.log_error.call_args.args[0]

    def test_g_qualification_is_reevaluated_between_operations(
            self, monkeypatch, tmp_path, graceful_shutdown):
        cert_paths = self._configure_qualified(
            monkeypatch, tmp_path, graceful_shutdown)
        os.unlink(cert_paths["client_crt"])
        assert graceful_shutdown._is_graceful_qualified(cert_paths) is False
        with open(cert_paths["client_crt"], "wb") as stream:
            stream.write(b"certificate-data")
        assert graceful_shutdown._is_graceful_qualified(cert_paths) is True


class FakeGrpcError(Exception):
    pass


class TestGnoiRequester:

    def _make_requester(self, monkeypatch, tmp_path):
        cert_dir = tmp_path / "certs"
        cert_dir.mkdir()
        (cert_dir / "ca.crt").write_bytes(b"ca")
        (cert_dir / "client.key").write_bytes(b"key")
        (cert_dir / "client.crt").write_bytes(b"cert")

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
            {"ca_crt": str(cert_dir / "ca.crt"),
             "client_crt": str(cert_dir / "client.crt"),
             "client_key": str(cert_dir / "client.key")},
            bmcctld.GNOI_SERVER_NAME)
        return requester, grpc_api, proto_api, client_factory, client, cert_dir

    def test_f_requester_uses_exact_mtls_and_rpc_shapes(self, monkeypatch, tmp_path):
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
    def test_f_transport_failures_are_normalized_once(
            self, failure_point, monkeypatch, tmp_path):
        requester, grpc_api, _, client_factory, client, cert_dir = \
            self._make_requester(monkeypatch, tmp_path)

        if failure_point == "cert-read":
            (cert_dir / "ca.crt").unlink()
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
        table = graceful_shutdown.policy_reader._thread_database.table(
            "CONFIG_DB", "BMC_GNOI")
        _set_table_entry(table, "certs", paths)
        timeout_table = graceful_shutdown.policy_reader._thread_database.table(
            "CONFIG_DB", bmcctld.CHASSIS_MODULE_TABLE)
        _set_table_entry(timeout_table, bmcctld.SWITCH_HOST_MODULE_KEY,
                         {bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT: "10"})
        monkeypatch.setattr(bmcctld.device_info, "is_switch_bmc", lambda: True)
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

    @pytest.mark.parametrize("action", [
        bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.ACTION_GRACEFUL_RESTART,
    ])
    def test_configured_files_reach_tls_through_operation(
            self, action, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, table, grpc_api, client = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        operation = _make_operation(action)
        operation.cancel.wait = MagicMock(return_value=False)
        execute = (graceful_shutdown.execute_restart
                   if action == bmcctld.ACTION_GRACEFUL_RESTART
                   else graceful_shutdown.execute)
        with patch.object(table, "get", wraps=table.get) as read_row, \
                patch("builtins.open", wraps=builtins.open) as read_file:
            outcome = execute(operation, bmcctld.GnoiRequester)

        assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        read_row.assert_called_once_with("certs")
        assert read_file.call_args_list == [
            call(paths["ca_crt"], "rb"), call(paths["client_key"], "rb"),
            call(paths["client_crt"], "rb"),
        ]
        grpc_api.ssl_channel_credentials.assert_called_once_with(
            root_certificates=b"ca_crt", private_key=b"client_key",
            certificate_chain=b"client_crt")
        client.system.Reboot.assert_called_once()
        client.__exit__.assert_called_once_with(None, None, None)
        graceful_shutdown.controller.power_off.assert_called_once_with(
            operation, operation.cancel)
        if action == bmcctld.ACTION_GRACEFUL_RESTART:
            graceful_shutdown.controller.power_on.assert_called_once_with(
                operation, operation.cancel)
        else:
            graceful_shutdown.controller.power_on.assert_not_called()

    def test_snapshot_is_shared_until_next_operation(
            self, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, table, grpc_api, _ = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        replacement = tmp_path / "replacement.pem"
        replacement.write_bytes(b"new-ca")
        qualify = graceful_shutdown._is_graceful_qualified

        def change_config_after_resolution(selected):
            table.set("certs", FieldValuePairs([
                ("ca_crt", str(replacement)), ("extra", "ignored")]))
            return qualify(selected)

        monkeypatch.setattr(graceful_shutdown, "_is_graceful_qualified",
                            change_config_after_resolution)
        with patch.object(table, "get", wraps=table.get) as read_row:
            for _ in range(2):
                outcome = graceful_shutdown.execute(
                    _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
                    bmcctld.GnoiRequester)
                assert outcome == (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        assert read_row.call_args_list == [call("certs"), call("certs")]
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
        paths, table, _, _ = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        monkeypatch.setattr(bmcctld, "DEFAULT_GNOI_CERT_PATHS", dict(paths))
        empty_file = tmp_path / "empty.pem"
        empty_file.touch()
        invalid = {"empty": "", "relative": "relative.pem",
                   "missing": str(tmp_path / "missing.pem"),
                   "empty-file": str(empty_file)}[failure]
        _set_table_entry(table, "certs", {field: invalid})
        factory = MagicMock()
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        assert graceful_shutdown.execute(operation, factory) == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, bmcctld.OP_REASON_NOT_QUALIFIED, True)
        factory.assert_not_called()
        graceful_shutdown.controller.power_off.assert_called_once_with(
            operation, operation.cancel)
        assert field in graceful_shutdown.log_warning.call_args.args[0]

    @pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
    def test_selected_file_read_failure_uses_rpc_failure(
            self, failure, monkeypatch, tmp_path, graceful_shutdown, chassis):
        paths, _, grpc_api, client = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        with patch("builtins.open", side_effect=failure("file changed")) as read_file:
            outcome = graceful_shutdown.execute(
                _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN),
                bmcctld.GnoiRequester)
        assert outcome == (bmcctld.OP_RESULT_SUCCESS_FORCED,
                           bmcctld.OP_REASON_RPC_FAILURE, True)
        read_file.assert_called_once_with(paths["ca_crt"], "rb")
        grpc_api.ssl_channel_credentials.assert_not_called()
        client.system.Reboot.assert_not_called()

    @pytest.mark.parametrize("action,confirmed_result", [
        (bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.OP_RESULT_SUCCESS_FORCED),
        (bmcctld.ACTION_GRACEFUL_RESTART, bmcctld.OP_RESULT_POWER_ON_FAILED),
    ])
    @pytest.mark.parametrize("power_result", [
        bmcctld.PowerCallResult.CONFIRMED, bmcctld.PowerCallResult.NOT_CONFIRMED,
    ])
    @pytest.mark.parametrize("cancelled", [False, True])
    def test_db_read_error_uses_existing_worker_recovery(
            self, action, confirmed_result, power_result, cancelled,
            monkeypatch, tmp_path, graceful_shutdown, chassis):
        _, table, _, _ = self._setup(
            monkeypatch, tmp_path, graceful_shutdown, chassis)
        table.get = MagicMock(side_effect=RuntimeError("DB unavailable"))
        controller = graceful_shutdown.controller
        controller.power_off.return_value = power_result
        controller._update_host_state = MagicMock()
        factory = MagicMock()
        daemon = SimpleNamespace(graceful_shutdown=graceful_shutdown,
                                 controller=controller, gnoi_requester_factory=factory)
        runner = bmcctld.OperationRunner(daemon, queue.PriorityQueue(), itertools.count())
        runner.log_error = MagicMock()
        operation = _make_operation(action)
        if cancelled:
            operation.cancel.set()
        runner._run_worker(operation)
        table.get.assert_called_once_with("certs")
        factory.assert_not_called()
        controller._update_host_state.assert_not_called()
        controller.power_on.assert_not_called()
        if cancelled:
            controller.power_off.assert_not_called()
            assert operation.outcome[0] == bmcctld.OP_RESULT_PREEMPTED
        else:
            controller.power_off.assert_called_once_with(operation, operation.cancel)
            expected = (confirmed_result if power_result == bmcctld.PowerCallResult.CONFIRMED
                        else bmcctld.OP_RESULT_POWER_OFF_FAILED)
            assert operation.outcome == (
                expected, bmcctld.OP_REASON_UNCLASSIFIED,
                expected == bmcctld.OP_RESULT_SUCCESS_FORCED)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestGracefulShutdownOperation:

    @pytest.mark.parametrize("action", [
        bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.ACTION_GRACEFUL_RESTART,
    ])
    @pytest.mark.parametrize("config", ["fresh", "missing-field", "stored-zero"])
    def test_default_and_stored_timeout_drive_graceful_leg(
            self, action, config, graceful_shutdown, chassis):
        controller = graceful_shutdown.controller
        table = controller.chassis_module_config_table
        if config == "stored-zero":
            table.set(bmcctld.SWITCH_HOST_MODULE_KEY, [
                (bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT, "0")])
        controller.initialize_chassis_module(bmcctld.ADMIN_UP)
        if config == "missing-field":
            table.hdel(bmcctld.SWITCH_HOST_MODULE_KEY,
                       bmcctld.FIELD_GRACEFUL_SHUTDOWN_TIMEOUT)
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        controller.power_on = MagicMock(return_value=bmcctld.PowerCallResult.CONFIRMED)
        operation = _make_operation(action)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "active [bmc-req:{}]".format(TEST_REQUEST_ID), active=True)
        factory = MagicMock(return_value=requester)
        clock = FakeClock()
        operation.cancel.wait = MagicMock(
            side_effect=lambda delay: clock.advance(delay) or False)
        execute = (graceful_shutdown.execute_restart
                   if action == bmcctld.ACTION_GRACEFUL_RESTART
                   else graceful_shutdown.execute)
        with patch("bmcctld.time.monotonic", side_effect=clock):
            outcome = execute(operation, factory)

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
        controller.power_off.assert_called_once_with(operation, operation.cancel)
        if action == bmcctld.ACTION_GRACEFUL_RESTART:
            expected_wait += 10
            controller.power_on.assert_called_once_with(operation, operation.cancel)
        else:
            controller.power_on.assert_not_called()
        assert clock.now == expected_wait

    def _setup(self, graceful_shutdown, chassis, timeout=10,
               power_result=None):
        if power_result is None:
            power_result = bmcctld.PowerCallResult.CONFIRMED
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = \
            MagicMock(return_value=timeout)
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=power_result)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        requester = MagicMock()
        factory = MagicMock(return_value=requester)
        return operation, requester, factory

    def test_g_not_qualified_precedes_timeout_zero(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=0)
        graceful_shutdown._is_graceful_qualified.return_value = False
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_NOT_QUALIFIED,
            True,
        )
        factory.assert_not_called()
        requester.send_halt.assert_not_called()

    def test_h_operation_start_race_already_off(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_ALREADY_OFF,
            True,
        )
        factory.assert_not_called()
        graceful_shutdown.controller.power_off.assert_called_once_with(
            operation, operation.cancel)

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
    def test_f_tagged_terminal_reports_force_with_exact_reason(
            self, response, expected_reason, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = response
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED, expected_reason, True)
        requester.poll_status.assert_called_once()

    def test_f_poll_rpc_failure_forces_without_retry(
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
        ],
        ids=["foreign", "untagged", "active", "duplicate-tag"],
    )
    def test_n_unattributed_reports_wait_to_deadline(
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

    def test_f_poll_consuming_budget_starts_no_sleep_or_second_poll(
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

    def test_f_budget_consumed_during_classification_starts_no_sleep(
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
    def test_f_deadline_precedes_late_terminal_report(
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

    def test_p_cancel_before_open_sends_nothing(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        operation.cancel.set()
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        requester.open.assert_not_called()
        requester.send_halt.assert_not_called()
        requester.close.assert_called_once()
        graceful_shutdown.controller.power_off.assert_not_called()

    def test_p_cancel_during_open_sends_nothing(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.open.side_effect = operation.cancel.set
        outcome = graceful_shutdown.execute(operation, factory)
        assert outcome[0] == bmcctld.OP_RESULT_PREEMPTED
        requester.send_halt.assert_not_called()
        requester.poll_status.assert_not_called()
        requester.close.assert_called_once()
        graceful_shutdown.controller.power_off.assert_not_called()

    def test_p_cancel_inside_poll_allows_only_that_rpc(
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

    def test_p_cancelled_power_off_preserves_completed_graceful_leg(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))

        def cancel_power_off(_operation, _cancel):
            operation.cancel.set()
            return bmcctld.PowerCallResult.CANCELLED

        graceful_shutdown.controller.power_off.side_effect = cancel_power_off

        outcome = graceful_shutdown.execute(operation, factory)

        assert outcome == (bmcctld.OP_RESULT_PREEMPTED, "-", False)
        graceful_shutdown.controller.power_off.assert_called_once_with(
            operation, operation.cancel)

    def test_w_power_off_failure_keeps_leg_reason(
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

    def test_requester_is_closed_when_classifier_raises(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(graceful_shutdown, chassis)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        with patch('bmcctld.classify_report', side_effect=RuntimeError("bug")):
            with pytest.raises(RuntimeError):
                graceful_shutdown.execute(operation, factory)
        requester.close.assert_called_once()
        graceful_shutdown.controller.power_off.assert_not_called()


class TestGracefulRestartOperation:

    def _setup(self, graceful_shutdown, chassis, timeout=0,
               power_off_result=None, power_on_result=None):
        if power_off_result is None:
            power_off_result = bmcctld.PowerCallResult.CONFIRMED
        if power_on_result is None:
            power_on_result = bmcctld.PowerCallResult.CONFIRMED
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = \
            MagicMock(return_value=timeout)
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        graceful_shutdown.controller.power_off = MagicMock(
            return_value=power_off_result)
        graceful_shutdown.controller.power_on = MagicMock(
            return_value=power_on_result)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_RESTART)
        operation.cancel.wait = MagicMock(return_value=False)
        requester = MagicMock()
        factory = MagicMock(return_value=requester)
        return operation, requester, factory

    def test_h_graceful_restart_uses_two_steps_and_preserves_admin_status(
            self, graceful_shutdown, chassis):
        assert bmcctld.RESTART_PAUSE_SECS == 10
        controller = graceful_shutdown.controller
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = \
            MagicMock(return_value=10)
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        original_set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=original_set_admin_state)
        controller._update_host_state = MagicMock(
            wraps=controller._update_host_state)
        controller.chassis_module_config_table.set(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            FieldValuePairs([(bmcctld.FIELD_ADMIN_STATUS,
                              bmcctld.ADMIN_DOWN)]))
        admin_before = dict(controller.chassis_module_config_table.get(
            bmcctld.SWITCH_HOST_MODULE_KEY)[1])
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_RESTART)
        operation.cancel.wait = MagicMock(return_value=False)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        factory = MagicMock(return_value=requester)

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
        assert operation.stage == bmcctld.STAGE_POWER_ON_CONFIRMED
        assert operation.cancel.wait.call_args_list == [
            call(bmcctld.RESTART_PAUSE_SECS)]
        assert [args[0] for args, _ in
                controller._update_host_state.call_args_list] == [
            bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
            bmcctld.SWITCH_HOST_POWERING_OFF,
            bmcctld.POWER_STATE_OFF,
            bmcctld.SWITCH_HOST_POWERING_ON,
            bmcctld.POWER_STATE_ON,
        ]
        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False), call(True)]
        assert chassis.switch_host.power_cycle_called is False
        assert dict(controller.chassis_module_config_table.get(
            bmcctld.SWITCH_HOST_MODULE_KEY)[1]) == admin_before
        requester.send_halt.assert_called_once()

    def test_h_forced_restart_keeps_leg_reason_and_uses_no_cycle(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis)

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            True,
        )
        graceful_shutdown.controller.power_off.assert_called_once_with(
            operation, operation.cancel)
        operation.cancel.wait.assert_called_once_with(
            bmcctld.RESTART_PAUSE_SECS)
        graceful_shutdown.controller.power_on.assert_called_once_with(
            operation, operation.cancel)
        factory.assert_not_called()
        requester.send_halt.assert_not_called()
        assert chassis.switch_host.power_cycle_called is False

    def test_p_restart_handshake_cancellation_makes_no_power_call(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=10)
        operation.cancel.set()

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        requester.open.assert_not_called()
        requester.send_halt.assert_not_called()
        graceful_shutdown.controller.power_off.assert_not_called()
        graceful_shutdown.controller.power_on.assert_not_called()

    def test_p_restart_power_off_cancellation_stops_before_pause_and_raise(
            self, graceful_shutdown, chassis):
        operation, _, factory = self._setup(
            graceful_shutdown, chassis,
            power_off_result=bmcctld.PowerCallResult.CANCELLED)

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            False,
        )
        operation.cancel.wait.assert_not_called()
        graceful_shutdown.controller.power_on.assert_not_called()

    def test_p_restart_cancelled_during_pause_stays_off(
            self, graceful_shutdown, chassis):
        operation, requester, factory = self._setup(
            graceful_shutdown, chassis, timeout=10)
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        operation.cancel.wait.return_value = True

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (bmcctld.OP_RESULT_PREEMPTED, "-", False)
        assert operation.stage == bmcctld.STAGE_PAUSE
        graceful_shutdown.controller.power_on.assert_not_called()

    def test_p_restart_power_on_cancellation_retains_shutdown_leg_reason(
            self, graceful_shutdown, chassis):
        operation, _, factory = self._setup(
            graceful_shutdown, chassis,
            power_on_result=bmcctld.PowerCallResult.CANCELLED)

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            False,
        )
        operation.cancel.wait.assert_called_once_with(
            bmcctld.RESTART_PAUSE_SECS)
        graceful_shutdown.controller.power_on.assert_called_once_with(
            operation, operation.cancel)

    def test_w_restart_power_off_failure_stops_before_pause_and_raise(
            self, graceful_shutdown, chassis):
        operation, _, factory = self._setup(
            graceful_shutdown, chassis,
            power_off_result=bmcctld.PowerCallResult.NOT_CONFIRMED)

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_POWER_OFF_FAILED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            False,
        )
        operation.cancel.wait.assert_not_called()
        graceful_shutdown.controller.power_on.assert_not_called()

    def test_w_restart_power_on_failure_keeps_shutdown_leg_reason(
            self, graceful_shutdown, chassis):
        operation, _, factory = self._setup(
            graceful_shutdown, chassis,
            power_on_result=bmcctld.PowerCallResult.NOT_CONFIRMED)

        outcome = graceful_shutdown.execute_restart(operation, factory)

        assert outcome == (
            bmcctld.OP_RESULT_POWER_ON_FAILED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            False,
        )
        graceful_shutdown.controller.power_on.assert_called_once_with(
            operation, operation.cancel)

    def test_p_restart_raise_is_refused_by_under_lock_critical_read(
            self, graceful_shutdown, chassis):
        controller = graceful_shutdown.controller
        graceful_shutdown.policy_reader.get_graceful_shutdown_timeout = \
            MagicMock(return_value=0)
        graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        original_set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=original_set_admin_state)
        controller.critical_event_checker = MagicMock()
        controller.critical_event_checker.has_any_critical_event.return_value = True
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_RESTART)
        operation.cancel.wait = MagicMock(return_value=False)

        outcome = graceful_shutdown.execute_restart(operation, MagicMock())

        assert outcome == (
            bmcctld.OP_RESULT_OFF_LEAK_BLOCKED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            False,
        )
        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False)]
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_OFFLINE
        state = dict(controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == \
            bmcctld.SWITCH_HOST_OFFLINE
        controller.critical_event_checker.has_any_critical_event.assert_called_once()


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
        assert item.priority == 2
        assert item.rack_cmd_key == "CMD_1"
        assert item.on_complete is not None

    def test_power_on_command_no_leak_enqueues_power_on(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        event_handler._handle_rack_mgr_command("CMD_2", self._cmd_fvs(bmcctld.CMD_POWER_ON))
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.priority == 5
        assert item.rack_cmd_key == "CMD_2"
        assert item.on_complete is not None

    def test_power_on_command_blocked_by_critical_leak(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
        event_handler._handle_rack_mgr_command("CMD_3", self._cmd_fvs(bmcctld.CMD_POWER_ON))
        assert event_handler.action_queue.empty()

    def test_power_cycle_command_enqueues_power_cycle(self, event_handler):
        event_handler._handle_rack_mgr_command("CMD_4", self._cmd_fvs(bmcctld.CMD_POWER_CYCLE))
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_CYCLE
        assert item.priority == 4
        assert item.rack_cmd_key == "CMD_4"

    def test_q_power_cycle_command_blocked_by_critical_leak(self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = \
            MagicMock(return_value=True)
        event_handler._handle_rack_mgr_command(
            "CMD_4B", self._cmd_fvs(bmcctld.CMD_POWER_CYCLE))
        assert event_handler.action_queue.empty()

    def test_already_processed_command_is_skipped(self, event_handler):
        event_handler._set_cmd_status = MagicMock()
        event_handler._handle_rack_mgr_command(
            "CMD_5", self._cmd_fvs(bmcctld.CMD_POWER_ON, bmcctld.CMD_STATUS_DONE))
        assert event_handler.action_queue.empty()
        event_handler._set_cmd_status.assert_not_called()

    def test_unknown_command_is_logged(self, event_handler):
        event_handler._set_cmd_status = MagicMock(wraps=event_handler._set_cmd_status)
        event_handler._handle_rack_mgr_command("CMD_6", self._cmd_fvs("INVALID_CMD"))
        assert event_handler.action_queue.empty()
        assert event_handler._set_cmd_status.call_args_list == [
            call("CMD_6", bmcctld.CMD_STATUS_IN_PROGRESS),
            call("CMD_6", bmcctld.CMD_STATUS_FAILED, "UNKNOWN_COMMAND"),
        ]

    def test_command_callbacks_preserve_their_own_rows(self, event_handler):
        commands = [
            (bmcctld.CMD_POWER_ON, bmcctld.ACTION_POWER_ON, 5),
            (bmcctld.CMD_POWER_OFF, bmcctld.ACTION_POWER_OFF, 2),
            (bmcctld.CMD_POWER_CYCLE, bmcctld.ACTION_POWER_CYCLE, 4),
            (bmcctld.CMD_GRACEFUL_SHUT, bmcctld.ACTION_GRACEFUL_SHUTDOWN, 3),
            (bmcctld.CMD_GRACEFUL_RESTART, bmcctld.ACTION_GRACEFUL_RESTART, 4),
        ]
        leak_check = MagicMock(return_value=False)
        event_handler.critical_event_checker.has_any_critical_event = leak_check
        table = event_handler._thread_database.table(
            "STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE)
        callbacks = []
        for index, (command, action, priority) in enumerate(commands):
            key = "CMD_{}".format(index)
            fields = self._cmd_fvs(command)
            fields["requester"] = "test-{}".format(index)
            fields[bmcctld.FIELD_REQUEST_ID] = "request-{}".format(index)
            table.set(key, FieldValuePairs(list(fields.items())))
            event_handler._handle_rack_mgr_command(key, fields)
            item = _dequeue_item(event_handler.action_queue)
            assert (item.action, item.priority, item.rack_cmd_key) == (action, priority, key)
            assert item.event_desc == "RACK_MGR_CMD:" + command
            assert dict(table.get(key)[1])[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_IN_PROGRESS
            callbacks.append((item.on_complete, key, fields, index))
        assert leak_check.call_count == 2

        for callback, key, fields, index in reversed(callbacks):
            success = index % 2 == 0
            detail = "BUSY" if index == 1 else ""
            callback(success, detail)
            row = dict(table.get(key)[1])
            assert row[bmcctld.FIELD_STATUS] == (
                bmcctld.CMD_STATUS_DONE if success else bmcctld.CMD_STATUS_FAILED)
            assert row[bmcctld.FIELD_RESULT] == (
                "SUCCESS" if success else (detail or "ERROR"))
            for field in (bmcctld.FIELD_COMMAND, bmcctld.FIELD_REQUEST_ID, "requester"):
                assert row[field] == fields[field]

    def test_q_graceful_restart_command_is_admitted_without_early_leak_gate(
            self, event_handler):
        event_handler.critical_event_checker.has_any_critical_event = \
            MagicMock(return_value=True)

        event_handler._handle_rack_mgr_command(
            "CMD_RESTART", self._cmd_fvs(bmcctld.CMD_GRACEFUL_RESTART))

        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_RESTART
        assert item.priority == 4
        assert item.rack_cmd_key == "CMD_RESTART"
        leak_check = event_handler.critical_event_checker.has_any_critical_event
        leak_check.assert_not_called()


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
        assert item.priority == 3

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
        assert item.priority == 3

    def test_admin_up_powers_on_when_no_leak(self, event_handler, controller):
        # host is OFFLINE → admin_up should enqueue power_on
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.priority == 5

    def test_admin_up_no_action_when_already_online(self, event_handler, controller):
        # host is already ONLINE → admin_up should be a no-op
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_ONLINE})
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        assert event_handler.action_queue.empty()

    def test_admin_up_blocked_by_critical_leak(self, event_handler, controller):
        _set_table_entry(controller.host_state_table, bmcctld.HOST_STATE_KEY,
                         {bmcctld.FIELD_DEVICE_STATUS: bmcctld.SWITCH_HOST_OFFLINE})
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
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
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)

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
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
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
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert item.priority == 0

    def test_critical_rack_alert_graceful_has_priority_one(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(
                rack_mgr_critical_alert_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_rack_mgr_alert(
            "Rack_level_leak",
            {bmcctld.FIELD_SEVERITY: bmcctld.ALERT_SEVERITY_CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_GRACEFUL_SHUTDOWN
        assert item.priority == 1

    def test_critical_system_leak_graceful_shutdown(self, event_handler):
        event_handler.policy_reader.get_leak_control_policy = MagicMock(
            return_value=self._make_policy(system_critical_leak_action=bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        )
        event_handler._handle_system_leak(
            bmcctld.SYSTEM_LEAK_STATUS_KEY,
            {bmcctld.FIELD_DEVICE_LEAK_STATUS: bmcctld.SYSTEM_LEAK_CRITICAL},
        )
        item = _dequeue_item(event_handler.action_queue)
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
        item = _dequeue_item(event_handler.action_queue)
        assert item.action == bmcctld.ACTION_POWER_OFF
        assert item.priority == 0

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
        assert item.priority == 0
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
# Tests: BmcctldDaemon - action loop
# --------------------------------------------------------------------------

class TestBmcctldDaemonActionLoop:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
            daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        return daemon

    def test_gnoi_requester_factory_is_injectable(self, chassis):
        daemon = self._make_daemon(chassis)
        assert daemon.gnoi_requester_factory is bmcctld.GnoiRequester
        replacement = MagicMock()
        daemon.gnoi_requester_factory = replacement
        assert daemon.gnoi_requester_factory is replacement

    def test_execute_graceful_shutdown(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.graceful_shutdown.execute = MagicMock(return_value=(
            bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True))
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        daemon.operation_runner._run_worker(operation)
        daemon.graceful_shutdown.execute.assert_called_once()
        assert operation.outcome == (
            bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)

    def test_execute_graceful_restart(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.graceful_shutdown.execute_restart = MagicMock(return_value=(
            bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True))
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_RESTART)

        daemon.operation_runner._run_worker(operation)

        daemon.graceful_shutdown.execute_restart.assert_called_once_with(
            operation, daemon.gnoi_requester_factory)
        assert operation.outcome == (
            bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)

    def test_execute_power_off(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        operation = _make_operation(bmcctld.ACTION_POWER_OFF)
        daemon.operation_runner._run_worker(operation)
        daemon.controller.power_off.assert_called_once()
        assert operation.outcome == (bmcctld.OP_RESULT_SUCCESS, "-", True)

    def test_execute_power_on(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        operation = _make_operation(bmcctld.ACTION_POWER_ON)
        daemon.operation_runner._run_worker(operation)
        daemon.controller.power_on.assert_called_once()
        assert operation.outcome == (bmcctld.OP_RESULT_SUCCESS, "-", True)

    def test_execute_power_cycle(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_cycle = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        operation = _make_operation(bmcctld.ACTION_POWER_CYCLE)
        daemon.operation_runner._run_worker(operation)
        daemon.controller.power_cycle.assert_called_once()
        assert operation.outcome == (bmcctld.OP_RESULT_SUCCESS, "-", True)

    def test_on_complete_called_with_success(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "test",
            bmcctld.action_priority(bmcctld.ACTION_POWER_ON),
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join()
        daemon.operation_runner.process_next(timeout=0)
        callback.assert_called_once_with(True, bmcctld.OP_RESULT_SUCCESS)

    def test_on_complete_called_with_failure(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.NOT_CONFIRMED)
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "test",
            bmcctld.action_priority(bmcctld.ACTION_POWER_ON),
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join()
        daemon.operation_runner.process_next(timeout=0)
        callback.assert_called_once_with(
            False, bmcctld.OP_RESULT_POWER_ON_FAILED)

    def test_action_loop_processes_queued_items(self, chassis):
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        def power_off(_operation, _cancel):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.PowerCallResult.CONFIRMED
        def power_on(_operation, _cancel):
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
            return bmcctld.PowerCallResult.CONFIRMED
        daemon.controller.power_off = MagicMock(side_effect=power_off)
        daemon.controller.power_on = MagicMock(side_effect=power_on)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "evt1", 5))
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "evt2", 2))

        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join()
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join()
        daemon.operation_runner.process_next(timeout=0)
        daemon.controller.power_on.assert_called_once()
        daemon.controller.power_off.assert_called_once()

    # -- Idempotency skip tests --

    def test_execute_power_off_skipped_when_already_offline(self, chassis):
        """power_off is not issued when host is already OFFLINE; on_complete(True) fired."""
        # chassis.switch_host starts OFFLINE by default
        daemon = self._make_daemon(chassis)
        daemon.controller.power_off = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "dup-event", 2,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.controller.power_off.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_execute_graceful_shutdown_skipped_when_already_offline(self, chassis):
        """graceful_shutdown is not issued when host is already OFFLINE."""
        daemon = self._make_daemon(chassis)
        daemon.graceful_shutdown.execute = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "dup-event", 3,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_ALREADY_OFF

    def test_execute_power_on_skipped_when_already_online(self, chassis):
        """power_on is not issued when host is already ONLINE; on_complete(True) fired."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "dup-event", 5,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.controller.power_on.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")

    def test_execute_power_cycle_not_skipped_when_online(self, chassis):
        """power_cycle always executes regardless of current oper_status."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_cycle = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_CYCLE, "test", 4)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join()
        daemon.controller.power_cycle.assert_called_once()

    def test_execute_power_off_skipped_when_powering_off_in_progress(self, chassis):
        """power_off is skipped when STATE_DB device_power_state shows POWERING_OFF (already in progress)."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        # Simulate a power_off already in progress by writing transitional power state to DB
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_OFF)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        daemon.controller.power_off = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "dup-leak-event", 2,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.controller.power_off.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")

    def test_execute_graceful_shutdown_skipped_when_powering_off_in_progress(self, chassis):
        """graceful_shutdown is skipped when STATE_DB device_power_state shows POWERING_OFF."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_OFF)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        daemon.graceful_shutdown.execute = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "dup-cmd", 3,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")

    def test_execute_graceful_shutdown_skipped_when_graceful_shutting_down_in_progress(self, chassis):
        """graceful_shutdown is skipped when STATE_DB device_power_state shows GRACEFUL_SHUTTING_DOWN."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        daemon.graceful_shutdown.execute = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "dup-grace-event", 3,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.graceful_shutdown.execute.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")

    def test_execute_power_on_skipped_when_powering_on_in_progress(self, chassis):
        """power_on is skipped when STATE_DB device_power_state shows POWERING_ON (already in progress)."""
        # Host is OFFLINE on platform but DB shows POWERING_ON (race: just started)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_ON)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        daemon.controller.power_on = MagicMock()
        callback = MagicMock()
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "dup-on-event", 5,
            on_complete=callback)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        daemon.controller.power_on.assert_not_called()
        callback.assert_called_once_with(True, "guard_skip")

    def test_p_terminal_powering_on_residue_is_retryable(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_ON)
        daemon.controller.write_operation_result(
            bmcctld.OP_RESULT_PREEMPTED, bmcctld.OP_REASON_PREEMPTED)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)

        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "retry-after-preempt", 5))
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)

        daemon.controller.power_on.assert_called_once()

    @pytest.mark.parametrize("oper_status", [
        MockModule.MODULE_STATUS_ONLINE,
        MockModule.MODULE_STATUS_OFFLINE,
    ])
    def test_failed_power_off_residue_is_not_guarded(
            self, oper_status, chassis):
        chassis.switch_host.set_oper_status(oper_status)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_OFF)
        daemon.controller.write_operation_result(
            bmcctld.OP_RESULT_POWER_OFF_FAILED, "-")
        daemon.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "retry", 2))

        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)

        daemon.controller.power_off.assert_called_once()

    def test_critical_shutdown_ignores_recorded_transition(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(
            bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN)
        daemon.controller.write_operation_start(TEST_REQUEST_ID, "active")
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical", 0)
        assert daemon.operation_runner._guard_should_skip(item) is False


class TestOperationRunnerConcurrency:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as platform:
            platform.return_value.get_chassis.return_value = chassis
            return bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)

    def test_p_priority_queue_orders_by_priority_then_arrival(self, chassis):
        daemon = self._make_daemon(chassis)
        items = [
            bmcctld.ActionItem(bmcctld.ACTION_POWER_ON, "on", 5),
            bmcctld.ActionItem(bmcctld.ACTION_POWER_CYCLE, "cycle-a", 4),
            bmcctld.ActionItem(bmcctld.ACTION_POWER_CYCLE, "cycle-b", 4),
            bmcctld.ActionItem(bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shutdown", 3),
            bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "critical-off", 0),
        ]
        for item in items:
            daemon.operation_runner.enqueue(item)
        ordered = [daemon.action_queue.get_nowait()[2].event_desc
                   for _ in items]
        assert ordered == [
            "critical-off", "shutdown", "cycle-a", "cycle-b", "on"]
        with pytest.raises(AttributeError):
            items[0].priority = 0

    def test_q_operation_record_lifecycle_and_release(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "record-test", 5)
        daemon.operation_runner.enqueue(item)
        daemon.operation_runner.process_next(timeout=0)
        operation = daemon.operation_runner.current
        start = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert start[bmcctld.FIELD_OP_REQUEST_ID] == operation.request_id
        assert start[bmcctld.FIELD_OP_TRIGGER] == "record-test"
        assert start[bmcctld.FIELD_OP_RESULT] == "-"
        assert start[bmcctld.FIELD_OP_REASON] == "-"
        assert operation.thread.name == "bmcctld-op"
        assert operation.thread.daemon is True
        operation.thread.join()
        daemon.operation_runner.process_next(timeout=0)
        terminal = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert terminal[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS
        assert terminal[bmcctld.FIELD_OP_REASON] == "-"
        assert daemon.operation_runner.current is None
        assert operation.joined_callbacks == []

    def test_q_shared_rack_command_row_keeps_fields_through_completion(
            self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        command_table = Table(
            daemon._thread_database.connection("STATE_DB"),
            bmcctld.RACK_MANAGER_COMMAND_TABLE)
        command_table.set("CMD_SHARED", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_OFF),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_PENDING),
            ("opaque", "preserve-me"),
        ]))
        table_factory = bmcctld.swsscommon.Table

        def shared_table(db, table_name):
            if table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE:
                return command_table
            return table_factory(db, table_name)

        with patch('bmcctld.swsscommon.Table', side_effect=shared_table):
            command_fvs = dict(command_table.get("CMD_SHARED")[1])
            daemon.event_handler._handle_rack_mgr_command(
                "CMD_SHARED", command_fvs)
            in_progress = dict(command_table.get("CMD_SHARED")[1])
            assert in_progress[bmcctld.FIELD_STATUS] == \
                bmcctld.CMD_STATUS_IN_PROGRESS
            assert bmcctld.FIELD_REQUEST_ID not in in_progress

            daemon.operation_runner.process_next(timeout=0)
            operation = daemon.operation_runner.current
            request_id = operation.request_id
            spawned = dict(command_table.get("CMD_SHARED")[1])
            assert spawned[bmcctld.FIELD_REQUEST_ID] == request_id
            operation.thread.join(2)
            daemon.operation_runner.process_next(timeout=0)

        completed = dict(command_table.get("CMD_SHARED")[1])
        assert completed[bmcctld.FIELD_COMMAND] == bmcctld.CMD_POWER_OFF
        assert completed[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_DONE
        assert completed[bmcctld.FIELD_RESULT] == "SUCCESS"
        assert completed[bmcctld.FIELD_REQUEST_ID] == request_id
        assert completed["opaque"] == "preserve-me"

    def test_q_graceful_restart_rack_retry_joins_one_operation(
            self, chassis):
        daemon = self._make_daemon(chassis)
        started = threading.Event()
        release = threading.Event()

        def blocked_restart(_operation, _factory):
            started.set()
            assert release.wait(2)
            return bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True

        daemon.graceful_shutdown.execute_restart = MagicMock(
            side_effect=blocked_restart)
        command_table = Table(
            daemon._thread_database.connection("STATE_DB"),
            bmcctld.RACK_MANAGER_COMMAND_TABLE)
        for key in ("RESTART_1", "RESTART_2"):
            command_table.set(key, FieldValuePairs([
                (bmcctld.FIELD_COMMAND, bmcctld.CMD_GRACEFUL_RESTART),
                (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_PENDING),
            ]))
        table_factory = bmcctld.swsscommon.Table

        def shared_table(db, table_name):
            if table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE:
                return command_table
            return table_factory(db, table_name)

        with patch('bmcctld.swsscommon.Table', side_effect=shared_table):
            daemon.event_handler._handle_rack_mgr_command(
                "RESTART_1", dict(command_table.get("RESTART_1")[1]))
            daemon.operation_runner.process_next(timeout=0)
            assert started.wait(1)
            operation = daemon.operation_runner.current
            request_id = operation.request_id

            daemon.event_handler._handle_rack_mgr_command(
                "RESTART_2", dict(command_table.get("RESTART_2")[1]))
            daemon.operation_runner.process_next(timeout=0)
            assert len(operation.joined_callbacks) == 1
            for key in ("RESTART_1", "RESTART_2"):
                in_progress = dict(command_table.get(key)[1])
                assert in_progress[bmcctld.FIELD_STATUS] == \
                    bmcctld.CMD_STATUS_IN_PROGRESS
                assert in_progress[bmcctld.FIELD_REQUEST_ID] == request_id

            release.set()
            operation.thread.join(2)
            daemon.operation_runner.process_next(timeout=0)

        daemon.graceful_shutdown.execute_restart.assert_called_once()
        for key in ("RESTART_1", "RESTART_2"):
            completed = dict(command_table.get(key)[1])
            assert completed[bmcctld.FIELD_COMMAND] == \
                bmcctld.CMD_GRACEFUL_RESTART
            assert completed[bmcctld.FIELD_STATUS] == \
                bmcctld.CMD_STATUS_DONE
            assert completed[bmcctld.FIELD_RESULT] == "SUCCESS"
            assert completed[bmcctld.FIELD_REQUEST_ID] == request_id

    def test_s_under_lock_raise_refusal_maps_to_operation_and_callback(
            self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.REFUSED_LEAK)
        callback = MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "late-critical", 5,
            on_complete=callback))

        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_OFF_LEAK_BLOCKED
        callback.assert_called_once_with(False, "CRITICAL_LEAK_PRESENT")

    def test_p_identical_noncritical_request_joins(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        started = threading.Event()
        release = threading.Event()
        def blocked_power_off(_operation, _cancel):
            started.set()
            assert release.wait(2)
            return bmcctld.PowerCallResult.CONFIRMED
        daemon.controller.power_off = MagicMock(side_effect=blocked_power_off)
        daemon.event_handler._set_cmd_request_id = MagicMock()
        first_callback = MagicMock()
        second_callback = MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "first", 2,
            on_complete=first_callback, rack_cmd_key="CMD_1"))
        daemon.operation_runner.process_next(timeout=0)
        assert started.wait(1)
        request_id = daemon.operation_runner.current.request_id
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "retry", 2,
            on_complete=second_callback, rack_cmd_key="CMD_2"))
        daemon.operation_runner.process_next(timeout=0)
        assert len(daemon.operation_runner.current.joined_callbacks) == 1
        release.set()
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)
        daemon.controller.power_off.assert_called_once()
        first_callback.assert_called_once_with(
            True, bmcctld.OP_RESULT_SUCCESS)
        second_callback.assert_called_once_with(
            True, bmcctld.OP_RESULT_SUCCESS)
        assert daemon.event_handler._set_cmd_request_id.call_args_list == [
            call("CMD_1", request_id),
            call("CMD_2", request_id),
        ]

    def test_p_equal_or_lower_priority_is_refused_busy(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        started = threading.Event()
        release = threading.Event()
        def blocked_power_off(_operation, _cancel):
            started.set()
            assert release.wait(2)
            return bmcctld.PowerCallResult.CONFIRMED
        daemon.controller.power_off = MagicMock(side_effect=blocked_power_off)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "running", 2))
        daemon.operation_runner.process_next(timeout=0)
        assert started.wait(1)
        running_id = daemon.operation_runner.current.request_id
        callback = MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "lower", 5, on_complete=callback))
        daemon.operation_runner.process_next(timeout=0)
        callback.assert_called_once_with(False, "BUSY")
        assert daemon.operation_runner.current.request_id == running_id
        release.set()
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

    def test_p_critical_same_action_is_refused_not_joined(self, chassis):
        daemon = self._make_daemon(chassis)
        operation = _make_operation(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, priority=1)
        operation.thread = MagicMock()
        operation.thread.is_alive.return_value = True
        daemon.operation_runner.current = operation
        callback = MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "second-critical", 1,
            on_complete=callback))

        daemon.operation_runner.process_next(timeout=0)

        callback.assert_called_once_with(False, "BUSY")
        assert operation.joined_callbacks == []

    def test_p_higher_priority_cancels_records_then_requeues(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        started = threading.Event()
        def graceful_wait(operation, _factory):
            started.set()
            assert operation.cancel.wait(2)
            return (bmcctld.OP_RESULT_PREEMPTED,
                    bmcctld.OP_REASON_PREEMPTED, False)
        daemon.graceful_shutdown.execute = MagicMock(side_effect=graceful_wait)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "ordinary", 3))
        daemon.operation_runner.process_next(timeout=0)
        assert started.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical", 0))
        daemon.operation_runner.process_next(timeout=0)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_PREEMPTED
        assert daemon.operation_runner.current is None
        assert _dequeue_item(daemon.action_queue).event_desc == "critical"

    def test_p_critical_off_preempts_restart_pause_and_is_guard_resolved(
            self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_graceful_shutdown_timeout = \
            MagicMock(return_value=0)
        daemon.graceful_shutdown._is_graceful_qualified = \
            MagicMock(return_value=True)
        original_set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=original_set_admin_state)
        daemon.controller.power_on = MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "restart", 4))
        daemon.operation_runner.process_next(timeout=0)
        restart_operation = daemon.operation_runner.current

        deadline = time.monotonic() + 1
        while restart_operation.stage != bmcctld.STAGE_PAUSE and \
                time.monotonic() < deadline:
            time.sleep(0.001)
        assert restart_operation.stage == bmcctld.STAGE_PAUSE

        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical-off", 0))
        daemon.operation_runner.process_next(timeout=0)

        assert restart_operation.outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_TIMEOUT_ZERO,
            False,
        )
        assert daemon.operation_runner.current is None
        daemon.controller.power_on.assert_not_called()
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_OFFLINE

        daemon.operation_runner.process_next(timeout=0)

        assert daemon.operation_runner.current is None
        assert daemon.action_queue.empty()
        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False)]
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == \
            bmcctld.SWITCH_HOST_OFFLINE
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_SUCCESS

    def test_admin_down_offline_completes_through_common_guard(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_OFF, bmcctld.SWITCH_HOST_OFFLINE)
        daemon.controller.power_off = MagicMock()
        daemon.operation_runner._new_request_id = MagicMock(
            return_value=TEST_UUID4)

        daemon.event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )

        assert daemon.operation_runner.process_next(timeout=0) is True
        assert daemon.operation_runner.current is None
        assert daemon.action_queue.empty()
        daemon.controller.power_off.assert_not_called()
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_REQUEST_ID] == TEST_UUID4
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_SUCCESS_FORCED
        assert state[bmcctld.FIELD_OP_REASON] == \
            bmcctld.OP_REASON_ALREADY_OFF

    @pytest.mark.parametrize(
        "timeout, graceful, expected_reason",
        [
            (0, False, bmcctld.OP_REASON_TIMEOUT_ZERO),
            (10, True, "-"),
        ],
        ids=["timeout-zero", "tagged-graceful"],
    )
    def test_admin_down_preempts_restart_while_host_is_off(
            self, timeout, graceful, expected_reason, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(
            return_value=timeout)
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(
            return_value=True)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)
        daemon.operation_runner._new_request_id = MagicMock(
            return_value=TEST_REQUEST_ID)
        original_set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=original_set_admin_state)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "restart", 4))
        daemon.operation_runner.process_next(timeout=0)
        restart_operation = daemon.operation_runner.current

        deadline = time.monotonic() + 1
        while restart_operation.stage != bmcctld.STAGE_PAUSE and \
                time.monotonic() < deadline:
            time.sleep(0.001)
        reached_pause = restart_operation.stage == bmcctld.STAGE_PAUSE
        if not reached_pause:
            restart_operation.cancel.set()
            restart_operation.thread.join(2)
        assert reached_pause
        assert restart_operation.leg_graceful is graceful

        daemon.event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )
        admitted = not daemon.action_queue.empty()
        if admitted:
            daemon.operation_runner.process_next(timeout=0)
            daemon.operation_runner.process_next(timeout=0)
        else:
            restart_operation.cancel.set()
            restart_operation.thread.join(2)

        assert admitted
        assert restart_operation.outcome == (
            bmcctld.OP_RESULT_PREEMPTED, expected_reason, False)
        assert daemon.operation_runner.current is None
        assert daemon.action_queue.empty()
        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False)]
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_OFFLINE

    def test_admin_down_preempts_power_on_verification(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(
            bmcctld.SWITCH_HOST_POWERING_ON,
            bmcctld.SWITCH_HOST_OFFLINE)
        verifying = threading.Event()

        def power_on_verification(operation, cancel):
            operation.stage = bmcctld.STAGE_POWER_ON_ISSUED
            chassis.switch_host.set_oper_status(
                MockModule.MODULE_STATUS_ONLINE)
            verifying.set()
            assert cancel.wait(2)
            return bmcctld.PowerCallResult.CANCELLED

        def confirmed_power_off(_operation, _cancel):
            chassis.switch_host.set_oper_status(
                MockModule.MODULE_STATUS_OFFLINE)
            return bmcctld.PowerCallResult.CONFIRMED

        daemon.controller.power_on = MagicMock(
            side_effect=power_on_verification)
        daemon.controller.power_off = MagicMock(
            side_effect=confirmed_power_off)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "power-on", 5))
        daemon.operation_runner.process_next(timeout=0)
        power_on_operation = daemon.operation_runner.current
        assert verifying.wait(1)
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_ONLINE

        daemon.event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_DOWN},
        )
        admitted = not daemon.action_queue.empty()
        if admitted:
            daemon.operation_runner.process_next(timeout=0)
            daemon.operation_runner.process_next(timeout=0)
            shutdown_operation = daemon.operation_runner.current
            shutdown_operation.thread.join(2)
            daemon.operation_runner.process_next(timeout=0)
        else:
            power_on_operation.cancel.set()
            power_on_operation.thread.join(2)

        assert admitted
        assert power_on_operation.outcome == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        assert daemon.operation_runner.current is None
        assert daemon.action_queue.empty()
        daemon.controller.power_off.assert_called_once()
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_OFFLINE

    def test_admin_up_during_shutdown_remains_lower_priority(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        started = threading.Event()
        release = threading.Event()

        def blocked_shutdown(_operation, _factory):
            started.set()
            assert release.wait(2)
            return (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)

        daemon.graceful_shutdown.execute = MagicMock(
            side_effect=blocked_shutdown)
        daemon.controller.write_operation_start = MagicMock(
            wraps=daemon.controller.write_operation_start)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "shutdown", 3))
        daemon.operation_runner.process_next(timeout=0)
        shutdown_operation = daemon.operation_runner.current
        assert started.wait(1)
        daemon.controller._update_host_state(
            bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN,
            bmcctld.SWITCH_HOST_OFFLINE)
        daemon.event_handler.critical_event_checker.has_any_critical_event = \
            MagicMock(return_value=False)

        daemon.event_handler._handle_chassis_module(
            bmcctld.SWITCH_HOST_MODULE_KEY,
            {bmcctld.FIELD_ADMIN_STATUS: bmcctld.ADMIN_UP},
        )
        assert not daemon.action_queue.empty()
        assert daemon.operation_runner.process_next(timeout=0) is True

        assert daemon.operation_runner.current is shutdown_operation
        assert daemon.action_queue.empty()
        assert daemon.controller.write_operation_start.call_count == 1
        release.set()
        shutdown_operation.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

    def test_stop_reaps_worker_that_finishes_during_join(self, chassis):
        daemon = self._make_daemon(chassis)
        first_callback = MagicMock()
        second_callback = MagicMock()
        operation = _make_operation(
            bmcctld.ACTION_POWER_ON, callback=first_callback)
        operation.joined_callbacks = [(second_callback, None)]

        def finish_after_cancel():
            assert operation.cancel.wait(2)
            operation.outcome = (
                bmcctld.OP_RESULT_PREEMPTED, "-", False)

        operation.thread = threading.Thread(
            target=finish_after_cancel, daemon=True)
        daemon.controller.write_operation_start(
            operation.request_id, operation.item.event_desc)
        daemon.operation_runner.current = operation
        operation.thread.start()

        daemon.operation_runner.stop()

        assert daemon.operation_runner.current is None
        first_callback.assert_called_once_with(
            False, bmcctld.OP_RESULT_PREEMPTED)
        second_callback.assert_called_once_with(
            False, bmcctld.OP_RESULT_PREEMPTED)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_PREEMPTED

        daemon.operation_runner.stop()
        first_callback.assert_called_once()
        second_callback.assert_called_once()

    @pytest.mark.parametrize("stop_before_join", [False, True])
    @pytest.mark.parametrize("outcome", [
        None, (bmcctld.OP_RESULT_SUCCESS, "-", True),
    ])
    def test_displacement_stop_preserves_live_worker_and_queue(
            self, chassis, stop_before_join, outcome):
        daemon = self._make_daemon(chassis)
        runner = daemon.operation_runner
        callback, joined_callback = MagicMock(), MagicMock()
        operation = _make_operation(
            bmcctld.ACTION_POWER_ON, callback=callback)
        operation.joined_callbacks.append((joined_callback, None))
        operation.outcome = outcome
        operation.thread = MagicMock()
        operation.thread.is_alive.return_value = True
        runner.current = operation
        runner._spawn = MagicMock()
        daemon.controller.write_operation_start(
            operation.request_id, operation.item.event_desc)
        daemon.controller.write_operation_result = MagicMock()
        successor = bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "stop", 2)
        entry = (successor.priority, 42, successor)
        daemon.action_queue.put(entry)

        def stop_during_join(timeout):
            assert operation.cancel.is_set()
            assert not daemon.stop_event.is_set(), "joined again after daemon stop"
            daemon.stop_event.set()

        operation.thread.join.side_effect = stop_during_join
        if stop_before_join:
            daemon.stop_event.set()

        assert runner.process_next(timeout=0) is True

        assert operation.thread.join.call_args_list == (
            [] if stop_before_join else [call(timeout=1)])
        assert operation.cancel.is_set()
        assert runner.current is operation
        assert operation.outcome is outcome
        assert daemon.action_queue.get_nowait() is entry
        assert daemon.action_queue.empty()
        runner._spawn.assert_not_called()
        daemon.controller.write_operation_result.assert_not_called()
        callback.assert_not_called()
        joined_callback.assert_not_called()
        state = dict(daemon.controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == "-"

    def test_displacement_waits_for_worker_with_periodic_warnings(
            self, chassis, monkeypatch):
        daemon = self._make_daemon(chassis)
        runner = daemon.operation_runner
        callback = MagicMock()
        operation = _make_operation(
            bmcctld.ACTION_POWER_ON, callback=callback)
        operation.thread = MagicMock()
        operation.thread.is_alive.return_value = True
        runner.current = operation
        runner._spawn = MagicMock()
        daemon.controller.write_operation_result = MagicMock()
        successor = bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "next", 2)
        entry = (successor.priority, 42, successor)
        daemon.action_queue.put(entry)
        now = 0
        warnings = []
        monkeypatch.setattr(bmcctld.time, "monotonic", lambda: now)
        runner.log_warning = MagicMock(side_effect=lambda message: warnings.append(now))

        def join_worker(timeout):
            nonlocal now
            assert timeout == 1
            assert operation.cancel.is_set()
            runner._spawn.assert_not_called()
            daemon.controller.write_operation_result.assert_not_called()
            callback.assert_not_called()
            now += timeout
            if now == 61:
                operation.outcome = (
                    bmcctld.OP_RESULT_PREEMPTED, bmcctld.OP_REASON_PREEMPTED, False)
                operation.thread.is_alive.return_value = False

        operation.thread.join.side_effect = join_worker

        assert runner.process_next(timeout=0) is True

        assert operation.thread.join.call_args_list == [call(timeout=1)] * 61
        assert warnings == [30, 60]
        assert runner.current is None
        assert daemon.action_queue.get_nowait() is entry
        runner._spawn.assert_not_called()
        daemon.controller.write_operation_result.assert_called_once_with(
            bmcctld.OP_RESULT_PREEMPTED, bmcctld.OP_REASON_PREEMPTED)
        callback.assert_called_once_with(False, bmcctld.OP_RESULT_PREEMPTED)

    def test_p_completed_worker_is_reaped_after_blocking_get(self, chassis):
        daemon = self._make_daemon(chassis)
        release = threading.Event()
        operation = _make_operation(bmcctld.ACTION_POWER_OFF)
        def finish_old():
            release.wait(2)
            operation.outcome = (bmcctld.OP_RESULT_SUCCESS, "-", True)
        operation.thread = threading.Thread(target=finish_old, daemon=True)
        daemon.operation_runner.current = operation
        operation.thread.start()
        recorded = []
        original_record = daemon.controller.write_operation_result
        def record(result, reason):
            recorded.append((result, reason))
            original_record(result, reason)
        daemon.controller.write_operation_result = MagicMock(side_effect=record)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)

        processor = threading.Thread(
            target=daemon.operation_runner.process_next,
            kwargs={"timeout": 2})
        processor.start()
        time.sleep(0.05)
        release.set()
        operation.thread.join(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "new", 5))
        processor.join(2)
        assert processor.is_alive() is False
        assert recorded[0] == (bmcctld.OP_RESULT_SUCCESS, "-")
        assert all(result != bmcctld.OP_RESULT_PREEMPTED
                   for result, _reason in recorded)
        assert daemon.operation_runner.current.item.event_desc == "new"
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

    def test_p_completed_before_cancel_keeps_truthful_outcome(self, chassis):
        daemon = self._make_daemon(chassis)
        outcome_set = threading.Event()
        operation = _make_operation(bmcctld.ACTION_POWER_ON, priority=5)

        def complete_then_linger():
            operation.outcome = (bmcctld.OP_RESULT_SUCCESS, "-", True)
            outcome_set.set()
            assert operation.cancel.wait(2)

        operation.thread = threading.Thread(
            target=complete_then_linger, daemon=True)
        daemon.controller.write_operation_start(
            operation.request_id, operation.item.event_desc)
        daemon.operation_runner.current = operation
        operation.thread.start()
        assert outcome_set.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "higher-priority", 2))

        daemon.operation_runner.process_next(timeout=0)

        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS
        assert daemon.operation_runner.current is None
        assert _dequeue_item(daemon.action_queue).event_desc == \
            "higher-priority"

    def test_p_displacement_preserves_entry_and_new_higher_priority_wins(
            self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        started = threading.Event()
        release = threading.Event()
        def blocked_cycle(operation, _cancel):
            started.set()
            assert release.wait(2)
            if operation.cancel.is_set():
                return bmcctld.PowerCallResult.CANCELLED
            return bmcctld.PowerCallResult.CONFIRMED
        daemon.controller.power_cycle = MagicMock(side_effect=blocked_cycle)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_CYCLE, "running-cycle", 4))
        daemon.operation_runner.process_next(timeout=0)
        assert started.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "requeued", 3))
        displacer = threading.Thread(
            target=daemon.operation_runner.process_next,
            kwargs={"timeout": 0})
        displacer.start()
        assert daemon.operation_runner.current.cancel.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "late-critical", 0))
        release.set()
        displacer.join(2)
        assert displacer.is_alive() is False
        entries = [daemon.action_queue.get_nowait(), daemon.action_queue.get_nowait()]
        assert [entry[2].event_desc for entry in entries] == [
            "late-critical", "requeued"]
        assert entries[1][1] < entries[0][1]

    def test_p_requeued_successor_runs_the_common_guard(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        started = threading.Event()

        def checkpoint_passed_power_off(operation, _factory):
            started.set()
            assert operation.cancel.wait(2)
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            return (bmcctld.OP_RESULT_PREEMPTED,
                    bmcctld.OP_REASON_PREEMPTED, False)

        daemon.graceful_shutdown.execute = MagicMock(
            side_effect=checkpoint_passed_power_off)
        daemon.controller.power_off = MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_SHUTDOWN, "running", 3))
        daemon.operation_runner.process_next(timeout=0)
        assert started.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical-successor", 0))

        daemon.operation_runner.process_next(timeout=0)
        assert daemon.operation_runner.current is None
        daemon.operation_runner.process_next(timeout=0)

        daemon.controller.power_off.assert_not_called()
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_q_guard_finalizes_state_after_cancelled_off_verify(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        verifying = threading.Event()
        set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=set_admin_state)

        def cancelled_verify(_expected, _timeout, _context, cancel=None):
            verifying.set()
            assert cancel.wait(2)
            return bmcctld.PowerCallResult.CANCELLED

        daemon.controller._verify_oper_status = MagicMock(
            side_effect=cancelled_verify)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "running", 2))
        daemon.operation_runner.process_next(timeout=0)
        assert verifying.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical-successor", 0))

        daemon.operation_runner.process_next(timeout=0)
        assert daemon.operation_runner.current is None
        daemon.operation_runner.process_next(timeout=0)

        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_DEVICE_STATUS] == \
            bmcctld.SWITCH_HOST_OFFLINE
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_s_startup_verify_is_displaceable(self, chassis):
        daemon = self._make_daemon(chassis)
        verifying = threading.Event()

        def startup_verify(operation, cancel):
            operation.stage = bmcctld.STAGE_POWER_ON_ISSUED
            verifying.set()
            assert cancel.wait(2)
            return bmcctld.PowerCallResult.CANCELLED

        daemon.controller.power_on = MagicMock(side_effect=startup_verify)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "STARTUP", 5))
        daemon.operation_runner.process_next(timeout=0)
        assert verifying.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical", 0))

        daemon.operation_runner.process_next(timeout=0)

        assert daemon.operation_runner.current is None
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_PREEMPTED

    def test_p_preempted_power_on_cannot_absorb_off_successor(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        raise_issued = threading.Event()
        admin_calls = []

        def delayed_admin_state(admin_up):
            admin_calls.append(admin_up)
            if admin_up:
                raise_issued.set()

        chassis.switch_host.set_admin_state = delayed_admin_state
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "STARTUP", 5))
        daemon.operation_runner.process_next(timeout=0)
        assert raise_issued.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical", 0))

        daemon.operation_runner.process_next(timeout=0)
        preempted = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert preempted[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_ON
        assert preempted[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_PREEMPTED
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

        assert admin_calls == [True, False]
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_p_preempted_power_cycle_cannot_absorb_off_successor(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        cycle_issued = threading.Event()
        admin_calls = []

        def delayed_cycle():
            cycle_issued.set()

        def record_admin_state(admin_up):
            admin_calls.append(admin_up)

        chassis.switch_host.do_power_cycle = delayed_cycle
        chassis.switch_host.set_admin_state = record_admin_state
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_CYCLE, "cycle", 4))
        daemon.operation_runner.process_next(timeout=0)
        assert cycle_issued.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical", 0))

        daemon.operation_runner.process_next(timeout=0)
        preempted = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert preempted[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWER_CYCLING
        assert preempted[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_PREEMPTED
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

        assert admin_calls == [False]
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_p_failed_off_after_preempted_raise_remains_retryable(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        raise_issued = threading.Event()
        admin_calls = []

        def fail_first_power_off(admin_up):
            admin_calls.append(admin_up)
            if admin_up:
                raise_issued.set()
            elif admin_calls.count(False) == 1:
                raise RuntimeError("first power-off failed")

        chassis.switch_host.set_admin_state = fail_first_power_off
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "STARTUP", 5))
        daemon.operation_runner.process_next(timeout=0)
        assert raise_issued.wait(1)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "critical", 0))

        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)
        failed = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert failed[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_OFF
        assert failed[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED

        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "retry", 2))
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

        assert admin_calls == [True, False, False]
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == bmcctld.POWER_STATE_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_SUCCESS

    def test_q_platform_calls_use_worker_while_operation_is_live(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        observations = []
        get_oper_status = chassis.switch_host.get_oper_status
        set_admin_state = chassis.switch_host.set_admin_state

        def in_flight():
            current = daemon.operation_runner.current
            return (current is not None and current.thread is not None and
                    current.thread.is_alive())

        def observed_status():
            observations.append(
                (threading.current_thread().name, in_flight()))
            return get_oper_status()

        def observed_admin_state(up):
            observations.append(
                (threading.current_thread().name, in_flight()))
            return set_admin_state(up)

        chassis.switch_host.get_oper_status = observed_status
        chassis.switch_host.set_admin_state = observed_admin_state
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "thread-check", 2))
        daemon.operation_runner.process_next(timeout=0)
        daemon.operation_runner.current.thread.join(2)
        daemon.operation_runner.process_next(timeout=0)

        live_callers = [name for name, live in observations if live]
        assert live_callers
        assert set(live_callers) == {"bmcctld-op"}

    def test_q_stop_cancels_and_joins_worker_for_five_seconds(self, chassis):
        daemon = self._make_daemon(chassis)
        callback = MagicMock()
        operation = _make_operation(
            bmcctld.ACTION_POWER_ON, callback=callback)
        operation.thread = MagicMock()
        operation.thread.is_alive.return_value = True
        daemon.operation_runner.current = operation
        daemon.controller.write_operation_result = MagicMock()

        daemon.operation_runner.stop()

        assert operation.cancel.is_set()
        operation.thread.join.assert_called_once_with(timeout=5)
        assert daemon.operation_runner.current is operation
        callback.assert_not_called()
        daemon.controller.write_operation_result.assert_not_called()

    def test_q_callback_failure_does_not_wedge_reap(self, chassis):
        daemon = self._make_daemon(chassis)
        first = MagicMock()
        third = MagicMock()
        def raising(_success, _detail):
            raise RuntimeError("callback failed")
        item = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_ON, "callbacks", 5,
            on_complete=first, rack_cmd_key="CMD_1")
        operation = bmcctld.Operation(
            item, TEST_REQUEST_ID, bmcctld.STAGE_POWER_ON_CONFIRMED)
        operation.outcome = (bmcctld.OP_RESULT_SUCCESS, "-", True)
        operation.joined_callbacks = [
            (raising, "CMD_2"), (third, "CMD_3")]
        operation.thread = MagicMock()
        operation.thread.is_alive.return_value = False
        daemon.operation_runner.current = operation
        daemon.operation_runner.log_error = MagicMock()
        assert daemon.operation_runner._reap_if_done() is True
        assert daemon.operation_runner.current is None
        first.assert_called_once_with(True, bmcctld.OP_RESULT_SUCCESS)
        third.assert_called_once_with(True, bmcctld.OP_RESULT_SUCCESS)
        daemon.operation_runner.log_error.assert_called_once_with(
            "CALLBACK_FAILED request_id={} cmd_key=CMD_2".format(
                TEST_REQUEST_ID))

    @pytest.mark.parametrize(
        "guarded, expected_request_id",
        [(True, TEST_REQUEST_ID), (False, "-")],
        ids=["guard", "refusal"],
    )
    def test_q_callback_failure_logs_path_identity(
            self, guarded, expected_request_id, chassis):
        daemon = self._make_daemon(chassis)
        daemon.operation_runner.log_error = MagicMock()
        daemon.operation_runner._new_request_id = MagicMock(
            return_value=TEST_REQUEST_ID)

        def raising(_success, _detail):
            raise RuntimeError("callback failed")

        if guarded:
            item = bmcctld.ActionItem(
                bmcctld.ACTION_POWER_OFF, "guard", 2,
                on_complete=raising, rack_cmd_key="CMD_GUARD")
        else:
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
            operation = _make_operation(bmcctld.ACTION_POWER_OFF)
            operation.thread = MagicMock()
            operation.thread.is_alive.return_value = True
            daemon.operation_runner.current = operation
            item = bmcctld.ActionItem(
                bmcctld.ACTION_POWER_ON, "refuse", 5,
                on_complete=raising, rack_cmd_key="CMD_REFUSE")
        daemon.operation_runner.enqueue(item)

        daemon.operation_runner.process_next(timeout=0)

        cmd_key = "CMD_GUARD" if guarded else "CMD_REFUSE"
        daemon.operation_runner.log_error.assert_called_once_with(
            "CALLBACK_FAILED request_id={} cmd_key={}".format(
                expected_request_id, cmd_key))

    def test_q_missing_worker_outcome_is_abandoned(self, chassis):
        daemon = self._make_daemon(chassis)
        operation = _make_operation(bmcctld.ACTION_POWER_ON)
        operation.thread = MagicMock()
        operation.thread.is_alive.return_value = False
        daemon.operation_runner.current = operation
        daemon.operation_runner.log_error = MagicMock()
        daemon.operation_runner._reap_if_done()
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == bmcctld.OP_RESULT_ABANDONED
        assert state[bmcctld.FIELD_OP_REASON] == bmcctld.OP_REASON_UNCLASSIFIED
        daemon.operation_runner.log_error.assert_called_once()

    def test_guard_does_not_treat_missing_op_result_as_in_flight(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller._update_host_state(bmcctld.SWITCH_HOST_POWERING_OFF)
        item = bmcctld.ActionItem(bmcctld.ACTION_POWER_OFF, "legacy", 2)
        assert daemon.operation_runner._guard_should_skip(item) is False


class TestWorkerExceptionRecovery:

    def _make_runner(self, chassis):
        with patch('sonic_platform.platform.Platform') as platform:
            platform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
        return daemon, daemon.operation_runner

    @pytest.mark.parametrize("action", [
        bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.ACTION_GRACEFUL_RESTART])
    @pytest.mark.parametrize("scenario", ["graceful", "rpc_failure", "deadline", "classifier_error"])
    @pytest.mark.parametrize("close_error", [False, True])
    def test_channel_close_preserves_worker_outcome(self, chassis, action, scenario, close_error):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        chassis.switch_host.set_admin_state = MagicMock(wraps=chassis.switch_host.set_admin_state)
        chassis.switch_host.do_power_cycle = MagicMock()
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(return_value=True)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(return_value=1)
        operation = _make_operation(action)
        clock = FakeClock()
        operation.cancel.wait = MagicMock(side_effect=lambda delay: clock.advance(delay) or False)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(operation.request_id), active=scenario == "deadline")
        if scenario == "rpc_failure":
            requester.poll_status.side_effect = bmcctld.GnoiRpcError("poll failed")
        if close_error:
            requester.close.side_effect = bmcctld.GnoiRpcError("close failed")
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)

        with patch('bmcctld.time.monotonic', side_effect=clock), \
                patch('bmcctld.classify_report', wraps=bmcctld.classify_report) as classify:
            if scenario == "classifier_error":
                classify.side_effect = RuntimeError("classification failed")
            runner._run_worker(operation)

        restart = action == bmcctld.ACTION_GRACEFUL_RESTART
        restart_failed = restart and scenario == "classifier_error"
        reason = {
            "graceful": "-", "rpc_failure": bmcctld.OP_REASON_RPC_FAILURE,
            "deadline": bmcctld.OP_REASON_DEADLINE,
            "classifier_error": bmcctld.OP_REASON_UNCLASSIFIED,
        }[scenario]
        result = (bmcctld.OP_RESULT_POWER_ON_FAILED if restart_failed else
                  bmcctld.OP_RESULT_SUCCESS_GRACEFUL if scenario == "graceful" else
                  bmcctld.OP_RESULT_SUCCESS_FORCED)
        assert operation.outcome == (result, reason, not restart_failed)
        assert chassis.switch_host.set_admin_state.call_args_list == (
            [call(False), call(True)] if restart and not restart_failed else [call(False)])
        chassis.switch_host.do_power_cycle.assert_not_called()
        daemon.gnoi_requester_factory.assert_called_once()
        requester.open.assert_called_once()
        requester.send_halt.assert_called_once()
        requester.poll_status.assert_called_once()
        requester.close.assert_called_once()
        assert not operation.cancel.is_set()

    @pytest.mark.parametrize(
        "command, action, expected_outcome, expected_cmd_status, expected_state",
        [
            (bmcctld.CMD_GRACEFUL_SHUT,
             bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             (bmcctld.OP_RESULT_SUCCESS_FORCED,
              bmcctld.OP_REASON_NOT_QUALIFIED, True),
             bmcctld.CMD_STATUS_DONE,
             bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN),
            (bmcctld.CMD_GRACEFUL_RESTART,
             bmcctld.ACTION_GRACEFUL_RESTART,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_NOT_QUALIFIED, False),
             bmcctld.CMD_STATUS_FAILED,
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
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(
            return_value=False)
        command_key = "bookkeeping-failure"
        daemon.event_handler._handle_rack_mgr_command(command_key, {
            bmcctld.FIELD_COMMAND: command,
            bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING,
        })

        get_power_state = daemon.controller.get_db_power_state
        get_device_status = daemon.controller.get_db_device_status

        def fail_worker_power_state_read():
            if threading.current_thread().name == "bmcctld-op":
                raise RuntimeError("power-state read failed")
            return get_power_state()

        def fail_worker_device_status_read():
            if threading.current_thread().name == "bmcctld-op":
                raise RuntimeError("device-status read failed")
            return get_device_status()

        update_host_state = daemon.controller._update_host_state

        def fail_downward_state_writes(power_state, device_status=None):
            if power_state in (
                    bmcctld.SWITCH_HOST_POWERING_OFF,
                    bmcctld.POWER_STATE_OFF):
                raise RuntimeError("power-state write failed")
            return update_host_state(power_state, device_status)

        daemon.controller.get_db_power_state = MagicMock(
            side_effect=fail_worker_power_state_read)
        daemon.controller.get_db_device_status = MagicMock(
            side_effect=fail_worker_device_status_read)
        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_downward_state_writes)
        set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=set_admin_state)

        assert runner.process_next(timeout=0) is True
        operation = runner.current
        assert operation.item.action == action
        operation.thread.join(2)
        assert operation.thread.is_alive() is False
        runner.process_next(timeout=0)

        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False)]
        assert chassis.switch_host.get_oper_status() == \
            MockModule.MODULE_STATUS_OFFLINE
        assert operation.stage == bmcctld.STAGE_POWER_OFF_CONFIRMED
        assert operation.outcome == expected_outcome
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == expected_outcome[0]
        assert state[bmcctld.FIELD_OP_REASON] == expected_outcome[1]
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == expected_state
        command_row = dict(daemon.event_handler._thread_database.table(
            "STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE).get(
                command_key)[1])
        assert command_row[bmcctld.FIELD_STATUS] == expected_cmd_status
        assert command_row[bmcctld.FIELD_RESULT] == (
            "SUCCESS" if expected_outcome[2]
            else bmcctld.OP_RESULT_POWER_ON_FAILED)
        assert command_row[bmcctld.FIELD_REQUEST_ID] == operation.request_id

    def test_graceful_restart_bookkeeping_failure_preserves_leg_reason(
            self, chassis):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        daemon.policy_reader.get_graceful_shutdown_timeout = MagicMock(
            return_value=10)
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(
            return_value=True)
        requester = MagicMock()
        requester.poll_status.return_value = _report(
            "done [bmc-req:{}]".format(TEST_REQUEST_ID))
        daemon.gnoi_requester_factory = MagicMock(return_value=requester)

        update_host_state = daemon.controller._update_host_state

        def fail_downward_state_writes(power_state, device_status=None):
            if power_state in (
                    bmcctld.SWITCH_HOST_POWERING_OFF,
                    bmcctld.POWER_STATE_OFF):
                raise RuntimeError("power-state write failed")
            return update_host_state(power_state, device_status)

        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_downward_state_writes)
        set_admin_state = chassis.switch_host.set_admin_state
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=set_admin_state)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_RESTART)

        runner._run_worker(operation)

        assert operation.leg_graceful is True
        assert operation.leg_reason == "-"
        assert chassis.switch_host.set_admin_state.call_args_list == [
            call(False)]
        assert operation.stage == bmcctld.STAGE_POWER_OFF_CONFIRMED
        assert operation.outcome == (
            bmcctld.OP_RESULT_POWER_ON_FAILED, "-", False)

    @pytest.mark.parametrize(
        "action, expected_reason",
        [
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             bmcctld.OP_REASON_NOT_QUALIFIED),
            (bmcctld.ACTION_GRACEFUL_RESTART,
             bmcctld.OP_REASON_NOT_QUALIFIED),
            (bmcctld.ACTION_POWER_OFF, "-"),
        ],
    )
    def test_downward_platform_exception_is_not_retried(
            self, action, expected_reason, chassis):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(
            return_value=False)
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=RuntimeError("platform call failed"))
        operation = _make_operation(action)

        runner._run_worker(operation)

        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        assert operation.stage == bmcctld.STAGE_POWER_OFF_ISSUED
        assert operation.outcome == (
            bmcctld.OP_RESULT_POWER_OFF_FAILED, expected_reason, False)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_OFF

    @pytest.mark.parametrize(
        "failure_point",
        ["prior-state-read", "transitional-write", "critical-state-read"],
    )
    def test_power_on_bookkeeping_failure_remains_fail_closed(
            self, failure_point, chassis):
        daemon, runner = self._make_runner(chassis)
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
            daemon.controller.critical_event_checker.has_any_critical_event = \
                MagicMock(side_effect=RuntimeError(
                    "critical-state read failed"))

        operation = _make_operation(bmcctld.ACTION_POWER_ON)
        runner._run_worker(operation)

        chassis.switch_host.set_admin_state.assert_not_called()
        assert operation.stage == bmcctld.STAGE_POWER_ON_PENDING
        assert operation.outcome == (
            bmcctld.OP_RESULT_POWER_ON_FAILED, "-", False)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
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
            (bmcctld.CMD_GRACEFUL_RESTART,
             bmcctld.ACTION_GRACEFUL_RESTART,
             bmcctld.OP_REASON_NOT_QUALIFIED,
             bmcctld.SWITCH_HOST_GRACEFUL_SHUTTING_DOWN),
            (bmcctld.CMD_POWER_OFF,
             bmcctld.ACTION_POWER_OFF, "-", bmcctld.POWER_STATE_ON),
        ],
    )
    def test_bookkeeping_and_platform_failures_issue_one_downward_call(
            self, command, action, expected_reason, expected_state, chassis):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.controller._update_host_state(
            bmcctld.POWER_STATE_ON, bmcctld.SWITCH_HOST_ONLINE)
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(
            return_value=False)
        command_key = "bookkeeping-and-platform-failure"
        daemon.event_handler._handle_rack_mgr_command(command_key, {
            bmcctld.FIELD_COMMAND: command,
            bmcctld.FIELD_STATUS: bmcctld.CMD_STATUS_PENDING,
        })

        get_power_state = daemon.controller.get_db_power_state

        def fail_worker_power_state_read():
            if threading.current_thread().name == "bmcctld-op":
                raise RuntimeError("power-state read failed")
            return get_power_state()

        update_host_state = daemon.controller._update_host_state

        def fail_downward_state_writes(power_state, device_status=None):
            if power_state in (
                    bmcctld.SWITCH_HOST_POWERING_OFF,
                    bmcctld.POWER_STATE_OFF):
                raise RuntimeError("power-state write failed")
            return update_host_state(power_state, device_status)

        daemon.controller.get_db_power_state = MagicMock(
            side_effect=fail_worker_power_state_read)
        daemon.controller.get_db_device_status = MagicMock(
            side_effect=RuntimeError("device-status read failed"))
        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_downward_state_writes)
        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=RuntimeError("platform call failed"))

        assert runner.process_next(timeout=0) is True
        operation = runner.current
        assert operation.item.action == action
        operation.thread.join(2)
        assert operation.thread.is_alive() is False
        runner.process_next(timeout=0)

        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        assert operation.stage == bmcctld.STAGE_POWER_OFF_ISSUED
        assert operation.outcome == (
            bmcctld.OP_RESULT_POWER_OFF_FAILED, expected_reason, False)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED
        assert state[bmcctld.FIELD_OP_REASON] == expected_reason
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == expected_state
        command_row = dict(daemon.event_handler._thread_database.table(
            "STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE).get(
                command_key)[1])
        assert command_row[bmcctld.FIELD_STATUS] == \
            bmcctld.CMD_STATUS_FAILED
        assert command_row[bmcctld.FIELD_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED
        assert command_row[bmcctld.FIELD_REQUEST_ID] == operation.request_id

    @pytest.mark.parametrize(
        "action, stage, power_result, expected, expected_power_calls",
        [
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.STAGE_HANDSHAKE,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_SUCCESS_FORCED,
              bmcctld.OP_REASON_UNCLASSIFIED, True), 1),
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.STAGE_HANDSHAKE,
             bmcctld.PowerCallResult.NOT_CONFIRMED,
             (bmcctld.OP_RESULT_POWER_OFF_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 1),
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN, bmcctld.STAGE_HANDSHAKE,
             bmcctld.PowerCallResult.CANCELLED,
             (bmcctld.OP_RESULT_PREEMPTED,
              bmcctld.OP_REASON_PREEMPTED, False), 1),
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             bmcctld.STAGE_POWER_OFF_ISSUED,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_POWER_OFF_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 0),
            (bmcctld.ACTION_POWER_OFF, bmcctld.STAGE_POWER_OFF_PENDING,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_SUCCESS, "-", True), 1),
            (bmcctld.ACTION_POWER_OFF, bmcctld.STAGE_POWER_OFF_PENDING,
             bmcctld.PowerCallResult.NOT_CONFIRMED,
             (bmcctld.OP_RESULT_POWER_OFF_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 1),
            (bmcctld.ACTION_POWER_OFF, bmcctld.STAGE_POWER_OFF_PENDING,
             bmcctld.PowerCallResult.CANCELLED,
             (bmcctld.OP_RESULT_PREEMPTED,
              bmcctld.OP_REASON_PREEMPTED, False), 1),
            (bmcctld.ACTION_POWER_OFF, bmcctld.STAGE_POWER_OFF_ISSUED,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_POWER_OFF_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 0),
            (bmcctld.ACTION_POWER_CYCLE, bmcctld.STAGE_POWER_CYCLE_PENDING,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 0),
            (bmcctld.ACTION_POWER_CYCLE, bmcctld.STAGE_POWER_CYCLE_ISSUED,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 0),
            (bmcctld.ACTION_POWER_ON, bmcctld.STAGE_POWER_ON_PENDING,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_POWER_ON_FAILED, "-", False), 0),
            (bmcctld.ACTION_POWER_ON, bmcctld.STAGE_POWER_ON_ISSUED,
             bmcctld.PowerCallResult.CONFIRMED,
             (bmcctld.OP_RESULT_POWER_ON_FAILED, "-", False), 0),
        ],
    )
    def test_q_stage_recovery_never_reissues_ambiguous_calls(
            self, action, stage, power_result, expected,
            expected_power_calls, chassis):
        daemon, runner = self._make_runner(chassis)
        operation = _make_operation(action)
        operation.stage = stage
        daemon.controller.power_off = MagicMock(return_value=power_result)
        daemon.controller.power_on = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        daemon.controller.power_cycle = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        assert runner._recover_worker_exception(operation) == expected
        assert daemon.controller.power_off.call_count == expected_power_calls
        daemon.controller.power_on.assert_not_called()
        daemon.controller.power_cycle.assert_not_called()

    @pytest.mark.parametrize(
        "action, stage, leg_graceful, leg_reason, expected",
        [
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             bmcctld.STAGE_POWER_OFF_CONFIRMED, True, None,
             (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)),
            (bmcctld.ACTION_GRACEFUL_SHUTDOWN,
             bmcctld.STAGE_POWER_OFF_CONFIRMED, False,
             bmcctld.OP_REASON_DEADLINE,
             (bmcctld.OP_RESULT_SUCCESS_FORCED,
              bmcctld.OP_REASON_DEADLINE, True)),
            (bmcctld.ACTION_GRACEFUL_RESTART,
             bmcctld.STAGE_POWER_ON_CONFIRMED, True, None,
             (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)),
            (bmcctld.ACTION_GRACEFUL_RESTART,
             bmcctld.STAGE_POWER_ON_CONFIRMED, False,
             bmcctld.OP_REASON_CHECK_FAILED,
             (bmcctld.OP_RESULT_SUCCESS_FORCED,
              bmcctld.OP_REASON_CHECK_FAILED, True)),
            (bmcctld.ACTION_POWER_OFF,
             bmcctld.STAGE_POWER_OFF_CONFIRMED, False, None,
             (bmcctld.OP_RESULT_SUCCESS, "-", True)),
            (bmcctld.ACTION_POWER_ON,
             bmcctld.STAGE_POWER_ON_CONFIRMED, False, None,
             (bmcctld.OP_RESULT_SUCCESS, "-", True)),
            (bmcctld.ACTION_POWER_CYCLE,
             bmcctld.STAGE_POWER_CYCLE_CONFIRMED, False, None,
             (bmcctld.OP_RESULT_SUCCESS, "-", True)),
        ],
    )
    def test_q_confirmed_stage_never_becomes_false_failure(
            self, action, stage, leg_graceful, leg_reason, expected,
            chassis):
        daemon, runner = self._make_runner(chassis)
        operation = _make_operation(action)
        operation.stage = stage
        operation.leg_graceful = leg_graceful
        operation.leg_reason = leg_reason
        daemon.controller.power_off = MagicMock()
        daemon.controller.power_on = MagicMock()
        daemon.controller.power_cycle = MagicMock()
        assert runner._recover_worker_exception(operation) == expected
        daemon.controller.power_off.assert_not_called()
        daemon.controller.power_on.assert_not_called()
        daemon.controller.power_cycle.assert_not_called()

    @pytest.mark.parametrize(
        "stage, power_result, leg_reason, expected, expected_power_calls",
        [
            (bmcctld.STAGE_HANDSHAKE,
             bmcctld.PowerCallResult.CONFIRMED, None,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 1),
            (bmcctld.STAGE_HANDSHAKE,
             bmcctld.PowerCallResult.NOT_CONFIRMED, None,
             (bmcctld.OP_RESULT_POWER_OFF_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 1),
            (bmcctld.STAGE_POWER_OFF_ISSUED,
             bmcctld.PowerCallResult.CONFIRMED, None,
             (bmcctld.OP_RESULT_POWER_OFF_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 0),
            (bmcctld.STAGE_POWER_OFF_CONFIRMED,
             bmcctld.PowerCallResult.CONFIRMED, None,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_UNCLASSIFIED, False), 0),
            (bmcctld.STAGE_PAUSE,
             bmcctld.PowerCallResult.CONFIRMED,
             bmcctld.OP_REASON_DEADLINE,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_DEADLINE, False), 0),
            (bmcctld.STAGE_POWER_ON_ISSUED,
             bmcctld.PowerCallResult.CONFIRMED,
             bmcctld.OP_REASON_CHECK_FAILED,
             (bmcctld.OP_RESULT_POWER_ON_FAILED,
              bmcctld.OP_REASON_CHECK_FAILED, False), 0),
        ],
    )
    def test_q_restart_stage_recovery_never_adds_a_raise(
            self, stage, power_result, leg_reason, expected,
            expected_power_calls, chassis):
        daemon, runner = self._make_runner(chassis)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_RESTART)
        operation.stage = stage
        operation.leg_reason = leg_reason
        daemon.controller.power_off = MagicMock(return_value=power_result)
        daemon.controller.power_on = MagicMock()
        daemon.controller.power_cycle = MagicMock()

        assert runner._recover_worker_exception(operation) == expected
        assert daemon.controller.power_off.call_count == expected_power_calls
        daemon.controller.power_on.assert_not_called()
        daemon.controller.power_cycle.assert_not_called()

    @pytest.mark.parametrize(
        "action, initial_status, final_state, confirmed_stage",
        [
            (bmcctld.ACTION_POWER_OFF, MockModule.MODULE_STATUS_ONLINE,
             bmcctld.POWER_STATE_OFF, bmcctld.STAGE_POWER_OFF_CONFIRMED),
            (bmcctld.ACTION_POWER_ON, MockModule.MODULE_STATUS_OFFLINE,
             bmcctld.POWER_STATE_ON, bmcctld.STAGE_POWER_ON_CONFIRMED),
            (bmcctld.ACTION_POWER_CYCLE, MockModule.MODULE_STATUS_ONLINE,
             bmcctld.POWER_STATE_CYCLE,
             bmcctld.STAGE_POWER_CYCLE_CONFIRMED),
        ],
    )
    def test_q_confirmed_wrapper_bookkeeping_failure_reaches_recovery(
            self, action, initial_status, final_state, confirmed_stage,
            chassis):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(initial_status)
        operation = _make_operation(action)
        update_state = daemon.controller._update_host_state

        def fail_final_state(power_state, device_status=None):
            if power_state == final_state:
                raise RuntimeError("final state write failed")
            return update_state(power_state, device_status)

        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_final_state)
        runner._run_worker(operation)

        assert operation.stage == confirmed_stage
        assert operation.outcome == (
            bmcctld.OP_RESULT_SUCCESS, "-", True)

    def test_graceful_shutdown_transitional_write_failure_preserves_reason(
            self, chassis):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        daemon.graceful_shutdown._is_graceful_qualified = MagicMock(
            return_value=False)

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

        runner._run_worker(operation)

        assert failed_once[0] is True
        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        assert operation.stage == bmcctld.STAGE_POWER_OFF_CONFIRMED
        assert operation.outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_NOT_QUALIFIED,
            True,
        )

    def test_q_recovery_power_off_always_leaves_an_outcome(self, chassis):
        daemon, runner = self._make_runner(chassis)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        daemon.graceful_shutdown.execute = MagicMock(
            side_effect=RuntimeError("operation body failed"))
        update_state = daemon.controller._update_host_state

        def fail_final_state(power_state, device_status=None):
            if power_state == bmcctld.POWER_STATE_OFF:
                raise RuntimeError("final state write failed")
            return update_state(power_state, device_status)

        daemon.controller._update_host_state = MagicMock(
            side_effect=fail_final_state)
        runner._run_worker(operation)

        assert chassis.switch_host.get_admin_state() is False
        assert operation.stage == bmcctld.STAGE_POWER_OFF_CONFIRMED
        assert operation.outcome == (
            bmcctld.OP_RESULT_SUCCESS_FORCED,
            bmcctld.OP_REASON_UNCLASSIFIED,
            True,
        )

    def test_q_cancel_during_recovery_fallback_prevents_retry(self, chassis):
        daemon, runner = self._make_runner(chassis)
        operation = _make_operation(bmcctld.ACTION_POWER_OFF)

        def cancel_then_raise(_operation, cancel):
            cancel.set()
            raise RuntimeError("fallback call failed after cancellation")

        daemon.controller.power_off = MagicMock(side_effect=cancel_then_raise)
        assert runner._recover_worker_exception(operation) == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        daemon.controller.power_off.assert_called_once_with(
            operation, operation.cancel)

    def test_q_cancel_wins_before_exception_recovery_call(self, chassis):
        daemon, runner = self._make_runner(chassis)
        operation = _make_operation(bmcctld.ACTION_GRACEFUL_SHUTDOWN)
        operation.cancel.set()
        daemon.controller.power_off = MagicMock()
        assert runner._recover_worker_exception(operation) == (
            bmcctld.OP_RESULT_PREEMPTED,
            bmcctld.OP_REASON_PREEMPTED,
            False,
        )
        daemon.controller.power_off.assert_not_called()


# --------------------------------------------------------------------------
# Tests: BmcctldDaemon - initial power-on sequence
# --------------------------------------------------------------------------

class TestBmcctldDaemonInitialSequence:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
            # Default to admin_status=up so tests exercise the power-on logic
            daemon.policy_reader.get_switch_host_admin_status = MagicMock(return_value=bmcctld.ADMIN_UP)
            daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        return daemon

    def test_skips_power_on_when_admin_status_down(self, chassis):
        """When admin_status=down (default), Switch-Host must not be powered on at startup."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_switch_host_admin_status = MagicMock(return_value=bmcctld.ADMIN_DOWN)
        daemon.controller.power_on = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_skips_power_on_when_admin_status_not_set(self, chassis):
        """When no CHASSIS_MODULE entry exists, default is down — Switch-Host stays off."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        # Simulate missing entry — get_switch_host_admin_status returns ADMIN_DOWN
        daemon.policy_reader.get_switch_host_admin_status = MagicMock(return_value=bmcctld.ADMIN_DOWN)
        daemon.controller.power_on = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_powers_on_when_no_leak_and_host_offline(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon._initial_power_on_sequence()
        item = _dequeue_item(daemon.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.event_desc == "STARTUP"

    def test_skips_power_on_if_critical_leak_present(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
        daemon.controller.power_on = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_refreshes_state_when_already_online(self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock()
        daemon.controller.init_host_state = MagicMock()
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()
        daemon.controller.init_host_state.assert_called_once()

    def test_boot_delay_skipped_when_system_uptime_exceeds_delay(self, chassis):
        """If system uptime already exceeds power_on_delay, boot delay is skipped (no fresh timer)."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        # Pretend the system has been up for 10 minutes — bmcctld restart mid-life
        # must not re-arm the full 60s delay.
        with patch('time.clock_gettime', return_value=600):
            t0 = time.monotonic()
            daemon._initial_power_on_sequence()
            elapsed = time.monotonic() - t0
        assert elapsed < 1.0, "boot delay should have been skipped (elapsed={:.2f}s)".format(elapsed)
        item = _dequeue_item(daemon.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON

    def test_stop_event_during_boot_delay_skips_sequence(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock()
        daemon.stop_event.set()  # Signal stop before delay expires
        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_boot_delay_processes_queued_actions(self, chassis):
        """Action items queued by the event thread during the boot delay are executed."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        # Use a small non-zero delay so the queue-drain loop runs at least once
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=1)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_off = MagicMock(
            return_value=bmcctld.PowerCallResult.CONFIRMED)
        # Simulate a POWER_OFF arriving from Rack Manager during boot delay
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "RACK_MGR_BOOT_DELAY", 2))
        # Force system uptime to 0 so the full configured delay applies.
        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()
        # The POWER_OFF must have been consumed from the queue during the delay
        assert daemon.action_queue.empty()
        daemon.controller.power_off.assert_called_once()

    def test_q_boot_delay_hands_off_live_worker_without_startup_tail(
            self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=1)
        daemon._rack_mgr_power_cmd_executed = MagicMock(return_value=False)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(
            return_value=False)
        daemon.controller.init_host_state = MagicMock()
        verifying = threading.Event()
        release = threading.Event()

        def unconfirmed_power_off(up):
            assert up is False
            chassis.switch_host._admin_state = up

        def blocked_verify(_expected, _timeout, _context, cancel=None):
            verifying.set()
            assert release.wait(3)
            return bmcctld.PowerCallResult.NOT_CONFIRMED

        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=unconfirmed_power_off)
        daemon.controller._verify_oper_status = MagicMock(
            side_effect=blocked_verify)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "boot-delay-worker", 2))

        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()

        assert verifying.is_set()
        assert daemon.operation_runner.current is not None
        assert daemon.operation_runner.current.thread.is_alive()
        assert daemon.action_queue.empty()
        daemon._rack_mgr_power_cmd_executed.assert_not_called()
        daemon.critical_event_checker.has_any_critical_event.assert_not_called()
        daemon.controller.init_host_state.assert_not_called()
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == "-"

        release.set()
        daemon.operation_runner.current.thread.join(1)
        daemon.operation_runner.process_next(timeout=0)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED

    def test_q_boot_delay_hands_off_reaped_worker_without_startup_tail(
            self, chassis):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=2)
        daemon._rack_mgr_power_cmd_executed = MagicMock(return_value=False)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(
            return_value=False)
        daemon.controller.init_host_state = MagicMock()

        def unconfirmed_power_off(up):
            assert up is False
            chassis.switch_host._admin_state = up

        chassis.switch_host.set_admin_state = MagicMock(
            side_effect=unconfirmed_power_off)
        daemon.controller._verify_oper_status = MagicMock(
            return_value=bmcctld.PowerCallResult.NOT_CONFIRMED)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "boot-delay-reaped-worker", 2))

        with patch('time.clock_gettime', return_value=0):
            daemon._initial_power_on_sequence()

        assert daemon.operation_runner.current is None
        assert daemon.action_queue.empty()
        daemon._rack_mgr_power_cmd_executed.assert_not_called()
        daemon.critical_event_checker.has_any_critical_event.assert_not_called()
        daemon.controller.init_host_state.assert_not_called()
        chassis.switch_host.set_admin_state.assert_called_once_with(False)
        state = dict(daemon.controller.host_state_table.get(
            bmcctld.HOST_STATE_KEY)[1])
        assert state[bmcctld.FIELD_DEVICE_POWER_STATE] == \
            bmcctld.SWITCH_HOST_POWERING_OFF
        assert state[bmcctld.FIELD_OP_RESULT] == \
            bmcctld.OP_RESULT_POWER_OFF_FAILED

        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        retry = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "retry-after-unconfirmed-off", 2)
        assert daemon.operation_runner._guard_should_skip(retry) is False

    def test_rack_mgr_power_off_during_boot_delay_skips_auto_power_on(self, chassis):
        """If Rack Manager POWER_OFF cmd executed during boot delay, automatic power-on is skipped."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._rack_mgr_power_cmd_executed = MagicMock(return_value=True)
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_rack_mgr_power_on_during_boot_delay_skips_auto_power_on(self, chassis):
        """If Rack Manager POWER_ON cmd executed during boot delay, automatic power-on is skipped."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=0)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._rack_mgr_power_cmd_executed = MagicMock(return_value=True)
        daemon._initial_power_on_sequence()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()

    def test_rack_mgr_power_cmd_executed_detects_done_power_off(self, chassis):
        """_rack_mgr_power_cmd_executed returns True when POWER_OFF is DONE in RACK_MANAGER_COMMAND."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_OFF),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_DONE),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    def test_rack_mgr_power_cmd_executed_detects_in_progress_power_on(self, chassis):
        """_rack_mgr_power_cmd_executed returns True when POWER_ON is IN_PROGRESS."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_ON),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_IN_PROGRESS),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    def test_rack_mgr_power_cmd_executed_ignores_power_cycle(self, chassis):
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_CYCLE),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_DONE),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is False

    def test_rack_mgr_power_cmd_executed_detects_graceful_shut(self, chassis):
        """_rack_mgr_power_cmd_executed returns True when GRACEFUL_SHUT is DONE."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_GRACEFUL_SHUT),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_DONE),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    @pytest.mark.parametrize("status", [
        bmcctld.CMD_STATUS_IN_PROGRESS,
        bmcctld.CMD_STATUS_DONE,
    ])
    def test_rack_mgr_power_cmd_executed_detects_graceful_restart(
            self, chassis, status):
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_GRACEFUL_RESTART),
            (bmcctld.FIELD_STATUS, status),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is True

    def test_rack_mgr_power_cmd_executed_ignores_pending(self, chassis):
        """_rack_mgr_power_cmd_executed returns False when command is still PENDING."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        tbl.set("CMD_1", FieldValuePairs([
            (bmcctld.FIELD_COMMAND, bmcctld.CMD_POWER_OFF),
            (bmcctld.FIELD_STATUS, bmcctld.CMD_STATUS_PENDING),
        ]))
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is False

    def test_rack_mgr_power_cmd_executed_empty_table(self, chassis):
        """_rack_mgr_power_cmd_executed returns False when no commands exist."""
        daemon = self._make_daemon(chassis)
        tbl = Table(daemon._thread_database.connection("STATE_DB"),
                    bmcctld.RACK_MANAGER_COMMAND_TABLE)
        with patch('bmcctld.swsscommon.Table', return_value=tbl):
            assert daemon._rack_mgr_power_cmd_executed() is False

    def test_cold_boot_applies_power_on_delay(self, chassis):
        """On a FULL POWER LOSS (cold boot), SWITCH_HOST_POWER_ON_DELAY must be applied."""
        chassis.set_reboot_cause(bmcctld.ChassisBase.REBOOT_CAUSE_POWER_LOSS)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=5)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock(return_value=True)
        # Bound the sequence by setting stop_event after one queue.get cycle
        daemon.stop_event.set()
        daemon._initial_power_on_sequence()
        daemon.policy_reader.get_power_on_delay.assert_called_once()

    def test_warm_boot_skips_power_on_delay(self, chassis):
        """On warm/fast/soft reboot (non-POWER_LOSS), the boot delay must be skipped."""
        chassis.set_reboot_cause(chassis.REBOOT_CAUSE_NON_HARDWARE)
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=60)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._initial_power_on_sequence()
        # Boot delay is skipped → get_power_on_delay must NOT be called
        daemon.policy_reader.get_power_on_delay.assert_not_called()
        item = _dequeue_item(daemon.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON

    def test_reboot_cause_exception_falls_back_to_cold_boot(self, chassis):
        """If chassis.get_reboot_cause() raises, fall back to cold-boot behavior (apply delay)."""
        chassis.get_reboot_cause = MagicMock(side_effect=RuntimeError("platform API not available"))
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=5)
        daemon.critical_event_checker.has_any_critical_event = MagicMock(return_value=False)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon.stop_event.set()
        daemon._initial_power_on_sequence()
        daemon.policy_reader.get_power_on_delay.assert_called_once()


class TestBmcctldDaemonRun:

    def _make_daemon(self, chassis):
        with patch('sonic_platform.platform.Platform') as MockPlatform:
            MockPlatform.return_value.get_chassis.return_value = chassis
            daemon = bmcctld.BmcctldDaemon(bmcctld.SYSLOG_IDENTIFIER)
            daemon.policy_reader.get_switch_host_admin_status = MagicMock(
                return_value=bmcctld.ADMIN_UP)
        return daemon

    @pytest.fixture
    def lifecycle(self, chassis, monkeypatch):
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        chassis.switch_host.set_admin_state = MagicMock()
        chassis.switch_host.do_power_cycle = MagicMock()
        daemon.controller.write_operation_result = MagicMock(
            wraps=daemon.controller.write_operation_result)
        daemon._run_action_loop = MagicMock(wraps=daemon._run_action_loop)
        event_thread = MagicMock(name="event_thread")
        event_thread.start.side_effect = daemon.event_handler._subscription_ready.set
        worker = MagicMock(name="operation_thread")
        worker.is_alive.return_value = True
        worker.start.side_effect = daemon.stop_event.set

        def join_worker(timeout):
            assert daemon.operation_runner.current.cancel.is_set()

        def join_events(timeout):
            assert daemon.stop_event.is_set()
            operation = daemon.operation_runner.current
            if operation is not None and worker.is_alive():
                assert operation.cancel.is_set()

        worker.join.side_effect = join_worker
        event_thread.join.side_effect = join_events

        def make_thread(*, target, name, daemon, args=()):
            if name == "bmcctld-events":
                return event_thread
            assert name == "bmcctld-op"
            return worker

        monkeypatch.setattr(bmcctld.threading, "Thread", make_thread)
        monkeypatch.setattr(bmcctld, "BmcctldDaemon", MagicMock(return_value=daemon))
        yield daemon, event_thread, worker
        chassis.switch_host.set_admin_state.assert_not_called()
        chassis.switch_host.do_power_cycle.assert_not_called()

    @pytest.mark.parametrize("completion", ["alive", "before_stop", "worker_join", "event_join"])
    def test_main_shutdown_joins_worker_once(self, lifecycle, completion):
        daemon, event_thread, worker = lifecycle
        first_callback, joined_callback = MagicMock(), MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "test", 4,
            on_complete=first_callback))
        operation = None

        def finish():
            worker.is_alive.return_value = False
            operation.outcome = (bmcctld.OP_RESULT_PREEMPTED, "-", False)

        def start():
            nonlocal operation
            operation = daemon.operation_runner.current
            operation.joined_callbacks.append((joined_callback, None))
            daemon.stop_event.set()
            if completion == "before_stop":
                finish()

        worker.start.side_effect = start
        if completion == "worker_join":
            def finish_on_worker_join(timeout):
                assert operation.cancel.is_set()
                finish()
            worker.join.side_effect = finish_on_worker_join
        elif completion == "event_join":
            def finish_on_event_join(timeout):
                assert daemon.stop_event.is_set()
                assert operation.cancel.is_set()
                finish()
            event_thread.join.side_effect = finish_on_event_join

        assert bmcctld.main() == bmcctld.exit_code

        worker.start.assert_called_once()
        assert worker.join.call_args_list == (
            [] if completion == "before_stop" else [call(timeout=5)])
        event_thread.join.assert_called_once_with(timeout=5)
        assert daemon.action_queue.empty()
        if completion in ("before_stop", "worker_join"):
            assert daemon.operation_runner.current is None
            daemon.controller.write_operation_result.assert_called_once_with(
                bmcctld.OP_RESULT_PREEMPTED, "-")
            for callback in (first_callback, joined_callback):
                callback.assert_called_once_with(False, bmcctld.OP_RESULT_PREEMPTED)
            daemon.operation_runner.stop()
            daemon.controller.write_operation_result.assert_called_once()
            first_callback.assert_called_once()
            joined_callback.assert_called_once()
        else:
            assert daemon.operation_runner.current is operation
            daemon.controller.write_operation_result.assert_not_called()
            first_callback.assert_not_called()
            joined_callback.assert_not_called()
            state = dict(daemon.controller.host_state_table.get(bmcctld.HOST_STATE_KEY)[1])
            assert state[bmcctld.FIELD_OP_RESULT] == "-"

    @pytest.mark.parametrize("startup_drain", [False, True])
    @pytest.mark.parametrize("completion", ["alive", "displacement", "cleanup"])
    def test_displacement_stop_reaches_run_cleanup(
            self, lifecycle, chassis, monkeypatch, startup_drain, completion):
        daemon, event_thread, worker = lifecycle
        callback, joined_callback, successor_callback = MagicMock(), MagicMock(), MagicMock()
        runner = daemon.operation_runner
        runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "running", 4, on_complete=callback))
        successor = bmcctld.ActionItem(
            bmcctld.ACTION_POWER_OFF, "next", 2, on_complete=successor_callback)
        entry = (successor.priority, 42, successor)
        operation = None
        if startup_drain:
            chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
            chassis.set_liquid_cooled(True)
            chassis.set_reboot_cause(bmcctld.ChassisBase.REBOOT_CAUSE_POWER_LOSS)
            daemon.policy_reader.get_power_on_delay = MagicMock(return_value=20)
            monkeypatch.setattr(bmcctld.time, "clock_gettime", lambda clock: 10)
            monkeypatch.setattr(bmcctld.time, "monotonic", lambda: 0)

        def start_worker():
            nonlocal operation
            operation = runner.current
            operation.joined_callbacks.append((joined_callback, None))
            if completion != "alive":
                operation.outcome = (bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-", True)
            daemon.action_queue.put(entry)

        def join_worker(timeout):
            assert operation.cancel.is_set()
            if not daemon.stop_event.is_set():
                assert timeout == 1
                daemon.stop_event.set()
                if completion == "displacement":
                    worker.is_alive.return_value = False
            else:
                assert timeout == 5
                if completion == "cleanup":
                    worker.is_alive.return_value = False

        worker.start.side_effect = start_worker
        worker.join.side_effect = join_worker

        assert bmcctld.main() == bmcctld.exit_code

        worker.start.assert_called_once()
        assert worker.join.call_args_list == (
            [call(timeout=1)] if completion == "displacement" else
            [call(timeout=1), call(timeout=5)])
        event_thread.join.assert_called_once_with(timeout=5)
        assert daemon._run_action_loop.call_count == (0 if startup_drain else 1)
        assert daemon.action_queue.get_nowait() is entry
        assert daemon.action_queue.empty()
        successor_callback.assert_not_called()
        if completion == "alive":
            assert runner.current is operation
            assert operation.outcome is None
            daemon.controller.write_operation_result.assert_not_called()
            callback.assert_not_called()
            joined_callback.assert_not_called()
        else:
            assert runner.current is None
            daemon.controller.write_operation_result.assert_called_once_with(
                bmcctld.OP_RESULT_SUCCESS_GRACEFUL, "-")
            callback.assert_called_once_with(True, bmcctld.OP_RESULT_SUCCESS_GRACEFUL)
            joined_callback.assert_called_once_with(True, bmcctld.OP_RESULT_SUCCESS_GRACEFUL)

    @pytest.mark.parametrize("fail_after_admission", [False, True])
    def test_startup_drain_cleans_up_before_event_join(
            self, lifecycle, chassis, monkeypatch, fail_after_admission):
        daemon, event_thread, worker = lifecycle
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        chassis.set_liquid_cooled(True)
        chassis.set_reboot_cause(bmcctld.ChassisBase.REBOOT_CAUSE_POWER_LOSS)
        daemon.policy_reader.get_power_on_delay = MagicMock(return_value=20)
        monkeypatch.setattr(bmcctld.time, "clock_gettime", lambda clock: 10)
        monkeypatch.setattr(bmcctld.time, "monotonic", lambda: 0)
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "test", 4))
        process_next = daemon.operation_runner.process_next

        def drain(timeout):
            assert process_next(timeout=0)
            worker.start.assert_called_once()
            if fail_after_admission:
                raise RuntimeError("drain failed")
            return True

        daemon.operation_runner.process_next = MagicMock(side_effect=drain)
        if fail_after_admission:
            with pytest.raises(RuntimeError, match="drain failed"):
                bmcctld.main()
        else:
            assert bmcctld.main() == bmcctld.exit_code
        daemon.policy_reader.get_power_on_delay.assert_called_once()
        daemon.operation_runner.process_next.assert_called_once_with(timeout=1.0)
        daemon._run_action_loop.assert_not_called()
        worker.join.assert_called_once_with(timeout=5)
        event_thread.join.assert_called_once_with(timeout=5)
        assert daemon.operation_runner.current.cancel.is_set()
        assert daemon.action_queue.empty()
        daemon.controller.write_operation_result.assert_not_called()

    def test_action_loop_error_after_admission_still_cleans_up(self, lifecycle):
        daemon, event_thread, worker = lifecycle
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "test", 4))
        process_next = daemon.operation_runner.process_next

        def admit_then_fail(timeout):
            assert process_next(timeout=0)
            worker.start.assert_called_once()
            raise RuntimeError("action loop failed")

        daemon.operation_runner.process_next = MagicMock(side_effect=admit_then_fail)
        with pytest.raises(RuntimeError, match="action loop failed"):
            bmcctld.main()
        daemon._run_action_loop.assert_called_once()
        worker.join.assert_called_once_with(timeout=5)
        event_thread.join.assert_called_once_with(timeout=5)
        assert daemon.stop_event.is_set()
        assert daemon.operation_runner.current.cancel.is_set()
        assert daemon.action_queue.empty()
        daemon.controller.write_operation_result.assert_not_called()

    def test_stop_time_record_failure_still_joins_event_thread(self, lifecycle):
        daemon, event_thread, worker = lifecycle
        callback, joined_callback = MagicMock(), MagicMock()
        daemon.operation_runner.enqueue(bmcctld.ActionItem(
            bmcctld.ACTION_GRACEFUL_RESTART, "test", 4, on_complete=callback))

        def finish(timeout):
            operation = daemon.operation_runner.current
            assert operation.cancel.is_set()
            operation.joined_callbacks.append((joined_callback, None))
            operation.outcome = (bmcctld.OP_RESULT_PREEMPTED, "-", False)
            worker.is_alive.return_value = False

        worker.join.side_effect = finish
        daemon.controller.write_operation_result.side_effect = RuntimeError("record failed")
        with pytest.raises(RuntimeError, match="record failed"):
            bmcctld.main()
        worker.join.assert_called_once_with(timeout=5)
        event_thread.join.assert_called_once_with(timeout=5)
        assert daemon.stop_event.is_set()
        assert daemon.operation_runner.current is not None
        daemon.controller.write_operation_result.assert_called_once_with(
            bmcctld.OP_RESULT_PREEMPTED, "-")
        callback.assert_not_called()
        joined_callback.assert_not_called()

    @pytest.mark.parametrize("subscription_error", [False, True])
    def test_subscription_exit_cleans_up_without_worker(self, lifecycle, subscription_error):
        daemon, event_thread, worker = lifecycle
        wait = MagicMock(return_value=False)
        if subscription_error:
            wait.side_effect = RuntimeError("subscription failed")
        daemon.event_handler.wait_for_event_subscriptions = wait
        if subscription_error:
            with pytest.raises(RuntimeError, match="subscription failed"):
                bmcctld.main()
        else:
            assert bmcctld.main() == bmcctld.exit_code
        event_thread.join.assert_called_once_with(timeout=5)
        assert daemon.stop_event.is_set()
        assert daemon.operation_runner.current is None
        worker.start.assert_not_called()
        worker.join.assert_not_called()
        daemon._run_action_loop.assert_not_called()
        daemon.controller.write_operation_result.assert_not_called()

    @pytest.mark.parametrize("failure", ["init", "seed", "thread_start"])
    def test_failure_before_event_start_has_no_worker_or_join(self, lifecycle, failure):
        daemon, event_thread, worker = lifecycle
        target = {
            "init": (daemon.controller, "init_host_state"),
            "seed": (daemon.event_handler, "seed_chassis_module_admin_status"),
            "thread_start": (event_thread, "start"),
        }[failure]
        with patch.object(*target, side_effect=RuntimeError("startup failed")):
            with pytest.raises(RuntimeError, match="startup failed"):
                bmcctld.main()
        event_thread.join.assert_not_called()
        worker.start.assert_not_called()
        worker.join.assert_not_called()
        assert daemon.operation_runner.current is None
        daemon._run_action_loop.assert_not_called()
        daemon.controller.write_operation_result.assert_not_called()

    def test_main_stop_without_current_worker(self, lifecycle):
        daemon, event_thread, worker = lifecycle
        daemon.stop_event.set()
        assert bmcctld.main() == bmcctld.exit_code
        event_thread.join.assert_called_once_with(timeout=5)
        worker.start.assert_not_called()
        worker.join.assert_not_called()
        assert daemon.operation_runner.current is None
        daemon.controller.write_operation_result.assert_not_called()

    def test_run_not_liquid_cooled_powers_on_immediately(self, chassis):
        """Non-liquid-cooled + admin up: power_on is called immediately."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock(
            side_effect=daemon.event_handler._subscription_ready.set)
        chassis.set_liquid_cooled(False)
        result = daemon.run()
        assert result is False
        item = _dequeue_item(daemon.action_queue)
        assert item.action == bmcctld.ACTION_POWER_ON
        assert item.event_desc == "STARTUP"
        daemon._run_action_loop.assert_called_once()

    def test_run_not_liquid_cooled_skips_power_on_when_admin_down(self, chassis):
        """Non-liquid-cooled + admin down: Switch-Host stays off at BMC reboot."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.policy_reader.get_switch_host_admin_status = MagicMock(
            return_value=bmcctld.ADMIN_DOWN)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock(
            side_effect=daemon.event_handler._subscription_ready.set)
        chassis.set_liquid_cooled(False)
        daemon.run()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()
        daemon._run_action_loop.assert_called_once()

    def test_run_not_liquid_cooled_skips_initial_sequence_but_starts_event_thread(self, chassis):
        """Non-liquid-cooled: event thread starts (for CLI admin cmds), but no boot sequence."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon.controller.power_on = MagicMock(return_value=True)
        daemon._run_action_loop = MagicMock()
        daemon._initial_power_on_sequence = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock(
            side_effect=daemon.event_handler._subscription_ready.set)
        chassis.set_liquid_cooled(False)
        daemon.run()
        daemon._initial_power_on_sequence.assert_not_called()
        daemon.event_handler.run_event_loop.assert_called_once()

    def test_run_daemon_restart_skips_boot_sequence(self, chassis):
        """Daemon restart: Switch-Host already ONLINE — skip boot sequence entirely."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock()
        daemon.controller.power_on = MagicMock()
        daemon.controller.init_host_state = MagicMock()
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock(
            side_effect=daemon.event_handler._subscription_ready.set)
        chassis.set_liquid_cooled(True)
        result = daemon.run()
        assert result is False
        daemon._initial_power_on_sequence.assert_not_called()
        assert daemon.action_queue.empty()
        daemon.controller.power_on.assert_not_called()
        daemon.controller.init_host_state.assert_called_once()
        daemon._run_action_loop.assert_called_once()

    def test_run_initializes_host_state_before_event_thread(self, chassis):
        """HOST_STATE sync and CONFIG dedup happen before the event thread starts."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        daemon = self._make_daemon(chassis)
        call_order = []

        def track_init():
            call_order.append("init_host_state")

        def track_seed():
            call_order.append("seed")

        def track_event_loop():
            daemon.event_handler._subscription_ready.set()
            call_order.append("event_loop")

        daemon.controller.init_host_state = MagicMock(side_effect=track_init)
        daemon.event_handler.seed_chassis_module_admin_status = MagicMock(side_effect=track_seed)
        daemon.event_handler.run_event_loop = MagicMock(side_effect=track_event_loop)
        daemon._run_action_loop = MagicMock()
        chassis.set_liquid_cooled(True)
        daemon.run()
        assert call_order.index("init_host_state") < call_order.index("seed")
        assert call_order.index("seed") < call_order.index("event_loop")

    def test_run_liquid_cooled_runs_full_sequence(self, chassis):
        """Liquid-cooled: event thread and initial power-on sequence are both invoked."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_OFFLINE)
        daemon = self._make_daemon(chassis)
        daemon._initial_power_on_sequence = MagicMock()
        daemon._run_action_loop = MagicMock()
        daemon.event_handler.run_event_loop = MagicMock(
            side_effect=daemon.event_handler._subscription_ready.set)
        # Set stop_event so _run_action_loop returns without looping
        daemon._initial_power_on_sequence.side_effect = lambda: daemon.stop_event.set()
        chassis.set_liquid_cooled(True)
        result = daemon.run()
        assert result is False
        daemon._initial_power_on_sequence.assert_called_once()

    def test_run_propagates_event_subscription_setup_error(self, chassis):
        daemon = self._make_daemon(chassis)
        daemon.event_handler._create_event_subscriptions = MagicMock(
            side_effect=RuntimeError("subscription setup failed"))
        daemon._run_action_loop = MagicMock()

        with pytest.raises(RuntimeError, match="subscription setup failed"):
            daemon.run()

        daemon._run_action_loop.assert_not_called()


# --------------------------------------------------------------------------
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
        controller.power_on(_make_operation(bmcctld.ACTION_POWER_ON))
        result = controller.chassis_module_info_table.get(bmcctld.SWITCH_HOST_MODULE_KEY)
        assert result[0] is True
        info = dict(result[1])
        assert info[bmcctld.CHASSIS_MODULE_INFO_OPERSTATUS_FIELD] == bmcctld.SWITCH_HOST_ONLINE

    def test_power_off_mirrors_oper_status_offline(self, chassis, controller):
        """power_off updates oper_status=OFFLINE in CHASSIS_MODULE_TABLE."""
        chassis.switch_host.set_oper_status(MockModule.MODULE_STATUS_ONLINE)
        controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
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
        controller.power_off(_make_operation(bmcctld.ACTION_POWER_OFF))
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
        event_handler.critical_event_checker.has_any_critical_event = MagicMock(return_value=True)
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

    def test_subscription_wait_returns_when_daemon_stops(self, event_handler):
        event_handler.stop_event.clear()
        result = []
        waiter = threading.Thread(
            target=lambda: result.append(
                event_handler.wait_for_event_subscriptions()))
        waiter.start()
        event_handler.stop_event.set()
        waiter.join(2)

        assert not waiter.is_alive()
        assert result == [False]

    def test_each_execution_thread_owns_its_database_objects(
            self, monkeypatch, chassis):
        import weakref

        store = {}
        created = []
        connection_records = []
        connection_sequence = itertools.count()
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
            def __init__(self, kind, retain=True):
                self.kind = kind
                self.owner_id = threading.get_ident()
                self.owner_name = threading.current_thread().name
                self._active = threading.Lock()
                if retain:
                    created.append(self)

            def check_owner(self):
                assert threading.get_ident() == self.owner_id, (
                    "{} created by {} used by {}".format(
                        self.kind, self.owner_name,
                        threading.current_thread().name))

            def enter(self):
                self.check_owner()
                assert self._active.acquire(False), (
                    "overlapping use of {} owned by {}".format(
                        self.kind, self.owner_name))

            def leave(self):
                self._active.release()

        class Connection(OwnedObject):
            def __init__(self, db_name):
                self.db_name = db_name
                self.token = next(connection_sequence)
                super().__init__("{} connector".format(db_name), retain=False)
                connection_records.append(
                    (self.token, self.db_name, self.owner_name))

        class StrictTable(OwnedObject):
            def __init__(self, db, table_name, kind="table", retain_db=True):
                db.check_owner()
                self._db = db if retain_db else weakref.ref(db)
                self.connector_id = db.token
                self.table_name = table_name
                self._waited = False
                self._active_db = None
                super().__init__("{} {}".format(table_name, kind))

            def connection(self):
                db = self._db() if isinstance(
                    self._db, weakref.ReferenceType) else self._db
                assert db is not None, (
                    "{} lost its source connector".format(self.kind))
                return db

            def enter(self):
                db = self.connection()
                db.enter()
                try:
                    super().enter()
                except Exception:
                    db.leave()
                    raise
                self._active_db = db

            def leave(self):
                super().leave()
                self._active_db.leave()
                self._active_db = None

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
                    table_data(self.connection().db_name, self.table_name).setdefault(
                        key, {}).update(values)
                finally:
                    self.leave()

            def get(self, key):
                self.enter()
                try:
                    value = table_data(
                        self.connection().db_name, self.table_name).get(key)
                    return [value is not None, list(value.items()) if value else []]
                finally:
                    self.leave()

            def getKeys(self):
                self.enter()
                try:
                    return list(table_data(
                        self.connection().db_name, self.table_name))
                finally:
                    self.leave()

        class StrictSubscriber(StrictTable):
            def __init__(self, db, table_name):
                super().__init__(
                    db, table_name, "subscriber", retain_db=False)

            def getFd(self):
                self.check_owner()
                return id(self)

            def pop(self):
                self.enter()
                try:
                    rows = table_data(
                        self.connection().db_name, self.table_name)
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
                self.enter()
                try:
                    selectable.check_owner()
                    self.selectables.append(selectable)
                finally:
                    self.leave()

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

        def worker_access():
            controller._update_host_state(
                bmcctld.SWITCH_HOST_POWERING_OFF,
                bmcctld.SWITCH_HOST_OFFLINE)
            policy_reader.get_graceful_shutdown_timeout()
            policy_reader.get_gnoi_cert_paths()
            critical_checker.get_system_leak_status()

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
            event_handler._set_cmd_request_id("CMD_THREAD", TEST_UUID4)
        release.set()
        event_thread.join(3)
        worker_thread.join(3)

        assert both_started, errors
        assert not event_thread.is_alive()
        assert not worker_thread.is_alive()
        assert errors == []

        item = _dequeue_item(action_queue)
        item.on_complete(True, "")
        command = table_data(
            "STATE_DB", bmcctld.RACK_MANAGER_COMMAND_TABLE)["CMD_THREAD"]
        assert command[bmcctld.FIELD_REQUEST_ID] == TEST_UUID4
        assert command[bmcctld.FIELD_STATUS] == bmcctld.CMD_STATUS_DONE

        for role in (
                admission_thread_name, "db-owner-event", "db-owner-worker"):
            assert any(obj.table_name == bmcctld.BMC_GNOI_TABLE and
                       obj.owner_name == role for obj in created
                       if isinstance(obj, StrictTable))
            assert {
                db_name for _, db_name, owner_name in connection_records
                if owner_name == role
            } == {
                "STATE_DB", "CONFIG_DB"}

        role_connector_ids = {
            role: {
                object_id for object_id, _, owner_name in connection_records
                if owner_name == role
            }
            for role in (
                admission_thread_name, "db-owner-event", "db-owner-worker")
        }
        assert role_connector_ids[admission_thread_name].isdisjoint(
            role_connector_ids["db-owner-event"])
        assert role_connector_ids[admission_thread_name].isdisjoint(
            role_connector_ids["db-owner-worker"])
        assert role_connector_ids["db-owner-event"].isdisjoint(
            role_connector_ids["db-owner-worker"])

        event_command_tables = [
            obj for obj in created
            if isinstance(obj, StrictTable) and
            not isinstance(obj, StrictSubscriber) and
            obj.owner_name == "db-owner-event" and
            obj.table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE]
        admission_command_tables = [
            obj for obj in created
            if isinstance(obj, StrictTable) and
            not isinstance(obj, StrictSubscriber) and
            obj.owner_name == admission_thread_name and
            obj.table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE]
        command_subscribers = [
            obj for obj in created
            if isinstance(obj, StrictSubscriber) and
            obj.table_name == bmcctld.RACK_MANAGER_COMMAND_TABLE]
        assert len(event_command_tables) == 1
        assert len(admission_command_tables) == 1
        assert len(command_subscribers) == 1
        assert event_command_tables[0] is not admission_command_tables[0]
        assert event_command_tables[0].connector_id != \
            command_subscribers[0].connector_id
