"""
    Re-export the swsscommon mock used when BMCCTLD_UNIT_TESTING=1.
    The real mock lives in mocked_libs/swsscommon/swsscommon.py so that
    sonic_py_common.device_info can also resolve swsscommon to the mock.
"""

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_mocked_libs = os.path.join(_tests_dir, 'mocked_libs')
if _mocked_libs not in sys.path:
    sys.path.insert(0, _mocked_libs)

from swsscommon.swsscommon import (  # noqa: F401, E402
    ConfigDBConnector,
    SonicV2Connector,
    Table,
    FieldValuePairs,
    Select,
    SubscriberStateTable,
    RedisPipeline,
    STATE_DB,
    CONFIG_DB,
)

