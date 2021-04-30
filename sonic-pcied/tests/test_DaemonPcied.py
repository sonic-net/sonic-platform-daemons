import datetime
import os
import sys
from imp import load_source  # Replace with importlib once we no longer need to support Python 2

import pytest

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info.major == 3:
    from unittest import mock
else:
    import mock
from sonic_py_common import daemon_base

from .mock_platform import MockPcieUtil

SYSLOG_IDENTIFIER = 'pcied_test'
NOT_AVAILABLE = 'N/A'

daemon_base.db_connect = mock.MagicMock()

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)
load_source('pcied', os.path.join(scripts_path, 'pcied'))
import pcied

pcie_no_aer_stats = \
"""
{'correctable': {}, 'fatal': {}, 'non_fatal': {}}
"""

pcie_aer_stats_no_err = \
"""
{'correctable': {'field1': '0', 'field2': '0'},
 'fatal': {'field3': '0', 'field4': '0'},
 'non_fatal': {'field5': '0', 'field6': '0'}}
"""

pcie_aer_stats_err = \
"""
{'correctable': {'field1': '1', 'field2': '0'},
 'fatal': {'field3': '0', 'field4': '1'},
 'non_fatal': {'field5': '0', 'field6': '1'}}
"""

pcie_device_list = \
"""
[{'bus': '00', 'dev': '01', 'fn': '0', 'id': '1f10', 'name': 'PCI A'},
 {'bus': '00', 'dev': '02', 'fn': '0', 'id': '1f11', 'name': 'PCI B'},
 {'bus': '00', 'dev': '03', 'fn': '0', 'id': '1f13', 'name': 'PCI C'}]
"""

pcie_check_result_pass = \
"""
[{'bus': '00', 'dev': '01', 'fn': '0', 'id': '1f10', 'name': 'PCI A', 'result': 'Passed'},
 {'bus': '00', 'dev': '02', 'fn': '0', 'id': '1f11', 'name': 'PCI B', 'result': 'Passed'},
 {'bus': '00', 'dev': '03', 'fn': '0', 'id': '1f12', 'name': 'PCI C', 'result': 'Passed'}]
"""

pcie_check_result_fail = \
"""
[{'bus': '00', 'dev': '01', 'fn': '0', 'id': '1f10', 'name': 'PCI A', 'result': 'Passed'},
 {'bus': '00', 'dev': '02', 'fn': '0', 'id': '1f11', 'name': 'PCI B', 'result': 'Passed'},
 {'bus': '00', 'dev': '03', 'fn': '0', 'id': '1f12', 'name': 'PCI C', 'result': 'Failed'}]
"""

class TestDaemonPcied(object):
    """
    Test cases to cover functionality in DaemonPcied class
    """

    def test_signal_handler(self):
        pcied.platform_chassis = MockChassis()
        daemon_pcied = psud.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.stop_event.set = mock.MagicMock()
        daemon_pcied.log_info = mock.MagicMock()
        daemon_pcied.log_warning = mock.MagicMock()

        # Test SIGHUP
        daemon_pcied.signal_handler(pcied.signal.SIGHUP, None)
        assert daemon_pcied.log_info.call_count == 1
        daemon_pcied.log_info.assert_called_with("Caught signal 'SIGHUP' - ignoring...")
        assert daemon_pcied.log_warning.call_count == 0
        assert daemon_pcied.stop_event.set.call_count == 0
        assert pcied.exit_code == 0

        # Reset
        daemon_pcied.log_info.reset_mock()
        daemon_pcied.log_warning.reset_mock()
        daemon_pcied.stop_event.set.reset_mock()

        # Test SIGINT
        test_signal = pcied.signal.SIGINT
        daemon_pcied.signal_handler(test_signal, None)
        assert daemon_pcied.log_info.call_count == 1
        daemon_pcied.log_info.assert_called_with("Caught signal 'SIGINT' - exiting...")
        assert daemon_pcied.log_warning.call_count == 0
        assert daemon_pcied.stop_event.set.call_count == 1
        assert pcied.exit_code == (128 + test_signal)

        # Reset
        daemon_pcied.log_info.reset_mock()
        daemon_pcied.log_warning.reset_mock()
        daemon_pcied.stop_event.set.reset_mock()

        # Test SIGTERM
        test_signal = pcied.signal.SIGTERM
        daemon_pcied.signal_handler(test_signal, None)
        assert daemon_pcied.log_info.call_count == 1
        daemon_pcied.log_info.assert_called_with("Caught signal 'SIGTERM' - exiting...")
        assert daemon_pcied.log_warning.call_count == 0
        assert daemon_pcied.stop_event.set.call_count == 1
        assert pcied.exit_code == (128 + test_signal)

        # Reset
        daemon_pcied.log_info.reset_mock()
        daemon_pcied.log_warning.reset_mock()
        daemon_pcied.stop_event.set.reset_mock()
        pcied.exit_code = 0

        # Test an unhandled signal
        daemon_pcied.signal_handler(pcied.signal.SIGUSR1, None)
        assert daemon_pcied.log_warning.call_count == 1
        daemon_pcied.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1' - ignoring...")
        assert daemon_pcied.log_info.call_count == 0
        assert daemon_pcied.stop_event.set.call_count == 0
        assert pcied.exit_code == 0

    def test_run(self):
        pcied.platform_pcieutil = MockPcieUtil()
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.check_pcie_devices = mock.MagicMock()

        daemon_pcied.run()
        assert daemon_pcied.check_pcie_devices.call_count == 1

    def test_check_pcie_devices(self):
        pcied.platform_pcieutil = MockPcieUtil()
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.device_table = mock.MagicMock()
        daemon_pcied.update_aer_to_statedb = mock.MagicMock()
        daemon_pcied.log_warning = mock.MagicMock()

        # Test platform_pcieutil is None
        pcied.platform_pcieutil = None
        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_aer_to_statedb.call_count == 0
        assert daemon_pcied.log_warning.call_count == 0

        # Test with mocked platform_pcieutil
        pcied.platform_pcieutil = MockPcieUtil()
        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_aer_to_statedb.call_count == 1

        # Test with mocked platform_pcieutil with 3 mocked pcie devices + all passed results
        pcied.platform_pcieutil = MockPcieUtil(pcie_device_list, pcie_check_result_pass, pcie_no_aer_stats)
        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_aer_to_statedb.call_count == 3

        # Test with mocked platform_pcieutil with 3 mocked pcie devices + 1 failed result
        pcied.platform_pcieutil = MockPcieUtil(pcie_device_list, pcie_check_result_fail, pcie_no_aer_stats)
        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_aer_to_statedb.call_count == 2
        daemon_pcied.device_table.set.reset_mock()

    def test_update_aer_to_stats(self):
        daemon_psud = psud.DaemonPsud(SYSLOG_IDENTIFIER)
        daemon_psud.log_debug = mock.MagicMock()
        daemon_psud.device_table = mock.MagicMock()
        daemon_psud.update_aer_to_stats(mocked_device, pcie_no_aer_stats)
        assert daemon_pcied.log_debug.call_count == 1

        daemon_psud.fan_tbl.set.reset_mock()

        mocked_device_name = "00:01.0"
        mocked_expected_fvp = pcied.swsscommon.FieldValuePairs(
            [("correctable|field1", '0'),
             ("correctable|field2", '0'),
             ("fatal|field3", '0'),
             ("fatal|field4", '0'),
             ("non_fatal|field5", '0'),
             ("non_fatal|field6", '0'),
             ])

        daemon_psud.update_aer_to_stats(mocked_device, pcie_aer_stats_no_err)
        assert daemon_pcied.log_debug.call_count == 0
        daemon_psud.device_table.set.assert_called_with(mocked_device, expected_fvp)

        daemon_psud.fan_tbl.set.reset_mock()
