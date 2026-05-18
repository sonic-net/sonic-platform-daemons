"""
    Mock sonic_platform.platform for bmcctld mocked_libs.
"""

from sonic_platform.chassis import Chassis


class Platform:
    def __init__(self):
        self._chassis = Chassis()

    def get_chassis(self):
        return self._chassis
