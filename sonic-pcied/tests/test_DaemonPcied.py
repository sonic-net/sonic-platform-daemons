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

from .mock_platform import MockPcieUtil

SYSLOG_IDENTIFIER = 'pcied_test'
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
load_source('pcied', os.path.join(scripts_path, 'pcied'))
import pcied

pcie_no_aer_stats = \
"""
{'correctable': {}, 'fatal': {}, 'non_fatal': {}}
"""

pcie_aer_stats_no_err = {'correctable': {'field1': '0', 'field2': '0'},
 'fatal': {'field3': '0', 'field4': '0'},
 'non_fatal': {'field5': '0', 'field6': '0'}}


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

pcie_check_result_no = []

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

    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_signal_handler(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
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

    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_run(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.check_pcie_devices = mock.MagicMock()

        # Case 1: Test normal execution path
        daemon_pcied.stop_event.wait = mock.MagicMock(return_value=False)
        assert daemon_pcied.run() == True
        daemon_pcied.check_pcie_devices.assert_called_once()

        # Case 2: Test when stop_event.wait returns True (signal received)
        daemon_pcied.check_pcie_devices.reset_mock()
        daemon_pcied.stop_event.wait = mock.MagicMock(return_value=True)
        assert daemon_pcied.run() == False
        daemon_pcied.check_pcie_devices.assert_not_called()

        # Case 3: Test exception handling during stop_event.wait
        daemon_pcied.check_pcie_devices.reset_mock()
        daemon_pcied.stop_event.wait = mock.MagicMock(side_effect=Exception("Test Exception"))
        daemon_pcied.log_error = mock.MagicMock()
        assert daemon_pcied.run() == False
        daemon_pcied.log_error.assert_called_once_with(
            "Exception occurred during stop_event wait: Test Exception"
        )
        daemon_pcied.check_pcie_devices.assert_not_called()

    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_del(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.device_table = mock.MagicMock()
        daemon_pcied.status_table = mock.MagicMock()
        daemon_pcied.detach_info = mock.MagicMock()

        daemon_pcied.device_table.getKeys.return_value = ['device1', 'device2']
        daemon_pcied.status_table.getKeys.return_value = ['status1']

        daemon_pcied.__del__()

        assert daemon_pcied.device_table._del.call_count == 2
        daemon_pcied.device_table._del.assert_any_call('device1')
        daemon_pcied.device_table._del.assert_any_call('device2')

        assert daemon_pcied.status_table._del.call_count == 1
        daemon_pcied.status_table._del.assert_called_with('status1')


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.log.log_warning')
    def test_del_exception(self, mock_log_warning):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.device_table = mock.MagicMock()
        daemon_pcied.device_table.getKeys.side_effect = Exception("Test Exception")

        del daemon_pcied

        mock_log_warning.assert_called_once_with("Exception during cleanup: Test Exception", True)

    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_is_dpu_in_detaching_mode(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.detach_info = mock.MagicMock()
        daemon_pcied.detach_info.getKeys = mock.MagicMock(return_value=['DPU_0', 'DPU_1'])
        # Mock the get() method to return tuple of (exists, field_value_pairs)
        daemon_pcied.detach_info.get = mock.MagicMock(
            side_effect=lambda key: {
                'DPU_0': (True, [('bus_info', '0000:03:00.1'), ('dpu_state', 'detaching')]),
                'DPU_1': (True, [('bus_info', '0000:03:00.2'), ('dpu_state', 'attached')])
            }.get(key, (False, []))
        )

        # Test when the device is in detaching mode
        assert daemon_pcied.is_dpu_in_detaching_mode('0000:03:00.1') == True

        # Test when the device is not in detaching mode
        assert daemon_pcied.is_dpu_in_detaching_mode('0000:03:00.2') == False

        # Test when the device does not exist in detach_info
        assert daemon_pcied.is_dpu_in_detaching_mode('0000:03:00.3') == False

        # Test when detach_info is None
        daemon_pcied.detach_info = None
        assert daemon_pcied.is_dpu_in_detaching_mode('0000:03:00.1') == False

        # Test when detach_info has no keys
        daemon_pcied.detach_info = mock.MagicMock()
        daemon_pcied.detach_info.getKeys.return_value = []
        assert daemon_pcied.is_dpu_in_detaching_mode('0000:03:00.1') == False

    @mock.patch('pcied.device_info.is_smartswitch', mock.MagicMock(return_value=False))
    @mock.patch('pcied.DaemonPcied.is_dpu_in_detaching_mode', mock.MagicMock(return_value=False))
    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_check_pcie_devices(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_pcie_devices_status_db = mock.MagicMock()
        daemon_pcied.check_n_update_pcie_aer_stats = mock.MagicMock()
        daemon_pcied.redisPipeline.flush = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_check = mock.MagicMock(
            return_value=[
                {"result": "Failed", "bus": "03", "dev": "00", "fn": "1", "name": "PCIe Device 1"},
            ]
        )

        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_pcie_devices_status_db.call_count == 1
        assert daemon_pcied.check_n_update_pcie_aer_stats.call_count == 0
        assert daemon_pcied.redisPipeline.flush.call_count == 1

    @mock.patch('pcied.device_info.is_smartswitch', mock.MagicMock(return_value=False))
    @mock.patch('pcied.DaemonPcied.is_dpu_in_detaching_mode', mock.MagicMock(return_value=False))
    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_check_pcie_devices_update_aer(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_pcie_devices_status_db = mock.MagicMock()
        daemon_pcied.check_n_update_pcie_aer_stats = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_check = mock.MagicMock(
            return_value=[
                {"result": "Passed", "bus": "03", "dev": "00", "fn": "1", "name": "PCIe Device 1"},
            ]
        )

        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_pcie_devices_status_db.call_count == 1
        assert daemon_pcied.check_n_update_pcie_aer_stats.call_count == 1

    @mock.patch('pcied.device_info.is_smartswitch', mock.MagicMock(return_value=True))
    @mock.patch('pcied.DaemonPcied.is_dpu_in_detaching_mode', mock.MagicMock(return_value=True))
    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_check_pcie_devices_detaching(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_pcie_devices_status_db = mock.MagicMock()
        daemon_pcied.check_n_update_pcie_aer_stats = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_check = mock.MagicMock(
            return_value=[
                {"result": "Failed", "bus": "03", "dev": "00", "fn": "1", "name": "PCIe Device 1"},
            ]
        )

        daemon_pcied.check_pcie_devices()
        assert daemon_pcied.update_pcie_devices_status_db.call_count == 1
        assert daemon_pcied.check_n_update_pcie_aer_stats.call_count == 0

    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_update_pcie_devices_status_db(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.log_info = mock.MagicMock()
        daemon_pcied.log_error = mock.MagicMock()
        daemon_pcied.status_table = mock.MagicMock()

        # Case 1: test for pass resultInfo
        daemon_pcied.update_pcie_devices_status_db(0)
        daemon_pcied.log_info.assert_called_once_with("PCIe device status check : PASSED")
        daemon_pcied.status_table.set.assert_called_once_with(
            "status", pcied.swsscommon.FieldValuePairs([('status', 'PASSED')])
        )
        daemon_pcied.log_error.assert_not_called()

        daemon_pcied.log_info.reset_mock()
        daemon_pcied.status_table.set.reset_mock()
        daemon_pcied.log_error.reset_mock()

        # Case 2: test for resultInfo with 1 device failed to detect
        daemon_pcied.update_pcie_devices_status_db(1)
        daemon_pcied.log_error.assert_called_once_with("PCIe device status check : FAILED")
        daemon_pcied.status_table.set.assert_called_once_with(
            "status", pcied.swsscommon.FieldValuePairs([('status', 'FAILED')])
        )
        daemon_pcied.log_info.assert_not_called()

        daemon_pcied.log_info.reset_mock()
        daemon_pcied.status_table.set.reset_mock()
        daemon_pcied.log_error.reset_mock()

        # Case 3: test exception handling
        daemon_pcied.status_table.set.side_effect = Exception("Test Exception")
        daemon_pcied.update_pcie_devices_status_db(0)
        daemon_pcied.log_error.assert_called_once_with(
            "Exception while updating PCIe device status to STATE_DB: Test Exception"
        )


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_check_n_update_pcie_aer_stats(self, mock_read):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_aer_to_statedb = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()
        daemon_pcied.log_error = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock()

        # Case 1: read_id_file returns None, no further actions
        mock_read.return_value = None
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)
        daemon_pcied.device_table.set.assert_not_called()
        daemon_pcied.update_aer_to_statedb.assert_not_called()
        pcied.platform_pcieutil.get_pcie_aer_stats.assert_not_called()
        daemon_pcied.log_error.assert_not_called()

        # Case 2: read_id_file returns valid ID, normal flow
        mock_read.return_value = '1714'
        pcied.platform_pcieutil.get_pcie_aer_stats.return_value = pcie_aer_stats_no_err
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)
        daemon_pcied.device_table.set.assert_called_once_with(
            '00:01.0', pcied.swsscommon.FieldValuePairs([('id', '1714')])
        )
        daemon_pcied.update_aer_to_statedb.assert_called_once()
        pcied.platform_pcieutil.get_pcie_aer_stats.assert_called_once_with(bus=0, dev=1, func=0)
        daemon_pcied.log_error.assert_not_called()

        # Case 3: Exception handling when get_pcie_aer_stats raises exception
        daemon_pcied.device_table.set.reset_mock()
        daemon_pcied.update_aer_to_statedb.reset_mock()
        pcied.platform_pcieutil.get_pcie_aer_stats.side_effect = Exception("Test Exception")
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)
        daemon_pcied.log_error.assert_called_once_with(
            "Exception while checking AER attributes for 00:01.0: Test Exception"
        )


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    def test_update_aer_to_statedb(self):
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.log_debug = mock.MagicMock()
        daemon_pcied.log_error = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()
        daemon_pcied.device_name = "PCIe Device 1"
        daemon_pcied.pcied_cache[daemon_pcied.device_name] = mock.MagicMock()

        # Case 1: Test when aer_stats is None
        daemon_pcied.aer_stats = None
        daemon_pcied.update_aer_to_statedb()
        daemon_pcied.log_debug.assert_called_once_with("PCIe device PCIe Device 1 has no AER Stats")
        daemon_pcied.pcied_cache[daemon_pcied.device_name].pop.assert_called_once()
        daemon_pcied.device_table.set.assert_not_called()
        daemon_pcied.log_debug.reset_mock()
        daemon_pcied.pcied_cache[daemon_pcied.device_name].reset_mock()

        # Case 2: Test when aer_stats is empty
        daemon_pcied.aer_stats = {'correctable': {}, 'fatal': {}, 'non_fatal': {}}
        daemon_pcied.update_aer_to_statedb()
        daemon_pcied.log_debug.assert_called_once_with("PCIe device PCIe Device 1 has no AER attributes")
        daemon_pcied.pcied_cache[daemon_pcied.device_name].pop.assert_called_once()
        daemon_pcied.device_table.set.assert_not_called()
        daemon_pcied.log_debug.reset_mock()
        daemon_pcied.pcied_cache[daemon_pcied.device_name].reset_mock()

        # Case 3: Test when aer_stats has valid data
        daemon_pcied.aer_stats = pcie_aer_stats_no_err
        daemon_pcied.update_aer_to_statedb()
        expected_fields = [
            ("correctable|field1", '0'),
            ("correctable|field2", '0'),
            ("fatal|field3", '0'),
            ("fatal|field4", '0'),
            ("non_fatal|field5", '0'),
            ("non_fatal|field6", '0'),
        ]
        daemon_pcied.device_table.set.assert_called_once_with(
            "PCIe Device 1",
            pcied.swsscommon.FieldValuePairs(expected_fields)
        )
        daemon_pcied.log_debug.assert_not_called()
        daemon_pcied.log_error.assert_not_called()
        daemon_pcied.pcied_cache[daemon_pcied.device_name].pop.assert_not_called()
        daemon_pcied.device_table.set.reset_mock()

        # Case 4: Test exception handling
        daemon_pcied.device_table.set.side_effect = Exception("Test Exception")
        daemon_pcied.update_aer_to_statedb()
        daemon_pcied.log_error.assert_called_once_with(
            "Exception while updating AER attributes to STATE_DB for PCIe Device 1: Test Exception"
        )


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.daemon_base.db_connect', mock.MagicMock())
    @mock.patch('pcied.sys.exit')
    @mock.patch('pcied.log')
    def test_init_db_connection_failure(self, mock_log, mock_exit):
        # Case 1 : Normal Execution path; Verify error was not logged and exit was not called
        pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        mock_log.log_error.assert_not_called()
        mock_exit.assert_not_called()

        # Reset mock objects
        mock_log.reset_mock()
        mock_exit.reset_mock()

        # Case 2 : Test exception during Redis connection or table creation error and verify error was logged and exit was called with correct error code
        with mock.patch('pcied.swsscommon.Table', side_effect=Exception('Test Redis DB Exception')):
            pcied.DaemonPcied(SYSLOG_IDENTIFIER)

            mock_log.log_error.assert_called_once_with(
                'Failed to connect to STATE_DB or create table. Error: Test Redis DB Exception',
                True
            )

            mock_exit.assert_called_once_with(pcied.PCIEUTIL_CONF_FILE_ERROR)

    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_cache_device_id_unchanged(self, mock_read):
        """Test that device_table.set is NOT called when device ID hasn't changed"""
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_aer_to_statedb = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=pcie_aer_stats_no_err)

        # First call: Device ID is new, should write to DB
        mock_read.return_value = '1714'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify device ID was written to DB, cache was populated with device ID and AER stats was written to DB
        daemon_pcied.device_table.set.assert_called_once_with(
            '00:01.0', pcied.swsscommon.FieldValuePairs([('id', '1714')])
        )
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'
        assert daemon_pcied.update_aer_to_statedb.call_count == 1

        # Reset mock
        daemon_pcied.device_table.set.reset_mock()
        daemon_pcied.update_aer_to_statedb.reset_mock()

        # Second call: Same device ID, should NOT write device ID to DB
        mock_read.return_value = '1714'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify device_table.set was NOT called for device ID (cache hit)
        daemon_pcied.device_table.set.assert_not_called()

        # But update_aer_to_statedb should still be called (AER stats are considered "new" first time)
        assert daemon_pcied.update_aer_to_statedb.call_count == 1


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_cache_device_id_changed(self, mock_read):
        """Test that device_table.set IS called when device ID changes"""
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_aer_to_statedb = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=pcie_aer_stats_no_err)

        # First call: Initial device ID. should write device ID to DB, populate cache and write AER stats to DB
        mock_read.return_value = '1714'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        daemon_pcied.device_table.set.assert_called_once_with(
            '00:01.0', pcied.swsscommon.FieldValuePairs([('id', '1714')])
        )
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'
        assert daemon_pcied.update_aer_to_statedb.call_count == 1

        # Reset mock
        daemon_pcied.device_table.set.reset_mock()
        daemon_pcied.update_aer_to_statedb.reset_mock()

        # Second call: Device ID changed (e.g., device was replaced)
        mock_read.return_value = '1715'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify device_table.set WAS called for new device ID
        daemon_pcied.device_table.set.assert_called_once_with(
            '00:01.0', pcied.swsscommon.FieldValuePairs([('id', '1715')])
        )

        # Verify cache was updated with new device ID and AER stats was also written to DB
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1715'
        daemon_pcied.update_aer_to_statedb.assert_called_once()


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_cache_aer_stats_unchanged(self, mock_read):
        """Test that update_aer_to_statedb is NOT called when AER stats haven't changed"""
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.log_debug = mock.MagicMock()
        daemon_pcied.log_error = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=pcie_aer_stats_no_err)

        # NOT mocking update_aer_to_statedb as we're testing its cache update functionality here
        # First call: Initial AER stats
        mock_read.return_value = '1714'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify device table was updated on first call and cache was populated with AER stats
        assert daemon_pcied.device_table.set.call_count == 2  # once for device ID, once for AER stats
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'
        assert daemon_pcied.pcied_cache['00:01.0']['aer_stats'] == pcie_aer_stats_no_err

        # Reset mock
        daemon_pcied.device_table.set.reset_mock()

        # Second call: Same AER stats, should NOT update DB
        pcied.platform_pcieutil.get_pcie_aer_stats.return_value = pcie_aer_stats_no_err
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify device_table.set was NOT called (both device ID and AER stats are cached)
        daemon_pcied.device_table.set.assert_not_called()


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_cache_aer_stats_changed(self, mock_read):
        """Test that update_aer_to_statedb IS called when AER stats change"""
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.log_debug = mock.MagicMock()
        daemon_pcied.log_error = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()

        # NOT mocking update_aer_to_statedb as we're testing its cache update functionality here
        initial_aer_stats = {'correctable': {'field1': '0', 'field2': '0'},
                        'fatal': {'field3': '0', 'field4': '0'},
                        'non_fatal': {'field5': '0', 'field6': '0'}}

        changed_aer_stats = {'correctable': {'field1': '1', 'field2': '0'},  # field1 changed
                        'fatal': {'field3': '0', 'field4': '0'},
                        'non_fatal': {'field5': '0', 'field6': '0'}}

        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=initial_aer_stats)

        # First call: Initial AER stats
        mock_read.return_value = '1714'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        assert daemon_pcied.device_table.set.call_count == 2  # once for device ID, once for AER stats
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'
        assert daemon_pcied.pcied_cache['00:01.0']['aer_stats'] == initial_aer_stats

        # Reset mock
        daemon_pcied.device_table.set.reset_mock()

        # Second call: AER stats changed
        pcied.platform_pcieutil.get_pcie_aer_stats.return_value = changed_aer_stats
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify device_table.set was called only once for the AER stats (not for device ID since it's cached and remains unchanged)
        daemon_pcied.device_table.set.assert_called_once()
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'

        # Verify cache was updated with new stats
        assert daemon_pcied.pcied_cache['00:01.0']['aer_stats'] == changed_aer_stats


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_cache_device_removal(self, mock_read):
        """Test that cache entry is removed when device is no longer present"""
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.update_aer_to_statedb = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=pcie_aer_stats_no_err)

        # First call: Device is present
        mock_read.return_value = '1714'
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify cache entry was created
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'
        assert daemon_pcied.device_table.set.call_count == 1  # for device ID

        # Reset mock
        daemon_pcied.device_table.set.reset_mock()

        # Second call: Device is removed (read_id_file returns None)
        mock_read.return_value = None
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify cache entry was removed and no DB update was attempted
        assert '00:01.0' not in daemon_pcied.pcied_cache
        daemon_pcied.device_table.set.assert_not_called()


    @mock.patch('pcied.load_platform_pcieutil', mock.MagicMock())
    @mock.patch('pcied.read_id_file')
    def test_cache_multiple_devices(self, mock_read):
        """Test that cache correctly handles multiple devices independently"""
        daemon_pcied = pcied.DaemonPcied(SYSLOG_IDENTIFIER)
        daemon_pcied.log_debug = mock.MagicMock()
        daemon_pcied.log_error = mock.MagicMock()
        daemon_pcied.device_table = mock.MagicMock()

        aer_stats_dev1 = {'correctable': {'field1': '0'}, 'fatal': {}, 'non_fatal': {}}
        aer_stats_dev2 = {'correctable': {'field1': '1'}, 'fatal': {}, 'non_fatal': {}}

        # Device 1: 00:01.0
        mock_read.return_value = '1714'
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=aer_stats_dev1)
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Device 2: 00:02.0
        mock_read.return_value = '1715'
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=aer_stats_dev2)
        daemon_pcied.check_n_update_pcie_aer_stats(0, 2, 0)

        # Verify both devices are in cache
        assert '00:01.0' in daemon_pcied.pcied_cache
        assert '00:02.0' in daemon_pcied.pcied_cache

        # Verify each device has correct data
        assert daemon_pcied.pcied_cache['00:01.0']['device_id'] == '1714'
        assert daemon_pcied.pcied_cache['00:02.0']['device_id'] == '1715'
        assert daemon_pcied.pcied_cache['00:01.0']['aer_stats'] == aer_stats_dev1
        assert daemon_pcied.pcied_cache['00:02.0']['aer_stats'] == aer_stats_dev2
        assert daemon_pcied.device_table.set.call_count == 4  # 2 devices x (1 for ID + 1 for AER stats)

        # Reset mock
        daemon_pcied.device_table.set.reset_mock()

        # Update only Device 1's stats
        aer_stats_dev1_updated = {'correctable': {'field1': '2'}, 'fatal': {}, 'non_fatal': {}}
        mock_read.return_value = '1714'
        pcied.platform_pcieutil.get_pcie_aer_stats = mock.MagicMock(return_value=aer_stats_dev1_updated)
        daemon_pcied.check_n_update_pcie_aer_stats(0, 1, 0)

        # Verify Device 1's AER stats were updated in DB (should be called once for updated stats)
        assert daemon_pcied.device_table.set.call_count == 1
        assert daemon_pcied.pcied_cache['00:01.0']['aer_stats'] == aer_stats_dev1_updated

        # Verify Device 2's cache is unchanged
        assert daemon_pcied.pcied_cache['00:02.0']['aer_stats'] == aer_stats_dev2
