import os
import sys
from imp import load_source  # Replace with importlib once we no longer need to support Python 2

import pytest

# TODO: Clean this up once we no longer need to support Python 2
if sys.version_info >= (3, 3):
    from unittest.mock import MagicMock, patch, mock_open
else:
    from mock import MagicMock, patch, mock_open

from .mock_platform import MockPcieUtil

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)
from sonic_py_common import daemon_base, device_info

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)
load_source('pcied', os.path.join(scripts_path, 'pcied'))
import pcied


daemon_base.db_connect = MagicMock()

SYSLOG_IDENTIFIER = 'pcied_test'
NOT_AVAILABLE = 'N/A'


@patch('pcied.load_platform_pcieutil', MagicMock())
@patch('pcied.DaemonPcied.run')
def test_main(mock_run):
    mock_run.return_value = False

    pcied.main()
    assert mock_run.call_count == 1


@patch('pcied.os.path.exists', MagicMock(return_value=True))
def test_read_id_file():

    device_name = "test"

    with patch('builtins.open', new_callable=mock_open, read_data='15') as mock_fd:
        rc = pcied.read_id_file(device_name)
        assert rc == "15"

@patch('pcied.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/tmp', None)))
def test_load_platform_pcieutil():
    with patch('pcied.log') as mock_log:

        # Case 1: Successfully import sonic_platform.pcie.Pcie
        with patch('sonic_platform.pcie.Pcie') as mock_pcie:
            instance = mock_pcie.return_value
            result = pcied.load_platform_pcieutil()

            mock_pcie.assert_called_once_with('/tmp')
            assert result == instance
            mock_log.log_notice.assert_not_called()
            mock_log.log_error.assert_not_called()

        # Case 2: Fallback to sonic_platform_base.sonic_pcie.pcie_common.PcieUtil
        with patch('sonic_platform.pcie.Pcie', side_effect=ImportError("No module named 'sonic_platform.pcie'")), \
             patch('sonic_platform_base.sonic_pcie.pcie_common.PcieUtil') as mock_pcieutil:
            instance = mock_pcieutil.return_value
            result = pcied.load_platform_pcieutil()

            mock_pcieutil.assert_called_once_with('/tmp')
            assert result == instance
            mock_log.log_notice.assert_called_once()
            mock_log.log_error.assert_not_called()
            mock_log.reset_mock()

        # Case 3: Failure to import both modules
        with patch('sonic_platform.pcie.Pcie', side_effect=ImportError("No module named 'sonic_platform.pcie'")), \
             patch('sonic_platform_base.sonic_pcie.pcie_common.PcieUtil', side_effect=ImportError("No module named 'sonic_platform_base.sonic_pcie.pcie_common'")):
            with pytest.raises(RuntimeError, match="Unable to load PCIe utility module."):
                pcied.load_platform_pcieutil()

            mock_log.log_notice.assert_called_once()
            assert mock_log.log_error.call_count == 2
