from sonic_platform_base.chassis_base import ChassisBase

from .sfp import Sfp


class Chassis(ChassisBase):
    def __init__(self):
        ChassisBase.__init__(self)
        self._sfps = {}

    def get_sfp(self, index):
        if index not in self._sfps:
            self._sfps[index] = Sfp(index)
        return self._sfps[index]
