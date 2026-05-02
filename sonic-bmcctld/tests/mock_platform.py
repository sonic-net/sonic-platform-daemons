"""
    Mock platform classes for bmcctld unit tests.

    Models a two-module chassis:
      index 0 → BMC module
      index 1 → Switch-Host module (controlled by bmcctld)
"""


class MockModule:
    """Simulates a sonic_platform_base.module_base.ModuleBase object."""

    MODULE_STATUS_ONLINE = "Online"
    MODULE_STATUS_OFFLINE = "Offline"
    MODULE_TYPE_BMC = "BMC"
    MODULE_TYPE_SWITCH_HOST = "SWITCH-HOST"

    def __init__(self, index, name, module_type, oper_status=MODULE_STATUS_ONLINE):
        self.index = index
        self.name = name
        self.module_type = module_type
        self._oper_status = oper_status
        self._admin_state = True
        self.power_cycle_called = False

    def get_name(self):
        return self.name

    def get_type(self):
        return self.module_type

    def get_oper_status(self):
        return self._oper_status

    def set_oper_status(self, status):
        self._oper_status = status

    def set_admin_state(self, up):
        self._admin_state = up
        # Reflect admin action in oper_status for realistic testing
        self._oper_status = self.MODULE_STATUS_ONLINE if up else self.MODULE_STATUS_OFFLINE

    def get_admin_state(self):
        return self._admin_state

    def get_description(self):
        return "Switch Host Module"

    def get_slot(self):
        return self.index

    def get_serial(self):
        return "MOCK-SERIAL-{}".format(self.index)

    def do_power_cycle(self):
        self.power_cycle_called = True
        self._oper_status = self.MODULE_STATUS_ONLINE


class MockChassis:
    """Simulates a sonic_platform_base.chassis_base.ChassisBase with two modules."""

    def __init__(self):
        bmc = MockModule(0, "BMC", MockModule.MODULE_TYPE_BMC)
        switch_host = MockModule(
            1, "SWITCH-HOST", MockModule.MODULE_TYPE_SWITCH_HOST,
            oper_status=MockModule.MODULE_STATUS_OFFLINE
        )
        self._module_list = [bmc, switch_host]

    def get_all_modules(self):
        return self._module_list

    def get_module(self, index):
        return self._module_list[index]

    def get_num_modules(self):
        return len(self._module_list)

    @property
    def switch_host(self):
        return self._module_list[1]
