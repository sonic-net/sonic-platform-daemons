import os
import sys
import pytest

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info.major == 3:
    from unittest import mock
else:
    import mock
from sonic_py_common import daemon_base

from .mock_platform import MockPsu, MockChassis

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)
import psud


daemon_base.db_connect = mock.MagicMock()


SYSLOG_IDENTIFIER = 'psud_test'
NOT_AVAILABLE = 'N/A'


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_num_psus():
    mock_logger = mock.MagicMock()
    psud.platform_chassis.get_num_pdbs.return_value = 0
    # Test new platform API is available and implemented
    psud._wrapper_get_num_psus(mock_logger)
    assert psud.platform_chassis.get_num_psus.call_count == 1
    assert psud.platform_chassis.get_num_pdbs.call_count == 1
    assert psud.platform_psuutil.get_num_psus.call_count == 0

    # PDB count is added to PSU count when both are implemented
    psud.platform_chassis.get_num_psus.reset_mock()
    psud.platform_chassis.get_num_pdbs.reset_mock()
    psud.platform_chassis.get_num_psus.return_value = 2
    psud.platform_chassis.get_num_pdbs.return_value = 1
    assert psud._wrapper_get_num_psus(mock_logger) == 3

    # get_num_pdbs can raise NotImplementedError while PSUs still count
    psud.platform_chassis.get_num_psus.reset_mock()
    psud.platform_chassis.get_num_pdbs.reset_mock()
    psud.platform_chassis.get_num_psus.return_value = 4
    psud.platform_chassis.get_num_pdbs.side_effect = NotImplementedError
    assert psud._wrapper_get_num_psus(mock_logger) == 4

    # get_num_psus can raise NotImplementedError while PDBs still count
    psud.platform_chassis.get_num_psus.reset_mock()
    psud.platform_chassis.get_num_pdbs.reset_mock()
    psud.platform_chassis.get_num_psus.side_effect = NotImplementedError
    psud.platform_chassis.get_num_pdbs.side_effect = None
    psud.platform_chassis.get_num_pdbs.return_value = 2
    assert psud._wrapper_get_num_psus(mock_logger) == 2

    # Both counts can be unavailable on chassis API
    psud.platform_chassis.get_num_psus.reset_mock()
    psud.platform_chassis.get_num_pdbs.reset_mock()
    psud.platform_chassis.get_num_psus.side_effect = NotImplementedError
    psud.platform_chassis.get_num_pdbs.side_effect = NotImplementedError
    assert psud._wrapper_get_num_psus(mock_logger) == 0

    # Restore sane defaults for the remainder of this test
    psud.platform_chassis.get_num_psus.side_effect = None
    psud.platform_chassis.get_num_pdbs.side_effect = None
    psud.platform_chassis.get_num_psus.return_value = 0
    psud.platform_chassis.get_num_pdbs.return_value = 0

    # Test new platform API is available but get_num_psus not implemented:
    # current behavior is to treat PSU count as 0 (no psuutil fallback while chassis exists)
    psud.platform_chassis.get_num_psus.reset_mock()
    psud.platform_chassis.get_num_pdbs.reset_mock()
    psud.platform_chassis.get_num_psus.side_effect = NotImplementedError
    psud.platform_chassis.get_num_pdbs.return_value = 0
    psud._wrapper_get_num_psus(mock_logger)
    assert psud.platform_chassis.get_num_psus.call_count == 1
    assert psud.platform_chassis.get_num_pdbs.call_count == 1
    assert psud.platform_psuutil.get_num_psus.call_count == 0

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_num_psus(mock_logger)
    assert psud.platform_psuutil.get_num_psus.call_count == 1

    # Test with None logger - should not crash
    psud.platform_chassis = mock.MagicMock()
    psud.platform_chassis.get_num_pdbs.return_value = 0
    psud.platform_psuutil = mock.MagicMock()
    psud._wrapper_get_num_psus(None)
    assert psud.platform_chassis.get_num_psus.call_count >= 1

    # Test when both providers are unavailable
    psud.platform_chassis = None
    psud.platform_psuutil = None
    mock_logger.reset_mock()
    result = psud._wrapper_get_num_psus(mock_logger)
    assert result == 0
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with("PSU provider unavailable; defaulting to 0 PSUs")


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_psu_presence():
    mock_logger = mock.MagicMock()
    mock_psu = mock.MagicMock()
    mock_psu.get_presence.return_value = True

    # Test new platform API is available and working
    psud.platform_chassis.get_psu.return_value = mock_psu
    result = psud._wrapper_get_psu_presence(mock_logger, 1)
    assert result is True
    psud.platform_chassis.get_psu.assert_called_with(0)
    mock_psu.get_presence.assert_called_once()
    assert psud.platform_psuutil.get_psu_presence.call_count == 0
    assert mock_logger.log_error.call_count == 0

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_presence.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test _wrapper_get_psu returns None (PSU object retrieval failed)
    psud.platform_chassis.get_psu.side_effect = Exception("PSU retrieval failed")
    psud._wrapper_get_psu_presence(mock_logger, 1)
    # Should fallback to platform_psuutil
    psud.platform_chassis.get_psu.assert_called_with(0)
    assert psud.platform_psuutil.get_psu_presence.call_count == 1
    psud.platform_psuutil.get_psu_presence.assert_called_with(1)

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    psud.platform_chassis.get_psu.side_effect = None  # Remove side effect
    psud.platform_psuutil.get_psu_presence.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test new platform API PSU available but get_presence not implemented
    psud.platform_chassis.get_psu.return_value = mock_psu
    mock_psu.get_presence.side_effect = NotImplementedError
    psud._wrapper_get_psu_presence(mock_logger, 1)
    # Should fallback to platform_psuutil
    psud.platform_chassis.get_psu.assert_called_with(0)
    mock_psu.get_presence.assert_called_once()
    assert psud.platform_psuutil.get_psu_presence.call_count == 1
    psud.platform_psuutil.get_psu_presence.assert_called_with(1)

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_presence.reset_mock()
    psud.platform_psuutil.get_psu_presence.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test psu.get_presence() raises general exception
    psud.platform_chassis.get_psu.return_value = mock_psu
    mock_psu.get_presence.side_effect = RuntimeError("Hardware error")
    result = psud._wrapper_get_psu_presence(mock_logger, 1)
    assert result is False
    psud.platform_chassis.get_psu.assert_called_with(0)
    mock_psu.get_presence.assert_called_once()
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with("Exception in psu.get_presence() for PSU 1: Hardware error")

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_presence.reset_mock()
    psud.platform_psuutil.get_psu_presence.reset_mock()
    mock_logger.log_warning.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_psu_presence(mock_logger, 1)
    # Should use platform_psuutil
    assert psud.platform_psuutil.get_psu_presence.call_count == 1
    psud.platform_psuutil.get_psu_presence.assert_called_with(1)

    # Reset mocks
    psud.platform_psuutil.get_psu_presence.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test platform_psuutil.get_psu_presence raises exception
    psud.platform_chassis = None
    psud.platform_psuutil = mock.MagicMock()
    psud.platform_psuutil.get_psu_presence.side_effect = Exception("PSU presence error")
    result = psud._wrapper_get_psu_presence(mock_logger, 1)
    assert result is False
    assert psud.platform_psuutil.get_psu_presence.call_count == 1
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with("Exception in platform_psuutil.get_psu_presence(1): PSU presence error")

    # Reset mocks
    psud.platform_psuutil.get_psu_presence.reset_mock()
    mock_logger.log_warning.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test both platform_chassis and platform_psuutil are None
    psud.platform_chassis = None
    psud.platform_psuutil = None
    result = psud._wrapper_get_psu_presence(mock_logger, 1)
    assert result is False
    assert mock_logger.log_error.call_count == 1
    mock_logger.log_error.assert_called_with("Failed to get PSU 1 presence")


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_psu():
    mock_logger = mock.MagicMock()
    mock_psu = mock.MagicMock()

    # Test new platform API is available and working
    psud.platform_chassis.get_psu.return_value = mock_psu
    result = psud._wrapper_get_psu(mock_logger, 1)
    assert result == mock_psu
    psud.platform_chassis.get_psu.assert_called_with(0)  # psu_index - 1
    assert mock_logger.log_warning.call_count == 0

    # Reset mock
    psud.platform_chassis.get_psu.reset_mock()
    mock_logger.log_warning.reset_mock()

    # Test NotImplementedError
    psud.platform_chassis.get_psu.side_effect = NotImplementedError("Not implemented")
    result = psud._wrapper_get_psu(mock_logger, 1)
    assert result is None
    psud.platform_chassis.get_psu.assert_called_with(0)
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with("get_psu() not implemented by platform chassis: Not implemented")

    # Reset mock
    psud.platform_chassis.get_psu.reset_mock()
    mock_logger.log_warning.reset_mock()

    # Test general Exception
    psud.platform_chassis.get_psu.side_effect = Exception("General error")
    result = psud._wrapper_get_psu(mock_logger, 2)
    assert result is None
    psud.platform_chassis.get_psu.assert_called_with(1)  # psu_index - 1
    assert mock_logger.log_error.call_count == 1
    mock_logger.log_error.assert_called_with("Failed to get PSU 2 from platform chassis: General error")

    # Reset mock
    psud.platform_chassis.get_psu.reset_mock()
    mock_logger.log_warning.reset_mock()

    # Test with logger as None
    psud.platform_chassis.get_psu.side_effect = NotImplementedError("Not implemented")
    result = psud._wrapper_get_psu(None, 1)
    assert result is None
    psud.platform_chassis.get_psu.assert_called_with(0)

    # Test with None logger and different exception types
    mock_logger.reset_mock()
    psud.platform_chassis.get_psu.side_effect = RuntimeError("Hardware error")
    result = psud._wrapper_get_psu(None, 1)
    assert result is None
    
    # Test with valid logger and RuntimeError - should log error
    psud.platform_chassis = mock.MagicMock()
    psud.platform_chassis.get_psu.side_effect = RuntimeError("Hardware error")
    mock_logger.reset_mock()
    result = psud._wrapper_get_psu(mock_logger, 2)
    assert result is None
    assert mock_logger.log_error.call_count == 1
    mock_logger.log_error.assert_called_with("Failed to get PSU 2 from platform chassis: Hardware error")
    
    # Test platform_chassis is None
    psud.platform_chassis = None
    result = psud._wrapper_get_psu(mock_logger, 1)
    assert result is None

    # Invalid 1-based index short-circuits without calling into the chassis
    psud.platform_chassis = mock.MagicMock()
    assert psud._wrapper_get_psu(mock_logger, 0) is None
    psud.platform_chassis.get_psu.assert_not_called()


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_pdb():
    mock_logger = mock.MagicMock()
    mock_pdb = mock.MagicMock()

    psud.platform_chassis.get_pdb.return_value = mock_pdb
    assert psud._wrapper_get_pdb(mock_logger, 1) == mock_pdb
    psud.platform_chassis.get_pdb.assert_called_with(0)

    psud.platform_chassis.get_pdb.reset_mock()
    psud.platform_chassis.get_pdb.side_effect = NotImplementedError('Not implemented')
    assert psud._wrapper_get_pdb(mock_logger, 1) is None
    mock_logger.log_warning.assert_called_with(
        "get_pdb() not implemented by platform chassis: Not implemented")

    psud.platform_chassis.get_pdb.reset_mock()
    mock_logger.log_warning.reset_mock()
    psud.platform_chassis.get_pdb.side_effect = Exception('boom')
    assert psud._wrapper_get_pdb(mock_logger, 2) is None
    mock_logger.log_error.assert_called_with(
        "Failed to get PDB 2 from platform chassis: boom")

    assert psud._wrapper_get_pdb(None, 1) is None  # no logger
    psud.platform_chassis = None
    assert psud._wrapper_get_pdb(mock_logger, 1) is None
    psud.platform_chassis = mock.MagicMock()
    assert psud._wrapper_get_pdb(mock_logger, 0) is None
    psud.platform_chassis.get_pdb.assert_not_called()


def test_wrapper_get_pdb_presence_and_status():
    mock_logger = mock.MagicMock()
    mock_pdb = mock.MagicMock()
    psud.platform_chassis = mock.MagicMock()
    psud.platform_chassis.get_pdb.return_value = mock_pdb

    mock_pdb.get_presence.return_value = True
    assert psud._wrapper_get_pdb_presence(mock_logger, 1) is True

    mock_pdb.get_presence.side_effect = NotImplementedError
    assert psud._wrapper_get_pdb_presence(mock_logger, 1) is False

    mock_pdb.get_presence.side_effect = RuntimeError('bad')
    assert psud._wrapper_get_pdb_presence(mock_logger, 1) is False
    mock_logger.log_warning.assert_called_with(
        "Exception in pdb.get_presence() for PDB 1: bad")

    psud.platform_chassis = None
    assert psud._wrapper_get_pdb_presence(mock_logger, 1) is False

    psud.platform_chassis = mock.MagicMock()
    psud.platform_chassis.get_pdb.return_value = None
    assert psud._wrapper_get_pdb_presence(mock_logger, 1) is False

    psud.platform_chassis.get_pdb.return_value = mock_pdb
    mock_pdb.get_presence.side_effect = None
    mock_pdb.get_powergood_status.return_value = True
    assert psud._wrapper_get_pdb_status(mock_logger, 1) is True

    mock_pdb.get_powergood_status.side_effect = NotImplementedError
    assert psud._wrapper_get_pdb_status(mock_logger, 1) is False

    mock_pdb.get_powergood_status.side_effect = ValueError('x')
    assert psud._wrapper_get_pdb_status(mock_logger, 1) is False
    mock_logger.log_warning.assert_called_with(
        "Exception in pdb.get_powergood_status() for PDB 1: x")

    psud.platform_chassis = None
    assert psud._wrapper_get_pdb_status(mock_logger, 1) is False


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_psu_status():
    mock_logger = mock.MagicMock()
    mock_psu = mock.MagicMock()
    mock_psu.get_powergood_status.return_value = True

    # Test new platform API is available and working
    psud.platform_chassis.get_psu.return_value = mock_psu
    result = psud._wrapper_get_psu_status(mock_logger, 1)
    assert result is True
    psud.platform_chassis.get_psu.assert_called_with(0)
    mock_psu.get_powergood_status.assert_called_once()
    assert psud.platform_psuutil.get_psu_status.call_count == 0
    assert mock_logger.log_error.call_count == 0

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_powergood_status.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test _wrapper_get_psu returns None (PSU object retrieval failed)
    psud.platform_chassis.get_psu.side_effect = Exception("PSU retrieval failed")
    psud._wrapper_get_psu_status(mock_logger, 1)
    # Should fallback to platform_psuutil
    psud.platform_chassis.get_psu.assert_called_with(0)
    assert psud.platform_psuutil.get_psu_status.call_count == 1
    psud.platform_psuutil.get_psu_status.assert_called_with(1)

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    psud.platform_chassis.get_psu.side_effect = None  # Remove side effect
    psud.platform_psuutil.get_psu_status.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test new platform API PSU available but get_powergood_status not implemented
    psud.platform_chassis.get_psu.return_value = mock_psu
    mock_psu.get_powergood_status.side_effect = NotImplementedError
    psud._wrapper_get_psu_status(mock_logger, 1)
    # Should fallback to platform_psuutil
    psud.platform_chassis.get_psu.assert_called_with(0)
    mock_psu.get_powergood_status.assert_called_once()
    assert psud.platform_psuutil.get_psu_status.call_count == 1
    psud.platform_psuutil.get_psu_status.assert_called_with(1)

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_powergood_status.reset_mock()
    psud.platform_psuutil.get_psu_status.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test psu.get_powergood_status() raises general exception
    psud.platform_chassis.get_psu.return_value = mock_psu
    mock_psu.get_powergood_status.side_effect = RuntimeError("Hardware error")
    result = psud._wrapper_get_psu_status(mock_logger, 1)
    assert result is False
    psud.platform_chassis.get_psu.assert_called_with(0)
    mock_psu.get_powergood_status.assert_called_once()
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with("Exception in psu.get_powergood_status() for PSU 1: Hardware error")

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_powergood_status.reset_mock()
    psud.platform_psuutil.get_psu_status.reset_mock()
    mock_logger.log_warning.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_psu_status(mock_logger, 1)
    # Should use platform_psuutil
    assert psud.platform_psuutil.get_psu_status.call_count == 1
    psud.platform_psuutil.get_psu_status.assert_called_with(1)

    # Reset mocks
    psud.platform_psuutil.get_psu_status.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test platform_psuutil.get_psu_status raises exception
    psud.platform_chassis = None
    psud.platform_psuutil = mock.MagicMock()
    psud.platform_psuutil.get_psu_status.side_effect = Exception("PSU status error")
    result = psud._wrapper_get_psu_status(mock_logger, 1)
    assert result is False
    assert psud.platform_psuutil.get_psu_status.call_count == 1
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with("Exception in platform_psuutil.get_psu_status(1): PSU status error")

    # Reset mocks
    psud.platform_psuutil.get_psu_status.reset_mock()
    mock_logger.log_warning.reset_mock()
    mock_logger.log_error.reset_mock()

    # Test both platform_chassis and platform_psuutil are None
    psud.platform_chassis = None
    psud.platform_psuutil = None
    result = psud._wrapper_get_psu_status(mock_logger, 1)
    assert result is False
    assert mock_logger.log_error.call_count == 1
    mock_logger.log_error.assert_called_with("Failed to get PSU 1 status")

def test_log_on_status_changed():
    normal_log = "Normal log message"
    abnormal_log = "Abnormal log message"

    mock_logger = mock.MagicMock()

    psud.log_on_status_changed(mock_logger, True, normal_log, abnormal_log)
    assert mock_logger.log_notice.call_count == 1
    assert mock_logger.log_warning.call_count == 0
    mock_logger.log_notice.assert_called_with(normal_log)

    mock_logger.log_notice.reset_mock()

    psud.log_on_status_changed(mock_logger, False, normal_log, abnormal_log)
    assert mock_logger.log_notice.call_count == 0
    assert mock_logger.log_warning.call_count == 1
    mock_logger.log_warning.assert_called_with(abnormal_log)


@mock.patch('psud.platform_chassis', mock.MagicMock())
def test_get_psu_key():
    mock_psu = mock.MagicMock()
    mock_psu.get_name.return_value = "PSU-1"

    # Test platform_chassis is available and PSU get_name() works
    psud.platform_chassis.get_psu.return_value = mock_psu
    result = psud.get_psu_key(1)
    assert result == "PSU-1"
    psud.platform_chassis.get_psu.assert_called_with(0)  # psu_index - 1
    mock_psu.get_name.assert_called_once()

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_name.reset_mock()

    # Test _wrapper_get_psu returns None (PSU object retrieval failed)
    psud.platform_chassis.get_psu.side_effect = Exception("PSU retrieval failed")
    result = psud.get_psu_key(2)
    assert result == "PSU 2"
    psud.platform_chassis.get_psu.assert_called_with(1)  # psu_index - 1

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    psud.platform_chassis.get_psu.side_effect = None  # Remove side effect

    # Test PSU available but get_name() raises NotImplementedError
    psud.platform_chassis.get_psu.return_value = mock_psu
    mock_psu.get_name.side_effect = NotImplementedError
    result = psud.get_psu_key(3)
    assert result == "PSU 3"
    psud.platform_chassis.get_psu.assert_called_with(2)  # psu_index - 1
    mock_psu.get_name.assert_called_once()

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_name.reset_mock()

    # Test PSU available but get_name() raises IndexError
    mock_psu.get_name.side_effect = IndexError
    result = psud.get_psu_key(4)
    assert result == "PSU 4"
    psud.platform_chassis.get_psu.assert_called_with(3)  # psu_index - 1
    mock_psu.get_name.assert_called_once()

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_name.reset_mock()

    # Test PSU available but get_name() raises general Exception
    mock_psu.get_name.side_effect = RuntimeError("Hardware error")
    result = psud.get_psu_key(4)
    assert result == "PSU 4"
    psud.platform_chassis.get_psu.assert_called_with(3)  # psu_index - 1
    mock_psu.get_name.assert_called_once()

    # Reset mocks
    psud.platform_chassis.get_psu.reset_mock()
    mock_psu.get_name.reset_mock()

    # Test platform_chassis is None
    psud.platform_chassis = None
    result = psud.get_psu_key(5)
    assert result == "PSU 5"


@mock.patch('psud.platform_chassis', mock.MagicMock())
def test_get_pdb_key():
    mock_pdb = mock.MagicMock()
    mock_pdb.get_name.return_value = 'PDB-A'

    psud.platform_chassis.get_pdb.return_value = mock_pdb
    assert psud.get_pdb_key(1) == 'PDB-A'

    psud.platform_chassis.get_pdb.reset_mock()
    mock_pdb.get_name.reset_mock()
    psud.platform_chassis.get_pdb.side_effect = Exception('fail')
    assert psud.get_pdb_key(2) == psud.PDB_INFO_KEY_TEMPLATE.format(2)

    psud.platform_chassis.get_pdb.reset_mock()
    psud.platform_chassis.get_pdb.side_effect = None
    psud.platform_chassis.get_pdb.return_value = mock_pdb
    mock_pdb.get_name.side_effect = RuntimeError
    assert psud.get_pdb_key(3) == psud.PDB_INFO_KEY_TEMPLATE.format(3)

    psud.platform_chassis = None
    assert psud.get_pdb_key(4) == psud.PDB_INFO_KEY_TEMPLATE.format(4)


def test_sum_system_power_from_other_psus_and_pdbs():
    chassis = MockChassis()
    psu0 = MockPsu('PSU 1', 0, True, True)
    psu1 = MockPsu('PSU 2', 1, True, True)
    psu0.set_power(10.0)
    psu1.set_power(25.0)
    pdb0 = MockPsu('PDB 1', 0, True, True)
    pdb0.set_power(7.0)
    chassis._psu_list = [psu0, psu1]
    chassis._pdb_list = [pdb0]

    # Sum others while measuring for psu0: psu1 + pdb0
    assert psud._sum_system_power_from_other_psus_and_pdbs(chassis, psu0, 100.0) == 132.0

    # Measuring for pdb0: psu0 + psu1
    assert psud._sum_system_power_from_other_psus_and_pdbs(chassis, pdb0, 50.0) == 85.0

    ch2 = mock.MagicMock()
    ch2.get_all_psus.side_effect = NotImplementedError
    ch2.get_all_pdbs.return_value = [pdb0]
    assert psud._sum_system_power_from_other_psus_and_pdbs(ch2, pdb0, 1.0) == 1.0

    ch3 = mock.MagicMock()
    ch3.get_all_psus.return_value = [psu0]
    ch3.get_all_pdbs.side_effect = NotImplementedError
    assert psud._sum_system_power_from_other_psus_and_pdbs(ch3, psu0, 2.0) == 2.0


def _make_mock_swsscommon_for_main():
    """Build a minimal swsscommon module so DaemonPsud can connect to STATE_DB without real Redis."""
    import types
    mod = types.ModuleType('swsscommon')
    mod.STATE_DB = ''

    class Table:
        def __init__(self, db, table_name):
            self.table_name = table_name
            self.mock_dict = {}

        def _del(self, key):
            self.mock_dict.pop(key, None)

        def set(self, key, fvs):
            self.mock_dict[key] = getattr(fvs, 'fv_dict', fvs) if hasattr(fvs, 'fv_dict') else fvs

        def get(self, key):
            return self.mock_dict.get(key)

    class FieldValuePairs:
        def __init__(self, tuple_list):
            if isinstance(tuple_list, list) and tuple_list and isinstance(tuple_list[0], (tuple, list)):
                self.fv_dict = dict(tuple_list)
            else:
                self.fv_dict = {}

    mod.Table = Table
    mod.FieldValuePairs = FieldValuePairs
    return mod


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.DaemonPsud.run')
def test_main(mock_run):
    mock_run.return_value = False
    mock_swsscommon = _make_mock_swsscommon_for_main()
    with mock.patch('psud.swsscommon', mock_swsscommon):
        psud.main()
    assert mock_run.call_count == 1
