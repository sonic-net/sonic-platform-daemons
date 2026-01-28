import datetime
import os
import sys
import runpy

from imp import load_source, reload
from sonic_py_common import syslogger

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info.major == 3:
    from unittest.mock import patch, MagicMock, mock_open
else:
    from mock import patch, MagicMock, mock_open

# Add mocked_libs path so that the file under test can load mocked modules from there
tests_path = os.path.dirname(os.path.abspath(__file__))
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)

from .mocked_libs.swsscommon import swsscommon
from sonic_py_common import daemon_base

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)
load_source('stormond', os.path.join(scripts_path, 'stormond'))

import stormond
import pytest


log_identifier = 'storage_daemon_test'


config_intvls = '''
daemon_polling_interval,
60,
fsstats_sync_interval,
300
'''

fsio_dict = {"total_fsio_reads": "", "total_fsio_writes": "", "latest_fsio_reads": "1000", "latest_fsio_writes": "2000"}
fsio_json_dict = { 'sda' : {"total_fsio_reads": "10500", "total_fsio_writes": "21000", "latest_fsio_reads": "1000", "latest_fsio_writes": "2000"}}
bad_fsio_json_dict = { 'sda' : {"total_fsio_reads": None, "total_fsio_writes": "21000", "latest_fsio_reads": "1000", "latest_fsio_writes": "2000"}}
fsio_statedb_dict = { 'sda' : {"total_fsio_reads": "10500", "total_fsio_writes": "21000", "latest_fsio_reads": "200", "latest_fsio_writes": "400"}}

dynamic_dict = {'firmware': 'ILLBBK', 'health': '40', 'temperature': '5000', 'latest_fsio_reads': '150', 'latest_fsio_writes': '270', 'disk_io_reads': '1000', 'disk_io_writes': '2000', 'reserved_blocks': '3'}

class TestDaemonStorage(object):
    """
    Test cases to cover functionality in DaemonStorage class
    """

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_default_configdb_intervals_no_config(self):

        stormon_daemon = stormond.DaemonStorage(log_identifier)

        assert (stormon_daemon.timeout) == 3600
        assert (stormon_daemon.fsstats_sync_interval) == 86400

    
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_storage_devices(self):

        def new_mock_factory(self, key):
            return MagicMock()

        with patch('sonic_platform_base.sonic_storage.storage_devices.StorageDevices._storage_device_object_factory', new=new_mock_factory):

            stormon_daemon = stormond.DaemonStorage(log_identifier)

            assert(list(stormon_daemon.storage.devices.keys()) == ['sda'])


    @patch('os.path.exists', MagicMock(return_value=True))
    @patch('json.load', MagicMock(return_value=bad_fsio_json_dict))
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_json_false(self):

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon = stormond.DaemonStorage(log_identifier)

            assert stormon_daemon.fsio_json_file_loaded == False


    @patch('os.path.exists', MagicMock(return_value=True))
    @patch('json.load', MagicMock(return_value=fsio_json_dict))
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_json_true(self):

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon = stormond.DaemonStorage(log_identifier)

            assert stormon_daemon.fsio_json_file_loaded == True


    @patch('os.path.exists', MagicMock(return_value=True))
    @patch('json.load', MagicMock(side_effect=Exception))
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_json_exception(self):

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon = stormond.DaemonStorage(log_identifier)

            assert stormon_daemon.fsio_json_file_loaded == False
    
    @patch('sonic_py_common.daemon_base.db_connect')
    def testget_configdb_intervals(self, mock_db_connect):
        # Test that get_configdb_intervals() reuses the connection from __init__
        # and does not create a new connection
        mock_db = MagicMock()
        mock_db.hgetall = MagicMock(return_value={})
        mock_db_connect.return_value = mock_db

        # Connection should be made once in __init__
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        initial_call_count = mock_db_connect.call_count
        
        # get_configdb_intervals() should NOT create a new connection
        stormon_daemon.get_configdb_intervals()
        
        # Verify no additional connection was made
        assert mock_db_connect.call_count == initial_call_count

    @patch('sonic_py_common.daemon_base.db_connect')
    def test_configdb_connection_established_in_init(self, mock_db_connect):
        # Test that CONFIG_DB connection is established during initialization
        mock_config_db = MagicMock()
        mock_state_db = MagicMock()
        
        # First call returns STATE_DB, second call returns CONFIG_DB
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]
        
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        
        # Verify db_connect was called twice (once for STATE_DB, once for CONFIG_DB)
        assert mock_db_connect.call_count == 2
        # Verify CONFIG_DB connection is stored
        assert stormon_daemon.config_db is not None

    @patch('sonic_py_common.daemon_base.db_connect')
    def test_configdb_connection_failure_in_init(self, mock_db_connect):
        # Test that daemon continues even if CONFIG_DB connection fails
        mock_state_db = MagicMock()
        
        # First call returns STATE_DB successfully, second call fails for CONFIG_DB
        def side_effect_fn(db_name):
            if db_name == "STATE_DB":
                return mock_state_db
            elif db_name == "CONFIG_DB":
                raise Exception("Connection failed")
        
        mock_db_connect.side_effect = side_effect_fn
        
        # Daemon should initialize successfully even if CONFIG_DB fails
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        
        # Verify CONFIG_DB connection is None after failure
        assert stormon_daemon.config_db is None
        # Verify daemon still has STATE_DB connection
        assert stormon_daemon.state_db is not None

    @patch('sonic_py_common.daemon_base.db_connect')
    def test_get_configdb_intervals_with_no_connection(self, mock_db_connect):
        # Test that get_configdb_intervals() handles missing CONFIG_DB connection gracefully
        mock_state_db = MagicMock()
        
        # Only STATE_DB connection succeeds, CONFIG_DB fails
        def side_effect_fn(db_name):
            if db_name == "STATE_DB":
                return mock_state_db
            elif db_name == "CONFIG_DB":
                raise Exception("Connection failed")
        
        mock_db_connect.side_effect = side_effect_fn
        
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        
        # Store original timeout values
        original_timeout = stormon_daemon.timeout
        original_sync_interval = stormon_daemon.fsstats_sync_interval
        
        # Call get_configdb_intervals() with no CONFIG_DB connection
        stormon_daemon.get_configdb_intervals()
        
        # Verify default values are retained
        assert stormon_daemon.timeout == original_timeout
        assert stormon_daemon.fsstats_sync_interval == original_sync_interval

    @patch('sonic_py_common.daemon_base.db_connect')
    def test_get_configdb_intervals_exception_logs_previous_values(self, mock_db_connect):
        # Test that exception handler logs previously set interval values instead of defaults
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        
        # Setup successful connection initially
        def side_effect_fn(db_name):
            if db_name == "STATE_DB":
                return mock_state_db
            elif db_name == "CONFIG_DB":
                return mock_config_db
        
        mock_db_connect.side_effect = side_effect_fn
        
        # Create daemon
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        
        # Set custom interval values (different from defaults)
        stormon_daemon.timeout = 7200
        stormon_daemon.fsstats_sync_interval = 43200
        
        # Mock the hgetall to raise an exception
        mock_config_db.hgetall = MagicMock(side_effect=Exception("Connection error"))
        
        # Mock logger to verify log calls
        stormon_daemon.log.log_error = MagicMock()
        stormon_daemon.log.log_notice = MagicMock()
        
        # Call get_configdb_intervals which should catch exception
        stormon_daemon.get_configdb_intervals()
        
        # Verify error was logged
        assert stormon_daemon.log.log_error.call_count == 1
        
        # Verify the notice log was called with the PREVIOUS values (7200 and 43200)
        # not the defaults (3600 and 86400)
        assert stormon_daemon.log.log_notice.call_count == 1
        log_call_args = stormon_daemon.log.log_notice.call_args[0][0]
        assert "7200" in log_call_args
        assert "43200" in log_call_args

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_statedb(self):

        keys_list = ['STORAGE_INFO|sda', 'STORAGE_INFO|FSSTATS_SYNC']

        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.storage.devices = {'sda' : MagicMock()}
        stormon_daemon.state_db.keys = MagicMock(return_value=keys_list)
        stormon_daemon.state_db.hget = MagicMock()

        stormon_daemon._load_fsio_rw_statedb()

        assert stormon_daemon.statedb_storage_info_loaded == True


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_statedb_exception(self):

        keys_list = ['STORAGE_INFO|sda', 'STORAGE_INFO|FSSTATS_SYNC']

        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.storage.devices = {'sda' : MagicMock()}
        stormon_daemon.state_db.keys = MagicMock(return_value=keys_list)
        stormon_daemon.state_db.hget = MagicMock(side_effect=Exception)
        stormon_daemon.log.log_error = MagicMock()

        stormon_daemon._load_fsio_rw_statedb()

        assert stormon_daemon.statedb_storage_info_loaded == False
        assert stormon_daemon.log.log_error.call_count == 1


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_statedb_value_none(self):

        keys_list = ['STORAGE_INFO|sda', 'STORAGE_INFO|FSSTATS_SYNC']

        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.storage.devices = {'sda' : MagicMock()}
        stormon_daemon.state_db.keys = MagicMock(return_value=keys_list)
        stormon_daemon.state_db.hget = MagicMock(return_value=None)

        stormon_daemon._load_fsio_rw_statedb()

        assert stormon_daemon.statedb_storage_info_loaded == False


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    @patch('json.dump', MagicMock())
    def test_sync_fsio_rw_json_exception(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon.log.log_error = MagicMock()
            stormon_daemon.get_formatted_time = MagicMock(side_effect=Exception)
            stormon_daemon.sync_fsio_rw_json()

            assert stormon_daemon.state_db.call_count == 0
            assert stormon_daemon.log.log_error.call_count == 1


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    @patch('json.dump', MagicMock())
    @patch('time.time', MagicMock(return_value=1000))
    def test_sync_fsio_rw_json_happy(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon.sync_fsio_rw_json()

            assert stormon_daemon.state_db.call_count == 0

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_reconcile_fsio_rw_values_init(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.use_statedb_baseline = False
        stormon_daemon.use_fsio_json_baseline = False

        (reads, writes) = stormon_daemon._reconcile_fsio_rw_values(fsio_dict, MagicMock())

        assert reads == '1000'
        assert writes == '2000'


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_determine_sot(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.statedb_storage_info_loaded = True

        stormon_daemon._determine_sot()

        assert stormon_daemon.use_statedb_baseline == True
        assert stormon_daemon.use_fsio_json_baseline == False


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_reconcile_fsio_rw_values_reboot(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        
        stormon_daemon.use_statedb_baseline = False
        stormon_daemon.use_fsio_json_baseline = True
        stormon_daemon.fsio_rw_json = fsio_json_dict

        (reads, writes) = stormon_daemon._reconcile_fsio_rw_values(fsio_dict, 'sda')

        assert reads == '11500'
        assert writes == '23000'


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_reconcile_fsio_rw_values_daemon_crash(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        
        stormon_daemon.use_statedb_baseline = True
        stormon_daemon.use_fsio_json_baseline = True
        stormon_daemon.fsio_rw_statedb = fsio_statedb_dict

        (reads, writes) = stormon_daemon._reconcile_fsio_rw_values(fsio_dict, 'sda')

        assert reads == '11300'
        assert writes == '22600'
    
    
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_update_storage_info_status_db(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        stormon_daemon.update_storage_info_status_db('sda', fsio_json_dict['sda'])

        assert stormon_daemon.device_table.getKeys() == ['sda']
    

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_get_static_fields_no_storage_device_object(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.log.log_notice = MagicMock()

        stormon_daemon.storage.devices = {'sda' : None}

        stormon_daemon.get_static_fields_update_state_db()

        assert stormon_daemon.log.log_notice.call_count == 1


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_get_static_fields_happy(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        mock_storage_device_object = MagicMock()
        mock_storage_device_object.get_model.return_value = "Skynet"
        mock_storage_device_object.get_serial.return_value = "T1000"

        stormon_daemon.storage.devices = {'sda' : mock_storage_device_object}
        stormon_daemon.get_static_fields_update_state_db()

        assert stormon_daemon.device_table.getKeys() == ['sda']
        assert stormon_daemon.device_table.get('sda') == {'device_model': 'Skynet', 'serial': 'T1000'}


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_get_static_fields_exception(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        mock_storage_device_object = MagicMock()
        mock_storage_device_object.get_model.return_value = "Skynet"
        mock_storage_device_object.get_serial.return_value = "T1000"

        stormon_daemon.storage.devices = {'sda' : mock_storage_device_object}
        stormon_daemon.log.log_error = MagicMock()
        stormon_daemon.update_storage_info_status_db = MagicMock(side_effect=Exception)
        
        stormon_daemon.get_static_fields_update_state_db()

        assert stormon_daemon.log.log_error.call_count == 1


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_get_dynamic_fields_no_storage_device_object(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.log.log_notice = MagicMock()

        stormon_daemon.storage.devices = {'sda' : None}

        stormon_daemon.get_dynamic_fields_update_state_db()

        assert stormon_daemon.log.log_notice.call_count == 1


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_get_dynamic_fields(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        mock_storage_device_object = MagicMock()
        mock_storage_device_object.get_firmware.return_value = "ILLBBK"
        mock_storage_device_object.get_health.return_value = "40"
        mock_storage_device_object.get_temperature.return_value = "5000"
        mock_storage_device_object.get_fs_io_reads.return_value = "150"
        mock_storage_device_object.get_fs_io_writes.return_value = "270"
        mock_storage_device_object.get_disk_io_reads.return_value = "1000"
        mock_storage_device_object.get_disk_io_writes.return_value = "2000"
        mock_storage_device_object.get_reserved_blocks.return_value = "3"

        stormon_daemon.storage.devices = {'sda' : mock_storage_device_object}
        stormon_daemon.get_dynamic_fields_update_state_db()

        assert stormon_daemon.device_table.getKeys() == ['sda']
        for field, value in dynamic_dict.items():
            assert stormon_daemon.device_table.get('sda')[field] == value


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_get_dynamic_fields_exception(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.log.log_notice = MagicMock()

        mock_storage_device_object = MagicMock()
        mock_storage_device_object.fetch_parse_info = MagicMock(side_effect=Exception)

        stormon_daemon.storage.devices = {'sda' : mock_storage_device_object}
        stormon_daemon.get_dynamic_fields_update_state_db()

        assert stormon_daemon.log.log_notice.call_count == 1


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    @patch('json.dump', MagicMock())
    @patch('time.time', MagicMock(return_value=1000))
    def test_write_sync_time_statedb(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.sync_fsio_rw_json = MagicMock(return_value=True)

        stormon_daemon.write_sync_time_statedb()
        assert stormon_daemon.state_db.call_count == 0


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_signal_handler(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.sync_fsio_rw_json = MagicMock()

        stormon_daemon.stop_event.set = MagicMock()
        stormon_daemon.log.log_notice = MagicMock()
        stormon_daemon.log.log_warning = MagicMock()

        # Test SIGHUP
        stormon_daemon.signal_handler(stormond.signal.SIGHUP, None)
        assert stormon_daemon.log.log_notice.call_count == 1
        stormon_daemon.log.log_notice.assert_called_with("Caught signal 'SIGHUP' - ignoring...")
        assert stormon_daemon.log.log_warning.call_count == 0
        assert stormon_daemon.stop_event.set.call_count == 0
        assert stormond.exit_code == 0

        # Reset
        stormon_daemon.log.log_notice.reset_mock()
        stormon_daemon.log.log_warning.reset_mock()
        stormon_daemon.stop_event.set.reset_mock()

        # Test SIGINT
        test_signal = stormond.signal.SIGINT
        stormon_daemon.signal_handler(test_signal, None)
        assert stormon_daemon.log.log_notice.call_count == 2
        stormon_daemon.log.log_notice.assert_called_with("Exiting with SIGINT")
        assert stormon_daemon.log.log_warning.call_count == 0
        assert stormon_daemon.stop_event.set.call_count == 1
        assert stormond.exit_code == (128 + test_signal)

        # Reset
        stormon_daemon.log.log_notice.reset_mock()
        stormon_daemon.log.log_warning.reset_mock()
        stormon_daemon.stop_event.set.reset_mock()

        # Test SIGTERM
        test_signal = stormond.signal.SIGTERM
        stormon_daemon.signal_handler(test_signal, None)
        assert stormon_daemon.log.log_notice.call_count == 2
        stormon_daemon.log.log_notice.assert_called_with("Exiting with SIGTERM")
        assert stormon_daemon.log.log_warning.call_count == 0
        assert stormon_daemon.stop_event.set.call_count == 1
        assert stormond.exit_code == (128 + test_signal)

        # Reset
        stormon_daemon.log.log_notice.reset_mock()
        stormon_daemon.log.log_warning.reset_mock()
        stormon_daemon.stop_event.set.reset_mock()
        stormond.exit_code = 0

        # Test an unhandled signal
        stormon_daemon.signal_handler(stormond.signal.SIGUSR1, None)
        assert stormon_daemon.log.log_warning.call_count == 1
        stormon_daemon.log.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1' - ignoring...")
        assert stormon_daemon.log.log_notice.call_count == 0
        assert stormon_daemon.stop_event.set.call_count == 0
        assert stormond.exit_code == 0


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_signal_handler_sync_fsio_json_failed(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.sync_fsio_rw_json = MagicMock(return_value=False)

        stormon_daemon.stop_event.set = MagicMock()
        stormon_daemon.log.log_notice = MagicMock()
        stormon_daemon.log.log_warning = MagicMock()

        test_signal = stormond.signal.SIGTERM
        stormon_daemon.signal_handler(test_signal, None)
        assert stormon_daemon.log.log_notice.call_count == 2
        assert stormon_daemon.log.log_warning.call_count == 1
        assert stormon_daemon.stop_event.set.call_count == 1
        assert stormond.exit_code == (128 + test_signal)


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_run(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.get_dynamic_fields_update_state_db = MagicMock()
        stormon_daemon.sync_fsio_rw_json = MagicMock(return_value=True)
        stormon_daemon.write_sync_time_statedb = MagicMock(return_value=True)

        def mock_intervals():
            stormon_daemon.timeout = 5
            stormon_daemon.fsstats_sync_interval = 15

            stormon_daemon.fsio_sync_time = 0

        with patch.object(stormon_daemon, 'get_configdb_intervals', new=mock_intervals):
            rc = stormon_daemon.run()

            assert stormon_daemon.get_dynamic_fields_update_state_db.call_count == 1
            assert stormon_daemon.sync_fsio_rw_json.call_count == 1
            assert stormon_daemon.write_sync_time_statedb.call_count == 1
            assert rc == True


    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_run_stop_event(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.get_dynamic_fields_update_state_db = MagicMock()
        stormon_daemon.stop_event.wait = MagicMock(return_value=True)

        def mock_intervals():
            stormon_daemon.timeout = 5
            stormon_daemon.fsstats_sync_interval = 15

        with patch.object(stormon_daemon, 'get_configdb_intervals', new=mock_intervals):
            rc = stormon_daemon.run()

            assert stormon_daemon.get_dynamic_fields_update_state_db.call_count == 1
            assert rc == False

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_run_sync_fsio_rw_json_failed(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon.get_dynamic_fields_update_state_db = MagicMock()
        stormon_daemon.sync_fsio_rw_json = MagicMock(return_value=False)
        stormon_daemon.log.log_warning = MagicMock()

        def mock_intervals():
            stormon_daemon.timeout = 5
            stormon_daemon.fsstats_sync_interval = 15

            stormon_daemon.fsio_sync_time = 0

        with patch.object(stormon_daemon, 'get_configdb_intervals', new=mock_intervals):
            rc = stormon_daemon.run()

            assert stormon_daemon.get_dynamic_fields_update_state_db.call_count == 1
            assert stormon_daemon.sync_fsio_rw_json.call_count == 1
            assert stormon_daemon.log.log_warning.call_count == 1
            assert rc == True

class TestStormon():

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    @patch('sonic_py_common.syslogger.SysLogger', MagicMock())
    def test_main(self):

        stormond.DaemonStorage.run = MagicMock(return_value=False)
        assert stormond.main()
