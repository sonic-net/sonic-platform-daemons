"""Shared pieces for the integration tests: fake platform, daemon wiring, DB helpers."""

import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

from swsscommon import swsscommon

NAMESPACES = ['asic0', 'asic1']
PORTS_PER_NS = 4
XCVR_INFO = {'type': 'QSFP-DD', 'vendor_rev': 'A', 'serial': 'S1', 'manufacturer': 'Molex', 'model': '1064141421',
             'vendor_oui': '00-00-00', 'vendor_date': '2026', 'connector': 'LC', 'encoding': 'N/A',
             'ext_identifier': 'N/A', 'ext_rateselect_compliance': 'N/A', 'cable_type': 'Length Cable Assembly(m)',
             'cable_length': 1, 'specification_compliance': '{}', 'nominal_bit_rate': 0,
             'application_advertisement': 'N/A', 'dom_capability': 'N/A', 'type_abbrv_name': 'QSFP-DD'}


def ports_of(ns):
    """[(lport, pport, asic_id)] of an emulated namespace: asic0 -> Ethernet0..12 / pports 1..4, asic1 -> Ethernet16.. / 5..8."""
    asic = NAMESPACES.index(ns)
    return [('Ethernet{}'.format(4 * (asic * PORTS_PER_NS + i)), asic * PORTS_PER_NS + i + 1, asic) for i in range(PORTS_PER_NS)]


def connector(db_name, ns):
    return swsscommon.DBConnector(db_name, 0, True, ns)


def config_port_fvs(lport, pport):
    """The CONFIG_DB PORT row of an emulated port."""
    return swsscommon.FieldValuePairs([('index', str(pport)), ('lanes', '1,2,3,4,5,6,7,8'), ('speed', '400000'),
                                       ('admin_status', 'up'), ('alias', lport)])


def populate_config_db(ns):
    tbl = swsscommon.Table(connector('CONFIG_DB', ns), 'PORT')
    for lport, pport, _ in ports_of(ns):
        tbl.set(lport, config_port_fvs(lport, pport))


def populate_appl_db(ns, markers):
    tbl = swsscommon.Table(connector('APPL_DB', ns), 'PORT_TABLE')
    for lport, pport, _ in ports_of(ns):
        tbl.set(lport, swsscommon.FieldValuePairs([('admin_status', 'up'), ('speed', '400000'), ('index', str(pport))]))
    for marker in markers:
        write_marker(ns, marker)


def write_marker(ns, marker):
    swsscommon.Table(connector('APPL_DB', ns), 'PORT_TABLE').set(marker, swsscommon.FieldValuePairs([('count', '4')]))


def del_marker(ns, marker):
    swsscommon.Table(connector('APPL_DB', ns), 'PORT_TABLE')._del(marker)


def appl_port_rows(ns):
    """{lport: {field: value}} of APPL_DB PORT_TABLE as a plain Table read (what orchagent would consume after the producer)."""
    tbl = swsscommon.Table(connector('APPL_DB', ns), 'PORT_TABLE')
    return {key: dict(tbl.get(key)[1]) for key in tbl.getKeys()}


def state_field(ns, table, key, field):
    found, value = swsscommon.Table(connector('STATE_DB', ns), table).hget(key, field)
    return value if found else None


DOM_THRESHOLDS = {'temphighalarm': '75.0', 'templowalarm': '-5.0', 'vcchighalarm': '3.6', 'vcclowalarm': '3.0'}
DOM_VALUES = {'temperature': '35.5', 'voltage': '3.3', 'rx1power': '-1.5', 'tx1bias': '7.0'}


class FakeSfp:
    """A module whose EEPROM reads succeed while `readable` holds; the CMIS worker treats it as flat memory (no
    state machine); its DOM thresholds and sensor values are fixed (DOM_THRESHOLDS, DOM_VALUES plus the port).

    `present` is what get_presence() reports and may be changed at any time; a change is not an event,
    get_change_event() delivers queued events only (FakeChassis.events). `readable` False makes
    get_transceiver_info() return None, what the publishing worker takes for an EEPROM that is not ready.
    `presence_readers` records the thread that made each presence read."""

    def __init__(self, pport):
        self.pport = pport
        self.present = True
        self.readable = True
        self.api = MagicMock(name='api-{}'.format(pport))
        self.api.is_flat_memory = MagicMock(return_value=True)
        self.api.is_copper = MagicMock(return_value=False)
        self.calls = []
        self.presence_readers = []                   # (monotonic time, thread name) per get_presence() call

    def _rec(self, name):
        self.calls.append(name)

    def get_presence(self):
        self._rec('get_presence')
        self.presence_readers.append((time.monotonic(), threading.current_thread().name))
        return self.present

    def get_transceiver_info(self):
        self._rec('get_transceiver_info'); return dict(XCVR_INFO, serial='S{}'.format(self.pport)) if self.readable else None

    def is_replaceable(self):
        self._rec('is_replaceable'); return True

    def get_xcvr_api(self):
        self._rec('get_xcvr_api'); return self.api

    def get_transceiver_dom_threshold(self):
        self._rec('get_transceiver_dom_threshold'); return dict(DOM_THRESHOLDS)

    def get_transceiver_threshold_info(self):
        return dict(DOM_THRESHOLDS)

    def get_transceiver_dom_real_value(self):
        return dict(DOM_VALUES, port=str(self.pport))

    def get_transceiver_dom_flags(self):
        return {}

    def get_transceiver_info_firmware_versions(self):
        return {}

    def remove_xcvr_api(self):
        self._rec('remove_xcvr_api')

    def __getattr__(self, name):                     # every other API: recorded, returns nothing useful
        if name.startswith('_'):
            raise AttributeError(name)
        def stub(*args, **kwargs):
            self._rec(name); return {} if name.startswith('get_') else None
        return stub


def management_init_window(sfp, until):
    """Until `until` (monotonic) the module answers no xcvr api and no EEPROM data, as a module inside its
    management-interface start-up; afterwards the fake's normal answers."""
    real_api, real_info = sfp.get_xcvr_api, sfp.get_transceiver_info

    def api():
        if time.monotonic() < until:
            sfp.calls.append('get_xcvr_api:none')
            return None
        return real_api()

    def info():
        if time.monotonic() < until:
            sfp.calls.append('get_transceiver_info:none')
            return None
        return real_info()
    sfp.get_xcvr_api, sfp.get_transceiver_info = api, info


class FakeChassis:
    """idle_status=False: a poll without a queued event answers (False, {}), the shape of a platform whose idle
    poll reports no status; the publishing worker then never matures a soaking insertion on an idle poll."""

    def __init__(self, event_period=1.0, idle_status=True):
        self.sfps = {pport: FakeSfp(pport) for ns in NAMESPACES for _, pport, _ in ports_of(ns)}
        self.event_period = event_period
        self.idle_status = idle_status
        self.events = []                              # queued {pport: status} dicts delivered by get_change_event
        self.change_event_hold = None                 # (held, release) armed by hold_next_change_event()

    def get_sfp(self, pport):
        return self.sfps[pport]

    def get_cpo(self, pport):
        raise NotImplementedError

    def get_num_sfps(self):
        return len(self.sfps)

    def get_all_sfps(self):
        return list(self.sfps.values())

    def set_presence(self, pports, value):
        """Make the modules of pports present (True) or absent (False); no event is queued."""
        for pport in pports:
            self.sfps[pport].present = value

    def hold_next_change_event(self):
        """Hold the next get_change_event() call: on entry it sets the returned `held` event and it returns
        only once the returned `release` event is set (bounded wait, so a failing test cannot hang the worker)."""
        self.change_event_hold = (threading.Event(), threading.Event())
        return self.change_event_hold

    def get_change_event(self, timeout=0):
        hold, self.change_event_hold = self.change_event_hold, None
        if hold is not None:
            held, release = hold
            held.set()
            release.wait(30)
        time.sleep(min(timeout / 1000.0, self.event_period) if timeout else self.event_period)
        if self.events:
            return True, {'sfp': self.events.pop(0), 'sfp_error': {}}
        return self.idle_status, {'sfp': {}, 'sfp_error': {}}


class LogRecorder:
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def _log(self, level):
        def f(msg, *args, **kwargs):
            with self.lock:
                self.lines.append((level, time.monotonic(), str(msg)))
        return f

    def __getattr__(self, name):
        if name.startswith('log_'):
            return self._log(name[4:])
        raise AttributeError(name)

    def find(self, token):
        with self.lock:
            return [m for _, _, m in self.lines if token in m]

    def wait_for(self, token, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = self.find(token)
            if hits:
                return hits
            time.sleep(0.05)
        return []


def install_fake_platform(chassis):
    """sonic_platform.platform.Platform().get_chassis() -> chassis, as xcvrd's init() imports it."""
    platform_mod = types.ModuleType('sonic_platform.platform')
    platform_mod.Platform = lambda: MagicMock(get_chassis=MagicMock(return_value=chassis))
    pkg = types.ModuleType('sonic_platform')
    pkg.platform = platform_mod
    sys.modules['sonic_platform'] = pkg
    sys.modules['sonic_platform.platform'] = platform_mod


def multi_asic_patches():
    """The multi-ASIC facts of the emulated system, for the modules that consult sonic_py_common.multi_asic."""
    from sonic_py_common import multi_asic
    index = {ns: i for i, ns in enumerate(NAMESPACES)}
    return [patch.object(multi_asic, 'get_front_end_namespaces', return_value=list(NAMESPACES)),
            patch.object(multi_asic, 'get_asic_index_from_namespace', side_effect=lambda ns: index.get(ns, 0)),
            patch.object(multi_asic, 'is_multi_asic', return_value=True),
            patch.object(multi_asic, 'is_front_panel_port', return_value=True),
            patch.object(swsscommon.SonicDBConfig, 'initializeGlobalConfig', staticmethod(lambda: None))]
