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

from .mock_platform import MockPsu, MockChassis

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)
load_source('psud', os.path.join(scripts_path, 'psud'))
import psud


daemon_base.db_connect = mock.MagicMock()


SYSLOG_IDENTIFIER = 'psud_test'
NOT_AVAILABLE = 'N/A'


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_num_psus():
    mock_logger = mock.MagicMock()
    # Test new platform API is available and implemented
    psud._wrapper_get_num_psus(mock_logger)
    assert psud.platform_chassis.get_num_psus.call_count == 1
    assert psud.platform_psuutil.get_num_psus.call_count == 0

    # Test new platform API is available but not implemented
    psud.platform_chassis.get_num_psus.side_effect = NotImplementedError
    psud._wrapper_get_num_psus(mock_logger)
    assert psud.platform_chassis.get_num_psus.call_count == 2
    assert psud.platform_psuutil.get_num_psus.call_count == 1

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_num_psus(mock_logger)
    assert psud.platform_psuutil.get_num_psus.call_count == 2

    # Test with None logger - should not crash
    psud.platform_chassis = mock.MagicMock()
    psud.platform_psuutil = mock.MagicMock()
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
    assert result is False
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
@mock.patch('psud.DaemonPsud.run')
def test_main(mock_run):
    mock_run.return_value = False

    psud.main()
    assert mock_run.call_count == 1
