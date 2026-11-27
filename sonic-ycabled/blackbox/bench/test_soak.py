"""Soak test: mux ports toggled together once a minute for hours, while each
round is checked and the resources of the ycabled under test are sampled.

Not part of the suite or of the benchmarks: run it explicitly, with no or a
long per-test time limit, once per implementation:

    run.sh --bb-test-timeout=0 bench/test_soak.py
    BB_YCABLED_CMD=/src/target/release/ycabled \\
        run.sh --bb-test-timeout=0 bench/test_soak.py

Environment:
    BB_SOAK_SECONDS  how long each cable type is toggled (default 21600, 6 h)
    BB_SOAK_PERIOD   seconds from one round to the next (default 60)
    BB_SOAK_PORTS    mux ports (default 100)
    BB_SOAK_OUT      directory of the results (default: the report directory)
    BB_SOAK_GC_FREEZE  1: gc.freeze() after each round, so that the test
                     process's growing records do not make its full
                     collections, which stop its SoC doubles and syslog
                     reader, longer and longer

Each cable type writes soak-<implementation>-<cable type>.jsonl: a "start"
record, a "round" record per round, then an "end" record. Telemetry stays
enabled, as on a device.
"""

import gc
import hashlib
import json
import os
import re
import shlex
import statistics
import sys
import threading
import time

import pytest

from harness.fake_hw import TARGET_UPPER_TOR, TARGET_LOWER_TOR
from harness.redisdb import HARNESS_ADDRESS
from harness.topology import Topology, soc_ip
from harness.ycabled import implementation

SECONDS = float(os.environ.get("BB_SOAK_SECONDS", 6 * 3600))
PERIOD = float(os.environ.get("BB_SOAK_PERIOD", 60))
PORTS = int(os.environ.get("BB_SOAK_PORTS", 100))
ANSWER_TIMEOUT = 30
CLK_TCK = os.sysconf("SC_CLK_TCK")
SAMPLE_INTERVAL = 1.0
GC_FREEZE = os.environ.get("BB_SOAK_GC_FREEZE") == "1"
SEVERITIES = ("EMERG", "ALERT", "CRIT", "ERR", "WARNING", "NOTICE", "INFO", "DEBUG")


def impl_name():
    cmd = os.environ.get("BB_YCABLED_CMD", "")
    if implementation() == "python":
        return "python"
    name = os.path.basename(cmd.split()[0])
    # The Rust daemon's binary: ycabled, or ycabled-rs as it was first named.
    return "rust" if name in ("ycabled", "ycabled-rs") else name


def round_state(r):
    """The state requested in round `r`. The ports start active."""
    return "standby" if r % 2 == 0 else "active"


def opposite(state):
    return "active" if state == "standby" else "standby"


def _field(text, name):
    m = re.search(r"^{}:\s+(\d+)".format(name), text, re.M)
    return int(m.group(1)) if m else None


def proc_sample(pid):
    """Resources of process `pid`, or None once it is gone."""
    try:
        with open("/proc/{}/status".format(pid)) as f:
            status = f.read()
        with open("/proc/{}/smaps_rollup".format(pid)) as f:
            rollup = f.read()
        with open("/proc/{}/stat".format(pid)) as f:
            stat = f.read().rsplit(")", 1)[1].split()
        fds = os.listdir("/proc/{}/fd".format(pid))
    except OSError:
        return None
    sockets = 0
    for fd in fds:
        try:
            sockets += os.readlink("/proc/{}/fd/{}".format(pid, fd)).startswith("socket:")
        except OSError:
            pass
    # /proc/<pid>/status counts the main thread's switches only: add up the
    # threads alive now.
    vctx = nvctx = 0
    try:
        tids = os.listdir("/proc/{}/task".format(pid))
    except OSError:
        tids = []
    for tid in tids:
        try:
            with open("/proc/{}/task/{}/status".format(pid, tid)) as f:
                task = f.read()
        except OSError:
            continue
        vctx += _field(task, "voluntary_ctxt_switches") or 0
        nvctx += _field(task, "nonvoluntary_ctxt_switches") or 0
    io = {}
    try:
        with open("/proc/{}/io".format(pid)) as f:
            for line in f:
                k, v = line.split(":")
                io[k] = int(v)
    except OSError:
        pass
    return {
        "rss_kb": _field(status, "VmRSS"),
        "hwm_kb": _field(status, "VmHWM"),
        "anon_kb": _field(status, "RssAnon"),
        "pss_kb": _field(rollup, "Pss"),
        "uss_kb": (_field(rollup, "Private_Clean") or 0) + (_field(rollup, "Private_Dirty") or 0),
        "threads": _field(status, "Threads"),
        "fds": len(fds),
        "sockets": sockets,
        "utime": int(stat[11]),
        "stime": int(stat[12]),
        "vctx": vctx,
        "nvctx": nvctx,
        "syscr": io.get("syscr"),
        "syscw": io.get("syscw"),
    }


class PeakSampler(threading.Thread):
    """Peak threads and RSS of a process, every SAMPLE_INTERVAL; take() returns
    the peaks since the previous take()."""

    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.peak = {"threads": 0, "rss_kb": 0}

    def run(self):
        while not self.stopped.wait(SAMPLE_INTERVAL):
            try:
                with open("/proc/{}/status".format(self.pid)) as f:
                    status = f.read()
            except OSError:
                return
            with self.lock:
                self.peak["threads"] = max(self.peak["threads"], _field(status, "Threads") or 0)
                self.peak["rss_kb"] = max(self.peak["rss_kb"], _field(status, "VmRSS") or 0)

    def take(self):
        with self.lock:
            peak, self.peak = self.peak, {"threads": 0, "rss_kb": 0}
        return peak


def redis_sample(dut):
    clients = dut.config.r.client_list()
    return {
        "ycabled_conns": len([c for c in clients if not c["addr"].startswith(HARNESS_ADDRESS + ":")]),
        "used_memory": dut.config.r.info("memory")["used_memory"],
        "keys": {"appl": dut.appl.r.dbsize(), "config": dut.config.r.dbsize(), "state": dut.state.r.dbsize()},
    }


def table_counts(db):
    """Keys of a redis DB by table."""
    counts = {}
    for key in db.r.scan_iter(count=1000):
        key = key.decode() if isinstance(key, bytes) else key
        table = re.split(r"[:|]", key, maxsplit=1)[0]
        counts[table] = counts.get(table, 0) + 1
    return counts


def redis_tables(dut):
    return {"appl": table_counts(dut.appl), "config": table_counts(dut.config), "state": table_counts(dut.state)}


def lat_stats(values):
    if not values:
        return None
    values = sorted(values)
    return {"min": round(values[0] * 1000, 1), "p50": round(statistics.median(values) * 1000, 1),
            "p99": round(values[max(0, int(len(values) * 0.99) - 1)] * 1000, 1), "max": round(values[-1] * 1000, 1)}


class GcPauses(object):
    """The longest collection of the test process since the last take(), per
    generation: while it runs, the SoC doubles and the syslog reader stop."""

    def __init__(self):
        self.started = None
        self.longest = {}
        gc.callbacks.append(self.callback)

    def callback(self, phase, info):
        if phase == "start":
            self.started = time.perf_counter()
        elif self.started is not None:
            gen = info["generation"]
            self.longest[gen] = max(self.longest.get(gen, 0), time.perf_counter() - self.started)
            self.started = None

    def take(self):
        longest, self.longest = self.longest, {}
        return {str(g): round(s * 1000, 1) for g, s in sorted(longest.items())}

    def close(self):
        gc.callbacks.remove(self.callback)


def normalized(text):
    return re.sub(r"\d+", "#", text)[:200]


class Soak(object):
    """One cable type's soak: the device, the rounds' marks, the results."""

    def __init__(self, dut, cable_type, out):
        self.dut = dut
        self.cable_type = cable_type
        self.out = out
        self.ports = ["Ethernet{}".format(4 * i) for i in range(PORTS)]
        self.problems = []
        self.seen = {}

    def write(self, record):
        self.out.write(json.dumps(record, sort_keys=True) + "\n")
        self.out.flush()

    def configure(self):
        self.topo = Topology(nports=PORTS)
        self.read_sides = {}
        self.socs = {}
        if self.cable_type == "active-standby":
            self.topo.active_standby(*self.ports)
        else:
            for port in self.ports:
                self.topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
        self.dut.configure(self.topo)
        if self.cable_type == "active-standby":
            for i, port in enumerate(self.ports):
                cable = self.dut.hw.port(self.topo.index(port))
                cable.read_side = cable.mux_direction = TARGET_UPPER_TOR if i % 2 == 0 else TARGET_LOWER_TOR
                self.read_sides[port] = cable.read_side
        else:
            self.socs = {port: self.dut.soc(soc_ip(port)) for port in self.ports}

    def requests(self, state):
        if self.cable_type == "active-standby":
            return [("HW_MUX_CABLE_TABLE", port, state) for port in self.ports]
        return [(table, port, s) for port in self.ports
                for table, s in (("HW_MUX_CABLE_TABLE", state), ("HW_FORWARDING_STATE_PEER", opposite(state)))]

    def answer_tables(self, state):
        if self.cable_type == "active-standby":
            return {"HW_MUX_CABLE_TABLE": state}
        return {"HW_MUX_CABLE_TABLE": state, "HW_MUX_CABLE_TABLE_PEER": opposite(state)}

    def marks(self):
        return {"monitor": self.dut.monitor.mark(), "log": self.dut.syslog.mark(), "hw": self.dut.hw.mark(),
                "socs": {port: soc.mark() for port, soc in self.socs.items()}}

    def send(self, state):
        with self.dut.appl.r.pipeline(transaction=True) as p:
            for table, port, s in self.requests(state):
                p.hset("{}:{}".format(table, port), "state", s)
            p.execute()

    def answers(self, since, until, table):
        got = {}
        for a in self.dut.monitor.accesses(since, kind="write", db="STATE_DB", table=table, field="state"):
            if until is not None and a.seq >= until:
                break
            got.setdefault(a.key, []).append(a)
        return got

    def all_answered(self, since, state):
        return all(set(self.ports) <= set(self.answers(since, None, table))
                   for table in self.answer_tables(state))

    def request_ts(self, since, until):
        for op in self.dut.monitor.ops(since, clients=("harness",)):
            if until is not None and op.seq >= until:
                break
            if op.cmd == "HSET":
                return op.ts
        return None

    def check_window(self, r, ctx, nxt):
        """Everything of round `r` from its requests to the next round's
        (`nxt`, None for the end): answers, cable or SoC calls, logs."""
        state = ctx["state"]
        m0, m1 = ctx["marks"], nxt
        until = m1["monitor"] if m1 else None
        rec = {"type": "round", "round": r, "state": state, "t": ctx["t"], "complete_s": ctx["complete_s"]}
        t_req = self.request_ts(m0["monitor"], until)
        problems = []
        # An answer after the wait gave up still counts below: say the round was late.
        if ctx["complete_s"] is None:
            problems.append("not every port answered within {} s".format(ANSWER_TIMEOUT))
        for table, want in self.answer_tables(state).items():
            got = self.answers(m0["monitor"], until, table)
            missing = [p for p in self.ports if p not in got]
            wrong = {p: [a.value for a in got[p]] for p in self.ports if p in got and
                     [a.value for a in got[p]] != [want]}
            lat = [got[p][0].ts - t_req for p in self.ports if p in got and t_req is not None]
            rec[table] = {"answered": len(got), "missing": missing[:10], "n_missing": len(missing),
                          "wrong": dict(list(wrong.items())[:10]), "n_wrong": len(wrong), "lat_ms": lat_stats(lat)}
            if missing or wrong:
                problems.append("{}: {} missing, {} wrong".format(table, len(missing), len(wrong)))
        if self.cable_type == "active-standby":
            hw_end = m1["hw"] if m1 else None
            calls = self.dut.hw.calls(m0["hw"], op=None)
            if hw_end is not None:
                calls = calls[:hw_end - m0["hw"]]
            toggles = {}
            for c in calls:
                if c.op == "driver" and c.method.startswith("toggle_mux_to_tor_"):
                    toggles.setdefault(c.port, []).append(c.method)
            bad = {}
            for port in self.ports:
                read_side = self.read_sides[port]
                side = read_side if state == "active" else (
                    TARGET_LOWER_TOR if read_side == TARGET_UPPER_TOR else TARGET_UPPER_TOR)
                want = ["toggle_mux_to_tor_a" if side == TARGET_UPPER_TOR else "toggle_mux_to_tor_b"]
                have = toggles.get(self.topo.index(port), [])
                if have != want:
                    bad[port] = have
            overlaps = [o for o in self.dut.hw.overlaps(m0["hw"]) if hw_end is None or o.seq < hw_end]
            rec["driver_calls"] = len([c for c in calls if c.op == "driver"])
            rec["toggle_problems"] = {"n": len(bad), "sample": dict(list(bad.items())[:5])}
            rec["overlaps"] = {"n": len(overlaps), "sample": [o._asdict() for o in overlaps[:3]]}
            if bad:
                problems.append("{} cables without exactly one toggle call toward the requested side".format(len(bad)))
            if overlaps:
                problems.append("{} overlapping cable calls".format(len(overlaps)))
        else:
            bad = {}
            rpc_count = 0
            for port, soc in self.socs.items():
                rpcs = soc.rpcs(since=m0["socs"][port])
                if m1:
                    rpcs = rpcs[:m1["socs"][port] - m0["socs"][port]]
                rpc_count += len(rpcs)
                sets = [x.request for x in rpcs if x.name == "SetAdminForwardingPortState"]
                want = sorted([([0], [state == "active"]), ([1], [state == "standby"])])
                have = sorted((s["portid"], s["state"]) for s in sets)
                if have != want:
                    bad[port] = have
            rec["soc_rpcs"] = rpc_count
            rec["rpc_problems"] = {"n": len(bad), "sample": {k: str(v) for k, v in list(bad.items())[:5]}}
            if bad:
                problems.append("{} SoCs without exactly the round's two SetAdminForwardingPortState".format(len(bad)))
        msgs = self.dut.syslog.messages(m0["log"])
        if m1:
            msgs = msgs[:m1["log"] - m0["log"]]
        rec["logs"] = {s: 0 for s in SEVERITIES}
        new = []
        for m in msgs:
            sev = m.severity or "RAW"
            rec["logs"][sev] = rec["logs"].get(sev, 0) + 1
            if sev in ("EMERG", "ALERT", "CRIT", "ERR", "WARNING", "RAW"):
                key = (sev, normalized(m.text))
                if key not in self.seen:
                    self.seen[key] = 0
                    new.append({"severity": sev, "text": m.text[:300]})
                self.seen[key] += 1
        rec["new_messages"] = new
        ops = self.dut.monitor.ops(m0["monitor"])
        rec["ycabled_cmds"] = len([op for op in ops if until is None or op.seq < until])
        if problems:
            self.problems.append("round {}: {}".format(r, "; ".join(problems)))
        rec["problems"] = problems
        return rec

    def sample(self, rec, pid, redis_pid, sampler):
        rec["sample_t"] = time.time()
        rec["harness_gc_ms"] = self.gc_pauses.take()
        rec["proc"] = proc_sample(pid)
        rec["peak"] = sampler.take()
        rec["redis"] = redis_sample(self.dut)
        rec["redis_server"] = proc_sample(redis_pid)
        rec["harness"] = {"rss_kb": proc_sample(os.getpid())["rss_kb"], "monitor_ops": self.dut.monitor.mark(),
                          "syslog_msgs": self.dut.syslog.mark(), "hw_calls": self.dut.hw.mark()}
        rec["alive"] = self.dut.proc.running()

    def final_checks(self, last_state):
        problems = []
        for port in self.ports:
            row = self.dut.state.get("HW_MUX_CABLE_TABLE", port)
            if row.get("state") != last_state:
                problems.append("{} HW_MUX_CABLE_TABLE {}".format(port, row))
            if self.cable_type == "active-standby":
                read_side = self.read_sides[port]
                side = read_side if last_state == "active" else (
                    TARGET_LOWER_TOR if read_side == TARGET_UPPER_TOR else TARGET_UPPER_TOR)
                direction = self.dut.hw.port(self.topo.index(port)).mux_direction
                if direction != side:
                    problems.append("{} mux direction {}".format(port, direction))
            else:
                peer = self.dut.state.get("HW_MUX_CABLE_TABLE_PEER", port)
                if peer.get("state") != opposite(last_state):
                    problems.append("{} HW_MUX_CABLE_TABLE_PEER {}".format(port, peer))
                if self.socs[port].admin != [last_state == "active", last_state != "active"]:
                    problems.append("{} SoC admin {}".format(port, self.socs[port].admin))
        return problems


def binary_md5():
    cmd = os.environ.get("BB_YCABLED_CMD")
    if not cmd:
        return None
    with open(shlex.split(cmd)[0], "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


@pytest.mark.parametrize("cable_type", ["active-active", "active-standby"])
def test_soak(dut, cable_type, request):
    """PORTS mux ports of one cable type toggled together every PERIOD seconds
    for SECONDS, with telemetry enabled: every round is answered once per
    port with the requested state, each cable or SoC gets the calls the round
    asks for, and ycabled's resources are recorded every round.

    Cases: active-active, with a SoC double per port and the peer toggled the
    other way; then active-standby, half of the cables read from the lower
    ToR.

    Steps:
    1. Configure the ports, start ycabled, and wait until it has been quiet
       for 1 s. Record ycabled's resources after startup.
    2. Every PERIOD seconds, in one transaction, request standby (even rounds)
       or active (odd rounds) for every port, and wait until every port has
       answered, up to 30 s.
    3. At the next round's requests, check the round over everything from its
       requests on: every port answered within 30 s; one answer per port and
       table with the requested state; one toggle call per cable toward the
       requested side, no overlapping cable calls (active-standby); the two
       SetAdminForwardingPortState RPCs of the round at each SoC
       (active-active). Record the answers' latency, ycabled's CPU time,
       memory, threads, file descriptors, sockets, redis connections and
       commands, the keys in redis, and its logs by severity.
    4. After the last round, wait until ycabled has been quiet for 2 s, check
       the last round, then each port's final rows, cable or SoC.
    5. Expect no problem in any round.
    """
    out_dir = os.environ.get("BB_SOAK_OUT") or request.config.getoption("--bb-report-dir") or "/tmp/bb-report"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "soak-{}-{}.jsonl".format(impl_name(), cable_type))
    with open(path, "w") as out:
        soak = Soak(dut, cable_type, out)
        soak.gc_pauses = GcPauses()
        soak.configure()
        t_start = time.monotonic()
        dut.start(timeout=300)
        startup_s = time.monotonic() - t_start
        dut.monitor.wait_settled(quiet=1, timeout=60)
        pid, redis_pid = dut.proc.pid, dut.env.redis.proc.pid
        sampler = PeakSampler(pid)
        sampler.start()
        start = {"type": "start", "implementation": impl_name(), "cable_type": cable_type, "ports": PORTS,
                 "period_s": PERIOD, "seconds": SECONDS, "telemetry": True, "t": time.time(),
                 "startup_s": round(startup_s, 2), "cmd": os.environ.get("BB_YCABLED_CMD"),
                 "binary_md5": binary_md5(), "python": sys.version.split()[0]}
        soak.sample(start, pid, redis_pid, sampler)
        start["redis_tables"] = redis_tables(dut)
        soak.write(start)

        t0 = time.monotonic()
        prev = None
        r = 0
        while r * PERIOD < SECONDS:
            delay = t0 + r * PERIOD - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            state = round_state(r)
            marks = soak.marks()
            if prev is not None:
                rec = soak.check_window(r - 1, prev, marks)
                soak.sample(rec, pid, redis_pid, sampler)
                soak.write(rec)
                if not rec["alive"]:
                    soak.problems.append("ycabled exited after round {}".format(r - 1))
                    break
            sent = time.monotonic()
            soak.send(state)
            complete_s = None
            while time.monotonic() - sent < ANSWER_TIMEOUT:
                if soak.all_answered(marks["monitor"], state):
                    complete_s = round(time.monotonic() - sent, 3)
                    break
                time.sleep(0.2)
            prev = {"state": state, "marks": marks, "t": time.time(), "complete_s": complete_s}
            if GC_FREEZE:
                gc.freeze()
            r += 1
        else:
            dut.monitor.wait_settled(quiet=2, timeout=60)
            dut.monitor.barrier()
            rec = soak.check_window(r - 1, prev, None)
            soak.sample(rec, pid, redis_pid, sampler)
            soak.write(rec)
            final = soak.final_checks(prev["state"])
            soak.problems += final
        sampler.stopped.set()
        soak.gc_pauses.close()
        soak.write({"type": "end", "rounds": r, "t": time.time(), "problems": soak.problems[:100],
                    "n_problems": len(soak.problems), "redis_tables": redis_tables(dut),
                    "messages": [{"severity": k[0], "text": k[1], "count": v} for k, v in soak.seen.items()]})
    assert not soak.problems, soak.problems[:20]
