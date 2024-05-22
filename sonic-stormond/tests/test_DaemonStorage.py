import datetime
import os
import sys
from imp import load_source

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


#daemon_base.db_connect = MagicMock()

config_intvls = '''
daemon_polling_interval,
60,
fsstats_sync_interval,
300
'''


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
    @patch('json.load', MagicMock(return_value={}))
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    def test_load_fsio_rw_json(self):

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
    def test_get_configdb_intervals(self, mock_daemon_base):

        mock_daemon_base = MagicMock()

        stormon_daemon = stormond.DaemonStorage(log_identifier)
        stormon_daemon._get_configdb_intervals()

        assert mock_daemon_base.call_count == 0

            
    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    @patch('json.dump', MagicMock())
    def test_sync_fsio_rw_json_exception(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon._sync_fsio_rw_json()

            assert stormon_daemon.state_db.call_count == 0

    @patch('sonic_py_common.daemon_base.db_connect', MagicMock())
    @patch('json.dump', MagicMock())
    @patch('time.time', MagicMock(return_value=1000))
    def test_sync_fsio_rw_json_happy(self):
        stormon_daemon = stormond.DaemonStorage(log_identifier)

        with patch('builtins.open', new_callable=mock_open, read_data='{}') as mock_fd:
            stormon_daemon._sync_fsio_rw_json()

            assert stormon_daemon.state_db.call_count == 0


