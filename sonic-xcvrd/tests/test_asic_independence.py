"""Unit tests for XCVRD multi-ASIC startup independence: the publisher's outcomes, the readiness
registry, the publishing workers' per-namespace start-up pass, the per-namespace gates of the other
workers, the readiness checker and the daemon's init() / run() lifecycle.

Module-level mocking mirrors tests/test_xcvrd.py lines 38-43 so that this file runs alone
and inside the whole suite: swsscommon.Table, ProducerStateTable, SubscriberStateTable and
SonicDBConfig are replaced by MagicMocks at import time; swsscommon.Select is deliberately
left real and patched per test where a checker test needs it.
"""

import os
import sys
import threading
import time

import pytest

if sys.version_info >= (3, 3):
    from unittest.mock import MagicMock, patch
else:
    from mock import MagicMock, patch

from sonic_py_common import daemon_base
from swsscommon import swsscommon

daemon_base.db_connect = MagicMock()
swsscommon.Table = MagicMock()
swsscommon.ProducerStateTable = MagicMock()
swsscommon.SubscriberStateTable = MagicMock()
swsscommon.SonicDBConfig = MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
sys.path.insert(0, modules_path)

os.environ["XCVRD_UNIT_TESTING"] = "1"

from xcvrd.xcvrd import SfpStateUpdateTask
from xcvrd.dom.dom_mgr import DomInfoUpdateTask
from xcvrd.xcvrd_utilities import common
from xcvrd.xcvrd_utilities import media_settings_parser
from xcvrd.xcvrd_utilities import port_event_helper
from xcvrd.xcvrd_utilities.port_event_helper import PortChangeEvent, PortMapping
from xcvrd.xcvrd_utilities.xcvr_table_helper import (NPU_SI_SETTINGS_NOTIFIED_VALUE, NPU_SI_SETTINGS_SYNC_STATUS_KEY, XcvrTableHelper)
from .mock_swsscommon import Table

import contextlib

from sonic_platform_base.sfp_base import SfpBase
from sonic_py_common import multi_asic

from xcvrd import xcvrd as xcvrd_module
from xcvrd.xcvrd import DaemonXcvrd, SYSLOG_IDENTIFIER, NORMAL_EVENT
from xcvrd.cpo.cpo_state_task import CpoStateUpdateTask
from xcvrd.xcvrd_utilities.asic_readiness import AsicReadiness
from xcvrd.xcvrd_utilities.sfp_status_helper import SFP_STATUS_INSERTED, SFP_STATUS_REMOVED
from xcvrd.cmis.cmis_manager_task import CmisManagerTask
from xcvrd.cpo.cpo_manager_task import CpoManagerTask
from xcvrd.dom.dom_mgr import DomThermalInfoUpdateTask
from xcvrd.sff_mgr import SffManagerTask
from xcvrd.xcvrd_utilities.common import CMIS_STATE_INSERTED, CMIS_STATE_REMOVED
import collections
import gc
import weakref

from xcvrd.xcvrd import (
    ASIC_CHECKER_FAILED, ASIC_CHECKER_JOIN_TIMEOUT_SECS, ASIC_CHECKER_MAX_SELECT_ERRORS, EVENT_ON_ALL_SFP,
    PORT_CONFIG_LOAD_ERROR, RETRY_PERIOD_FOR_SYSTEM_READY_MSECS, SFP_EEPROM_NOT_READY, SFP_INSERT_EVENT_POLL_PERIOD_MSECS,
    SFP_SYSTEM_ERROR, STATE_MACHINE_UPDATE_PERIOD_MSECS, SYSTEM_NOT_READY)

REAL_SELECT = swsscommon.Select          # real class: only Table/ProducerStateTable/SubscriberStateTable/SonicDBConfig are mocked above

DEFAULT_NAMESPACE = ['']


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def mapping(ports):
    """PortMapping from [(lport, pport, asic_id), ...]."""
    port_mapping = PortMapping()
    for lport, pport, asic_id in ports:
        port_mapping.handle_port_change_event(PortChangeEvent(lport, pport, asic_id, PortChangeEvent.PORT_ADD))
    return port_mapping


class PublisherEnv:
    """Everything notify_media_setting() touches, under test control.

    cfg_rows: {lport: (found, [(field, value), ...])} served by the CONFIG_DB PORT table mock.
    update_required: the fixed answer of is_npu_si_settings_update_required(); None for the real check
    against this env's STATE_DB PORT_TABLE.
    """

    def __init__(self, cfg_rows=None, update_required=True):
        self.cfg_rows = cfg_rows if cfg_rows is not None else {
            'Ethernet0': (True, [('speed', '400000'), ('lanes', '1,2,3,4,5,6,7,8'), ('subport', '0')])}
        self.cfg_port_tbl = MagicMock()
        self.cfg_port_tbl.get = MagicMock(side_effect=lambda port: self.cfg_rows.get(port, (False, None)))
        self.app_port_tbl = Table("APPL_DB", 'PORT_TABLE')
        self.state_port_tbl = Table("STATE_DB", 'PORT_TABLE')
        self.helper = MagicMock()
        self.helper.get_cfg_port_tbl = MagicMock(return_value=self.cfg_port_tbl)
        self.helper.get_app_port_tbl = MagicMock(return_value=self.app_port_tbl)
        self.helper.get_state_port_tbl = MagicMock(return_value=self.state_port_tbl)
        self.helper.get_gearbox_line_lanes_dict = MagicMock(return_value={})
        self.helper.get_info_tables = MagicMock(side_effect=lambda asic: [self.helper.get_intf_tbl(asic)])
        if update_required is None:
            self.helper.get_state_db_port_table_val_by_key = \
                lambda lport, pm, key: XcvrTableHelper.get_state_db_port_table_val_by_key(self.helper, lport, pm, key)
            self.helper.is_npu_si_settings_update_required = \
                MagicMock(side_effect=lambda lport, pm: XcvrTableHelper.is_npu_si_settings_update_required(self.helper, lport, pm))
        else:
            self.helper.is_npu_si_settings_update_required = MagicMock(return_value=update_required)

    def writes(self):
        return self.app_port_tbl.get_size()


MEDIA_KEY = {'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP-DD-sm_media_interface',
             'lane_speed_key': 'speed:400GAUI-8', 'medium_lane_speed_key': 'UNKNOWN'}
XCVR_INFO = {'manufacturer': 'Molex', 'model': '1064141421', 'cable_type': 'Length Cable Assembly(m)',
             'cable_length': '255', 'specification_compliance': "{}", 'type_abbrv_name': 'QSFP-DD'}
MATCHING_SETTINGS = {'main': {'lane0': '0x1', 'lane1': '0x2', 'lane2': '0x3', 'lane3': '0x4',
                              'lane4': '0x5', 'lane5': '0x6', 'lane6': '0x7', 'lane7': '0x8'}}


def publisher_patches(media=MATCHING_SETTINGS, custom=None, key=MEDIA_KEY, g_dict=None):
    """Context manager patching the publisher's collaborators for one call."""
    return patch.multiple(
        'xcvrd.xcvrd_utilities.media_settings_parser',
        g_dict={'GLOBAL_MEDIA_SETTINGS': {}} if g_dict is None else g_dict,
        get_media_settings_key=MagicMock(return_value=key),
        get_media_settings_value=MagicMock(return_value=media),
        get_custom_media_settings_value=MagicMock(return_value={} if custom is None else custom),
    )


# ---------------------------------------------------------------------------
# Publisher outcomes
# ---------------------------------------------------------------------------

class TestPublisherOutcomes:

    def _call(self, env, lport='Ethernet0', port_mapping=None, transceiver_dict=None, presence=True):
        port_mapping = port_mapping if port_mapping is not None else mapping([(lport, 1, 0)])
        transceiver_dict = {1: XCVR_INFO} if transceiver_dict is None else transceiver_dict
        with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=presence)):
            media_settings_parser.notify_media_setting(lport, transceiver_dict, env.helper, port_mapping)

    def test_notify_media_setting_outcomes_and_validation(self):
        env = PublisherEnv()
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.helper_logger', log):
            # no media_settings.json loaded in this lifetime: nothing written, nothing looked up
            with publisher_patches(g_dict={}):
                self._call(env)
            assert env.writes() == 0 and env.helper.is_npu_si_settings_update_required.call_count == 0

            with publisher_patches():
                # no table helper
                pm = mapping([('Ethernet0', 1, 0)])
                with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)):
                    media_settings_parser.notify_media_setting('Ethernet0', {1: XCVR_INFO}, None, pm)
                assert log.lines('xcvr_table_helper not initialized') == ['Notify media setting: xcvr_table_helper not initialized for lport Ethernet0']

                # any non-default status suppresses, before the CONFIG_DB row is read
                notified = PublisherEnv(update_required=False)
                self._call(notified)
                assert log.lines('already notified') == ['Notify media setting: Media settings already notified for lport Ethernet0']
                assert notified.helper.get_cfg_port_tbl.call_count == 0 and notified.writes() == 0

                # mapping knows the ASIC but has no physical port: rejected after the CONFIG_DB row is read
                odd_mapping = MagicMock()
                odd_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)
                odd_mapping.logical_port_name_to_physical_port_list = MagicMock(return_value=None)
                self._call(env, port_mapping=odd_mapping)
                assert log.lines('No physical ports found') == ["Error: No physical ports found for logical port 'Ethernet0'"]
                assert env.helper.get_cfg_port_tbl.call_count == 1

                # nothing present at the publisher's own presence check
                self._call(env, presence=False)
                assert log.lines('presence not detected') == ['Media 1 presence not detected during notify']
                assert env.writes() == 0

                # present, but not in transceiver_dict
                self._call(env, transceiver_dict={})
                assert log.lines('eeprom not populated') == ['Media 1 eeprom not populated in transceiver dict']
                assert env.writes() == 0

            # no media settings for the key
            with publisher_patches(media={}, custom={}):
                self._call(env)
            assert len(log.lines('Error in obtaining media setting')) == 1
            # custom settings matched but serialise to nothing
            with publisher_patches(media={}, custom={'CUSTOM:X': {'lane0': 1}}), \
                    patch.object(media_settings_parser.CustomMediaSettingsParser, 'to_db_value', MagicMock(return_value=None)):
                self._call(env)
            assert len(log.lines('Error in obtaining media setting')) == 2
            # none of the paths above wrote APPL_DB or touched the sync status
            assert env.writes() == 0 and env.state_port_tbl.get_size() == 0

            # published: one APPL_DB write and the status flipped to notified
            with publisher_patches():
                self._call(env)
            assert env.writes() == 1
            found, status = env.state_port_tbl.get('Ethernet0')
            assert found and dict(status)[NPU_SI_SETTINGS_SYNC_STATUS_KEY] == NPU_SI_SETTINGS_NOTIFIED_VALUE

    # media_per_member lists the successive return values of get_media_settings_value(); members that
    # are absent or lack EEPROM data never reach that lookup. A member's write stands whatever happens
    # to the other member, and the sync status flips with it.
    @pytest.mark.parametrize("presence, in_dict, media_per_member, writes", [
        # absent + published
        ([False, True], [False, True], [MATCHING_SETTINGS, MATCHING_SETTINGS], 1),
        # absent + no data
        ([False, True], [False, False], [MATCHING_SETTINGS, MATCHING_SETTINGS], 0),
        # published + no data
        ([True, True], [True, False], [MATCHING_SETTINGS, MATCHING_SETTINGS], 1),
        # published + no match (write happened; the early return is kept)
        ([True, True], [True, True], [MATCHING_SETTINGS, {}], 1),
        # no data + no match (only the second member reaches the settings lookup)
        ([True, True], [False, True], [{}, {}], 0),
        # absent + absent
        ([False, False], [True, True], [MATCHING_SETTINGS, MATCHING_SETTINGS], 0),
    ])
    def test_notify_media_setting_ganged_outcomes(self, presence, in_dict, media_per_member, writes):
        env = PublisherEnv()
        ganged = MagicMock()
        ganged.get_asic_id_for_logical_port = MagicMock(return_value=0)
        ganged.logical_port_name_to_physical_port_list = MagicMock(return_value=[1, 2])
        transceiver_dict = {pport: XCVR_INFO for pport, present in zip((1, 2), in_dict) if present}
        presence_by_port = dict(zip((1, 2), presence))
        with publisher_patches(), \
                patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(side_effect=lambda p: presence_by_port[p])):
            media_settings_parser.get_media_settings_value.side_effect = list(media_per_member)
            media_settings_parser.notify_media_setting('Ethernet0', transceiver_dict, env.helper, ganged)
        assert env.writes() == writes
        assert env.state_port_tbl.get('Ethernet0')[0] == (writes == 1)


class TestPortMappingHelpers:

    def test_get_port_mapping_reads_every_namespace(self):
        table = MagicMock()
        table.getKeys = MagicMock(return_value=[])
        connect = MagicMock()
        with patch('xcvrd.xcvrd_utilities.port_event_helper.swsscommon.Table', MagicMock(return_value=table)), \
                patch('xcvrd.xcvrd_utilities.port_event_helper.daemon_base.db_connect', connect):
            port_mapping = port_event_helper.get_port_mapping(['asic0', 'asic1'])
        assert [c.kwargs['namespace'] for c in connect.call_args_list] == ['asic0', 'asic1']
        assert port_mapping.logical_port_list == []

class TestWorkerHardening:

    @patch('xcvrd.dom.dom_mgr.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortChangeObserver', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock())
    def test_dom_task_skips_port_without_object(self):
        # The port WITHOUT a device object comes first in iteration order, so the guard must be taken
        # before the pass reaches the port that ends the test; without the guard Ethernet0 is visited.
        port_mapping = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 0)])
        stop_event = threading.Event()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, {2: MagicMock()}, stop_event, True, 0)
        task.check_port_update = MagicMock()
        task.is_port_dom_monitoring_disabled = MagicMock(return_value=False)
        task.dom_db_utils = MagicMock()
        task.status_db_utils = MagicMock()
        task.vdm_db_utils = MagicMock()
        task.vdm_utils = MagicMock()
        task.vdm_utils.is_transceiver_vdm_supported = MagicMock(return_value=False)
        visited = []

        def firmware(lport, *args, **kwargs):
            visited.append(lport)
            task.task_stopping_event.set()          # one pass is enough

        task.post_port_sfp_firmware_info_to_db = MagicMock(side_effect=firmware)
        task.task_worker()
        assert visited == ['Ethernet8']            # Ethernet0 (no device object) was skipped, no exception

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    def test_sfp_task_raise_exception_interrupts_through_thread_ident(self):
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, PortMapping(), {}, threading.Event(), threading.Event())
        started = threading.Event()
        release = threading.Event()                 # independent cleanup path if the interrupt never lands

        def block(*args, **kwargs):
            started.set()
            while not release.is_set():
                time.sleep(0.05)

        task.task_worker = block
        try:
            task.start()
            assert started.wait(5)
            task.raise_exception()                  # interrupts the running body through Thread.ident
            threading.Thread.join(task, timeout=5)  # the class override has no timeout parameter
            assert not task.is_alive()
        finally:
            release.set()
            threading.Thread.join(task, timeout=5)

    def test_sfp_task_init_reports_pass_duration(self):
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()})
        task._post_port_sfp_info_and_dom_thr_to_db_once = MagicMock(return_value={'Ethernet8'})
        task._init_port_sfp_status_sw_tbl = MagicMock()
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=mapping(TWO_NS_PORTS))), \
                patch('xcvrd.xcvrd.helper_logger', log):
            task.init()
        posted = log.lines('Posted all port DOM/SFP info to DB')
        prefix = 'SfpStateUpdateTask: Posted all port DOM/SFP info to DB in '
        assert len(posted) == 1 and posted[0].startswith(prefix) and posted[0].endswith('s (2 ports, 1 to retry)')
        assert float(posted[0][len(prefix):].split('s ')[0]) >= 0
        status = log.lines('Initialized port sfp status table')
        assert len(status) == 1 and status[0].startswith('SfpStateUpdateTask: Initialized port sfp status table in ') and status[0].endswith('s')
        assert task.retry_eeprom_set == {'Ethernet8'}


# ---------------------------------------------------------------------------
# Shared helpers for the readiness registry and the publishing workers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def pin_multi_asic(namespaces):
    """Deterministic namespace <-> ASIC id conversion for multi-namespace fixtures.

    In the CI container (no asic.conf) multi_asic.get_asic_index_from_namespace() returns 0 for
    every namespace and is_multi_asic() is False, which would collapse AsicReadiness._ns_by_asic
    to one entry and make common.get_namespace_from_asic_id() return ''. The three modules that
    use these functions reference them through the multi_asic module object, so patching the
    module attributes is enough.
    """
    index = {ns: (0 if ns == '' else int(ns[len('asic'):])) for ns in namespaces}
    with patch.object(multi_asic, 'get_asic_index_from_namespace', side_effect=lambda ns: index.get(ns, 0)), \
            patch.object(multi_asic, 'is_multi_asic', return_value=len(namespaces) > 1):
        yield


def registry(ready, all_ns):
    """AsicReadiness over all_ns with the namespaces in `ready` marked."""
    with pin_multi_asic(all_ns):
        reg = AsicReadiness(all_ns)
    for ns in ready:
        reg.mark_ready(ns)
    return reg


def recorder():
    """A logger replacement whose calls can be inspected: recorder().lines(token)."""
    log = MagicMock()
    log.lines = lambda token: [c.args[0] for name in ('log_notice', 'log_warning', 'log_error', 'log_info')
                               for c in getattr(log, name).call_args_list if token in c.args[0]]
    return log


@contextlib.contextmanager
def patch_publishers(attribute, mock):
    """One mock for a method that CpoStateUpdateTask overrides (the platform poll, the info post), on both publishing
    workers' classes, so that a test drives the two workers through the same stand-in."""
    with patch.object(SfpStateUpdateTask, attribute, mock), patch.object(CpoStateUpdateTask, attribute, mock):
        yield mock


def make_sfp_task(namespaces, port_mapping, obj_dict, reg=None, warm=None, cls=SfpStateUpdateTask):
    """Publishing worker with the DB helper mocked and the boot policy sampled from `warm`."""
    warm = warm or {}
    with pin_multi_asic(namespaces), \
            patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock()), \
            patch('xcvrd.cpo.cpo_state_task.CpoXcvrTableHelper', MagicMock()), \
            patch('xcvrd.xcvrd_utilities.common.is_syncd_warm_restore_complete', side_effect=lambda ns: warm.get(ns, False)), \
            patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False)):
        if reg is None:
            task = cls(namespaces, port_mapping, obj_dict, threading.Event(), threading.Event())
        else:
            task = cls(namespaces, port_mapping, obj_dict, threading.Event(), threading.Event(), asic_readiness=reg)
    task._sample_boot_policy = lambda ns: warm.get(ns, False)      # a namespace sampled after construction reads `warm` too
    task.dom_db_utils = MagicMock()
    task.vdm_db_utils = MagicMock()
    for getter in ('get_state_port_tbl', 'get_intf_tbl', 'get_status_sw_tbl'):
        setattr(task.xcvr_table_helper, getter, MagicMock(return_value=Table("STATE_DB", getter)))
    task.xcvr_table_helper.get_info_tables = MagicMock(side_effect=lambda asic: [task.xcvr_table_helper.get_intf_tbl(asic)])
    return task


def config_db_read(task, configured=None):
    """Patch of the worker's CONFIG_DB port-mapping read, which the admission pass makes for the admitted namespace:
    it serves `configured`, by default the worker's own mapping (CONFIG_DB unchanged since the daemon's snapshot)."""
    return patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping',
                 MagicMock(side_effect=lambda namespaces: configured if configured is not None else task.port_mapping))


def run_worker_iteration(task, events, error_dict=None, db_writes=False, configured=None, events_first=False):
    """Two task_worker() iterations: an idle poll the platform answers (its event baseline), then CONFIG_DB port
    events, the admission pass, the EEPROM retry, the platform poll delivering `events` ({pport: status}) as a
    NORMAL_EVENT and the processing of the events. Returns the CONFIG_DB read mock (config_db_read()).

    events_first=True delivers `events` in the first poll instead, so that their processing precedes the admission
    pass of the second iteration. db_writes=True keeps post_port_info_to_db(), the status-table write and the
    row deletion real (the caller patches the platform wrappers, see platform_reads()); by default all three are
    mocked.
    """
    polls = [(True, {}, {}), (True, dict(events), error_dict or {})]
    if events_first:
        polls.reverse()
    with patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None))), \
            patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock()), \
            patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock()), \
            patch_publishers('_get_port_change_event', MagicMock(side_effect=polls)), \
            patch('xcvrd.xcvrd.SfpStateUpdateTask._mapping_event_from_change_event', MagicMock(return_value=NORMAL_EVENT)), \
            (contextlib.nullcontext() if db_writes else patch_publishers('post_port_info_to_db', MagicMock(return_value=None))), \
            (contextlib.nullcontext() if db_writes else patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw', MagicMock())), \
            (contextlib.nullcontext() if db_writes else patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db', MagicMock())), \
            patch('xcvrd.xcvrd.platform_chassis', MagicMock()), \
            patch('xcvrd.xcvrd._wrapper_soak_sfp_insert_event', MagicMock()), \
            config_db_read(task, configured) as reads:
        stopping = threading.Event()
        stopping.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker(stopping, threading.Event())
    return reads


def admit(task, configured=None):
    """The admission pass alone, with CONFIG_DB serving `configured` (config_db_read())."""
    with config_db_read(task, configured):
        task._admit_pending_namespaces()


def startup_pass(task, port_mapping):
    """The publishing worker's start-up pass with successful EEPROM reads."""
    with patch_publishers('post_port_info_to_db', MagicMock(return_value=None)):
        return task._post_port_sfp_info_and_dom_thr_to_db_once(port_mapping, task.xcvr_table_helper, threading.Event())


def worker_init(task, port_mapping):
    """init() of a publishing worker over port_mapping; the platform wrappers (or the post function) are the caller's."""
    with patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=port_mapping)):
        task.init()


CMIS_XCVR_INFO = dict(XCVR_INFO, cmis_rev='5.0')        # a CMIS module: post_port_info_to_db() posts the dict as is


@contextlib.contextmanager
def platform_reads(presence=True, info=CMIS_XCVR_INFO):
    """The platform wrappers behind the start-up pass: presence and EEPROM info, each a value or a callable of the pport."""
    def per_port(value):
        return value if callable(value) else (lambda pport: value)
    with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(side_effect=per_port(presence))) as presence_mock, \
            patch('xcvrd.xcvrd._wrapper_get_transceiver_info', MagicMock(side_effect=per_port(info))) as info_mock:
        yield presence_mock, info_mock


def rows(table):
    """{key: {field: value}} of a Table mock."""
    return {key: dict(table.get(key)[1]) for key in table.mock_dict}


TWO_NS = ['asic0', 'asic1']
TWO_NS_PORTS = [('Ethernet0', 1, 0), ('Ethernet8', 2, 1)]     # one port per namespace


# ---------------------------------------------------------------------------
# AsicReadiness registry
# ---------------------------------------------------------------------------

class TestAsicReadiness:

    def test_asic_readiness_registry_basic(self):
        reg = registry([], TWO_NS)
        reg.mark_ready('asic0')
        reg.mark_ready('asic0')                     # idempotent
        with pytest.raises(ValueError):
            reg.mark_ready('asic9')
        assert reg.is_ready('asic0') and not reg.is_ready('asic1') and not reg.is_ready('zz')
        assert not reg.all_ready()
        first, second = reg.ready_namespaces(), reg.ready_namespaces()
        assert first == frozenset({'asic0'}) and first is not second
        reg.mark_ready('asic1')
        assert reg.all_ready() and reg.ready_namespaces() == frozenset(TWO_NS)

    def test_asic_readiness_asic_id_helpers(self):
        with pin_multi_asic(TWO_NS):
            reg = AsicReadiness(TWO_NS)
            calls_after_construction = multi_asic.get_asic_index_from_namespace.call_count
            reg.mark_ready('asic0')
            assert reg.is_asic_ready(0) and not reg.is_asic_ready(1)
            assert not reg.is_asic_ready(None) and not reg.is_asic_ready(7)  # unattributable: waits for every namespace
            assert reg.namespace_for_asic(1) == 'asic1' and reg.namespace_for_asic(7) is None
            reg.mark_ready('asic1')
            assert reg.is_asic_ready(None) and reg.is_asic_ready(7)
            assert multi_asic.get_asic_index_from_namespace.call_count == calls_after_construction

    def test_asic_readiness_admitted_level(self):
        reg = registry(['asic0'], TWO_NS)
        # no publisher declared: the admitted set is the ready set
        assert reg.admitted_namespaces() == frozenset({'asic0'}) and reg.is_asic_admitted(0) and not reg.is_asic_admitted(1)
        reg.register_publisher('SfpStateUpdateTask', {'asic0'})                 # exempt: ready at the publisher's construction
        assert reg.admitted_namespaces() == frozenset({'asic0'})
        reg.mark_ready('asic1')
        assert reg.is_asic_ready(1) and not reg.is_asic_admitted(1)              # ready, not published yet
        sampled = reg.admitted_namespaces()
        reg.mark_admitted('SfpStateUpdateTask', 'asic1')
        assert reg.is_asic_admitted(1) and not reg.is_asic_admitted(1, sampled)  # admitted after the sample: not yet
        assert reg.is_asic_admitted(0, sampled) and not reg.is_asic_admitted(7, sampled)   # unknown id: only with a complete set
        assert reg.is_asic_admitted(7) and reg.is_asic_admitted(None)
        first, second = reg.admitted_namespaces(), reg.admitted_namespaces()
        assert first == second == frozenset(TWO_NS) and first is not second
        # two publishers: the intersection; a namespace one of them marked stays out until the other marks it
        reg.register_publisher('CpoStateUpdateTask', {'asic0'})
        assert reg.admitted_namespaces() == frozenset({'asic0'}) and not reg.is_asic_admitted(1)
        reg.mark_admitted('CpoStateUpdateTask', 'asic1')
        assert reg.admitted_namespaces() == frozenset(TWO_NS)
        # a namespace marked before it is ready counts once it is; an undeclared publisher is a wiring bug
        other = registry([], TWO_NS)
        other.register_publisher('SfpStateUpdateTask')
        other.mark_admitted('SfpStateUpdateTask', 'asic1')
        assert other.admitted_namespaces() == frozenset() and not other.is_asic_admitted(1)
        other.mark_ready('asic1')
        assert other.admitted_namespaces() == frozenset({'asic1'})
        with pytest.raises(KeyError):
            other.mark_admitted('undeclared', 'asic1')

    def test_asic_readiness_all_ready_registry(self):
        with pin_multi_asic(TWO_NS):
            reg = AsicReadiness.all_ready_registry(TWO_NS)
        assert reg.all_ready() and reg.is_ready('asic0') and reg.is_ready('asic1')
        with pin_multi_asic(['']):
            single = AsicReadiness.all_ready_registry([''])
        assert single.all_ready() and single.is_asic_ready(0) and single.namespace_for_asic(0) == ''

    def test_asic_readiness_registry_concurrent_readers(self):
        namespaces = ['asic{}'.format(i) for i in range(50)]
        reg = registry([], namespaces)
        known = frozenset(namespaces)
        errors, stop = [], threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    snapshot = reg.ready_namespaces()
                    if not snapshot <= known:
                        errors.append(snapshot)
                    reg.all_ready()
            except Exception as e:                      # a reader failure must fail the test, not vanish
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for ns in namespaces:
            reg.mark_ready(ns)
            time.sleep(0.002)
        stop.set()
        for t in threads:
            t.join(5)
        assert not errors and reg.all_ready() and not any(t.is_alive() for t in threads)


class TestRegistryWiring:

    def test_registry_built_from_discovered_namespaces(self):
        created = []

        class Recording(AsicReadiness):
            def __init__(self, namespaces):
                created.append(list(namespaces))
                super().__init__(namespaces)

        with pin_multi_asic(TWO_NS), \
                patch('xcvrd.xcvrd.AsicReadiness', Recording), \
                patch('xcvrd.xcvrd.multi_asic.get_front_end_namespaces', MagicMock(return_value=list(TWO_NS))), \
                patch('xcvrd.xcvrd.DaemonXcvrd.load_platform_util', MagicMock()), \
                patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock()), \
                patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=PortMapping())), \
                patch('xcvrd.xcvrd.common.get_pluggable_obj_dict', MagicMock(return_value={})), \
                patch('xcvrd.xcvrd.common.get_cpo_obj_dict', MagicMock(return_value={})), \
                patch('xcvrd.xcvrd.common.is_fast_reboot_enabled', MagicMock(return_value=False)), \
                patch('xcvrd.xcvrd.media_settings_parser.load_media_settings', MagicMock()), \
                patch('xcvrd.xcvrd.optics_si_parser.load_optics_si_settings', MagicMock()):
            with patch.object(multi_asic, 'get_asic_index_from_namespace') as index:
                daemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
            assert daemon.asic_readiness is None and created == [] and index.call_count == 0   # nothing built, nothing resolved for the placeholder ['']
            daemon.initialize_port_init_control_fields_in_port_table = MagicMock()
            daemon.remove_stale_transceiver_info = MagicMock()
            stub_checker(daemon, list(TWO_NS))                               # both namespaces ready in the first sweep
            daemon.init()
            reg = daemon.asic_readiness
            assert created == [list(TWO_NS)]
            assert reg.namespace_for_asic(0) == 'asic0' and reg.namespace_for_asic(1) == 'asic1'
            assert reg.all_ready()                                            # both registered before the build
            # multi-ASIC warm-policy regression: asic0's flag must be looked up under 'asic0', not ''
            task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg, warm={'asic0': True})
            with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting') as publisher:
                task._publish_media_settings('Ethernet0', {1: XCVR_INFO}, task.port_mapping)
                assert publisher.call_count == 0                                                   # asic0 warm: suppressed
                task._publish_media_settings('Ethernet8', {2: XCVR_INFO}, task.port_mapping)
                assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8']


# ---------------------------------------------------------------------------
# Publishing workers
# ---------------------------------------------------------------------------

class TestPublishingWorkers:

    def test_sfp_task_init_runs_pass_over_ready_namespaces_only(self):
        pm = mapping(TWO_NS_PORTS)
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, registry(['asic0'], TWO_NS))
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            worker_init(task, pm)
        # the real post ran for the ready namespace's port only; the pending namespace waits for its admission
        assert [c.args[0] for c in read.call_args_list] == [1]
        assert sorted(rows(task.xcvr_table_helper.get_intf_tbl(0))) == ['Ethernet0']
        assert rows(task.xcvr_table_helper.get_status_sw_tbl(0)) == {'Ethernet0': {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}}
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet0']
        assert task._pending_namespaces == {'asic1'}
        assert log.lines('PORTS_DEFERRED') == ['[asic1] PORTS_DEFERRED (SfpStateUpdateTask): namespace not ready, start-up pass deferred (1 ports)']
        assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (1 ports, 0 to retry)')
        # every namespace ready: the pass functions get the old calls (logical_ports=None) and visit both ports
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, registry(TWO_NS, TWO_NS))
        for name in ('_post_port_sfp_info_and_dom_thr_to_db_once', '_init_port_sfp_status_sw_tbl'):
            setattr(task, name, MagicMock(wraps=getattr(task, name)))
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            worker_init(task, pm)
        assert task._post_port_sfp_info_and_dom_thr_to_db_once.call_args.kwargs['logical_ports'] is None
        assert task._init_port_sfp_status_sw_tbl.call_args.kwargs['logical_ports'] is None
        assert [c.args[0] for c in read.call_args_list] == [1, 2]
        assert sorted(rows(task.xcvr_table_helper.get_intf_tbl(0))) == ['Ethernet0', 'Ethernet8']
        assert sorted(rows(task.xcvr_table_helper.get_status_sw_tbl(0))) == ['Ethernet0', 'Ethernet8']
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet0', 'Ethernet8']
        assert task._pending_namespaces == set() and log.lines('PORTS_DEFERRED') == []

    def test_sfp_task_namespace_registered_before_init_is_pending(self):
        # a namespace registered after the daemon's stale-row cleanup skipped it (pending then) and before the worker's
        # init(): pending for the worker, as at its construction, so that the pass at its admission cleans the rows
        # of a module that went away while xcvrd was down; a readiness snapshot taken at init() would give it the
        # ordinary pass (no deletion) and no admission, and its stale row would stand until its next event
        reg = registry(['asic0'], TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        intf = Table("STATE_DB", 'TRANSCEIVER_INFO')
        for lport, _, _ in TWO_NS_PORTS:
            intf.set(lport, [('manufacturer', 'Molex')])                       # rows of an earlier run; asic1's module is gone
        present = {1: True, 2: False}
        daemon = make_daemon(TWO_NS)
        daemon.asic_readiness = reg
        daemon.xcvr_table_helper = MagicMock(get_intf_tbl=MagicMock(return_value=intf), get_info_tables=MagicMock(return_value=[intf]))
        with platform_reads(presence=lambda pport: present[pport]) as (presence, _):
            daemon.remove_stale_transceiver_info(pm)                            # init(): asic1 pending, its row left
        assert [c.args[0] for c in presence.call_args_list] == [1] and sorted(rows(intf)) == ['Ethernet0', 'Ethernet8']
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)  # run(): the worker is constructed ...
        task.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=intf)
        status, state_port = task.xcvr_table_helper.get_status_sw_tbl(1), task.xcvr_table_helper.get_state_port_tbl(1)
        assert task._pending_namespaces == {'asic1'}
        reg.mark_ready('asic1')                                                  # ... and its loop registers asic1 before the worker's init()
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=lambda pport: present[pport]) as (presence, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            worker_init(task, pm)
            assert task._pending_namespaces == {'asic1'} and len(log.lines('[asic1] PORTS_DEFERRED')) == 1
            assert [c.args[0] for c in presence.call_args_list] == [1, 1] and 'Ethernet8' not in rows(status)   # asic1 not visited
            assert sorted(rows(intf)) == ['Ethernet0', 'Ethernet8']
            run_worker_iteration(task, {}, db_writes=True)                      # an answered poll, then the admission pass
        assert log.lines('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB')[0].endswith('s (1 ports, 0 to retry)')
        assert sorted(rows(intf)) == ['Ethernet0']                               # the stale row is gone
        assert rows(status) == {'Ethernet0': {'status': SFP_STATUS_INSERTED, 'error': 'N/A'},
                                'Ethernet8': {'status': SFP_STATUS_REMOVED, 'error': 'N/A'}}
        assert not state_port.get('Ethernet8')[0]                                # the sync status is not touched, as by the daemon's cleanup
        assert [c.args[0] for c in read.call_args_list] == [1]                   # the absent module is not read ...
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet0', 'Ethernet8']   # ... and offered as today (declined for lack of EEPROM data)
        assert task._pending_namespaces == set()

    def test_sfp_task_admission_pass_runs_once_at_next_iteration(self):
        reg = registry(['asic0'], TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        log = recorder()

        def asic1_calls(mock):
            return [c.args[0] for c in mock.call_args_list].count('Ethernet8')

        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(), \
                patch('xcvrd.xcvrd.helper_logger', log):
            worker_init(task, pm)
            assert 'Ethernet8' not in rows(intf) and 'Ethernet8' not in rows(status)
            reg.mark_ready('asic1')
            for _ in range(2):                                          # the second iteration finds nothing pending
                run_worker_iteration(task, {}, db_writes=True)
                assert rows(intf)['Ethernet8']['cmis_rev'] == '5.0'
                assert rows(status)['Ethernet8'] == {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}
                assert asic1_calls(task.dom_db_utils.post_port_dom_thresholds_to_db) == 1
                assert asic1_calls(task.vdm_db_utils.post_port_vdm_thresholds_to_db) == 1
                assert asic1_calls(publisher) == 1
                notices = log.lines('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB')
                assert len(notices) == 1 and notices[0].endswith('s (1 ports, 0 to retry)')
                assert task._pending_namespaces == set()

    def test_sfp_task_late_asic_recovery_is_admitted(self):
        # the admission pass reads the live platform state: a module that appeared while its namespace was pending,
        # with no platform event, gets its rows, its thresholds and one publication
        reg = registry(['asic0'], TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        present = {1: True, 2: False}
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=lambda pport: present[pport]):
            worker_init(task, pm)
            run_worker_iteration(task, {}, db_writes=True)
            assert sorted(rows(intf)) == ['Ethernet0'] and sorted(rows(status)) == ['Ethernet0']
            assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet0']
            present[2] = True
            reg.mark_ready('asic1')
            run_worker_iteration(task, {}, db_writes=True)
        assert rows(intf)['Ethernet8']['manufacturer'] == 'Molex'
        assert rows(status)['Ethernet8'] == {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}
        for thresholds in (task.dom_db_utils.post_port_dom_thresholds_to_db, task.vdm_db_utils.post_port_vdm_thresholds_to_db):
            assert [c.args[0] for c in thresholds.call_args_list].count('Ethernet8') == 1
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet0', 'Ethernet8']

    def test_sfp_task_admission_pass_uses_live_mapping(self):
        reg = registry(['asic0'], TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        for name in ('_post_port_sfp_info_and_dom_thr_to_db_once', '_init_port_sfp_status_sw_tbl'):
            setattr(task, name, MagicMock(wraps=getattr(task, name)))
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)), \
                patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db', MagicMock()), \
                patch_publishers('post_port_info_to_db', MagicMock(return_value=None)) as posted:
            # CONFIG_DB changes while the namespace is pending: Ethernet12 added on pport 3, Ethernet8 deleted
            task.on_port_config_change(PortChangeEvent('Ethernet12', 3, 1, PortChangeEvent.PORT_ADD))
            task.on_port_config_change(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_REMOVE))
            assert [c.args[0] for c in posted.call_args_list] == ['Ethernet12'] and publisher.call_count == 0
            reg.mark_ready('asic1')
            admit(task)
        assert task._post_port_sfp_info_and_dom_thr_to_db_once.call_args.kwargs['logical_ports'] == ['Ethernet12']
        assert task._init_port_sfp_status_sw_tbl.call_args.kwargs['logical_ports'] == ['Ethernet12']
        assert [c.args[0] for c in posted.call_args_list] == ['Ethernet12', 'Ethernet12']       # the DPB add, then the admission pass
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12'] and task._pending_namespaces == set()

    def test_sfp_task_admission_handles_port_deleted_before_subscription(self):
        # a port of the pending namespace deleted from CONFIG_DB between the daemon's mapping snapshot and the worker's
        # subscription has no removal event (the subscription replays the ports that exist): the worker's mapping keeps
        # it until the namespace's admission, whose pass handles it as the missed removal, whether its module is present
        # (Ethernet8, with a retry entry from an insertion read that failed while pending) or absent (Ethernet16): not
        # read, tables cleared, retry entry dropped, out of the mapping, no row re-created, not published; one CONFIG_DB
        # read for the namespace; the name re-created later is an addition again
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1), ('Ethernet16', 4, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock(), 4: MagicMock()}, reg)
        intf, status, state_port = (task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1),
                                    task.xcvr_table_helper.get_state_port_tbl(1))
        task.on_remove_logical_port = MagicMock(wraps=task.on_remove_logical_port)
        task._pending_namespaces = {'asic1'}
        task.retry_eeprom_set = {'Ethernet8'}
        task.last_retry_eeprom_time = time.time()                                   # no EEPROM retry before the admission
        configured = mapping([('Ethernet0', 1, 0), ('Ethernet12', 3, 1)])          # CONFIG_DB at the admission
        present = {1: True, 2: True, 3: True, 4: False}
        reg.mark_ready('asic1')
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=lambda pport: present[pport]) as (presence, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            reads = run_worker_iteration(task, {}, db_writes=True, configured=configured)
            assert [c.args[0] for c in read.call_args_list] == [3]
            assert sorted({c.args[0] for c in presence.call_args_list}) == [3]
            assert sorted(rows(intf)) == ['Ethernet12'] and sorted(rows(status)) == ['Ethernet12'] and rows(state_port) == {}
            assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12']
            assert [c.args[0].port_name for c in task.on_remove_logical_port.call_args_list] == ['Ethernet8', 'Ethernet16']
            assert task.port_mapping.logical_port_list == ['Ethernet0', 'Ethernet12']
            assert task.port_mapping.get_physical_to_logical(2) is None and task.port_mapping.get_physical_to_logical(4) is None
            assert task.retry_eeprom_set == set() and task._pending_namespaces == set()
            assert [c.args[0] for c in reads.call_args_list] == [['asic1']]
            assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (1 ports, 0 to retry)')
            assert log.lines('deleted from CONFIG_DB before the subscription, removed') == [
                '[asic1] SfpStateUpdateTask: Ethernet8 deleted from CONFIG_DB before the subscription, removed',
                '[asic1] SfpStateUpdateTask: Ethernet16 deleted from CONFIG_DB before the subscription, removed']
            task.last_retry_eeprom_time = 0                                         # the retry interval elapses: nothing left to retry
            task.retry_eeprom_reading()
            assert [c.args[0] for c in read.call_args_list] == [3] and [c.args[0] for c in publisher.call_args_list] == ['Ethernet12']
            # Ethernet8 re-created with its index: no longer a known port, so the subscription's SET is an addition
            with patch('xcvrd.xcvrd.platform_chassis', MagicMock()):
                port_event_helper.read_port_config_change({FakeSubscriberTable([('Ethernet8', SET, (('index', '2'),))]): 1},
                                                          task.port_mapping, log, task.on_port_config_change)
        assert task.port_mapping.is_logical_port('Ethernet8') and rows(intf)['Ethernet8']['cmis_rev'] == '5.0'
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12', 'Ethernet8']

    def test_sfp_task_admission_reads_config_db_once_per_admitted_namespace(self):
        # one namespace, ready before the worker exists: the loop never reads CONFIG_DB (the single-ASIC path is the old
        # one); two namespaces: no read while the namespace is pending, one at its admission, none afterwards
        task = make_sfp_task([''], mapping([('Ethernet0', 1, 0)]), {1: MagicMock()})
        with platform_reads(), patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()):
            worker_init(task, task.port_mapping)
            reads = run_worker_iteration(task, {1: SFP_STATUS_INSERTED}, db_writes=True)
        assert task._pending_namespaces == set() and reads.call_count == 0
        reg = registry(['asic0'], TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        with platform_reads(), patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()):
            worker_init(task, task.port_mapping)
            assert run_worker_iteration(task, {}, db_writes=True).call_count == 0
            reg.mark_ready('asic1')
            assert [c.args[0] for c in run_worker_iteration(task, {}, db_writes=True).call_args_list] == [['asic1']]
            assert run_worker_iteration(task, {}, db_writes=True).call_count == 0

    def test_sfp_task_event_paths_defer_publication_without_bookkeeping(self):
        reg = registry(['asic0'], TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(), \
                patch('xcvrd.xcvrd.helper_logger', log):
            run_worker_iteration(task, {2: SFP_STATUS_INSERTED}, db_writes=True)
            # the rows are posted; the publication waits for the namespace's admission pass, nothing is recorded per port
            assert rows(intf)['Ethernet8']['cmis_rev'] == '5.0'
            assert rows(status)['Ethernet8'] == {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}
            assert publisher.call_count == 0 and task._pending_namespaces == {'asic1'}
            assert log.lines('MEDIA_SETTINGS_DEFERRED') == ['[asic1] MEDIA_SETTINGS_DEFERRED (SfpStateUpdateTask): namespace pending, first pending port Ethernet8']
            reg.mark_ready('asic1')
            run_worker_iteration(task, {}, db_writes=True)
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8'] and task._pending_namespaces == set()
        assert len(log.lines('MEDIA_SETTINGS_DEFERRED')) == 1

    def test_two_publishing_workers_publish_their_own_ports(self):
        # each publishing worker polls its own event stream and handles the ports of its own device dictionary: the
        # CPO port's insertion event reaches the CPO worker, which publishes the port at once when its namespace is
        # ready and defers it otherwise; nothing is recorded per port, and the admission pass of the owning worker
        # publishes it once (the pluggable worker's pass leaves the port alone)
        pm = mapping(TWO_NS_PORTS)
        for ready in (True, False):
            reg = registry(['asic0', 'asic1'] if ready else ['asic0'], TWO_NS)
            sfp = make_sfp_task(TWO_NS, pm, {1: MagicMock()}, reg)
            cpo = make_sfp_task(TWO_NS, pm, {2: MagicMock()}, reg, cls=CpoStateUpdateTask)
            with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                    patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)), \
                    patch('xcvrd.xcvrd._wrapper_get_transceiver_info', MagicMock(return_value=XCVR_INFO)):
                run_worker_iteration(sfp, {})
                run_worker_iteration(cpo, {2: SFP_STATUS_INSERTED})
                if ready:
                    assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8']
                else:
                    assert publisher.call_count == 0
                    sfp._pending_namespaces, cpo._pending_namespaces = {'asic1'}, {'asic1'}
                    reg.mark_ready('asic1')
                    run_worker_iteration(sfp, {})
                    run_worker_iteration(cpo, {})
                    assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8']

    def test_two_publishing_workers_admission_pass_as_at_base(self):
        # both publishing workers run the admitted namespace's start-up pass, as both run the start-up pass at init(),
        # each over the ports of its own device dictionary: the CPO port is posted and published by the CPO worker
        reg = registry(['asic0'], TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        env = PublisherEnv(cfg_rows={'Ethernet8': (True, [('speed', '400000'), ('lanes', '1,2,3,4,5,6,7,8'), ('subport', '0')])},
                           update_required=None)
        env.helper.get_intf_tbl = MagicMock(return_value=Table("STATE_DB", 'TRANSCEIVER_INFO'))
        env.helper.get_status_sw_tbl = MagicMock(return_value=Table("STATE_DB", 'TRANSCEIVER_STATUS_SW'))
        env.app_port_tbl.set = MagicMock(wraps=env.app_port_tbl.set)
        sfp = make_sfp_task(TWO_NS, pm, {1: MagicMock()}, reg)
        cpo = make_sfp_task(TWO_NS, pm, {2: MagicMock()}, reg, cls=CpoStateUpdateTask)
        for task in (sfp, cpo):
            task.xcvr_table_helper = env.helper
            task._pending_namespaces = {'asic1'}
        reg.mark_ready('asic1')
        log = recorder()
        # the CPO worker's own info post (OE and ELSFP reads) is replaced by one that reads the module's info
        cpo_post = MagicMock(side_effect=lambda lport, pm, table, tdict, stop_event=None: tdict.update({2: CMIS_XCVR_INFO}))
        with publisher_patches(), platform_reads(), patch('xcvrd.xcvrd.helper_logger', log), \
                patch.object(CpoStateUpdateTask, 'post_port_info_to_db', cpo_post):
            admit(sfp)
            admit(cpo)
        assert [c.args[0] for c in cpo_post.call_args_list] == ['Ethernet8']
        assert env.writes() == 1 and env.app_port_tbl.set.call_count == 1
        assert env.helper.is_npu_si_settings_update_required.call_count == 1             # the owning worker only
        found, row = env.state_port_tbl.get('Ethernet8')
        assert found and dict(row)[NPU_SI_SETTINGS_SYNC_STATUS_KEY] == NPU_SI_SETTINGS_NOTIFIED_VALUE
        assert sfp._pending_namespaces == set() and cpo._pending_namespaces == set()
        assert [line.split(': Posted')[0] for line in log.lines('Posted all port DOM/SFP info')] == ['[asic1] SfpStateUpdateTask', '[asic1] CpoStateUpdateTask']

    def test_sfp_task_admission_position_in_loop(self):
        # the admission opens the iteration that follows a poll the platform answered (after that poll's events, before
        # the EEPROM retry); a poll that reported a system event (SYSTEM_NOT_READY) leaves it closed for the next
        # iteration; an event-less poll counts as answered whatever its status flag, as the state machine treats it
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, registry(TWO_NS, TWO_NS))
        order = []
        polls = [(True, {}, {}), (True, {}, {}), (False, {EVENT_ON_ALL_SFP: SYSTEM_NOT_READY}, {}), (True, {}, {}),
                 (False, {}, {}), (True, {}, {})]
        task._admit_pending_namespaces = MagicMock(side_effect=lambda: order.append('admit'))
        task.retry_eeprom_reading = MagicMock(side_effect=lambda: order.append('retry'))
        with patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock(side_effect=lambda *a, **k: order.append('config'))), \
                patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None))), \
                patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock()), \
                patch_publishers('_get_port_change_event', MagicMock(side_effect=lambda timeout: order.append('poll') or polls.pop(0))), \
                patch('xcvrd.xcvrd._wrapper_soak_sfp_insert_event', MagicMock()), \
                patch('xcvrd.xcvrd.time.sleep', MagicMock()):                    # the SYSTEM_NOT_READY branch sleeps out its period
            stopping = threading.Event()
            stopping.is_set = MagicMock(side_effect=[False] * 6 + [True])
            task.task_worker(stopping, threading.Event())
        assert order == ['config', 'retry', 'poll',                              # no answered poll yet
                         'config', 'admit', 'retry', 'poll',                     # answered: admission before the retry
                         'config', 'admit', 'retry', 'poll',                     # this poll reports SYSTEM_NOT_READY ...
                         'config', 'retry', 'poll',                              # ... so no admission follows it
                         'config', 'admit', 'retry', 'poll',                     # this poll is event-less with status False ...
                         'config', 'admit', 'retry', 'poll']                     # ... which counts as answered
        assert polls == []

    def test_sfp_task_pending_namespace_shortens_poll(self):
        # while a namespace is pending the loop polls at the start-up period, so that its admission is at most one
        # poll away; the insert-soak period is shorter and still wins; after the admission the state's timeout is back
        reg = registry(['asic0'], TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        insertion = {1: SFP_STATUS_INSERTED}
        polls = []
        script = [                                                      # per poll: (what happens during it, the events it returns)
            (lambda: None, insertion),                                  # INIT state, asic1 pending; the event takes the state to NORMAL
            (lambda: task.sfp_insert_events.update({2: time.time()}), {}),              # NORMAL state, asic1 pending
            (lambda: (task.sfp_insert_events.clear(), reg.mark_ready('asic1')), insertion),   # insert soak pending; asic1 admitted at the next iteration
            (lambda: None, {}),                                         # NORMAL state, nothing pending
        ]

        def poll(timeout):
            polls.append(timeout)
            during, events = script.pop(0)
            during()
            return True, dict(events), {}

        with patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None))), \
                patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock()), \
                patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock()), \
                patch_publishers('_get_port_change_event', MagicMock(side_effect=poll)), \
                patch('xcvrd.xcvrd._wrapper_soak_sfp_insert_event', MagicMock()), \
                patch_publishers('post_port_info_to_db', MagicMock(return_value=None)), \
                patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw', MagicMock()), \
                patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)), \
                patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()):
            stopping = threading.Event()
            stopping.is_set = MagicMock(side_effect=[False] * len(script) + [True])
            task.task_worker(stopping, threading.Event())
        assert polls == [RETRY_PERIOD_FOR_SYSTEM_READY_MSECS, RETRY_PERIOD_FOR_SYSTEM_READY_MSECS,
                         SFP_INSERT_EVENT_POLL_PERIOD_MSECS, STATE_MACHINE_UPDATE_PERIOD_MSECS]      # 5000, 5000, 1000, 60000
        assert script == [] and task._pending_namespaces == set()

    def test_sfp_task_admission_retry_merge(self):
        # the admission adds its result to the retry set and drops nothing: the entry of a port the pass read stands
        # (the retry completes what the pass could not, thresholds a transient fault left out) and the next retry
        # reads the port once more, as after an insertion event while its entry stands
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        task.retry_eeprom_set = {'Ethernet0', 'Ethernet12'}            # another namespace's entry, and one the pass reads
        eeprom = {1: None, 2: None, 3: CMIS_XCVR_INFO}
        reg.mark_ready('asic1')
        log = recorder()
        thresholds = task.dom_db_utils.post_port_dom_thresholds_to_db
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(info=lambda pport: eeprom[pport]), \
                patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
            assert task.retry_eeprom_set == {'Ethernet0', 'Ethernet8', 'Ethernet12'}
            assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12']
            assert [c.args[0] for c in thresholds.call_args_list] == ['Ethernet12']
            assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (2 ports, 1 to retry)')
            eeprom[2] = CMIS_XCVR_INFO
            task.last_retry_eeprom_time = 0
            task.retry_eeprom_reading()
        assert sorted(c.args[0] for c in publisher.call_args_list) == ['Ethernet12', 'Ethernet12', 'Ethernet8']
        assert sorted(c.args[0] for c in thresholds.call_args_list) == ['Ethernet12', 'Ethernet12', 'Ethernet8']
        assert task.retry_eeprom_set == {'Ethernet0'}
        assert sorted(rows(task.xcvr_table_helper.get_intf_tbl(1))) == ['Ethernet12', 'Ethernet8']

    def test_sfp_task_admission_keeps_blocked_port_in_retry_set(self):
        # a port of the admitted namespace that sits in the retry set behind a blocking error recorded meanwhile: the
        # pass does not read it, its entry survives the admission, which adds to the set, and the retry completes the
        # port once the error has turned non-blocking without an insertion event
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf = task.xcvr_table_helper.get_intf_tbl(1)
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        high_temp = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_HIGH_TEMP)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read):
            task.last_retry_eeprom_time = time.time()                           # the retry runs only when forced below
            run_worker_iteration(task, {2: blocking}, db_writes=True)           # error event while pending
            task.retry_eeprom_set = {'Ethernet0', 'Ethernet8'}                  # another namespace's entry, and the blocked port's
            reg.mark_ready('asic1')
            admit(task)
            assert task.retry_eeprom_set == {'Ethernet0', 'Ethernet8'}
            assert [c.args[0] for c in read.call_args_list] == [3]
            run_worker_iteration(task, {2: high_temp}, db_writes=True)          # the error turns non-blocking, no insertion
            assert task.sfp_error_dict[2][0] == high_temp and 'Ethernet8' not in rows(intf)
            task.last_retry_eeprom_time = 0
            task.retry_eeprom_reading()
        assert sorted(c.args[0] for c in read.call_args_list[1:]) == [1, 2]     # the retry read both entries
        assert sorted(c.args[0] for c in publisher.call_args_list) == ['Ethernet0', 'Ethernet12', 'Ethernet8']
        assert 'Ethernet8' in rows(intf) and task.retry_eeprom_set == set()

    def test_sfp_task_admission_blocked_port_recovers_when_error_clears(self):
        # a module present at boot in a pending namespace whose blocking error is recorded before the admission: the
        # pass leaves it unread but retried, so the port gets its rows when the error turns non-blocking (an insertion
        # event would complete it on its own)
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        power = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            task.last_retry_eeprom_time = time.time()
            run_worker_iteration(task, {2: blocking}, db_writes=True)
            reg.mark_ready('asic1')
            run_worker_iteration(task, {}, db_writes=True)                      # admission
            assert task._pending_namespaces == set() and task.retry_eeprom_set == {'Ethernet8'}
            assert [c.args[0] for c in read.call_args_list] == [3] and sorted(rows(intf)) == ['Ethernet12']
            assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (2 ports, 1 to retry)')
            run_worker_iteration(task, {2: power}, db_writes=True)              # the error turns non-blocking
            assert rows(status)['Ethernet8']['status'] == power
            task.last_retry_eeprom_time = 0
            task.retry_eeprom_reading()
        assert [c.args[0] for c in read.call_args_list] == [3, 2]
        assert sorted(rows(intf)) == ['Ethernet12', 'Ethernet8'] and task.retry_eeprom_set == set()
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12', 'Ethernet8']
        for thresholds in (task.dom_db_utils.post_port_dom_thresholds_to_db, task.vdm_db_utils.post_port_vdm_thresholds_to_db):
            assert [c.args[0] for c in thresholds.call_args_list] == ['Ethernet12', 'Ethernet8']

    def test_sfp_task_blocked_retry_entry_completes_after_non_blocking_error(self):
        # the sequence a late namespace's module can produce: its insertion matures while the namespace is pending and
        # both reads fail (retry entry), it reports a blocking error, the namespace is admitted (no read behind the
        # error, the entry kept), the error turns into a non-blocking one that is not plain INSERTED, which only
        # rewrites the status row: the next retry posts the port's info and thresholds and publishes its settings
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_I2C_STUCK)
        power = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED)
        eeprom = {2: None, 3: CMIS_XCVR_INFO}
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(info=lambda pport: eeprom[pport]) as (_, read), \
                patch('xcvrd.xcvrd.TIME_FOR_SFP_READY_SECS', 0), \
                patch('xcvrd.xcvrd.helper_logger', log):
            run_worker_iteration(task, {2: SFP_STATUS_INSERTED}, db_writes=True)   # the insertion: the read fails
            assert [c.args[0] for c in read.call_args_list] == [2] and task.retry_eeprom_set == {'Ethernet8'}   # the second attempt reuses the cached None
            assert rows(status)['Ethernet8']['status'] == SFP_STATUS_INSERTED and 'Ethernet8' not in rows(intf)
            task.last_retry_eeprom_time = time.time()                               # the retry interval has not elapsed
            run_worker_iteration(task, {2: blocking}, db_writes=True)               # the blocking error
            assert task._recorded_error('Ethernet8', task.port_mapping) == (True, True)
            assert rows(status)['Ethernet8'] == {'status': blocking, 'error': '{}|{}'.format(
                SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING, SfpBase.SFP_ERROR_DESCRIPTION_I2C_STUCK)}
            reg.mark_ready('asic1')
            run_worker_iteration(task, {}, db_writes=True)                          # the admission
            assert task._pending_namespaces == set()
            assert [c.args[0] for c in read.call_args_list] == [2, 3]               # no read behind the blocking error
            assert task.retry_eeprom_set == {'Ethernet8'}                           # the entry survives the pass
            assert log.lines('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info')[0].endswith('s (2 ports, 1 to retry)')
            eeprom[2] = CMIS_XCVR_INFO                                               # readable once the error is not blocking
            run_worker_iteration(task, {2: power}, db_writes=True)                  # the error turns non-blocking
            assert task._recorded_error('Ethernet8', task.port_mapping) == (True, False)
            assert rows(status)['Ethernet8'] == {'status': power, 'error': SfpBase.SFP_ERROR_DESCRIPTION_POWER_BUDGET_EXCEEDED}
            assert [c.args[0] for c in read.call_args_list] == [2, 3] and 'Ethernet8' not in rows(intf)   # the event reads nothing
            task.last_retry_eeprom_time = 0                                          # the retry interval elapses
            run_worker_iteration(task, {}, db_writes=True)
        assert [c.args[0] for c in read.call_args_list] == [2, 3, 2]
        assert sorted(rows(intf)) == ['Ethernet12', 'Ethernet8']
        for thresholds in (task.dom_db_utils.post_port_dom_thresholds_to_db, task.vdm_db_utils.post_port_vdm_thresholds_to_db):
            assert [c.args[0] for c in thresholds.call_args_list] == ['Ethernet12', 'Ethernet8']
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12', 'Ethernet8']
        assert task.retry_eeprom_set == set()
        assert rows(status)['Ethernet8']['status'] == power                         # the error row stands, as on an event path

    def test_sfp_task_admission_pass_respects_cached_errors(self):
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        high_temp = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_HIGH_TEMP)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            task.last_retry_eeprom_time = time.time()                           # the retry is not part of this test
            run_worker_iteration(task, {2: blocking, 3: high_temp}, db_writes=True)       # error events while pending
            error_rows = rows(status)
            assert error_rows == {'Ethernet8': {'status': blocking, 'error': 'Blocking EEPROM from being read'},
                                  'Ethernet12': {'status': high_temp, 'error': 'High temperature'}}
            assert read.call_count == 0 and rows(intf) == {}
            reg.mark_ready('asic1')
            run_worker_iteration(task, {}, db_writes=True)
        # no EEPROM read behind the blocking error, the port is left to the retry; the other port is posted and
        # published; both error rows are kept
        assert [c.args[0] for c in read.call_args_list] == [3]
        assert sorted(rows(intf)) == ['Ethernet12']
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet12']
        assert rows(status) == error_rows
        assert task._pending_namespaces == set() and task.retry_eeprom_set == {'Ethernet8'}
        assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (2 ports, 1 to retry)')

    def test_sfp_task_error_cache_is_per_worker(self):
        # each publishing worker caches the platform errors it consumed itself, as today: an error the other worker
        # recorded is unknown to this worker's passes (every platform without CPO ports runs one worker; each worker
        # consumes the events of the ports in its own device dictionary)
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1)])
        sfp = make_sfp_task(TWO_NS, pm, {1: MagicMock()}, reg)
        other = make_sfp_task(TWO_NS, pm, {2: MagicMock()}, reg, cls=CpoStateUpdateTask)
        other.xcvr_table_helper = sfp.xcvr_table_helper                                    # one STATE_DB
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), platform_reads():
            run_worker_iteration(other, {2: blocking}, db_writes=True)
        assert other.sfp_error_dict == {2: (blocking, {})} and sfp.sfp_error_dict == {}
        assert other._recorded_error('Ethernet8', other.port_mapping) == (True, True) and sfp._recorded_error('Ethernet8', sfp.port_mapping) == (False, False)

    def test_sfp_task_admission_removes_stale_info_of_absent_port(self):
        # a module that went away while its namespace was pending had no removal event handled: the admission pass
        # removes its TRANSCEIVER_INFO row as the daemon's start removes a stale one and writes REMOVED (thresholds,
        # NPU sync status and cache entry stand as today) and publishes nothing; a port behind a recorded blocking
        # error is not probed (on some platforms presence is an EEPROM read): its rows stand and it is retried; the
        # present port is posted as usual
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1), ('Ethernet16', 4, 1)])
        env = PublisherEnv(cfg_rows={lport: (True, [('speed', '400000'), ('lanes', '1,2,3,4,5,6,7,8'), ('subport', '0')])
                                     for lport in ('Ethernet8', 'Ethernet12', 'Ethernet16')}, update_required=None)
        intf, status, dom_thr = (Table("STATE_DB", name) for name in ('TRANSCEIVER_INFO', 'TRANSCEIVER_STATUS_SW', 'TRANSCEIVER_DOM_THRESHOLD'))
        env.helper.get_intf_tbl = MagicMock(return_value=intf)
        env.helper.get_status_sw_tbl = MagicMock(return_value=status)
        env.helper.get_dom_threshold_tbl = MagicMock(return_value=dom_thr)
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock(), 4: MagicMock()}, reg)
        task.xcvr_table_helper = env.helper
        task._pending_namespaces = {'asic1'}
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        for lport in ('Ethernet8', 'Ethernet16'):
            intf.set(lport, [('manufacturer', 'Molex')])                       # rows of the modules' earlier presence
            dom_thr.set(lport, [('temphighalarm', '75.0')])
            env.state_port_tbl.set(lport, [(NPU_SI_SETTINGS_SYNC_STATUS_KEY, NPU_SI_SETTINGS_NOTIFIED_VALUE)])
        task.sfp_error_dict[4] = (blocking, {})                                  # Ethernet16 reported a blocking error; its presence reads False
        status.set('Ethernet16', [('status', blocking), ('error', SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING)])
        present = {2: False, 3: True, 4: False}
        reg.mark_ready('asic1')
        log = recorder()
        with publisher_patches(), platform_reads(presence=lambda pport: present[pport]) as (presence, read), \
                patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db', wraps=common.del_port_sfp_dom_info_from_db) as deleter, \
                patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
        assert [c.args[0] for c in deleter.call_args_list] == ['Ethernet8'] and deleter.call_args.args[2] == [intf]
        assert sorted(rows(intf)) == ['Ethernet12', 'Ethernet16'] and sorted(rows(dom_thr)) == ['Ethernet16', 'Ethernet8']
        assert rows(status) == {'Ethernet8': {'status': SFP_STATUS_REMOVED, 'error': 'N/A'},
                                'Ethernet12': {'status': SFP_STATUS_INSERTED, 'error': 'N/A'},
                                'Ethernet16': {'status': blocking, 'error': SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING}}
        assert [c.args[0] for c in read.call_args_list] == [3]
        assert 4 not in [c.args[0] for c in presence.call_args_list]           # no presence read behind the blocking error
        assert env.writes() == 1 and env.app_port_tbl.get('Ethernet12')[0]      # the present port published, the others not
        for lport in ('Ethernet8', 'Ethernet16'):                               # the sync status stands, as today
            assert dict(env.state_port_tbl.get(lport)[1]) == {NPU_SI_SETTINGS_SYNC_STATUS_KEY: NPU_SI_SETTINGS_NOTIFIED_VALUE}
        assert task.sfp_error_dict == {4: (blocking, {})}
        assert task._pending_namespaces == set() and task.retry_eeprom_set == {'Ethernet16'}
        assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (3 ports, 1 to retry)')

    def test_sfp_task_admission_cleans_before_it_publishes(self):
        # the admission pass writes the status rows and cleans the absent ports before it reads and publishes, as the
        # daemon's start cleans before the workers publish; the start-up pass keeps today's order
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        calls = []
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads(presence=lambda pport: calls.append(('presence', pport)) or True,
                               info=lambda pport: calls.append(('read', pport)) or CMIS_XCVR_INFO):
            worker_init(task, pm)
            assert calls == [('presence', 1), ('read', 1), ('presence', 1)]        # info pass, then status pass
            del calls[:]
            reg.mark_ready('asic1')
            admit(task)
        assert calls == [('presence', 2), ('presence', 3), ('presence', 2), ('read', 2), ('presence', 3), ('read', 3)]

    def test_sfp_task_admission_keeps_rows_of_module_that_stops_answering(self):
        # a module enters a blocking error after the admission pass wrote its status row and before its info read (on
        # some platforms presence then reads absent): the pass does not take it for absent, its rows posted while the
        # namespace was pending stand, and the error event that follows writes its error row
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        intf.set('Ethernet8', [('manufacturer', 'Molex')])                        # posted by the event path while pending
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        answers = {2: [True]}                                                     # present at the status pass, absent from then on

        def presence(pport):
            return answers.get(pport, [True]).pop() if answers.get(pport) else pport != 2
        reg.mark_ready('asic1')
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=presence) as (_, read), \
                patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db', wraps=common.del_port_sfp_dom_info_from_db) as deleter:
            run_worker_iteration(task, {2: blocking}, db_writes=True)             # the admission, then the error event
        assert [c.args[0] for c in read.call_args_list] == [3]
        assert 'Ethernet8' not in [c.args[0] for c in deleter.call_args_list if c.args[2] and c.args[2][0] is intf]
        assert rows(intf)['Ethernet8'] == {'manufacturer': 'Molex'} and 'Ethernet12' in rows(intf)
        assert rows(status) == {'Ethernet8': {'status': blocking, 'error': SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING},
                                'Ethernet12': {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}}
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8', 'Ethernet12']   # the absent read is offered as today, declined
        assert task.sfp_error_dict == {2: (blocking, {})} and task.retry_eeprom_set == set()

    def test_sfp_task_admission_leaves_blocked_port_to_its_events(self):
        # a port with a recorded blocking error whose presence reads absent at admission (on NVIDIA's firmware-control
        # path presence is an EEPROM read): no presence read, no deletion, error row and cache entry intact, the next
        # event decides; the namespace's absent port without an error is still cleaned, its present port posted
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1), ('Ethernet16', 4, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock(), 4: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        intf, status = task.xcvr_table_helper.get_intf_tbl(1), task.xcvr_table_helper.get_status_sw_tbl(1)
        i2c_stuck = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_I2C_STUCK)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), platform_reads():
            run_worker_iteration(task, {4: SFP_STATUS_INSERTED}, db_writes=True)     # Ethernet16 inserted while pending ...
            run_worker_iteration(task, {4: i2c_stuck}, db_writes=True)               # ... then its EEPROM access is blocked
        error_row = rows(status)['Ethernet16']
        assert error_row['status'] == i2c_stuck and task.sfp_error_dict == {4: (i2c_stuck, {})} and 'Ethernet16' in rows(intf)
        intf.set('Ethernet8', [('manufacturer', 'Molex')])                        # Ethernet8's module went away, no event
        present = {2: False, 3: True, 4: False}
        reg.mark_ready('asic1')
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=lambda pport: present[pport]) as (presence, read), \
                patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db', wraps=common.del_port_sfp_dom_info_from_db) as deleter:
            admit(task)
        assert 4 not in [c.args[0] for c in presence.call_args_list] and [c.args[0] for c in read.call_args_list] == [3]
        assert [c.args[0] for c in deleter.call_args_list] == ['Ethernet8']
        assert sorted(rows(intf)) == ['Ethernet12', 'Ethernet16']
        assert rows(status) == {'Ethernet8': {'status': SFP_STATUS_REMOVED, 'error': 'N/A'},
                                'Ethernet12': {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}, 'Ethernet16': error_row}
        assert task.sfp_error_dict == {4: (i2c_stuck, {})}
        # the blocked port is not offered to the publisher; the absent one is, as today (declined for lack of EEPROM data)
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8', 'Ethernet12']
        assert task._pending_namespaces == set() and task.retry_eeprom_set == {'Ethernet16'}   # retried like an unreadable one
        # the error clears with a re-plug: the insertion event reads, posts and publishes the port (the retry entry
        # stays until a retry succeeds, as after any insertion of a port in the set; kept out of this window)
        present[4] = True
        task.last_retry_eeprom_time = time.time()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=lambda pport: present[pport]):
            run_worker_iteration(task, {4: SFP_STATUS_INSERTED}, db_writes=True)
        assert task.sfp_error_dict == {} and rows(status)['Ethernet16'] == {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet16'] and task.retry_eeprom_set == {'Ethernet16'}


    def test_sfp_task_init_pass_leaves_blocked_port_to_its_events(self):
        # the guard applies to the start-up pass as to the admission pass: behind a recorded blocking error neither
        # pass reads the port, presence included, and the error row stands (a worker's own cache is empty at its
        # start-up pass; the entry stands in for one recorded before the pass)
        reg = registry(TWO_NS, TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        sfp = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)
        status = sfp.xcvr_table_helper.get_status_sw_tbl(1)
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        error_row = {'status': blocking, 'error': SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING}
        sfp.sfp_error_dict[2] = (blocking, {})
        status.set('Ethernet8', list(error_row.items()))
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads(presence=lambda pport: pport != 2) as (presence, read):     # behind the error presence reads absent
            worker_init(sfp, pm)
        assert [c.args[0] for c in presence.call_args_list] == [1, 1] and [c.args[0] for c in read.call_args_list] == [1]
        assert rows(status) == {'Ethernet0': {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}, 'Ethernet8': error_row}
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet0']
        assert sfp.sfp_error_dict == {2: (blocking, {})} and sfp.retry_eeprom_set == {'Ethernet8'}

    def test_sfp_task_error_cache_keyed_by_port_index(self):
        # a platform may key its module errors as int and its removal/insertion events as str: the cache is
        # kept by port index, so the later events clear the error and the recovered port is read and published
        reg = registry(['asic0'], TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        blocking = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            run_worker_iteration(task, {2: blocking})
            assert task.sfp_error_dict == {2: (blocking, {})} and task._recorded_error('Ethernet8', task.port_mapping) == (True, True)
            run_worker_iteration(task, {'2': SFP_STATUS_REMOVED})
            run_worker_iteration(task, {'2': SFP_STATUS_INSERTED})
            assert task.sfp_error_dict == {} and task._recorded_error('Ethernet8', task.port_mapping) == (False, False)
            assert publisher.call_count == 0                           # the namespace is pending: deferred, nothing recorded
            reg.mark_ready('asic1')
            run_worker_iteration(task, {}, db_writes=True)
        assert [c.args[0] for c in read.call_args_list] == [2]             # the admission pass reads the recovered port
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8']
        posted = log.lines('[asic1] SfpStateUpdateTask: Posted all port DOM/SFP info to DB')
        assert len(posted) == 1 and posted[0].endswith('s (1 ports, 0 to retry)')

    def test_sfp_task_add_logical_port_finds_str_keyed_vendor_error(self):
        # a vendor error and its description keyed as str by the platform: a logical port added on that physical
        # port afterwards gets the cached description (looked up by integer index) instead of an exception
        reg = registry(TWO_NS, TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        vendor_error = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING | 0x10000)
        with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)):
            run_worker_iteration(task, {'2': vendor_error}, error_dict={'2': 'vendor fault'})
            assert task.sfp_error_dict == {2: (vendor_error, {2: 'vendor fault'})}
            with patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw') as status_write:
                task.on_add_logical_port(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_ADD))
        assert status_write.call_args.args[2] == vendor_error and status_write.call_args.args[3].endswith('|vendor fault')

    def test_sfp_task_error_dict_non_integer_key_tolerated(self):
        # a platform may key an error description by something other than a port index: the entry is kept under its
        # own key instead of failing the event, the index-keyed entries are still cached by index
        reg = registry(TWO_NS, TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        vendor_error = str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING | 0x10000)
        with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)):
            run_worker_iteration(task, {'2': vendor_error}, error_dict={'2': 'vendor fault', 'Ethernet0': 'not a port index'})
            assert task.sfp_error_dict == {2: (vendor_error, {2: 'vendor fault', 'Ethernet0': 'not a port index'})}
            with patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw') as status_write:
                task.on_add_logical_port(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_ADD))
        assert status_write.call_args.args[2] == vendor_error and status_write.call_args.args[3].endswith('|vendor fault')

    def test_sfp_task_error_cache_entry_counts_when_its_value_parses(self):
        # an entry whose status value is not a number records nothing for its port: the admission pass writes the
        # port's status row and reads the port as any other; a ganged port is recorded through its parsable member
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
        task._pending_namespaces = {'asic1'}
        task.sfp_error_dict[2] = ('not-an-event', {})
        assert task._recorded_error('Ethernet8', task.port_mapping) == (False, False)
        status = task.xcvr_table_helper.get_status_sw_tbl(1)
        reg.mark_ready('asic1')
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read):
            admit(task)
        assert rows(status)['Ethernet8'] == {'status': SFP_STATUS_INSERTED, 'error': 'N/A'}
        assert [c.args[0] for c in read.call_args_list] == [2, 3] and task.retry_eeprom_set == set()
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8', 'Ethernet12']
        ganged = MagicMock(logical_port_name_to_physical_port_list=MagicMock(return_value=[1, 2]))
        task.sfp_error_dict[1] = (str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING), {})
        assert task._recorded_error('Ethernet0', ganged) == (True, True)
        task.sfp_error_dict[1] = (str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_HIGH_TEMP), {})
        assert task._recorded_error('Ethernet0', ganged) == (True, False)

    def test_sfp_task_admission_checks_stop_between_ports(self):
        ports = [('Ethernet{}'.format(4 * i), 100 + i, 1) for i in range(200)]
        reg = registry([], ['asic1'])
        task = make_sfp_task(['asic1'], mapping(ports), {p: MagicMock() for _, p, _ in ports}, reg)
        task._pending_namespaces = {'asic1'}
        reg.mark_ready('asic1')
        calls = []

        def presence(pport):
            calls.append(pport)
            if len(calls) == 3:
                task.task_stopping_event.set()
            return True

        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads(presence=presence), \
                patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
        assert len(calls) <= 3 and log.lines('Posted all port DOM/SFP info') == []
        assert 'asic1' not in reg.admitted_namespaces() and task.retry_eeprom_set == set()

    def _soaking_fixture(self, warm=None):
        """asic1 pending and ready, with Ethernet8's module (pport 2) inside its insertion soak (the platform keyed the
        event as str); Ethernet12 (pport 3) is the namespace's other port."""
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg, warm=warm)
        assert task._pending_namespaces == {'asic1'} and reg.admitted_namespaces() == frozenset({'asic0'})
        task.sfp_insert_events = {'2': time.time()}
        reg.mark_ready('asic1')
        return reg, task

    def test_sfp_task_admission_pass_waits_for_a_soaking_insertion(self):
        # a module inserted just before the admission, whose insertion event is held for the management-init delay:
        # the namespace's pass is deferred as a whole (one notice), the namespace stays pending and unadmitted; once
        # the event has been handled the next attempt runs the pass over every port of the namespace and admits it
        reg, task = self._soaking_fixture()
        intf = task.xcvr_table_helper.get_intf_tbl(1)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
            admit(task)                                                          # still soaking: deferred again, silently
            assert read.call_count == 0 and rows(intf) == {} and task._pending_namespaces == {'asic1'}
            assert 'asic1' not in reg.admitted_namespaces()
            assert log.lines('start-up pass deferred') == ['[asic1] SfpStateUpdateTask: start-up pass deferred, 1 physical port(s) with a soaking insertion event']
            assert log.lines('Posted all port DOM/SFP info') == []
            task.sfp_insert_events.clear()                                       # the soak ends: the event is delivered ...
            run_worker_iteration(task, {2: SFP_STATUS_INSERTED}, db_writes=True, events_first=True)   # ... handled, then the pass
        assert [c.args[0] for c in read.call_args_list] == [2, 2, 3]            # the event's read, then the pass over every port
        assert sorted(rows(intf)) == ['Ethernet12', 'Ethernet8'] and 'asic1' in reg.admitted_namespaces()
        assert task._pending_namespaces == set() and task._pass_deadline == {}
        assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (2 ports, 0 to retry)')
        # the event path published nothing while the namespace was pending; the pass published both ports
        assert log.lines('MEDIA_SETTINGS_DEFERRED') == ['[asic1] MEDIA_SETTINGS_DEFERRED (SfpStateUpdateTask): namespace pending, first pending port Ethernet8']
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8', 'Ethernet12']

    def test_sfp_task_admission_pass_runs_at_the_deadline_while_soaking(self):
        # a soak that does not mature (a platform whose idle polls answer no status): two soak periods after the
        # first deferral the pass runs anyway, over every port, and says how many are still soaking
        reg, task = self._soaking_fixture()
        intf = task.xcvr_table_helper.get_intf_tbl(1)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.MGMT_INIT_TIME_DELAY_SECS', 0.05), \
                patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
            assert read.call_count == 0 and len(log.lines('start-up pass deferred')) == 1
            time.sleep(0.15)
            admit(task)
        assert [c.args[0] for c in read.call_args_list] == [2, 3] and sorted(rows(intf)) == ['Ethernet12', 'Ethernet8']
        assert 'asic1' in reg.admitted_namespaces() and task._pass_deadline == {}
        assert log.lines('Posted all port DOM/SFP info')[0].endswith('s (2 ports, 0 to retry, 1 physical port(s) still soaking)')
        assert '2' in task.sfp_insert_events                                     # the soak entry stands, as the event path left it

    def test_sfp_task_admission_pass_deferral_is_per_namespace(self):
        # a soaking insertion in another namespace, or on a port deleted from CONFIG_DB, does not hold the pass; a
        # removal event ends the hold before the deadline
        reg, task = self._soaking_fixture()
        task.sfp_insert_events = {1: time.time()}                                # asic0's port
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
        assert [c.args[0] for c in read.call_args_list] == [2, 3] and log.lines('start-up pass deferred') == []
        for ending in ('config_del', 'removal'):
            reg, task = self._soaking_fixture()
            log = recorder()
            with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                    platform_reads() as (_, read), \
                    patch('xcvrd.xcvrd.helper_logger', log):
                admit(task)
                assert read.call_count == 0 and 'asic1' in task._pass_deadline
                if ending == 'config_del':
                    task.on_port_config_change(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_REMOVE))
                else:
                    xcvrd_module._wrapper_soak_sfp_insert_event(task.sfp_insert_events, {'2': SFP_STATUS_REMOVED})
                admit(task)
            assert [c.args[0] for c in read.call_args_list] == ([3] if ending == 'config_del' else [2, 3]), ending
            assert 'asic1' in reg.admitted_namespaces() and task._pass_deadline == {}
            assert len(log.lines('start-up pass deferred')) == 1 and len(log.lines('Posted all port DOM/SFP info')) == 1

    def test_sfp_task_admission_stop_during_deferral_leaves_namespace_pending(self):
        reg, task = self._soaking_fixture()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads() as (_, read):
            admit(task)
            task.task_stopping_event.set()
            reads = config_db_read(task)
            with reads as config_reads:
                task._admit_pending_namespaces()
        assert read.call_count == 0 and config_reads.call_count == 0
        assert task._pending_namespaces == {'asic1'} and 'asic1' not in reg.admitted_namespaces()

    def test_sfp_task_admission_reconciles_config_db_once(self):
        # the CONFIG_DB reconcile runs at the first attempt only: a deferred attempt does not read again, and a row
        # that would fail the read later is never read (the port's later deletions arrive through the subscription)
        reg, task = self._soaking_fixture()
        answers = [task.port_mapping, RuntimeError('a row without an index')]
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads() as (_, read), \
                patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping',
                      MagicMock(side_effect=lambda namespaces: answers.pop(0))) as config_reads:
            task._admit_pending_namespaces()                                     # deferred after the reconcile
            assert config_reads.call_count == 1 and read.call_count == 0 and task._reconciled == {'asic1'}
            task._admit_pending_namespaces()                                     # deferred again: no read
            task.sfp_insert_events.clear()
            task._admit_pending_namespaces()                                     # the pass
        assert config_reads.call_count == 1 and [c.args[0] for c in read.call_args_list] == [2, 3]
        assert 'asic1' in reg.admitted_namespaces()

    def test_sfp_task_boot_policy_sampled_when_the_pass_runs(self):
        # the warm flag is up while the pass is deferred and down when it runs: the sample is the pass's, so the
        # namespace's media settings are published
        warm = {'asic1': True}
        reg, task = self._soaking_fixture(warm=warm)
        assert task.warm_fast_reboot_status['asic1'] is None
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                platform_reads():
            admit(task)                                                          # deferred: not sampled
            assert task.warm_fast_reboot_status['asic1'] is None
            warm['asic1'] = False
            task.sfp_insert_events.clear()
            admit(task)
        assert task.warm_fast_reboot_status['asic1'] is False
        assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet8', 'Ethernet12']

    def test_sfp_task_admission_arms_the_retry_clock(self):
        # the pass's unreadable port enters an empty retry set: the retry that follows the admission in the same
        # iteration waits for the ordinary interval instead of reading the port again at once; a set that already had
        # an entry keeps its clock (the entry is due, as today)
        for preset in (set(), {'Ethernet0'}):
            reg = registry(['asic0'], TWO_NS)
            pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
            task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()}, reg)
            task.retry_eeprom_set = set(preset)
            t0 = time.time() - 30 if preset else 0                              # the existing entry's clock: half an interval ago
            task.last_retry_eeprom_time = t0
            eeprom = {1: CMIS_XCVR_INFO, 2: None, 3: CMIS_XCVR_INFO}
            reg.mark_ready('asic1')
            with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                    platform_reads(info=lambda pport: eeprom[pport]) as (_, read):
                run_worker_iteration(task, {}, db_writes=True)                   # the admission, then the retry
            assert [c.args[0] for c in read.call_args_list] == [2, 3]            # the pass only: no retry read in this iteration
            if not preset:
                assert task.last_retry_eeprom_time > 0                           # the clock armed by the admission
            else:
                assert task.last_retry_eeprom_time == t0                         # the existing entry's clock kept
            assert task.retry_eeprom_set == preset | {'Ethernet8'} and 'asic1' in reg.admitted_namespaces()

    def test_sfp_task_admission_marks_the_namespace_before_its_notice(self):
        reg = registry(['asic0'], TWO_NS)
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        reg.mark_ready('asic1')
        log = recorder()
        seen = {}

        def mark(name, ns):
            seen['notices_at_mark'] = list(log.lines('Posted all port DOM/SFP info'))
            seen['pending_at_mark'] = set(task._pending_namespaces)
            AsicReadiness.mark_admitted(reg, name, ns)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads(), patch.object(reg, 'mark_admitted', mark), patch('xcvrd.xcvrd.helper_logger', log):
            admit(task)
        assert seen == {'notices_at_mark': [], 'pending_at_mark': set()} and 'asic1' in reg.admitted_namespaces()
        assert len(log.lines('Posted all port DOM/SFP info')) == 1

    def test_two_publishing_workers_each_mark_their_pass(self):
        # a CPO chassis: the namespace is admitted once both publishing workers have run their pass; each worker
        # soaks its own insertion events, so one of them may still be deferred while the other has passed
        reg = registry(['asic0'], TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        sfp = make_sfp_task(TWO_NS, pm, {1: MagicMock()}, reg)
        cpo = make_sfp_task(TWO_NS, pm, {2: MagicMock()}, reg, cls=CpoStateUpdateTask)
        assert set(reg._admitted) == {'SfpStateUpdateTask', 'CpoStateUpdateTask'} and cpo.name == 'CpoStateUpdateTask'
        cpo.sfp_insert_events = {2: time.time()}
        reg.mark_ready('asic1')
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), \
                platform_reads(), patch('xcvrd.xcvrd.helper_logger', log):
            admit(sfp)
            admit(cpo)                                                           # deferred by its own soak
            assert reg.admitted_namespaces() == frozenset({'asic0'}) and not reg.is_asic_admitted(1)
            assert [line.split(': ', 1)[0] for line in log.lines('start-up pass deferred')] == ['[asic1] CpoStateUpdateTask']
            cpo.sfp_insert_events.clear()
            admit(cpo)
        assert reg.admitted_namespaces() == frozenset(TWO_NS)
        assert [line.split(': Posted')[0] for line in log.lines('Posted all port DOM/SFP info')] == ['[asic1] SfpStateUpdateTask', '[asic1] CpoStateUpdateTask']

    def test_sfp_task_constructor_declares_publisher_with_its_snapshot(self):
        # the publishing worker declares itself where it takes its pending snapshot: the namespaces ready then are
        # exempt from the gate, a namespace registered afterwards waits for the worker's pass
        reg = registry(['asic0'], TWO_NS)
        assert reg.admitted_namespaces() == frozenset({'asic0'})                 # no publisher yet: the ready set
        pm = mapping(TWO_NS_PORTS)
        sfps = {1: MagicMock(), 2: MagicMock()}
        task = make_sfp_task(TWO_NS, pm, sfps, reg)
        assert reg._admitted == {'SfpStateUpdateTask': {'asic0'}} and task._pending_namespaces == {'asic1'}
        reg.mark_ready('asic1')
        assert reg.admitted_namespaces() == frozenset({'asic0'})
        cmis = make_cmis_task(TWO_NS, pm, sfps, reg)
        for lport, pport, asic in TWO_NS_PORTS:
            cmis.on_port_update_event(PortChangeEvent(lport, pport, asic, PortChangeEvent.PORT_SET, dict(CMIS_PORT_ATTRS)))
        cmis_iteration(cmis, 2)
        assert sfps[1].get_presence.call_count == 1 and sfps[2].method_calls == []   # ready is not enough
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()), platform_reads():
            admit(task)
        cmis_iteration(cmis, 2)
        assert sfps[2].get_presence.call_count == 1                              # processed after the pass

    def test_sfp_task_boot_policy_sampled_per_namespace(self):
        pm = mapping(TWO_NS_PORTS)
        # (a) the constructor samples the namespaces that are ready then; a namespace not ready is sampled once, when
        # its start-up pass runs at its admission (the event paths before it defer their publication), and that
        # sample decides
        for warm_later in (True, False):
            flags = {'asic0': False}
            with pin_multi_asic(TWO_NS), \
                    patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock()), \
                    patch('xcvrd.xcvrd_utilities.common.is_syncd_warm_restore_complete', MagicMock(side_effect=lambda ns: flags[ns])) as warm, \
                    patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False)) as fast:
                reg = registry(['asic0'], TWO_NS)
                task = SfpStateUpdateTask(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, threading.Event(), threading.Event(), asic_readiness=reg)
                assert [c.args[0] for c in warm.call_args_list] == ['asic0'] and fast.call_count == 1
                assert task.warm_fast_reboot_status == {'asic0': False, 'asic1': None}
                flags['asic1'] = warm_later                                    # the namespace's databases are populated later
                task.dom_db_utils, task.vdm_db_utils = MagicMock(), MagicMock()
                task.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=Table("STATE_DB", 'TRANSCEIVER_STATUS_SW'))   # read by the admission pass
                task._pending_namespaces = {'asic1'}
                with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting') as publisher, \
                        patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)), \
                        patch_publishers('post_port_info_to_db', MagicMock(return_value=None)):
                    task._publish_media_settings('Ethernet8', {2: XCVR_INFO}, pm)         # pending: deferred, not sampled
                    assert publisher.call_count == 0 and task.warm_fast_reboot_status['asic1'] is None
                    reg.mark_ready('asic1')
                    task._publish_media_settings('Ethernet8', {2: XCVR_INFO}, pm)         # an event path before the pass: still deferred
                    assert publisher.call_count == 0 and task.warm_fast_reboot_status['asic1'] is None
                    admit(task)
                    assert publisher.call_count == (0 if warm_later else 1) and task._pending_namespaces == set()
                assert [c.args[0] for c in warm.call_args_list] == ['asic0', 'asic1']         # one sample per namespace
                assert task.warm_fast_reboot_status == {'asic0': False, 'asic1': warm_later}
        # (a2) the sample does not wait for a readable module: it is taken when the namespace's pass runs, at its
        # admission; a namespace that turned ready between the constructor and init() is pending for init() (the
        # constructor's snapshot decides, not sampled there) and sampled at its admission like any other
        for at_init in (False, True):
            flags = {'asic0': False, 'asic1': True}
            with pin_multi_asic(TWO_NS), \
                    patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock()), \
                    patch('xcvrd.xcvrd_utilities.common.is_syncd_warm_restore_complete', MagicMock(side_effect=lambda ns: flags[ns])) as warm, \
                    patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False)):
                reg = registry(['asic0'], TWO_NS)
                task = SfpStateUpdateTask(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, threading.Event(), threading.Event(), asic_readiness=reg)
                task.dom_db_utils, task.vdm_db_utils = MagicMock(), MagicMock()
                task.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=Table("STATE_DB", 'TRANSCEIVER_STATUS_SW'))
                reg.mark_ready('asic1')
                with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting') as publisher, \
                        patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)), \
                        patch_publishers('post_port_info_to_db', MagicMock(return_value=SFP_EEPROM_NOT_READY)):   # nothing readable
                    if at_init:
                        worker_init(task, pm)                                     # asic1 pending since the constructor: asic0 only
                        assert [c.args[0] for c in warm.call_args_list] == ['asic0'] and task.retry_eeprom_set == {'Ethernet0'}
                        assert task._pending_namespaces == {'asic1'} and task.warm_fast_reboot_status['asic1'] is None
                    admit(task)
                    assert publisher.call_count == 0 and task.retry_eeprom_set == ({'Ethernet0', 'Ethernet8'} if at_init else {'Ethernet8'})
                assert [c.args[0] for c in warm.call_args_list] == ['asic0', 'asic1'] and task.warm_fast_reboot_status['asic1'] is True
        # (b) profiles skipped by init() (fast flag seen there) while the worker later samples a cold policy
        cold = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, registry(TWO_NS, TWO_NS))
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.g_dict', {}), \
                patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', wraps=media_settings_parser.notify_media_setting) as publisher:
            cold._publish_media_settings('Ethernet0', {1: XCVR_INFO}, pm)
        assert publisher.call_count == 1 and cold.xcvr_table_helper.get_app_port_tbl.call_count == 0     # reached, nothing to publish
        # (c) two workers sampling on opposite sides of a flag clear keep their own values
        before = make_sfp_task(TWO_NS, pm, {1: MagicMock()}, registry(TWO_NS, TWO_NS), warm={'asic0': True})
        after = make_sfp_task(TWO_NS, pm, {2: MagicMock()}, registry(TWO_NS, TWO_NS), warm={}, cls=CpoStateUpdateTask)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher:
            before._publish_media_settings('Ethernet0', {1: XCVR_INFO}, pm)
            assert publisher.call_count == 0                                                   # warm: suppressed
            after._publish_media_settings('Ethernet0', {1: XCVR_INFO}, pm)
            assert publisher.call_count == 1

    def test_sfp_task_all_publication_paths_use_helper(self):
        pm = mapping([('Ethernet0', 1, 0)])
        task = make_sfp_task([''], pm, {1: MagicMock()}, registry([''], ['']))
        task._publish_media_settings = MagicMock(wraps=task._publish_media_settings)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)):
            startup_pass(task, pm)                                                          # 1
            run_worker_iteration(task, {1: SFP_STATUS_INSERTED})                            # 2
            task.retry_eeprom_set = {'Ethernet0'}
            task.last_retry_eeprom_time = 0
            with patch_publishers('post_port_info_to_db', MagicMock(return_value=None)):
                task.on_add_logical_port(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))   # 3
                task.retry_eeprom_reading()                                                 # 4
                task._pending_namespaces = {''}
                admit(task)                                            # 5
        assert task._publish_media_settings.call_count == 5
        assert publisher.call_count == 5                        # every publisher call went through the helper

    def test_sfp_task_default_registry_publishes_immediately(self):
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1), ('Ethernet12', 3, 1)])
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock(), 3: MagicMock()})       # no asic_readiness argument
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                patch('xcvrd.xcvrd.helper_logger', log), \
                patch_publishers('post_port_info_to_db', MagicMock(return_value=None)):
            worker_init(task, pm)
        assert publisher.call_count == 3 and task._pending_namespaces == set() and log.lines('_DEFERRED') == []

    def test_sfp_task_no_asic_outcome(self):
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet40', 9, 5)])          # ASIC 5 is not a managed namespace
        task = make_sfp_task(TWO_NS, pm, {1: MagicMock(), 9: MagicMock()}, reg)
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                patch('xcvrd.xcvrd.helper_logger', log), \
                patch('xcvrd.xcvrd_utilities.common.get_namespace_from_asic_id', MagicMock(return_value='asic5')) as legacy:
            task._publish_media_settings('Ethernet999', {}, pm)
            assert publisher.call_count == 0 and len(log.lines('no ASIC index')) == 1
            task._publish_media_settings('Ethernet40', {9: XCVR_INFO}, pm)
            assert [c.args[0] for c in publisher.call_args_list] == ['Ethernet40'] and legacy.call_args.args[0] == 5
            assert task._pending_namespaces == {'asic1'}                        # the constructor's set, untouched

    def test_cpo_state_task_forwards_readiness(self):
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet0', 1, 1), ('Ethernet8', 2, 1)])
        task = make_sfp_task(TWO_NS, pm, {2: MagicMock()}, reg, cls=CpoStateUpdateTask)
        assert task.asic_readiness is reg and task.name == 'CpoStateUpdateTask'
        log = recorder()
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting') as publisher, \
                patch_publishers('post_port_info_to_db', MagicMock(return_value=None)), \
                patch('xcvrd.xcvrd.helper_logger', log):
            startup_pass(task, pm)
            worker_init(task, pm)
        assert publisher.call_count == 0 and task._pending_namespaces == {'asic1'}
        assert '(CpoStateUpdateTask)' in log.lines('MEDIA_SETTINGS_DEFERRED')[0]
        assert log.lines('PORTS_DEFERRED') == ['[asic1] PORTS_DEFERRED (CpoStateUpdateTask): namespace not ready, start-up pass deferred (2 ports)']


# ---------------------------------------------------------------------------
# Gated workers (CMIS/CPO manager, SFF, DOM)
# ---------------------------------------------------------------------------

CMIS_PORT_ATTRS = {'speed': '400000', 'lanes': '1,2,3,4,5,6,7,8', 'host_tx_ready': 'true', 'admin_status': 'up', 'subport': '0'}


def make_cmis_task(namespaces, port_mapping, obj_dict, reg=None, cls=CmisManagerTask):
    """CMIS manager with the DB helper mocked; the STATE_DB CMIS-state table is a real Table mock."""
    with pin_multi_asic(namespaces), \
            patch('xcvrd.cmis.cmis_manager_task.XcvrTableHelper', MagicMock()), \
            patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False)):
        kwargs = {'asic_readiness': reg} if reg is not None else {}
        task = cls(namespaces, port_mapping, obj_dict, threading.Event(), **kwargs)
    task.status_table = Table("STATE_DB", "TRANSCEIVER_STATUS_SW")
    task.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=task.status_table)
    task.xcvr_table_helper.get_gearbox_line_lanes_dict = MagicMock(return_value={})
    return task


def cmis_iteration(task, ports):
    """One task_worker() iteration of the CMIS manager over `ports` (no observer events)."""
    with patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock())):
        task.task_stopping_event.is_set = MagicMock(side_effect=[False] * (1 + ports) + [True])
        task.task_worker()


def cmis_state(task, lport):
    found, state = task.status_table.hget(lport, 'cmis_state')
    return state if found else None


class TestCmisGate:

    def _two_port_task(self, reg):
        pm = mapping(TWO_NS_PORTS)
        sfps = {1: MagicMock(name='sfp1'), 2: MagicMock(name='sfp2')}
        task = make_cmis_task(TWO_NS, pm, sfps, reg)
        for lport, pport, asic in TWO_NS_PORTS:
            task.on_port_update_event(PortChangeEvent(lport, pport, asic, PortChangeEvent.PORT_SET, dict(CMIS_PORT_ATTRS)))
        return task, sfps

    def test_cmis_task_skips_ports_of_pending_namespace(self):
        reg = registry(['asic0'], TWO_NS)
        task, sfps = self._two_port_task(reg)
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED        # observer metadata, allowed while pending
        with patch('xcvrd.xcvrd_utilities.common.get_cmis_state_from_state_db', wraps=common.get_cmis_state_from_state_db) as state_read:
            cmis_iteration(task, 2)
            assert [c.args[0] for c in state_read.call_args_list] == ['Ethernet0']
        assert sfps[1].get_presence.call_count == 1                        # asic0 port processed
        assert sfps[2].method_calls == []                                  # no device access for the pending port
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED        # never advanced

        reg.mark_ready('asic1')
        with patch('xcvrd.xcvrd_utilities.common.get_cmis_state_from_state_db', wraps=common.get_cmis_state_from_state_db) as state_read:
            cmis_iteration(task, 2)                                        # no observer event, no platform event
            assert sorted(c.args[0] for c in state_read.call_args_list) == ['Ethernet0', 'Ethernet8']
        assert sfps[2].get_presence.call_count == 1                        # processed with the metadata accumulated while pending
        assert task.port_dict['Ethernet8']['speed'] == '400000' and task.port_dict['Ethernet8']['host_tx_ready'] == 'true'

    def test_cmis_task_observer_metadata_allowed_while_pending(self):
        reg = registry(['asic0'], TWO_NS)
        task, sfps = self._two_port_task(reg)
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_DEL, {}, 'STATE_DB', 'TRANSCEIVER_INFO'))
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_REMOVED
        task.process_single_lport('Ethernet8', task.port_dict['Ethernet8'])
        assert sfps[2].method_calls == [] and cmis_state(task, 'Ethernet8') == CMIS_STATE_REMOVED

    def test_cmis_task_run_does_not_wait_for_port_config_done(self):
        assert not hasattr(CmisManagerTask, 'wait_for_port_config_done')
        task = make_cmis_task([''], mapping([('Ethernet0', 1, 0)]), {1: MagicMock()})
        task.task_worker = MagicMock()
        log = recorder()
        with patch('xcvrd.cmis.cmis_manager_task.helper_logger', log), \
                patch('xcvrd.cmis.cmis_manager_task.swsscommon.SubscriberStateTable') as sub_table:
            task.run()
        assert task.task_worker.call_count == 1 and sub_table.call_count == 0
        assert log.lines('Waiting for PortConfigDone') == []

    def test_cmis_task_default_registry_processes_all(self):
        pm = mapping(TWO_NS_PORTS)
        sfps = {1: MagicMock(), 2: MagicMock()}
        task = make_cmis_task(TWO_NS, pm, sfps)                              # no asic_readiness argument
        for lport, pport, asic in TWO_NS_PORTS:
            task.on_port_update_event(PortChangeEvent(lport, pport, asic, PortChangeEvent.PORT_SET, dict(CMIS_PORT_ATTRS)))
        cmis_iteration(task, 2)
        assert sfps[1].get_presence.call_count == 1 and sfps[2].get_presence.call_count == 1

    def test_cmis_task_logs_first_deferral_once_per_namespace(self):
        reg = registry(['asic0'], TWO_NS)
        task, sfps = self._two_port_task(reg)
        log = recorder()
        with patch('xcvrd.cmis.cmis_manager_task.helper_logger', log):
            cmis_iteration(task, 2)
            cmis_iteration(task, 2)                                        # still pending: no second notice
            reg.mark_ready('asic1')
            cmis_iteration(task, 2)
        assert log.lines('PORTS_DEFERRED') == ['[asic1] PORTS_DEFERRED (CmisManagerTask): namespace pending, first deferred port Ethernet8']

    def test_cmis_task_waits_for_the_publishing_workers_pass(self):
        reg = registry(['asic0'], TWO_NS)
        reg.register_publisher('SfpStateUpdateTask', {'asic0'})
        task, sfps = self._two_port_task(reg)
        reg.mark_ready('asic1')                                                # the marker alone opens nothing
        log = recorder()
        with patch('xcvrd.cmis.cmis_manager_task.helper_logger', log):
            cmis_iteration(task, 2)
        assert sfps[1].get_presence.call_count == 1 and sfps[2].method_calls == []
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED
        assert log.lines('PORTS_DEFERRED') == ['[asic1] PORTS_DEFERRED (CmisManagerTask): namespace pending, first deferred port Ethernet8']
        reg.mark_admitted('SfpStateUpdateTask', 'asic1')                      # the publishing worker's pass is over
        cmis_iteration(task, 2)
        assert sfps[2].get_presence.call_count == 1

    def test_cmis_task_admission_during_iteration_waits_for_the_next_cache(self):
        reg = registry(['asic0'], TWO_NS)
        reg.register_publisher('SfpStateUpdateTask', {'asic0'})
        reg.mark_ready('asic1')
        task, sfps = self._two_port_task(reg)
        # the namespace is admitted while this iteration's gearbox cache is being read
        task.xcvr_table_helper.get_gearbox_line_lanes_dict = MagicMock(side_effect=lambda: reg.mark_admitted('SfpStateUpdateTask', 'asic1') or {})
        cmis_iteration(task, 2)
        assert sfps[1].get_presence.call_count == 1 and sfps[2].method_calls == []   # not processed with the stale cache
        cmis_iteration(task, 2)
        assert sfps[2].get_presence.call_count == 1                                   # processed in the next iteration

    def test_cmis_task_leaves_a_port_behind_a_blocking_error(self):
        reg = registry(TWO_NS, TWO_NS)
        task, sfps = self._two_port_task(reg)

        def status_row(lport, error):                                             # the row the port's last event wrote
            task.status_table.set(lport, [('cmis_state', CMIS_STATE_INSERTED), ('status', '1'), ('error', error)])
        status_row('Ethernet0', SfpBase.SFP_ERROR_DESCRIPTION_HIGH_TEMP)          # a non-blocking error gates nothing
        status_row('Ethernet8', BLOCKING_ERROR)
        cmis_iteration(task, 2)
        cmis_iteration(task, 2)                                                  # revisited each iteration, still behind the error
        assert sfps[1].get_presence.call_count == 1 and sfps[2].method_calls == []
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED              # no state taken behind the error
        status_row('Ethernet8', 'N/A')                                           # the error ended (the clearing insertion's row)
        cmis_iteration(task, 2)
        assert sfps[2].get_presence.call_count == 1

    def test_cmis_task_restarts_an_interrupted_initialisation_after_a_blocking_error(self):
        reg = registry(TWO_NS, TWO_NS)
        task, sfps = self._two_port_task(reg)
        table = task.status_table

        def merging_set(key, fvs, _set=table.set):                               # the real table keeps the other fields
            found, row = table.get(key)
            merged = dict(row) if found else {}
            merged.update(dict(fvs))
            _set(key, list(merged.items()))
        table.set = merging_set
        task.port_dict['Ethernet8']['cmis_retries'] = 1
        task.port_dict['Ethernet8']['cmis_expired'] = object()                     # a timer the error outlives
        table.set('Ethernet8', [('cmis_state', common.CMIS_STATE_DP_INIT), ('status', '1'), ('error', BLOCKING_ERROR)])
        cmis_iteration(task, 2)
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED and sfps[2].method_calls == []
        assert task.port_dict['Ethernet8']['cmis_retries'] == 1 and task.port_dict['Ethernet8']['cmis_expired'] is None
        cmis_iteration(task, 2)                                                  # still behind the error: nothing more
        assert cmis_state(task, 'Ethernet8') == CMIS_STATE_INSERTED and sfps[2].method_calls == []
        table.set('Ethernet8', [('status', '1'), ('error', 'N/A')])              # the error ended: the initialisation restarts
        cmis_iteration(task, 2)
        assert sfps[2].get_presence.call_count == 1

    def test_cpo_manager_task_forwards_readiness(self):
        reg = registry(['asic0'], TWO_NS)
        task = make_cmis_task(TWO_NS, mapping(TWO_NS_PORTS), {2: MagicMock()}, reg, cls=CpoManagerTask)
        assert task.asic_readiness is reg and task.name == 'CpoManagerTask'


SFF_ATTRS = {'type': 'QSFP28', 'subport': '0', 'lanes': '1,2,3,4', 'host_tx_ready': 'true', 'admin_status': 'up'}
BLOCKING_ERROR = '|'.join([SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING, SfpBase.SFP_ERROR_DESCRIPTION_I2C_STUCK])   # an error event's row


def make_sff_task(namespaces, reg, sfps):
    """SFF manager whose chassis serves the sfp mocks in `sfps` ({pport: mock})."""
    chassis = MagicMock()
    chassis.get_sfp = MagicMock(side_effect=lambda pport: sfps[pport])
    with pin_multi_asic(namespaces), patch('xcvrd.sff_mgr.XcvrTableHelper', MagicMock()):
        task = SffManagerTask(namespaces, threading.Event(), chassis, MagicMock(), asic_readiness=reg)
    task.status_table = Table("STATE_DB", "TRANSCEIVER_STATUS_SW")               # the rows the ports' events write
    task.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=task.status_table)
    return task, chassis


def sff_api(copper=False, tx_disable_support=True, tx_disable=(True, True, True, True)):
    api = MagicMock()
    api.is_copper = MagicMock(return_value=copper)
    api.get_tx_disable_support = MagicMock(return_value=tx_disable_support)
    api.get_lpmode_support = MagicMock(return_value=False)
    api.get_power_class = MagicMock(return_value=1)
    api.get_tx_disable = MagicMock(return_value=list(tx_disable))
    api.tx_disable_channel = MagicMock(return_value=True)
    return api


def sff_sfp(api, present=True):
    sfp = MagicMock()
    sfp.get_presence = MagicMock(return_value=present)
    sfp.get_xcvr_api = MagicMock(return_value=api)
    return sfp


def sff_iteration(task, has_event, visited_ports):
    """One SFF task_worker() iteration; `has_event` is what the observer reports."""
    observer_cls = MagicMock()
    observer_cls.return_value.handle_port_update_event = MagicMock(return_value=has_event)
    with patch('xcvrd.sff_mgr.PortChangeObserver', observer_cls), \
            patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(return_value=False)):
        task.task_stopping_event.is_set = MagicMock(side_effect=[False] * (1 + visited_ports) + [True])
        task.task_worker()


class TestSffGate:

    def test_sff_task_defers_ports_of_pending_namespace(self):
        reg = registry(['asic0'], TWO_NS)
        api = sff_api()
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(api)})
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        # iteration 1: the event is consumed, the port is deferred without module access
        sff_iteration(task, has_event=True, visited_ports=1)
        assert chassis.get_sfp.call_count == 0 and task.deferred_ports == {'Ethernet8'}
        assert 'Ethernet8' not in task.port_dict_prev
        # iteration 2: no event, namespace still pending -> nothing happens
        sff_iteration(task, has_event=False, visited_ports=0)
        assert chassis.get_sfp.call_count == 0 and task.deferred_ports == {'Ethernet8'}
        # iteration 3: namespace ready, no observer event -> processed exactly once
        reg.mark_ready('asic1')
        sff_iteration(task, has_event=False, visited_ports=1)
        assert chassis.get_sfp.call_count == 1 and api.tx_disable_channel.call_count == 1
        assert task.deferred_ports == set() and 'Ethernet8' in task.port_dict_prev
        # iteration 4: nothing deferred, no event -> idle
        sff_iteration(task, has_event=False, visited_ports=0)
        assert api.tx_disable_channel.call_count == 1

    def test_sff_task_waits_for_the_publishing_workers_pass(self):
        reg = registry(['asic0'], TWO_NS)
        reg.register_publisher('SfpStateUpdateTask', {'asic0'})
        api = sff_api()
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(api)})
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        reg.mark_ready('asic1')                                                # the marker alone opens nothing
        sff_iteration(task, has_event=True, visited_ports=1)
        sff_iteration(task, has_event=False, visited_ports=0)
        assert chassis.get_sfp.call_count == 0 and task.deferred_ports == {'Ethernet8'}
        reg.mark_admitted('SfpStateUpdateTask', 'asic1')                      # the publishing worker's pass is over
        sff_iteration(task, has_event=False, visited_ports=1)
        assert chassis.get_sfp.call_count == 1 and api.tx_disable_channel.call_count == 1 and task.deferred_ports == set()

    def test_sff_task_deferred_port_change_detected_once(self):
        reg = registry(['asic0'], TWO_NS)
        api = sff_api(tx_disable=(False, False, False, False))                  # TX currently enabled
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(api)})
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        sff_iteration(task, has_event=True, visited_ports=1)
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, {'host_tx_ready': 'false'}))
        sff_iteration(task, has_event=True, visited_ports=1)                     # still pending: deferred again
        assert chassis.get_sfp.call_count == 0 and task.deferred_ports == {'Ethernet8'}
        reg.mark_ready('asic1')
        sff_iteration(task, has_event=False, visited_ports=1)
        assert api.tx_disable_channel.call_count == 1
        assert api.tx_disable_channel.call_args.args[1] is True                    # acted on the final value: TX disabled
        sff_iteration(task, has_event=False, visited_ports=0)
        sff_iteration(task, has_event=True, visited_ports=1)                     # an unrelated event: no change detected
        assert api.tx_disable_channel.call_count == 1

    def test_sff_task_deferred_removed_on_config_del(self):
        reg = registry(['asic0'], TWO_NS)
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(sff_api())})
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        sff_iteration(task, has_event=True, visited_ports=1)
        assert task.deferred_ports == {'Ethernet8'}
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_DEL, {}, 'CONFIG_DB', 'PORT'))
        assert 'Ethernet8' not in task.port_dict
        sff_iteration(task, has_event=True, visited_ports=0)
        assert task.deferred_ports == set() and chassis.get_sfp.call_count == 0

    def test_sff_task_deferred_removed_regardless_of_branch(self):
        ports = [('Ethernet0', 10, 1), ('Ethernet4', 11, 1), ('Ethernet8', 12, 1), ('Ethernet12', 13, 1)]
        cmis_api = sff_api()
        sfps = {10: sff_sfp(sff_api(), present=False),                        # absent after readiness
                11: sff_sfp(sff_api(copper=True)),                            # copper
                12: sff_sfp(sff_api(tx_disable_support=False)),               # no tx-disable support
                13: sff_sfp(cmis_api)}                                        # CMIS module (not SFF's business)
        reg = registry([], ['asic1'])
        task, chassis = make_sff_task(['asic1'], reg, sfps)
        for lport, pport, asic in ports:
            task.on_port_update_event(PortChangeEvent(lport, pport, asic, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        sff_iteration(task, has_event=True, visited_ports=4)
        assert task.deferred_ports == {name for name, _, _ in ports} and chassis.get_sfp.call_count == 0
        reg.mark_ready('asic1')
        with patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(side_effect=lambda api: api is cmis_api)):
            observer_cls = MagicMock()
            observer_cls.return_value.handle_port_update_event = MagicMock(return_value=False)
            with patch('xcvrd.sff_mgr.PortChangeObserver', observer_cls):
                task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, False, False, False, True])
                task.task_worker()
        assert chassis.get_sfp.call_count == 4 and task.deferred_ports == set()
        for sfp in sfps.values():
            assert sfp.get_xcvr_api.return_value.tx_disable_channel.call_count == 0
        sff_iteration(task, has_event=False, visited_ports=0)                    # no timed re-processing
        assert chassis.get_sfp.call_count == 4

    def test_sff_task_ready_namespace_unchanged(self):
        reg = registry(TWO_NS, TWO_NS)
        api = sff_api()
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(api)})
        task.get_host_tx_status = MagicMock(return_value='true')
        task.get_admin_status = MagicMock(return_value='up')
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, {'type': 'QSFP28', 'subport': '0', 'lanes': '1,2,3,4'}))
        sff_iteration(task, has_event=True, visited_ports=1)                     # insertion: TX enabled
        assert api.tx_disable_channel.call_count == 1 and task.deferred_ports == set()
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, {'host_tx_ready': 'false'}))
        api.get_tx_disable = MagicMock(return_value=[False, False, False, False])
        sff_iteration(task, has_event=True, visited_ports=1)                     # host_tx_ready change: TX disabled
        assert api.tx_disable_channel.call_count == 2
        sff_iteration(task, has_event=True, visited_ports=1)                     # no change
        assert api.tx_disable_channel.call_count == 2 and task.port_dict == task.port_dict_prev

    def test_sff_task_logs_first_deferral_once_per_namespace(self):
        reg = registry([], TWO_NS)
        task, chassis = make_sff_task(TWO_NS, reg, {1: sff_sfp(sff_api()), 2: sff_sfp(sff_api())})
        task.helper_logger = recorder()
        for lport, pport, asic in TWO_NS_PORTS:
            task.on_port_update_event(PortChangeEvent(lport, pport, asic, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        sff_iteration(task, has_event=True, visited_ports=2)
        sff_iteration(task, has_event=True, visited_ports=2)                     # deferred again: no new notice
        for ns in TWO_NS:
            reg.mark_ready(ns)
        sff_iteration(task, has_event=False, visited_ports=2)
        assert [line.split(': ', 1)[1] for line in task.helper_logger.lines('PORTS_DEFERRED')] == [
            '[asic0] PORTS_DEFERRED (SffManagerTask): namespace pending, first deferred port Ethernet0',
            '[asic1] PORTS_DEFERRED (SffManagerTask): namespace pending, first deferred port Ethernet8']


    def test_sff_task_defers_a_port_behind_a_blocking_error(self):
        reg = registry(TWO_NS, TWO_NS)
        api = sff_api()
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(api)})
        task.status_table.set('Ethernet8', [('status', '1'), ('error', BLOCKING_ERROR)])
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        sff_iteration(task, has_event=True, visited_ports=1)
        assert chassis.get_sfp.call_count == 0 and task.deferred_ports == {'Ethernet8'} and 'Ethernet8' not in task.port_dict_prev
        sff_iteration(task, has_event=False, visited_ports=1)                     # revisited without an event, still behind the error
        assert chassis.get_sfp.call_count == 0 and task.deferred_ports == {'Ethernet8'}
        task.status_table.set('Ethernet8', [('status', '1'), ('error', 'N/A')])   # the error ended (the clearing insertion's row, no observer event)
        sff_iteration(task, has_event=False, visited_ports=1)
        assert chassis.get_sfp.call_count == 1 and api.tx_disable_channel.call_count == 1
        assert task.deferred_ports == set() and 'Ethernet8' in task.port_dict_prev

    def test_sff_task_blocking_error_on_a_processed_port_changes_nothing(self):
        reg = registry(TWO_NS, TWO_NS)
        api = sff_api()
        task, chassis = make_sff_task(TWO_NS, reg, {2: sff_sfp(api)})
        task.on_port_update_event(PortChangeEvent('Ethernet8', 2, 1, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
        sff_iteration(task, has_event=True, visited_ports=1)
        assert chassis.get_sfp.call_count == 1 and api.tx_disable_channel.call_count == 1
        task.status_table.set('Ethernet8', [('status', '1'), ('error', BLOCKING_ERROR)])
        sff_iteration(task, has_event=True, visited_ports=1)                      # an unrelated event during the error: no module read
        assert chassis.get_sfp.call_count == 1 and task.deferred_ports == {'Ethernet8'}
        assert task.port_dict_prev['Ethernet8']['type'] == 'QSFP28'               # the snapshot keeps the type the module was processed with
        task.status_table.set('Ethernet8', [('status', '1'), ('error', 'N/A')])
        sff_iteration(task, has_event=False, visited_ports=1)
        assert chassis.get_sfp.call_count == 2 and api.tx_disable_channel.call_count == 1 and task.deferred_ports == set()


DOM_PATCHES = [
    ('xcvrd.dom.dom_mgr.XcvrTableHelper', MagicMock()),
    ('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status', MagicMock(return_value=False)),
    ('xcvrd.xcvrd_utilities.port_event_helper.PortChangeObserver', MagicMock()),
    ('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None))),
    ('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock()),
]


def make_dom_task(namespaces, port_mapping, obj_dict, reg=None):
    with pin_multi_asic(namespaces), patch('xcvrd.dom.dom_mgr.XcvrTableHelper', MagicMock()):
        kwargs = {'asic_readiness': reg} if reg is not None else {}
        task = DomInfoUpdateTask(namespaces, port_mapping, obj_dict, threading.Event(), True, 0, **kwargs)
    budget = {'checks': 0}

    def bounded_check(*args, **kwargs):                    # a regression that never reaches the sentinel port must not hang
        budget['checks'] += 1
        if budget['checks'] > 50:
            task.task_stopping_event.set()

    task.check_port_update = MagicMock(side_effect=bounded_check)
    task.is_port_dom_monitoring_disabled = MagicMock(return_value=False)
    task.dom_db_utils = MagicMock()
    task.status_db_utils = MagicMock()
    task.vdm_db_utils = MagicMock()
    task.vdm_utils = MagicMock()
    task.vdm_utils.is_transceiver_vdm_supported = MagicMock(return_value=False)
    task.xcvrd_utils = MagicMock()
    return task


@contextlib.contextmanager
def dom_patches(presence_calls):
    with contextlib.ExitStack() as stack:
        for target, value in DOM_PATCHES:
            stack.enter_context(patch(target, value))
        stack.enter_context(patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence',
                                  MagicMock(side_effect=lambda pport: presence_calls.append(pport) or True)))
        yield


def dom_pass(task, stop_on_lport):
    """One periodic DOM pass; the firmware step of `stop_on_lport` ends the loop."""
    visited = []

    def firmware(lport, *args, **kwargs):
        visited.append(lport)
        if lport == stop_on_lport:
            task.task_stopping_event.set()

    task.post_port_sfp_firmware_info_to_db = MagicMock(side_effect=firmware)
    task.task_stopping_event.clear()
    task.task_worker()
    return visited


class TestDomGate:

    def test_dom_task_skips_ports_of_pending_namespace(self):
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet8', 2, 1), ('Ethernet0', 1, 0)])           # the pending port is visited first
        task = make_dom_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)
        presence = []
        with dom_patches(presence):
            assert dom_pass(task, stop_on_lport='Ethernet0') == ['Ethernet0']
        assert presence == [1]                                              # no module access for the pending port
        assert [c.args[0] for c in task.is_port_dom_monitoring_disabled.call_args_list] == ['Ethernet0']   # no CONFIG_DB read either
        reg.mark_ready('asic1')
        presence.clear()
        with dom_patches(presence):
            assert dom_pass(task, stop_on_lport='Ethernet0') == ['Ethernet8', 'Ethernet0']   # next pass, no new event
        assert presence == [2, 1]

    def test_dom_task_link_change_skips_pending_namespace(self):
        reg = registry(['asic0'], TWO_NS)
        task = make_dom_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        with patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status', MagicMock(return_value=False)) as error_check:
            task.update_port_db_diagnostics_on_link_change(2)
            assert error_check.call_count == 0 and task.xcvrd_utils.get_transceiver_presence.call_count == 0
            assert task.is_port_dom_monitoring_disabled.call_count == 0
            reg.mark_ready('asic1')
            task.update_port_db_diagnostics_on_link_change(2)
            assert error_check.call_count == 1 and task.xcvrd_utils.get_transceiver_presence.call_count == 1

    def test_dom_task_reads_a_ready_namespace_before_its_admission(self):
        # the DOM worker waits for the marker only: it makes no sticky decision from a read, its periodic pass reads
        # the port again, so it does not wait for the publishing workers' pass
        reg = registry(['asic0'], TWO_NS)
        reg.register_publisher('SfpStateUpdateTask', {'asic0'})
        pm = mapping([('Ethernet8', 2, 1), ('Ethernet0', 1, 0)])
        task = make_dom_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)
        reg.mark_ready('asic1')
        assert not reg.is_asic_admitted(1)
        presence = []
        with dom_patches(presence):
            assert dom_pass(task, stop_on_lport='Ethernet0') == ['Ethernet8', 'Ethernet0']
        assert presence == [2, 1]

    def test_dom_task_default_registry(self):
        pm = mapping([('Ethernet8', 2, 1), ('Ethernet0', 1, 0)])
        task = make_dom_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()})       # no asic_readiness argument
        presence = []
        with dom_patches(presence):
            assert dom_pass(task, stop_on_lport='Ethernet0') == ['Ethernet8', 'Ethernet0']

    def test_dom_task_logs_first_deferral_once_per_namespace(self):
        reg = registry(['asic0'], TWO_NS)
        pm = mapping([('Ethernet8', 2, 1), ('Ethernet0', 1, 0)])
        task = make_dom_task(TWO_NS, pm, {1: MagicMock(), 2: MagicMock()}, reg)
        task.helper_logger = recorder()
        presence = []
        with dom_patches(presence):
            dom_pass(task, stop_on_lport='Ethernet0')
            dom_pass(task, stop_on_lport='Ethernet0')                      # second pass: no second notice
        task.update_port_db_diagnostics_on_link_change(2)                  # the link-change path shares the set
        reg.mark_ready('asic1')
        with dom_patches(presence):
            dom_pass(task, stop_on_lport='Ethernet0')
        assert task.helper_logger.lines('PORTS_DEFERRED') == ['[asic1] PORTS_DEFERRED (DomInfoUpdateTask): namespace pending, first deferred port Ethernet8']

    def test_thermal_task_covers_all_asics_ungated(self):
        def thermal_pass(port_mapping):
            with pin_multi_asic(TWO_NS), patch('xcvrd.dom.dom_mgr.XcvrTableHelper', MagicMock()):
                task = DomThermalInfoUpdateTask(TWO_NS, port_mapping, {1: MagicMock(), 2: MagicMock()}, threading.Event(), 0)
            # the constructor takes no registry; a gate could only ever consult the inherited attribute
            task.asic_readiness = registry(['asic0'], TWO_NS)
            task.is_port_dom_monitoring_disabled = MagicMock(return_value=False)
            task.dom_db_utils = MagicMock()
            polled = []

            def temperature(lport):
                polled.append(lport)
                if lport == 'Ethernet0':
                    task.task_stopping_event.set()

            task.dom_db_utils.post_port_dom_temperature_info_to_db = MagicMock(side_effect=temperature)
            with patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status', MagicMock(return_value=False)), \
                    patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True)):
                task.task_worker()
            return polled

        assert thermal_pass(mapping([('Ethernet8', 2, 1), ('Ethernet0', 1, 0)])) == ['Ethernet8', 'Ethernet0']   # pending asic1 polled too
        assert thermal_pass(mapping([('Ethernet0', 1, 0)])) == ['Ethernet0']                                    # negative control


class TestRunWiring:

    @patch('xcvrd.xcvrd.platform_chassis', MagicMock(), create=True)     # deinit() of another test may have deleted the global
    @patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock())
    @patch('xcvrd.xcvrd.DaemonXcvrd.init')
    def test_run_passes_registry_to_workers(self, mock_init):
        mock_init.return_value = PortMapping()
        names = ['SffManagerTask', 'CmisManagerTask', 'CpoManagerTask', 'DomInfoUpdateTask',
                 'CpoDomInfoUpdateTask', 'DomThermalInfoUpdateTask', 'SfpStateUpdateTask']
        with contextlib.ExitStack() as stack:
            mocks = {name: stack.enter_context(patch('xcvrd.xcvrd.' + name)) for name in names}
            cpo_state = stack.enter_context(patch('xcvrd.cpo.cpo_state_task.CpoStateUpdateTask'))
            daemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
            daemon.enable_sff_mgr = True
            daemon.dom_temperature_poll_interval = 10
            daemon.sfp_obj_dict = {1: MagicMock()}
            daemon.cpo_obj_dict = {2: MagicMock()}
            daemon.stop_event.wait = MagicMock()
            daemon.asic_readiness = reg = registry([''], [''])             # init() is mocked out
            daemon.readiness_thread = MagicMock(**{'is_alive.return_value': False})
            daemon.run()
        assert isinstance(reg, AsicReadiness)
        for name in ('SffManagerTask', 'CmisManagerTask', 'CpoManagerTask', 'DomInfoUpdateTask', 'CpoDomInfoUpdateTask', 'SfpStateUpdateTask'):
            assert mocks[name].call_args.kwargs.get('asic_readiness') is reg, name
        assert cpo_state.call_args.kwargs.get('asic_readiness') is reg
        assert 'asic_readiness' not in mocks['DomThermalInfoUpdateTask'].call_args.kwargs      # thermal is not gated


# ---------------------------------------------------------------------------
# Readiness checker, init() and run() lifecycle
# ---------------------------------------------------------------------------

SEL_OBJECT, SEL_TIMEOUT, SEL_ERROR = REAL_SELECT.OBJECT, REAL_SELECT.TIMEOUT, REAL_SELECT.ERROR
SET, DEL = swsscommon.SET_COMMAND, swsscommon.DEL_COMMAND


class FakeSubscriberTable:
    """SubscriberStateTable stand-in: pop() serves queued (key, op, fvs) entries, ('', '', ()) when empty."""

    def __init__(self, entries=()):
        self.entries = collections.deque(entries)
        self.pop_count = 0

    def push(self, key, op=SET):
        self.entries.append((key, op, ()))

    def pop(self):
        self.pop_count += 1
        return self.entries.popleft() if self.entries else ('', '', ())

    def hasCachedData(self):
        return len(self.entries) > 1


def marker_rows(n, marker=None, trailing=0, prefix='Ethernet'):
    rows = [('{}{}'.format(prefix, 4 * i), SET, ()) for i in range(n)]
    if marker:
        rows.append((marker, SET, ()))
    rows += [('{}{}'.format(prefix, 4 * (n + i)), SET, ()) for i in range(trailing)]
    return rows


class FakeSelect:
    """swsscommon.Select stand-in with a finite termination contract.

    script: iterable of (state, selectable) results or zero-argument callables returning one (hooks).
    When the script is exhausted, `on_exhausted='stop'` sets daemon.stop_event and returns TIMEOUT
    (synchronous checker runs always terminate); 'timeout' keeps returning TIMEOUT. `budget` caps the
    number of select() calls (sets stop_event afterwards). `rearm=True` makes a registered table with
    queued entries return OBJECT immediately, like the real Select with cached data. Tables are tracked
    by id / weakref only, so the fixture never keeps a table alive.
    """
    OBJECT, TIMEOUT, ERROR = SEL_OBJECT, SEL_TIMEOUT, SEL_ERROR

    def __init__(self, script=(), budget=None, on_exhausted='stop', rearm=False, traffic=None):
        self.script = collections.deque(script)
        self.budget, self.on_exhausted, self.rearm = budget, on_exhausted, rearm
        self.traffic = traffic                             # callable run at the start of every select(): writes by other daemons
        self.daemon = None
        self.call_count = 0
        self.object_results = 0
        self.registered = {}                       # id(table) -> weakref
        self.added, self.removed = [], []           # ids, in order

    def addSelectable(self, tbl):
        if id(tbl) in self.registered:
            raise RuntimeError("table added twice")
        self.registered[id(tbl)] = weakref.ref(tbl)
        self.added.append(id(tbl))

    def removeSelectable(self, tbl):
        if id(tbl) not in self.registered:
            raise RuntimeError("table removed but never added")
        del self.registered[id(tbl)]
        self.removed.append(id(tbl))

    def _stop(self):
        if self.daemon is not None:
            self.daemon.stop_event.set()

    def select(self, timeout_ms):
        self.call_count += 1
        if self.traffic is not None:
            self.traffic()
        if self.budget is not None and self.call_count > self.budget:
            self._stop()
            return (SEL_TIMEOUT, None)
        if self.rearm:
            for ref in list(self.registered.values()):
                tbl = ref()
                if tbl is not None and tbl.entries:
                    self.object_results += 1
                    return (SEL_OBJECT, tbl)
        while self.script:
            item = self.script.popleft()
            result = item() if callable(item) else item
            if result is not None:
                if result[0] == SEL_OBJECT:
                    self.object_results += 1
                return result
        if self.on_exhausted == 'stop':
            self._stop()
        return (SEL_TIMEOUT, None)


def make_daemon(namespaces):
    daemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
    daemon.namespaces = list(namespaces)
    daemon.asic_readiness = registry([], namespaces)
    daemon.log = recorder()
    for level in ('log_notice', 'log_warning', 'log_error', 'log_info', 'log_debug'):
        setattr(daemon, level, getattr(daemon.log, level))
    return daemon


@contextlib.contextmanager
def patch_checker_env(daemon, tables_by_ns, select):
    """Patch Select / SubscriberStateTable / db_connect for one checker run; hands out tables in namespace order
    without keeping strong references to them."""
    select.daemon = daemon
    select_cls = MagicMock(return_value=select)
    select_cls.OBJECT, select_cls.TIMEOUT, select_cls.ERROR = SEL_OBJECT, SEL_TIMEOUT, SEL_ERROR
    pending_tables = collections.deque(tables_by_ns[ns] for ns in daemon.namespaces)
    del tables_by_ns                                       # the generator frame must not keep the tables alive
    env = {'table_calls': 0, 'db_calls': 0, 'connectors': [], 'on_connect': None}

    def connect(db_name, namespace=None):                  # plain functions: a MagicMock would record (and keep alive) its arguments
        env['db_calls'] += 1
        if env['on_connect']:
            env['on_connect'](namespace)
        conn = MagicMock(name='conn-{}'.format(namespace))
        env['connectors'].append(weakref.ref(conn))
        return conn

    def make_table(db, name):
        env['table_calls'] += 1
        return pending_tables.popleft()

    with patch('xcvrd.xcvrd.swsscommon.Select', select_cls), \
            patch('xcvrd.xcvrd.swsscommon.SubscriberStateTable', new=make_table), \
            patch('xcvrd.xcvrd.daemon_base.db_connect', new=connect):
        yield env


def drain(daemon):
    return daemon._drain_ready_queue()


def stub_checker(daemon, items=(), delays=(), scan_done=True):
    """Replace the checker by a stub that queues `items` (with optional per-item delays) then sets initial_scan_done."""
    def checker():
        for i, item in enumerate(items):
            if i < len(delays):
                time.sleep(delays[i])
            daemon.ready_asic_queue.put(item)
        if scan_done:
            daemon.initial_scan_done.set()
    daemon._asic_readiness_checker = checker


def registration_event(daemon):
    """Event set once the real on_asic_ready() has registered a namespace (ASIC_ACTIVE is logged before the registry changes)."""
    registered = threading.Event()
    real_on_asic_ready = daemon.on_asic_ready

    def on_asic_ready(namespace):
        real_on_asic_ready(namespace)
        registered.set()
    daemon.on_asic_ready = on_asic_ready
    return registered


class FakeWorker:
    """A worker thread stand-in for run() lifecycle tests."""

    def __init__(self, name, alive=True, join_error=None):
        self.name = name
        self.task_stopping_event = threading.Event()
        self._alive, self.join_error = alive, join_error
        self.started = False
        self.events = []

    def start(self):
        self.started = True
        self.events.append('start')

    def is_alive(self):
        return self._alive

    def raise_exception(self):
        self.events.append('interrupt')

    def join(self):
        self.events.append('join')
        if self.join_error:
            raise self.join_error


@contextlib.contextmanager
def init_env(namespaces, port_mapping=None, sfp=None, cpo=None):
    """Everything init() touches apart from the checker: platform, DB helper, settings, builders."""
    with pin_multi_asic(namespaces), \
            patch('xcvrd.xcvrd.multi_asic.get_front_end_namespaces', MagicMock(return_value=list(namespaces))), \
            patch('xcvrd.xcvrd.DaemonXcvrd.load_platform_util', MagicMock()), \
            patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock()), \
            patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=port_mapping if port_mapping is not None else PortMapping())) as get_mapping, \
            patch('xcvrd.xcvrd.common.get_pluggable_obj_dict', MagicMock(return_value=sfp if sfp is not None else {})) as get_sfp, \
            patch('xcvrd.xcvrd.common.get_cpo_obj_dict', MagicMock(return_value=cpo if cpo is not None else {})) as get_cpo, \
            patch('xcvrd.xcvrd.common.is_fast_reboot_enabled', MagicMock(return_value=False)), \
            patch('xcvrd.xcvrd.media_settings_parser.load_media_settings', MagicMock()), \
            patch('xcvrd.xcvrd.optics_si_parser.load_optics_si_settings', MagicMock()):
        yield {'get_mapping': get_mapping, 'get_sfp': get_sfp, 'get_cpo': get_cpo}


def bounded_init(daemon, deadline):
    """Run daemon.init() in a thread with an independent deadline. Returns (('returned', value) | ('timeout', None), elapsed).
    On timeout the daemon is asked to stop so that the thread does not outlive the test."""
    box = {}

    def target():
        box['value'] = daemon.init()
    thread = threading.Thread(target=target)
    t0 = time.monotonic()
    thread.start()
    thread.join(deadline)
    elapsed = time.monotonic() - t0
    if thread.is_alive():
        daemon.stop_event.set()
        thread.join(5)
        return ('timeout', None), elapsed
    return ('returned', box.get('value')), elapsed


def init_daemon(namespaces):
    daemon = make_daemon(namespaces)
    daemon.asic_readiness = None                                 # init() builds it
    daemon.initialize_port_init_control_fields_in_port_table = MagicMock()
    daemon.remove_stale_transceiver_info = MagicMock()
    return daemon


class TestChecker:

    def test_checker_two_namespaces_ready_in_order(self):
        for order in (TWO_NS, list(reversed(TWO_NS))):
            daemon = make_daemon(order)
            tables = {'asic0': FakeSubscriberTable(marker_rows(8, 'PortConfigDone', trailing=3)),
                      'asic1': FakeSubscriberTable(marker_rows(5))}
            refs = {ns: weakref.ref(t) for ns, t in tables.items()}
            ids = {ns: id(t) for ns, t in tables.items()}           # plain ints: safe to keep after the tables die
            select = FakeSelect([(SEL_OBJECT, tables['asic0']), (SEL_TIMEOUT, None)], on_exhausted='timeout')
            b_marker, parked = threading.Event(), threading.Event()

            def late_marker():
                parked.set()                                          # the checker is now inside its third select()
                b_marker.wait(5)
                tables_b = refs['asic1']()
                tables_b.push('PortInitDone')
                return (SEL_OBJECT, tables_b)
            select.script.append(late_marker)
            a_connector = None
            with patch_checker_env(daemon, tables, select) as env:
                del tables
                thread = threading.Thread(target=daemon._asic_readiness_checker)
                thread.start()
                try:
                    assert parked.wait(5)
                    assert daemon.log.lines('ASIC_READY') and '[asic0]' in daemon.log.lines('ASIC_READY')[0]
                    gc.collect()
                    assert refs['asic0']() is None                # A's table released while B is pending
                    assert refs['asic1']() is not None
                    a_connector = env['connectors'][order.index('asic0')]
                    assert a_connector() is None                  # A's connector released as well
                    b_marker.set()
                    thread.join(5)
                    assert not thread.is_alive()
                finally:
                    b_marker.set()                                # unconditional teardown, whatever failed above
                    daemon.stop_event.set()
                    thread.join(5)
                gc.collect()
                assert refs['asic1']() is None
            assert drain(daemon) == ['asic0', 'asic1']
            assert sorted(select.added) == sorted(ids.values())      # both subscribed (namespace order) ...
            assert select.removed == [ids['asic0'], ids['asic1']]    # ... each removed exactly once, in ready order
            ready_lines = daemon.log.lines('ASIC_READY')
            assert len(ready_lines) == 2 and 'PortConfigDone' in ready_lines[0] and 'PortInitDone' in ready_lines[1]
            assert daemon.initial_scan_done.is_set()

    def test_checker_initial_scan_registers_all_ready(self):
        # (a) both markers in the snapshot: both queued BEFORE initial_scan_done is set
        daemon = make_daemon(TWO_NS)
        tables = {'asic0': FakeSubscriberTable(marker_rows(2, 'PortConfigDone')), 'asic1': FakeSubscriberTable(marker_rows(2, 'PortInitDone'))}
        scan_done_at_put = []
        real_put = daemon.ready_asic_queue.put
        daemon.ready_asic_queue.put = lambda item: (scan_done_at_put.append(daemon.initial_scan_done.is_set()), real_put(item))[1]
        select = FakeSelect([(SEL_OBJECT, tables['asic0'])])
        with patch_checker_env(daemon, tables, select):
            daemon._asic_readiness_checker()
        assert drain(daemon) == ['asic0', 'asic1'] and daemon.initial_scan_done.is_set()
        assert scan_done_at_put == [False, False]                   # the sweep completes before the event is set
        # (b) no markers: initial_scan_done is not set when the first select() is entered, set after its TIMEOUT
        daemon = make_daemon(TWO_NS)
        tables = {'asic0': FakeSubscriberTable(marker_rows(2)), 'asic1': FakeSubscriberTable(marker_rows(2))}
        scan_done_at_select = []
        select = FakeSelect([lambda: (scan_done_at_select.append(daemon.initial_scan_done.is_set()), (SEL_TIMEOUT, None))[1]])
        with patch_checker_env(daemon, tables, select):
            daemon._asic_readiness_checker()
        assert drain(daemon) == [] and daemon.initial_scan_done.is_set() and scan_done_at_select == [False]
        # (c) B's marker arrives after B's table was drained in the first sweep: found in the second sweep
        daemon = make_daemon(TWO_NS)
        tables = {'asic0': FakeSubscriberTable(marker_rows(2, 'PortConfigDone')), 'asic1': FakeSubscriberTable(marker_rows(2))}
        scan_state = []

        def second_sweep():
            scan_state.append(daemon.initial_scan_done.is_set())
            tables['asic1'].push('PortConfigDone')
            return (SEL_OBJECT, tables['asic1'])
        select = FakeSelect([(SEL_OBJECT, tables['asic0']), second_sweep])
        with patch_checker_env(daemon, tables, select):
            daemon._asic_readiness_checker()
        assert drain(daemon) == ['asic0', 'asic1'] and scan_state == [True]

    def test_checker_staggered_readiness(self):
        def run(remove_selectable_works):
            daemon = make_daemon(TWO_NS)
            tables = {'asic0': FakeSubscriberTable(marker_rows(2, 'PortConfigDone')), 'asic1': FakeSubscriberTable(marker_rows(2))}
            # A's PORT_TABLE keeps being written (flap_count, NPU SI fields ...) on every tick, like a healthy namespace
            select = FakeSelect([(SEL_TIMEOUT, None)] * 3, budget=6, on_exhausted='timeout',
                                rearm=True, traffic=lambda: tables['asic0'].push('Ethernet0', SET))    # re-arm delivers A's marker
            if not remove_selectable_works:
                select.removeSelectable = lambda tbl: None          # mutation control: A stays registered in the Select
            with patch_checker_env(daemon, tables, select):
                daemon._asic_readiness_checker()
            return select.call_count, select.object_results, drain(daemon)

        calls, objects, queued = run(remove_selectable_works=True)
        assert queued == ['asic0'] and calls == 7                  # budget of 6 ticks + the one that sets stop_event
        assert objects == 1                                         # A woke the Select once (its marker); its later traffic is not seen
        calls_bad, objects_bad, _ = run(remove_selectable_works=False)
        assert calls_bad == 7 and objects_bad == 6                 # negative control: A's traffic re-arms the Select on every tick (spin)

    @pytest.mark.parametrize("rows, expect_ready, expect_key", [
        ([('PortInitDone', DEL, ())], False, None),
        ([('PortInitDone', SET, ())], True, 'PortInitDone'),
        ([('PortConfigDone', SET, ()), ('PortConfigDone', DEL, ())], True, 'PortConfigDone'),
        ([('PortConfigDone', SET, ()), ('PortInitDone', SET, ()), ('PortConfigDone', SET, ())], True, 'PortConfigDone'),
        ([('Ethernet0', SET, ())], False, None),
    ])
    def test_checker_marker_semantics(self, rows, expect_ready, expect_key):
        daemon = make_daemon(['asic0'])
        tables = {'asic0': FakeSubscriberTable(rows)}
        select = FakeSelect([(SEL_OBJECT, tables['asic0'])])
        with patch_checker_env(daemon, tables, select):
            daemon._asic_readiness_checker()
        queued = drain(daemon)
        assert queued == (['asic0'] if expect_ready else [])
        if expect_ready:
            assert len(daemon.log.lines('ASIC_READY')) == 1 and expect_key in daemon.log.lines('ASIC_READY')[0]

    def test_checker_never_ready_namespace_idles(self):
        daemon = make_daemon(TWO_NS)
        tables = {'asic0': FakeSubscriberTable(marker_rows(2, 'PortConfigDone')), 'asic1': FakeSubscriberTable(marker_rows(3))}
        select = FakeSelect([(SEL_OBJECT, tables['asic0'])], budget=6, on_exhausted='timeout')
        with patch_checker_env(daemon, tables, select):
            daemon._asic_readiness_checker()
        assert drain(daemon) == ['asic0']
        assert select.call_count == 7                               # budget of 6 ticks + the one that set stop_event
        assert tables['asic1'].pop_count == 4                       # B drained in the first sweep only (3 rows + empty pop)
        assert daemon.stop_event.is_set() and daemon.initial_scan_done.is_set()

    def test_checker_heartbeat_independent_of_traffic(self):
        for state in (SEL_TIMEOUT, SEL_OBJECT):
            daemon = make_daemon(['asic1'])
            tables = {'asic1': FakeSubscriberTable()}
            clock = {'now': 1000.0}

            def tick():
                clock['now'] += 100.0                                # 7 ticks = 700 simulated seconds
                if state == SEL_OBJECT:
                    tables['asic1'].push('Ethernet0', SET)          # non-marker traffic
                    return (SEL_OBJECT, tables['asic1'])
                return (SEL_TIMEOUT, None)
            select = FakeSelect([tick] * 7)
            with patch_checker_env(daemon, tables, select), patch('xcvrd.xcvrd.time.monotonic', side_effect=lambda: clock['now']):
                daemon._asic_readiness_checker()
            beats = daemon.log.lines('ASIC_STILL_WAITING')
            assert len(beats) == 2 and all("['asic1']" in b for b in beats), beats

    def test_checker_exception_puts_sentinel(self):
        daemon = make_daemon(['asic0'])
        table = FakeSubscriberTable(marker_rows(1, 'PortConfigDone'))
        table.pop = MagicMock(side_effect=RuntimeError('pop failed'))
        select = FakeSelect([(SEL_OBJECT, table)])
        with patch_checker_env(daemon, {'asic0': table}, select):
            daemon._asic_readiness_checker()
        items = drain(daemon)
        assert len(items) == 1 and items[0] is ASIC_CHECKER_FAILED
        assert len(daemon.log.lines('ASIC_CHECKER_FAILED')) == 1 and 'pop failed' in daemon.log.lines('ASIC_CHECKER_FAILED')[0]
        assert daemon.initial_scan_done.is_set()
        daemon = make_daemon(['asic0'])
        table = FakeSubscriberTable(marker_rows(1, 'PortConfigDone'))
        select = FakeSelect([(SEL_OBJECT, table)])
        select.removeSelectable = MagicMock(side_effect=RuntimeError('epoll'))
        with patch_checker_env(daemon, {'asic0': table}, select):
            daemon._asic_readiness_checker()
        items = drain(daemon)
        assert len(items) == 1 and items[0] is ASIC_CHECKER_FAILED

    def test_checker_persistent_select_error_is_fatal(self):
        daemon = make_daemon(['asic1'])
        select = FakeSelect([(SEL_ERROR, None)] * ASIC_CHECKER_MAX_SELECT_ERRORS)
        with patch_checker_env(daemon, {'asic1': FakeSubscriberTable()}, select), patch.object(daemon.stop_event, 'wait', MagicMock(return_value=False)) as wait:
            daemon._asic_readiness_checker()                        # the post-error wait is mocked: no real one-second sleeps
        items = drain(daemon)
        assert len(items) == 1 and items[0] is ASIC_CHECKER_FAILED and len(daemon.log.lines('ASIC_CHECKER_FAILED')) == 1
        assert wait.call_args_list == [((1,),)] * (ASIC_CHECKER_MAX_SELECT_ERRORS - 1)   # the last error raises before waiting
        daemon = make_daemon(['asic1'])
        select = FakeSelect([(SEL_ERROR, None)] * 9 + [(SEL_TIMEOUT, None)] + [(SEL_ERROR, None)] * 9, budget=25)
        with patch_checker_env(daemon, {'asic1': FakeSubscriberTable()}, select), patch.object(daemon.stop_event, 'wait', MagicMock(return_value=False)) as wait:
            daemon._asic_readiness_checker()
        assert drain(daemon) == [] and wait.call_args_list == [((1,),)] * 18

    def test_checker_select_error_count_resets_on_object(self):
        daemon = make_daemon(['asic1'])
        tables = {'asic1': FakeSubscriberTable()}

        def traffic():
            tables['asic1'].push('Ethernet0', SET)                    # non-marker traffic: an OBJECT result without readiness
            return (SEL_OBJECT, tables['asic1'])
        select = FakeSelect([(SEL_ERROR, None)] * 9 + [traffic] + [(SEL_ERROR, None)] * 9, budget=25)
        with patch_checker_env(daemon, tables, select), patch.object(daemon.stop_event, 'wait', MagicMock(return_value=False)) as wait:
            daemon._asic_readiness_checker()
        assert drain(daemon) == [] and wait.call_args_list == [((1,),)] * 18 and daemon.log.lines('ASIC_CHECKER_FAILED') == []

    def test_checker_stop_during_subscription_and_drain(self):
        daemon = make_daemon(TWO_NS)
        select = FakeSelect()
        tables = {'asic0': FakeSubscriberTable(), 'asic1': FakeSubscriberTable()}
        with patch_checker_env(daemon, tables, select) as env:
            env['on_connect'] = lambda namespace: daemon.stop_event.set()
            daemon._asic_readiness_checker()
        assert env['table_calls'] == 1 and len(select.added) == 1 and select.call_count == 0
        assert drain(daemon) == [] and daemon.initial_scan_done.is_set()
        daemon = make_daemon(['asic0'])
        table = FakeSubscriberTable(marker_rows(1000))
        real_pop = table.pop

        def pop_then_stop():
            if table.pop_count == 3:
                daemon.stop_event.set()
            return real_pop()
        table.pop = pop_then_stop
        select = FakeSelect([(SEL_OBJECT, table)])
        with patch_checker_env(daemon, {'asic0': table}, select):
            daemon._asic_readiness_checker()
        assert table.pop_count <= 4

    def test_checker_thread_attributes(self):
        daemon = init_daemon(['asic0'])
        seen = {}

        def checker():
            seen['thread'] = threading.current_thread()
            daemon.ready_asic_queue.put('asic0')
            daemon.initial_scan_done.set()
        daemon._asic_readiness_checker = checker
        with init_env(['asic0']):
            daemon.init()
        assert daemon.readiness_thread is seen['thread']
        assert daemon.readiness_thread.name == 'AsicReadinessChecker' and daemon.readiness_thread.daemon is True


class TestInit:

    def test_init_returns_after_first_ready_asic(self):
        daemon = init_daemon(TWO_NS)
        order = []
        stub_checker(daemon, ['asic0'])
        pm = mapping(TWO_NS_PORTS)
        with init_env(TWO_NS, port_mapping=pm, sfp={1: MagicMock(), 2: MagicMock()}) as env:
            env['get_mapping'].side_effect = lambda ns: order.append('mapping') or pm
            result = daemon.init()
        assert result is pm
        assert env['get_mapping'].call_args.args[0] == TWO_NS and env['get_sfp'].call_count == 1 and env['get_cpo'].call_count == 1
        assert daemon.asic_readiness.ready_namespaces() == frozenset({'asic0'})
        notices = [c.args[0] for c in daemon.log.log_notice.call_args_list]
        assert [n for n in notices if 'ASIC_DISCOVERED' in n][0].startswith('ASIC_DISCOVERED: 2 namespace(s)')
        idx = {key: next(i for i, n in enumerate(notices) if key in n) for key in ('ASIC_DISCOVERED', 'XCVRD INIT: Wait for port config is done', 'ASIC_ACTIVE', 'XCVRD INIT: After port config is done', 'OBJ_DICT_BUILT')}
        assert idx['ASIC_DISCOVERED'] < idx['XCVRD INIT: Wait for port config is done'] < idx['ASIC_ACTIVE'] < idx['XCVRD INIT: After port config is done'] < idx['OBJ_DICT_BUILT']
        assert '[asic0] ASIC_ACTIVE: registering; 1/2 namespace(s) ready' in notices

    def test_init_registers_all_initially_ready_namespaces(self):
        daemon = init_daemon(TWO_NS)
        order = []
        registered = registration_event(daemon)

        def checker():                                              # asic0 at once, asic1 once init() has registered asic0, then the sweep completes
            daemon.ready_asic_queue.put('asic0')
            order.append('asic1' if registered.wait(5) else 'timeout')
            daemon.ready_asic_queue.put('asic1')
            order.append('scan_done')
            daemon.initial_scan_done.set()
        daemon._asic_readiness_checker = checker
        with init_env(TWO_NS) as env:
            env['get_mapping'].side_effect = lambda ns: order.append('mapping') or PortMapping()
            assert daemon.init() is not None
        assert order == ['asic1', 'scan_done', 'mapping']           # asic1 queued after asic0 was registered; the build waits for the first sweep
        assert daemon.asic_readiness.all_ready()
        actives = daemon.log.lines('ASIC_ACTIVE')
        assert len(actives) == 2 and '1/2' in actives[0] and '2/2' in actives[1]
        reg = daemon.asic_readiness
        task = make_sfp_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                patch_publishers('post_port_info_to_db', MagicMock(return_value=None)):
            worker_init(task, mapping(TWO_NS_PORTS))
        assert publisher.call_count == 2 and task._pending_namespaces == set()
        cmis = make_cmis_task(TWO_NS, mapping(TWO_NS_PORTS), {1: MagicMock(), 2: MagicMock()}, reg)
        assert cmis.asic_readiness.is_asic_ready(0) and cmis.asic_readiness.is_asic_ready(1)

    def test_init_initial_scan_wait_is_cancellable(self):
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0'], scan_done=False)
        threading.Timer(0.5, daemon.stop_event.set).start()
        with init_env(TWO_NS) as env:
            result, elapsed = bounded_init(daemon, 5.0)             # independent deadline: a missing cancellation check fails here
            assert result == ('returned', None) and elapsed < 3.0    # stop at 0.5 s, then at most one 1 s poll, with slack for a loaded host
        assert env['get_mapping'].call_count == 0 and env['get_sfp'].call_count == 0

    def test_init_blocks_until_first_ready(self):
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0'], delays=(0.5,))
        with init_env(TWO_NS):
            result, elapsed = bounded_init(daemon, 5.0)
            assert result[0] == 'returned' and result[1] is not None and elapsed >= 0.5

    def test_init_stops_promptly_when_stop_event_set(self):
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, [], scan_done=False)
        threading.Timer(0.3, daemon.stop_event.set).start()
        with init_env(TWO_NS):
            result, elapsed = bounded_init(daemon, 5.0)
            # stop at 0.3 s, then at most one 1 s poll; the bound leaves slack for a loaded host, the 5 s deadline
            # catches a wait that ignores the stop
            assert result == ('returned', None) and elapsed < 3.0

    def test_init_checkpoints_between_phases(self):
        for phase in ('builders', 'stale'):
            daemon = init_daemon(TWO_NS)
            stub_checker(daemon, ['asic0'])
            with init_env(TWO_NS) as env:
                if phase == 'builders':
                    env['get_sfp'].side_effect = lambda pm: (daemon.stop_event.set(), {})[1]
                else:
                    daemon.remove_stale_transceiver_info = MagicMock(side_effect=lambda pm: daemon.stop_event.set())
                assert daemon.init() is None
                if phase == 'builders':
                    assert daemon.remove_stale_transceiver_info.call_count == 0
            # a run() on this daemon starts no worker, joins the checker and still runs deinit() (as today)
            daemon.init = MagicMock(return_value=None)
            daemon.deinit = MagicMock()
            with patch('xcvrd.xcvrd.SfpStateUpdateTask') as sfp_cls, patch('xcvrd.xcvrd.DomInfoUpdateTask') as dom_cls:
                daemon.run()
            assert sfp_cls.call_count == 0 and dom_cls.call_count == 0
            assert daemon.deinit.call_count == 1 and not daemon.readiness_thread.is_alive()

    def test_init_stale_cleanup_skips_pending_namespace(self):
        # the stale-row cleanup of init() covers the ready namespaces: a pending namespace's modules are not read and
        # its rows are left, as they are left today while the daemon waits, to the admission pass
        daemon = make_daemon(TWO_NS)
        daemon.asic_readiness = registry(['asic0'], TWO_NS)
        pm = mapping(TWO_NS_PORTS)
        intf = Table("STATE_DB", 'TRANSCEIVER_INFO')
        for lport, _, _ in TWO_NS_PORTS:
            intf.set(lport, [('manufacturer', 'Molex')])                 # rows of an earlier run; both modules are gone
        daemon.xcvr_table_helper = MagicMock(get_intf_tbl=MagicMock(return_value=intf), get_info_tables=MagicMock(return_value=[intf]))
        with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=False)) as presence:
            daemon.remove_stale_transceiver_info(pm)
            assert [c.args[0] for c in presence.call_args_list] == [1] and sorted(rows(intf)) == ['Ethernet8']
            daemon.asic_readiness.mark_ready('asic1')
            daemon.remove_stale_transceiver_info(pm)
        assert [c.args[0] for c in presence.call_args_list] == [1, 2] and rows(intf) == {}

    def test_init_checker_failed_exits(self):
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, [ASIC_CHECKER_FAILED])
        daemon.deinit = MagicMock()
        with init_env(TWO_NS), pytest.raises(SystemExit) as exc:
            daemon.run()
        assert exc.value.code == SFP_SYSTEM_ERROR
        assert not daemon.readiness_thread.is_alive() and daemon.deinit.call_count == 0
        assert daemon.log.lines('ASIC readiness checker failed')

    def test_init_checker_failed_after_first_namespace(self):
        # a valid namespace followed by the sentinel in the initial drain: the first is registered, the exit is the same
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0', ASIC_CHECKER_FAILED])
        daemon.deinit = MagicMock()
        with init_env(TWO_NS) as env, pytest.raises(SystemExit) as exc:
            daemon.run()
        assert exc.value.code == SFP_SYSTEM_ERROR and not daemon.readiness_thread.is_alive() and daemon.deinit.call_count == 0
        assert daemon.log.lines('ASIC_ACTIVE') == ['[asic0] ASIC_ACTIVE: registering; 1/2 namespace(s) ready']
        assert env['get_mapping'].call_count == 0                    # nothing built after the sentinel

    def test_init_checkpoints_after_drain_and_control_fields(self):
        # (a) stop request seen at the checkpoint after the initial drain: no mapping is read
        daemon = init_daemon(TWO_NS)
        registered = registration_event(daemon)
        waited = {}

        def checker():
            daemon.ready_asic_queue.put('asic0')
            waited['asic0'] = registered.wait(5)                     # init() has registered asic0 and waits for the first sweep
            daemon.stop_event.set()
            daemon.initial_scan_done.set()
        daemon._asic_readiness_checker = checker
        with init_env(TWO_NS) as env:
            assert daemon.init() is None
        assert waited == {'asic0': True}
        assert daemon.asic_readiness.is_ready('asic0') and env['get_mapping'].call_count == 0
        # (b) stop request during the control-field initialisation (after the device dictionaries, which select the
        # table helper the control fields are written with): the stale-row cleanup does not run
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0'])
        daemon.initialize_port_init_control_fields_in_port_table = MagicMock(side_effect=lambda pm: daemon.stop_event.set())
        daemon.remove_stale_transceiver_info = MagicMock()
        with init_env(TWO_NS) as env:
            assert daemon.init() is None
        assert env['get_mapping'].call_count == 1 and daemon.initialize_port_init_control_fields_in_port_table.call_count == 1
        assert daemon.remove_stale_transceiver_info.call_count == 0

    def test_init_empty_namespaces_exit(self):
        daemon = init_daemon([])
        daemon.deinit = MagicMock()
        with init_env([]), pytest.raises(SystemExit) as exc:
            daemon.run()
        assert exc.value.code == PORT_CONFIG_LOAD_ERROR
        assert daemon.log.lines('no front-end namespaces') and daemon.readiness_thread is None

    def test_init_obj_dict_built_summary(self):
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0', 'asic1'])
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet4', 2, 0), ('Ethernet8', 3, 0), ('Ethernet100', 10, 1), ('Ethernet104', 11, 1)])
        with init_env(TWO_NS, port_mapping=pm, sfp={1: MagicMock(), 2: MagicMock(), 3: MagicMock(), 10: MagicMock()}):
            daemon.init()
        line = daemon.log.lines('OBJ_DICT_BUILT')[0]
        assert "{'asic0': 3, 'asic1': 2}" in line and 'without device object 1' in line
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0'])
        with init_env(TWO_NS, port_mapping=mapping([('Ethernet0', 1, 0)]), sfp={1: MagicMock()}):
            daemon.init()
        assert "{'asic0': 1, 'asic1': 0}" in daemon.log.lines('OBJ_DICT_BUILT')[0]      # no CONFIG_DB ports in asic1
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, ['asic0', 'asic1'])
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet2', 1, 0), ('Ethernet4', 2, 1)])     # breakout: two logical ports on cage 1
        with init_env(TWO_NS, port_mapping=pm, sfp={1: MagicMock()}):
            daemon.init()
        line = daemon.log.lines('OBJ_DICT_BUILT')[0]
        assert "{'asic0': 1, 'asic1': 1}" in line and 'without device object 1' in line    # cages counted, not logical ports; cage 2 missing

    def test_init_wait_notices_kept(self):
        daemon = init_daemon(TWO_NS)
        events = []
        real_put = daemon.ready_asic_queue.put

        def checker():
            events.append(('put', len(daemon.log.lines('XCVRD INIT: Wait for port config is done'))))
            real_put('asic0')
            daemon.initial_scan_done.set()
        daemon._asic_readiness_checker = checker
        with init_env(TWO_NS) as env:
            env['get_mapping'].side_effect = lambda ns: events.append(('mapping', len(daemon.log.lines('XCVRD INIT: After port config is done')))) or PortMapping()
            daemon.init()
        assert events[0] == ('put', 1)                              # "Wait..." precedes the checker's first put
        assert events[1] == ('mapping', 1)                          # "After..." precedes the mapping read
        notices = [c.args[0] for c in daemon.log.log_notice.call_args_list]
        assert notices.index('XCVRD INIT: After port config is done') > max(i for i, n in enumerate(notices) if 'ASIC_ACTIVE' in n)


@contextlib.contextmanager
def run_env(daemon, workers):
    """Patch the worker classes so that run() constructs the given FakeWorkers (in the order run() starts them); yields the deinit mock."""
    names = ['SffManagerTask', 'CmisManagerTask', 'CpoManagerTask', 'DomInfoUpdateTask', 'CpoDomInfoUpdateTask',
             'DomThermalInfoUpdateTask', 'SfpStateUpdateTask']
    queue_of_workers = collections.deque(workers)
    with contextlib.ExitStack() as stack:
        for name in names:
            stack.enter_context(patch('xcvrd.xcvrd.' + name, MagicMock(side_effect=lambda *a, **k: queue_of_workers.popleft())))
        stack.enter_context(patch('xcvrd.cpo.cpo_state_task.CpoStateUpdateTask', MagicMock(side_effect=lambda *a, **k: queue_of_workers.popleft())))
        stack.enter_context(patch('xcvrd.xcvrd.platform_chassis', MagicMock(), create=True))
        deinit = stack.enter_context(patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock()))
        yield deinit


class TestRun:

    def _daemon(self, namespaces, ready):
        daemon = make_daemon(namespaces)
        daemon.asic_readiness = registry(ready, namespaces)
        daemon.init = MagicMock(return_value=PortMapping())        # default worker set: DomInfoUpdateTask + SfpStateUpdateTask
        daemon.readiness_thread = MagicMock(**{'is_alive.return_value': False})   # init() is mocked out
        daemon.sfp_obj_dict = {1: MagicMock()}                     # SfpStateUpdateTask is created for pluggable ports only
        daemon.skip_cmis_mgr = True                                # ... and so is CmisManagerTask, unless skipped
        return daemon

    def test_run_drains_later_asics(self):
        daemon = self._daemon(['asic0', 'asic1', 'asic2'], ['asic0'])
        workers = [FakeWorker('dom'), FakeWorker('sfp')]
        marks = []
        real_mark = daemon.asic_readiness.mark_ready
        daemon.asic_readiness.mark_ready = lambda ns: (marks.append((ns, len(daemon.log.lines('ASIC_ACTIVE')))), real_mark(ns))[1]

        staged = {}

        def feeder():
            deadline = time.monotonic() + 5

            def wait_for(condition):
                while not condition() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return bool(condition())
            staged['loop'] = wait_for(lambda: daemon.log.lines('Start daemon main loop'))
            daemon.ready_asic_queue.put('asic1')
            staged['asic1'] = wait_for(lambda: daemon.asic_readiness.is_ready('asic1'))    # admitted before asic2 is queued
            daemon.ready_asic_queue.put('asic2')
            staged['all'] = wait_for(lambda: daemon.log.lines('ASIC_ALL_READY'))
            daemon.stop_event.set()
        feeder_thread = threading.Thread(target=feeder)
        feeder_thread.start()
        with run_env(daemon, workers):
            daemon.run()                                            # no SystemExit
        feeder_thread.join(5)
        assert staged == {'loop': True, 'asic1': True, 'all': True}
        assert daemon.log.lines('ASIC_ALL_READY') == ['ASIC_ALL_READY: 3 namespace(s) ready']
        assert [m[0] for m in marks] == ['asic1', 'asic2'] and all(count == i + 1 for i, (_, count) in enumerate(marks))   # log precedes mark
        assert all(w.task_stopping_event.is_set() and 'join' in w.events for w in workers)

    def test_run_passes_registry_to_every_worker_kind(self):
        daemon = self._daemon([''], [''])
        daemon.enable_sff_mgr = True
        daemon.dom_temperature_poll_interval = 10
        daemon.sfp_obj_dict = {1: MagicMock()}
        daemon.cpo_obj_dict = {2: MagicMock()}
        daemon.skip_cmis_mgr = False
        daemon.stop_event.wait = MagicMock()
        names = ['SffManagerTask', 'CmisManagerTask', 'CpoManagerTask', 'DomInfoUpdateTask', 'CpoDomInfoUpdateTask', 'DomThermalInfoUpdateTask', 'SfpStateUpdateTask']
        with contextlib.ExitStack() as stack:
            mocks = {n: stack.enter_context(patch('xcvrd.xcvrd.' + n)) for n in names}
            cpo_state = stack.enter_context(patch('xcvrd.cpo.cpo_state_task.CpoStateUpdateTask'))
            deinit = stack.enter_context(patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock()))
            daemon.run()
        for n in names:
            assert mocks[n].call_count == 1, n
            if n != 'DomThermalInfoUpdateTask':
                assert mocks[n].call_args.kwargs['asic_readiness'] is daemon.asic_readiness, n
        assert 'asic_readiness' not in mocks['DomThermalInfoUpdateTask'].call_args.kwargs
        assert cpo_state.call_args.kwargs['asic_readiness'] is daemon.asic_readiness
        assert mocks['SfpStateUpdateTask'].call_args.kwargs == {'asic_readiness': daemon.asic_readiness} == cpo_state.call_args.kwargs


    def test_run_gives_each_publishing_worker_its_own_error_cache(self):
        # the daemon's own wiring, with the real worker classes: the two publishing workers of a chassis with CPO ports
        # hold distinct error caches, so an error one of them consumes is unknown to the other, as today
        daemon = self._daemon(TWO_NS, TWO_NS)
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet8', 2, 1)])
        daemon.init = MagicMock(return_value=pm)
        daemon.sfp_obj_dict = {1: MagicMock()}
        daemon.cpo_obj_dict = {2: MagicMock()}
        daemon.stop_event.wait = MagicMock()
        started = []
        with contextlib.ExitStack() as stack:
            for n in ('SffManagerTask', 'CmisManagerTask', 'CpoManagerTask', 'DomInfoUpdateTask', 'CpoDomInfoUpdateTask', 'DomThermalInfoUpdateTask'):
                stack.enter_context(patch('xcvrd.xcvrd.' + n))
            stack.enter_context(pin_multi_asic(TWO_NS))
            stack.enter_context(patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock()))
            stack.enter_context(patch('xcvrd.xcvrd_utilities.common.is_syncd_warm_restore_complete', MagicMock(return_value=False)))
            stack.enter_context(patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False)))
            stack.enter_context(patch.object(threading.Thread, 'start', lambda self: started.append(self)))
            stack.enter_context(patch.object(threading.Thread, 'is_alive', lambda self: True))
            stack.enter_context(patch.object(threading.Thread, 'join', lambda self, timeout=None: None))
            stack.enter_context(patch.object(SfpStateUpdateTask, 'raise_exception', lambda self: None))   # never started: no thread to interrupt
            stack.enter_context(patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock()))
            daemon.run()
        sfp = next(w for w in started if type(w).__name__ == 'SfpStateUpdateTask')
        cpo = next(w for w in started if type(w).__name__ == 'CpoStateUpdateTask')
        assert sfp.sfp_error_dict is not cpo.sfp_error_dict
        cpo.sfp_error_dict[2] = (str(int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING), {})
        assert sfp.sfp_error_dict == {} and sfp._recorded_error('Ethernet8', sfp.port_mapping) == (False, False) and cpo._recorded_error('Ethernet8', cpo.port_mapping) == (True, True)

    def test_run_checker_failed_after_start_exits_nonzero(self):
        daemon = self._daemon(TWO_NS, ['asic0'])
        workers = [FakeWorker('dom'), FakeWorker('sfp')]
        daemon.stop_event.wait = MagicMock()                         # must never be called: asserted below (a raising spy would be swallowed by run()'s guard)
        flags_at_join = []
        for w in workers:
            w.join = (lambda w=w: lambda: flags_at_join.append(all(x.task_stopping_event.is_set() for x in workers)) or w.events.append('join'))()
        threading.Timer(0.2, lambda: daemon.ready_asic_queue.put(ASIC_CHECKER_FAILED)).start()
        with run_env(daemon, workers) as deinit, pytest.raises(SystemExit) as exc:
            daemon.run()
        assert exc.value.code == SFP_SYSTEM_ERROR and daemon.stop_event.is_set()
        assert daemon.stop_event.wait.call_count == 0              # the steady-state wait was never entered with the sentinel queued
        assert flags_at_join and all(flags_at_join)                # every stop flag set before the first join
        assert deinit.call_count == 1

    def test_run_preserves_sfp_error_exit(self):
        daemon = self._daemon([''], [''])
        daemon.stop_event.wait = MagicMock(side_effect=lambda: daemon.sfp_error_event.set())
        with run_env(daemon, [FakeWorker('dom'), FakeWorker('sfp')]), pytest.raises(SystemExit) as exc:
            daemon.run()
        assert exc.value.code == SFP_SYSTEM_ERROR

    def test_run_stop_during_init_joins_checker_and_deinits(self):
        daemon = init_daemon(TWO_NS)
        stub_checker(daemon, [], scan_done=False)                  # nothing ever becomes ready
        daemon.deinit = MagicMock()
        threading.Timer(0.3, daemon.stop_event.set).start()
        with init_env(TWO_NS), patch('xcvrd.xcvrd.SfpStateUpdateTask') as sfp_cls, patch('xcvrd.xcvrd.DomInfoUpdateTask') as dom_cls:
            daemon.run()                                            # intentional stop: no SystemExit
        assert sfp_cls.call_count == 0 and dom_cls.call_count == 0
        assert daemon.readiness_thread is not None and not daemon.readiness_thread.is_alive()
        assert daemon.deinit.call_count == 1

    def test_run_joins_checker_thread(self):
        daemon = self._daemon([''], [''])
        daemon.stop_event.wait = MagicMock(side_effect=daemon.stop_event.set)   # the steady-state wait is mocked; the checker polls is_set()

        def cooperative_checker():
            while not daemon.stop_event.is_set():
                time.sleep(0.01)
        cooperative = threading.Thread(target=cooperative_checker)
        cooperative.join = MagicMock(wraps=cooperative.join)
        cooperative.start()
        daemon.readiness_thread = cooperative
        with run_env(daemon, [FakeWorker('dom'), FakeWorker('sfp')]):
            daemon.run()
        assert cooperative.join.call_args_list == [((ASIC_CHECKER_JOIN_TIMEOUT_SECS,),)]      # a real join with the configured timeout
        assert not cooperative.is_alive() and daemon.log.lines('did not stop') == []
        daemon = self._daemon([''], [''])
        daemon.stop_event.wait = MagicMock()
        release = threading.Event()
        stubborn = threading.Thread(target=release.wait)
        stubborn.join = MagicMock(wraps=stubborn.join)
        stubborn.start()
        daemon.readiness_thread = stubborn
        safety_net = threading.Timer(3.0, release.set)             # a missing join timeout must fail the test, not hang it
        safety_net.start()
        try:
            with run_env(daemon, [FakeWorker('dom'), FakeWorker('sfp')]) as deinit, patch('xcvrd.xcvrd.ASIC_CHECKER_JOIN_TIMEOUT_SECS', 0.2):
                daemon.run()                                        # a checker that does not stop is logged, not fatal
            assert stubborn.join.call_args_list == [((0.2,),)]
            assert daemon.log.lines('readiness checker did not stop') and deinit.call_count == 1
        finally:
            safety_net.cancel()
            release.set()
            stubborn.join(5)

    def test_run_dead_thread_sigkill_path_unchanged(self):
        daemon = self._daemon([''], [''])
        daemon.stop_event.wait = MagicMock()
        dead = FakeWorker('dom', alive=False, join_error=NotImplementedError('died'))
        live = FakeWorker('sfp')
        with run_env(daemon, [dead, live]) as deinit, patch('xcvrd.xcvrd.os.kill') as kill:
            daemon.run()
        assert kill.call_count == 1 and 'join' in live.events and deinit.call_count == 1

    def test_run_cleanup_broadcasts_stop_before_dead_thread_join(self):
        daemon = self._daemon([''], [''])
        daemon.stop_event.wait = MagicMock()
        live = FakeWorker('sfp')
        dead = FakeWorker('dom', alive=False)
        dead.join = lambda: dead.events.append(('join', dead.task_stopping_event.is_set(), live.task_stopping_event.is_set()))
        with run_env(daemon, [dead, live]) as deinit, patch('xcvrd.xcvrd.os.kill') as kill:
            daemon.run()
        assert ('join', True, True) in dead.events and kill.call_count == 0 and 'join' in live.events


class TestSingleAsic:

    def test_single_asic_flow_unchanged(self):
        daemon = init_daemon([''])
        stub_checker(daemon, [''])
        pm = mapping([('Ethernet0', 1, 0), ('Ethernet4', 2, 0)])
        sfp = {1: MagicMock(), 2: MagicMock()}
        with init_env([''], port_mapping=pm, sfp=sfp):
            result = daemon.init()
        assert result is pm and daemon.asic_readiness.all_ready() and daemon.asic_readiness.namespace_for_asic(0) == ''
        assert daemon.log.lines('ASIC_ACTIVE') == ['[] ASIC_ACTIVE: registering; 1/1 namespace(s) ready']
        daemon.init = MagicMock(return_value=pm)
        daemon.stop_event.wait = MagicMock()
        with contextlib.ExitStack() as stack:
            dom_cls = stack.enter_context(patch('xcvrd.xcvrd.DomInfoUpdateTask'))
            sfp_cls = stack.enter_context(patch('xcvrd.xcvrd.SfpStateUpdateTask'))
            cmis_cls = stack.enter_context(patch('xcvrd.xcvrd.CmisManagerTask'))
            deinit = stack.enter_context(patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock()))
            daemon.run()
        assert daemon.log.lines('ASIC_ALL_READY') == ['ASIC_ALL_READY: 1 namespace(s) ready']
        assert cmis_cls.call_args.args == ([''], pm, sfp, daemon.stop_event) and cmis_cls.call_args.kwargs == {'skip_cmis_mgr': False, 'asic_readiness': daemon.asic_readiness}
        assert dom_cls.call_args.args == ([''], pm, sfp, daemon.stop_event, False, None) and dom_cls.call_args.kwargs == {'asic_readiness': daemon.asic_readiness}
        assert sfp_cls.call_args.args == ([''], pm, sfp, daemon.stop_event, daemon.sfp_error_event) and sfp_cls.call_args.kwargs == {'asic_readiness': daemon.asic_readiness}
        reg = daemon.asic_readiness
        assert reg.admitted_namespaces() == frozenset(['']) and reg.is_asic_admitted(0)     # before any publisher is declared ...
        with pin_multi_asic(['']):
            task = make_sfp_task([''], pm, sfp, reg)
            assert reg._admitted == {'SfpStateUpdateTask': {''}} and reg.admitted_namespaces() == frozenset([''])   # ... and after
            for name in ('_post_port_sfp_info_and_dom_thr_to_db_once', '_init_port_sfp_status_sw_tbl'):
                setattr(task, name, MagicMock(wraps=getattr(task, name)))
            with patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting', MagicMock()) as publisher, \
                    patch_publishers('post_port_info_to_db', MagicMock(return_value=None)):
                worker_init(task, pm)
            assert publisher.call_count == 2 and task._pending_namespaces == set()
            for pass_fn in (task._post_port_sfp_info_and_dom_thr_to_db_once, task._init_port_sfp_status_sw_tbl):
                assert pass_fn.call_args.kwargs['logical_ports'] is None             # the old calls, over every port
            cmis = make_cmis_task([''], pm, sfp, reg)
            for lport, pport, asic in [('Ethernet0', 1, 0), ('Ethernet4', 2, 0)]:
                cmis.on_port_update_event(PortChangeEvent(lport, pport, asic, PortChangeEvent.PORT_SET, dict(CMIS_PORT_ATTRS)))
            cmis_iteration(cmis, 2)
            assert sfp[1].get_presence.call_count == 1 and sfp[2].get_presence.call_count == 1
            sff, chassis = make_sff_task([''], reg, {1: sff_sfp(sff_api())})
            sff.on_port_update_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET, dict(SFF_ATTRS)))
            sff_iteration(sff, has_event=True, visited_ports=1)
            assert chassis.get_sfp.call_count == 1 and sff.deferred_ports == set()
