"""Receives what ycabled sends to syslog. /dev/log points at the sink's socket
(env.BlackboxEnv.prepare_system)."""

import os
import re
import socket
import threading
import time
from collections import namedtuple

from .util import wait_until

DEV_LOG = "/dev/log"

SEVERITIES = ["EMERG", "ALERT", "CRIT", "ERR", "WARNING", "NOTICE", "INFO", "DEBUG"]

# glibc: "<PRI>Mmm dd hh:mm:ss ident[pid]: message", with [pid] only for LOG_PID.
_LINE_RE = re.compile(r'^<(\d+)>(\w{3} [ \d]\d \d{2}:\d{2}:\d{2}) ([^:\[\s]*)(?:\[(\d+)\])?: ?(.*)$', re.S)

Message = namedtuple("Message", "seq ts facility severity ident pid text raw")


class SyslogSink(object):
    def __init__(self, path):
        self.path = path
        self._msgs = []
        self._sock = None
        self._stop = False

    def start(self):
        if os.path.exists(self.path) or os.path.islink(self.path):
            os.unlink(self.path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(self.path)
        threading.Thread(target=self._run, name="syslog-sink", daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                data = self._sock.recv(1 << 16)
            except OSError:
                return
            raw = data.decode("utf-8", "replace").rstrip("\n\x00")
            m = _LINE_RE.match(raw)
            if m:
                pri, _, ident, pid, text = m.groups()
                pri = int(pri)
                msg = Message(len(self._msgs), time.time(), pri >> 3, SEVERITIES[pri & 7], ident,
                              int(pid) if pid else None, text, raw)
            else:
                msg = Message(len(self._msgs), time.time(), None, None, None, None, raw, raw)
            self._msgs.append(msg)

    def stop(self):
        self._stop = True
        if self._sock is not None:
            self._sock.close()

    def mark(self):
        return len(self._msgs)

    def messages(self, since=0, severity=None, contains=None, regex=None):
        out = []
        pattern = re.compile(regex) if regex else None
        for m in self._msgs[since:]:
            if severity is not None and m.severity != severity:
                continue
            if contains is not None and contains not in m.text:
                continue
            if pattern is not None and not pattern.search(m.text):
                continue
            out.append(m)
        return out

    def wait_for(self, since=0, timeout=10, **match):
        return wait_until(lambda: self.messages(since, **match), timeout,
                          message="no syslog message {} within {}s".format(match, timeout))
