import os
import sys
from imp import load_source

import pytest

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info.major == 3:
    from unittest import mock
else:
    import mock
from sonic_py_common import daemon_base

from . import mock_swsscommon
from .mock_platform import MockChassis, MockPsu, MockFanDrawer, MockModule

SYSLOG_IDENTIFIER = 'psud_test'
NOT_AVAILABLE = 'N/A'

daemon_base.db_connect = mock.MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

os.environ["PSUD_UNIT_TESTING"] = "1"
load_source('psud', scripts_path + '/psud')
import psud

class TestDaemonPsud(object):
    """
    Test cases to cover functionality in DaemonPsud class
    """

    def test_signal_handler(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.stop.set = mock.MagicMock()
        daemon_psud.log_info = mock.MagicMock()
        daemon_psud.log_warning = mock.MagicMock()

        # Test SIGHUP
        daemon_psud.signal_handler(psud.signal.SIGHUP, None)
        assert daemon_psud.log_info.call_count == 1
        daemon_psud.log_info.assert_called_with("Caught SIGHUP - ignoring...")
        assert daemon_psud.log_warning.call_count == 0
        assert daemon_psud.stop.set.call_count == 0

        # Test SIGINT
        daemon_psud.log_info.reset_mock()
        daemon_psud.log_warning.reset_mock()
        daemon_psud.stop.set.reset_mock()
        daemon_psud.signal_handler(psud.signal.SIGINT, None)
        assert daemon_psud.log_info.call_count == 1
        daemon_psud.log_info.assert_called_with("Caught SIGINT - exiting...")
        assert daemon_psud.log_warning.call_count == 0
        assert daemon_psud.stop.set.call_count == 1

        # Test SIGTERM
        daemon_psud.log_info.reset_mock()
        daemon_psud.log_warning.reset_mock()
        daemon_psud.stop.set.reset_mock()
        daemon_psud.signal_handler(psud.signal.SIGTERM, None)
        assert daemon_psud.log_info.call_count == 1
        daemon_psud.log_info.assert_called_with("Caught SIGTERM - exiting...")
        assert daemon_psud.log_warning.call_count == 0
        assert daemon_psud.stop.set.call_count == 1

        # Test an unhandled signal
        daemon_psud.log_info.reset_mock()
        daemon_psud.log_warning.reset_mock()
        daemon_psud.stop.set.reset_mock()
        daemon_psud.signal_handler(psud.signal.SIGUSR1, None)
        assert daemon_psud.log_warning.call_count == 1
        daemon_psud.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1'")
        assert daemon_psud.log_info.call_count == 0
        assert daemon_psud.stop.set.call_count == 0

    def test_set_psu_led(self):
        mock_logger = mock.MagicMock()
        mock_psu = MockPsu(True, True, "PSU 1")
        psu_status = psud.PsuStatus(mock_logger, mock_psu)

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        psu_status.presence = True
        psu_status.power_good = True
        psu_status.voltage_good = True
        psu_status.temperature_good = True
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

    def test_update_psu_chassis_info(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        # We mock the actual implementation of psud.PsuChassisInfo because it will be instantiated by daemon_psud.update_psu_chassis_info()
        with mock.patch("psud.PsuChassisInfo", mock.MagicMock()) as mock_psu_chassis_info:
            # If daemon_psud.platform_chassis is None, update_psu_chassis_info() should do nothing
            psud.platform_chassis = None
            daemon_psud.psu_chassis_info = None
            daemon_psud.update_psu_chassis_info(None)
            assert daemon_psud.psu_chassis_info is None

            # Now we mock platform_chassis, so that daemon_psud.psu_chassis_info should be instantiated and run_power_budget() should be called
            psud.platform_chassis = MockChassis()
            daemon_psud.update_psu_chassis_info(None)
            assert daemon_psud.psu_chassis_info is not None
            assert daemon_psud.psu_chassis_info.run_power_budget.call_count == 1
            daemon_psud.psu_chassis_info.run_power_budget.assert_called_with(None)

    def test_update_master_led_color(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)

        # If daemon_psud.platform_chassis or daemon_psud.psu_chassis_info is None, update_master_led_color() should do nothing
        psud.platform_chassis = None
        daemon_psud.psu_chassis_info = mock.MagicMock()
        daemon_psud.update_master_led_color(None)
        assert daemon_psud.psu_chassis_info._set_psu_master_led.call_count == 0

        # Now we mock platform_chassis and daemon_psud.psu_chassis_info so that update_master_led_color() should be called
        psud.platform_chassis = MockChassis()
        daemon_psud.update_master_led_color(None)
        assert daemon_psud.psu_chassis_info._set_psu_master_led.call_count == 1
        daemon_psud.psu_chassis_info._set_psu_master_led.assert_called_with(daemon_psud.psu_chassis_info.master_status_good)

    def test_update_psu_entity_info(self):
        mock_psu1 = MockPsu(True, True, "PSU 1")
        mock_psu2 = MockPsu(True, True, "PSU 2")

        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud._update_single_psu_entity_info = mock.MagicMock()

        # If daemon_psud.platform_chassis is None, _update_psu_entity_info() should do nothing
        psud.platform_chassis = None
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 0

        psud.platform_chassis = MockChassis()
        psud.platform_chassis.get_all_psus = mock.Mock(return_value=[mock_psu1])
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 1
        daemon_psud._update_single_psu_entity_info.assert_called_with(1, mock_psu1)

        daemon_psud._update_single_psu_entity_info.reset_mock()
        psud.platform_chassis.get_all_psus = mock.Mock(return_value=[mock_psu1, mock_psu2])
        daemon_psud._update_psu_entity_info()
        assert daemon_psud._update_single_psu_entity_info.call_count == 2
        daemon_psud._update_single_psu_entity_info.assert_called_with(2, mock_psu2)
