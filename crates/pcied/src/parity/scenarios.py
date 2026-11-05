"""pcied's parity scenarios: the vendor's Pcie object (what `get_pcie_check`
and `get_pcie_aer_stats` answer), the PCI devices directory, the detach
table, and passes over them.

build() sets all of that up for one half -- after the harness has emptied
the tables -- and answers the chassis; steps are ('pass',) -- one
`DaemonPcied.run()` -- or ('call', f), f(chassis) between passes.
"""

import os
import shutil
import tempfile

H = None
PD = None
ALIVE = []
SYSFS = [None]


def setup(harness, root):
    global H, PD
    H = harness
    PD = H.script('pcied', os.path.join(root, 'sonic-pcied', 'scripts', 'pcied'))

    # The daemon reads a device's id out of the kernel's PCI directory; the
    # scenario's own directory stands in for it, for both halves (the Rust
    # one is handed the same path).
    def read_id_file(device_name):
        path = os.path.join(SYSFS[0], '0000:%s' % device_name, 'device')
        if os.path.exists(path):
            with open(path, 'r') as fd:
                return fd.read().strip()
        return None
    PD.read_id_file = read_id_file


def sysfs():
    return SYSFS[0]


class Pcie(object):
    """A vendor Pcie: the parts list as checked, and the AER counters."""

    def __init__(self, check, aer=None, aer_raises=False):
        self.check = check
        self.aer = aer or {}
        self.aer_raises = aer_raises

    def get_pcie_check(self):
        return [dict(d) for d in self.check] if self.check is not None else None

    def get_pcie_aer_stats(self, domain=0, bus=0, dev=0, func=0):
        if self.aer_raises:
            raise RuntimeError('cannot read aer_dev_correctable')
        return self.aer.get((bus, dev, func), {'correctable': {}, 'fatal': {}, 'non_fatal': {}})


def device(bus, dev, fn, name, passed=True):
    return {'bus': '%02x' % bus, 'dev': '%02x' % dev, 'fn': '%x' % fn, 'id': '1f10',
            'name': name, 'result': 'Passed' if passed else 'Failed'}


def platform(check, ids, smartswitch=False, detach=(), **kwargs):
    """Set up one half's platform; answers its chassis."""
    from sonic_platform_base.chassis_base import ChassisBase
    if SYSFS[0]:
        shutil.rmtree(SYSFS[0], ignore_errors=True)
    SYSFS[0] = tempfile.mkdtemp(prefix='pcied-parity-')
    for bdf, pci_id in ids.items():
        os.makedirs(os.path.join(SYSFS[0], '0000:' + bdf))
        with open(os.path.join(SYSFS[0], '0000:' + bdf, 'device'), 'w') as f:
            f.write(pci_id + '\n')
    H.use_pcie(Pcie(check, **kwargs))
    H.FLAGS['is_smartswitch'] = smartswitch
    table = H.Table(H.Db('STATE_DB'), 'PCIE_DETACH_INFO')
    for key, bus_info, state in detach:
        table.set(key, [('bus_info', bus_info), ('dpu_state', state)])

    class Chassis(ChassisBase):
        def is_smartswitch(self):
            return smartswitch
    return Chassis()


def all_present():
    def build():
        return platform([device(0, 1, 0, 'PCI A'), device(3, 0, 1, 'PCI B')],
                        {'00:01.0': '0x1f10', '03:00.1': '0xcf70'},
                        aer={(0, 1, 0): {'correctable': {'RxErr': '3', 'BadTLP': '0'},
                                         'fatal': {'DLP': '0'}, 'non_fatal': {}}})
    return build, [('pass',), ('pass',)]


def one_missing():
    def build():
        return platform([device(0, 1, 0, 'PCI A'), device(5, 0, 0, 'PCI C', passed=False)],
                        {'00:01.0': '0x1f10'})
    return build, [('pass',)]


def detaching_dpu():
    def build():
        return platform([device(0, 1, 0, 'PCI A'), device(8, 0, 0, 'DPU0', passed=False),
                         device(9, 0, 0, 'DPU1', passed=False)],
                        {'00:01.0': '0x1f10'}, smartswitch=True,
                        detach=[('DPU0', '0000:08:00.0', 'detaching'),
                                ('DPU1', '0000:09:00.0', 'attached')])
    return build, [('pass',)]


def no_id_file():
    def build():
        return platform([device(0, 1, 0, 'PCI A'), device(0, 2, 0, 'PCI D')], {'00:01.0': '0x1f10'})
    return build, [('pass',)]


def aer_unreadable():
    def build():
        return platform([device(0, 1, 0, 'PCI A')], {'00:01.0': '0x1f10'}, aer_raises=True)
    return build, [('pass',)]


def nothing_to_check():
    def build():
        return platform(None, {})
    return build, [('pass',)]


SCENARIOS = [all_present, one_missing, detaching_dpu, no_id_file, aer_unreadable, nothing_to_check]


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
    d = PD.DaemonPcied('pcied')
    d.stop_event.wait = lambda *args, **kwargs: False
    ALIVE.append(d)
    for step in steps:
        if step[0] == 'pass':
            d.run()
        else:
            step[1](c)
