"""The readiness checker against the real swsscommon binding and redis: marker semantics, cost and release."""

import os
import resource
import threading
import time
import types
import warnings
from unittest.mock import patch

import pytest

from .conftest import record_result, skip_unless_enabled

skip_unless_enabled()                                  # explicit selection of this file bypasses pytest_ignore_collect

from swsscommon import swsscommon

from xcvrd.xcvrd import DaemonXcvrd, SYSLOG_IDENTIFIER
from xcvrd.xcvrd_utilities.asic_readiness import AsicReadiness

from . import harness as h

pytestmark = pytest.mark.integration


class CountingSelect(swsscommon.Select):
    """The real Select, counting calls and OBJECT results; instances are attributed to the constructing thread."""
    instances = []

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.objects = 0
        self.thread_ident = threading.get_ident()
        CountingSelect.instances.append(self)

    def select(self, timeout_ms):
        self.calls += 1
        result = super().select(timeout_ms)
        if result[0] == swsscommon.Select.OBJECT:
            self.objects += 1
        return result

    @classmethod
    def of_thread(cls, thread):
        found = [s for s in cls.instances if s.thread_ident == thread.ident]
        assert len(found) == 1, 'expected one Select constructed by {}, found {}'.format(thread.name, len(found))
        return found[0]


class RecordingSubscriberStateTable(swsscommon.SubscriberStateTable):
    """The real SubscriberStateTable, recording every non-empty tuple popped by the checker as (namespace, key, op)."""
    records = []

    def __init__(self, db, table_name, *args):
        super().__init__(db, table_name, *args)
        self.ns = db.getNamespace()

    def pop(self, *args):
        result = super().pop(*args)
        if result[0]:
            RecordingSubscriberStateTable.records.append((self.ns, result[0], result[1]))
        return result


def make_daemon(namespaces):
    daemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
    daemon.namespaces = list(namespaces)
    daemon.asic_readiness = AsicReadiness(namespaces)
    daemon.log = h.LogRecorder()
    for level in ('log_notice', 'log_warning', 'log_error', 'log_info', 'log_debug'):
        setattr(daemon, level, getattr(daemon.log, level))
    return daemon


class CheckerRun:
    """The checker in a daemon thread with guaranteed teardown on any exit path: stop_event, bounded join,
    and a failure when the thread survives the join (reported as a warning if a test failure is already in flight)."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.thread = threading.Thread(target=daemon._asic_readiness_checker, name='it-checker', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.daemon.stop_event.set()
        self.thread.join(10)
        if self.thread.is_alive():
            message = 'checker thread did not exit within 10 s of stop_event'
            if exc_type is None:
                raise AssertionError(message)
            warnings.warn(message)                      # the original failure propagates
        return False

    def exited(self, timeout):
        self.thread.join(timeout)
        return not self.thread.is_alive()


def first_queued(daemon, timeout, since=None):
    """(first item, seconds from `since` (default: now) until it arrived) or (None, None)."""
    t0 = time.monotonic() if since is None else since
    try:
        return daemon.ready_asic_queue.get(timeout=timeout), time.monotonic() - t0
    except Exception:
        return None, None


def queued(daemon, timeout):
    """Everything that arrives within `timeout` seconds."""
    items, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            items.append(daemon.ready_asic_queue.get(timeout=0.05))
        except Exception:
            pass
    return items


def wait_until(predicate, timeout, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def join_started(threads, timeout):
    """Join the threads that were started (joining an unstarted thread raises and would mask the test failure)."""
    for t in threads:
        if t.ident is not None:
            t.join(timeout)


def thread_cpu_seconds(native_id):
    with open('/proc/self/task/{}/stat'.format(native_id)) as f:
        fields = f.read().rsplit(')', 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')


@pytest.fixture(autouse=True)
def multi_asic(clean_dbs):
    patches = h.multi_asic_patches()
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


def flush_all(clean_dbs):
    for ns in h.NAMESPACES:
        clean_dbs.cli(ns, 'flushall')


@pytest.mark.parametrize('order', [h.NAMESPACES, list(reversed(h.NAMESPACES))])
def test_checker_marker_semantics_on_binding(clean_dbs, order):
    # (1) markers present before the subscription (snapshot replay): both namespaces queued in the first sweep
    h.populate_appl_db('asic0', ['PortConfigDone'])
    h.populate_appl_db('asic1', ['PortInitDone'])
    daemon = make_daemon(order)
    with CheckerRun(daemon) as run:
        items = queued(daemon, 3)
        assert run.exited(5)
    assert sorted(items) == ['asic0', 'asic1'] and daemon.initial_scan_done.is_set()
    ready = daemon.log.find('ASIC_READY')
    assert len(ready) == 2 and any('PortConfigDone' in l for l in ready) and any('PortInitDone' in l for l in ready)

    # (2) no markers, then a live SET: the first queue item arrives well within the 1 s select timeout
    flush_all(clean_dbs)
    h.populate_appl_db('asic0', []); h.populate_appl_db('asic1', [])
    daemon = make_daemon(order)
    RecordingSubscriberStateTable.records.clear()
    records = RecordingSubscriberStateTable.records
    with patch('xcvrd.xcvrd.swsscommon.SubscriberStateTable', RecordingSubscriberStateTable), CheckerRun(daemon):
        assert daemon.initial_scan_done.wait(3)
        assert queued(daemon, 1.5) == []
        t_write = time.monotonic()
        h.write_marker('asic1', 'PortConfigDone')
        item, live_latency = first_queued(daemon, 3, since=t_write)      # measured from before the write
        assert item == 'asic1' and live_latency < 1.0, (item, live_latency)
        assert ('asic1', 'PortConfigDone', 'SET') in records
        # (5) sustained non-marker traffic on the pending namespace keeps it pending
        for i in range(100):
            swsscommon.Table(h.connector('APPL_DB', 'asic0'), 'PORT_TABLE').set('Ethernet0', swsscommon.FieldValuePairs([('flap_count', str(i))]))
        assert queued(daemon, 1.5) == []
        assert ('asic0', 'Ethernet0', 'SET') in records and not any(r[0] == 'asic0' and r[1] in ('PortConfigDone', 'PortInitDone') for r in records)
        # (4b) a DEL of a marker key while the namespace is pending must not count. On the real binding a
        # DEL event is only produced for a key that existed, so the SET is made unobservable: HSET and DEL in
        # one MULTI/EXEC transaction. The subscriber's read-back of the HSET event misses the key (skipped as
        # outdated) and only the DEL tuple is popped, which the recording table shows.
        reply = clean_dbs.cli_script('asic0', 'MULTI\nHSET PORT_TABLE:PortInitDone count 4\nDEL PORT_TABLE:PortInitDone\nEXEC\n')
        assert reply.count('QUEUED') == 2 and 'ERR' not in reply, reply
        assert clean_dbs.cli('asic0', 'exists', 'PORT_TABLE:PortInitDone').strip() == '0'
        assert wait_until(lambda: ('asic0', 'PortInitDone', 'DEL') in records, 2), records[-5:]   # the DEL reached the checker ...
        assert ('asic0', 'PortInitDone', 'SET') not in records                                      # ... without a SET ...
        assert queued(daemon, 1.0) == []                                                            # ... and did not make asic0 ready
        h.write_marker('asic0', 'PortInitDone')                                                     # a real SET does
        item, _ = first_queued(daemon, 3)
        assert item == 'asic0', item
        assert ('asic0', 'PortInitDone', 'SET') in records

    # (3) duplicate markers and (4) snapshot SET then live DEL: one queue entry, readiness kept
    flush_all(clean_dbs)
    h.populate_appl_db('asic0', ['PortConfigDone', 'PortInitDone'])
    h.populate_appl_db('asic1', [])
    daemon = make_daemon(order)
    with CheckerRun(daemon):
        items = queued(daemon, 2)
        assert items == ['asic0']
        h.del_marker('asic0', 'PortConfigDone'); h.del_marker('asic0', 'PortInitDone')
        h.write_marker('asic0', 'PortConfigDone')
        assert queued(daemon, 1.5) == []                   # unsubscribed: nothing more from asic0
        assert daemon.asic_readiness.ready_namespaces() == frozenset()               # the checker never touches the registry

    # (6) heartbeat: exactly two ASIC_STILL_WAITING over exactly 700 simulated seconds while asic1 is pending,
    # under continuous non-marker traffic (the checker's clock advances 100 s per reading, then stops)
    flush_all(clean_dbs)
    h.populate_appl_db('asic0', ['PortConfigDone']); h.populate_appl_db('asic1', [])
    daemon = make_daemon(order)
    clock = {'now': 1000.0, 'readings': 0}

    def fake_monotonic():
        clock['readings'] += 1
        clock['now'] = min(clock['now'] + 100.0, 1800.0)   # t0 = 1100, heartbeats at 1400 and 1700, then the clock stands still
        return clock['now']
    stop_traffic = threading.Event()

    def traffic():
        tbl = swsscommon.Table(h.connector('APPL_DB', 'asic1'), 'PORT_TABLE')
        n = 0
        while not stop_traffic.is_set():
            tbl.set('Ethernet16', swsscommon.FieldValuePairs([('flap_count', str(n))])); n += 1
            time.sleep(0.02)
    shim = types.SimpleNamespace(monotonic=fake_monotonic, time=time.time, sleep=time.sleep)
    writer = threading.Thread(target=traffic, daemon=True)
    try:
        with patch('xcvrd.xcvrd.time', shim), CheckerRun(daemon):   # only the checker's clock is shimmed
            writer.start()
            assert wait_until(lambda: clock['now'] >= 1800.0, 15)
            time.sleep(1.0)                                # more iterations with a standing clock: no further heartbeat allowed
    finally:
        stop_traffic.set()
        join_started([writer], 5)
    beats = daemon.log.find('ASIC_STILL_WAITING')
    assert len(beats) == 2 and all("['asic1']" in b for b in beats), beats
    assert 'after 300s' in beats[0] and 'after 600s' in beats[1], beats
    record_result('checker_markers', order=''.join(n[-1] for n in order), live_set_latency='{:.3f}s'.format(live_latency),
                  heartbeats=len(beats), simulated_seconds=700)


@pytest.mark.parametrize('order', [h.NAMESPACES, list(reversed(h.NAMESPACES))])
def test_checker_cost_and_release(clean_dbs, order):
    duration = float(os.environ.get('XCVRD_IT_DURATION', '20'))
    h.populate_appl_db('asic0', [])
    tbl0 = swsscommon.Table(h.connector('APPL_DB', 'asic0'), 'PORT_TABLE')
    for i in range(32):
        tbl0.set('Ethernet{}'.format(4 * i), swsscommon.FieldValuePairs([('admin_status', 'up')]))
    h.write_marker('asic0', 'PortConfigDone'); h.write_marker('asic0', 'PortInitDone')
    h.populate_appl_db('asic1', [])
    tbl1 = swsscommon.Table(h.connector('APPL_DB', 'asic1'), 'PORT_TABLE')

    healthy_stats = {'idle': [], 'traffic': [], 'burst': []}
    healthy_ready = threading.Event()                      # set after the healthy loop's first idle iteration
    phase = {'name': 'idle'}
    stop = threading.Event()
    counts = {'asic1_events': 0}

    def healthy_loop():                                    # a worker-like select loop on the healthy namespace
        sel = swsscommon.Select()
        sub = swsscommon.SubscriberStateTable(h.connector('CONFIG_DB', 'asic0'), 'PORT')
        sel.addSelectable(sub)
        while not stop.is_set():
            t0 = time.monotonic()
            sel.select(100)
            while sub.pop()[0]:
                pass
            healthy_stats[phase['name']].append(time.monotonic() - t0)
            healthy_ready.set()

    def writer0():
        n = 0
        while not stop.is_set():
            tbl0.set('Ethernet0', swsscommon.FieldValuePairs([('flap_count', str(n))])); n += 1
            time.sleep(0.05)

    def writer1():
        n = 0
        burst_done = False
        t_start = time.monotonic()
        while not stop.is_set():
            tbl1.set('Ethernet16', swsscommon.FieldValuePairs([('flap_count', str(n))])); n += 1
            counts['asic1_events'] += 1
            if not burst_done and time.monotonic() - t_start > duration * 0.4:
                phase['name'] = 'burst'
                for k in range(1000):
                    tbl1.set('Ethernet20', swsscommon.FieldValuePairs([('flap_count', str(k))]))
                counts['asic1_events'] += 1000
                burst_done = True
                phase['name'] = 'traffic'
            time.sleep(0.1)

    threads = [threading.Thread(target=healthy_loop, daemon=True), threading.Thread(target=writer0, daemon=True),
               threading.Thread(target=writer1, daemon=True)]
    daemon = make_daemon(order)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        threads[0].start()
        assert healthy_ready.wait(5)                       # its subscription exists before the baseline is sampled
        time.sleep(0.5)
        baseline_pubsub = {ns: clean_dbs.pubsub_clients(ns) for ns in h.NAMESPACES}   # the healthy loop's own subscription
        assert baseline_pubsub == {'asic0': 1, 'asic1': 0}, baseline_pubsub
        CountingSelect.instances.clear()
        with patch('xcvrd.xcvrd.swsscommon.Select', CountingSelect), CheckerRun(daemon) as run:
            assert daemon.log.wait_for('[asic0] ASIC_READY', 5)
            assert wait_until(lambda: clean_dbs.pubsub_clients('asic0') == baseline_pubsub['asic0'], 2)   # asic0's subscription released
            assert clean_dbs.pubsub_clients('asic1') == baseline_pubsub['asic1'] + 1                     # asic1's still open
            phase['name'] = 'traffic'
            threads[1].start(); threads[2].start()
            t0 = time.monotonic()
            native_id = run.thread.native_id
            cpu0 = thread_cpu_seconds(native_id)
            time.sleep(duration)
            cpu = thread_cpu_seconds(native_id) - cpu0
            sel = CountingSelect.of_thread(run.thread)     # the checker's Select, not any other
            calls_after_ready = sel.calls
            elapsed = time.monotonic() - t0
            # the late marker
            t_marker = time.monotonic()
            h.write_marker('asic1', 'PortConfigDone')
            assert daemon.log.wait_for('[asic1] ASIC_READY', 3)
            detect_latency = time.monotonic() - t_marker
            assert run.exited(2)                           # every namespace ready: the thread ends by itself
    finally:
        stop.set()
        join_started(threads, 5)
    items = []
    while not daemon.ready_asic_queue.empty():
        items.append(daemon.ready_asic_queue.get_nowait())
    assert items == ['asic0', 'asic1']
    assert clean_dbs.pubsub_clients('asic1') == baseline_pubsub['asic1']
    rss_growth_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0
    # no spin: at most one select() per asic1 event plus one per idle second (with margin)
    assert calls_after_ready <= counts['asic1_events'] + elapsed + 10, (calls_after_ready, counts['asic1_events'], elapsed)
    assert cpu / duration < 0.05, 'checker CPU {:.1%}'.format(cpu / duration)
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    idle, traffic_med, burst_med = med(healthy_stats['idle']), med(healthy_stats['traffic']), med(healthy_stats['burst'])
    assert idle is not None and traffic_med is not None and traffic_med <= idle * 1.2 + 0.005, (idle, traffic_med)
    record_result('checker_cost', order=''.join(n[-1] for n in order), duration='{:.0f}s'.format(duration), select_calls=calls_after_ready,
                  asic1_events=counts['asic1_events'], checker_cpu='{:.1%}'.format(cpu / duration), detect_latency='{:.3f}s'.format(detect_latency),
                  healthy_iter_ms_idle='{:.1f}'.format(idle * 1000), healthy_iter_ms_traffic='{:.1f}'.format(traffic_med * 1000),
                  healthy_iter_ms_burst='{:.1f}'.format(burst_med * 1000) if burst_med else 'n/a', rss_growth_kb=rss_growth_kb)
    assert detect_latency < 1.0
    assert rss_growth_kb < 2 * 1024                        # ru_maxrss is the process-wide peak: coarse; 2 MB is the limit this test enforces
