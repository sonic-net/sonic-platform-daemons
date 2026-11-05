"""The real daemon against real per-namespace databases, with the platform mocked."""

import os
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from .conftest import record_result, skip_unless_enabled

skip_unless_enabled()                                  # explicit selection of this file bypasses pytest_ignore_collect

from sonic_platform_base.sfp_base import SfpBase
from swsscommon import swsscommon

from xcvrd import xcvrd as xcvrd_module
from xcvrd.xcvrd import DaemonXcvrd, SYSLOG_IDENTIFIER
from xcvrd.xcvrd_utilities import media_settings_parser, port_event_helper

from . import harness as h

pytestmark = pytest.mark.integration

SI_SETTINGS = {'preemphasis': {'lane{}'.format(i): '0x{:x}'.format(i + 1) for i in range(8)}}
MEDIA_KEY = {'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP-DD-sm_media_interface',
             'lane_speed_key': 'speed:400GAUI-8', 'medium_lane_speed_key': 'UNKNOWN'}


def populate(markers_asic1=()):
    for ns in h.NAMESPACES:
        h.populate_config_db(ns)
    h.populate_appl_db('asic0', ['PortConfigDone', 'PortInitDone'])
    h.populate_appl_db('asic1', list(markers_asic1))


def si_rows(cli, ns):
    """{lport: {field: value}} of the producer-side APPL_DB rows (_PORT_TABLE:*) that carry NPU SI fields.

    The ProducerStateTable writes _PORT_TABLE:<key> and the key set that orchagent's ConsumerStateTable consumes;
    reading the producer side is the earliest point at which a publication is observable.
    """
    rows = {}
    for key in cli(ns, 'keys', '_PORT_TABLE:*').split():
        fields = cli(ns, 'hgetall', key).split('\n')
        d = dict(zip(fields[0::2], fields[1::2]))
        if 'preemphasis' in d:
            rows[key.split(':', 1)[1]] = d
    return rows


def status(ns, lport):
    return h.state_field(ns, 'PORT_TABLE', lport, 'NPU_SI_SETTINGS_SYNC_STATUS')


def cmis_state(ns, lport):
    return h.state_field(ns, 'TRANSCEIVER_STATUS_SW', lport, 'cmis_state')


def wait_until(predicate, timeout, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class DaemonRun:
    """DaemonXcvrd.run() in a thread with the platform, settings and loggers under test control."""

    SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)

    def __init__(self, chassis, **daemon_kwargs):
        self.chassis = chassis
        # DaemonBase.__init__ registers the instance's signal_handler for every signal without a handler, which would
        # keep the first daemon of the process, its workers and their redis connections (one per swsscommon.Table,
        # several hundred per run) alive for the rest of the session; stop() restores the handlers
        self.saved_signals = {sig: signal.getsignal(sig) for sig in self.SIGNALS}
        self.daemon = DaemonXcvrd(SYSLOG_IDENTIFIER, **daemon_kwargs)
        self.log = h.LogRecorder()
        self.mark_holds = []                             # (held, release) of hold_mark_admitted(); released by stop()
        for level in ('log_notice', 'log_warning', 'log_error', 'log_info', 'log_debug'):
            setattr(self.daemon, level, getattr(self.log, level))
        self.worker_log = h.LogRecorder()
        self.outcome = {}
        self.sfp_subscribed = threading.Event()          # the publishing worker's CONFIG_DB subscription exists (its start-up pass is over)
        # Test hooks, run on the publishing worker's thread: before its init() reads the port mapping (its first read;
        # the admission pass reads too) and before its CONFIG_DB subscription
        self.before_sfp_init = None
        self.before_sfp_subscribe = None
        real_subscribe = port_event_helper.subscribe_port_config_change
        real_get_port_mapping = port_event_helper.get_port_mapping

        def in_sfp_worker():                             # the DOM worker subscribes and reads too
            return isinstance(threading.current_thread(), xcvrd_module.SfpStateUpdateTask)

        def subscribe(namespaces):
            if in_sfp_worker() and self.before_sfp_subscribe is not None:
                self.before_sfp_subscribe()
            result = real_subscribe(namespaces)
            if in_sfp_worker():
                self.sfp_subscribed.set()
            return result

        def get_port_mapping(namespaces):
            if in_sfp_worker() and self.before_sfp_init is not None:
                hook, self.before_sfp_init = self.before_sfp_init, None
                hook()
            return real_get_port_mapping(namespaces)
        self.thread = threading.Thread(target=self._run, name='it-daemon', daemon=True)
        self.patches = h.multi_asic_patches() + [
            patch.object(port_event_helper, 'subscribe_port_config_change', subscribe),
            patch.object(port_event_helper, 'get_port_mapping', get_port_mapping),
            patch('xcvrd.xcvrd.helper_logger', self.worker_log),
            patch.object(media_settings_parser, 'helper_logger', self.worker_log),      # "Published SI setting" notices
            patch.object(media_settings_parser, 'load_media_settings', lambda: media_settings_parser.g_dict.update({'GLOBAL_MEDIA_SETTINGS': {'1-64': {}}})),
            patch.object(media_settings_parser, 'get_media_settings_key', lambda *a, **k: dict(MEDIA_KEY)),
            patch.object(media_settings_parser, 'get_media_settings_value', lambda *a, **k: dict(SI_SETTINGS)),
            patch.object(media_settings_parser, 'get_custom_media_settings_value', lambda *a, **k: {}),
            patch('xcvrd.xcvrd_utilities.optics_si_parser.load_optics_si_settings', lambda: None),
        ]

    def _run(self):
        try:
            self.daemon.run()
            self.outcome['exit'] = 0
        except SystemExit as e:
            self.outcome['exit'] = e.code
        except BaseException as e:            # pragma: no cover - reported by the test
            self.outcome['exception'] = repr(e)

    def start(self):
        h.install_fake_platform(self.chassis)
        media_settings_parser.g_dict.clear()
        for p in self.patches:
            p.start()
        self.thread.start()
        assert self.log.wait_for('Start daemon main loop', 60), self.log.lines[-20:]

    def hold_mark_admitted(self, ns):
        """Hold the publishing worker at the end of its admission pass over `ns`, before it marks the namespace
        admitted: `held` is set when the worker gets there, the worker goes on once `release` is set (bounded, so a
        failing test cannot hang the worker; stop() releases too). Call after start(): init() builds the registry."""
        held, release = threading.Event(), threading.Event()
        registry = self.daemon.asic_readiness
        real_mark = registry.mark_admitted

        def mark(name, namespace):
            if namespace == ns:
                held.set()
                release.wait(30)
            real_mark(name, namespace)
        registry.mark_admitted = mark
        self.mark_holds.append((held, release))
        return held, release

    def stop(self):
        """Stop the daemon and undo the patches and the signal handlers; returns (exit status, still alive, exception repr)."""
        for _, release in self.mark_holds:
            release.set()
        self.daemon.stop_event.set()
        self.thread.join(60)
        for p in self.patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        self.patches.clear()                      # the subscribe closure refers to this object: no cycle to keep the daemon alive
        for sig, handler in self.saved_signals.items():
            if handler is not None:               # None: not installed from Python, nothing to restore
                signal.signal(sig, handler)
        return self.outcome.get('exit'), self.thread.is_alive(), self.outcome.get('exception')


def published_ports(run):
    return sorted(l.split('lport ')[1].split(' ')[0] for l in run.worker_log.find('Published SI setting'))


def deferred_passes(run):
    """The publishing worker's PORTS_DEFERRED notices: one per namespace whose start-up pass init() deferred."""
    return run.worker_log.find('PORTS_DEFERRED (SfpStateUpdateTask)')


def pass_notices(run, ns):
    """The notices of the start-up pass that ns received at its admission (none while it is pending)."""
    return run.worker_log.find('[{}] SfpStateUpdateTask: Posted all port DOM/SFP info to DB'.format(ns))


def sfp_worker(run):
    return next(t for t in run.daemon.threads if isinstance(t, xcvrd_module.SfpStateUpdateTask))


def admitted(run):
    return run.daemon.asic_readiness.admitted_namespaces()


def cmis_presence_reads(chassis, pports):
    """{pport: [monotonic times]} of the presence reads the CMIS worker made on these modules."""
    return {pp: [t for t, thread in chassis.sfps[pp].presence_readers if thread == 'CmisManagerTask'] for pp in pports}


def info_rows(ns):
    tbl = swsscommon.Table(h.connector('STATE_DB', ns), 'TRANSCEIVER_INFO')
    return {k: list(tbl.get(k)[1]) for k in tbl.getKeys()}


def seed_info_rows(ns, rows):
    tbl = swsscommon.Table(h.connector('STATE_DB', ns), 'TRANSCEIVER_INFO')
    for k, fvs in rows.items():
        tbl.set(k, swsscommon.FieldValuePairs(fvs))


def state_rows(cli, ns, pattern):
    """{key: {field: value}} of the STATE_DB hashes matching `pattern`."""
    rows = {}
    for key in cli(ns, '-n', '6', 'keys', pattern).split():
        fields = cli(ns, '-n', '6', 'hgetall', key).split('\n')
        rows[key] = dict(zip(fields[0::2], fields[1::2]))
    return rows


def clean_stop(run):
    exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert run.log.find('Stop daemon main loop') and not run.log.find('Traceback') and not run.worker_log.find('Traceback')


def test_daemon_late_namespace_end_to_end(clean_dbs):
    populate()
    chassis = h.FakeChassis(event_period=1.0)
    run = DaemonRun(chassis)
    try:
        _late_namespace_checks(clean_dbs, chassis, run)
    finally:
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert run.log.find('Stop daemon main loop') and not run.log.find('Traceback')


def _late_namespace_checks(clean_dbs, chassis, run):
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    asic1_ports = [lp for lp, _, _ in asic1]
    (deleted, deleted_pport, _), pre_notified = asic1[3], asic1_ports[2]
    # one asic1 port carries a non-default, non-notified status before the daemon starts: never published
    swsscommon.Table(h.connector('STATE_DB', 'asic1'), 'PORT_TABLE').set(pre_notified, swsscommon.FieldValuePairs([('NPU_SI_SETTINGS_SYNC_STATUS', 'SOME_OTHER_VALUE')]))
    run.start()
    # The start-up pass covers asic0 (ready) and is deferred for asic1 (pending) as a whole: no producer row, no
    # TRANSCEIVER_INFO row and no status row for its ports. The one-shot assertions run after the publishing worker
    # finished its start-up pass and subscribed to CONFIG_DB.
    assert run.sfp_subscribed.wait(60)
    assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (4 ports)']
    assert not run.worker_log.find('MEDIA_SETTINGS_DEFERRED')                            # no event path visited asic1 either
    assert set(si_rows(clean_dbs.cli, 'asic0')) == set(asic0_ports)
    assert si_rows(clean_dbs.cli, 'asic1') == {}
    for lp in asic0_ports:
        assert status('asic0', lp) == 'NPU_SI_SETTINGS_NOTIFIED'
    for lp in asic1_ports:
        assert status('asic1', lp) == ('SOME_OTHER_VALUE' if lp == pre_notified else 'NPU_SI_SETTINGS_DEFAULT')
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') is None
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') is None     # the CMIS worker's cmis_state shares the row
    # CMIS gated. Progress anchors: the CMIS worker has taken every asic1 port into its dictionary (its observer
    # writes cmis_state=INSERTED, metadata allowed before readiness) and has completed a sweep that brought every
    # asic0 port to READY (flat-memory path). From then on, no asic1 port may reach READY for several sweeps.
    # (The device accessors get_xcvr_api()/is_flat_memory() are also used by the publishing worker's threshold
    # posting, a permitted read, so they are not an oracle.)
    assert wait_until(lambda: all(cmis_state('asic1', lp) == 'INSERTED' for lp in asic1_ports), 10), [cmis_state('asic1', lp) for lp in asic1_ports]
    assert wait_until(lambda: all(cmis_state('asic0', lp) == 'READY' for lp in asic0_ports), 10)     # ready namespace processed
    assert not wait_until(lambda: any(cmis_state('asic1', lp) == 'READY' for lp in asic1_ports), 3)  # held for ~3 sweeps
    # a CONFIG_DB port of the pending namespace is deleted while the namespace is pending: the worker drops it from
    # its port mapping (the removal logs nothing; observed on the mapping) and the pass at admission never visits it
    swsscommon.Table(h.connector('CONFIG_DB', 'asic1'), 'PORT')._del(deleted)
    worker = sfp_worker(run)
    assert wait_until(lambda: not worker.port_mapping.is_logical_port(deleted), 10)
    asic0_before = si_rows(clean_dbs.cli, 'asic0')
    # the late namespace publishes its marker: its start-up pass runs over its remaining ports; the worker is held at
    # the end of the pass, before it marks the namespace admitted
    mark_held, mark_release = run.hold_mark_admitted('asic1')
    h.write_marker('asic1', 'PortConfigDone')
    assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5) and run.log.wait_for('ASIC_ALL_READY', 5)
    assert mark_held.wait(15)
    remaining = [lp for lp in asic1_ports if lp != deleted]
    expected = [lp for lp in remaining if lp != pre_notified]
    # the pass's rows and publications are in place; the namespace is not admitted, its notice not logged
    assert 'asic1' not in admitted(run) and not pass_notices(run, 'asic1')
    for lp, pport, _ in asic1:
        if lp != deleted:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
    assert set(si_rows(clean_dbs.cli, 'asic1')) == set(expected)
    for lp in expected:
        assert status('asic1', lp) == 'NPU_SI_SETTINGS_NOTIFIED'
        assert si_rows(clean_dbs.cli, 'asic1')[lp]['preemphasis'] == ','.join(SI_SETTINGS['preemphasis'].values())
    # the pre-notified port: posted by the pass, skipped by the publisher's own check
    assert status('asic1', pre_notified) == 'SOME_OTHER_VALUE'
    assert len(run.worker_log.find('Media settings already notified for lport {}'.format(pre_notified))) == 1
    # the deleted port: never visited (get_transceiver_info() is only called by the publishing worker)
    assert 'get_transceiver_info' not in chassis.sfps[deleted_pport].calls
    assert h.state_field('asic1', 'TRANSCEIVER_INFO', deleted, 'serial') is None
    assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', deleted, 'status') is None
    assert si_rows(clean_dbs.cli, 'asic0') == asic0_before                                 # ready namespace untouched
    # CMIS still held while the mark is: the marker and the pass's rows open nothing
    assert not wait_until(lambda: any(cmis_state('asic1', lp) == 'READY' for lp in remaining), 2)
    assert all(not reads for reads in cmis_presence_reads(chassis, [pp for lp, pp, _ in asic1]).values())
    mark_release.set()
    notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
    assert len(notices) == 1 and notices[0].endswith('(3 ports, 0 to retry)'), notices
    assert 'asic1' in admitted(run)
    assert wait_until(lambda: all(cmis_state('asic1', lp) == 'READY' for lp in remaining), 10)   # CMIS resumed
    assert published_ports(run) == sorted(asic0_ports + expected)                           # each published port exactly once


@pytest.mark.parametrize('deleted_at', ['worker_init', 'subscription'])
def test_daemon_port_deleted_before_subscription(clean_dbs, deleted_at):
    """A CONFIG_DB port of the late namespace is deleted after the daemon took its port mapping and before the
    publishing worker subscribed to CONFIG_DB changes (before the worker's own init() read, or after that read and just
    before the subscription). No removal event describes it: the worker's mapping keeps it until the namespace's
    admission, whose pass handles it as the missed removal (not read, no rows, not published, out of the mapping); a
    module event for its index then finds no port, and the name re-created is handled as an addition."""
    populate()
    chassis = h.FakeChassis(event_period=0.5)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    deleted, deleted_pport, _ = asic1[1]
    remaining = [lp for lp, _, _ in asic1 if lp != deleted]

    def delete_port():                                                           # on the worker's thread
        swsscommon.Table(h.connector('CONFIG_DB', 'asic1'), 'PORT')._del(deleted)
    run = DaemonRun(chassis)
    if deleted_at == 'worker_init':
        run.before_sfp_init = delete_port
    else:
        run.before_sfp_subscribe = delete_port
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        worker = sfp_worker(run)
        assert not swsscommon.Table(h.connector('CONFIG_DB', 'asic1'), 'PORT').get(deleted)[0]
        assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred ({} ports)'.format(
            3 if deleted_at == 'worker_init' else 4)]
        # the subscription replays the ports that exist and delivers no removal for one deleted before it: after the
        # worker has been through its loop, its mapping still lists the port
        time.sleep(3)
        assert worker.port_mapping.is_logical_port(deleted)
        h.write_marker('asic1', 'PortConfigDone')
        assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5)
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(3 ports, 0 to retry)'), notices
        # the deleted port: handled as its removal event would have been, before the pass
        assert not worker.port_mapping.is_logical_port(deleted) and worker.port_mapping.get_physical_to_logical(deleted_pport) is None
        assert 'get_transceiver_info' not in chassis.sfps[deleted_pport].calls
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', deleted, 'serial') is None
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', deleted, 'status') is None
        assert not run.worker_log.find('No info found for port {} in cfg_port_tbl'.format(deleted))   # the publisher never saw it
        for lp, pport, _ in asic1:
            if lp != deleted:
                assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
                assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
        assert set(si_rows(clean_dbs.cli, 'asic1')) == set(remaining)
        # a module event for its index finds no logical port
        chassis.events.append({deleted_pport: '1'})
        assert run.worker_log.wait_for('Got unknown FP port index {}, ignored'.format(deleted_pport), 10)
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', deleted, 'serial') is None
        # the name re-created with the same index: an addition, posted and published
        swsscommon.Table(h.connector('CONFIG_DB', 'asic1'), 'PORT').set(deleted, h.config_port_fvs(deleted, deleted_pport))
        assert wait_until(lambda: h.state_field('asic1', 'TRANSCEIVER_INFO', deleted, 'serial') == 'S{}'.format(deleted_pport), 15)
        assert wait_until(lambda: deleted in si_rows(clean_dbs.cli, 'asic1'), 10)
        assert status('asic1', deleted) == 'NPU_SI_SETTINGS_NOTIFIED'
        assert published_ports(run) == sorted(asic0_ports + remaining + [deleted])      # each published port exactly once
    finally:
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert run.log.find('Stop daemon main loop') and not run.log.find('Traceback') and not run.worker_log.find('Traceback')


def test_daemon_marker_during_startup_pass(clean_dbs):
    populate()
    chassis = h.FakeChassis(event_period=1.0)
    run = DaemonRun(chassis)
    held, release = threading.Event(), threading.Event()
    first_wait_held, first_wait_release = chassis.hold_next_change_event()      # the worker's first event wait
    real_post = xcvrd_module.SfpStateUpdateTask.post_port_info_to_db
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1_ports = [lp for lp, _, _ in h.ports_of('asic1')]
    seen = []

    def blocking_post(task, lport, *args, **kwargs):
        # The start-up pass covers the ready namespace only; it is held inside asic0's SECOND port so that asic1's
        # readiness arrives after the worker's constructor took its readiness snapshot and while the pass is still under way.
        if lport in asic0_ports and not held.is_set():
            seen.append(lport)
            if len(seen) == 2:
                held.set()
                release.wait(30)
        return real_post(task, lport, *args, **kwargs)
    run.patches.append(patch('xcvrd.xcvrd.SfpStateUpdateTask.post_port_info_to_db', blocking_post))
    try:
        run.start()
        assert held.wait(30)                                                     # the pass is inside asic0's second port
        assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (4 ports)']
        h.write_marker('asic1', 'PortConfigDone')
        assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5)                       # registered while the pass is held ...
        assert wait_until(lambda: run.daemon.asic_readiness.is_ready('asic1'), 5)   # ... (logged before the registry changes)
        assert not pass_notices(run, 'asic1') and si_rows(clean_dbs.cli, 'asic1') == {}   # the held pass does not reach asic1
        release.set()
        # The worker's first loop iteration polls the platform before any admission: when the first event wait
        # begins, asic1 is still unpublished; its pass runs once that wait returns, at the start of the next iteration.
        assert first_wait_held.wait(30)
        assert not pass_notices(run, 'asic1') and si_rows(clean_dbs.cli, 'asic1') == {}
        first_wait_release.set()
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 0 to retry)'), notices
        assert set(si_rows(clean_dbs.cli, 'asic1')) == set(asic1_ports)
        assert not run.worker_log.find('MEDIA_SETTINGS_DEFERRED')                # no asic1 port was visited while pending
        assert published_ports(run) == sorted(asic0_ports + asic1_ports)         # every port exactly once
    finally:
        release.set()
        first_wait_release.set()
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)


def test_daemon_late_asic_recovery(clean_dbs):
    """The late namespace's modules are absent when the daemon starts and appear before the platform reports its
    first change event: no insertion event ever describes them, the pass at the namespace's admission finds them."""
    populate()
    chassis = h.FakeChassis(event_period=1.0)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    asic1_ports = [lp for lp, _, _ in asic1]
    asic1_pports = [pport for _, pport, _ in asic1]
    chassis.set_presence(asic1_pports, False)
    held, release = chassis.hold_next_change_event()                            # the SFP worker's first get_change_event()
    run = DaemonRun(chassis)
    try:
        run.start()
        assert held.wait(60)                                                     # init() is over, the worker is inside the call
        assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (4 ports)']
        for lp in asic1_ports:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') is None
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') is None
        chassis.set_presence(asic1_pports, True)                                 # the modules appear; no event is queued ...
        release.set()                                                            # ... the platform's baseline sees them present, no transition
        h.write_marker('asic1', 'PortConfigDone')
        assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5)
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 0 to retry)'), notices
        for lp, pport, _ in asic1:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
            assert status('asic1', lp) == 'NPU_SI_SETTINGS_NOTIFIED'
        assert set(si_rows(clean_dbs.cli, 'asic1')) == set(asic1_ports)
        assert not run.worker_log.find('Got SFP inserted event')                 # nothing reported the modules' appearance
        assert published_ports(run) == sorted(asic0_ports + asic1_ports)         # each port exactly once
    finally:
        release.set()
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert run.log.find('Stop daemon main loop') and not run.log.find('Traceback') and not run.worker_log.find('Traceback')


def test_daemon_blocked_retry_entry_survives_admission(clean_dbs):
    """A late namespace's module whose insertion read failed while the namespace was pending and that then reported
    a blocking error: the admission pass does not read it and keeps its retry entry, and once the error is no longer
    blocking the retry posts the port and publishes it."""
    populate()
    chassis = h.FakeChassis(event_period=1.0)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    lport, pport, _ = asic1[0]
    chassis.sfps[pport].readable = False
    blocking = str(SfpBase.SFP_STATUS_BIT_INSERTED | SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_I2C_STUCK)
    power = str(SfpBase.SFP_STATUS_BIT_INSERTED | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED)
    run = DaemonRun(chassis)
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        worker = sfp_worker(run)
        worker.last_retry_eeprom_time = time.time()                              # no EEPROM retry during the next 60 s
        assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (4 ports)']
        # the module's insertion matures while the namespace is pending; the read fails (the second attempt reuses the
        # cached result): the port enters the retry set
        chassis.events.append({pport: '1'})
        assert wait_until(lambda: worker.retry_eeprom_set == {lport}, 20), worker.retry_eeprom_set
        assert run.worker_log.find('{}: SFP EEPROM is not ready. One more try...'.format(lport))
        reads = chassis.sfps[pport].calls.count('get_transceiver_info')
        assert reads == 1 and h.state_field('asic1', 'TRANSCEIVER_INFO', lport, 'serial') is None
        # then it reports a blocking error
        chassis.events.append({pport: blocking})
        assert wait_until(lambda: h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lport, 'status') == blocking, 10)
        # the namespace is admitted: the pass reads the other ports only and keeps the blocked port's entry
        h.write_marker('asic1', 'PortConfigDone')
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 1 to retry)'), notices
        assert worker.retry_eeprom_set == {lport}
        assert chassis.sfps[pport].calls.count('get_transceiver_info') == reads
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', lport, 'serial') is None
        for lp, pp, _ in asic1[1:]:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pp)
        assert set(si_rows(clean_dbs.cli, 'asic1')) == {lp for lp, _, _ in asic1[1:]}
        # the error turns into a non-blocking one and the EEPROM becomes readable: the event rewrites the status row
        # only, the next retry posts the port and publishes it
        chassis.sfps[pport].readable = True
        chassis.events.append({pport: power})
        assert wait_until(lambda: h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lport, 'status') == power, 10)
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', lport, 'serial') is None
        assert chassis.sfps[pport].calls.count('get_transceiver_info') == reads
        worker.last_retry_eeprom_time = 0                                        # the retry interval elapses
        assert wait_until(lambda: h.state_field('asic1', 'TRANSCEIVER_INFO', lport, 'serial') == 'S{}'.format(pport), 15)
        assert wait_until(lambda: lport in si_rows(clean_dbs.cli, 'asic1') and not worker.retry_eeprom_set, 10)
        assert status('asic1', lport) == 'NPU_SI_SETTINGS_NOTIFIED'
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lport, 'status') == power
        assert published_ports(run) == sorted(asic0_ports + [lp for lp, _, _ in asic1])     # each port exactly once
    finally:
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert not run.log.find('Traceback') and not run.worker_log.find('Traceback')


def test_daemon_blocked_port_left_to_its_events_at_admission(clean_dbs):
    """A module of the late namespace is inserted, then reports a blocking error while the namespace is pending, and
    from then on its presence reads absent (NVIDIA's firmware-control presence is an EEPROM read). The pass at the
    namespace's admission leaves the port to its events: rows, error row and cache entry stand, nothing is read behind
    the error (the port is left to the retry, kept out of this window); the insertion event that clears the error
    posts and publishes the port."""
    populate()
    chassis = h.FakeChassis(event_period=0.2)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    (blocked, blocked_pport, _), (gone, gone_pport, _) = asic1[1], asic1[2]
    posted = [lp for lp, _, _ in asic1 if lp not in (blocked, gone)]
    i2c_stuck = str(SfpBase.SFP_STATUS_BIT_INSERTED | SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_I2C_STUCK)
    presence_readers = []                                                            # threads that read the blocked module's presence

    def blocked_presence():
        presence_readers.append(threading.current_thread().name)
        return chassis.sfps[blocked_pport].present
    chassis.sfps[blocked_pport].get_presence = blocked_presence
    run = DaemonRun(chassis)
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        chassis.events.append({blocked_pport: '1'})                                  # inserted while pending: the event path posts its rows
        assert wait_until(lambda: h.state_field('asic1', 'TRANSCEIVER_INFO', blocked, 'serial') == 'S{}'.format(blocked_pport), 15)
        assert run.worker_log.find('MEDIA_SETTINGS_DEFERRED') == ['[asic1] MEDIA_SETTINGS_DEFERRED (SfpStateUpdateTask): namespace pending, first pending port {}'.format(blocked)]
        chassis.events.append({blocked_pport: i2c_stuck})                            # then its EEPROM access is blocked
        assert wait_until(lambda: h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', blocked, 'status') == i2c_stuck, 15)
        worker = sfp_worker(run)
        assert worker.sfp_error_dict.get(blocked_pport, (None,))[0] == i2c_stuck
        worker.last_retry_eeprom_time = time.time()                                  # the pass hands the blocked port to the retry; none during this window
        chassis.set_presence([blocked_pport, gone_pport], False)                     # behind the error presence reads absent; another module is gone, no event
        del presence_readers[:]
        info_reads = chassis.sfps[blocked_pport].calls.count('get_transceiver_info')
        h.write_marker('asic1', 'PortConfigDone')
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 1 to retry)'), notices
        assert worker.retry_eeprom_set == {blocked}
        # the blocked port: not read (presence included), rows and error row intact, cache entry kept, not published
        assert 'SfpStateUpdateTask' not in presence_readers, presence_readers
        assert chassis.sfps[blocked_pport].calls.count('get_transceiver_info') == info_reads
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', blocked, 'serial') == 'S{}'.format(blocked_pport)
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', blocked, 'status') == i2c_stuck
        assert worker.sfp_error_dict.get(blocked_pport, (None,))[0] == i2c_stuck
        # the module that went away without an event: its TRANSCEIVER_INFO row removed, as the daemon's start removes a stale one; the others posted and published
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', gone, 'status') == '0'
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', gone, 'serial') is None
        for lp, pport, _ in asic1:
            if lp in posted:
                assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
                assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
        assert set(si_rows(clean_dbs.cli, 'asic1')) == set(posted)
        # the CMIS manager's sweep after the admission takes the posted ports to READY (the witness that it ran) and
        # leaves the blocked port to its error's end: no presence read, state INSERTED
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'READY' for lp in posted), 15), [cmis_state('asic1', lp) for lp in posted]
        assert not wait_until(lambda: cmis_state('asic1', blocked) != 'INSERTED', 2), cmis_state('asic1', blocked)
        assert 'CmisManagerTask' not in presence_readers, presence_readers
        # the error clears with a re-plug: the insertion event reads the module, posts and publishes the port, and the
        # CMIS worker, which the identical TRANSCEIVER_INFO row does not re-arm, initialises it from the state it kept
        chassis.set_presence([blocked_pport], True)
        chassis.events.append({blocked_pport: '1'})
        assert wait_until(lambda: blocked in si_rows(clean_dbs.cli, 'asic1'), 15)
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', blocked, 'status') == '1' and blocked_pport not in worker.sfp_error_dict
        assert wait_until(lambda: cmis_state('asic1', blocked) == 'READY', 15), cmis_state('asic1', blocked)
        assert published_ports(run) == sorted(asic0_ports + posted + [blocked])      # each published port exactly once, the gone one never
    finally:
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert run.log.find('Stop daemon main loop') and not run.log.find('Traceback') and not run.worker_log.find('Traceback')


def test_daemon_namespace_registered_between_cleanup_and_worker_init(clean_dbs):
    """asic1's marker appears after the daemon's init-time stale-row cleanup skipped the namespace (pending then) and
    the main loop registers it before the publishing worker's init(): the namespace is pending for the worker, as at
    its construction, and the pass at its admission removes the TRANSCEIVER_INFO row of a module that went away while
    xcvrd was down, as the daemon's cleanup does for the ready namespace."""
    populate()
    chassis = h.FakeChassis(event_period=1.0)
    asic0, asic1 = h.ports_of('asic0'), h.ports_of('asic1')
    (gone0, gone0_pport, _), (gone1, gone1_pport, _) = asic0[3], asic1[0]
    chassis.set_presence([gone0_pport, gone1_pport], False)
    for ns, lport in (('asic0', gone0), ('asic1', gone1)):                      # rows of an earlier run: deinit() keeps TRANSCEIVER_INFO
        swsscommon.Table(h.connector('STATE_DB', ns), 'TRANSCEIVER_INFO').set(lport, swsscommon.FieldValuePairs([('serial', 'STALE'), ('manufacturer', 'Molex')]))
        swsscommon.Table(h.connector('STATE_DB', ns), 'TRANSCEIVER_DOM_THRESHOLD').set(lport, swsscommon.FieldValuePairs([('temphighalarm', '75.0')]))   # left by a crash
        swsscommon.Table(h.connector('STATE_DB', ns), 'PORT_TABLE').set(lport, swsscommon.FieldValuePairs([('NPU_SI_SETTINGS_SYNC_STATUS', 'NPU_SI_SETTINGS_NOTIFIED')]))
    run = DaemonRun(chassis)
    real_cleanup = run.daemon.remove_stale_transceiver_info

    def cleanup(port_mapping):
        real_cleanup(port_mapping)
        h.write_marker('asic1', 'PortConfigDone')                                # the marker appears once the cleanup has skipped asic1
    run.daemon.remove_stale_transceiver_info = cleanup
    real_init = xcvrd_module.SfpStateUpdateTask.init
    registered_first = []

    def init(self):                                                              # the main loop registers asic1 before the worker's init() (its CONFIG_DB read usually takes longer)
        registered_first.append(wait_until(lambda: run.daemon.asic_readiness.is_ready('asic1'), 30))
        return real_init(self)
    run.patches.append(patch.object(xcvrd_module.SfpStateUpdateTask, 'init', init))
    try:
        run.start()
        assert run.sfp_subscribed.wait(60) and registered_first == [True]
        assert run.log.find('Remove stale transceiver info: Transceiver is absent for lport {}'.format(gone0))
        assert not run.log.find('Remove stale transceiver info: Transceiver is absent for lport {}'.format(gone1))
        assert h.state_field('asic0', 'TRANSCEIVER_INFO', gone0, 'serial') is None   # the daemon's cleanup covered the ready namespace
        assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (4 ports)'], \
            (deferred_passes(run), h.state_field('asic1', 'TRANSCEIVER_INFO', gone1, 'serial'))
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 0 to retry)'), notices
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', gone1, 'serial') is None   # cleaned by the admission pass
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', gone1, 'status') == '0'
        for ns, lport in (('asic0', gone0), ('asic1', gone1)):                  # both cleanups leave the other rows alone
            assert h.state_field(ns, 'TRANSCEIVER_DOM_THRESHOLD', lport, 'temphighalarm') == '75.0'
            assert status(ns, lport) == 'NPU_SI_SETTINGS_NOTIFIED'
        for lp, pport, _ in asic1[1:]:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
        assert set(si_rows(clean_dbs.cli, 'asic1')) == {lp for lp, _, _ in asic1[1:]}
        assert published_ports(run) == sorted([lp for lp, _, _ in asic0[:3]] + [lp for lp, _, _ in asic1[1:]])
    finally:
        exit_code, alive, exception = run.stop()
    assert exit_code == 0 and not alive and exception is None, (exit_code, alive, exception)
    assert run.log.find('Stop daemon main loop') and not run.log.find('Traceback') and not run.worker_log.find('Traceback')



def _rows_of_a_ready_lifetime():
    """One lifetime with both namespaces ready: the TRANSCEIVER_INFO rows the daemon writes for asic1, captured before
    the clean stop (a crash or a warm reboot leaves them in STATE_DB)."""
    populate(markers_asic1=['PortConfigDone', 'PortInitDone'])
    chassis = h.FakeChassis(event_period=1.0)
    run = DaemonRun(chassis)
    asic1_ports = [lp for lp, _, _ in h.ports_of('asic1')]
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'READY' for lp in asic1_ports), 30), \
            [cmis_state('asic1', lp) for lp in asic1_ports]
        rows = info_rows('asic1')
        assert set(rows) == set(asic1_ports), rows.keys()
    finally:
        clean_stop(run)
    return rows


@pytest.mark.parametrize('retained', [True, False], ids=['retained-identical-rows', 'no-retained-rows'])
def test_daemon_cmis_waits_for_the_late_namespace_pass(clean_dbs, retained):
    """The late namespace's modules read absent until the publishing worker's pass reads them (a platform whose
    presence depends on an initialisation the worker's own reads trigger). Between the marker and the end of the
    pass the CMIS worker neither reads the modules nor moves their state; the ports reach READY afterwards, also when
    TRANSCEIVER_INFO rows of an earlier lifetime, identical to what the pass writes, are still in STATE_DB (the
    pass's write is then no event for the CMIS observer)."""
    rows = _rows_of_a_ready_lifetime() if retained else {}
    for ns in h.NAMESPACES:
        clean_dbs.cli(ns, 'flushall')
    populate()                                                                   # asic1 without markers
    if retained:
        seed_info_rows('asic1', rows)
    chassis = h.FakeChassis(event_period=1.0)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    asic1_ports = [lp for lp, _, _ in asic1]
    asic1_pports = [pp for _, pp, _ in asic1]
    chassis.set_presence(asic1_pports, False)                                    # absent until "initialised"
    run = DaemonRun(chassis)
    release = None
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'INSERTED' for lp in asic1_ports), 15), \
            [cmis_state('asic1', lp) for lp in asic1_ports]
        held, release = chassis.hold_next_change_event()                         # the publishing worker held in its poll: no pass
        assert held.wait(30)
        mark_held, mark_release = run.hold_mark_admitted('asic1')
        h.write_marker('asic1', 'PortConfigDone')
        assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5)
        time.sleep(3)                                                            # several CMIS sweeps between the marker and the pass
        assert all(cmis_state('asic1', lp) == 'INSERTED' for lp in asic1_ports), [cmis_state('asic1', lp) for lp in asic1_ports]
        assert all(not reads for reads in cmis_presence_reads(chassis, asic1_pports).values())
        chassis.set_presence(asic1_pports, True)                                 # initialised before the pass reads them
        release.set()
        assert mark_held.wait(30)                                                # the pass is over, the mark held
        assert 'asic1' not in admitted(run) and not pass_notices(run, 'asic1')
        for lp, pport, _ in asic1:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
        time.sleep(1.5)                                                          # CMIS sweeps with the rows written and the mark held
        assert all(cmis_state('asic1', lp) == 'INSERTED' for lp in asic1_ports), [cmis_state('asic1', lp) for lp in asic1_ports]
        assert all(not reads for reads in cmis_presence_reads(chassis, asic1_pports).values())
        t_release = time.monotonic()
        mark_release.set()
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 0 to retry)'), notices
        assert 'asic1' in admitted(run)
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'READY' for lp in asic1_ports), 15), \
            [cmis_state('asic1', lp) for lp in asic1_ports]
        reads = cmis_presence_reads(chassis, asic1_pports)
        assert all(reads[pp] and min(reads[pp]) >= t_release for pp in asic1_pports), reads
        assert published_ports(run) == sorted(asic0_ports + asic1_ports)         # each port exactly once
    finally:
        if release is not None:
            release.set()
        clean_stop(run)


def test_daemon_admission_pass_waits_for_a_soaking_insertion(clean_dbs):
    """An insertion event for every port of the late namespace arrives with its marker (the shape of a platform
    that reports the whole ASIC when it comes up, an empty cage included), and the modules answer no EEPROM data
    and no API for a moment after the event's delivery. The namespace's pass waits for the events' soak, the
    events post the modules, then the pass covers every port (the empty cage's status back to absent), marks the
    namespace and the CMIS worker reads the modules only after that."""
    populate()
    chassis = h.FakeChassis(event_period=1.0)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    asic1_ports = [lp for lp, _, _ in asic1]
    asic1_pports = [pp for _, pp, _ in asic1]
    by_pport = {pp: lp for lp, pp, _ in asic1}
    (absent, absent_pport, _) = asic1[-1]
    present_pports = [pp for pp in asic1_pports if pp != absent_pport]
    present_ports = [by_pport[pp] for pp in present_pports]
    chassis.set_presence([absent_pport], False)
    cmis_log = h.LogRecorder()
    run = DaemonRun(chassis)
    run.patches.append(patch('xcvrd.cmis.cmis_manager_task.helper_logger', cmis_log))
    release = None
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'INSERTED' for lp in asic1_ports), 15)
        held, release = chassis.hold_next_change_event()
        assert held.wait(30)
        chassis.events.append({pp: '1' for pp in asic1_pports})                 # the whole ASIC, the empty cage included
        h.write_marker('asic1', 'PortConfigDone')
        assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5)
        t_release = time.monotonic()
        window_end = t_release + chassis.event_period + 1.0                      # delivery is one period after the release
        for pp in present_pports:
            h.management_init_window(chassis.sfps[pp], window_end)
        release.set()
        deferred = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: start-up pass deferred', 15)
        assert deferred == ['[asic1] SfpStateUpdateTask: start-up pass deferred, 4 physical port(s) with a soaking insertion event']
        assert wait_until(lambda: len(run.worker_log.find('Got SFP inserted event')) == len(asic1_ports), 20), \
            run.worker_log.find('Got SFP inserted event')
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 15)
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 0 to retry)'), notices
        assert len(run.worker_log.find('[asic1] SfpStateUpdateTask: start-up pass deferred')) == 1 and 'asic1' in admitted(run)
        # the order is read from the worker's own log clock, not from the test thread: the deferral, then every
        # insertion handler, then the pass (the namespace was marked right before its notice)
        stamps = {kind: [t for _, t, msg in run.worker_log.lines if token in msg]
                  for kind, token in (('deferred', 'SfpStateUpdateTask: start-up pass deferred'), ('inserted', 'Got SFP inserted event'),
                                      ('pass', '[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB'))}
        assert len(stamps['deferred']) == len(stamps['pass']) == 1 and len(stamps['inserted']) == len(asic1_ports), stamps
        assert stamps['deferred'][0] < min(stamps['inserted']) and max(stamps['inserted']) < stamps['pass'][0], stamps
        assert not run.worker_log.find('One more try')                           # the events' reads fell after the window
        # the pass covered the ports the events had posted and the empty cage: its storm status back to absent
        for pp in present_pports:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', by_pport[pp], 'serial') == 'S{}'.format(pp)
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', by_pport[pp], 'status') == '1'
        assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', absent, 'status') == '0'
        assert h.state_field('asic1', 'TRANSCEIVER_INFO', absent, 'serial') is None
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'READY' for lp in present_ports), 20), \
            [cmis_state('asic1', lp) for lp in asic1_ports]
        assert wait_until(lambda: cmis_state('asic1', absent) == 'REMOVED', 10)
        # the CMIS worker read a module only after its insertion event was handled and the window was over
        inserted_at = {msg.split(':')[0]: t for _, t, msg in run.worker_log.lines if 'Got SFP inserted event' in msg}
        reads = cmis_presence_reads(chassis, present_pports)
        for pp in present_pports:
            assert reads[pp] and min(reads[pp]) >= max(inserted_at[by_pport[pp]], window_end), (pp, reads[pp], inserted_at)
        assert not cmis_log.find('no xcvr api')
        # the event path published nothing while the namespace was pending; the pass published each port once
        assert run.worker_log.find('MEDIA_SETTINGS_DEFERRED') == ['[asic1] MEDIA_SETTINGS_DEFERRED (SfpStateUpdateTask): namespace pending, first pending port {}'.format(asic1_ports[0])]
        assert published_ports(run) == sorted(asic0_ports + present_ports)
    finally:
        if release is not None:
            release.set()
        clean_stop(run)
    assert not cmis_log.find('Traceback')


def test_daemon_admission_pass_runs_at_the_deadline_on_a_false_idle_platform(clean_dbs):
    """On a platform whose idle poll reports no status the soak of an insertion event does not mature on an idle
    poll: two soak periods after the first deferral the late namespace's pass runs anyway, over every port, and
    admits the namespace; the soaked events mature at the platform's next status-bearing poll and are handled then."""
    populate()
    chassis = h.FakeChassis(event_period=1.0, idle_status=False)
    asic0_ports = [lp for lp, _, _ in h.ports_of('asic0')]
    asic1 = h.ports_of('asic1')
    asic1_ports = [lp for lp, _, _ in asic1]
    asic1_pports = [pp for _, pp, _ in asic1]
    run = DaemonRun(chassis)
    release = None
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'INSERTED' for lp in asic1_ports), 15)
        held, release = chassis.hold_next_change_event()
        assert held.wait(30)
        chassis.events.append({pp: '1' for pp in asic1_pports})
        h.write_marker('asic1', 'PortConfigDone')
        assert run.log.wait_for('[asic1] ASIC_ACTIVE', 5)
        t_release = time.monotonic()
        release.set()
        deferred = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: start-up pass deferred', 15)
        assert deferred == ['[asic1] SfpStateUpdateTask: start-up pass deferred, 4 physical port(s) with a soaking insertion event']
        notices = run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 20)
        t_notice = time.monotonic()
        assert len(notices) == 1 and notices[0].endswith('(4 ports, 0 to retry, 4 physical port(s) still soaking)'), notices
        assert t_notice - t_release >= 2 * xcvrd_module.MGMT_INIT_TIME_DELAY_SECS, t_notice - t_release
        assert 'asic1' in admitted(run) and not run.worker_log.find('Got SFP inserted event')   # the events still soak
        for lp, pport, _ in asic1:
            assert h.state_field('asic1', 'TRANSCEIVER_INFO', lp, 'serial') == 'S{}'.format(pport)
            assert h.state_field('asic1', 'TRANSCEIVER_STATUS_SW', lp, 'status') == '1'
        assert set(si_rows(clean_dbs.cli, 'asic1')) == set(asic1_ports)
        assert wait_until(lambda: all(cmis_state('asic1', lp) == 'READY' for lp in asic1_ports), 20), \
            [cmis_state('asic1', lp) for lp in asic1_ports]
        chassis.events.append({})                                                # one status-bearing poll: the soak matures
        assert wait_until(lambda: len(run.worker_log.find('Got SFP inserted event')) == len(asic1_ports), 20)
        time.sleep(2)                                                            # the handlers' identical rows change nothing
        assert all(cmis_state('asic1', lp) == 'READY' for lp in asic1_ports)
        assert published_ports(run) == sorted(asic0_ports + asic1_ports)         # each port exactly once
    finally:
        if release is not None:
            release.set()
        clean_stop(run)


def _steady_state(clean_dbs, late):
    """The daemon over both namespaces until its STATE_DB tables are complete, asic1's marker present before the
    start or written after the start-up pass; the content of the per-port tables and the publications, with the
    clock fields normalised."""
    for ns in h.NAMESPACES:
        clean_dbs.cli(ns, 'flushall')
    populate(markers_asic1=() if late else ['PortConfigDone', 'PortInitDone'])
    chassis = h.FakeChassis(event_period=0.5)
    run = DaemonRun(chassis, dom_update_interval=1)
    every_port = [(ns, lp) for ns in h.NAMESPACES for lp, _, _ in h.ports_of(ns)]
    try:
        run.start()
        assert run.sfp_subscribed.wait(60)
        if late:
            assert run.worker_log.wait_for('SfpStateUpdateTask: Initialized port sfp status table', 30)   # the start path is over
            assert deferred_passes(run) == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (4 ports)']
            h.write_marker('asic1', 'PortConfigDone')
            assert run.worker_log.wait_for('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB', 30)
        else:
            assert not deferred_passes(run)
        assert wait_until(lambda: all(cmis_state(ns, lp) == 'READY' for ns, lp in every_port), 30), \
            [(ns, lp, cmis_state(ns, lp)) for ns, lp in every_port]
        assert wait_until(lambda: all(h.state_field(ns, 'TRANSCEIVER_DOM_SENSOR', lp, 'temperature') is not None for ns, lp in every_port), 30)
        time.sleep(2.5)                                                          # a further DOM period
        snapshot = {}
        for ns in h.NAMESPACES:
            tables = state_rows(clean_dbs.cli, ns, 'TRANSCEIVER_*|*')
            tables.update(state_rows(clean_dbs.cli, ns, 'PORT_TABLE|*'))
            for key, fields in tables.items():
                timed = key.split('|')[0].endswith(('_SET_TIME', '_CLEAR_TIME'))
                for field, value in fields.items():
                    if (timed or field.endswith('time')) and value not in ('never', 'N/A', ''):
                        fields[field] = '<time>'
            snapshot[ns] = {'state': tables, 'si': si_rows(clean_dbs.cli, ns)}
        snapshot['published'] = published_ports(run)
        snapshot['already_notified'] = sorted(l.split('lport ')[1] for l in run.worker_log.find('Media settings already notified'))
    finally:
        clean_stop(run)
    return snapshot


def test_daemon_late_namespace_steady_state_equals_start(clean_dbs):
    """Guard rail for the two start-up paths: with the same stable inputs (modules present and readable, CONFIG_DB
    unchanged, no events, no errors, cold boot policy) a namespace published at its admission ends in the same
    STATE_DB content and publications as a namespace published by the start-up pass. The timing differences are
    not compared: only the content, with the clock fields normalised."""
    start = _steady_state(clean_dbs, late=False)
    late = _steady_state(clean_dbs, late=True)
    for ns in h.NAMESPACES:
        assert set(start[ns]['state']) == set(late[ns]['state']), (ns, set(start[ns]['state']) ^ set(late[ns]['state']))
        for key in sorted(start[ns]['state']):
            assert start[ns]['state'][key] == late[ns]['state'][key], (ns, key, start[ns]['state'][key], late[ns]['state'][key])
        assert start[ns]['si'] == late[ns]['si'], ns
    assert start['published'] == late['published'] == sorted(lp for _, lp in [(ns, lp) for ns in h.NAMESPACES for lp, _, _ in h.ports_of(ns)])
    assert start['already_notified'] == late['already_notified'] == []
    # the comparison is not vacuous: the tables the two paths fill are present for every port
    asic1_ports = [lp for lp, _, _ in h.ports_of('asic1')]
    for table in ('TRANSCEIVER_INFO', 'TRANSCEIVER_STATUS_SW', 'TRANSCEIVER_DOM_THRESHOLD', 'TRANSCEIVER_DOM_SENSOR'):
        assert all('{}|{}'.format(table, lp) in late['asic1']['state'] for lp in asic1_ports), table
    assert all(late['asic1']['state']['TRANSCEIVER_DOM_SENSOR|{}'.format(lp)]['last_update_time'] == '<time>' for lp in asic1_ports)


def test_daemon_sigterm_during_burst(clean_dbs):
    populate()
    driver = os.path.join(os.path.dirname(__file__), 'xcvrd_it_driver.py')
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = dict(os.environ, PYTHONPATH=root, PYTHONUNBUFFERED='1')
    proc = subprocess.Popen([sys.executable, driver, clean_dbs.global_config], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, cwd=root)
    lines, err_lines = [], []
    stop_writer = threading.Event()
    written = {'count': 0}

    def reader():
        for line in proc.stdout:
            lines.append(line.rstrip())

    def err_reader():
        for line in proc.stderr:
            err_lines.append(line.rstrip())

    def burst():                                                                 # sustained traffic until told to stop
        tbl1 = swsscommon.Table(h.connector('APPL_DB', 'asic1'), 'PORT_TABLE')
        k = 0
        while not stop_writer.is_set():
            tbl1.set('Ethernet16', swsscommon.FieldValuePairs([('flap_count', str(k))])); k += 1
            written['count'] = k
    reader_thread = threading.Thread(target=reader, daemon=True); reader_thread.start()
    err_thread = threading.Thread(target=err_reader, daemon=True); err_thread.start()
    writer = threading.Thread(target=burst, daemon=True)
    try:
        try:
            assert wait_until(lambda: any('Start daemon main loop' in l for l in lines), 90), (lines[-30:], err_lines[-30:])
            writer.start()
            assert wait_until(lambda: written['count'] >= 1000, 30)            # the pending namespace is under load
            count_at_signal = written['count']
            t_sig = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                rc = proc.wait(60)
            except subprocess.TimeoutExpired:
                pytest.fail('daemon did not exit within 60 s of SIGTERM; last lines: {}'.format(lines[-20:]))
            t_exit = time.monotonic() - t_sig
            count_at_exit = written['count']
        finally:
            stop_writer.set()
            if writer.ident is not None:                                        # joining an unstarted thread raises
                writer.join(30)
    finally:                                                                     # the child never outlives the test
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)
        reader_thread.join(5)
        err_thread.join(5)
    assert count_at_exit > count_at_signal                                       # the traffic overlapped the shutdown
    assert rc == 0, (rc, lines[-30:], err_lines[-30:])
    assert any('Stop daemon main loop' in l for l in lines) and any('Shutting down...' in l for l in lines), lines[-30:]
    problems = [l for l in lines if 'ASIC_CHECKER_FAILED' in l or 'Traceback' in l or 'Exception' in l]
    assert not problems, problems[:10]
    assert not any('Traceback' in l for l in err_lines), err_lines[-40:]         # nothing died on a thread outside the daemon's logging
    record_result('daemon_sigterm', sigterm_to_exit='{:.2f}s'.format(t_exit), rc=rc, writes_during_shutdown=count_at_exit - count_at_signal,
                  stderr_lines=len(err_lines))
