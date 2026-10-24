"""Real SONiC platform APIs with only the physical EEPROM transport replaced."""

from sonic_platform_base.chassis_base import ChassisBase
from sonic_platform_base.sonic_xcvr.sfp_optoe_base import SfpOptoeBase


class EmulatedSfp(SfpOptoeBase):
    """A pluggable module backed by an xcvr-emu gRPC process."""

    def __init__(self, index, client):
        super().__init__()
        self.index = index
        self.client = client
        self.read_count = 0
        self.writes = []

    def get_presence(self):
        """Read actual emulated presence, propagating transport failures."""
        return self.client.get_info().present

    def is_replaceable(self):
        """Emulate a pluggable rather than a CPO device."""
        return True

    def get_eeprom_path(self):
        """There is no sysfs EEPROM; reads and writes use gRPC instead."""
        return None

    def read_eeprom(self, offset, num_bytes):
        """Read bytes using the same linear offsets as a physical platform."""
        data = self.client.read_linear(offset, num_bytes)
        self.read_count += 1
        return data

    def write_eeprom(self, offset, num_bytes, write_buffer):
        """Write bytes and record successful hardware accesses for assertions."""
        if len(write_buffer) != num_bytes:
            raise ValueError('EEPROM write length does not match the buffer')
        data = bytes(write_buffer)
        self.client.write_linear(offset, data)
        self.writes.append((offset, data))
        return True

    def set_present(self, present):
        """Change presence and invalidate SONiC's cached transceiver API."""
        self.client.set_present(present)
        self.remove_xcvr_api()


class EmulatedChassis(ChassisBase):
    """Minimal chassis exposing independently emulated physical ports."""

    def __init__(self, sfps):
        super().__init__()
        self.sfps = sfps

    def get_sfp(self, index):
        """Return the requested physical port."""
        try:
            return self.sfps[index]
        except KeyError:
            raise IndexError('No emulated SFP at index {}'.format(index)) from None

    def get_cpo(self, index):
        """The emulated chassis has no CPO devices."""
        return None
