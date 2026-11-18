"""Integration tests: real python3-swsscommon against local redis instances, one per emulated namespace.

They run ONLY in their own pytest process:

    XCVRD_INTEGRATION=1 python3 -m pytest -q -o addopts="" tests/integration

tests/test_xcvrd.py replaces swsscommon.Table / SubscriberStateTable / ProducerStateTable / SonicDBConfig by
MagicMocks at import time, so a whole-suite collection must never execute these tests; the session fixture
below refuses to run when it finds those mocks. The directory is skipped when XCVRD_INTEGRATION is not set
or when redis-server is not on PATH (upstream CI has none).
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from unittest.mock import MagicMock

import pytest

NAMESPACES = ['asic0', 'asic1']
DB_IDS = {'APPL_DB': 0, 'CONFIG_DB': 4, 'STATE_DB': 6}
SEPARATORS = {'APPL_DB': ':', 'CONFIG_DB': '|', 'STATE_DB': '|'}
RUN_ID = '{}-{}'.format(time.strftime('%Y%m%dT%H%M%S'), os.getpid())


def results_path():
    """Where the measured numbers go: XCVRD_IT_RESULTS, or a run-scoped file in the temp directory."""
    return os.environ.get('XCVRD_IT_RESULTS') or os.path.join(tempfile.gettempdir(), 'xcvrd-it-results-{}.txt'.format(RUN_ID))


def record_result(test_id, **fields):
    with open(results_path(), 'a') as f:
        f.write('{} run={} {} {}\n'.format(time.strftime('%Y-%m-%dT%H:%M:%S'), RUN_ID, test_id,
                                            ' '.join('{}={}'.format(k, v) for k, v in fields.items())))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if os.path.exists(results_path()):
        terminalreporter.write_line('xcvrd integration results: {}'.format(results_path()))


def integration_enabled():
    """Opt-in flag and both redis executables present (redis-cli is used for CLIENT LIST / INFO / KEYS)."""
    return (os.environ.get('XCVRD_INTEGRATION') == '1' and shutil.which('redis-server') is not None
            and shutil.which('redis-cli') is not None)


# Without the opt-in the test modules of this directory are not collected (collect_ignore_glob applies to the
# conftest's own directory on every pytest version, unlike the pathlib signature of pytest_ignore_collect); an
# explicitly selected test file bypasses it, which is why every test module also calls skip_unless_enabled()
# at import time.
collect_ignore_glob = [] if integration_enabled() else ['test_*.py']


def skip_unless_enabled():
    if not integration_enabled():
        pytest.skip('integration tests need XCVRD_INTEGRATION=1, redis-server and redis-cli', allow_module_level=True)


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class RedisNamespaces:
    """One redis-server per namespace plus a SonicDBConfig global config that maps namespaces to them."""

    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.ports = {ns: _free_port() for ns in NAMESPACES}
        self.procs = {}
        self.global_config = None

    def start(self):
        try:
            self._start()
        except Exception:
            self.stop()
            raise

    def _start(self):
        for ns, port in self.ports.items():
            workdir = os.path.join(self.tmpdir, ns)
            os.makedirs(workdir, exist_ok=True)
            self.procs[ns] = subprocess.Popen(
                ['redis-server', '--port', str(port), '--bind', '127.0.0.1', '--save', '', '--appendonly', 'no',
                 '--notify-keyspace-events', 'KEA', '--loglevel', 'warning', '--dir', workdir],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._write_db_config(ns, port)
        # the default (host) namespace points at asic0's instance: xcvrd only uses the front-end namespaces
        self._write_db_config('', self.ports['asic0'])
        includes = [{'include': 'default/database_config.json'}]
        includes += [{'namespace': ns, 'include': '{}/database_config.json'.format(ns)} for ns in NAMESPACES]
        self.global_config = os.path.join(self.tmpdir, 'database_global.json')
        with open(self.global_config, 'w') as f:
            json.dump({'INCLUDES': includes, 'VERSION': '1.0'}, f)
        for ns, port in self.ports.items():
            self._wait_ready(port, self.procs[ns])

    def _write_db_config(self, ns, port):
        d = os.path.join(self.tmpdir, ns or 'default')
        os.makedirs(d, exist_ok=True)
        cfg = {'INSTANCES': {'redis': {'hostname': '127.0.0.1', 'port': port,
                                       'unix_socket_path': os.path.join(d, 'redis.sock'),
                                       'persistence_for_warm_boot': 'yes'}},
               'DATABASES': {name: {'id': DB_IDS[name], 'separator': SEPARATORS[name], 'instance': 'redis'}
                             for name in DB_IDS},
               'VERSION': '1.0'}
        with open(os.path.join(d, 'database_config.json'), 'w') as f:
            json.dump(cfg, f)

    @staticmethod
    def _wait_ready(port, proc, timeout=10):
        """The endpoint must answer AND belong to our child (a stray listener on a reused port is refused)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('redis-server on port {} exited with {} (port in use?)'.format(port, proc.returncode))
            try:
                info = subprocess.run(['redis-cli', '-p', str(port), 'info', 'server'], capture_output=True, text=True, timeout=2).stdout
                pid = [l.split(':', 1)[1].strip() for l in info.splitlines() if l.startswith('process_id:')]
                if pid and int(pid[0]) == proc.pid:
                    return
                if pid:
                    raise RuntimeError('port {} is served by pid {}, not by our redis-server {}'.format(port, pid[0], proc.pid))
            except (subprocess.TimeoutExpired, ValueError):
                pass
            time.sleep(0.1)
        raise RuntimeError('redis-server on port {} did not come up'.format(port))

    def cli(self, ns, *args):
        return subprocess.run(['redis-cli', '-p', str(self.ports[ns])] + [str(a) for a in args],
                              capture_output=True, text=True, timeout=10).stdout

    def cli_script(self, ns, commands):
        """Several commands on ONE connection (e.g. a MULTI/EXEC transaction), newline separated."""
        return subprocess.run(['redis-cli', '-p', str(self.ports[ns])], input=commands, capture_output=True,
                              text=True, timeout=10).stdout

    def pubsub_clients(self, ns):
        """Number of connected clients that hold a (pattern) subscription on this namespace's redis."""
        return sum(1 for line in self.cli(ns, 'client', 'list').splitlines()
                   if 'flags=' in line and any(f in line.split('flags=')[1].split()[0] for f in 'PS'))

    def stop(self):
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(scope='session')
def redis_ns(tmp_path_factory):
    from swsscommon import swsscommon
    logging.raiseExceptions = False          # SysLogger cannot reach /dev/log in the container: do not spam stderr
    for name in ('Table', 'SubscriberStateTable', 'ProducerStateTable', 'SonicDBConfig', 'Select'):
        assert not isinstance(getattr(swsscommon, name), MagicMock), \
            '{} is a MagicMock: integration tests must run in their own pytest process'.format(name)
    rn = RedisNamespaces(str(tmp_path_factory.mktemp('redis')))
    rn.start()
    try:
        swsscommon.SonicDBConfig.load_sonic_global_db_config(rn.global_config)
        yield rn
    finally:
        rn.stop()


@pytest.fixture
def clean_dbs(redis_ns):
    for ns in NAMESPACES:
        redis_ns.cli(ns, 'flushall')
    yield redis_ns
