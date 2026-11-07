import bb_hw_client
from sonic_platform_base.platform_base import PlatformBase

from .chassis import Chassis


class Platform(PlatformBase):
    def __init__(self):
        PlatformBase.__init__(self)
        bb_hw_client.call("platform_init", None, "Platform")
        self._chassis = Chassis()
