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

@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_num_psus():
    # Test new platform API is available
    psud._wrapper_get_num_psus()
    assert psud.platform_chassis.get_num_psus.call_count == 1
    assert psud.platform_psuutil.get_num_psus.call_count == 0

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_num_psus()
    assert psud.platform_psuutil.get_num_psus.call_count == 1


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_psu_presence():
    # Test new platform API is available
    psud._wrapper_get_psu_presence(1)
    assert psud.platform_chassis.get_psu(0).get_presence.call_count == 1

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_psu_presence(1)
    assert psud.platform_psuutil.get_psu_presence.call_count == 1
    psud.platform_psuutil.get_psu_presence.assert_called_with(1)


@mock.patch('psud.platform_chassis', mock.MagicMock())
@mock.patch('psud.platform_psuutil', mock.MagicMock())
def test_wrapper_get_psu_status():
    # Test new platform API is available
    psud._wrapper_get_psu_status(1)
    assert psud.platform_chassis.get_psu(0).get_powergood_status.call_count == 1

    # Test new platform API not available
    psud.platform_chassis = None
    psud._wrapper_get_psu_status(1)
    assert psud.platform_psuutil.get_psu_status.call_count == 1
    psud.platform_psuutil.get_psu_status.assert_called_with(1)
