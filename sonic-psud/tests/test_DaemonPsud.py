import datetime
import os
import sys
import pytest

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info.major == 3:
    from unittest import mock
else:
    import mock

from .mock_platform import MockChassis, MockFan, MockPsu

SYSLOG_IDENTIFIER = 'psud_test'
NOT_AVAILABLE = 'N/A'


tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)

from sonic_py_common import daemon_base
daemon_base.db_connect = mock.MagicMock()

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)
import psud


class TestDaemonPsud(object):
    """
    Test cases to cover functionality in DaemonPsud class
    """

    def test_signal_handler(self):
        psud.platform_chassis = MockChassis()
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.stop_event.set = mock.MagicMock()
        daemon_psud.log_info = mock.MagicMock()
        daemon_psud.log_warning = mock.MagicMock()

        # Test SIGHUP
        daemon_psud.signal_handler(psud.signal.SIGHUP, None)
        assert daemon_psud.log_info.call_count == 1
        daemon_psud.log_info.assert_called_with("Caught signal 'SIGHUP' - ignoring...")
        assert daemon_psud.log_warning.call_count == 0
        assert daemon_psud.stop_event.set.call_count == 0
        assert psud.exit_code == 0

        # Reset
        daemon_psud.log_info.reset_mock()
        daemon_psud.log_warning.reset_mock()
        daemon_psud.stop_event.set.reset_mock()

        # Test SIGINT
        test_signal = psud.signal.SIGINT
        daemon_psud.signal_handler(test_signal, None)
        assert daemon_psud.log_info.call_count == 1
        daemon_psud.log_info.assert_called_with("Caught signal 'SIGINT' - exiting...")
        assert daemon_psud.log_warning.call_count == 0
        assert daemon_psud.stop_event.set.call_count == 1
        assert psud.exit_code == (128 + test_signal)

        # Reset
        daemon_psud.log_info.reset_mock()
        daemon_psud.log_warning.reset_mock()
        daemon_psud.stop_event.set.reset_mock()

        # Test SIGTERM
        test_signal = psud.signal.SIGTERM
        daemon_psud.signal_handler(test_signal, None)
        assert daemon_psud.log_info.call_count == 1
        daemon_psud.log_info.assert_called_with("Caught signal 'SIGTERM' - exiting...")
        assert daemon_psud.log_warning.call_count == 0
        assert daemon_psud.stop_event.set.call_count == 1
        assert psud.exit_code == (128 + test_signal)

        # Reset
        daemon_psud.log_info.reset_mock()
        daemon_psud.log_warning.reset_mock()
        daemon_psud.stop_event.set.reset_mock()
        psud.exit_code = 0

        # Test an unhandled signal
        daemon_psud.signal_handler(psud.signal.SIGUSR1, None)
        assert daemon_psud.log_warning.call_count == 1
        daemon_psud.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1' - ignoring...")
        assert daemon_psud.log_info.call_count == 0
        assert daemon_psud.stop_event.set.call_count == 0
        assert psud.exit_code == 0

    def test_run(self):
        psud.platform_chassis = MockChassis()
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud._update_psu_entity_info = mock.MagicMock()
        daemon_psud.update_psu_data = mock.MagicMock()
        daemon_psud.update_pdb_data = mock.MagicMock()
        daemon_psud._update_led_color = mock.MagicMock()
        daemon_psud.update_psu_chassis_info = mock.MagicMock()

        daemon_psud.run()
        assert daemon_psud.first_run is False

    def test_update_psu_data(self):
        mock_psu1 = MockPsu("PSU 1", 0, True, True)
        mock_psu2 = MockPsu("PSU 2", 1, True, True)

        psud.platform_chassis = MockChassis()
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud._update_single_psu_data = mock.MagicMock()
        daemon_psud.log_warning = mock.MagicMock()

        # Test platform_chassis is None
        psud.platform_chassis = None
        daemon_psud.update_psu_data()
        assert daemon_psud._update_single_psu_data.call_count == 0
        assert daemon_psud.log_warning.call_count == 0

        # Test with mocked platform_chassis
        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list = [mock_psu1, mock_psu2]
        daemon_psud.update_psu_data()
        assert daemon_psud._update_single_psu_data.call_count == 2
        expected_calls = [mock.call(mock_psu1), mock.call(mock_psu2)]
        assert daemon_psud._update_single_psu_data.mock_calls == expected_calls
        assert daemon_psud.log_warning.call_count == 0

        daemon_psud._update_single_psu_data.reset_mock()

        # Test _update_single_psu_data() throws exception
        daemon_psud._update_single_psu_data.side_effect = Exception("Test message")
        daemon_psud.update_psu_data()
        assert daemon_psud._update_single_psu_data.call_count == 2
        expected_calls = [mock.call(mock_psu1), mock.call(mock_psu2)]
        assert daemon_psud._update_single_psu_data.mock_calls == expected_calls
        assert daemon_psud.log_warning.call_count == 2
        expected_calls = [mock.call("Failed to update PSU data - Test message")] * 2
        assert daemon_psud.log_warning.mock_calls == expected_calls

    def test_update_pdb_data(self):
        mock_pdb1 = MockPsu("PDB 1", 0, True, True)
        mock_pdb2 = MockPsu("PDB 2", 1, True, True)

        psud.platform_chassis = MockChassis()
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud._update_single_pdb_data = mock.MagicMock()
        daemon_psud.log_warning = mock.MagicMock()

        # Test platform_chassis is None
        psud.platform_chassis = None
        daemon_psud.update_pdb_data()
        assert daemon_psud._update_single_pdb_data.call_count == 0
        assert daemon_psud.log_warning.call_count == 0

        # Test with mocked platform_chassis
        psud.platform_chassis = MockChassis()
        psud.platform_chassis._pdb_list = [mock_pdb1, mock_pdb2]
        daemon_psud.update_pdb_data()
        assert daemon_psud._update_single_pdb_data.call_count == 2
        expected_calls = [mock.call(mock_pdb1), mock.call(mock_pdb2)]
        assert daemon_psud._update_single_pdb_data.mock_calls == expected_calls
        assert daemon_psud.log_warning.call_count == 0

        daemon_psud._update_single_pdb_data.reset_mock()

        # Test _update_single_pdb_data() throws exception
        daemon_psud._update_single_pdb_data.side_effect = Exception("Test message")
        daemon_psud.update_pdb_data()
        assert daemon_psud._update_single_pdb_data.call_count == 2
        expected_calls = [mock.call(mock_pdb1), mock.call(mock_pdb2)]
        assert daemon_psud._update_single_pdb_data.mock_calls == expected_calls
        assert daemon_psud.log_warning.call_count == 2
        expected_calls = [mock.call("Failed to update PDB data - Test message")] * 2
        assert daemon_psud.log_warning.mock_calls == expected_calls

    # Fixed timestamp for FieldValuePairs matching (psud uses datetime.now().timestamp())
    _FIXED_PSU_TS = 1234567890.0

    def _construct_expected_fvp(self, power=100.0, power_warning_suppress_threshold='N/A', power_critical_threshold='N/A', power_overload=False,
                                timestamp=None):
        if timestamp is None:
            timestamp = self._FIXED_PSU_TS
        expected_fvp = psud.swsscommon.FieldValuePairs(
            [(psud.PSU_INFO_MODEL_FIELD, 'Fake Model'),
             (psud.PSU_INFO_SERIAL_FIELD, '12345678'),
             (psud.PSU_INFO_REV_FIELD, '1234'),
             (psud.PSU_INFO_TEMP_FIELD, '30.0'),
             (psud.PSU_INFO_TEMP_TH_FIELD, '50.0'),
             (psud.PSU_INFO_VOLTAGE_FIELD, '12.0'),
             (psud.PSU_INFO_VOLTAGE_MIN_TH_FIELD, '11.0'),
             (psud.PSU_INFO_VOLTAGE_MAX_TH_FIELD, '13.0'),
             (psud.PSU_INFO_CURRENT_FIELD, '8.0'),
             (psud.PSU_INFO_POWER_FIELD, str(power)),
             (psud.PSU_INFO_POWER_WARNING_SUPPRESS_THRESHOLD, str(power_warning_suppress_threshold)),
             (psud.PSU_INFO_POWER_CRITICAL_THRESHOLD, str(power_critical_threshold)),
             (psud.PSU_INFO_POWER_OVERLOAD, str(power_overload)),
             (psud.PSU_INFO_FRU_FIELD, 'True'),
             (psud.PSU_INFO_IN_VOLTAGE_FIELD, '220.25'),
             (psud.PSU_INFO_IN_CURRENT_FIELD, '0.72'),
             (psud.PSU_INFO_POWER_MAX_FIELD, 'N/A'),
             (psud.PSU_INFO_PRESENCE_FIELD, 'true'),
             (psud.PSU_INFO_STATUS_FIELD, 'true'),
             (psud.PSU_INFO_TIMESTAMP_FIELD, str(timestamp)),
             ])
        return expected_fvp

    @mock.patch('psud.datetime')
    @mock.patch('psud._wrapper_get_psu_presence', mock.MagicMock())
    @mock.patch('psud._wrapper_get_psu_status', mock.MagicMock())
    def test_update_single_psu_data(self, mock_datetime):
        mock_now = mock.MagicMock()
        mock_now.timestamp.return_value = self._FIXED_PSU_TS
        mock_datetime.now.return_value = mock_now

        psud._wrapper_get_psu_presence.return_value = True
        psud._wrapper_get_psu_status.return_value = True

        psu1 = MockPsu('PSU 1', 0, True, 'Fake Model', '12345678', '1234')
        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list.append(psu1)

        expected_fvp = self._construct_expected_fvp()

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.psu_tbl = mock.MagicMock()
        daemon_psud._update_single_psu_data(psu1)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        assert not daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold

    @mock.patch('psud.datetime')
    @mock.patch('psud.daemon_base.db_connect', mock.MagicMock())
    def test_power_threshold(self, mock_datetime):
        mock_now = mock.MagicMock()
        mock_now.timestamp.return_value = self._FIXED_PSU_TS
        mock_datetime.now.return_value = mock_now

        psu = MockPsu('PSU 1', 0, True, 'Fake Model', '12345678', '1234')
        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list.append(psu)
        another_psu = MockPsu('PSU 2', 0, True, 'Fake Model', '12345678', '1234')
        another_psu.set_power(10.0)
        psud.platform_chassis._psu_list.append(another_psu)

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        daemon_psud.psu_tbl = mock.MagicMock()
        psu.get_psu_power_critical_threshold = mock.MagicMock(return_value=130.0)
        psu.get_psu_power_warning_suppress_threshold = mock.MagicMock(return_value=120.0)

        # Normal start. All good and all thresholds are supported
        # Power is in normal range (below warning threshold)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(100.0, 120.0, 130.0, False)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        daemon_psud._update_led_color()
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        daemon_psud.first_run = False

        # Power is increasing across the warning threshold
        # Normal => (warning, critical)
        psu.set_power(115.0)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(115.0, 120.0, 130.0, False)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        daemon_psud._update_led_color()
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # Power is increasing across the critical threshold. Alarm raised
        # (warning, critical) => (critical, )
        psu.set_power(125.0)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(125.0, 120.0, 130.0, True)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        daemon_psud._update_led_color()
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # Power is decreasing across the critical threshold. Alarm not cleared
        # (critical, ) => (warning, critical)
        psu.set_power(115.0)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(115.0, 120.0, 130.0, True)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        daemon_psud._update_led_color()
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # Power is decreasing across the warning threshold. Alarm cleared
        # (warning, critical) => Normal
        psu.set_power(105.0)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(105.0, 120.0, 130.0, False)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()
        daemon_psud._update_led_color()

        # Power is increasing across the critical threshold. Alarm raised
        # Normal => (critical, )
        psu.set_power(125.0)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(125.0, 120.0, 130.0, True)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        daemon_psud._update_led_color()
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # Power is increasing across the critical threshold. Alarm raised
        # (critical, ) => Normal
        psu.set_power(105.0)
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        expected_fvp = self._construct_expected_fvp(105.0, 120.0, 130.0, False)
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        daemon_psud._update_led_color()
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # PSU power becomes down
        psu.set_status(False)
        daemon_psud._update_single_psu_data(psu)
        daemon_psud._update_led_color()
        assert not daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        assert psu.STATUS_LED_COLOR_RED == psu.get_status_led()

        # PSU power becomes up
        psu.set_status(True)
        daemon_psud._update_single_psu_data(psu)
        daemon_psud._update_led_color()
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # PSU becomes absent
        psu.set_presence(False)
        daemon_psud._update_single_psu_data(psu)
        daemon_psud._update_led_color()
        assert not daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        assert psu.STATUS_LED_COLOR_RED == psu.get_status_led()

        # PSU becomes present
        psu.set_presence(True)
        daemon_psud._update_single_psu_data(psu)
        daemon_psud._update_led_color()
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        assert psu.STATUS_LED_COLOR_GREEN == psu.get_status_led()

        # Thresholds become invalid on the fly
        psu.get_psu_power_critical_threshold = mock.MagicMock(side_effect=NotImplementedError(''))
        daemon_psud._update_single_psu_data(psu)
        assert not daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        psu.get_psu_power_critical_threshold = mock.MagicMock(return_value=120.0)
        daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold = True
        daemon_psud._update_single_psu_data(psu)
        assert daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold
        psu.get_psu_power_warning_suppress_threshold = mock.MagicMock(side_effect=NotImplementedError(''))
        daemon_psud._update_single_psu_data(psu)
        assert not daemon_psud.psu_status_dict['PSU 1'].check_psu_power_threshold
        assert not daemon_psud.psu_status_dict['PSU 1'].power_exceeded_threshold

    def test_set_psu_led(self):
        mock_logger = mock.MagicMock()
        mock_psu = MockPsu("PSU 1", 0, True, True)
        psu_status = psud.PsuStatus(mock_logger, mock_psu, 1)

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_GREEN

        psu_status.presence = False
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_RED

        psu_status.presence = True
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_GREEN

        psu_status.power_good = False
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_RED

        psu_status.power_good = True
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_GREEN

        psu_status.voltage_good = False
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_RED

        psu_status.voltage_good = True
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_GREEN

        psu_status.temperature_good = False
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_RED

        psu_status.temperature_good = True
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert mock_psu.get_status_led() == mock_psu.STATUS_LED_COLOR_GREEN

        # Test set_status_led not implemented
        mock_psu.set_status_led = mock.MagicMock(side_effect=NotImplementedError)
        daemon_psud.log_warning = mock.MagicMock()
        daemon_psud._set_psu_led(mock_psu, psu_status)
        assert daemon_psud.log_warning.call_count == 1
        daemon_psud.log_warning.assert_called_with("set_status_led() not implemented")

    def test_update_led_color(self):
        mock_psu = MockPsu("PSU 1", 0, True, True)
        mock_logger = mock.MagicMock()
        psu_status = psud.PsuStatus(mock_logger, mock_psu, 1)

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.psu_tbl = mock.MagicMock()
        daemon_psud._update_psu_fan_led_status = mock.MagicMock()

        # If psud.platform_chassis is None, _update_psu_fan_led_status() should do nothing
        psud.platform_chassis = None
        daemon_psud._update_led_color()
        assert daemon_psud.psu_tbl.set.call_count == 0
        assert daemon_psud._update_psu_fan_led_status.call_count == 0

        psud.platform_chassis = MockChassis()
        daemon_psud.psu_status_dict['PSU 1'] = psu_status
        expected_fvp = psud.swsscommon.FieldValuePairs([('led_status', MockPsu.STATUS_LED_COLOR_OFF)])
        daemon_psud._update_led_color()
        assert daemon_psud.psu_tbl.set.call_count == 1
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        assert daemon_psud._update_psu_fan_led_status.call_count == 1
        daemon_psud._update_psu_fan_led_status.assert_called_with(mock_psu, 'PSU 1')

        daemon_psud.psu_tbl.reset_mock()
        daemon_psud._update_psu_fan_led_status.reset_mock()

        mock_psu.set_status_led(MockPsu.STATUS_LED_COLOR_GREEN)
        expected_fvp = psud.swsscommon.FieldValuePairs([('led_status', MockPsu.STATUS_LED_COLOR_GREEN)])
        daemon_psud._update_led_color()
        assert daemon_psud.psu_tbl.set.call_count == 1
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        assert daemon_psud._update_psu_fan_led_status.call_count == 1
        daemon_psud._update_psu_fan_led_status.assert_called_with(mock_psu, 'PSU 1')

        daemon_psud.psu_tbl.reset_mock()
        daemon_psud._update_psu_fan_led_status.reset_mock()

        mock_psu.set_status_led(MockPsu.STATUS_LED_COLOR_RED)
        expected_fvp = psud.swsscommon.FieldValuePairs([('led_status', MockPsu.STATUS_LED_COLOR_RED)])
        daemon_psud._update_led_color()
        assert daemon_psud.psu_tbl.set.call_count == 1
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        assert daemon_psud._update_psu_fan_led_status.call_count == 1
        daemon_psud._update_psu_fan_led_status.assert_called_with(mock_psu, 'PSU 1')

        daemon_psud.psu_tbl.reset_mock()
        daemon_psud._update_psu_fan_led_status.reset_mock()

        # Test exception handling
        mock_psu.get_status_led = mock.Mock(side_effect=NotImplementedError)
        expected_fvp = psud.swsscommon.FieldValuePairs([('led_status', psud.NOT_AVAILABLE)])
        daemon_psud._update_led_color()
        assert daemon_psud.psu_tbl.set.call_count == 1
        daemon_psud.psu_tbl.set.assert_called_with('PSU 1', expected_fvp)
        assert daemon_psud._update_psu_fan_led_status.call_count == 1
        daemon_psud._update_psu_fan_led_status.assert_called_with(mock_psu, 'PSU 1')

    def test_update_psu_fan_led_status(self):
        mock_fan = MockFan("PSU 1 Test Fan 1", MockFan.FAN_DIRECTION_INTAKE)
        mock_psu = MockPsu("PSU 1", 0, True, True)
        mock_psu._fan_list = [mock_fan]
        mock_logger = mock.MagicMock()

        psud.platform_chassis = MockChassis()

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.fan_tbl = mock.MagicMock()

        expected_fvp = psud.swsscommon.FieldValuePairs([(psud.FAN_INFO_LED_STATUS_FIELD, MockFan.STATUS_LED_COLOR_OFF)])
        daemon_psud._update_psu_fan_led_status(mock_psu, 'PSU 1')
        assert daemon_psud.fan_tbl.set.call_count == 1
        daemon_psud.fan_tbl.set.assert_called_with("PSU 1 Test Fan 1", expected_fvp)

        daemon_psud.fan_tbl.set.reset_mock()

        # Test Fan.get_status_led not implemented
        mock_fan.get_status_led = mock.Mock(side_effect=NotImplementedError)
        expected_fvp = psud.swsscommon.FieldValuePairs([(psud.FAN_INFO_LED_STATUS_FIELD, psud.NOT_AVAILABLE)])
        daemon_psud._update_psu_fan_led_status(mock_psu, 'PSU 1')
        assert daemon_psud.fan_tbl.set.call_count == 1
        daemon_psud.fan_tbl.set.assert_called_with("PSU 1 Test Fan 1", expected_fvp)

        daemon_psud.fan_tbl.set.reset_mock()

        # Test Fan.get_name not implemented
        mock_fan.get_name = mock.Mock(side_effect=NotImplementedError)
        daemon_psud._update_psu_fan_led_status(mock_psu, 'PSU 1')
        assert daemon_psud.fan_tbl.set.call_count == 1
        daemon_psud.fan_tbl.set.assert_called_with("PSU 1 FAN 1", expected_fvp)

    @mock.patch('psud.PsuChassisInfo', mock.MagicMock())
    def test_update_psu_chassis_info(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        # If psud.platform_chassis is None, update_psu_chassis_info() should do nothing
        psud.platform_chassis = None
        daemon_psud.psu_chassis_info = None
        daemon_psud.update_psu_chassis_info()
        assert daemon_psud.psu_chassis_info is None

        # Now we mock platform_chassis, so that daemon_psud.psu_chassis_info should be instantiated and run_power_budget() should be called
        psud.platform_chassis = MockChassis()
        daemon_psud.update_psu_chassis_info()
        assert daemon_psud.psu_chassis_info is not None
        assert daemon_psud.psu_chassis_info.run_power_budget.call_count == 1

    def test_update_psu_entity_info(self):
        mock_psu1 = MockPsu("PSU 1", 0, True, True)
        mock_psu2 = MockPsu("PSU 2", 1, True, True)

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud._update_single_psu_entity_info = mock.MagicMock()

        # If psud.platform_chassis is None, _update_psu_entity_info() should do nothing
        psud.platform_chassis = None
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 0

        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list = [mock_psu1]
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 1
        daemon_psud._update_single_psu_entity_info.assert_called_with(mock_psu1)

        daemon_psud._update_single_psu_entity_info.reset_mock()
        psud.platform_chassis._psu_list = [mock_psu1, mock_psu2]
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 2
        expected_calls = [mock.call(mock_psu1), mock.call(mock_psu2)]
        assert daemon_psud._update_single_psu_entity_info.mock_calls == expected_calls

        # Test behavior if _update_single_psu_entity_info raises an exception
        daemon_psud._update_single_psu_entity_info.reset_mock()
        daemon_psud._update_single_psu_entity_info.side_effect = Exception("Test message")
        daemon_psud.log_warning = mock.MagicMock()
        psud.platform_chassis._psu_list = [mock_psu1]
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 1
        daemon_psud._update_single_psu_entity_info.assert_called_with(mock_psu1)
        assert daemon_psud.log_warning.call_count == 1
        daemon_psud.log_warning.assert_called_with("Failed to update PSU entity data - Test message")

    def test_update_single_psu_entity_info(self):
        #creating psu object in slot not used to allow for name specific check
        mock_psu1 = MockPsu("PSU 3", 2, True, True)
        expected_fvp = psud.swsscommon.FieldValuePairs(
            [('position_in_parent', '2'),
             ('parent_name', psud.CHASSIS_INFO_KEY),
             ])

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.phy_entity_tbl = mock.MagicMock()

        daemon_psud._update_single_psu_entity_info(mock_psu1)
        daemon_psud.phy_entity_tbl.set.assert_called_with('PSU 3', expected_fvp)

    def test_deinit(self):
        psud.platform_chassis = MockChassis()
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.num_psus = 2
        daemon_psud.psu_tbl = psud.swsscommon.Table("STATE_DB", "psu_table")
        daemon_psud.psu_tbl._del = mock.MagicMock()
        daemon_psud.phy_entity_tbl = psud.swsscommon.Table("STATE_DB", "phy_entity_table")
        # Pre-populate physical entity table with PSU entries and extra entries
        daemon_psud.phy_entity_tbl.mock_dict['PSU 1'] = {}
        daemon_psud.phy_entity_tbl.mock_dict['PSU 2'] = {}
        daemon_psud.phy_entity_tbl.mock_dict['FAN 1'] = {}  # Extra entry not owned by psud
        daemon_psud.phy_entity_tbl._del = mock.MagicMock()
        daemon_psud.chassis_tbl = psud.swsscommon.Table("STATE_DB", "chassis_table")
        daemon_psud.chassis_tbl._del = mock.MagicMock()

        daemon_psud.__del__()

        # Verify PSU table entries are deleted for all PSUs
        assert daemon_psud.psu_tbl._del.call_count == 2
        expected_psu_calls = [mock.call('PSU 1'), mock.call('PSU 2')]
        daemon_psud.psu_tbl._del.assert_has_calls(expected_psu_calls, any_order=True)

        # Verify only physical entity entries for PSUs are deleted (not FAN 1)
        assert daemon_psud.phy_entity_tbl._del.call_count == 2
        daemon_psud.phy_entity_tbl._del.assert_has_calls(expected_psu_calls, any_order=True)

        # Verify chassis table entries are deleted
        assert daemon_psud.chassis_tbl._del.call_count == 2
        expected_chassis_calls = [
            mock.call(psud.CHASSIS_INFO_KEY),
            mock.call(psud.CHASSIS_INFO_POWER_KEY_TEMPLATE.format(1))
        ]
        daemon_psud.chassis_tbl._del.assert_has_calls(expected_chassis_calls, any_order=True)

    @mock.patch('psud.datetime')
    def test_update_psu_fan_data(self, mock_datetime):
        fake_time = datetime.datetime(2021, 1, 1, 12, 34, 56)
        mock_datetime.now.return_value = fake_time

        mock_fan = MockFan("PSU 1 Test Fan 1", MockFan.FAN_DIRECTION_INTAKE)
        mock_psu1 = MockPsu("PSU 1", 0, True, True)
        mock_psu1._fan_list = [mock_fan]
        mock_logger = mock.MagicMock()

        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list = [mock_psu1]

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.fan_tbl = mock.MagicMock()

        expected_fvp = psud.swsscommon.FieldValuePairs(
            [(psud.FAN_INFO_PRESENCE_FIELD, "True"),
             (psud.FAN_INFO_STATUS_FIELD, "True"),
             (psud.FAN_INFO_DIRECTION_FIELD, mock_fan.get_direction()),
             (psud.FAN_INFO_SPEED_FIELD, str(mock_fan.get_speed())),
             (psud.FAN_INFO_TIMESTAMP_FIELD, fake_time.strftime('%Y%m%d %H:%M:%S'))
             ])
        daemon_psud._update_psu_fan_data(mock_psu1)
        assert daemon_psud.fan_tbl.set.call_count == 1
        daemon_psud.fan_tbl.set.assert_called_with("PSU 1 Test Fan 1", expected_fvp)

        daemon_psud.fan_tbl.set.reset_mock()

    @mock.patch('psud._wrapper_get_psu_presence', mock.MagicMock(return_value=True))
    @mock.patch('psud._wrapper_get_psu_status', mock.MagicMock(return_value=True))
    def test_update_single_psu_data_db_error(self):
        """Test that RuntimeError from DB operations is handled gracefully"""
        psu1 = MockPsu('PSU 1', 0, True, 'Fake Model', '12345678', '1234')
        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list.append(psu1)

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.psu_tbl = mock.MagicMock()
        daemon_psud.log_error = mock.MagicMock()

        # Simulate Redis BUSY error
        daemon_psud.psu_tbl.set.side_effect = RuntimeError("BUSY Redis is busy running a script")

        # Should not raise exception, should log error instead
        daemon_psud._update_single_psu_data(psu1)

        assert daemon_psud.psu_tbl.set.call_count == 1
        assert daemon_psud.log_error.call_count == 1
        daemon_psud.log_error.assert_called_with(
            "Failed to update {} info to DB: BUSY Redis is busy running a script".format('PSU 1')
        )

    def test_update_led_color_db_error(self):
        """Test that RuntimeError from DB operations in _update_led_color is handled gracefully"""
        mock_psu = MockPsu("PSU 1", 0, True, True)
        mock_logger = mock.MagicMock()
        psu_status = psud.PsuStatus(mock_logger, mock_psu, 1)

        psud.platform_chassis = MockChassis()
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.psu_tbl = mock.MagicMock()
        daemon_psud._update_psu_fan_led_status = mock.MagicMock()
        daemon_psud.log_error = mock.MagicMock()
        daemon_psud.psu_status_dict['PSU 1'] = psu_status

        # Simulate Redis BUSY error
        daemon_psud.psu_tbl.set.side_effect = RuntimeError("BUSY Redis is busy running a script")

        # Should not raise exception, should log error and continue to update fan LED status
        daemon_psud._update_led_color()

        assert daemon_psud.psu_tbl.set.call_count == 1
        assert daemon_psud.log_error.call_count == 1
        # Should still call _update_psu_fan_led_status even after DB error
        assert daemon_psud._update_psu_fan_led_status.call_count == 1

    @mock.patch('psud.datetime')
    def test_update_psu_fan_data_db_error(self, mock_datetime):
        """Test that RuntimeError from DB operations in _update_psu_fan_data is handled gracefully"""
        fake_time = datetime.datetime(2021, 1, 1, 12, 34, 56)
        mock_datetime.now.return_value = fake_time

        mock_fan = MockFan("PSU 1 Test Fan 1", MockFan.FAN_DIRECTION_INTAKE)
        mock_psu1 = MockPsu("PSU 1", 0, True, True)
        mock_psu1._fan_list = [mock_fan]

        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list = [mock_psu1]

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.fan_tbl = mock.MagicMock()
        daemon_psud.log_error = mock.MagicMock()

        # Simulate Redis BUSY error
        daemon_psud.fan_tbl.set.side_effect = RuntimeError("BUSY Redis is busy running a script")

        # Should not raise exception, should log error instead
        daemon_psud._update_psu_fan_data(mock_psu1)

        assert daemon_psud.fan_tbl.set.call_count == 1
        assert daemon_psud.log_error.call_count == 1
        daemon_psud.log_error.assert_called_with(
            "Failed to update fan {} info to DB: BUSY Redis is busy running a script".format("PSU 1 Test Fan 1")
        )

    def test_update_psu_fan_led_status_db_error(self):
        """Test that RuntimeError from DB operations in _update_psu_fan_led_status is handled gracefully"""
        mock_fan = MockFan("PSU 1 Test Fan 1", MockFan.FAN_DIRECTION_INTAKE)
        mock_psu = MockPsu("PSU 1", 0, True, True)
        mock_psu._fan_list = [mock_fan]

        psud.platform_chassis = MockChassis()

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.fan_tbl = mock.MagicMock()
        daemon_psud.log_error = mock.MagicMock()

        # Simulate Redis BUSY error
        daemon_psud.fan_tbl.set.side_effect = RuntimeError("BUSY Redis is busy running a script")

        # Should not raise exception, should log error instead
        daemon_psud._update_psu_fan_led_status(mock_psu, 'PSU 1')

        assert daemon_psud.fan_tbl.set.call_count == 1
        assert daemon_psud.log_error.call_count == 1
        daemon_psud.log_error.assert_called_with(
            "Failed to update fan {} LED status to DB: BUSY Redis is busy running a script".format("PSU 1 Test Fan 1")
        )

    def test_update_single_psu_entity_info_db_error(self):
        """Test that RuntimeError from DB operations in _update_single_psu_entity_info is handled gracefully"""
        mock_psu1 = MockPsu("PSU 1", 0, True, True)

        psud.platform_chassis = MockChassis()

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.phy_entity_tbl = mock.MagicMock()
        daemon_psud.log_error = mock.MagicMock()

        # Simulate Redis BUSY error
        daemon_psud.phy_entity_tbl.set.side_effect = RuntimeError("BUSY Redis is busy running a script")

        # Should not raise exception, should log error instead
        daemon_psud._update_single_psu_entity_info(mock_psu1)

        assert daemon_psud.phy_entity_tbl.set.call_count == 1
        assert daemon_psud.log_error.call_count == 1
        daemon_psud.log_error.assert_called_with(
            "Failed to update PSU 1 entity info to DB: BUSY Redis is busy running a script"
        )

    @mock.patch('psud.swsscommon.Table')
    @mock.patch('psud.daemon_base.db_connect')
    def test_init_state_db_connect_error(self, mock_db_connect, mock_table):
        """Test that exception when connecting to STATE_DB leads to sys.exit(PSU_DB_CONNECT_ERROR)"""
        psud.platform_chassis = MockChassis()

        # Simulate exception when creating Table for STATE_DB
        mock_table.side_effect = Exception("Connection refused")

        with pytest.raises(SystemExit) as exc_info:
            psud.DaemonPsud(SYSLOG_IDENTIFIER)

        assert exc_info.value.code == psud.PSU_DB_CONNECT_ERROR

    @mock.patch('psud.swsscommon.Table')
    @mock.patch('psud.daemon_base.db_connect')
    def test_init_set_psu_number_db_error(self, mock_db_connect, mock_table):
        """Test that RuntimeError when setting PSU number to DB is handled gracefully"""
        psud.platform_chassis = MockChassis()

        mock_chassis_tbl = mock.MagicMock()
        mock_psu_tbl = mock.MagicMock()
        mock_fan_tbl = mock.MagicMock()
        mock_phy_entity_tbl = mock.MagicMock()

        # Return different mock tables for each Table() call
        mock_table.side_effect = [mock_chassis_tbl, mock_psu_tbl, mock_fan_tbl, mock_phy_entity_tbl]

        # Simulate RuntimeError when setting PSU number
        mock_chassis_tbl.set.side_effect = RuntimeError("BUSY Redis is busy running a script")

        # Patch log_error on the class to capture the error logging during __init__
        with mock.patch.object(psud.DaemonPsud, 'log_error') as mock_log_error:
            # Should not raise exception, should just log error
            daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

            # Verify that the error was logged
            mock_log_error.assert_called_with("Failed to set PSU/PDB number to DB: BUSY Redis is busy running a script")

    def test_del_chassis_info_db_error(self):
        """Test that exception when deleting chassis info from DB in __del__ is handled gracefully (no raise)."""
        psud.platform_chassis = MockChassis()

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        # Mock the chassis_tbl._del to raise exception (e.g. KeyError when key missing in test mock)
        daemon_psud.chassis_tbl = mock.MagicMock()
        daemon_psud.chassis_tbl._del.side_effect = Exception("Failed to delete")

        # Mock psu_tbl to avoid errors when deleting PSU info
        daemon_psud.psu_tbl = mock.MagicMock()

        # __del__ must not raise; it swallows exceptions to avoid issues during interpreter teardown
        daemon_psud.__del__()

    def test_update_psu_data_not_implemented(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        ch = MockChassis()
        ch.get_all_psus = mock.MagicMock(side_effect=NotImplementedError)
        psud.platform_chassis = ch
        daemon_psud._update_single_psu_data = mock.MagicMock()
        daemon_psud.update_psu_data()
        daemon_psud._update_single_psu_data.assert_not_called()

    def test_update_pdb_data_not_implemented(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        ch = MockChassis()
        ch.get_all_pdbs = mock.MagicMock(side_effect=NotImplementedError)
        psud.platform_chassis = ch
        daemon_psud._update_single_pdb_data = mock.MagicMock()
        daemon_psud.update_pdb_data()
        daemon_psud._update_single_pdb_data.assert_not_called()

    def test_update_psu_entity_info_pdbs_and_errors(self):
        mock_pdb = MockPsu("PDB 1", 0, True, True)
        psud.platform_chassis = MockChassis()
        psud.platform_chassis._pdb_list = [mock_pdb]

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud._update_single_psu_entity_info = mock.MagicMock()
        daemon_psud._update_psu_entity_info()
        daemon_psud._update_single_psu_entity_info.assert_called_with(mock_pdb)

        daemon_psud._update_single_psu_entity_info.reset_mock()
        daemon_psud._update_single_psu_entity_info = mock.MagicMock()
        psud.platform_chassis.get_all_pdbs = mock.MagicMock(side_effect=NotImplementedError)
        daemon_psud._update_psu_entity_info()
        daemon_psud._update_single_psu_entity_info.assert_not_called()

        psud.platform_chassis = MockChassis()
        psud.platform_chassis._pdb_list = [mock_pdb]
        daemon_psud._update_single_psu_entity_info = mock.MagicMock(side_effect=Exception("pdb err"))
        daemon_psud.log_warning = mock.MagicMock()
        daemon_psud._update_psu_entity_info()
        assert daemon_psud.log_warning.call_count == 1
        daemon_psud.log_warning.assert_called_with("Failed to update PDB entity data - pdb err")

    @mock.patch('psud.swsscommon.Table')
    @mock.patch('psud.daemon_base.db_connect')
    def test_init_skips_name_list_when_psu_pdb_enumeration_not_implemented(self, mock_db_connect, mock_table):
        """Chassis counts succeed but listing PSUs/PDBs raises: STATE_DB counts still posted, names list empty."""
        ch = mock.MagicMock()
        ch.get_num_psus.return_value = 2
        ch.get_all_psus.side_effect = NotImplementedError
        ch.get_num_pdbs.return_value = 1
        ch.get_all_pdbs.side_effect = NotImplementedError
        psud.platform_chassis = ch

        mock_chassis_tbl = mock.MagicMock()
        mock_psu_tbl = mock.MagicMock()
        mock_fan_tbl = mock.MagicMock()
        mock_phy_entity_tbl = mock.MagicMock()
        mock_table.side_effect = [mock_chassis_tbl, mock_psu_tbl, mock_fan_tbl, mock_phy_entity_tbl]

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        assert daemon_psud.all_power_entity_names == []
        mock_chassis_tbl.set.assert_called()

    @mock.patch('psud.swsscommon.Table')
    @mock.patch('psud.daemon_base.db_connect')
    def test_del_deletes_by_psu_key_when_entity_names_empty(self, mock_db_connect, mock_table):
        ch = mock.MagicMock()
        ch.get_num_psus.return_value = 2
        ch.get_num_pdbs.return_value = 0
        ch.get_all_psus.side_effect = NotImplementedError
        ch.get_all_pdbs.side_effect = NotImplementedError
        psud.platform_chassis = ch

        mock_chassis_tbl = mock.MagicMock()
        mock_psu_tbl = mock.MagicMock()
        mock_fan_tbl = mock.MagicMock()
        mock_phy_entity_tbl = mock.MagicMock()
        mock_table.side_effect = [mock_chassis_tbl, mock_psu_tbl, mock_fan_tbl, mock_phy_entity_tbl]

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        key_chassis = MockChassis()
        key_chassis._psu_list = [
            MockPsu("PSU 1", 0, True, True),
            MockPsu("PSU 2", 1, True, True),
        ]
        psud.platform_chassis = key_chassis
        daemon_psud.__del__()
        assert mock_psu_tbl._del.call_count == 2
        mock_psu_tbl._del.assert_has_calls(
            [mock.call("PSU 1"), mock.call("PSU 2")], any_order=True)

    @mock.patch('psud.datetime')
    def test_update_single_pdb_fallback_name_and_first_run_absent(self, mock_datetime):
        mock_now = mock.MagicMock()
        mock_now.timestamp.return_value = self._FIXED_PSU_TS
        mock_datetime.now.return_value = mock_now

        # Use MockPsu so _update_single_power_entity_data has all required APIs
        # (is_replaceable, STATUS_LED_COLOR_*, get_model, etc.); force fallback name via get_name.
        pdb_ent = MockPsu('ignored', 2, False, True)
        pdb_ent.index = 2
        pdb_ent.get_name = mock.MagicMock(side_effect=RuntimeError('no name'))
        psud.platform_chassis = MockChassis()

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.psu_tbl = mock.MagicMock()
        daemon_psud.log_error = mock.MagicMock()
        daemon_psud.first_run = True
        daemon_psud._update_single_pdb_data(pdb_ent)
        daemon_psud.log_error.assert_called_once_with('PDB 2 is not present.')
        daemon_psud.psu_tbl.set.assert_called_once()
        call_name, _fvs = daemon_psud.psu_tbl.set.call_args[0]
        assert call_name == 'PDB 2'

    @mock.patch('psud.datetime')
    def test_system_power_threshold_includes_other_pdbs(self, mock_datetime):
        mock_now = mock.MagicMock()
        mock_now.timestamp.return_value = self._FIXED_PSU_TS
        mock_datetime.now.return_value = mock_now

        psu = MockPsu('PSU 1', 0, True, 'Fake Model', '12345678', '1234')
        psu.set_power(80.0)
        psu.get_psu_power_critical_threshold = mock.MagicMock(return_value=160.0)
        psu.get_psu_power_warning_suppress_threshold = mock.MagicMock(return_value=100.0)

        pdb_other = MockPsu('PDB 1', 0, True, 'Fake Model', '12345678', '1234')
        pdb_other.set_power(90.0)

        psud.platform_chassis = MockChassis()
        psud.platform_chassis._psu_list = [psu]
        psud.platform_chassis._pdb_list = [pdb_other]

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.psu_tbl = mock.MagicMock()
        # system_power = 80 + 90 (other PDB) exceeds critical threshold 160
        daemon_psud._update_single_psu_data(psu)
        status = daemon_psud.psu_status_dict['PSU 1']
        assert status.check_psu_power_threshold
        assert status.power_exceeded_threshold
