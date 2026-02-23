import os
import sys
import threading
from imp import load_source  # TODO: Replace with importlib once we no longer need to support Python 2

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

from sonic_py_common import daemon_base

from .mock_platform import MockChassis, MockFan, MockModule, MockPsu, MockSfp, MockThermal
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
        temp_updater.phy_entity_table._del = mock.MagicMock()
        temp_updater.phy_entity_table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        temp_updater.chassis_table = Table("STATE_DB", "ctable")
        temp_updater.chassis_table._del = mock.MagicMock()
        temp_updater.is_chassis_system = True
        temp_updater.is_chassis_upd_required = True

        temp_updater.__del__()
        assert temp_updater.table.getKeys.call_count == 1
        assert temp_updater.table._del.call_count == 2
        expected_calls = [mock.call('key1'), mock.call('key2')]
        temp_updater.table._del.assert_has_calls(expected_calls, any_order=True)
        assert temp_updater.chassis_table._del.call_count == 2

    def test_deinit_exception(self):
        chassis = MockChassis()
        temp_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temp_updater.temperature_status_dict = {'key1': 'value1', 'key2': 'value2'}
        temp_updater.table = Table("STATE_DB", "xtable")
        temp_updater.table._del = mock.MagicMock()
        temp_updater.table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        temp_updater.phy_entity_table = Table("STATE_DB", "ytable")
        temp_updater.phy_entity_table._del = mock.MagicMock()
        temp_updater.phy_entity_table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        temp_updater.chassis_table = Table("STATE_DB", "ctable")
        temp_updater.chassis_table._del = mock.Mock()
        temp_updater.chassis_table._del.side_effect = Exception('test')
        temp_updater.is_chassis_system = True
        temp_updater.is_chassis_upd_required = True

        temp_updater.__del__()
        assert temp_updater.table.getKeys.call_count == 1
        assert temp_updater.table._del.call_count == 2
        expected_calls = [mock.call('key1'), mock.call('key2')]
        temp_updater.table._del.assert_has_calls(expected_calls, any_order=True)
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

    def test_update_sfp_thermals(self):
        """Test SFP thermal processing with Redis-based temperature reading"""
        chassis = MockChassis()
        sfp = MockSfp()
        mock_thermal = MockThermal()
        sfp._thermal_list.append(mock_thermal)
        chassis._sfp_list.append(sfp)
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Reset warning count after init (init may log SfpUtilHelper warning)
        temperature_updater.log_warning.reset_mock()

        # With sfp_util as None (default), no Redis reading happens, no warnings
        temperature_updater.update()
        assert temperature_updater.log_warning.call_count == 0

        # With sfp_util mocked and port_name available, Redis reading is attempted
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '55.5')])
        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (True, [])
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()

        temperature_updater.update()
        # Verify Redis table was queried
        temperature_updater.xcvr_dom_temp_tbl.get.assert_called_with('Ethernet0')

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
        assert len(temperature_updater.all_thermals) == 3

        chassis._module_list = []
        temperature_updater.update()
        assert len(temperature_updater.all_thermals) == 0

    def test_sfp_temperature_from_redis(self):
        """Test reading SFP temperature from Redis tables and verify TEMPERATURE_INFO is populated correctly"""
        chassis = MockChassis()
        sfp = MockSfp()
        sfp._name = 'Ethernet0'
        thermal = MockThermal()
        thermal._name = 'xSFP module 1 Temp'
        sfp._thermal_list.append(thermal)
        chassis._sfp_list.append(sfp)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Mock the SfpUtilHelper to return correct port mapping
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']

        # Mock the Redis tables to return temperature data
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '55.5')])

        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (True, [
            ('temphighwarning', '70.0'),
            ('templowwarning', '-5.0'),
            ('temphighalarm', '75.0'),
            ('templowalarm', '-10.0')
        ])

        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()

        # Use a real Table object to capture the set() calls
        temperature_updater.table = Table("STATE_DB", "TEMPERATURE_INFO")

        temperature_updater.update()

        # Verify temperature was read from Redis
        temperature_updater.xcvr_dom_temp_tbl.get.assert_called_with('Ethernet0')
        temperature_updater.xcvr_dom_threshold_tbl.get.assert_called_with('Ethernet0')

        # Verify TEMPERATURE_INFO table was populated with correct values
        assert 'xSFP module 1 Temp' in temperature_updater.table.mock_dict
        stored_data = temperature_updater.table.mock_dict['xSFP module 1 Temp']

        # Verify parsed temperature value
        assert stored_data['temperature'] == '55.5'
        # Verify parsed threshold values
        assert stored_data['high_threshold'] == '70.0'
        assert stored_data['low_threshold'] == '-5.0'
        assert stored_data['critical_high_threshold'] == '75.0'
        assert stored_data['critical_low_threshold'] == '-10.0'
        # Verify warning status (should be False since 55.5 is within thresholds)
        assert stored_data['warning_status'] == 'False'
        # Verify other expected fields exist
        assert 'minimum_temperature' in stored_data
        assert 'maximum_temperature' in stored_data
        assert 'is_replaceable' in stored_data
        assert 'timestamp' in stored_data

    def test_sfp_temperature_warning_status(self):
        """Test that warning_status is True when temperature exceeds high threshold"""
        chassis = MockChassis()
        sfp = MockSfp()
        thermal = MockThermal()
        thermal._name = 'xSFP module 1 Temp'
        sfp._thermal_list.append(thermal)
        chassis._sfp_list.append(sfp)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']

        # Temperature exceeds high threshold (80 > 70)
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '80.0')])

        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (True, [
            ('temphighwarning', '70.0'),
            ('templowwarning', '-5.0')
        ])

        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.table = Table("STATE_DB", "TEMPERATURE_INFO")

        temperature_updater.update()

        stored_data = temperature_updater.table.mock_dict['xSFP module 1 Temp']
        assert stored_data['temperature'] == '80.0'
        assert stored_data['warning_status'] == 'True'

    def test_sfp_temperature_fallback_to_dom_sensor(self):
        """Test fallback to TRANSCEIVER_DOM_SENSOR table when DOM_TEMPERATURE is not available"""
        chassis = MockChassis()
        sfp = MockSfp()
        sfp._name = 'Ethernet0'
        thermal = MockThermal()
        thermal._name = 'xSFP module 1 Temp'
        sfp._thermal_list.append(thermal)
        chassis._sfp_list.append(sfp)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Mock the SfpUtilHelper
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']

        # Mock DOM_TEMPERATURE table to return no data
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (False, [])

        # Mock DOM_THRESHOLD table to return no data
        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (False, [])

        # Mock DOM_SENSOR table to return temperature data (fallback)
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_sensor_tbl.get.return_value = (True, [
            ('temperature', '60.0'),
            ('temphighwarning', '75.0'),
            ('templowwarning', '-5.0'),
            ('temphighalarm', '80.0'),
            ('templowalarm', '-10.0')
        ])

        # Use a real Table object to capture the set() calls
        temperature_updater.table = Table("STATE_DB", "TEMPERATURE_INFO")

        temperature_updater.update()

        # Verify fallback to DOM_SENSOR table was called
        temperature_updater.xcvr_dom_sensor_tbl.get.assert_called()

        # Verify TEMPERATURE_INFO table was populated with fallback values
        assert 'xSFP module 1 Temp' in temperature_updater.table.mock_dict
        stored_data = temperature_updater.table.mock_dict['xSFP module 1 Temp']

        # Verify temperature from DOM_SENSOR fallback
        assert stored_data['temperature'] == '60.0'
        # Verify thresholds from DOM_SENSOR fallback
        assert stored_data['high_threshold'] == '75.0'
        assert stored_data['low_threshold'] == '-5.0'
        assert stored_data['critical_high_threshold'] == '80.0'
        assert stored_data['critical_low_threshold'] == '-10.0'

    def test_sfp_temperature_no_sfp_util(self):
        """Test that SFP temperature is skipped when SfpUtilHelper is not available"""
        chassis = MockChassis()
        sfp = MockSfp()
        sfp._thermal_list.append(MockThermal())
        chassis._sfp_list.append(sfp)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Reset warning count after init (init may log SfpUtilHelper warning)
        temperature_updater.log_warning.reset_mock()

        # Set sfp_util to None (simulating import failure)
        temperature_updater.sfp_util = None

        # Should not raise exception
        temperature_updater.update()
        assert temperature_updater.log_warning.call_count == 0

    def test_get_port_name_by_index(self):
        """Test _get_port_name_by_index method"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Mock the SfpUtilHelper
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0', 'Ethernet1']

        # Test getting port name for index 0 (physical index 1)
        port_name = temperature_updater._get_port_name_by_index(0)
        assert port_name == 'Ethernet0'
        temperature_updater.sfp_util.get_physical_to_logical.assert_called_with(1)

        # Test with no mapping found
        temperature_updater.sfp_util.get_physical_to_logical.return_value = None
        port_name = temperature_updater._get_port_name_by_index(5)
        assert port_name is None

        # Test with sfp_util not available
        temperature_updater.sfp_util = None
        port_name = temperature_updater._get_port_name_by_index(0)
        assert port_name is None

    def test_get_sfp_temperature_from_db(self):
        """Test _get_sfp_temperature_from_db method"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Mock the Redis tables
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()

        # Test reading from DOM_TEMPERATURE table
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '55.5')])
        temp = temperature_updater._get_sfp_temperature_from_db('Ethernet0')
        assert temp == 55.5

        # Test fallback to DOM_SENSOR table
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (False, [])
        temperature_updater.xcvr_dom_sensor_tbl.get.return_value = (True, [('temperature', '60.0')])
        temp = temperature_updater._get_sfp_temperature_from_db('Ethernet0')
        assert temp == 60.0

        # Test with N/A value
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', 'N/A')])
        temperature_updater.xcvr_dom_sensor_tbl.get.return_value = (False, [])
        temp = temperature_updater._get_sfp_temperature_from_db('Ethernet0')
        assert temp == thermalctld.NOT_AVAILABLE

        # Test with temperature value containing unit suffix
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '55.5 C')])
        temp = temperature_updater._get_sfp_temperature_from_db('Ethernet0')
        assert temp == 55.5

    def test_sfp_temperature_na_value(self):
        """Test that N/A temperature is stored correctly in TEMPERATURE_INFO"""
        chassis = MockChassis()
        sfp = MockSfp()
        thermal = MockThermal()
        thermal._name = 'xSFP module 1 Temp'
        sfp._thermal_list.append(thermal)
        chassis._sfp_list.append(sfp)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']

        # Return N/A temperature
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', 'N/A')])

        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (False, [])

        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_sensor_tbl.get.return_value = (False, [])

        temperature_updater.table = Table("STATE_DB", "TEMPERATURE_INFO")

        temperature_updater.update()

        assert 'xSFP module 1 Temp' in temperature_updater.table.mock_dict
        stored_data = temperature_updater.table.mock_dict['xSFP module 1 Temp']

        # Verify N/A temperature is stored correctly
        assert stored_data['temperature'] == 'N/A'
        # Verify thresholds are also N/A when not available
        assert stored_data['high_threshold'] == 'N/A'
        assert stored_data['low_threshold'] == 'N/A'
        # Warning status should be False when temperature is N/A
        assert stored_data['warning_status'] == 'False'

    def test_sfp_temperature_with_unit_suffix(self):
        """Test parsing temperature values with unit suffix (e.g., '55.5 C')"""
        chassis = MockChassis()
        sfp = MockSfp()
        thermal = MockThermal()
        thermal._name = 'xSFP module 1 Temp'
        sfp._thermal_list.append(thermal)
        chassis._sfp_list.append(sfp)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']

        # Temperature with unit suffix
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '55.5 C')])

        # Thresholds with unit suffix
        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (True, [
            ('temphighwarning', '70.0 C'),
            ('templowwarning', '-5.0 C'),
            ('temphighalarm', '75.0 C'),
            ('templowalarm', '-10.0 C')
        ])

        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.table = Table("STATE_DB", "TEMPERATURE_INFO")

        temperature_updater.update()

        stored_data = temperature_updater.table.mock_dict['xSFP module 1 Temp']

        # Verify unit suffix is stripped from values
        assert stored_data['temperature'] == '55.5'
        assert stored_data['high_threshold'] == '70.0'
        assert stored_data['low_threshold'] == '-5.0'
        assert stored_data['critical_high_threshold'] == '75.0'
        assert stored_data['critical_low_threshold'] == '-10.0'

    def test_init_sfp_util_helper_multi_asic(self):
        """Test _init_sfp_util_helper with multi-asic configuration"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        with mock.patch.object(thermalctld, 'SfpUtilHelper') as mock_sfp_util_class, \
             mock.patch.object(thermalctld.multi_asic, 'is_multi_asic', return_value=True), \
             mock.patch.object(thermalctld.multi_asic, 'get_num_asics', return_value=2), \
             mock.patch.object(thermalctld.device_info, 'get_paths_to_platform_and_hwsku_dirs', return_value=('/platform', '/hwsku')):
            mock_sfp_util_instance = mock.MagicMock()
            mock_sfp_util_class.return_value = mock_sfp_util_instance

            result = temperature_updater._init_sfp_util_helper()

            assert result is mock_sfp_util_instance
            mock_sfp_util_instance.read_all_porttab_mappings.assert_called_once_with('/hwsku', 2)

    def test_init_sfp_util_helper_system_exit(self):
        """Test _init_sfp_util_helper handles SystemExit from read_porttab_mappings"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        with mock.patch.object(thermalctld, 'SfpUtilHelper') as mock_sfp_util_class, \
             mock.patch.object(thermalctld.multi_asic, 'is_multi_asic', return_value=False), \
             mock.patch.object(thermalctld.device_info, 'get_path_to_port_config_file', return_value='/path/to/port_config.ini'):
            mock_sfp_util_instance = mock.MagicMock()
            mock_sfp_util_instance.read_porttab_mappings.side_effect = SystemExit(1)
            mock_sfp_util_class.return_value = mock_sfp_util_instance

            result = temperature_updater._init_sfp_util_helper()

            assert result is None
            temperature_updater.log_warning.assert_called()

    def test_init_sfp_util_helper_exception(self):
        """Test _init_sfp_util_helper handles generic Exception"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        with mock.patch.object(thermalctld, 'SfpUtilHelper') as mock_sfp_util_class, \
             mock.patch.object(thermalctld.multi_asic, 'is_multi_asic', return_value=False), \
             mock.patch.object(thermalctld.device_info, 'get_path_to_port_config_file', return_value='/path/to/port_config.ini'):
            mock_sfp_util_instance = mock.MagicMock()
            mock_sfp_util_instance.read_porttab_mappings.side_effect = Exception("File not found")
            mock_sfp_util_class.return_value = mock_sfp_util_instance

            result = temperature_updater._init_sfp_util_helper()

            assert result is None
            temperature_updater.log_warning.assert_called()

    def test_init_sfp_util_helper_not_available(self):
        """Test _init_sfp_util_helper when SfpUtilHelper import failed"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        # Save original and set to None
        original_sfp_util_helper = thermalctld.SfpUtilHelper
        thermalctld.SfpUtilHelper = None

        try:
            result = temperature_updater._init_sfp_util_helper()
            assert result is None
            temperature_updater.log_warning.assert_called()
        finally:
            thermalctld.SfpUtilHelper = original_sfp_util_helper

    def test_modular_chassis_sfp_thermals(self):
        """Test SFP thermal updates on modular chassis with modules"""
        chassis = MockChassis()
        chassis.set_modular_chassis(True)
        chassis.set_my_slot(1)

        # Create a module with SFP
        module = MockModule(1)
        module._name = 'Module 1'
        sfp = MockSfp()
        thermal = MockThermal()
        thermal._name = 'Module 1 xSFP module 1 Temp'
        sfp._thermal_list.append(thermal)
        module._sfp_list.append(sfp)
        chassis._module_list.append(module)

        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())
        temperature_updater.sfp_util = mock.MagicMock()
        temperature_updater.sfp_util.get_physical_to_logical.return_value = ['Ethernet0']

        # Mock Redis tables
        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (True, [('temperature', '45.0')])
        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (True, [
            ('temphighwarning', '70.0'),
            ('templowwarning', '-5.0'),
            ('temphighalarm', '75.0'),
            ('templowalarm', '-10.0')
        ])
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.table = Table("STATE_DB", "TEMPERATURE_INFO")

        temperature_updater.update()

        # Verify module SFP thermal was updated
        assert 'Module 1 xSFP module 1 Temp' in temperature_updater.table.mock_dict

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

    def test_get_sfp_temperature_from_db_exception_dom_temp(self):
        """Test _get_sfp_temperature_from_db handles exception from DOM_TEMPERATURE table"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.side_effect = Exception("Redis error")
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_sensor_tbl.get.return_value = (True, [('temperature', '50.0')])

        # Should fallback to DOM_SENSOR
        temp = temperature_updater._get_sfp_temperature_from_db('Ethernet0')
        assert temp == 50.0

    def test_get_sfp_temperature_from_db_exception_dom_sensor(self):
        """Test _get_sfp_temperature_from_db handles exception from DOM_SENSOR table"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        temperature_updater.xcvr_dom_temp_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_temp_tbl.get.return_value = (False, [])
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_sensor_tbl.get.side_effect = Exception("Redis error")

        temp = temperature_updater._get_sfp_temperature_from_db('Ethernet0')
        assert temp == thermalctld.NOT_AVAILABLE

    def test_get_sfp_thresholds_from_db_exception(self):
        """Test _get_sfp_thresholds_from_db handles parsing exceptions"""
        chassis = MockChassis()
        temperature_updater = thermalctld.TemperatureUpdater(chassis, threading.Event())

        temperature_updater.xcvr_dom_threshold_tbl = mock.MagicMock()
        # Return invalid threshold value that will cause float() to fail
        temperature_updater.xcvr_dom_threshold_tbl.get.return_value = (True, [
            ('temphighwarning', 'invalid_float'),
        ])
        temperature_updater.xcvr_dom_sensor_tbl = mock.MagicMock()
        temperature_updater.xcvr_dom_sensor_tbl.get.return_value = (False, [])

        # Should return N/A values without raising exception
        high, low, high_crit, low_crit = temperature_updater._get_sfp_thresholds_from_db('Ethernet0')
        assert high == thermalctld.NOT_AVAILABLE
        assert low == thermalctld.NOT_AVAILABLE
        assert high_crit == thermalctld.NOT_AVAILABLE
        assert low_crit == thermalctld.NOT_AVAILABLE


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
