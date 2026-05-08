"""
    Mock sonic_platform.chassis for bmcctld mocked_libs.
    Returns a two-module chassis (BMC at index 0, Switch-Host at index 1).
"""

import sys
import os

# Re-use the shared mock_platform from the tests directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from tests.mock_platform import MockChassis  # noqa: E402


class Chassis(MockChassis):
    def __init__(self):
        super(Chassis, self).__init__()
