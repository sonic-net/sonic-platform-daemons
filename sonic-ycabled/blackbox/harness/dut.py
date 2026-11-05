"""One test's dual ToR device: its configuration, the ycabled under test, and
stand-ins for everything ycabled talks to.

- orchagent and linkmgrd: `toggle`, `peer_toggle`, `probe`, `fwd_probe`
- xcvrd: `insert`, `remove`
- the `show/config muxcable` CLI: `cli`
- the NIC SoCs: `soc`, `blackhole`
- the cables: `hw` (harness/fake_hw.py)

Requests wait for ycabled's answer in redis, by watching its writes in the
MONITOR stream, so a test sees exactly what ycabled wrote and when.
"""

import json
import os
import shutil
from collections import namedtuple

from . import contract
from .mux_simulator import MuxSimulator, UPPER_TOR
from .soc import Blackhole, Soc
from .topology import transceiver_info_row
from .util import clear_dir, wait_until
from .ycabled import YcabledProcess, implementation

# Tables ycabled subscribes to once it has found a mux cable.
MUX_SUBSCRIPTIONS = [("APPL_DB", t) for t in ("HW_MUX_CABLE_TABLE", "MUX_CABLE_COMMAND_TABLE",
                                              "FORWARDING_STATE_COMMAND", "HW_FORWARDING_STATE_PEER")]
MUX_SUBSCRIPTIONS += [("APPL_DB", cmd) for cmd, *_ in contract.CLI_TABLES]
MUX_SUBSCRIPTIONS += [("APPL_DB", "XCVRD_DOWN_FW_STATUS_CMD"), ("CONFIG_DB", "XCVRD_LOG")]
BASE_SUBSCRIPTIONS = [("STATE_DB", "TRANSCEIVER_INFO")]

# The field each CLI command writes into its command table.
CLI_COMMAND_FIELDS = {
    "XCVRD_SHOW_HWMODE_DIR_CMD": "state",
    "XCVRD_CONFIG_HWMODE_DIR_CMD": "config",
    "XCVRD_SHOW_HWMODE_SWMODE_CMD": "state",
    "XCVRD_CONFIG_HWMODE_SWMODE_CMD": "config",
    "XCVRD_SHOW_FW_CMD": "firmware_version",
    "XCVRD_DOWN_FW_CMD": "download_firmware",
    "XCVRD_ACTI_FW_CMD": "activate_firmware",
    "XCVRD_ROLL_FW_CMD": "rollback_firmware",
    "XCVRD_CONFIG_PRBS_CMD": "config_prbs",
    "XCVRD_CONFIG_LOOP_CMD": "config_loop",
    "XCVRD_EVENT_LOG_CMD": "show_event",
    "XCVRD_GET_FEC_CMD": "get_fec",
    "XCVRD_GET_BER_CMD": "get_ber",
}
CLI_SPECS = {cmd: (arg, rsp, rsp_field, res) for cmd, arg, rsp, rsp_field, _, res, _ in contract.CLI_TABLES}

FIRMWARE_DIR = "/usr/share/sonic/firmware"
GRPC_SECRETS = "/etc/sonic/grpc_secrets.json"
MUX_SIMULATOR_CONFIG = "/etc/sonic/mux_simulator.json"

# Signs that the daemon crashed or a worker died.
CRASH_MARKERS = ("Traceback (most recent call last)", "Exception occured at child thread", "panicked at")

CliResult = namedtuple("CliResult", "rsp res cmd_deleted")


class Dut(object):
    def __init__(self, env, name):
        self.env = env
        self.name = name
        self.dbs = env.dbs
        self.appl = env.dbs.appl
        self.config = env.dbs.config
        self.state = env.dbs.state
        self.hw = env.hw
        self.monitor = env.monitor
        self.syslog = env.syslog
        self.topo = None
        self.proc = None
        self.procs = []
        self.socs = {}
        self.blackholes = []
        self.simulators = []
        self.files = []
        self.test_mark = env.monitor.mark()
        self.log_mark = env.syslog.mark()
        self.start_mark = None
        self.stopped = False
        # Set by tests that expect the daemon to exit, or a worker to crash.
        self.allow_exit = False
        self.allow_crash = False
        self.allow_contract_violations = False
        self.torn_down = False

    # Configuration ----------------------------------------------------------

    def configure(self, topo):
        self.topo = topo
        topo.apply(self.dbs, self.hw)
        return topo

    def soc(self, ip, tls=None, start=True):
        soc = Soc(ip, tls)
        if start:
            soc.start()
        self.socs[ip] = soc
        return soc

    def blackhole(self, ip):
        bh = Blackhole(ip)
        self.blackholes.append(bh)
        return bh

    def mux_simulator(self, side=UPPER_TOR):
        """Start a mux simulator and point /etc/sonic/mux_simulator.json at it."""
        sim = MuxSimulator().start()
        self.simulators.append(sim)
        self.write_file(MUX_SIMULATOR_CONFIG, sim.config(side))
        return sim

    def write_file(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not isinstance(content, (str, bytes)):
            content = json.dumps(content)
        with open(path, "w" if isinstance(content, str) else "wb") as f:
            f.write(content)
        self.files.append(path)
        return path

    def firmware_file(self, name, content="bb firmware"):
        return self.write_file(os.path.join(FIRMWARE_DIR, name), content)

    # The daemon -------------------------------------------------------------

    def start(self, ready=True, expect_mux=None, timeout=20, extra_env=None, publish_transceivers=True):
        if publish_transceivers and self.topo is not None:
            self.topo.publish_transceivers(self.dbs, self.hw)
        self.start_mark = self.monitor.mark()
        self.stopped = False
        platform = self.topo.platform if self.topo else ""
        self.proc = YcabledProcess(self.env, platform, self.env.coverage, extra_env)
        self.proc.start("{}-{}".format(self.name, len(self.procs)))
        self.procs.append(self.proc)
        if ready:
            if expect_mux is None:
                expect_mux = bool(self.topo and self.topo.mux)
            self.wait_ready(expect_mux, timeout)
        return self.proc

    def subscriptions(self, since=None):
        since = self.start_mark if since is None else since
        return {(a.db, a.table) for a in self.monitor.accesses(since, kind="subscribe")}

    def wait_ready(self, expect_mux=True, timeout=20):
        """ycabled has subscribed to the tables it listens to, and gone quiet."""
        expected = set(BASE_SUBSCRIPTIONS + (MUX_SUBSCRIPTIONS if expect_mux else []))

        def ready():
            if not self.proc.running():
                raise AssertionError("ycabled exited with {} during startup:\n{}".format(
                    self.proc.returncode(), self.proc.output()[-3000:]))
            return expected <= self.subscriptions()
        wait_until(ready, timeout, message=lambda: "ycabled did not subscribe to {} within {}s".format(
            sorted(expected - self.subscriptions()), timeout))
        self.monitor.wait_settled(quiet=0.3, timeout=timeout)

    def stop(self, sigterm_timeout=3.0):
        """SIGTERM as supervisord sends it; SIGKILL after `sigterm_timeout`."""
        self.stopped = True
        result = self.proc.stop(sigterm_timeout)
        self._settle_recorders()
        return result

    def _settle_recorders(self):
        """Once ycabled has stopped: wait until what it sent to redis and syslog
        is recorded, so that none of it is missed or taken for the next test's."""
        if self.monitor.error is None:
            self.monitor.barrier()
        self.syslog.barrier()

    # orchagent, linkmgrd, xcvrd ---------------------------------------------

    def _request(self, write, wait_table, wait_db, port, wait_field=None, timeout=5, wait=True):
        mark = self.monitor.mark()
        write()
        if not wait:
            return mark
        self.monitor.wait_access(mark, timeout, kind="write", db=wait_db, table=wait_table, key=port,
                                 field=wait_field)
        return mark

    def toggle(self, port, state, timeout=5, wait=True):
        """orchagent: APPL_DB HW_MUX_CABLE_TABLE. Returns STATE_DB HW_MUX_CABLE_TABLE."""
        self._request(lambda: self.appl.set("HW_MUX_CABLE_TABLE", port, {"state": state}),
                      "HW_MUX_CABLE_TABLE", "STATE_DB", port, "state", timeout, wait)
        return self.state.get("HW_MUX_CABLE_TABLE", port)

    def peer_toggle(self, port, state, timeout=5, wait=True):
        """linkmgrd: APPL_DB HW_FORWARDING_STATE_PEER. Returns STATE_DB HW_MUX_CABLE_TABLE_PEER."""
        self._request(lambda: self.appl.set("HW_FORWARDING_STATE_PEER", port, {"state": state}),
                      "HW_MUX_CABLE_TABLE_PEER", "STATE_DB", port, "state", timeout, wait)
        return self.state.get("HW_MUX_CABLE_TABLE_PEER", port)

    def probe(self, port, timeout=5, wait=True):
        """linkmgrd (active-standby): MUX_CABLE_COMMAND_TABLE probe. Returns the response."""
        self._request(lambda: self.appl.set("MUX_CABLE_COMMAND_TABLE", port, {"command": "probe"}),
                      "MUX_CABLE_RESPONSE_TABLE", "APPL_DB", port, "response", timeout, wait)
        return self.appl.field("MUX_CABLE_RESPONSE_TABLE", port, "response")

    def fwd_probe(self, port, timeout=5, wait=True):
        """linkmgrd (active-active): FORWARDING_STATE_COMMAND probe. Returns the response row."""
        self._request(lambda: self.appl.set("FORWARDING_STATE_COMMAND", port, {"command": "probe"}),
                      "FORWARDING_STATE_RESPONSE", "APPL_DB", port, "response_peer", timeout, wait)
        return self.appl.get("FORWARDING_STATE_RESPONSE", port)

    def insert(self, port, row=None):
        """xcvrd: the cable is inserted."""
        model = self.hw.port(self.topo.index(port))
        model.present = True
        self.state.set("TRANSCEIVER_INFO", port, row or transceiver_info_row(model))

    def remove(self, port):
        """xcvrd: the cable is removed."""
        self.hw.port(self.topo.index(port)).present = False
        self.state.delete("TRANSCEIVER_INFO", port)

    # The CLI ----------------------------------------------------------------

    def cli(self, cmd_table, value, port, args=None, timeout=10, wait=True):
        """What `show/config muxcable` does: clear the tables, write the
        arguments and the command, and wait for the response."""
        arg_table, rsp_table, rsp_field, res_table = CLI_SPECS[cmd_table]
        self.appl.delete(cmd_table, port)
        self.state.delete(rsp_table, port)
        if arg_table:
            self.appl.delete(arg_table, port)
        if res_table:
            self.state.delete(res_table, port)
        mark = self.monitor.mark()
        for k, v in (args or {}).items():
            self.appl.set(arg_table, port, {k: v})
        self.appl.set(cmd_table, port, {CLI_COMMAND_FIELDS[cmd_table]: value})
        if not wait:
            return mark
        self.monitor.wait_access(mark, timeout, kind="write", db="STATE_DB", table=rsp_table, key=port,
                                 field=rsp_field)
        deleted = wait_until(lambda: self.monitor.accesses(mark, kind="delete", db="APPL_DB", table=cmd_table,
                                                           key=port), 2, message="command key not deleted")
        return CliResult(self.state.get(rsp_table, port),
                         self.state.get(res_table, port) if res_table else {}, bool(deleted))

    def tolerate_python_failure(self, crash=False, exit=False, contract=False):
        """For a known issue of Python ycabled: on Python, let the teardown
        accept the crash, exit or contract violation the issue causes. Other
        implementations still get every check."""
        if implementation() == "python":
            self.allow_crash |= crash
            self.allow_exit |= exit
            self.allow_contract_violations |= contract

    def crash_evidence(self):
        """Signs of a crash since the test started: in syslog or in the
        output of the daemon's processes."""
        found = [m.text for m in self.syslog.messages(self.log_mark)
                 if any(marker in m.text for marker in CRASH_MARKERS)]
        for p in self.procs:
            out = p.output()
            found += [marker for marker in CRASH_MARKERS if marker in out]
        return found

    # Observation ------------------------------------------------------------

    def writes(self, since=None, **match):
        since = self.start_mark if since is None else since
        return self.monitor.accesses(since, kind="write", **match)

    def logs(self, since=None, **match):
        return self.syslog.messages(self.log_mark if since is None else since, **match)

    # Teardown ---------------------------------------------------------------

    def teardown(self):
        if self.torn_down:
            return []
        self.torn_down = True
        problems = []
        if self.proc is not None:
            if not self.proc.running() and not self.stopped and not self.allow_exit:
                problems.append("ycabled exited unexpectedly with {}".format(self.proc.returncode()))
            self.proc.kill()
            try:
                self._settle_recorders()
            except RuntimeError as e:
                problems.append(str(e))
            if not self.allow_crash:
                for p in self.procs:
                    out = p.output()
                    for marker in CRASH_MARKERS:
                        if marker in out:
                            problems.append("ycabled output has {!r}:\n{}".format(marker, out[-3000:]))
                            break
                for m in self.syslog.messages(self.log_mark):
                    if any(marker in m.text for marker in CRASH_MARKERS):
                        problems.append("ycabled logged a crash: {}".format(m.text[:2000]))
        if self.monitor.error is not None:
            problems.append("redis MONITOR stopped recording: {!r}".format(self.monitor.error))
        elif not self.allow_contract_violations:
            for v in contract.violations(self.monitor.accesses(self.test_mark)):
                problems.append("DB contract: " + v)
        for soc in self.socs.values():
            soc.stop()
        for bh in self.blackholes:
            bh.stop()
        for sim in self.simulators:
            sim.stop()
        self.hw.reset()
        self.env.redis.flushall()
        for path in self.files + [GRPC_SECRETS, MUX_SIMULATOR_CONFIG]:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.unlink(path)
        clear_dir(FIRMWARE_DIR)
        return problems
