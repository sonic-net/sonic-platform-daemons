import os
import sys
import multiprocessing

# Python 2 support
from imp import load_source
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

from .mock_platform import MockChassis, MockVsensor, MockIsensor
from .mock_swsscommon import Table

daemon_base.db_connect = mock.MagicMock()

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, 'scripts')
sys.path.insert(0, modules_path)

load_source('sensormond', os.path.join(scripts_path, 'sensormond'))
import sensormond


VOLTAGE_INFO_TABLE_NAME = 'VOLTAGE_INFO'
CURRENT_INFO_TABLE_NAME = 'CURRENT_INFO'


@pytest.fixture(scope='function', autouse=True)
def configure_mocks():
    sensormond.SensorStatus.log_notice = mock.MagicMock()
    sensormond.SensorStatus.log_warning = mock.MagicMock()
    sensormond.VoltageUpdater.log_notice = mock.MagicMock()
    sensormond.VoltageUpdater.log_warning = mock.MagicMock()
    sensormond.CurrentUpdater.log_notice = mock.MagicMock()
    sensormond.CurrentUpdater.log_warning = mock.MagicMock()

    yield

    sensormond.SensorStatus.log_notice.reset()
    sensormond.SensorStatus.log_warning.reset()
    sensormond.VoltageUpdater.log_notice.reset()
    sensormond.VoltageUpdater.log_warning.reset()
    sensormond.CurrentUpdater.log_notice.reset()
    sensormond.CurrentUpdater.log_warning.reset()

def test_sensor_status_set_over_threshold():
    sensor_status = sensormond.SensorStatus()
    ret = sensor_status.set_over_threshold(sensormond.NOT_AVAILABLE, sensormond.NOT_AVAILABLE)
    assert not ret

    ret = sensor_status.set_over_threshold(sensormond.NOT_AVAILABLE, 0)
    assert not ret

    ret = sensor_status.set_over_threshold(0, sensormond.NOT_AVAILABLE)
    assert not ret

    ret = sensor_status.set_over_threshold(2, 1)
    assert ret
    assert sensor_status.over_threshold

    ret = sensor_status.set_over_threshold(1, 2)
    assert ret
    assert not sensor_status.over_threshold


def test_sensor_status_set_under_threshold():
    sensor_status = sensormond.SensorStatus()
    ret = sensor_status.set_under_threshold(sensormond.NOT_AVAILABLE, sensormond.NOT_AVAILABLE)
    assert not ret

    ret = sensor_status.set_under_threshold(sensormond.NOT_AVAILABLE, 0)
    assert not ret

    ret = sensor_status.set_under_threshold(0, sensormond.NOT_AVAILABLE)
    assert not ret

    ret = sensor_status.set_under_threshold(1, 2)
    assert ret
    assert sensor_status.under_threshold

    ret = sensor_status.set_under_threshold(2, 1)
    assert ret
    assert not sensor_status.under_threshold


def test_sensor_status_set_not_available():
    SENSOR_NAME = 'Chassis 1 Sensor 1'
    sensor_status = sensormond.SensorStatus()
    sensor_status.value = 20.0

    sensor_status.set_value(SENSOR_NAME, sensormond.NOT_AVAILABLE)
    assert sensor_status.value is None
    assert sensor_status.log_warning.call_count == 1
    sensor_status.log_warning.assert_called_with('Value of {} became unavailable'.format(SENSOR_NAME))

class TestVoltageUpdater(object):
    """
    Test cases to cover functionality in VoltageUpdater class
    """
    def test_deinit(self):
        chassis = MockChassis()
        voltage_updater = sensormond.VoltageUpdater(chassis)
        voltage_updater.voltage_status_dict = {'key1': 'value1', 'key2': 'value2'}
        voltage_updater.table = Table("STATE_DB", "xtable")
        voltage_updater.table._del = mock.MagicMock()
        voltage_updater.table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        voltage_updater.phy_entity_table = Table("STATE_DB", "ytable")
        voltage_updater.phy_entity_table._del = mock.MagicMock()
        voltage_updater.phy_entity_table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        voltage_updater.chassis_table = Table("STATE_DB", "ctable")
        voltage_updater.chassis_table._del = mock.MagicMock()
        voltage_updater.is_chassis_system = True

        voltage_updater.__del__()
        assert voltage_updater.table.getKeys.call_count == 1
        assert voltage_updater.table._del.call_count == 2
        expected_calls = [mock.call('key1'), mock.call('key2')]
        voltage_updater.table._del.assert_has_calls(expected_calls, any_order=True)

    def test_over_voltage(self):
        chassis = MockChassis()
        chassis.make_over_threshold_vsensor()
        voltage_updater = sensormond.VoltageUpdater(chassis)
        voltage_updater.update()
        vsensor_list = chassis.get_all_vsensors()
        assert voltage_updater.log_warning.call_count == 1
        voltage_updater.log_warning.assert_called_with('High voltage warning: chassis 1 vsensor 1 current voltage 3mV, high threshold 2mV')

        vsensor_list[0].make_normal_value()
        voltage_updater.update()
        assert voltage_updater.log_notice.call_count == 1
        voltage_updater.log_notice.assert_called_with('High voltage warning cleared: chassis 1 vsensor 1 voltage restored to 2mV, high threshold 3mV')

    def test_under_voltage(self):
        chassis = MockChassis()
        chassis.make_under_threshold_vsensor()
        voltage_updater = sensormond.VoltageUpdater(chassis)
        voltage_updater.update()
        vsensor_list = chassis.get_all_vsensors()
        assert voltage_updater.log_warning.call_count == 1
        voltage_updater.log_warning.assert_called_with('Low voltage warning: chassis 1 vsensor 1 current voltage 1mV, low threshold 2mV')

        vsensor_list[0].make_normal_value()
        voltage_updater.update()
        assert voltage_updater.log_notice.call_count == 1
        voltage_updater.log_notice.assert_called_with('Low voltage warning cleared: chassis 1 vsensor 1 voltage restored to 2mV, low threshold 1mV')

    def test_update_vsensor_with_exception(self):
        chassis = MockChassis()
        chassis.make_error_vsensor()
        vsensor = MockVsensor()
        vsensor.make_over_threshold()
        chassis.get_all_vsensors().append(vsensor)

        voltage_updater = sensormond.VoltageUpdater(chassis)
        voltage_updater.update()
        assert voltage_updater.log_warning.call_count == 2

        if sys.version_info.major == 3:
            expected_calls = [
                mock.call("Failed to update vsensor status for chassis 1 vsensor 1 - Exception('Failed to get voltage')"),
                mock.call('High voltage warning: chassis 1 vsensor 2 current voltage 3mV, high threshold 2mV')
            ]
        else:
            expected_calls = [
                mock.call("Failed to update vsensor status for chassis 1 vsensor 1 - Exception('Failed to get voltage',)"),
                mock.call('High voltage warning: chassis 1 vsensor 2 current voltage 3mV, high threshold 2mV')
            ]
        assert voltage_updater.log_warning.mock_calls == expected_calls

    def test_update_module_vsensors(self):
        chassis = MockChassis()
        chassis.make_module_vsensor()
        chassis.set_modular_chassis(True)
        voltage_updater = sensormond.VoltageUpdater(chassis)
        voltage_updater.update()
        assert len(voltage_updater.module_vsensors) == 1
        
        chassis._module_list = []
        voltage_updater.update()
        assert len(voltage_updater.module_vsensors) == 0


class TestCurrentUpdater(object):
    """
    Test cases to cover functionality in CurrentUpdater class
    """
    def test_deinit(self):
        chassis = MockChassis()
        current_updater = sensormond.CurrentUpdater(chassis)
        current_updater.current_status_dict = {'key1': 'value1', 'key2': 'value2'}
        current_updater.table = Table("STATE_DB", "xtable")
        current_updater.table._del = mock.MagicMock()
        current_updater.table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        current_updater.phy_entity_table = Table("STATE_DB", "ytable")
        current_updater.phy_entity_table._del = mock.MagicMock()
        current_updater.phy_entity_table.getKeys = mock.MagicMock(return_value=['key1','key2'])
        current_updater.chassis_table = Table("STATE_DB", "ctable")
        current_updater.chassis_table._del = mock.MagicMock()
        current_updater.is_chassis_system = True

        current_updater.__del__()
        assert current_updater.table.getKeys.call_count == 1
        assert current_updater.table._del.call_count == 2
        expected_calls = [mock.call('key1'), mock.call('key2')]
        current_updater.table._del.assert_has_calls(expected_calls, any_order=True)

    def test_over_current(self):
        chassis = MockChassis()
        chassis.make_over_threshold_isensor()
        current_updater = sensormond.CurrentUpdater(chassis)
        current_updater.update()
        isensor_list = chassis.get_all_isensors()
        assert current_updater.log_warning.call_count == 1
        current_updater.log_warning.assert_called_with('High Current warning: chassis 1 isensor 1 current Current 3mA, high threshold 2mA')

        isensor_list[0].make_normal_value()
        current_updater.update()
        assert current_updater.log_notice.call_count == 1
        current_updater.log_notice.assert_called_with('High Current warning cleared: chassis 1 isensor 1 current restored to 2mA, high threshold 3mA')

    def test_under_current(self):
        chassis = MockChassis()
        chassis.make_under_threshold_isensor()
        current_updater = sensormond.CurrentUpdater(chassis)
        current_updater.update()
        isensor_list = chassis.get_all_isensors()
        assert current_updater.log_warning.call_count == 1
        current_updater.log_warning.assert_called_with('Low current warning: chassis 1 isensor 1 current current 1mA, low threshold 2mA')

        isensor_list[0].make_normal_value()
        current_updater.update()
        assert current_updater.log_notice.call_count == 1
        current_updater.log_notice.assert_called_with('Low current warning cleared: chassis 1 isensor 1 current restored to 2mA, low threshold 1mA')

    def test_update_isensor_with_exception(self):
        chassis = MockChassis()
        chassis.make_error_isensor()
        isensor = MockIsensor()
        isensor.make_over_threshold()
        chassis.get_all_isensors().append(isensor)

        current_updater = sensormond.CurrentUpdater(chassis)
        current_updater.update()
        assert current_updater.log_warning.call_count == 2

        if sys.version_info.major == 3:
            expected_calls = [
                mock.call("Failed to update isensor status for chassis 1 isensor 1 - Exception('Failed to get current')"),
                mock.call('High Current warning: chassis 1 isensor 2 current Current 3mA, high threshold 2mA')
            ]
        else:
            expected_calls = [
                mock.call("Failed to update isensor status for chassis 1 isensor 1 - Exception('Failed to get current',)"),
                mock.call('High Current warning: chassis 1 isensor 2 current Current 3mA, high threshold 2mA')
            ]
        assert current_updater.log_warning.mock_calls == expected_calls

    def test_update_module_isensors(self):
        chassis = MockChassis()
        chassis.make_module_isensor()
        chassis.set_modular_chassis(True)
        current_updater = sensormond.CurrentUpdater(chassis)
        current_updater.update()
        assert len(current_updater.module_isensors) == 1
        
        chassis._module_list = []
        current_updater.update()
        assert len(current_updater.module_isensors) == 0

# Modular chassis-related tests


def test_updater_vsensor_check_modular_chassis():
    chassis = MockChassis()
    assert chassis.is_modular_chassis() == False

    voltage_updater = sensormond.VoltageUpdater(chassis)
    assert voltage_updater.chassis_table == None

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(-1)
    voltage_updater = sensormond.VoltageUpdater(chassis)
    assert voltage_updater.chassis_table == None

    my_slot = 1
    chassis.set_my_slot(my_slot)
    voltage_updater = sensormond.VoltageUpdater(chassis)
    assert voltage_updater.chassis_table != None
    assert voltage_updater.chassis_table.table_name == '{}_{}'.format(VOLTAGE_INFO_TABLE_NAME, str(my_slot))


def test_updater_vsensor_check_chassis_table():
    chassis = MockChassis()

    vsensor1 = MockVsensor()
    chassis.get_all_vsensors().append(vsensor1)

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    voltage_updater = sensormond.VoltageUpdater(chassis)

    voltage_updater.update()
    assert voltage_updater.chassis_table.get_size() == chassis.get_num_vsensors()

    vsensor2 = MockVsensor()
    chassis.get_all_vsensors().append(vsensor2)
    voltage_updater.update()
    assert voltage_updater.chassis_table.get_size() == chassis.get_num_vsensors()

def test_updater_vsensor_check_min_max():
    chassis = MockChassis()

    vsensor = MockVsensor(1)
    chassis.get_all_vsensors().append(vsensor)

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    voltage_updater = sensormond.VoltageUpdater(chassis)

    voltage_updater.update()
    slot_dict = voltage_updater.chassis_table.get(vsensor.get_name())
    assert slot_dict['minimum_voltage'] == str(vsensor.get_minimum_recorded())
    assert slot_dict['maximum_voltage'] == str(vsensor.get_maximum_recorded())


def test_updater_isensor_check_modular_chassis():
    chassis = MockChassis()
    assert chassis.is_modular_chassis() == False

    current_updater = sensormond.CurrentUpdater(chassis)
    assert current_updater.chassis_table == None

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(-1)
    current_updater = sensormond.CurrentUpdater(chassis)
    assert current_updater.chassis_table == None

    my_slot = 1
    chassis.set_my_slot(my_slot)
    current_updater = sensormond.CurrentUpdater(chassis)
    assert current_updater.chassis_table != None
    assert current_updater.chassis_table.table_name == '{}_{}'.format(CURRENT_INFO_TABLE_NAME, str(my_slot))


def test_updater_isensor_check_chassis_table():
    chassis = MockChassis()

    isensor1 = MockIsensor()
    chassis.get_all_isensors().append(isensor1)

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    current_updater = sensormond.CurrentUpdater(chassis)

    current_updater.update()
    assert current_updater.chassis_table.get_size() == chassis.get_num_isensors()

    isensor2 = MockIsensor()
    chassis.get_all_isensors().append(isensor2)
    current_updater.update()
    assert current_updater.chassis_table.get_size() == chassis.get_num_isensors()


def test_updater_isensor_check_min_max():
    chassis = MockChassis()

    isensor = MockIsensor(1)
    chassis.get_all_isensors().append(isensor)

    chassis.set_modular_chassis(True)
    chassis.set_my_slot(1)
    current_updater = sensormond.CurrentUpdater(chassis)

    current_updater.update()
    slot_dict = current_updater.chassis_table.get(isensor.get_name())
    assert slot_dict['minimum_current'] == str(isensor.get_minimum_recorded())
    assert slot_dict['maximum_current'] == str(isensor.get_maximum_recorded())

def test_signal_handler():
    # Test SIGHUP
    daemon_sensormond = sensormond.SensorMonitorDaemon()
    daemon_sensormond.stop_event.set = mock.MagicMock()
    daemon_sensormond.log_info = mock.MagicMock()
    daemon_sensormond.log_warning = mock.MagicMock()
    daemon_sensormond.signal_handler(sensormond.signal.SIGHUP, None)
    assert daemon_sensormond.log_info.call_count == 1
    daemon_sensormond.log_info.assert_called_with("Caught signal 'SIGHUP' - ignoring...")
    assert daemon_sensormond.log_warning.call_count == 0
    assert daemon_sensormond.stop_event.set.call_count == 0
    assert sensormond.exit_code == 1

    # Test SIGINT
    daemon_sensormond = sensormond.SensorMonitorDaemon()
    daemon_sensormond.stop_event.set = mock.MagicMock()
    daemon_sensormond.log_info = mock.MagicMock()
    daemon_sensormond.log_warning = mock.MagicMock()
    test_signal = sensormond.signal.SIGINT
    daemon_sensormond.signal_handler(test_signal, None)
    assert daemon_sensormond.log_info.call_count == 1
    daemon_sensormond.log_info.assert_called_with("Caught signal 'SIGINT' - exiting...")
    assert daemon_sensormond.log_warning.call_count == 0
    assert daemon_sensormond.stop_event.set.call_count == 1
    assert sensormond.exit_code == (128 + test_signal)

    # Test SIGTERM
    sensormond.exit_code = 1
    daemon_sensormond = sensormond.SensorMonitorDaemon()
    daemon_sensormond.stop_event.set = mock.MagicMock()
    daemon_sensormond.log_info = mock.MagicMock()
    daemon_sensormond.log_warning = mock.MagicMock()
    test_signal = sensormond.signal.SIGTERM
    daemon_sensormond.signal_handler(test_signal, None)
    assert daemon_sensormond.log_info.call_count == 1
    daemon_sensormond.log_info.assert_called_with("Caught signal 'SIGTERM' - exiting...")
    assert daemon_sensormond.log_warning.call_count == 0
    assert daemon_sensormond.stop_event.set.call_count == 1
    assert sensormond.exit_code == (128 + test_signal)

    # Test an unhandled signal
    sensormond.exit_code = 1
    daemon_sensormond = sensormond.SensorMonitorDaemon()
    daemon_sensormond.stop_event.set = mock.MagicMock()
    daemon_sensormond.log_info = mock.MagicMock()
    daemon_sensormond.log_warning = mock.MagicMock()
    daemon_sensormond.signal_handler(sensormond.signal.SIGUSR1, None)
    assert daemon_sensormond.log_warning.call_count == 1
    daemon_sensormond.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1' - ignoring...")
    assert daemon_sensormond.log_info.call_count == 0
    assert daemon_sensormond.stop_event.set.call_count == 0
    assert sensormond.exit_code == 1


def test_daemon_run():
    daemon_sensormond = sensormond.SensorMonitorDaemon()
    daemon_sensormond.stop_event.wait = mock.MagicMock(return_value=True)
    ret = daemon_sensormond.run()
    assert ret is False

    daemon_sensormond = sensormond.SensorMonitorDaemon()
    daemon_sensormond.stop_event.wait = mock.MagicMock(return_value=False)
    ret = daemon_sensormond.run()
    assert ret is True


def test_try_get():
    def good_callback():
        return 'good result'

    def unimplemented_callback():
        raise NotImplementedError

    ret = sensormond.try_get(good_callback)
    assert ret == 'good result'

    ret = sensormond.try_get(unimplemented_callback)
    assert ret == sensormond.NOT_AVAILABLE

    ret = sensormond.try_get(unimplemented_callback, 'my default')
    assert ret == 'my default'


def test_update_entity_info():
    mock_table = mock.MagicMock()
    mock_vsensor = MockVsensor()
    expected_fvp = sensormond.swsscommon.FieldValuePairs(
        [('position_in_parent', '1'),
         ('parent_name', 'Parent Name')
         ])

    sensormond.update_entity_info(mock_table, 'Parent Name', 'Key Name', mock_vsensor, 1)
    assert mock_table.set.call_count == 1
    mock_table.set.assert_called_with('Key Name', expected_fvp)


@mock.patch('sensormond.SensorMonitorDaemon.run')
def test_main(mock_run):
    mock_run.return_value = False

    ret = sensormond.main()
    assert mock_run.call_count == 1
    assert  ret != 0
