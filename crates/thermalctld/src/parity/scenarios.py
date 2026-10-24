"""thermalctld's parity scenarios: what the platform is, and what the daemon
is asked to do to it, step by step.

Each scenario is a function answering (build, steps, intervals):

- build() makes a fresh chassis from the Python daemon's own test mocks
  (sonic-thermalctld/tests/mock_platform.py).  Each half gets its own.
- steps are ('fan',), ('temp', seconds) -- one FanUpdater.update() or one
  TemperatureUpdater.update() at that many seconds into the run -- or
  ('call', f), f(chassis) changing the platform between updates.
- intervals is platform.json's polling_interval as ThermalMonitor resolves
  it: {'psu': s or None, 'thermals': {name: s}}.
"""

import functools
import gc
import os
import signal
import threading
import time

H = None
TD = None
MP = None
# The last run's updaters, kept alive until the next one: their destructors
# delete every row they published (`thermalctld:FanUpdater.__del__`), which
# is the daemon exiting, not what it publishes while it runs.
ALIVE = []


def setup(harness, root):
    global H, TD, MP
    H = harness
    tests = os.path.join(root, 'sonic-thermalctld', 'tests')
    MP = H.load_file('thermalctld_parity_mock_platform', os.path.join(tests, 'mock_platform.py'))
    TD = H.script('thermalctld', os.path.join(root, 'sonic-thermalctld', 'scripts', 'thermalctld'))


# -- building blocks ---------------------------------------------------------

def fan(name=None, cls=None):
    f = (cls or MP.MockFan)()
    f._name = name
    return f


def drawer(index, fans, name=-1):
    d = MP.MockFanDrawer(index)
    if name != -1:
        d._name = name
    d._fan_list.extend(fans)
    return d


def thermal(name=None, cls=None):
    """A thermal reading floats, as a sensor driver's do; the thresholds stay
    the mock's ints, as a platform's configured limits often are."""
    t = (cls or MP.MockThermal)()
    t._name = name
    t._temperature, t._minimum_temperature, t._maximum_temperature = 2.0, 1.0, 5.0
    return t


def heat(t, temperature, high=None, low=None):
    t._temperature = temperature
    if high is not None:
        t._high_threshold = high
    if low is not None:
        t._low_threshold = low


def psu(name=None, fans=(), thermals=()):
    p = MP.MockPsu()
    p._name = name
    p._fan_list.extend(fans)
    p._thermal_list.extend(thermals)
    return p


def chassis(drawers=(), thermals=(), psus=(), pdbs=()):
    c = MP.MockChassis()
    for d in drawers:
        c._fan_drawer_list.append(d)
        c._fan_list.extend(d._fan_list)
    c._thermal_list.extend(thermals)
    c._psu_list.extend(psus)
    c._pdb_list = list(pdbs)
    return c


def _raising(getter):
    def f(self):
        raise RuntimeError('i2c read failed: {}'.format(getter))
    return f


def thermal_raising_in(getter, name):
    t = thermal(name)
    setattr(t, getter, _raising(getter).__get__(t))
    return t


def fan_raising_in(getter, name):
    f = fan(name)
    setattr(f, getter, _raising(getter).__get__(f))
    return f


def unnamed_drawer(index, fans):
    """A drawer whose get_name is the base class's: NotImplementedError."""
    d = drawer(index, fans)

    def get_name():
        raise NotImplementedError
    d.get_name = get_name
    return d


def no_presence_pdb(name, thermals):
    p = psu(name, thermals=thermals)

    def get_presence():
        raise NotImplementedError
    p.get_presence = get_presence
    return p


NO_INTERVALS = {'psu': None, 'thermals': {}}


# -- the scenarios ------------------------------------------------------------

def healthy():
    def build():
        return chassis(
            drawers=[drawer(0, [fan('fan1'), fan('fan2')]), drawer(1, [fan('fan3'), fan('fan4')])],
            thermals=[thermal('ASIC'), thermal('CPU Core 0'), thermal('Ambient Port Side')],
            psus=[psu('PSU 1', fans=[fan('psu1_fan1')], thermals=[thermal('PSU-1 Temp')])])
    return build, [('fan',), ('temp', 0)], NO_INTERVALS


def unnamed_devices():
    # Every fallback name at once: fans, thermals, a PSU and its devices.
    def build():
        return chassis(
            drawers=[drawer(0, [fan(), fan()])],
            thermals=[thermal(), thermal()],
            psus=[psu(None, fans=[fan()], thermals=[thermal()])])
    return build, [('fan',), ('temp', 0)], NO_INTERVALS


def thermal_raises():
    # The base mock's error thermal raises on get_temperature; another raises
    # mid-row, after the temperature, on a threshold.
    def build():
        return chassis(thermals=[thermal('Good'), thermal('Broken', MP.MockErrorThermal),
                                 thermal_raising_in('get_high_threshold', 'Halfway')])
    return build, [('temp', 0), ('temp', 1)], NO_INTERVALS


def fan_raises():
    # Raising at each point that leaves Python somewhere different: the speed
    # (entity row and LED pass only), the LED (its own pass), the model (all
    # but the FAN_INFO row), the position (nothing at all).
    def build():
        return chassis(drawers=[drawer(0, [fan('fan1'), fan('fan2', MP.MockErrorFan)]),
                                drawer(1, [fan_raising_in('get_status_led', 'fan3')]),
                                drawer(2, [fan_raising_in('get_model', 'fan4')]),
                                drawer(3, [fan_raising_in('get_position_in_parent', 'fan5')])])
    return build, [('fan',), ('fan',)], NO_INTERVALS


def absent_fan_raises():
    # An absent fan's readings are not asked (`thermalctld:FanUpdater
    # ._refresh_fan_status`), so getters that would raise never do: fan1's
    # speed, fan3's direction.  fan4 is absent with every getter working,
    # then inserted.
    def build():
        f1 = fan_raising_in('get_speed', 'fan1')
        f1.set_presence(False)
        f3 = fan_raising_in('get_direction', 'fan3')
        f3.set_presence(False)
        f4 = fan('fan4')
        f4.set_presence(False)
        return chassis(drawers=[drawer(0, [f1, fan('fan2')]), drawer(1, [f3]), drawer(2, [f4])])

    def insert(c):
        c._fan_list[3].set_presence(True)
    return build, [('fan',), ('call', insert), ('fan',)], NO_INTERVALS


def virtual_and_unnamed_drawers():
    # A drawer that names itself 'N/A' (fixed fans, one virtual drawer) and
    # one that does not implement get_name.
    def build():
        c = chassis(drawers=[drawer(0, [fan('fan1'), fan('fan2')], name='N/A'),
                             unnamed_drawer(1, [fan('fan3')])])
        return c
    return build, [('fan',)], NO_INTERVALS


def fan_states():
    def build():
        c = chassis(drawers=[drawer(0, [fan('fan1')])])
        c.make_absent_fan()
        c.make_faulty_fan()
        c.make_under_speed_fan()
        c.make_over_speed_fan()
        return c

    def recover(c):
        for f in c._fan_list:
            f.set_presence(True)
            f.set_status(True)
            f.make_normal_speed()
    return build, [('fan',), ('call', recover), ('fan',)], NO_INTERVALS


def temperature_states():
    # Over and under the thresholds, then back: the four warnings, with an
    # integer threshold and a float one.
    def build():
        hot, cold = thermal('Hot'), thermal('Cold')
        heat(hot, 3.5, high=2)
        heat(cold, 0.5, low=1.5)
        return chassis(thermals=[hot, cold])

    def recover(c):
        heat(c._thermal_list[0], 1.5)
        heat(c._thermal_list[1], 2.0)
    return build, [('temp', 0), ('call', recover), ('temp', 1)], NO_INTERVALS


def integer_readings():
    # The mock's own readings, which are ints: Python publishes them as the
    # platform answered them.
    def build():
        t = MP.MockThermal()
        t._name = 'Int'
        t.make_over_temper()
        return chassis(thermals=[t])
    return build, [('temp', 0)], NO_INTERVALS


def temperature_unreadable():
    # No temperature: nothing derived from it is read, so thresholds that
    # would raise are never asked.
    def build():
        t = thermal('NoReading')
        t.get_temperature = lambda: None
        for g in ('get_high_threshold', 'get_low_threshold', 'get_high_critical_threshold',
                  'get_low_critical_threshold', 'get_minimum_recorded', 'get_maximum_recorded'):
            setattr(t, g, _raising(g).__get__(t))
        return chassis(thermals=[t, thermal('Fine')])
    return build, [('temp', 0)], NO_INTERVALS


def polling_intervals():
    # 'Fast' every 10 s, 'Slow' at the default the named one implies, the
    # PSU's every 30 s; the readings change between passes, so which ones a
    # pass re-read shows in what it published.
    def build():
        return chassis(thermals=[thermal('Fast'), thermal('Slow')],
                       psus=[psu('PSU 1', thermals=[thermal('PSU-1 Temp')])])

    def warmer(c):
        for t in c._thermal_list + c._psu_list[0]._thermal_list:
            t._temperature += 1.0
    return build, [('temp', 0), ('call', warmer), ('temp', 3), ('call', warmer), ('temp', 11),
                   ('call', warmer), ('temp', 31), ('call', warmer), ('temp', 61)], \
        {'psu': 30.0, 'thermals': {'Fast': 10.0}}


def pdb_without_presence():
    def build():
        return chassis(thermals=[thermal('ASIC')],
                       pdbs=[no_presence_pdb('PDB 1', [thermal('PDB-1 Temp')])])
    return build, [('temp', 0)], NO_INTERVALS


def absent_psu():
    def build():
        p = psu('PSU 1', fans=[fan('psu1_fan1')], thermals=[thermal('PSU-1 Temp')])
        p.set_presence(False)
        return chassis(thermals=[thermal('ASIC')], psus=[p])

    def insert(c):
        c._psu_list[0].set_presence(True)
    return build, [('fan',), ('temp', 0), ('call', insert), ('fan',), ('temp', 1)], NO_INTERVALS


SCENARIOS = [healthy, unnamed_devices, thermal_raises, fan_raises, absent_fan_raises,
             virtual_and_unnamed_drawers, fan_states, temperature_states, integer_readings, temperature_unreadable, polling_intervals,
             pdb_without_presence, absent_psu]


def names():
    return [s.__name__ for s in SCENARIOS]


def scenario(name):
    return dict((s.__name__, s) for s in SCENARIOS)[name]()


def run_python(name):
    """The Python half: thermalctld's own updaters over a fresh chassis."""
    build, steps, intervals = scenario(name)
    del ALIVE[:]
    H.reset()
    # The bad-fan counts are class state in Python, one process's worth; a
    # scenario starts from none, as the Rust updater it is compared with does.
    TD.FanStatus.reset_fan_counter()
    c = build()
    H.use_chassis(c)
    stop = threading.Event()
    fans = TD.FanUpdater(c, stop)
    temps = TD.TemperatureUpdater(
        c, stop, psu_interval=intervals['psu'], thermal_intervals=dict(intervals['thermals']),
        default_interval=60 if intervals['thermals'] else None)
    ALIVE.extend([fans, temps])
    for step in steps:
        if step[0] == 'fan':
            fans.update()
        elif step[0] == 'temp':
            temps.update(now=1000.0 + step[1])
        else:
            step[1](c)


# -- stopping -----------------------------------------------------------------
#
# Not a step list: the whole daemon on each side -- `thermalctld:main` and
# `main::run` -- with SIGTERM arriving while the vendor's thermal manager is
# still in its start-up wait.

# How long the vendor waits when nothing ends the wait: what a daemon that
# did not hand the shutdown on would sit out (300 s on SPC1).
VENDOR_WAIT = 30.0
# How long either side may take, from the signal to its rows being gone.
PROMPT = 5.0


def vendor_platform():
    """A chassis whose vendor does what mlnx does.

    While the chassis is built it installs a SIGTERM/SIGINT handler that sets
    an event and chains to whatever was installed before it
    (`utils.watch_shutdown_signals`, called from `Chassis.__init__`), and its
    thermal manager's `initialize()` waits on that event for files that never
    come (`ThermalUpdater.unlink_hw_mgmt_thermal_files` on SPC1).  Returns the
    chassis, the installer for `use_chassis`, and the event `initialize()`
    sets once it is waiting.
    """
    from sonic_py_common import logger
    log = logger.Logger('thermalctld')
    shutdown = threading.Event()
    waiting = threading.Event()
    installed = [False]

    def on_shutdown(previous, signum, frame):
        shutdown.set()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            # mlnx re-raises the signal here, `signal.signal(signum, SIG_DFL)`
            # and `os.kill(os.getpid(), signum)`: the process would end.
            log.log_error('Re-raised signal {}: the process ends here'.format(signum))

    def watch_shutdown_signals():
        # Once per process, as mlnx's.  Its main-thread guard is left out:
        # the table is the harness's, which any thread may write.
        if installed[0]:
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, functools.partial(on_shutdown, signal.getsignal(sig)))
        installed[0] = True

    class ThermalManager(MP.MockThermalManager):
        @classmethod
        def initialize(cls):
            waiting.set()
            if shutdown.wait(VENDOR_WAIT):
                log.log_notice('Waiting for thermal files aborted: shutting down')
            else:
                log.log_notice('Timed out waiting for thermal files')

        @classmethod
        def load(cls, policy_file_name):
            pass

        @classmethod
        def init_thermal_algorithm(cls, chassis):
            pass

    c = chassis(drawers=[drawer(0, [fan('fan1')])], thermals=[thermal('ASIC')])
    c._thermal_manager = ThermalManager()
    return c, watch_shutdown_signals, waiting


def _published():
    return (H.has_row('STATE_DB', 'TEMPERATURE_INFO', 'ASIC')
            and H.has_row('STATE_DB', 'FAN_INFO', 'fan1'))


def stop_while_starting_python():
    """The Python half: `thermalctld:main` on a thread of its own, SIGTERM
    delivered once the manager is waiting and the first pass has published.
    Returns the seconds from the signal to the daemon gone and its rows with
    it."""
    del ALIVE[:]
    H.reset()
    TD.FanStatus.reset_fan_counter()
    c, watch, waiting = vendor_platform()
    H.use_chassis(c, watch)

    def main():
        # `thermalctld:main`, with the first pass at once rather than after
        # the default 5 s.
        daemon = TD.ThermalControlDaemon(0, 60, 30)
        while daemon.run():
            pass
        daemon.deinit()

    t = threading.Thread(target=main)
    t.start()
    if not waiting.wait(PROMPT):
        raise AssertionError("the thermal manager never started waiting")
    deadline = time.monotonic() + PROMPT
    while not _published():
        if time.monotonic() > deadline:
            raise AssertionError('the first pass never published')
        time.sleep(0.01)

    begin = time.monotonic()
    H.deliver(signal.SIGTERM)
    t.join(VENDOR_WAIT * 2)
    if t.is_alive():
        raise AssertionError('Python thermalctld did not stop')
    # The process exiting: the interpreter lets go of its signal handlers,
    # and with them of the daemon, whose updaters' destructors clear its rows
    # (`thermalctld:FanUpdater.__del__`, `thermalctld:TemperatureUpdater.__del__`).
    H.reset_signals()
    gc.collect()
    return time.monotonic() - begin


def stop_while_starting_rust():
    """The Rust half's platform: the same vendor, over a facade that has not
    yet built a chassis in this process.  Returns the event the manager sets
    once it is waiting."""
    H.reset()
    # The facade keeps the vendor's handler, and its one notification, for
    # the life of the process (`sonic_platform_api._escape_hatch`); a test
    # binary is one process for every scenario in it.
    import sonic_platform_api._escape_hatch as hatch
    hatch._shutdown_handlers.clear()
    hatch._shutdown_notified = threading.Lock()
    c, watch, waiting = vendor_platform()
    H.use_chassis(c, watch)
    return waiting
