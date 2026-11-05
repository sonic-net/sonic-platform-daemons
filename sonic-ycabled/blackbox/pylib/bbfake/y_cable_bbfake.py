"""Fake vendor Y-cable driver for the black-box suite.

The suite installs it as `sonic_y_cable.bbfake.y_cable_bbfake` in an overlay of
the real sonic_y_cable package, and maps the fake vendor and model to it. Every
call goes to the suite's fake hardware (harness/fake_hw.py).
"""

import bb_hw_client
from sonic_y_cable.y_cable_base import YCableBase

# Every driver method ycabled calls.
METHODS = (
    "get_vendor", "get_read_side", "get_mux_direction", "get_active_linked_tor_side",
    "toggle_mux_to_tor_a", "toggle_mux_to_tor_b", "get_switch_count_total", "get_eye_heights",
    "is_link_active", "get_firmware_version", "get_local_temperature", "get_local_voltage",
    "get_nic_temperature", "get_nic_voltage", "get_target_cursor_values", "get_switching_mode",
    "set_switching_mode", "enable_prbs_mode", "disable_prbs_mode", "enable_loopback_mode",
    "disable_loopback_mode", "reset", "set_anlt", "get_anlt", "set_fec_mode", "get_fec_mode",
    "get_speed", "get_event_log", "get_ber_info", "get_fec_stats", "get_pcs_stats",
    "get_alive_status", "health_check", "reset_cause", "operation_time", "debug_dump_registers",
    "queue_info", "download_firmware", "activate_firmware", "rollback_firmware",
)


class YCable(YCableBase):
    # Vendor drivers define this themselves; ycabled compares results with it.
    EEPROM_ERROR = -1

    def __init__(self, port, logger):
        YCableBase.__init__(self, port, logger)
        init = bb_hw_client.call("init", port, "__init__")
        self._initialized = init.get("initialized", True)

    @property
    def download_firmware_status(self):
        return bb_hw_client.call("attr", self.port, "download_firmware_status")

    @download_firmware_status.setter
    def download_firmware_status(self, value):
        # YCableBase.__init__ sets it; the fake hardware owns the value.
        pass


def _forward(name):
    def method(self, *args):
        return bb_hw_client.call("driver", self.port, name, args)
    method.__name__ = name
    return method


for _name in METHODS:
    setattr(YCable, _name, _forward(_name))
