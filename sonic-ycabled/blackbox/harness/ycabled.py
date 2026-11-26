"""Starts and stops the ycabled implementation under test.

By default that is Python ycabled from this source tree. BB_YCABLED_CMD runs
something else instead, for example the Rust implementation:

    BB_YCABLED_CMD=/usr/bin/ycabled-rs

Either way the process gets the same environment: the fake platform and
vendor driver on PYTHONPATH, PLATFORM from the test's topology, and the fake
hardware socket.
"""

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import namedtuple

StopResult = namedtuple("StopResult", "returncode exited_on_sigterm sigterm_to_exit")

# How often coverage data is saved while ycabled runs, to keep what it
# measured when the process kills itself.
COVERAGE_SAVE_PERIOD = 3

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))


def implementation():
    return "custom" if os.environ.get("BB_YCABLED_CMD") else "python"


class YcabledProcess(object):
    def __init__(self, env, platform, coverage=None, extra_env=None):
        self.env = env
        self.platform = platform
        # An env.CoverageTarget: run Python ycabled under coverage.py.
        self.coverage = coverage
        self.extra_env = dict(extra_env or {})
        self.proc = None
        self.log_path = None
        self.started_at = None

    def command(self):
        custom = os.environ.get("BB_YCABLED_CMD")
        if custom:
            return shlex.split(custom)
        if self.coverage is not None:
            return [sys.executable, os.path.join(HARNESS_DIR, "cov_main.py")]
        return [sys.executable, "-c",
                "import sys; sys.argv = ['/usr/local/bin/ycabled']; from ycable.ycable import main; main()"]

    def process_env(self):
        e = dict(self.env.base_env)
        e.pop("BB_YCABLED_CMD", None)
        paths = [self.env.overlay_dir, self.env.pylib_dir, self.env.gen_dir, self.env.src_dir]
        if e.get("PYTHONPATH"):
            paths.append(e["PYTHONPATH"])
        e["PYTHONPATH"] = ":".join(paths)
        e["PLATFORM"] = self.platform
        e["BB_HW_SOCKET"] = self.env.hw.socket_path
        e["PYTHONUNBUFFERED"] = "1"
        if self.coverage is not None:
            e["BB_COV_DATA_FILE"] = self.coverage.data_file
            e["BB_COV_BRANCH"] = "1" if self.coverage.branch else "0"
            e["BB_COV_SOURCE"] = os.path.join(self.env.src_dir, "ycable")
            e["BB_COV_SAVED_DIR"] = self.env.coverage_saved_dir
        e.update(self.extra_env)
        return e

    def start(self, name):
        self.log_path = os.path.join(self.env.workdir, "ycabled-{}.log".format(name))
        log = open(self.log_path, "w")
        self.started_at = time.time()
        self.proc = subprocess.Popen(self.command(), env=self.process_env(), stdout=log,
                                     stderr=subprocess.STDOUT, cwd="/")
        if self.coverage is not None and implementation() == "python":
            threading.Thread(target=self._save_periodically, daemon=True).start()
        return self

    def _coverage_ready(self):
        """cov_main.py has its SIGUSR1 handler in place."""
        return os.path.exists(os.path.join(self.env.coverage_saved_dir, "{}.ready".format(self.proc.pid)))

    def _save_periodically(self):
        proc = self.proc
        while proc.poll() is None:
            time.sleep(COVERAGE_SAVE_PERIOD)
            if not self._coverage_ready():
                continue
            try:
                os.kill(proc.pid, signal.SIGUSR1)
            except ProcessLookupError:
                return

    @property
    def pid(self):
        return self.proc.pid

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def returncode(self):
        return self.proc.poll()

    def wait_exit(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def output(self):
        if not self.log_path or not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, errors="replace") as f:
            return f.read()

    def save_coverage(self, timeout=5):
        if not (self.coverage is not None and self.running() and implementation() == "python"):
            return
        if not self._coverage_ready():
            # Still starting, so nothing of ycabled measured yet; and SIGUSR1
            # would kill it.
            return
        marker = os.path.join(self.env.coverage_saved_dir, str(self.proc.pid))
        if os.path.exists(marker):
            os.unlink(marker)
        try:
            os.kill(self.proc.pid, signal.SIGUSR1)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not os.path.exists(marker):
            time.sleep(0.05)

    def stop(self, sigterm_timeout=3.0):
        """SIGTERM, then SIGKILL if the process is still running after
        `sigterm_timeout`. Says whether SIGTERM alone ended it."""
        if self.proc is None:
            return StopResult(None, False, None)
        if not self.running():
            return StopResult(self.proc.returncode, False, None)
        t0 = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)
        rc = self.wait_exit(sigterm_timeout)
        if rc is not None:
            return StopResult(rc, True, time.monotonic() - t0)
        self.save_coverage()
        self.proc.kill()
        self.proc.wait(5)
        return StopResult(self.proc.returncode, False, None)

    def kill(self):
        if self.running():
            self.save_coverage()
            self.proc.kill()
            self.proc.wait(5)
