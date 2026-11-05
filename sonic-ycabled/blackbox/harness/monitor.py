"""Records every redis command ycabled sends, with redis's MONITOR.

The suite's connections come from 127.0.0.2 (redisdb.HARNESS_ADDRESS), so the
client address in each MONITOR line says whose command it is. The recorded
commands are turned into table-level accesses (reads, writes, deletes,
subscriptions), which the tests and the DB contract check work with.
"""

import re
import threading
import time
from collections import namedtuple

from .redisdb import HARNESS_ADDRESS, harness_client
from .util import DB_NAMES, SEPARATORS, wait_until

_LINE_RE = re.compile(r'^(\d+\.\d+) \[(\d+) ([^\]]+)\] (.*)$', re.S)
_ARG_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", '"': '"', "\\": "\\"}
_KEYSPACE_RE = re.compile(r'^__keyspace@(\d+)__:(.*)$')

Op = namedtuple("Op", "seq ts db client cmd args")
Access = namedtuple("Access", "seq ts kind db table key field value")

READ_CMDS = {"HGETALL", "HGET", "HMGET", "HEXISTS", "EXISTS", "HKEYS", "HVALS", "HLEN", "TYPE", "GET", "KEYS", "SCAN"}
IGNORED_CMDS = {"SELECT", "PING", "CLIENT", "HELLO", "INFO", "AUTH", "COMMAND", "MULTI", "EXEC", "DISCARD",
                "PUNSUBSCRIBE", "UNSUBSCRIBE", "SCRIPT", "QUIT"}


def _unescape(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "x" and i + 3 < len(s):
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(_ESCAPES.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_line(seq, line):
    m = _LINE_RE.match(line)
    if not m:
        return None
    ts, db, client, rest = m.groups()
    args = [_unescape(a) for a in _ARG_RE.findall(rest)]
    if not args:
        return None
    if client.startswith(HARNESS_ADDRESS + ":"):
        kind = "harness"
    elif client.startswith("unix:"):
        kind = "unix"
    elif client == "lua":
        kind = "lua"
    else:
        kind = "tcp"
    return Op(seq, float(ts), int(db), kind, args[0].upper(), args[1:])


def split_key(db, key):
    """(table, key) of a redis key in a SONiC database."""
    sep = SEPARATORS.get(db, "|")
    if sep in key:
        table, _, rest = key.partition(sep)
        return table, rest
    return key, None


def accesses(op):
    """The table-level accesses one command makes."""
    cmd, args, db = op.cmd, op.args, op.db

    def acc(kind, key, field=None, value=None, dbid=db):
        table, k = split_key(dbid, key)
        return Access(op.seq, op.ts, kind, DB_NAMES.get(dbid, str(dbid)), table, k, field, value)

    if cmd in ("HSET", "HMSET", "HSETNX"):
        key = args[0]
        for i in range(1, len(args) - 1, 2):
            yield acc("write", key, args[i], args[i + 1])
    elif cmd == "HDEL":
        for f in args[1:]:
            yield acc("hdel", args[0], f)
    elif cmd in ("DEL", "UNLINK"):
        for k in args:
            yield acc("delete", k)
    elif cmd in ("SET", "SETNX"):
        yield acc("write", args[0], None, args[1] if len(args) > 1 else None)
    elif cmd == "KEYS":
        yield acc("read", args[0].rstrip("*"), None)
    elif cmd in ("HGET", "HEXISTS"):
        yield acc("read", args[0], args[1] if len(args) > 1 else None)
    elif cmd in READ_CMDS:
        if args:
            yield acc("read", args[0])
    elif cmd in ("PSUBSCRIBE", "SUBSCRIBE"):
        for pattern in args:
            m = _KEYSPACE_RE.match(pattern)
            if m:
                dbid = int(m.group(1))
                yield acc("subscribe", m.group(2).rstrip("*"), dbid=dbid)
            else:
                yield Access(op.seq, op.ts, "subscribe", "PUBSUB", pattern, None, None, None)
    elif cmd in IGNORED_CMDS:
        return
    else:
        yield Access(op.seq, op.ts, "other:" + cmd, DB_NAMES.get(db, str(db)), " ".join(args[:1]), None, None, None)


class Monitor(object):
    def __init__(self):
        self._ops = []
        self._lines = 0
        self._stop = False
        self._thread = None
        self._conn = None
        self._ready = threading.Event()

    def start(self):
        # Keep the client: when it is collected, its pool closes the MONITOR connection.
        self._client = harness_client(decode_responses=False)
        self._mon = self._client.monitor()
        self._mon.__enter__()
        self._conn = self._mon.connection
        self._thread = threading.Thread(target=self._run, name="redis-monitor", daemon=True)
        self._thread.start()
        # MONITOR shows this marker once it is active.
        probe = harness_client()
        wait_until(lambda: probe.echo("bb-monitor-ready") and self._lines > 0, 5, 0.02,
                   "redis MONITOR did not start")

    def _run(self):
        seq = 0
        while not self._stop:
            try:
                raw = self._conn.read_response()
            except Exception:
                if self._stop:
                    return
                raise
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            self._lines += 1
            op = parse_line(seq, line)
            if op is not None:
                self._ops.append(op)
                seq += 1

    def stop(self):
        self._stop = True
        try:
            self._conn.disconnect()
        except Exception:
            pass

    # Access for tests -------------------------------------------------------

    def mark(self):
        return len(self._ops)

    def ops(self, since=0, clients=("tcp", "unix", "lua")):
        return [op for op in self._ops[since:] if op.client in clients]

    def accesses(self, since=0, kind=None, db=None, table=None, key=None, field=None):
        out = []
        for op in self.ops(since):
            for a in accesses(op):
                if kind is not None and a.kind != kind:
                    continue
                if db is not None and a.db != db:
                    continue
                if table is not None and a.table != table:
                    continue
                if key is not None and a.key != key:
                    continue
                if field is not None and a.field != field:
                    continue
                out.append(a)
        return out

    def wait_access(self, since, timeout=10, **match):
        """Wait until ycabled makes an access matching `match` after `since`."""
        return wait_until(lambda: self.accesses(since, **match), timeout,
                          message="ycabled made no access {} within {}s".format(match, timeout))

    def wait_settled(self, quiet=0.3, timeout=10):
        """Wait until ycabled has sent no command for `quiet` seconds."""
        start = self.mark()
        deadline = time.monotonic() + timeout
        last = 0
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.05)
            cur = len(self.ops(start))
            if cur != last:
                last = cur
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet:
                return True
        return False
