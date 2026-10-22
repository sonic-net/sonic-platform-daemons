"""psud's parity scenarios: what the platform is, and what happens to it
between passes.

Each scenario is a function answering (build, steps): build() makes a fresh
chassis from the Python daemon's own test mocks
(sonic-psud/tests/mock_platform.py), and steps are ('pass',) -- one
`DaemonPsud.run()` -- or ('call', f), f(chassis) changing the platform between
passes.
"""

import os

H = None
PS = None
MP = None
# The last run's daemon, kept alive until the next one: its destructor
# deletes what it published (`psud:DaemonPsud.__del__`), which is the daemon
# exiting, not what it publishes while it runs.
ALIVE = []


def setup(harness, root):
    global H, PS, MP
    H = harness
    tests = os.path.join(root, 'sonic-psud', 'tests')
    MP = H.load_file('psud_parity_mock_platform', os.path.join(tests, 'mock_platform.py'))
    PS = H.script('psud', os.path.join(root, 'sonic-psud', 'scripts', 'psud'))


def fan(name, position=1, **kwargs):
    return MP.MockFan(name, position, **kwargs)


def psu(name, position, fans=(), **kwargs):
    p = MP.MockPsu(name, position, **kwargs)
    p._fan_list.extend(fans)
    return p


def chassis(psus=(), pdbs=()):
    c = MP.MockChassis()
    c._psu_list.extend(psus)
    c._pdb_list.extend(pdbs)
    return c


def _raising(getter):
    def f(self):
        raise RuntimeError('i2c read failed: {}'.format(getter))
    return f


def raising_in(device, getter):
    setattr(device, getter, _raising(getter).__get__(device))
    return device


def healthy():
    def build():
        return chassis(psus=[psu('PSU 1', 1, fans=[fan('psu1_fan1')]),
                             psu('PSU 2', 2, fans=[fan('psu2_fan1')])])
    return build, [('pass',), ('pass',), ('pass',)]


def unnamed_devices():
    def build():
        return chassis(psus=[psu(None, 1, fans=[fan(None)]), psu(None, 2)],
                       pdbs=[psu(None, 1, fans=[fan(None)])])
    return build, [('pass',), ('pass',)]


def psu_raises():
    # One PSU's voltage will not read, mid-row.
    def build():
        return chassis(psus=[psu('PSU 1', 1), raising_in(psu('PSU 2', 2), 'get_voltage')])
    return build, [('pass',), ('pass',)]


def presence_changes():
    def build():
        return chassis(psus=[psu('PSU 1', 1, fans=[fan('psu1_fan1')]),
                             psu('PSU 2', 2, fans=[fan('psu2_fan1')], presence=False)])

    def insert(c):
        c._psu_list[1].set_presence(True)

    def remove(c):
        c._psu_list[1].set_presence(False)
    return build, [('pass',), ('call', insert), ('pass',), ('call', remove), ('pass',)]


def psu_fan_raises():
    # The fans are read on the first pass and on a presence change only.
    def build():
        return chassis(psus=[psu('PSU 1', 1, fans=[raising_in(fan('psu1_fan1'), 'get_speed')]),
                             psu('PSU 2', 2, fans=[fan('psu2_fan1')])])
    return build, [('pass',), ('pass',)]


def psu_fan_absent():
    # A fan that says it is absent in a PSU that is present: psud gates a
    # PSU fan's readings on the PSU's presence alone
    # (`psud:DaemonPsud._update_psu_fan_data`), never asking the fan.
    def build():
        return chassis(psus=[psu('PSU 1', 1, fans=[fan('psu1_fan1', presence=False),
                                                   fan('psu1_fan2', 2)])])
    return build, [('pass',), ('pass',)]


def pdb_without_presence():
    def build():
        pdb = psu('PDB 1', 1, fans=[fan('pdb1_fan1')])

        def get_presence():
            raise NotImplementedError
        pdb.get_presence = get_presence
        return chassis(psus=[psu('PSU 1', 1)], pdbs=[pdb])
    return build, [('pass',), ('pass',)]


def out_of_range():
    # Voltage over its high threshold, temperature over its own, power not
    # good -- then all three back.
    def build():
        return chassis(psus=[psu('PSU 1', 1, voltage=14.0), psu('PSU 2', 2, temp=60.0),
                             psu('PSU 3', 3, status=False)])

    def recover(c):
        c._psu_list[0]._voltage = 12.0
        c._psu_list[1]._temp = 30.0
        c._psu_list[2]._status = True
    return build, [('pass',), ('call', recover), ('pass',)]


SCENARIOS = [healthy, unnamed_devices, psu_raises, presence_changes, psu_fan_raises,
             psu_fan_absent, pdb_without_presence, out_of_range]


def names():
    return [s.__name__ for s in SCENARIOS]


def scenario(name):
    return dict((s.__name__, s) for s in SCENARIOS)[name]()


def run_python(name):
    """The Python half: psud's own daemon class, one run() per pass."""
    build, steps = scenario(name)
    del ALIVE[:]
    H.reset()
    c = build()
    H.use_chassis(c)
    d = PS.DaemonPsud('psud')
    # run() waits out the period on the stop event first; a pass here is
    # the work after the wait.
    d.stop_event.wait = lambda *args, **kwargs: False
    ALIVE.append(d)
    for step in steps:
        if step[0] == 'pass':
            d.run()
        else:
            step[1](c)
