import os
import sys
import threading
import time
import importlib.util
import importlib.machinery
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

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info.major == 3:
    from unittest import mock
else:
    import mock

import pytest
tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, 'mocked_libs')
sys.path.insert(0, mocked_libs_path)


import swsscommon
# Check we are using the mocked package
assert len(swsscommon.__path__) == 1
assert(os.path.samefile(swsscommon.__path__[0], os.path.join(mocked_libs_path, 'swsscommon')))

from sonic_py_common import daemon_base, device_info
from sonic_platform_base.liquid_cooling_base import LeakSeverity

from .mock_platform import MockChassis, MockFan, MockFanDrawer, MockModule, MockPsu, MockThermal
from .mock_swsscommon import Table

daemon_base.db_connect = mock.MagicMock()

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, 'scripts')
sys.path.insert(0, modules_path)

load_source('thermalctld', os.path.join(scripts_path, 'thermalctld'))
import thermalctld


TEMPER_INFO_TABLE_NAME = 'TEMPERATURE_INFO'


@pytest.fixture(scope='function', autouse=True)
def configure_mocks():
    thermalctld.FanStatus.log_notice = mock.MagicMock()
    thermalctld.FanStatus.log_warning = mock.MagicMock()
    thermalctld.FanUpdater.log_notice = mock.MagicMock()
    thermalctld.FanUpdater.log_warning = mock.MagicMock()
    thermalctld.TemperatureStatus.log_notice = mock.MagicMock()
    thermalctld.TemperatureStatus.log_warning = mock.MagicMock()
    thermalctld.TemperatureUpdater.log_notice = mock.MagicMock()
    thermalctld.TemperatureUpdater.log_warning = mock.MagicMock()

    yield

    thermalctld.FanStatus.log_notice.reset()
    thermalctld.FanStatus.log_warning.reset()
    thermalctld.FanUpdater.log_notice.reset()
    thermalctld.FanUpdater.log_notice.reset()
    thermalctld.TemperatureStatus.log_notice.reset()
    thermalctld.TemperatureStatus.log_warning.reset()
    thermalctld.TemperatureUpdater.log_warning.reset()
    thermalctld.TemperatureUpdater.log_warning.reset()


class TestFanStatus(object):
    """
    Test cases to cover functionality in FanStatus class
    """
    def test_set_presence(self):
        fan_status = thermalctld.FanStatus()
        ret = fan_status.set_presence(True)
        assert fan_status.presence
        assert not ret

        ret = fan_status.set_presence(False)
        assert not fan_status.presence
        assert ret

    def test_set_under_speed(self):
        fan_status = thermalctld.FanStatus()

        ret = fan_status.set_under_speed(False)
        assert not ret

        ret = fan_status.set_under_speed(True)
        assert ret
        assert fan_status.under_speed
        assert not fan_status.is_ok()

        ret = fan_status.set_under_speed(True)
        assert not ret

        ret = fan_status.set_under_speed(False)
        assert ret
        assert not fan_status.under_speed
        assert fan_status.is_ok()

        ret = fan_status.set_under_speed(False)
        assert not ret

    def test_set_over_speed(self):
        fan_status = thermalctld.FanStatus()

        ret = fan_status.set_over_speed(False)
        assert not ret

        ret = fan_status.set_over_speed(True)
        assert ret
        assert fan_status.over_speed
        assert not fan_status.is_ok()

        ret = fan_status.set_over_speed(True)
        assert not ret

        ret = fan_status.set_over_speed(False)
        assert ret
        assert not fan_status.over_speed
        assert fan_status.is_ok()

        ret = fan_status.set_over_speed(False)
        assert not ret


class TestFanUpdater(object):
    """
    Test cases to cover functionality in FanUpdater class
    """
    @mock.patch('thermalctld.try_get', mock.MagicMock(return_value=thermalctld.NOT_AVAILABLE))
    @mock.patch('thermalctld.update_entity_info', mock.MagicMock())
    def test_refresh_fan_drawer_status_fan_drawer_get_name_not_impl(self):
        # Test case where fan_drawer.get_name is not implemented
        fan_updater = thermalctld.FanUpdater(MockChassis(), threading.Event())
        mock_fan_drawer = mock.MagicMock()
        fan_updater._refresh_fan_drawer_status(mock_fan_drawer, 1)
        assert thermalctld.update_entity_info.call_count == 0

    # TODO: Add a test case for _refresh_fan_drawer_status with a good fan drawer

    def test_deinit(self):
        chassis = MockChassis()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.table = Table("STATE_DB", "fan_table")
        fan_updater.table._del = mock.MagicMock()
        fan_updater.table.getKeys = mock.MagicMock(return_value=['fan1', 'fan2'])
        fan_updater.drawer_table = Table("STATE_DB", "drawer_table")
        fan_updater.drawer_table._del = mock.MagicMock()
        fan_updater.drawer_table.getKeys = mock.MagicMock(return_value=['drawer1', 'drawer2'])
        fan_updater.phy_entity_table = Table("STATE_DB", "phy_entity_table")
        # Pre-populate physical entity table so .get() returns non-None
        fan_updater.phy_entity_table.mock_dict['fan1'] = {}
        fan_updater.phy_entity_table.mock_dict['fan2'] = {}
        fan_updater.phy_entity_table.mock_dict['fanExtra'] = {}
        fan_updater.phy_entity_table.mock_dict['drawer1'] = {}
        fan_updater.phy_entity_table.mock_dict['drawer2'] = {}
        fan_updater.phy_entity_table.mock_dict['drawerExtra'] = {}

        fan_updater.phy_entity_table._del = mock.MagicMock()

        fan_updater.__del__()

        # Verify fan table entries are deleted
        assert fan_updater.table.getKeys.call_count == 1
        assert fan_updater.table._del.call_count == 2
        fan_table_calls = [mock.call('fan1'), mock.call('fan2')]
        fan_updater.table._del.assert_has_calls(fan_table_calls, any_order=True)

        # Verify drawer table entries are deleted
        assert fan_updater.drawer_table.getKeys.call_count == 1
        assert fan_updater.drawer_table._del.call_count == 2
        drawer_table_calls = [mock.call('drawer1'), mock.call('drawer2')]
        fan_updater.drawer_table._del.assert_has_calls(drawer_table_calls, any_order=True)

        # Verify only physical entity entries matching fan and drawer keys are deleted
        # Should be 4 calls total: 2 for fans + 2 for drawers, rather than 6 (all 4 + redundant)
        assert fan_updater.phy_entity_table._del.call_count == 4
        phy_entity_calls = [mock.call('fan1'), mock.call('fan2'), mock.call('drawer1'), mock.call('drawer2')]
        fan_updater.phy_entity_table._del.assert_has_calls(phy_entity_calls, any_order=True)

    def test_update_fan_with_exception(self):
        chassis = MockChassis()
        chassis.make_error_fan()
        fan = MockFan()
        fan.make_over_speed()
        chassis.get_all_fans().append(fan)

        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        assert fan.get_status_led() == MockFan.STATUS_LED_COLOR_RED
        assert fan_updater.log_warning.call_count == 1

        # TODO: Clean this up once we no longer need to support Python 2
        if sys.version_info.major == 3:
            fan_updater.log_warning.assert_called_with("Failed to update fan status - Exception('Failed to get speed')")
        else:
            fan_updater.log_warning.assert_called_with("Failed to update fan status - Exception('Failed to get speed',)")

    def test_set_fan_led_exception(self):
        fan_status = thermalctld.FanStatus()
        mock_fan_drawer = mock.MagicMock()
        mock_fan = MockFan()
        mock_fan.set_status_led = mock.MagicMock(side_effect=NotImplementedError)

        fan_updater = thermalctld.FanUpdater(MockChassis(), threading.Event())
        fan_updater._set_fan_led(mock_fan_drawer, mock_fan, 'Test Fan', fan_status)
        assert fan_updater.log_warning.call_count == 1
        fan_updater.log_warning.assert_called_with('Failed to set status LED for fan Test Fan, set_status_led not implemented')

    def test_fan_absent(self):
        chassis = MockChassis()
        chassis.make_absent_fan()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        fan_list = chassis.get_all_fans()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
        assert fan_updater.log_warning.call_count == 2
        expected_calls = [
            mock.call('Fan removed warning: FanDrawer 0 fan 1 was removed from the system, potential overheat hazard'),
            mock.call('Insufficient number of working fans warning: 1 fan is not working')
        ]
        assert fan_updater.log_warning.mock_calls == expected_calls

        fan_list[0].set_presence(True)
        fan_updater.update()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
        assert fan_updater.log_notice.call_count == 2
        expected_calls = [
            mock.call('Fan removed warning cleared: FanDrawer 0 fan 1 was inserted'),
            mock.call('Insufficient number of working fans warning cleared: all fans are back to normal')
        ]
        assert fan_updater.log_notice.mock_calls == expected_calls

    def test_fan_faulty(self):
        chassis = MockChassis()
        chassis.make_faulty_fan()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        fan_list = chassis.get_all_fans()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
        assert fan_updater.log_warning.call_count == 2
        expected_calls = [
            mock.call('Fan fault warning: FanDrawer 0 fan 1 is broken'),
            mock.call('Insufficient number of working fans warning: 1 fan is not working')
        ]
        assert fan_updater.log_warning.mock_calls == expected_calls

        fan_list[0].set_status(True)
        fan_updater.update()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
        assert fan_updater.log_notice.call_count == 2
        expected_calls = [
            mock.call('Fan fault warning cleared: FanDrawer 0 fan 1 is back to normal'),
            mock.call('Insufficient number of working fans warning cleared: all fans are back to normal')
        ]
        assert fan_updater.log_notice.mock_calls == expected_calls

    def test_fan_under_speed(self):
        chassis = MockChassis()
        chassis.make_under_speed_fan()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        fan_list = chassis.get_all_fans()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
        assert fan_updater.log_warning.call_count == 1
        fan_updater.log_warning.assert_called_with('Fan low speed warning: FanDrawer 0 fan 1 current speed=1, target speed=2')

        fan_list[0].make_normal_speed()
        fan_updater.update()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
        assert fan_updater.log_notice.call_count == 1
        fan_updater.log_notice.assert_called_with('Fan low speed warning cleared: FanDrawer 0 fan 1 speed is back to normal')

    def test_fan_over_speed(self):
        chassis = MockChassis()
        chassis.make_over_speed_fan()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        fan_list = chassis.get_all_fans()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
        assert fan_updater.log_warning.call_count == 1
        fan_updater.log_warning.assert_called_with('Fan high speed warning: FanDrawer 0 fan 1 current speed=2, target speed=1')

        fan_list[0].make_normal_speed()
        fan_updater.update()
        assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
        assert fan_updater.log_notice.call_count == 1
        fan_updater.log_notice.assert_called_with('Fan high speed warning cleared: FanDrawer 0 fan 1 speed is back to normal')

    def test_update_psu_fans(self):
        chassis = MockChassis()
        psu = MockPsu()
        mock_fan = MockFan()
        psu._fan_list.append(mock_fan)
        chassis._psu_list.append(psu)
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        assert fan_updater.log_warning.call_count == 0

        fan_updater._refresh_fan_status = mock.MagicMock(side_effect=Exception("Test message"))
        fan_updater.update()
        assert fan_updater.log_warning.call_count == 1

        # TODO: Clean this up once we no longer need to support Python 2
        if sys.version_info.major == 3:
            fan_updater.log_warning.assert_called_with("Failed to update PSU fan status - Exception('Test message')")
        else:
            fan_updater.log_warning.assert_called_with("Failed to update PSU fan status - Exception('Test message',)")

    def test_update_module_fans(self):
        chassis = MockChassis()
        module = MockModule()
        mock_fan = MockFan()
        chassis.set_modular_chassis(True)
        module._fan_list.append(mock_fan)
        chassis._module_list.append(module)
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater.update()
        assert fan_updater.log_warning.call_count == 0

        fan_updater._refresh_fan_status = mock.MagicMock(side_effect=Exception("Test message"))
        fan_updater.update()
        assert fan_updater.log_warning.call_count == 1

        # TODO: Clean this up once we no longer need to support Python 2
        if sys.version_info.major == 3:
            fan_updater.log_warning.assert_called_with("Failed to update module fan status - Exception('Test message')")
        else:
            fan_updater.log_warning.assert_called_with("Failed to update module fan status - Exception('Test message',)")

    def test_collect_fans_returns_true(self):
        """Test _collect_fans returns True when processing completes normally"""
        chassis = MockChassis()
        fan_drawer = MockFanDrawer(0)
        fan_drawer._fan_list.append(MockFan())
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater._refresh_fan_status = mock.MagicMock()
        result = fan_updater._collect_fans(fan_drawer, 0, thermalctld.FanType.DRAWER)
        assert result is True
        assert fan_updater._refresh_fan_status.call_count == 1

    def test_collect_fans_stops_on_event(self):
        """Test _collect_fans returns False when task_stopping_event is set"""
        chassis = MockChassis()
        stopping_event = threading.Event()
        stopping_event.set()
        fan_drawer = MockFanDrawer(0)
        fan_drawer._fan_list.append(MockFan())
        fan_updater = thermalctld.FanUpdater(chassis, stopping_event)
        fan_updater._refresh_fan_status = mock.MagicMock()
        result = fan_updater._collect_fans(fan_drawer, 0, thermalctld.FanType.DRAWER)
        assert result is False
        assert fan_updater._refresh_fan_status.call_count == 0

    def test_collect_fans_with_exception(self):
        """Test _collect_fans logs warning with error_prefix on exception"""
        chassis = MockChassis()
        psu = MockPsu()
        psu._fan_list.append(MockFan())
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater._refresh_fan_status = mock.MagicMock(side_effect=Exception("test error"))
        result = fan_updater._collect_fans(psu, 0, thermalctld.FanType.PSU, 'PSU ')
        assert result is True
        assert fan_updater.log_warning.call_count == 1
        fan_updater.log_warning.assert_called_with("Failed to update PSU fan status - Exception('test error')")

    def test_update_stops_during_drawer_loop(self):
        """Test update() returns early when task_stopping_event is set during drawer loop"""
        chassis = MockChassis()
        chassis.make_absent_fan()  # adds a fan drawer
        stopping_event = threading.Event()
        fan_updater = thermalctld.FanUpdater(chassis, stopping_event)
        stopping_event.set()
        fan_updater._refresh_fan_drawer_status = mock.MagicMock()
        fan_updater.update()
        assert fan_updater._refresh_fan_drawer_status.call_count == 0

    def test_update_stops_during_collect_fans(self):
        """Test update() returns early when _collect_fans returns False"""
        chassis = MockChassis()
        chassis.make_absent_fan()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater._collect_fans = mock.MagicMock(return_value=False)
        fan_updater.update()
        assert fan_updater._collect_fans.call_count == 1

class TestLiquidCoolingUpdater(object):
    def test_update(self):
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater._refresh_leak_status = mock.MagicMock()

        liquid_cooling_updater.update()

        assert liquid_cooling_updater._refresh_leak_status.call_count == 1

    @mock.patch('thermalctld.try_get')
    def test_refresh_leak_status_no_leak(self, mock_try_get):
        """Test _refresh_leak_status when no sensors are leaking"""
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {}

        mock_try_get.side_effect = lambda func, default: func()

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.sensor_table.set.call_count == 2
        assert liquid_cooling_updater.log_error.call_count == 0
        assert liquid_cooling_updater.log_notice.call_count == 0
        assert len(liquid_cooling_updater.leaking_sensors) == 0
        assert len(liquid_cooling_updater.faulty_sensors) == 0

        calls = liquid_cooling_updater.sensor_table.set.call_args_list
        for call in calls:
            sensor_name, fvp = call[0]
            assert sensor_name in ["leakage1", "leakage2"]
            assert fvp.fv_dict['leaking'] == 'No'

        calls_sys = liquid_cooling_updater.system_table.set.call_args_list
        for call in calls_sys:
            scope_name, fvp = call[0]
            assert scope_name == "system"
            assert fvp.fv_dict['device_leak_status'] == 'None'

    @mock.patch('thermalctld.try_get')
    def test_refresh_leak_status_with_leak(self, mock_try_get):
        """Test _refresh_leak_status when one sensor is leaking"""
        mock_chassis = MockChassis()
        mock_chassis.get_liquid_cooling().make_sensor_leak(0)

        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {}

        mock_try_get.side_effect = lambda func, default: func()

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.sensor_table.set.call_count == 2
        assert liquid_cooling_updater.log_error.call_count == 1
        assert len(liquid_cooling_updater.leaking_sensors) == 1
        assert "leakage1" in liquid_cooling_updater.leaking_sensors
        assert len(liquid_cooling_updater.faulty_sensors) == 0
        assert liquid_cooling_updater.last_leak_status == LeakSeverity.CRITICAL

        liquid_cooling_updater.log_error.assert_called_with(
            'Liquid cooling leakage sensor leakage1 reported leaking'
        )

        calls = liquid_cooling_updater.sensor_table.set.call_args_list
        leak_statuses = {}
        for call in calls:
            sensor_name, fvp = call[0]
            leak_statuses[sensor_name] = fvp.fv_dict['leaking']

        assert leak_statuses["leakage1"] == "Yes"
        assert leak_statuses["leakage2"] == "No"

        calls_sys = liquid_cooling_updater.system_table.set.call_args_list
        for call in calls_sys:
            scope_name, fvp = call[0]
            assert scope_name == "system"
            assert fvp.fv_dict['device_leak_status'] == 'CRITICAL'

    @mock.patch('thermalctld.try_get')
    def test_refresh_status_with_multiple_leaks(self, mock_try_get):
        """Test _refresh_leak_status when multiple sensors are leaking"""
        mock_chassis = MockChassis()

        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {}

        mock_try_get.side_effect = lambda func, default: func()

        # Sensors need to be minor leaks to test the multi-leak logic.
        mock_chassis.get_liquid_cooling().leakage_sensors[0].get_leak_severity = mock.MagicMock(return_value=LeakSeverity.MINOR)
        mock_chassis.get_liquid_cooling().leakage_sensors[1].get_leak_severity = mock.MagicMock(return_value=LeakSeverity.MINOR)

        # Start with a single sensor leaking.
        mock_chassis.get_liquid_cooling().make_sensor_leak(0)

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.log_error.call_count == 1
        assert len(liquid_cooling_updater.leaking_sensors) == 1
        assert "leakage1" in liquid_cooling_updater.leaking_sensors
        assert len(liquid_cooling_updater.faulty_sensors) == 0
        assert liquid_cooling_updater.last_leak_status == LeakSeverity.MINOR

        # Make the second sensor leak and poll again.
        mock_chassis.get_liquid_cooling().make_sensor_leak(1)

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.log_error.call_count == 2
        assert len(liquid_cooling_updater.leaking_sensors) == 2
        assert "leakage1" in liquid_cooling_updater.leaking_sensors
        assert "leakage2" in liquid_cooling_updater.leaking_sensors
        assert len(liquid_cooling_updater.faulty_sensors) == 0
        assert liquid_cooling_updater.last_leak_status == LeakSeverity.CRITICAL

        calls_sys = liquid_cooling_updater.system_table.set.call_args_list
        for index, call in enumerate(calls_sys):
            scope_name, fvp = call[0]
            assert scope_name == "system"
            if index == 0:
                assert fvp.fv_dict['device_leak_status'] == 'MINOR'
            elif index == 1:
                assert fvp.fv_dict['device_leak_status'] == 'CRITICAL'

    @mock.patch('thermalctld.try_get')
    def test_refresh_status_with_long_leak(self, mock_try_get):
        """Test _refresh_leak_status when one sensor leaks for an extended period"""
        mock_chassis = MockChassis()
        mock_chassis.get_liquid_cooling().make_sensor_leak(0)

        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        # Sensors need to be minor leaks to test the long leak logic.
        mock_chassis.get_liquid_cooling().leakage_sensors[0].get_leak_severity = mock.MagicMock(return_value=LeakSeverity.MINOR)
        mock_chassis.get_liquid_cooling().leakage_sensors[0].get_leak_profile().get_leak_max_minor_duration_sec = mock.MagicMock(return_value=1)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {}

        mock_try_get.side_effect = lambda func, default: func()

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.log_error.call_count == 1
        assert len(liquid_cooling_updater.leaking_sensors) == 1
        assert "leakage1" in liquid_cooling_updater.leaking_sensors
        assert len(liquid_cooling_updater.faulty_sensors) == 0
        assert liquid_cooling_updater.last_leak_status == LeakSeverity.MINOR

        time.sleep(2)

        liquid_cooling_updater._refresh_leak_status()

        assert len(liquid_cooling_updater.leaking_sensors) == 1
        assert "leakage1" in liquid_cooling_updater.leaking_sensors
        assert len(liquid_cooling_updater.faulty_sensors) == 0
        assert liquid_cooling_updater.last_leak_status == LeakSeverity.CRITICAL

        calls_sys = liquid_cooling_updater.system_table.set.call_args_list
        for index, call in enumerate(calls_sys):
            scope_name, fvp = call[0]
            assert scope_name == "system"
            if index == 0:
                assert fvp.fv_dict['device_leak_status'] == 'MINOR'
            elif index == 1:
                assert fvp.fv_dict['device_leak_status'] == 'CRITICAL'

    @mock.patch('thermalctld.try_get')
    def test_refresh_leak_status_leak_recovery(self, mock_try_get):
        """Test _refresh_leak_status when a sensor recovers from leak"""
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {"leakage1": 0}

        mock_try_get.side_effect = lambda func, default: func()

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.sensor_table.set.call_count == 2
        assert len(liquid_cooling_updater.leaking_sensors) == 0
        assert len(liquid_cooling_updater.faulty_sensors) == 0

    @mock.patch('thermalctld.try_get')
    def test_refresh_leak_status_sensor_unavailable(self, mock_try_get):
        """Test _refresh_leak_status when sensor returns None/N/A"""
        mock_chassis = MockChassis()

        mock_chassis.get_liquid_cooling().leakage_sensors[0].is_leak_sensor_ok = mock.MagicMock(return_value=False)

        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {}

        mock_try_get.side_effect = lambda func, default: func()

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.sensor_table.set.call_count == 2

        calls = liquid_cooling_updater.sensor_table.set.call_args_list
        leak_statuses = {}
        for call in calls:
            sensor_name, fvp = call[0]
            leak_statuses[sensor_name] = fvp.fv_dict['leaking']

        assert len(liquid_cooling_updater.faulty_sensors) == 1
        assert "leakage1" in liquid_cooling_updater.faulty_sensors

        assert leak_statuses["leakage1"] == "N/A"
        assert leak_statuses["leakage2"] == "No"

        calls_sys = liquid_cooling_updater.system_table.call_args_list
        for call in calls_sys:
            scope_name, fvp = call[0]
            assert scope_name == "system"
            assert fvp.fv_dict['device_leak_status'] == 'None'

    @mock.patch('thermalctld.try_get')
    def test_refresh_leak_status_fault_recovery(self, mock_try_get):
        """Test _refresh_leak_status recovery when a sensor is no longer faulty."""
        mock_chassis = MockChassis()

        mock_chassis.get_liquid_cooling().leakage_sensors[0].is_leak_sensor_ok = mock.MagicMock(return_value=False)

        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_error = mock.MagicMock()
        liquid_cooling_updater.log_notice = mock.MagicMock()
        liquid_cooling_updater.sensor_table = mock.MagicMock()
        liquid_cooling_updater.system_table = mock.MagicMock()
        liquid_cooling_updater.leaking_sensors = {}

        mock_try_get.side_effect = lambda func, default: func()

        liquid_cooling_updater._refresh_leak_status()

        assert liquid_cooling_updater.sensor_table.set.call_count == 2

        calls = liquid_cooling_updater.sensor_table.set.call_args_list
        leak_statuses = {}
        for call in calls:
            sensor_name, fvp = call[0]
            leak_statuses[sensor_name] = fvp.fv_dict['leaking']

        assert len(liquid_cooling_updater.faulty_sensors) == 1
        assert "leakage1" in liquid_cooling_updater.faulty_sensors

        assert leak_statuses["leakage1"] == "N/A"
        assert leak_statuses["leakage2"] == "No"

        mock_chassis.get_liquid_cooling().leakage_sensors[0].is_leak_sensor_ok = mock.MagicMock(return_value=True)

        liquid_cooling_updater._refresh_leak_status()

        calls = liquid_cooling_updater.sensor_table.set.call_args_list
        leak_statuses = {}
        for call in calls:
            sensor_name, fvp = call[0]
            leak_statuses[sensor_name] = fvp.fv_dict['leaking']

        assert len(liquid_cooling_updater.faulty_sensors) == 0

        assert leak_statuses["leakage1"] == "No"
        assert leak_statuses["leakage2"] == "No"

    def test_run(self):
        """Test run method normal execution"""
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.task_worker = mock.MagicMock()

        liquid_cooling_updater.run()

        assert liquid_cooling_updater.thread_id is not None
        assert liquid_cooling_updater.task_worker.call_count == 1
        assert liquid_cooling_updater.exc is None
        assert not liquid_cooling_updater.task_stopping_event.is_set()

    def test_run_with_exception(self):
        """Test run method exception handling"""
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        test_exception = Exception("Test exception for liquid cooling")

        liquid_cooling_updater.task_worker = mock.MagicMock(side_effect=test_exception)

        liquid_cooling_updater.run()

        assert liquid_cooling_updater.thread_id is not None
        assert liquid_cooling_updater.task_worker.call_count == 1
        assert liquid_cooling_updater.exc is test_exception
        assert liquid_cooling_updater.task_stopping_event.is_set()

    @mock.patch('time.sleep')
    def test_task_worker(self, mock_sleep):
        """Test task_worker method logic"""
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_debug = mock.MagicMock()
        liquid_cooling_updater.update = mock.MagicMock()

        stopping_event = threading.Event()

        def side_effect_sleep(interval):
            stopping_event.set()

        mock_sleep.side_effect = side_effect_sleep

        liquid_cooling_updater.task_worker(stopping_event)

        assert liquid_cooling_updater.log_debug.call_count == 2
        expected_calls = [
            mock.call("Start liquid cooling updating"),
            mock.call("End liquid cooling updating")
        ]
        liquid_cooling_updater.log_debug.assert_has_calls(expected_calls)

        assert liquid_cooling_updater.update.call_count == 1

        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(0.5)

    @mock.patch('time.sleep')
    def test_task_worker_early_stop(self, mock_sleep):
        """Test task_worker method stops when task_stopping_event is set"""
        mock_chassis = MockChassis()
        liquid_cooling_updater = thermalctld.LiquidCoolingUpdater(mock_chassis, 0.5)

        liquid_cooling_updater.log_debug = mock.MagicMock()
        liquid_cooling_updater.update = mock.MagicMock()

        stopping_event = threading.Event()

        def side_effect_update():
            liquid_cooling_updater.task_stopping_event.set()

        liquid_cooling_updater.update.side_effect = side_effect_update

        liquid_cooling_updater.task_worker(stopping_event)

        assert liquid_cooling_updater.log_debug.call_count == 2
        expected_calls = [
            mock.call("Start liquid cooling updating"),
            mock.call("End liquid cooling updating")
        ]
        liquid_cooling_updater.log_debug.assert_has_calls(expected_calls)

        assert liquid_cooling_updater.update.call_count == 1

        assert mock_sleep.call_count == 0


class TestThermalMonitor(object):
    """
    Test cases to cover functionality in ThermalMonitor class
    """
    def test_main(self):
        mock_chassis = MockChassis()
        thermal_monitor = thermalctld.ThermalMonitor(mock_chassis, 5, 60, 30)
        thermal_monitor.fan_updater.update = mock.MagicMock()
        thermal_monitor.temperature_updater.update = mock.MagicMock()

        thermal_monitor.main()
        assert thermal_monitor.fan_updater.update.call_count == 1
        assert thermal_monitor.temperature_updater.update.call_count == 1


def test_insufficient_fan_number():
    fan_status1 = thermalctld.FanStatus()
    fan_status2 = thermalctld.FanStatus()

    fan_status1.set_presence(False)
    fan_status2.set_fault_status(False)
    assert thermalctld.FanStatus.get_bad_fan_count() == 2
    assert fan_status1.get_bad_fan_count() == 2
    assert fan_status2.get_bad_fan_count() == 2

    thermalctld.FanStatus.reset_fan_counter()
    assert thermalctld.FanStatus.get_bad_fan_count() == 0
    assert fan_status1.get_bad_fan_count() == 0
    assert fan_status2.get_bad_fan_count() == 0

    chassis = MockChassis()
    chassis.make_absent_fan()
    chassis.make_faulty_fan()
    fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
    fan_updater.update()
    assert fan_updater.log_warning.call_count == 3
    expected_calls = [
        mock.call('Fan removed warning: FanDrawer 0 fan 1 was removed from the system, potential overheat hazard'),
        mock.call('Fan fault warning: FanDrawer 1 fan 1 is broken'),
        mock.call('Insufficient number of working fans warning: 2 fans are not working')
    ]
    assert fan_updater.log_warning.mock_calls == expected_calls

    fan_list = chassis.get_all_fans()
    fan_list[0].set_presence(True)
    fan_updater.update()
    assert fan_updater.log_notice.call_count == 1
    fan_updater.log_warning.assert_called_with('Insufficient number of working fans warning: 1 fan is not working')

    fan_list[1].set_status(True)
    fan_updater.update()
    assert fan_updater.log_notice.call_count == 3
    expected_calls = [
            mock.call('Fan removed warning cleared: FanDrawer 0 fan 1 was inserted'),
            mock.call('Fan fault warning cleared: FanDrawer 1 fan 1 is back to normal'),
        mock.call('Insufficient number of working fans warning cleared: all fans are back to normal')
    ]
    assert fan_updater.log_notice.mock_calls == expected_calls


def test_temperature_status_set_over_temper():
    temperature_status = thermalctld.TemperatureStatus()
    ret = temperature_status.set_over_temperature(thermalctld.NOT_AVAILABLE, thermalctld.NOT_AVAILABLE)
    assert not ret

    ret = temperature_status.set_over_temperature(thermalctld.NOT_AVAILABLE, 0)
    assert not ret

    ret = temperature_status.set_over_temperature(0, thermalctld.NOT_AVAILABLE)
    assert not ret

    ret = temperature_status.set_over_temperature(2, 1)
    assert ret
    assert temperature_status.over_temperature

    ret = temperature_status.set_over_temperature(1, 2)
    assert ret
    assert not temperature_status.over_temperature


def test_temperstatus_set_under_temper():
    temperature_status = thermalctld.TemperatureStatus()
    ret = temperature_status.set_under_temperature(thermalctld.NOT_AVAILABLE, thermalctld.NOT_AVAILABLE)
    assert not ret

    ret = temperature_status.set_under_temperature(thermalctld.NOT_AVAILABLE, 0)
    assert not ret

    ret = temperature_status.set_under_temperature(0, thermalctld.NOT_AVAILABLE)
    assert not ret

    ret = temperature_status.set_under_temperature(1, 2)
    assert ret
    assert temperature_status.under_temperature

    ret = temperature_status.set_under_temperature(2, 1)
    assert ret
    assert not temperature_status.under_temperature


def test_temperature_status_set_not_available():
    THERMAL_NAME = 'Chassis 1 Thermal 1'
    temperature_status = thermalctld.TemperatureStatus()
    temperature_status.temperature = 20.0

    temperature_status.set_temperature(THERMAL_NAME, thermalctld.NOT_AVAILABLE)
    assert temperature_status.temperature is None
    assert temperature_status.log_warning.call_count == 1
    temperature_status.log_warning.assert_called_with('Temperature of {} became unavailable'.format(THERMAL_NAME))


class TestTemperatureUpdater(object):
    """
    Test cases to cover functionality in TemperatureUpdater class
    """
    def test_deinit(self):
        chassis = MockChassis()
        temp_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temp_updater.temperature_status_dict = {'key1': 'value1', 'key2': 'value2'}
        temp_updater.table = Table("STATE_DB", "xtable")
        temp_updater.table._del = mock.MagicMock()
        temp_updater.table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        temp_updater.phy_entity_table = Table("STATE_DB", "ytable")
        # Pre-populate physical entity table so .get() returns non-None
        temp_updater.phy_entity_table.mock_dict['key1'] = {}
        temp_updater.phy_entity_table.mock_dict['key2'] = {}
        temp_updater.phy_entity_table.mock_dict['keyredundant'] = {}
        temp_updater.phy_entity_table._del = mock.MagicMock()
        temp_updater.chassis_table = Table("STATE_DB", "ctable")
        temp_updater.chassis_table._del = mock.MagicMock()
        temp_updater.is_chassis_system = True
        temp_updater.is_chassis_upd_required = True

        temp_updater.__del__()

        # Verify temperature table entries are deleted
        assert temp_updater.table.getKeys.call_count == 1
        assert temp_updater.table._del.call_count == 2
        expected_calls = [mock.call('key1'), mock.call('key2')]
        temp_updater.table._del.assert_has_calls(expected_calls, any_order=True)

        # Verify chassis table entries are deleted
        assert temp_updater.chassis_table._del.call_count == 2

        # Verify only physical entity entries matching table keys are deleted (not redundant)
        assert temp_updater.phy_entity_table._del.call_count == 2
        temp_updater.phy_entity_table._del.assert_has_calls(expected_calls, any_order=True)

    def test_deinit_exception(self):
        chassis = MockChassis()
        temp_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temp_updater.temperature_status_dict = {'key1': 'value1', 'key2': 'value2'}
        temp_updater.table = Table("STATE_DB", "xtable")
        temp_updater.table._del = mock.MagicMock()
        temp_updater.table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        temp_updater.phy_entity_table = Table("STATE_DB", "ytable")
        # Pre-populate physical entity table so .get() returns non-None
        temp_updater.phy_entity_table.mock_dict['key1'] = {}
        temp_updater.phy_entity_table.mock_dict['key2'] = {}
        temp_updater.phy_entity_table._del = mock.MagicMock()
        temp_updater.chassis_table = Table("STATE_DB", "ctable")
        temp_updater.chassis_table._del = mock.Mock()
        temp_updater.chassis_table._del.side_effect = Exception('test')
        temp_updater.is_chassis_system = True
        temp_updater.is_chassis_upd_required = True

        temp_updater.__del__()

        # Verify temperature table entries are deleted
        assert temp_updater.table.getKeys.call_count == 1
        assert temp_updater.table._del.call_count == 2
        expected_calls = [mock.call('key1'), mock.call('key2')]
        temp_updater.table._del.assert_has_calls(expected_calls, any_order=True)

        # Verify chassis_table is set to None after exception
        assert temp_updater.chassis_table is None

    def test_over_temper(self):
        chassis = MockChassis()
        chassis.make_over_temper_thermal()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.log_warning.reset_mock()
        temperature_updater.update()
        thermal_list = chassis.get_all_thermals()
        assert temperature_updater.log_warning.call_count == 1
        temperature_updater.log_warning.assert_called_with('High temperature warning: chassis 1 Thermal 1 current temperature 3C, high threshold 2C')

        thermal_list[0].make_normal_temper()
        temperature_updater.update()
        assert temperature_updater.log_notice.call_count == 1
        temperature_updater.log_notice.assert_called_with('High temperature warning cleared: chassis 1 Thermal 1 temperature restored to 2C, high threshold 3C')

    def test_under_temper(self):
        chassis = MockChassis()
        chassis.make_under_temper_thermal()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.log_warning.reset_mock()
        temperature_updater.update()
        thermal_list = chassis.get_all_thermals()
        assert temperature_updater.log_warning.call_count == 1
        temperature_updater.log_warning.assert_called_with('Low temperature warning: chassis 1 Thermal 1 current temperature 1C, low threshold 2C')

        thermal_list[0].make_normal_temper()
        temperature_updater.update()
        assert temperature_updater.log_notice.call_count == 1
        temperature_updater.log_notice.assert_called_with('Low temperature warning cleared: chassis 1 Thermal 1 temperature restored to 2C, low threshold 1C')

    def test_update_psu_thermals(self):
        chassis = MockChassis()
        psu = MockPsu()
        mock_thermal = MockThermal()
        psu._thermal_list.append(mock_thermal)
        chassis._psu_list.append(psu)
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.log_warning.reset_mock()
        temperature_updater.update()
        assert temperature_updater.log_warning.call_count == 0

        mock_thermal.get_temperature = mock.MagicMock(side_effect=Exception("Test message"))
        temperature_updater.update()
        assert temperature_updater.log_warning.call_count == 1

        # TODO: Clean this up once we no longer need to support Python 2
        if sys.version_info.major == 3:
            temperature_updater.log_warning.assert_called_with("Failed to update thermal status for PSU 1 Thermal 1 - Exception('Test message')")
        else:
            temperature_updater.log_warning.assert_called_with("Failed to update thermal status for PSU 1 Thermal 1 - Exception('Test message',)")

    def test_update_thermal_with_exception(self):
        chassis = MockChassis()
        chassis.make_error_thermal()
        thermal = MockThermal()
        thermal.make_over_temper()
        chassis.get_all_thermals().append(thermal)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.log_warning.reset_mock()
        temperature_updater.update()
        assert temperature_updater.log_warning.call_count == 2

        # TODO: Clean this up once we no longer need to support Python 2
        if sys.version_info.major == 3:
            expected_calls = [
                mock.call("Failed to update thermal status for chassis 1 Thermal 1 - Exception('Failed to get temperature')"),
                mock.call('High temperature warning: chassis 1 Thermal 2 current temperature 3C, high threshold 2C')
            ]
        else:
            expected_calls = [
                mock.call("Failed to update thermal status for chassis 1 Thermal 1 - Exception('Failed to get temperature',)"),
                mock.call('High temperature warning: chassis 1 Thermal 2 current temperature 3C, high threshold 2C')
            ]
        assert temperature_updater.log_warning.mock_calls == expected_calls

    def test_update_module_thermals(self):
        chassis = MockChassis()
        chassis.make_module_thermal()
        chassis.set_modular_chassis(True)
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.update()
        # make_module_thermal adds 1 module thermal + 1 PSU thermal
        # (SFP thermals are not polled by thermalctld)
        assert len(temperature_updater.all_thermals) == 2

        chassis._module_list = []
        temperature_updater.update()
        assert len(temperature_updater.all_thermals) == 0

    def test_remove_thermal_from_db_exceptions(self):
        """Test _remove_thermal_from_db handles exceptions gracefully"""
        chassis = MockChassis()
        chassis.set_modular_chassis(True)
        chassis.set_my_slot(1)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Mock table to raise exception on _del
        temperature_updater.table = mock.MagicMock()
        temperature_updater.table._del.side_effect = Exception("Redis error")

        temperature_updater.chassis_table = mock.MagicMock()
        temperature_updater.chassis_table._del.side_effect = Exception("Chassis DB error")

        # Create a mock thermal
        thermal = MockThermal()
        thermal._name = 'Test Thermal'

        # Should not raise exception
        temperature_updater._remove_thermal_from_db(thermal, 'Test Parent', 0)

    def test_collect_thermals_returns_true(self):
        """Test _collect_thermals returns True when processing completes normally"""
        chassis = MockChassis()
        thermal = MockThermal(1)
        chassis._thermal_list.append(thermal)
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater._refresh_temperature_status = mock.MagicMock()
        available = set()
        result = temperature_updater._collect_thermals(available, 'chassis 1', [thermal])
        assert result is True
        assert len(available) == 1
        assert temperature_updater._refresh_temperature_status.call_count == 1

    def test_collect_thermals_stops_on_event(self):
        """Test _collect_thermals returns False when task_stopping_event is set"""
        chassis = MockChassis()
        stopping_event = threading.Event()
        stopping_event.set()
        thermal = MockThermal(1)
        temperature_updater = thermalctld.TemperatureUpdater(chassis, stopping_event)
        temperature_updater._refresh_temperature_status = mock.MagicMock()
        available = set()
        result = temperature_updater._collect_thermals(available, 'chassis 1', [thermal])
        assert result is False
        assert len(available) == 0
        assert temperature_updater._refresh_temperature_status.call_count == 0

    def test_update_stops_during_chassis_thermals(self):
        """Test update() returns early when _collect_thermals returns False for chassis thermals"""
        chassis = MockChassis()
        chassis._thermal_list.append(MockThermal(1))
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater._collect_thermals = mock.MagicMock(return_value=False)
        temperature_updater.update()
        assert temperature_updater._collect_thermals.call_count == 1

    def test_update_stops_during_psu_thermals(self):
        """Test update() returns early when _collect_thermals returns False for PSU thermals"""
        chassis = MockChassis()
        psu = MockPsu()
        psu._thermal_list.append(MockThermal(1))
        chassis._psu_list.append(psu)
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        # First call (chassis thermals) succeeds, second (PSU thermals) fails
        temperature_updater._collect_thermals = mock.MagicMock(side_effect=[True, False])
        temperature_updater.update()
        assert temperature_updater._collect_thermals.call_count == 2


# DPU chassis-related tests
def test_dpu_chassis_thermals():
    chassis = MockChassis()
    # Modular chassis (Not a dpu chassis) No Change in TemperatureUpdater Behaviour
    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert temperature_updater.chassis_table
    # DPU chassis TemperatureUpdater without is_smartswitch False return - No update to CHASSIS_STATE_DB
    chassis.set_modular_chassis(False)
    chassis.set_dpu(True)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert not temperature_updater.chassis_table
    # DPU chassis TemperatureUpdater without get_dpu_id implmenetation- No update to CHASSIS_STATE_DB
    chassis.set_smartswitch(True)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert not temperature_updater.chassis_table
    # DPU chassis TemperatureUpdater with get_dpu_id implemented - Update data to CHASSIS_STATE_DB
    dpu_id = 1
    chassis.set_dpu_id(dpu_id)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert temperature_updater.chassis_table
    # Table name in chassis state db = TEMPERATURE_INFO_0 for dpu_id 0
    assert temperature_updater.chassis_table.table_name == f"{TEMPER_INFO_TABLE_NAME}_{dpu_id}"
    temperature_updater.table = Table("STATE_DB", "xtable")
    temperature_updater.table._del = mock.MagicMock()


def test_dpu_chassis_state_deinit():
    # Confirm that the chassis_table entries for DPU Chassis are removed on deletion
    chassis = MockChassis()
    chassis.set_smartswitch(True)
    chassis.set_modular_chassis(False)
    chassis.set_dpu(True)
    chassis.set_dpu_id(1)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert temperature_updater.chassis_table
    temperature_updater.table = Table("STATE_DB", "xtable")
    temperature_updater.phy_entity_table = None
    temperature_updater.table.getKeys = mock.MagicMock(return_value=['key1', 'key2'])
    temperature_updater.table._del = mock.MagicMock()
    temperature_updater.chassis_table = Table("CHASSIS_STATE_DB", "ctable")
    temperature_updater.chassis_table._del = mock.MagicMock()
    temperature_updater.__del__()
    assert temperature_updater.chassis_table._del.call_count == 2
    expected_calls = [mock.call('key1'), mock.call('key2')]
    temperature_updater.chassis_table._del.assert_has_calls(expected_calls, any_order=True)


def test_updater_dpu_thermal_check_chassis_table():
    chassis = MockChassis()

    thermal1 = MockThermal()
    chassis.get_all_thermals().append(thermal1)

    chassis.set_dpu(True)
    chassis.set_smartswitch(True)
    chassis.set_dpu_id(1)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    temperature_updater.update()
    assert temperature_updater.chassis_table.get_size() == chassis.get_num_thermals()

    thermal2 = MockThermal()
    chassis.get_all_thermals().append(thermal2)
    temperature_updater.update()
    assert temperature_updater.chassis_table.get_size() == chassis.get_num_thermals()


# Modular chassis-related tests


def test_updater_thermal_check_modular_chassis():
    chassis = MockChassis()
    assert chassis.is_modular_chassis() == False

    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert temperature_updater.chassis_table == None

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(-1)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert temperature_updater.chassis_table == None

    my_slot = 1
    chassis.set_my_slot(my_slot)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
    assert temperature_updater.chassis_table != None
    assert temperature_updater.chassis_table.table_name == '{}_{}'.format(TEMPER_INFO_TABLE_NAME, str(my_slot))


def test_updater_thermal_check_chassis_table():
    chassis = MockChassis()

    thermal1 = MockThermal()
    chassis.get_all_thermals().append(thermal1)

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

    temperature_updater.update()
    assert temperature_updater.chassis_table.get_size() == chassis.get_num_thermals()

    thermal2 = MockThermal()
    chassis.get_all_thermals().append(thermal2)
    temperature_updater.update()
    assert temperature_updater.chassis_table.get_size() == chassis.get_num_thermals()


def test_updater_thermal_check_min_max():
    chassis = MockChassis()

    thermal = MockThermal(1)
    chassis.get_all_thermals().append(thermal)

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

    temperature_updater.update()
    slot_dict = temperature_updater.chassis_table.get(thermal.get_name())
    assert slot_dict['minimum_temperature'] == str(thermal.get_minimum_recorded())
    assert slot_dict['maximum_temperature'] == str(thermal.get_maximum_recorded())


def test_signal_handler():
    # Test SIGHUP
    daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)
    daemon_thermalctld.stop_event.set = mock.MagicMock()
    daemon_thermalctld.log_info = mock.MagicMock()
    daemon_thermalctld.log_warning = mock.MagicMock()
    daemon_thermalctld.thermal_manager.stop = mock.MagicMock()
    daemon_thermalctld.signal_handler(thermalctld.signal.SIGHUP, None)
    daemon_thermalctld.deinit() # Deinit becuase the test will hang if we assert
    assert daemon_thermalctld.log_info.call_count == 1
    daemon_thermalctld.log_info.assert_called_with("Caught signal 'SIGHUP' - ignoring...")
    assert daemon_thermalctld.log_warning.call_count == 0
    assert daemon_thermalctld.stop_event.set.call_count == 0
    assert daemon_thermalctld.thermal_manager.stop.call_count == 0
    assert thermalctld.exit_code == thermalctld.ERR_UNKNOWN

    # Test SIGINT
    daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)
    daemon_thermalctld.stop_event.set = mock.MagicMock()
    daemon_thermalctld.log_info = mock.MagicMock()
    daemon_thermalctld.log_warning = mock.MagicMock()
    daemon_thermalctld.thermal_manager.stop = mock.MagicMock()
    test_signal = thermalctld.signal.SIGINT
    daemon_thermalctld.signal_handler(test_signal, None)
    daemon_thermalctld.deinit() # Deinit becuase the test will hang if we assert
    assert daemon_thermalctld.log_info.call_count == 1
    daemon_thermalctld.log_info.assert_called_with("Caught signal 'SIGINT' - exiting...")
    assert daemon_thermalctld.log_warning.call_count == 0
    assert daemon_thermalctld.stop_event.set.call_count == 1
    assert daemon_thermalctld.thermal_manager.stop.call_count == 1
    assert thermalctld.exit_code == (128 + test_signal)

    # Test SIGTERM
    thermalctld.exit_code = thermalctld.ERR_UNKNOWN
    daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)
    daemon_thermalctld.stop_event.set = mock.MagicMock()
    daemon_thermalctld.log_info = mock.MagicMock()
    daemon_thermalctld.log_warning = mock.MagicMock()
    daemon_thermalctld.thermal_manager.stop = mock.MagicMock()
    test_signal = thermalctld.signal.SIGTERM
    daemon_thermalctld.signal_handler(test_signal, None)
    daemon_thermalctld.deinit() # Deinit becuase the test will hang if we assert
    assert daemon_thermalctld.log_info.call_count == 1
    daemon_thermalctld.log_info.assert_called_with("Caught signal 'SIGTERM' - exiting...")
    assert daemon_thermalctld.log_warning.call_count == 0
    assert daemon_thermalctld.stop_event.set.call_count == 1
    assert daemon_thermalctld.thermal_manager.stop.call_count == 1
    assert thermalctld.exit_code == (128 + test_signal)

    # Test an unhandled signal
    thermalctld.exit_code = thermalctld.ERR_UNKNOWN
    daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)
    daemon_thermalctld.stop_event.set = mock.MagicMock()
    daemon_thermalctld.log_info = mock.MagicMock()
    daemon_thermalctld.log_warning = mock.MagicMock()
    daemon_thermalctld.thermal_manager.stop = mock.MagicMock()
    daemon_thermalctld.signal_handler(thermalctld.signal.SIGUSR1, None)
    daemon_thermalctld.deinit() # Deinit becuase the test will hang if we assert
    assert daemon_thermalctld.log_warning.call_count == 1
    daemon_thermalctld.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1' - ignoring...")
    assert daemon_thermalctld.log_info.call_count == 0
    assert daemon_thermalctld.stop_event.set.call_count == 0
    assert daemon_thermalctld.thermal_manager.stop.call_count == 0
    assert thermalctld.exit_code == thermalctld.ERR_UNKNOWN


def test_daemon_run():
    daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)
    daemon_thermalctld.stop_event.wait = mock.MagicMock(return_value=True)
    daemon_thermalctld.thermal_manager.get_interval = mock.MagicMock(return_value=60)
    ret = daemon_thermalctld.run()
    daemon_thermalctld.deinit() # Deinit becuase the test will hang if we assert
    assert ret is False

    daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)
    daemon_thermalctld.stop_event.wait = mock.MagicMock(return_value=False)
    daemon_thermalctld.thermal_manager.get_interval = mock.MagicMock(return_value=60)
    ret = daemon_thermalctld.run()
    daemon_thermalctld.deinit() # Deinit becuase the test will hang if we assert
    assert ret is True


def test_try_get():
    def good_callback():
        return 'good result'

    def unimplemented_callback():
        raise NotImplementedError

    ret = thermalctld.try_get(good_callback)
    assert ret == 'good result'

    ret = thermalctld.try_get(unimplemented_callback)
    assert ret == thermalctld.NOT_AVAILABLE

    ret = thermalctld.try_get(unimplemented_callback, 'my default')
    assert ret == 'my default'


def test_update_entity_info():
    mock_table = mock.MagicMock()
    mock_fan = MockFan()
    expected_fvp = thermalctld.swsscommon.FieldValuePairs(
        [('position_in_parent', '1'),
         ('parent_name', 'Parent Name')
         ])

    thermalctld.update_entity_info(mock_table, 'Parent Name', 'Key Name', mock_fan, 1)
    assert mock_table.set.call_count == 1
    mock_table.set.assert_called_with('Key Name', expected_fvp)


@mock.patch('thermalctld.ThermalControlDaemon.run')
def test_main(mock_run):
    mock_run.return_value = False

    sys.argv = ['thermalctld']

    ret = thermalctld.main()
    assert mock_run.call_count == 1
    assert  ret != 0

class TestThermalControlDaemon(object):
    """
    Test cases to cover functionality in ThermalControlDaemon class
    """
    def test_get_chassis_exception(self):
        """Test ThermalControlDaemon initialization when get_chassis() raises exception"""
        with mock.patch('thermalctld.sonic_platform.platform.Platform') as mock_platform_class, \
              mock.patch.object(thermalctld.ThermalControlDaemon, 'log_error') as mock_log_error:
            # Mock Platform to raise exception on get_chassis()
            mock_platform_instance = mock.MagicMock()
            mock_platform_instance.get_chassis.side_effect = Exception("Failed to initialize chassis")
            mock_platform_class.return_value = mock_platform_instance

            # ThermalControlDaemon should raise SystemExit with CHASSIS_GET_ERROR code when chassis initialization fails
            with pytest.raises(SystemExit) as exc_info:
                daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)

            # Verify it exits with the correct error code
            assert exc_info.value.code == thermalctld.CHASSIS_GET_ERROR

            # Verify chassis initialization failure was logged
            mock_log_error.assert_called()
            expected_msg = "Failed to get chassis due to Exception('Failed to initialize chassis')"
            assert any(
                expected_msg in call_args[0]
                for call_args, _ in mock_log_error.call_args_list
            ), "Expected chassis initialization error not found in log_error calls"

    def test_get_chassis_success(self):
        """Test ThermalControlDaemon initialization when get_chassis() succeeds"""
        with mock.patch('thermalctld.sonic_platform.platform.Platform') as mock_platform_class, \
              mock.patch.object(thermalctld.ThermalControlDaemon, 'log_error') as mock_log_error:
            # Mock Platform to return chassis successfully
            mock_chassis = mock.MagicMock()
            mock_platform_instance = mock.MagicMock()
            mock_platform_instance.get_chassis.return_value = mock_chassis
            mock_platform_class.return_value = mock_platform_instance

            daemon_thermalctld = thermalctld.ThermalControlDaemon(5, 60, 30)

            # Verify chassis was set correctly
            assert daemon_thermalctld.chassis is mock_chassis

            # Verify no chassis initialization error was logged
            for call_args in mock_log_error.call_args_list:
                args, _ = call_args
                assert "Failed to get chassis due to" not in args[0]

            # Clean up
            daemon_thermalctld.deinit()


class TestParsePollingIntervals(object):
    """Tests for _parse_platform_json_polling_intervals()"""

    def _mock_platform_json(self, data):
        """Helper: return patch context managers for os.path.isfile + builtins.open."""
        import io, json as _json
        content = _json.dumps(data)
        mock_open = mock.mock_open(read_data=content)
        return (
            mock.patch('thermalctld.os.path.isfile', return_value=True),
            mock.patch('builtins.open', mock_open),
        )

    def test_returns_defaults_when_no_platform_json(self):
        with mock.patch('thermalctld.os.path.isfile', return_value=False):
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result == {'fan_drawer': None, 'psu': None, 'thermals': {}}

    def test_returns_defaults_on_exception(self):
        with mock.patch('thermalctld.os.path.isfile', return_value=True), \
             mock.patch('builtins.open', side_effect=Exception("file not found")):
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result == {'fan_drawer': None, 'psu': None, 'thermals': {}}

    def test_parses_fan_drawer_interval(self):
        p1, p2 = self._mock_platform_json({'fan_drawers': [{'polling_interval': '10'}]})
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result['fan_drawer'] == 10.0

    def test_parses_psu_interval(self):
        p1, p2 = self._mock_platform_json({'psus': [{'polling_interval': '30'}]})
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result['psu'] == 30.0

    def test_parses_thermal_intervals(self):
        p1, p2 = self._mock_platform_json({
            'thermals': [
                {'name': 'CPU Temp', 'polling_interval': '5'},
                {'name': 'GPU Temp', 'polling_interval': '15'},
            ]
        })
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result['thermals'] == {'CPU Temp': 5.0, 'GPU Temp': 15.0}

    def test_skips_empty_polling_interval(self):
        p1, p2 = self._mock_platform_json({
            'fan_drawers': [{'polling_interval': ''}],
            'psus': [{'polling_interval': ''}],
            'thermals': [{'name': 'T1', 'polling_interval': ''}],
        })
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result == {'fan_drawer': None, 'psu': None, 'thermals': {}}

    def test_skips_invalid_polling_interval(self):
        p1, p2 = self._mock_platform_json({
            'fan_drawers': [{'polling_interval': 'abc'}],
            'psus': [{'polling_interval': 'xyz'}],
            'thermals': [{'name': 'T1', 'polling_interval': 'bad'}],
        })
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result == {'fan_drawer': None, 'psu': None, 'thermals': {}}

    def test_skips_named_fan_drawer_entries(self):
        """Entries with 'name' are devices, not config — should be skipped."""
        p1, p2 = self._mock_platform_json({
            'fan_drawers': [
                {'name': 'FanDrawer 1', 'polling_interval': '10'},
            ]
        })
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result['fan_drawer'] is None

    def test_thermals_without_name_skipped(self):
        p1, p2 = self._mock_platform_json({
            'thermals': [{'polling_interval': '5'}]
        })
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result['thermals'] == {}

    def test_parses_chassis_nested_structure(self):
        """Real platform.json nests components under a 'chassis' key."""
        p1, p2 = self._mock_platform_json({
            'chassis': {
                'name': 'TestChassis',
                'fan_drawers': [{'polling_interval': '10'}, {'name': 'FD1'}],
                'psus': [{'polling_interval': '30'}, {'name': 'PSU1'}],
                'thermals': [{'name': 'CPU', 'polling_interval': '5'}],
            },
            'interfaces': {}
        })
        with p1, p2:
            result = thermalctld._parse_platform_json_polling_intervals()
        assert result['fan_drawer'] == 10.0
        assert result['psu'] == 30.0
        assert result['thermals'] == {'CPU': 5.0}


class TestShouldUpdateThermal(object):
    """Tests for TemperatureUpdater._should_update_thermal()"""

    def test_always_true_when_no_interval_configured(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        assert updater._should_update_thermal('Unknown Thermal') is True

    def test_default_interval_throttles_unconfigured_thermals(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), default_interval=60)
        # First call always returns True (last time is 0)
        assert updater._should_update_thermal('Unknown Thermal') is True
        # Immediately after, should be throttled
        assert updater._should_update_thermal('Unknown Thermal') is False

    def test_default_interval_allows_after_elapsed(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), default_interval=1)
        updater._should_update_thermal('Unknown Thermal')
        updater._last_thermal_update_times['Unknown Thermal'] = time.time() - 2
        assert updater._should_update_thermal('Unknown Thermal') is True

    def test_true_on_first_call(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), thermal_intervals={'CPU Temp': 10})
        assert updater._should_update_thermal('CPU Temp') is True

    def test_false_before_interval_elapses(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), thermal_intervals={'CPU Temp': 100})
        updater._should_update_thermal('CPU Temp')  # first call sets timestamp
        assert updater._should_update_thermal('CPU Temp') is False

    def test_true_after_interval_elapses(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), thermal_intervals={'CPU Temp': 1})
        updater._should_update_thermal('CPU Temp')
        # Fake that last update was 2 seconds ago
        updater._last_thermal_update_times['CPU Temp'] = time.time() - 2
        assert updater._should_update_thermal('CPU Temp') is True

    def test_explicit_interval_overrides_default(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(),
            thermal_intervals={'CPU Temp': 5}, default_interval=60)
        updater._should_update_thermal('CPU Temp')
        # CPU Temp uses explicit 5s, not default 60s
        updater._last_thermal_update_times['CPU Temp'] = time.time() - 6
        assert updater._should_update_thermal('CPU Temp') is True


class TestPsuIntervalGating(object):
    """Tests for PSU thermal polling interval gating in TemperatureUpdater.update()"""

    def test_psu_thermals_skipped_before_interval(self):
        chassis = MockChassis()
        psu = MockPsu()
        psu._thermal_list.append(MockThermal())
        chassis._psu_list.append(psu)

        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), psu_interval=300)
        updater._refresh_temperature_status = mock.MagicMock()

        # First update — should refresh PSU thermals (last_psu_thermal_update == 0)
        updater.update()
        first_count = updater._refresh_temperature_status.call_count
        assert first_count > 0

        updater._refresh_temperature_status.reset_mock()

        # Second update — PSU interval not yet elapsed, PSU thermals should be
        # collected (tracked in available_thermals) but NOT refreshed.
        # Chassis thermals still refresh.
        updater.update()
        # Only chassis thermals refreshed (0 chassis thermals here, so 0 calls)
        assert updater._refresh_temperature_status.call_count == 0

    def test_psu_thermals_refreshed_after_interval(self):
        chassis = MockChassis()
        psu = MockPsu()
        psu._thermal_list.append(MockThermal())
        chassis._psu_list.append(psu)

        updater = thermalctld.TemperatureUpdater(
            chassis, threading.Event(), psu_interval=1)
        updater._refresh_temperature_status = mock.MagicMock()

        updater.update()
        updater._refresh_temperature_status.reset_mock()

        # Fake that last PSU update was 2 seconds ago
        updater._last_psu_thermal_update = time.time() - 2
        updater.update()
        assert updater._refresh_temperature_status.call_count > 0

    def test_psu_thermals_always_refreshed_when_no_interval(self):
        chassis = MockChassis()
        psu = MockPsu()
        psu._thermal_list.append(MockThermal())
        chassis._psu_list.append(psu)

        updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        updater._refresh_temperature_status = mock.MagicMock()

        updater.update()
        first_count = updater._refresh_temperature_status.call_count
        updater._refresh_temperature_status.reset_mock()

        updater.update()
        assert updater._refresh_temperature_status.call_count == first_count


class TestThermalMonitorPollingIntervals(object):
    """Tests for ThermalMonitor with polling_intervals parameter"""

    def test_fan_update_interval_from_fan_drawer(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(
            chassis, 5, 60, 30,
            polling_intervals={'fan_drawer': 10, 'psu': None, 'thermals': {}})
        assert monitor._fan_update_interval == 10

    def test_fan_update_interval_from_min_of_fan_drawer_and_psu(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(
            chassis, 5, 60, 30,
            polling_intervals={'fan_drawer': 20, 'psu': 15, 'thermals': {}})
        assert monitor._fan_update_interval == 15

    def test_fan_update_interval_defaults_to_update_interval(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(chassis, 5, 60, 30)
        assert monitor._fan_update_interval == 60

    def test_update_interval_adjusted_for_fast_polling(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(
            chassis, 5, 60, 30,
            polling_intervals={'fan_drawer': 10, 'psu': None, 'thermals': {}})
        assert monitor.update_interval == 10

    def test_update_interval_not_adjusted_when_polling_is_slower(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(
            chassis, 5, 60, 30,
            polling_intervals={'fan_drawer': 120, 'psu': None, 'thermals': {}})
        assert monitor.update_interval == 60

    def test_main_skips_fan_update_before_interval(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(
            chassis, 5, 60, 30,
            polling_intervals={'fan_drawer': 300, 'psu': None, 'thermals': {}})
        monitor.fan_updater.update = mock.MagicMock()
        monitor.temperature_updater.update = mock.MagicMock()

        # First call — should update fans (last_fan_update == 0)
        monitor.main()
        assert monitor.fan_updater.update.call_count == 1

        monitor.fan_updater.update.reset_mock()

        # Second call immediately — should skip fans
        monitor.main()
        assert monitor.fan_updater.update.call_count == 0
        # Temperature always updates
        assert monitor.temperature_updater.update.call_count == 2

    def test_main_updates_fans_after_interval(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(
            chassis, 5, 60, 30,
            polling_intervals={'fan_drawer': 1, 'psu': None, 'thermals': {}})
        monitor.fan_updater.update = mock.MagicMock()
        monitor.temperature_updater.update = mock.MagicMock()

        monitor.main()
        monitor.fan_updater.update.reset_mock()

        # Fake that last fan update was 2 seconds ago
        monitor._last_fan_update = time.time() - 2
        monitor.main()
        assert monitor.fan_updater.update.call_count == 1

    def test_main_throttles_fan_update_at_default_interval(self):
        chassis = MockChassis()
        monitor = thermalctld.ThermalMonitor(chassis, 5, 60, 30)
        monitor.fan_updater.update = mock.MagicMock()
        monitor.temperature_updater.update = mock.MagicMock()

        # First call — should update fans (last_fan_update == 0)
        monitor.main()
        assert monitor.fan_updater.update.call_count == 1

        monitor.fan_updater.update.reset_mock()

        # Second call immediately — should skip fans (default 60s not elapsed)
        monitor.main()
        assert monitor.fan_updater.update.call_count == 0

        # After interval elapses, should update again
        monitor._last_fan_update = time.time() - 61
        monitor.main()
        assert monitor.fan_updater.update.call_count == 1


class TestCollectFansEarlyReturn(object):
    """Tests for FanUpdater._collect_fans() task_stopping_event handling"""

    def test_collect_fans_returns_false_on_stopping_event(self):
        chassis = MockChassis()
        stopping_event = threading.Event()
        stopping_event.set()  # Pre-set stopping event
        fan_updater = thermalctld.FanUpdater(chassis, stopping_event)
        fan_drawer = MockFanDrawer(0)
        fan_drawer._fan_list.append(MockFan())
        result = fan_updater._collect_fans(fan_drawer, 0, thermalctld.FanType.DRAWER)
        assert result is False

    def test_collect_fans_returns_true_normally(self):
        chassis = MockChassis()
        fan_updater = thermalctld.FanUpdater(chassis, threading.Event())
        fan_updater._refresh_fan_status = mock.MagicMock()
        fan_drawer = MockFanDrawer(0)
        fan_drawer._fan_list.append(MockFan())
        result = fan_updater._collect_fans(fan_drawer, 0, thermalctld.FanType.DRAWER)
        assert result is True
        assert fan_updater._refresh_fan_status.call_count == 1


class TestCollectThermalsEarlyReturn(object):
    """Tests for TemperatureUpdater._collect_thermals()"""

    def test_collect_thermals_returns_false_on_stopping_event(self):
        chassis = MockChassis()
        stopping_event = threading.Event()
        stopping_event.set()
        updater = thermalctld.TemperatureUpdater(chassis, stopping_event)
        available = set()
        result = updater._collect_thermals(available, 'test', [MockThermal()])
        assert result is False

    def test_collect_thermals_no_refresh_when_false(self):
        chassis = MockChassis()
        updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        updater._refresh_temperature_status = mock.MagicMock()
        updater.phy_entity_table = mock.MagicMock()
        available = set()
        result = updater._collect_thermals(available, 'PSU 1', [MockThermal()], refresh=False)
        assert result is True
        assert len(available) == 1
        updater._refresh_temperature_status.assert_not_called()
        # Entity info should still be updated even without temperature refresh
        updater.phy_entity_table.set.assert_called()
