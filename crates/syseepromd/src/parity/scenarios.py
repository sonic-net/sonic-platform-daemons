"""syseepromd's parity scenarios: an ONIE TLV EEPROM, decoded by the base
class's own `TlvInfoDecoder`, and what happens to the published table between
passes.

Each scenario answers (build, steps): build() makes a fresh chassis whose
`get_eeprom()` is a decoder over the given bytes, and steps are ('pass',) --
one `DaemonSyseeprom.run()` -- or ('call', f), f(chassis) changing the
platform or the table between passes.  The start-up post is the
constructor's, before any step.
"""

import struct
import zlib

H = None
SD = None
ALIVE = []


def setup(harness, root):
    global H, SD
    import os
    H = harness
    SD = H.script('syseepromd', os.path.join(root, 'sonic-syseepromd', 'scripts', 'syseepromd'))


def tlv(code, value):
    if isinstance(value, str):
        value = value.encode('ascii')
    return bytes([code, len(value)]) + value


def image(tlvs, bad_crc=False):
    body = b''.join(tlvs)
    header = b'TlvInfo\x00' + bytes([0x01]) + struct.pack('>H', len(body) + 6)
    data = header + body + bytes([0xFE, 4])
    crc = zlib.crc32(data) & 0xffffffff
    if bad_crc:
        crc ^= 1
    return bytearray(data + struct.pack('>I', crc))


GOOD = [tlv(0x21, 'SN0000-A'), tlv(0x22, 'PN-0001'), tlv(0x23, 'SERIAL0001'),
        tlv(0x24, bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55])),
        tlv(0x25, '01/02/2026 03:04:05'), tlv(0x26, bytes([1])),
        tlv(0x2A, struct.pack('>H', 254)), tlv(0x2B, 'Vendor'),
        tlv(0xFD, struct.pack('>I', 8072) + b'one'), tlv(0xFD, struct.pack('>I', 8072) + b'two')]


def chassis(data):
    from sonic_platform_base.chassis_base import ChassisBase
    from sonic_platform_base.sonic_eeprom.eeprom_tlvinfo import TlvInfoDecoder

    class Eeprom(TlvInfoDecoder):
        def __init__(self):
            super(Eeprom, self).__init__('/nonexistent', 0, '', True)
            self.data = data

        def read_eeprom(self):
            return self.data

    class Chassis(ChassisBase):
        def __init__(self):
            super(Chassis, self).__init__()
            self._eeprom = Eeprom()

        def get_eeprom(self):
            return self._eeprom
    return Chassis()


def table():
    return H.Table(H.Db('STATE_DB'), 'EEPROM_INFO')


def valid():
    return (lambda: chassis(image(GOOD))), [('pass',), ('pass',)]


def bad_checksum():
    return (lambda: chassis(image(GOOD, bad_crc=True))), [('pass',)]


def unreadable():
    def build():
        c = chassis(None)
        return c

    def readable(c):
        c._eeprom.data = image(GOOD)
    # Nothing at start-up; the next pass finds the table changed (empty is
    # not what was published -- nothing was) only if the key sets differ.
    return build, [('pass',), ('call', readable), ('pass',)]


def tampered():
    def drop(c):
        table()._del('0x21')

    def wipe(c):
        for k in table().getKeys():
            table()._del(k)
    return (lambda: chassis(image(GOOD))), [('pass',), ('call', drop), ('pass',), ('call', wipe), ('pass',)]


def changed_contents():
    # The platform's EEPROM gains a TLV; the table is only rewritten when its
    # keys stop matching what was published.
    def more(c):
        c._eeprom.data = image(GOOD + [tlv(0x2D, 'Label')])

    def drop(c):
        table()._del('0x22')
    return (lambda: chassis(image(GOOD))), [('call', more), ('pass',), ('call', drop), ('pass',)]


SCENARIOS = [valid, bad_checksum, unreadable, tampered, changed_contents]


def names():
    return [s.__name__ for s in SCENARIOS]


def scenario(name):
    return dict((s.__name__, s) for s in SCENARIOS)[name]()


def run_python(name):
    build, steps = scenario(name)
    del ALIVE[:]
    H.reset()
    c = build()
    H.use_chassis(c)
    d = SD.DaemonSyseeprom()
    d.stop_event.wait = lambda *args, **kwargs: False
    ALIVE.append(d)
    for step in steps:
        if step[0] == 'pass':
            d.run()
        else:
            step[1](c)
