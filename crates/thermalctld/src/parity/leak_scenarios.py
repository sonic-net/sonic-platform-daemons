"""Drive the original Python leak worker and the Rust loop with shared inputs."""
from datetime import datetime, timedelta
import logging
import os
from unittest import mock

H = None
TD = None
ALIVE = []


def setup(harness, root):
    global H, TD
    H = harness
    TD = H.script('thermalctld', os.path.join(root, 'sonic-thermalctld', 'scripts', 'thermalctld'))


def names():
    return ['transitions', 'sensor_failure', 'minimum_failure', 'policy_failure',
            'live_override', 'zero_timer', 'timer_expiry', 'restart', 'critical_downgrade_timer',
            'no_sensors', 'startup_sensor_failure',
            'test_only', 'test_timer', 'mixed_test_first', 'mixed_physical_first',
            'ignored_test_profile', 'source_switch', 'test_flag_transition',
            'restart_test', 'restart_physical_to_test', 'restart_test_to_physical']


def step(severities=('MINOR', 'MINOR'), minimum=2, override=None, **extra):
    return dict(severities=severities, minimum=minimum, override=override,
                failed_sensor=False, failed_minimum=False, failed_policy=False,
                seconds=0, **extra)


class Profile:
    def __init__(self, fixture): self.fixture = fixture
    def get_type(self): return 'p'
    def get_leak_max_minor_duration_sec(self): return self.fixture.limit


class Sensor:
    def __init__(self, fixture, index): self.fixture, self.index = fixture, index
    def get_name(self): return 'leakage{}'.format(self.index + 1)
    def is_test_leak(self): return self.index in self.fixture.current.get('tests', ())
    def is_leak_sensor_ok(self): return True
    def is_leak(self):
        if self.index == 0 and self.fixture.current['failed_sensor']:
            raise OSError('sensor unavailable')
        return self.fixture.current['severities'][self.index] is not None
    def get_leak_severity(self):
        name = self.fixture.current['severities'][self.index]
        return getattr(TD.LeakSeverity, name) if name else TD.LeakSeverity.MINOR
    def get_leak_profile(self):
        if self.index in self.fixture.current.get('bad_profiles', ()):
            raise RuntimeError('ignored test profile must not be read')
        return Profile(self.fixture)
    def get_leak_sensor_type(self): return 'leak'
    def get_leak_sensor_location(self): return 'rack'


class Cooling:
    def __init__(self, fixture):
        self.fixture = fixture
        self.leakage_sensors = [Sensor(fixture, i) for i in range(fixture.sensor_count)]
    def get_all_leak_sensors(self): return self.leakage_sensors
    def get_all_profiles(self): return [Profile(self.fixture)]
    def get_major_leak_num_min_sensors(self):
        if self.fixture.current['failed_minimum']:
            raise ValueError('minimum unavailable')
        return self.fixture.current['minimum']


class Chassis:
    def __init__(self, fixture): self.cooling = Cooling(fixture)
    def get_liquid_cooling(self): return self.cooling


class Fixture:
    def __init__(self, name):
        self.name, self.index, self.limit = name, 0, 10
        self.sensor_count = 2
        self.base = datetime.now().replace(microsecond=0)
        if name == 'transitions':
            self.steps = [step(), step(('CRITICAL', 'MINOR')), step(),
                          step(('MINOR', None)), step((None, None))]
        elif name == 'no_sensors':
            self.sensor_count = 0
            self.steps = [step(), step()]
        elif name == 'startup_sensor_failure':
            self.steps = [dict(step(), failed_sensor=True), step(), step((None, None))]
        elif name == 'test_only':
            self.steps = [step(('CRITICAL', None), tests=(0,)),
                          step(('MINOR', None), tests=(0,)), step((None, None))]
        elif name == 'test_timer':
            self.steps = [step(('MINOR', None), minimum=0, tests=(0,)),
                          dict(step(('MINOR', None), minimum=0, tests=(0,)), seconds=11)]
        elif name in ('mixed_test_first', 'mixed_physical_first'):
            test_index = 0 if name == 'mixed_test_first' else 1
            severities = ('CRITICAL', 'MINOR') if test_index == 0 else ('MINOR', 'CRITICAL')
            self.steps = [step(severities, tests=(test_index,)),
                          step(('MINOR', 'MINOR'), tests=(test_index,))]
        elif name == 'ignored_test_profile':
            self.steps = [step(tests=(0,), bad_profiles=(0,)),
                          dict(step(tests=(0,), bad_profiles=(0,)), seconds=11)]
        elif name == 'source_switch':
            self.steps = [step(('MINOR', None), tests=(0,)),
                          dict(step(('MINOR', None), tests=(0,)), seconds=11),
                          dict(step(('MINOR', None)), seconds=12),
                          dict(step(('MINOR', None)), seconds=23),
                          dict(step(('CRITICAL', None), tests=(0,)), seconds=24),
                          dict(step(('CRITICAL', None)), seconds=25)]
        elif name == 'test_flag_transition':
            self.steps = [step(('CRITICAL', None), tests=(0,)),
                          step(('CRITICAL', None)), step(('CRITICAL', None), tests=(0,))]
        elif name == 'restart_test_to_physical':
            self.steps = [step(('MINOR', None)), dict(step(('MINOR', None)), seconds=11),
                          step((None, None))]
        elif name in ('restart_test', 'restart_physical_to_test'):
            self.steps = [step(('MINOR', None), tests=(0,)),
                          dict(step(('MINOR', None), tests=(0,)), seconds=11),
                          step((None, None))]
        elif name == 'sensor_failure':
            self.steps = [step(('CRITICAL', None)),
                          dict(step(('CRITICAL', None)), failed_sensor=True),
                          step(('CRITICAL', None)), step((None, None))]
        elif name == 'minimum_failure':
            self.steps = [step(), dict(step(), failed_minimum=True), step()]
        elif name == 'policy_failure':
            self.steps = [step(), dict(step(), failed_policy=True), step()]
        elif name == 'live_override':
            self.steps = [step(minimum=3), step(minimum=3, override='2'),
                          step(minimum=3), step(minimum=2)]
        elif name == 'zero_timer':
            self.limit = 0
            self.steps = [step(('MINOR', None), minimum=0),
                          dict(step(('MINOR', None), minimum=0), seconds=30)]
        elif name == 'timer_expiry':
            self.steps = [step(('MINOR', None), minimum=0),
                          dict(step(('MINOR', None), minimum=0), seconds=11),
                          dict(step((None, None), minimum=0), seconds=12)]
        elif name == 'restart':
            self.steps = [step(('MINOR', None)), step((None, None)),
                          step(('CRITICAL', None))]
        elif name == 'critical_downgrade_timer':
            self.steps = [step(('CRITICAL', None)),
                          dict(step(('MINOR', None)), seconds=11)]
        else:
            raise AssertionError(name)

    @property
    def current(self): return self.steps[self.index]
    def chassis(self): return Chassis(self)
    def seconds(self): return self.current['seconds']
    def advance(self):
        self.index += 1
        return self.index == len(self.steps)
    def policy_entry(self):
        if self.current['failed_policy']:
            raise OSError('policy unavailable')
        value = self.current['override']
        return None if value is None else [('system_major_leak_num_min_sensors', value)]
    def initial_rows(self):
        if self.name not in ('restart', 'restart_test', 'restart_physical_to_test', 'restart_test_to_physical'): return {}
        timestamp = (self.base - timedelta(seconds=12)).strftime('%Y%m%d %H:%M:%S')
        return {
            'LIQUID_COOLING_INFO': {'leakage1': {
                'name': 'leakage1', 'leaking': 'Yes', 'leak_status': 'Yes',
                'leak_sensor_status': 'Good', 'type': 'leak', 'location': 'rack',
                'leak_severity': 'CRITICAL', 'timestamp': timestamp,
                'test_leak': 'Enabled' if self.name in ('restart_test', 'restart_test_to_physical') else 'Disabled'}},
            'SYSTEM_LEAK_STATUS': {'system': {'device_leak_status': 'CRITICAL', 'timestamp': timestamp}},
        }


def make(name):
    return Fixture(name)


def leak_tables():
    state = H.tables().get('STATE_DB', {})
    selected = {name: state[name] for name in
                ('LIQUID_COOLING_INFO', 'SYSTEM_LEAK_STATUS', 'LEAK_PROFILE')
                if state.get(name)}
    return {'STATE_DB': selected} if selected else {}


def run_python(name):
    ALIVE.clear()
    H.reset()
    fixture = Fixture(name)
    for table, rows in fixture.initial_rows().items():
        target = H.Table(H.Db('STATE_DB'), table)
        for key, values in rows.items(): target.set(key, list(values.items()))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixture.base + timedelta(seconds=fixture.seconds())

    class Stop:
        def is_set(self): return fixture.index == len(fixture.steps)

    # Record the real EventLogger's file channel separately from syslog.
    bmc = []
    class Events(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            level = record.levelname
            if message.startswith('NOTICE: '):
                level, message = 'NOTICE', message[len('NOTICE: '):]
            if level in ('NOTICE', 'WARNING', 'ERROR'):
                bmc.append((level, message))

    results = []
    def wait(_seconds):
        results.append((leak_tables(), H.logs(), list(bmc)))
        fixture.advance()

    with mock.patch.object(TD, 'datetime', Clock), \
            mock.patch.dict(os.environ, {'THERMALCTLD_UNIT_TESTING': '1'}):
        updater = TD.LiquidCoolingUpdater(fixture.chassis(), 0.001)
        ALIVE.append(updater)
        def read_policy(_key):
            values = fixture.policy_entry()
            return values is not None, values or []
        updater.policy_table.get = read_policy
        handler = Events()
        logger = updater.event_logger._file_logger
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(TD.time, 'sleep', wait):
                updater.task_worker(Stop())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
    return results
