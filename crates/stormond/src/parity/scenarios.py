"""stormond's parity scenarios: the disks `StorageDevices()` finds, what the
FSIO JSON file and STATE_DB hold from a previous run, CONFIG_DB's intervals,
and a number of polls.

Each scenario answers (build, polls): build() sets all of that up for one
half, after the harness has emptied the tables; polls is how many cycles the
daemon waits out.  Python's `time` in stormond is a clock that the stop
event's wait advances by the polling interval, the time the Rust daemon
counts in -- so "is a sync due" is asked of the same elapsed time.
"""

import json
import os
import tempfile
import types

H = None
SM = None
SD = None
ALIVE = []
DIR = tempfile.mkdtemp(prefix='stormond-parity-')
JSON = os.path.join(DIR, 'fsio-rw-stats.json')
DISKS = [{}]


def setup(harness, root):
    global H, SM, SD
    H = harness
    # The class stormond and the facade both construct, swapped for the
    # scenario's disks before either imports it.
    from sonic_platform_base.sonic_storage import storage_devices
    SD = storage_devices
    SD.StorageDevices = StorageDevices
    SM = H.script('stormond', os.path.join(root, 'sonic-stormond', 'scripts', 'stormond'))
    SM.FSIO_RW_JSON_FILE = JSON


def json_path():
    return JSON


class Disk(object):
    """A disk whose counters move on every `fetch_parse_info`."""

    def __init__(self, model='Model', serial='SN1', reads=1000, writes=2000,
                 step=(10, 20), raises=None):
        self.model, self.serial = model, serial
        self.reads, self.writes, self.step = reads, writes, step
        self.raises = raises
        self.fetches = 0

    def fetch_parse_info(self, path):
        self.fetches += 1
        if self.step:
            self.reads += self.step[0]
            self.writes += self.step[1]

    def _maybe(self, name, value):
        if self.raises == name:
            raise RuntimeError('smartctl failed: {}'.format(name))
        return value

    def get_model(self):
        return self._maybe('get_model', self.model)

    def get_serial(self):
        return self._maybe('get_serial', self.serial)

    def get_firmware(self):
        return self._maybe('get_firmware', 'FW1')

    def get_health(self):
        return self._maybe('get_health', 99.0)

    def get_temperature(self):
        return self._maybe('get_temperature', 41.0)

    def get_fs_io_reads(self):
        return self._maybe('get_fs_io_reads', self.reads)

    def get_fs_io_writes(self):
        return self._maybe('get_fs_io_writes', self.writes)

    def get_disk_io_reads(self):
        return self._maybe('get_disk_io_reads', '1,000 [1 GB]')

    def get_disk_io_writes(self):
        return self._maybe('get_disk_io_writes', '2,000 [2 GB]')

    def get_reserved_blocks(self):
        return self._maybe('get_reserved_blocks', 100.0)


class StorageDevices(object):
    def __init__(self):
        self.devices = DISKS[0]


def platform(disks, json_doc=None, statedb=None, intervals=None):
    DISKS[0] = disks
    if os.path.exists(JSON):
        os.remove(JSON)
    if json_doc is not None:
        with open(JSON, 'w') as f:
            json.dump(json_doc, f)
    table = H.Table(H.Db('STATE_DB'), 'STORAGE_INFO')
    for key, row in (statedb or {}).items():
        table.set(key, list(row.items()))
    if intervals is not None:
        H.Table(H.Db('CONFIG_DB'), 'STORMOND_CONFIG').set('INTERVALS', list(intervals.items()))


def fresh_start():
    return (lambda: platform({'sda': Disk()})), 2


def statedb_baseline():
    # The switch stayed up: STATE_DB has what the last run published.
    def build():
        platform({'sda': Disk()}, statedb={
            'sda': {'latest_fsio_reads': '900', 'latest_fsio_writes': '1900',
                    'total_fsio_reads': '5000', 'total_fsio_writes': '9000'},
            'FSSTATS_SYNC': {'successful_sync_time': '2026-01-01 00:00:00'}})
    return build, 2


def json_baseline():
    # A reboot: only the file survived.
    def build():
        platform({'sda': Disk()}, json_doc={
            'sda': {'latest_fsio_reads': '900', 'latest_fsio_writes': '1900',
                    'total_fsio_reads': '5000', 'total_fsio_writes': '9000'},
            'successful_sync_time': '2026-01-01 00:00:00'})
    return build, 2


def counters_unreadable():
    def build():
        platform({'sda': Disk(reads=0, writes=0, step=None)}, statedb={
            'sda': {'latest_fsio_reads': '900', 'latest_fsio_writes': '1900',
                    'total_fsio_reads': '5000', 'total_fsio_writes': '9000'},
            'FSSTATS_SYNC': {'successful_sync_time': '2026-01-01 00:00:00'}})
    return build, 1


def unhandled_disk():
    return (lambda: platform({'sda': Disk(), 'sdb': None})), 1


def short_intervals():
    # Poll every minute, sync every hundred seconds: a sync falls due.
    def build():
        platform({'sda': Disk()}, intervals={'daemon_polling_interval': '60',
                                              'fsstats_sync_interval': '100'})
    return build, 3


def bad_interval():
    def build():
        platform({'sda': Disk()}, intervals={'daemon_polling_interval': 'abc'})
    return build, 1


def disk_getter_raises():
    return (lambda: platform({'sda': Disk(raises='get_health'), 'sdb': Disk(serial='SN2')})), 1


SCENARIOS = [fresh_start, statedb_baseline, json_baseline, counters_unreadable, unhandled_disk,
             short_intervals, bad_interval, disk_getter_raises]


def names():
    return [s.__name__ for s in SCENARIOS]


def scenario(name):
    return dict((s.__name__, s) for s in SCENARIOS)[name]()


def run_python(name, polls):
    """The Python half: what main() does, then the signal handler's sync."""
    build, _ = scenario(name)
    del ALIVE[:]
    H.reset()
    build()
    clock = [1.7e9]
    SM.time = types.SimpleNamespace(time=lambda: clock[0])
    d = SM.DaemonStorage('stormond')

    def wait(timeout=None):
        clock[0] += timeout
        return False
    d.stop_event.wait = wait
    ALIVE.append(d)
    d.get_static_fields_update_state_db()
    # The Rust loop publishes once more than it waits.
    for _ in range(polls):
        d.run()
    d.get_configdb_intervals()
    d.get_dynamic_fields_update_state_db()
    # `signal_handler` on SIGTERM, without its two notices about the signal.
    if d.sync_fsio_rw_json():
        d.write_sync_time_statedb()
    else:
        d.log.log_warning("Unable to sync latest and total procfs RW to disk")
