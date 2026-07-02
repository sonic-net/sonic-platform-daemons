class ModuleBase():
    # Invalid slot for modular chassis
    MODULE_INVALID_SLOT = -1

    # Possible card types for modular chassis
    MODULE_TYPE_SUPERVISOR = "SUPERVISOR"
    MODULE_TYPE_LINE = "LINE-CARD"
    MODULE_TYPE_FABRIC = "FABRIC-CARD"
    MODULE_TYPE_DPU = "DPU"
    MODULE_TYPE_SWITCH_HOST = "SWITCH-HOST"

    # Possible card status for modular chassis
    # Module state is Empty if no module is inserted in the slot
    MODULE_STATUS_EMPTY = "Empty"
    # Module state if Offline if powered down. This is also the admin-down state.
    MODULE_STATUS_OFFLINE = "Offline"
    # Module state is Present when it is powered up, but not fully functional.
    MODULE_STATUS_PRESENT = "Present"
    # Module state is Present when it is powered up, but entered a fault state.
    # Module is not able to go Online.
    MODULE_STATUS_FAULT = "Fault"
    # Module state is Online when fully operational
    MODULE_STATUS_ONLINE = "Online"

    MIDPLANE_DOWN_REASON_POWER_LOSS = "Power Loss"
    MIDPLANE_DOWN_REASON_THERMAL_OVERLOAD_CPU = "Thermal Overload: CPU"
    MIDPLANE_DOWN_REASON_THERMAL_OVERLOAD_ASIC = "Thermal Overload: ASIC"
    MIDPLANE_DOWN_REASON_THERMAL_OVERLOAD_OTHER = "Thermal Overload: Other"
    MIDPLANE_DOWN_REASON_INSUFFICIENT_FAN_SPEED = "Insufficient Fan Speed"
    MIDPLANE_DOWN_REASON_WATCHDOG = "Watchdog"
    MIDPLANE_DOWN_REASON_HARDWARE_OTHER = "Hardware - Other"
    MIDPLANE_DOWN_REASON_HARDWARE_BIOS = "BIOS"
    MIDPLANE_DOWN_REASON_HARDWARE_CPU = "CPU"
    MIDPLANE_DOWN_REASON_HARDWARE_BUTTON = "Push button"
    MIDPLANE_DOWN_REASON_HARDWARE_RESET_FROM_ASIC = "Reset from ASIC"
    MIDPLANE_DOWN_REASON_NON_HARDWARE = "Non-Hardware"
