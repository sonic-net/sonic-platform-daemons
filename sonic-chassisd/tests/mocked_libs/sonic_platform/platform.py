"""
    Mock implementation of sonic_platform package for unit testing
"""

from sonic_platform_base.platform_base import PlatformBase
from sonic_platform.chassis import Chassis


class Platform(PlatformBase):
    def __init__(self):
        PlatformBase.__init__(self)
        self._chassis = Chassis()
