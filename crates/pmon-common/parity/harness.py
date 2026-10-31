"""The Python half of the parity tests: a Python daemon's own code, run
against a mock platform, with everything it publishes and logs recorded.

The Rust daemon is then run over the platform API bridge against an
identical mock -- the same `mock_platform.py` the Python daemon's unit tests
use -- and the two are compared (see `parity.rs`).  What makes the comparison
mean something is that neither side is a model of the other: the Python half
is the daemon script itself, imported the way its own tests import it, and
the Rust half is the daemon crate's own code.

Stand-ins, installed once per interpreter by `install()`:

- `swsscommon`: tables kept in memory with redis's semantics -- `set` merges
  fields into the row, as HSET does -- shared by name within a database, as
  two `Table` objects on one redis table are.  The daemons' own test mocks
  replace the whole row on `set`, which is not what a daemon running on a
  switch sees once it writes one row in two steps.
- `sonic_platform`: a package whose `Platform().get_chassis()` is whatever
  `use_chassis()` last set.  The Python daemon imports it, and so does the
  bridge the Rust daemon opens.
- `sonic_py_common`: the real package, with the database connection and the
  loggers replaced.  Only NOTICE and above are recorded: that is what reaches
  syslog from both daemons by default.
- `signal.signal` / `signal.getsignal`: a table rather than the interpreter's.
  Python lets only its main thread install a handler, and a test runs on
  another; and what a scenario needs of them is the chain -- whose handler a
  vendor installed over whose -- not delivery by the OS.  Replaced on the
  module itself, so the daemon's `DaemonBase`, a vendor's installer and the
  bridge's arming all read and write one table, and `deliver()` runs what it
  holds, as the interpreter would when the signal arrived.
"""

import importlib.machinery
import importlib.util
import sys
import types
from unittest import mock

LOGS = []
_TABLES = {}
_CHASSIS = [None]
# What `sonic_platform.pcie.Pcie(path)` constructs, and the answers the
# daemons' `device_info` calls get, per scenario.
_PCIE = [None]
FLAGS = {'is_smartswitch': False}
_INSTALLED = [False]
_SCRIPTS = {}
# Run each time the chassis is handed out (see `use_chassis`).
_BUILT = [None]
# signum -> handler; a signal not in here has its default, SIG_DFL.
_SIGNALS = {}


class Db(object):
    """A database handle, with DBConnector's key/field calls over the same
    tables ('TABLE|key')."""

    def __init__(self, name, *args, **kwargs):
        self.name = str(name)

    def _row(self, key, create=False):
        table, _, k = key.partition('|')
        rows = _TABLES.setdefault((self.name, table), {})
        return rows.setdefault(k, {}) if create else rows.get(k)

    def hget(self, key, field):
        row = self._row(key)
        return None if row is None else row.get(field)

    def hgetall(self, key):
        return dict(self._row(key) or {})

    def hset(self, key, field, value):
        self._row(key, create=True)[field] = value

    def exists(self, key):
        return self._row(key) is not None

    def delete(self, key):
        table, _, k = key.partition('|')
        return 1 if _TABLES.get((self.name, table), {}).pop(k, None) is not None else 0

    def keys(self, pattern='*'):
        import fnmatch
        out = []
        for (d, t), rows in _TABLES.items():
            if d == self.name:
                out.extend(k for k in (t + '|' + r for r in rows) if fnmatch.fnmatchcase(k, pattern))
        return out


def _db_name(db):
    return getattr(db, 'name', str(db))


class Table(object):
    """swsscommon.Table over a dict, with redis's semantics."""

    def __init__(self, db, table_name, *args, **kwargs):
        self.table_name = table_name
        self.rows = _TABLES.setdefault((_db_name(db), table_name), {})

    def set(self, key, fvs, *args, **kwargs):
        row = self.rows.setdefault(key, {})
        for field, value in fvs:
            if not isinstance(field, str) or not isinstance(value, str):
                # The C++ binding takes strings only.
                raise TypeError('in method set: expected (str, str), got (%r, %r)' % (field, value))
            row[field] = value

    def hset(self, key, field, value):
        self.set(key, [(field, value)])

    def _del(self, key):
        self.rows.pop(key, None)

    def delete(self, key):
        self._del(key)

    def hdel(self, key, field):
        row = self.rows.get(key)
        if row is not None:
            row.pop(field, None)
            if not row:
                del self.rows[key]

    def get(self, key):
        # A list, as the binding answers it -- chassisd tests
        # `isinstance(fvs, list)`.
        if key in self.rows:
            return [True, tuple(self.rows[key].items())]
        return [False, ()]

    def hget(self, key, field):
        row = self.rows.get(key)
        if row is not None and field in row:
            return True, row[field]
        return False, None

    def getKeys(self):
        return list(self.rows)


class SonicV2Connector(object):
    """The key/field interface, over the same tables: 'TABLE|key' (':' on
    APPL_DB, as the real one splits it)."""

    STATE_DB = 'STATE_DB'
    CONFIG_DB = 'CONFIG_DB'
    CHASSIS_STATE_DB = 'CHASSIS_STATE_DB'
    APPL_DB = 'APPL_DB'

    def __init__(self, *args, **kwargs):
        pass

    def connect(self, *args, **kwargs):
        pass

    def close(self, *args, **kwargs):
        pass

    @staticmethod
    def _split(db, key):
        table, _, rest = key.partition(':' if db == 'APPL_DB' else '|')
        return Table(Db(db), table), rest

    def hmset(self, db, key, fvs):
        table, k = self._split(db, key)
        table.set(k, list(fvs.items()))

    def set(self, db, key, field, value, *args, **kwargs):
        table, k = self._split(db, key)
        table.set(k, [(field, value)])

    def get(self, db, key, field, *args, **kwargs):
        table, k = self._split(db, key)
        return table.rows.get(k, {}).get(field)

    def get_all(self, db, key, *args, **kwargs):
        table, k = self._split(db, key)
        return dict(table.rows.get(k, {}))

    def exists(self, db, key):
        table, k = self._split(db, key)
        return k in table.rows

    def delete(self, db, key, *args, **kwargs):
        table, k = self._split(db, key)
        return 1 if table.rows.pop(k, None) is not None else 0

    def keys(self, db, pattern='*', *args, **kwargs):
        import fnmatch
        sep = ':' if db == 'APPL_DB' else '|'
        out = []
        for (d, t), rows in _TABLES.items():
            if d == db:
                out.extend(k for k in (t + sep + r for r in rows) if fnmatch.fnmatchcase(k, pattern))
        return out


class ConfigDBConnector(object):
    """CONFIG_DB's table/entry interface over the same tables."""

    def __init__(self, *args, **kwargs):
        pass

    def connect(self, *args, **kwargs):
        pass

    def get_table(self, table):
        return {k: dict(v) for k, v in _TABLES.get(('CONFIG_DB', table), {}).items()}

    def get_entry(self, table, key):
        return dict(_TABLES.get(('CONFIG_DB', table), {}).get(key, {}))


class FieldValuePairs(list):
    """A list of (field, value) pairs, as the binding's vector is."""

    def __init__(self, fvs=()):
        super(FieldValuePairs, self).__init__(fvs)


def _swsscommon():
    inner = types.ModuleType('swsscommon.swsscommon')
    inner.STATE_DB = 'STATE_DB'
    inner.CHASSIS_STATE_DB = 'CHASSIS_STATE_DB'
    inner.CONFIG_DB = 'CONFIG_DB'
    inner.APPL_DB = 'APPL_DB'
    inner.Table = Table
    inner.FieldValuePairs = FieldValuePairs
    inner.DBConnector = Db
    inner.SonicV2Connector = SonicV2Connector
    inner.ConfigDBConnector = ConfigDBConnector

    def missing(name):
        # Anything else a daemon reaches for at import time; nothing the
        # parity tests drive uses it.
        if name.startswith('__'):
            raise AttributeError(name)
        return mock.MagicMock(name='swsscommon.' + name)
    inner.__getattr__ = missing
    outer = types.ModuleType('swsscommon')
    outer.__path__ = []
    outer.swsscommon = inner
    return outer, inner


def _record(level):
    def log(self, msg, *args, **kwargs):
        LOGS.append((level, str(msg)))
    return log


def _quiet(self, *args, **kwargs):
    pass


def install():
    """Put the stand-ins in place.  Idempotent."""
    if _INSTALLED[0]:
        return
    outer, inner = _swsscommon()
    sys.modules['swsscommon'] = outer
    sys.modules['swsscommon.swsscommon'] = inner

    platform = types.ModuleType('sonic_platform.platform')

    class Platform(object):
        def get_chassis(self):
            if _BUILT[0] is not None:
                _BUILT[0]()
            return _CHASSIS[0]
    platform.Platform = Platform
    # psud imports the vendor's Psu class for the master LED, a classmethod.
    from sonic_platform_base.psu_base import PsuBase
    psu = types.ModuleType('sonic_platform.psu')

    class Psu(PsuBase):
        pass
    psu.Psu = Psu
    # pcied, and the facade's pcie escape hatch, construct the vendor's Pcie.
    pcie = types.ModuleType('sonic_platform.pcie')

    class Pcie(object):
        def __new__(cls, platform_path):
            return _PCIE[0]
    pcie.Pcie = Pcie
    package = types.ModuleType('sonic_platform')
    package.__path__ = []
    package.platform = platform
    package.psu = psu
    package.pcie = pcie
    sys.modules['sonic_platform'] = package
    sys.modules['sonic_platform.platform'] = platform
    sys.modules['sonic_platform.psu'] = psu
    sys.modules['sonic_platform.pcie'] = pcie

    import signal

    def set_handler(signum, handler):
        previous = _SIGNALS.get(signum, signal.SIG_DFL)
        _SIGNALS[signum] = handler
        return previous
    signal.signal = set_handler
    signal.getsignal = lambda signum: _SIGNALS.get(signum, signal.SIG_DFL)

    from sonic_py_common import daemon_base, device_info, logger, syslogger
    daemon_base.db_connect = lambda name, *args, **kwargs: Db(name)
    daemon_base.db_connect_remote = lambda *args, **kwargs: Db('REMOTE')
    # Off the switch host, as a daemon in a test is.
    device_info.is_switch_host = lambda: False
    device_info.is_switch_bmc = lambda: False
    device_info.is_smartswitch = lambda: FLAGS['is_smartswitch']
    device_info.get_paths_to_platform_and_hwsku_dirs = \
        lambda: ('/usr/share/sonic/platform', '/usr/share/sonic/hwsku')
    for cls in (logger.Logger, syslogger.SysLogger, daemon_base.DaemonBase):
        for level in ('error', 'warning', 'notice'):
            setattr(cls, 'log_' + level, _record(level.upper()))
        cls.log_info = _quiet
        cls.log_debug = _quiet
    _INSTALLED[0] = True


def reset():
    """Before each half of each scenario: no tables, no logs, the defaults."""
    _TABLES.clear()
    del LOGS[:]
    FLAGS['is_smartswitch'] = False
    _BUILT[0] = None
    reset_signals()


def reset_signals():
    """Every signal back to its default, as in a process that has installed
    nothing -- which lets go of every handler, and of whatever it holds."""
    _SIGNALS.clear()


def deliver(signum):
    """`signum` arriving: the handler the table holds runs, as the interpreter
    runs it.  With none, the process would have died there, which no scenario
    means to happen."""
    import signal
    handler = signal.getsignal(signum)
    if not callable(handler):
        raise AssertionError('signal {} has no handler ({!r}): the process would have '
                             'ended'.format(signum, handler))
    handler(signum, None)


def use_chassis(chassis, built=None):
    """What `sonic_platform.platform.Platform().get_chassis()` answers next.

    `built`, if given, runs every time it does: a vendor's chassis constructor
    runs every time `get_chassis()` builds one, and that is where a vendor
    installs its shutdown handler (mlnx's `utils.watch_shutdown_signals()`).
    """
    _CHASSIS[0] = chassis
    _BUILT[0] = built


def use_pcie(pcie):
    """What `sonic_platform.pcie.Pcie(path)` answers next."""
    _PCIE[0] = pcie


def load_file(name, path):
    """A module from a file, under a name of the caller's choosing."""
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def script(name, path):
    """A daemon script, imported once, the way its own tests import it."""
    if name not in _SCRIPTS:
        _SCRIPTS[name] = load_file(name, path)
    return _SCRIPTS[name]


def tables():
    """What was published: {database: {table: {key: {field: value}}}}."""
    out = {}
    for (db, table), rows in _TABLES.items():
        if rows:
            out.setdefault(db, {})[table] = {k: dict(v) for k, v in rows.items()}
    return out


def has_row(db, table, key):
    """Whether `key` is in `table` now -- answerable while a daemon's own
    thread is writing, which `tables()` is not."""
    return key in _TABLES.get((db, table), {})


def logs():
    return list(LOGS)
