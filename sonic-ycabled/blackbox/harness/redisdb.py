"""redis-server for the suite, and table-level access to the SONiC databases.

The suite connects over TCP from source address 127.0.0.2. ycabled connects
from 127.0.0.1 or over the unix socket, as database_config.json and its
libraries choose, which is how the recorder in `monitor.py` tells ycabled's
commands from the suite's own.
"""

import os
import socket
import subprocess

import redis

from .util import APPL_DB, CONFIG_DB, SEPARATORS, STATE_DB, wait_until

UNIX_SOCKET = "/var/run/redis/redis.sock"
TCP_PORT = 6379
HARNESS_ADDRESS = "127.0.0.2"


class HarnessConnection(redis.Connection):
    """A TCP connection from HARNESS_ADDRESS."""

    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.bind((HARNESS_ADDRESS, 0))
            sock.settimeout(self.socket_connect_timeout)
            sock.connect((self.host, self.port))
            sock.settimeout(self.socket_timeout)
        except OSError:
            sock.close()
            raise
        return sock


def harness_client(db=0, decode_responses=True, port=TCP_PORT):
    # Blocking reads, as in redis-py before 8 (which reads with a 5 s timeout):
    # the MONITOR connection is idle while ycabled is. The suite's waits have
    # their own timeouts.
    pool = redis.ConnectionPool(connection_class=HarnessConnection, host="127.0.0.1", port=port, db=db,
                                decode_responses=decode_responses, socket_timeout=None)
    return redis.Redis(connection_pool=pool)


class RedisServer(object):
    def __init__(self, workdir, unix_socket=UNIX_SOCKET, port=TCP_PORT):
        self.workdir = workdir
        self.unix_socket = unix_socket
        self.port = port
        self.proc = None

    def start(self):
        os.makedirs(os.path.dirname(self.unix_socket), exist_ok=True)
        os.makedirs(self.workdir, exist_ok=True)
        log = open(os.path.join(self.workdir, "redis-server.log"), "w")
        self.proc = subprocess.Popen(
            ["redis-server",
             "--bind", "127.0.0.1",
             # Protected mode accepts 127.0.0.1 but not the suite's 127.0.0.2.
             "--protected-mode", "no",
             "--port", str(self.port),
             "--unixsocket", self.unix_socket,
             "--unixsocketperm", "777",
             "--databases", "16",
             "--notify-keyspace-events", "AKE",
             "--save", "",
             "--appendonly", "no",
             "--dir", self.workdir],
            stdout=log, stderr=subprocess.STDOUT)
        wait_until(self._ping, 10, message="redis-server did not start")

    def _ping(self):
        if self.proc.poll() is not None:
            raise RuntimeError("redis-server exited with {}".format(self.proc.returncode))
        try:
            return harness_client(port=self.port).ping()
        except redis.exceptions.ConnectionError:
            return False

    def client(self, db):
        return harness_client(db, port=self.port)

    def flushall(self):
        harness_client(port=self.port).flushall()

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Db(object):
    """One SONiC database, addressed by table and key."""

    def __init__(self, server, db_id):
        self.id = db_id
        self.sep = SEPARATORS[db_id]
        self.r = server.client(db_id)

    def key(self, table, key):
        return "{}{}{}".format(table, self.sep, key)

    def set(self, table, key, fields=None):
        """HSET the fields into the row, as swsscommon's Table.set does."""
        fields = dict(fields or {"NULL": "NULL"})
        self.r.hset(self.key(table, key), mapping={k: str(v) for k, v in fields.items()})

    def get(self, table, key):
        return self.r.hgetall(self.key(table, key))

    def field(self, table, key, field):
        return self.r.hget(self.key(table, key), field)

    def exists(self, table, key):
        return bool(self.r.exists(self.key(table, key)))

    def delete(self, table, key):
        self.r.delete(self.key(table, key))

    def keys(self, table):
        prefix = table + self.sep
        return sorted(k[len(prefix):] for k in self.r.keys(prefix + "*"))

    def table(self, table):
        return {k: self.get(table, k) for k in self.keys(table)}


class Dbs(object):
    def __init__(self, server):
        self.server = server
        self.appl = Db(server, APPL_DB)
        self.config = Db(server, CONFIG_DB)
        self.state = Db(server, STATE_DB)

    def snapshot(self):
        """All keys of APPL_DB, CONFIG_DB and STATE_DB, for diagnostics."""
        out = {}
        for name, db in (("APPL_DB", self.appl), ("CONFIG_DB", self.config), ("STATE_DB", self.state)):
            out[name] = {k: db.r.hgetall(k) if db.r.type(k) == "hash" else db.r.get(k)
                         for k in sorted(db.r.keys("*"))}
        return out
