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

SYSLOG_IDENTIFIER = 'syseepromd_test'
NOT_AVAILABLE = 'N/A'

daemon_base.db_connect = mock.MagicMock()

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, 'mocked_libs')
sys.path.insert(0, mocked_libs_path)

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, 'scripts')
sys.path.insert(0, modules_path)

load_source('syseepromd', os.path.join(scripts_path, 'syseepromd'))
import syseepromd


def test_post_eeprom_to_db_eeprom_read_fail():
    daemon_syseepromd = syseepromd.DaemonSyseeprom()
    daemon_syseepromd.eeprom.read_eeprom = mock.MagicMock(return_value=None)
    daemon_syseepromd.eeprom_tbl = mock.MagicMock()
    daemon_syseepromd.log_error = mock.MagicMock()

    ret = daemon_syseepromd.post_eeprom_to_db()
    assert ret == syseepromd.ERR_FAILED_EEPROM
    assert daemon_syseepromd.log_error.call_count == 1
    daemon_syseepromd.log_error.assert_called_with('Failed to read EEPROM')
    assert daemon_syseepromd.eeprom_tbl.getKeys.call_count == 0


def test_post_eeprom_to_db_update_fail():
    daemon_syseepromd = syseepromd.DaemonSyseeprom()
    daemon_syseepromd.eeprom.update_eeprom_db = mock.MagicMock(return_value=1)
    daemon_syseepromd.eeprom_tbl = mock.MagicMock()
    daemon_syseepromd.log_error = mock.MagicMock()

    ret = daemon_syseepromd.post_eeprom_to_db()
    assert ret == syseepromd.ERR_FAILED_UPDATE_DB
    assert daemon_syseepromd.log_error.call_count == 1
    daemon_syseepromd.log_error.assert_called_with('Failed to update EEPROM info in database')
    assert daemon_syseepromd.eeprom_tbl.getKeys.call_count == 0


def test_post_eeprom_to_db_ok():
    daemon_syseepromd = syseepromd.DaemonSyseeprom()
    daemon_syseepromd.eeprom.update_eeprom_db = mock.MagicMock(return_value=0)
    daemon_syseepromd.eeprom_tbl = mock.MagicMock()
    daemon_syseepromd.log_error = mock.MagicMock()

    ret = daemon_syseepromd.post_eeprom_to_db()
    assert ret == syseepromd.ERR_NONE
    assert daemon_syseepromd.log_error.call_count == 0
    assert daemon_syseepromd.eeprom_tbl.getKeys.call_count == 1


def test_clear_db():
    daemon_syseepromd = syseepromd.DaemonSyseeprom()
    daemon_syseepromd.eeprom_tbl.getKeys = mock.MagicMock(return_value=['key1', 'key2'])
    daemon_syseepromd.eeprom_tbl._del = mock.MagicMock()

    daemon_syseepromd.clear_db()
    assert daemon_syseepromd.eeprom_tbl.getKeys.call_count == 1
    assert daemon_syseepromd.eeprom_tbl._del.call_count == 2


def test_detect_eeprom_table_integrity():
    daemon_syseepromd = syseepromd.DaemonSyseeprom()

    # Test entries as expected
    daemon_syseepromd.eeprom_tbl.getKeys = mock.MagicMock(return_value=['key1', 'key2'])
    daemon_syseepromd.eepromtbl_keys = ['key1', 'key2']
    ret = daemon_syseepromd.detect_eeprom_table_integrity()
    assert ret == True

    # Test differing amounts of entries
    daemon_syseepromd.eeprom_tbl.getKeys = mock.MagicMock(return_value=['key1', 'key2'])
    daemon_syseepromd.eepromtbl_keys = ['key1']
    ret = daemon_syseepromd.detect_eeprom_table_integrity()
    assert ret == False

    # Test same amount of entries, but with different keys
    daemon_syseepromd.eeprom_tbl.getKeys = mock.MagicMock(return_value=['key1', 'key2'])
    daemon_syseepromd.eepromtbl_keys = ['key1', 'key3']
    ret = daemon_syseepromd.detect_eeprom_table_integrity()
    assert ret == False


def test_signal_handler():
    daemon_syseepromd = syseepromd.DaemonSyseeprom()
    daemon_syseepromd.stop_event.set = mock.MagicMock()
    daemon_syseepromd.log_info = mock.MagicMock()
    daemon_syseepromd.log_warning = mock.MagicMock()

    # Test SIGHUP
    daemon_syseepromd.signal_handler(syseepromd.signal.SIGHUP, None)
    assert daemon_syseepromd.log_info.call_count == 1
    daemon_syseepromd.log_info.assert_called_with("Caught SIGHUP - ignoring...")
    assert daemon_syseepromd.log_warning.call_count == 0
    assert daemon_syseepromd.stop_event.set.call_count == 0

    # Test SIGINT
    daemon_syseepromd.log_info.reset_mock()
    daemon_syseepromd.log_warning.reset_mock()
    daemon_syseepromd.stop_event.set.reset_mock()
    daemon_syseepromd.signal_handler(syseepromd.signal.SIGINT, None)
    assert daemon_syseepromd.log_info.call_count == 1
    daemon_syseepromd.log_info.assert_called_with("Caught SIGINT - exiting...")
    assert daemon_syseepromd.log_warning.call_count == 0
    assert daemon_syseepromd.stop_event.set.call_count == 1

    # Test SIGTERM
    daemon_syseepromd.log_info.reset_mock()
    daemon_syseepromd.log_warning.reset_mock()
    daemon_syseepromd.stop_event.set.reset_mock()
    daemon_syseepromd.signal_handler(syseepromd.signal.SIGTERM, None)
    assert daemon_syseepromd.log_info.call_count == 1
    daemon_syseepromd.log_info.assert_called_with("Caught SIGTERM - exiting...")
    assert daemon_syseepromd.log_warning.call_count == 0
    assert daemon_syseepromd.stop_event.set.call_count == 1

    # Test an unhandled signal
    daemon_syseepromd.log_info.reset_mock()
    daemon_syseepromd.log_warning.reset_mock()
    daemon_syseepromd.stop_event.set.reset_mock()
    daemon_syseepromd.signal_handler(syseepromd.signal.SIGUSR1, None)
    assert daemon_syseepromd.log_warning.call_count == 1
    daemon_syseepromd.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1'")
    assert daemon_syseepromd.log_info.call_count == 0
    assert daemon_syseepromd.stop_event.set.call_count == 0


#@mock.patch('syseepromd.platform_chassis', mock.MagicMock())
#@mock.patch('syseepromd.platform_psuutil', mock.MagicMock())
#def test_wrapper_get_num_psus():
#    # Test new platform API is available and implemented
#    syseepromd._wrapper_get_num_psus()
#    assert syseepromd.platform_chassis.get_num_psus.call_count == 1
#    assert syseepromd.platform_psuutil.get_num_psus.call_count == 0
#
#    # Test new platform API is available but not implemented
#    syseepromd.platform_chassis.get_num_psus.side_effect = NotImplementedError
#    syseepromd._wrapper_get_num_psus()
#    assert syseepromd.platform_chassis.get_num_psus.call_count == 2
#    assert syseepromd.platform_psuutil.get_num_psus.call_count == 1
#
#    # Test new platform API not available
#    syseepromd.platform_chassis = None
#    syseepromd._wrapper_get_num_psus()
#    assert syseepromd.platform_psuutil.get_num_psus.call_count == 2
#
#
#@mock.patch('syseepromd.platform_chassis', mock.MagicMock())
#@mock.patch('syseepromd.platform_psuutil', mock.MagicMock())
#def test_wrapper_get_psu_presence():
#    # Test new platform API is available
#    syseepromd._wrapper_get_psu_presence(1)
#    assert syseepromd.platform_chassis.get_psu(0).get_presence.call_count == 1
#    assert syseepromd.platform_psuutil.get_psu_presence.call_count == 0
#
#    # Test new platform API is available but not implemented
#    syseepromd.platform_chassis.get_psu(0).get_presence.side_effect = NotImplementedError
#    syseepromd._wrapper_get_psu_presence(1)
#    assert syseepromd.platform_chassis.get_psu(0).get_presence.call_count == 2
#    assert syseepromd.platform_psuutil.get_psu_presence.call_count == 1
#
#    # Test new platform API not available
#    syseepromd.platform_chassis = None
#    syseepromd._wrapper_get_psu_presence(1)
#    assert syseepromd.platform_psuutil.get_psu_presence.call_count == 2
#    syseepromd.platform_psuutil.get_psu_presence.assert_called_with(1)
#
#
#@mock.patch('syseepromd.platform_chassis', mock.MagicMock())
#@mock.patch('syseepromd.platform_psuutil', mock.MagicMock())
#def test_wrapper_get_psu_status():
#    # Test new platform API is available
#    syseepromd._wrapper_get_psu_status(1)
#    assert syseepromd.platform_chassis.get_psu(0).get_powergood_status.call_count == 1
#    assert syseepromd.platform_psuutil.get_psu_status.call_count == 0
#
#    # Test new platform API is available but not implemented
#    syseepromd.platform_chassis.get_psu(0).get_powergood_status.side_effect = NotImplementedError
#    syseepromd._wrapper_get_psu_status(1)
#    assert syseepromd.platform_chassis.get_psu(0).get_powergood_status.call_count == 2
#    assert syseepromd.platform_psuutil.get_psu_status.call_count == 1
#
#    # Test new platform API not available
#    syseepromd.platform_chassis = None
#    syseepromd._wrapper_get_psu_status(1)
#    assert syseepromd.platform_psuutil.get_psu_status.call_count == 2
#    syseepromd.platform_psuutil.get_psu_status.assert_called_with(1)
#
#
#@mock.patch('syseepromd._wrapper_get_psu_presence', mock.MagicMock())
#@mock.patch('syseepromd._wrapper_get_psu_status', mock.MagicMock())
#def test_psu_db_update():
#    psu_tbl = mock.MagicMock()
#
#    syseepromd._wrapper_get_psu_presence.return_value = True
#    syseepromd._wrapper_get_psu_status.return_value = True
#    expected_fvp = syseepromd.swsscommon.FieldValuePairs(
#        [(syseepromd.PSU_INFO_PRESENCE_FIELD, 'true'),
#         (syseepromd.PSU_INFO_STATUS_FIELD, 'true'),
#         ])
#    syseepromd.psu_db_update(psu_tbl, 1)
#    assert psu_tbl.set.call_count == 1
#    psu_tbl.set.assert_called_with(syseepromd.PSU_INFO_KEY_TEMPLATE.format(1), expected_fvp)
#
#    psu_tbl.set.reset_mock()
#
#    syseepromd._wrapper_get_psu_presence.return_value = False
#    syseepromd._wrapper_get_psu_status.return_value = True
#    expected_fvp = syseepromd.swsscommon.FieldValuePairs(
#        [(syseepromd.PSU_INFO_PRESENCE_FIELD, 'false'),
#         (syseepromd.PSU_INFO_STATUS_FIELD, 'true'),
#         ])
#    syseepromd.psu_db_update(psu_tbl, 1)
#    assert psu_tbl.set.call_count == 1
#    psu_tbl.set.assert_called_with(syseepromd.PSU_INFO_KEY_TEMPLATE.format(1), expected_fvp)
#
#    psu_tbl.set.reset_mock()
#
#    syseepromd._wrapper_get_psu_presence.return_value = True
#    syseepromd._wrapper_get_psu_status.return_value = False
#    expected_fvp = syseepromd.swsscommon.FieldValuePairs(
#        [(syseepromd.PSU_INFO_PRESENCE_FIELD, 'true'),
#         (syseepromd.PSU_INFO_STATUS_FIELD, 'false'),
#         ])
#    syseepromd.psu_db_update(psu_tbl, 1)
#    assert psu_tbl.set.call_count == 1
#    psu_tbl.set.assert_called_with(syseepromd.PSU_INFO_KEY_TEMPLATE.format(1), expected_fvp)
#
#    psu_tbl.set.reset_mock()
#
#    syseepromd._wrapper_get_psu_presence.return_value = False
#    syseepromd._wrapper_get_psu_status.return_value = False
#    expected_fvp = syseepromd.swsscommon.FieldValuePairs(
#        [(syseepromd.PSU_INFO_PRESENCE_FIELD, 'false'),
#         (syseepromd.PSU_INFO_STATUS_FIELD, 'false'),
#         ])
#    syseepromd.psu_db_update(psu_tbl, 1)
#    assert psu_tbl.set.call_count == 1
#    psu_tbl.set.assert_called_with(syseepromd.PSU_INFO_KEY_TEMPLATE.format(1), expected_fvp)
#
#    psu_tbl.set.reset_mock()
#
#    syseepromd._wrapper_get_psu_presence.return_value = True
#    syseepromd._wrapper_get_psu_status.return_value = True
#    expected_fvp = syseepromd.swsscommon.FieldValuePairs(
#        [(syseepromd.PSU_INFO_PRESENCE_FIELD, 'true'),
#         (syseepromd.PSU_INFO_STATUS_FIELD, 'true'),
#         ])
#    syseepromd.psu_db_update(psu_tbl, 32)
#    assert psu_tbl.set.call_count == 32
#    psu_tbl.set.assert_called_with(syseepromd.PSU_INFO_KEY_TEMPLATE.format(32), expected_fvp)
#
#
#def test_log_on_status_changed():
#    normal_log = "Normal log message"
#    abnormal_log = "Abnormal log message"
#
#    mock_logger = mock.MagicMock()
#
#    syseepromd.log_on_status_changed(mock_logger, True, normal_log, abnormal_log)
#    assert mock_logger.log_notice.call_count == 1
#    assert mock_logger.log_warning.call_count == 0
#    mock_logger.log_notice.assert_called_with(normal_log)
#
#    mock_logger.log_notice.reset_mock()
#
#    syseepromd.log_on_status_changed(mock_logger, False, normal_log, abnormal_log)
#    assert mock_logger.log_notice.call_count == 0
#    assert mock_logger.log_warning.call_count == 1
#    mock_logger.log_warning.assert_called_with(abnormal_log)


@mock.patch('syseepromd.DaemonSyseeprom.run')
def test_main(mock_run):
    mock_run.return_value = False

    syseepromd.main()
    assert mock_run.call_count == 1
