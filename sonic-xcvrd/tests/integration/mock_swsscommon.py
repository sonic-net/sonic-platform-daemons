"""In-memory swsscommon surface used by xcvrd and its SONiC dependencies.

Tables share rows by namespace, database and table name. SET merges fields;
subscribers receive independent snapshots, including existing rows on startup.
No native swsscommon bindings or Redis connections are used.
"""

from collections import defaultdict, deque
from threading import Condition
import time


APPL_DB = 0
COUNTERS_DB = 2
CONFIG_DB = 4
STATE_DB = 6
CHASSIS_STATE_DB = 13
CFG_PORT_TABLE_NAME = 'PORT'
APP_PORT_TABLE_NAME = 'PORT_TABLE'
STATE_PORT_TABLE_NAME = 'PORT_TABLE'
CFG_LOGGER_TABLE_NAME = 'LOGGER'
SET_COMMAND = 'SET'
DEL_COMMAND = 'DEL'
FieldValuePairs = list

DB_NAMES = {
    APPL_DB: 'APPL_DB',
    COUNTERS_DB: 'COUNTERS_DB',
    CONFIG_DB: 'CONFIG_DB',
    STATE_DB: 'STATE_DB',
    CHASSIS_STATE_DB: 'CHASSIS_STATE_DB',
}


class MemoryDatabase:
    """Thread-safe shared storage and notifications for one test."""

    def __init__(self):
        self.condition = Condition()
        self.rows = defaultdict(dict)
        self.subscribers = defaultdict(list)
        self.history = []

    def reset(self):
        """Discard the previous test's data after its workers have stopped."""
        with self.condition:
            self.rows.clear()
            self.subscribers.clear()
            self.history.clear()

    def publish(self, address, key, op, fields):
        """Record a write and notify every subscriber while holding the lock."""
        self.history.append((address, key, op, dict(fields)))
        for subscriber in self.subscribers[address]:
            subscriber.events.append((key, op, list(fields.items())))
        self.condition.notify_all()

    def table(self, db_name, table_name, namespace=''):
        """Return a table handle for arranging and asserting test data."""
        return Table(DBConnector(db_name, namespace=namespace), table_name)

    def set(self, db_name, table_name, key, fields, namespace=''):
        """Set string-valued fields as a SONiC producer would."""
        self.table(db_name, table_name, namespace).set(
            key, [(field, str(value)) for field, value in fields.items()])

    def get(self, db_name, table_name, key, namespace=''):
        """Read a row without exposing mutable backing storage."""
        found, fields = self.table(db_name, table_name, namespace).get(key)
        return dict(fields) if found else {}

    def delete(self, db_name, table_name, key, namespace=''):
        """Delete a row and publish its DEL notification."""
        self.table(db_name, table_name, namespace)._del(key)

    def cmis_states(self, lport):
        """Return the CMIS states actually written by the worker."""
        with self.condition:
            return [fields['cmis_state']
                    for address, key, op, fields in self.history
                    if address == ('', 'STATE_DB', 'TRANSCEIVER_STATUS_SW')
                    and key == lport and op == SET_COMMAND
                    and 'cmis_state' in fields]


DATABASE = MemoryDatabase()


class DBConnector:
    """Database identity only; construction never opens a socket."""

    def __init__(self, db_name, timeout=0, is_tcp_conn=False, namespace=''):
        self.db_name = DB_NAMES.get(db_name, db_name)
        if self.db_name not in DB_NAMES.values():
            raise ValueError('Unsupported test database: {}'.format(db_name))
        self.namespace = namespace

    def hget(self, key, field):
        """Read a Redis-style hash field through a table handle."""
        separator = ':' if self.db_name == 'APPL_DB' else '|'
        table, row = key.split(separator, 1)
        found, value = Table(self, table).hget(row, field)
        return value if found else None


class Table:
    """Shared hash table with merge, delete and snapshot-read semantics."""

    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.address = (db.namespace, db.db_name, table_name)

    def set(self, key, fvs):
        """Merge fields atomically and publish the resulting row."""
        fields = dict(fvs)
        if not isinstance(key, str) or any(
                not isinstance(field, str) or not isinstance(value, str)
                for field, value in fields.items()):
            raise TypeError('Table keys, fields and values must be strings')
        with DATABASE.condition:
            row = DATABASE.rows[self.address].setdefault(key, {})
            row.update(fields)
            DATABASE.publish(self.address, key, SET_COMMAND, row)

    def get(self, key):
        """Return (found, field/value pairs), copying the row."""
        with DATABASE.condition:
            row = DATABASE.rows[self.address].get(key)
            return (False, []) if row is None else (True, list(row.items()))

    def hget(self, key, field):
        """Return (found, value) for a single field."""
        _, fvs = self.get(key)
        row = dict(fvs)
        return (True, row[field]) if field in row else (False, '')

    def hdel(self, key, field):
        """Delete a field, deleting the row when its last field is removed."""
        with DATABASE.condition:
            row = DATABASE.rows[self.address].get(key)
            if row is not None and field in row:
                del row[field]
                if row:
                    DATABASE.publish(self.address, key, SET_COMMAND, row)
                else:
                    self._del(key)

    def _del(self, key):
        with DATABASE.condition:
            if key in DATABASE.rows[self.address]:
                del DATABASE.rows[self.address][key]
                DATABASE.publish(self.address, key, DEL_COMMAND, {})

    def getKeys(self):
        """Return a snapshot of existing row keys."""
        with DATABASE.condition:
            return list(DATABASE.rows[self.address])


ProducerStateTable = Table


class SubscriberStateTable(Table):
    """A table subscriber with its own notification queue."""

    def __init__(self, db, table_name):
        super().__init__(db, table_name)
        with DATABASE.condition:
            self.events = deque(
                (key, SET_COMMAND, list(row.items()))
                for key, row in DATABASE.rows[self.address].items())
            DATABASE.subscribers[self.address].append(self)
            DATABASE.condition.notify_all()

    def pop(self):
        """Pop one notification, or the swsscommon empty sentinel."""
        with DATABASE.condition:
            return self.events.popleft() if self.events else ('', '', [])


class Select:
    """Wait for queued notifications without spinning or faking time."""

    OBJECT = 0
    ERROR = 1
    TIMEOUT = 2

    def __init__(self):
        self.selectables = []

    def addSelectable(self, selectable):
        """Register a subscriber to wait on."""
        self.selectables.append(selectable)

    def select(self, timeout):
        """Honor the caller's timeout in milliseconds."""
        deadline = time.monotonic() + timeout / 1000
        with DATABASE.condition:
            while True:
                for selectable in self.selectables:
                    if selectable.events:
                        return self.OBJECT, selectable
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.TIMEOUT, None
                DATABASE.condition.wait(remaining)


class SonicV2Connector:
    """Hash operations used by sonic-py-common's runtime logger config."""

    def __init__(self, use_unix_socket_path=False, namespace=''):
        self.namespace = namespace
        self.connections = {}

    def connect(self, db_name):
        """Create an in-memory connector."""
        self.connections[db_name] = DBConnector(db_name, namespace=self.namespace)

    def get(self, db_name, key, field):
        """Read a hash field from a connected database."""
        return self.connections[db_name].hget(key, field)

    def hmset(self, db_name, key, fields):
        """Merge fields in a connected database."""
        db = self.connections[db_name]
        separator = ':' if db.db_name == 'APPL_DB' else '|'
        table, row = key.split(separator, 1)
        Table(db, table).set(row, fields.items())


class ConfigDBConnector(DBConnector):
    """CONFIG_DB accessors imported by sonic-py-common."""

    def __init__(self, namespace=''):
        super().__init__('CONFIG_DB', namespace=namespace)

    def connect(self, wait_for_init=True):
        """No connection is needed for in-memory storage."""

    def get_entry(self, table, key):
        """Return a copy of a configuration row."""
        _, fields = Table(self, table).get(key)
        return dict(fields)

    def get_table(self, table):
        """Return copies of all configuration rows."""
        handle = Table(self, table)
        return {key: self.get_entry(table, key) for key in handle.getKeys()}
