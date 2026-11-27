"""Benchmarks of the ycabled implementation under test (HLD section 5).

Not part of the test suite (pytest.ini collects tests/ only); run them
explicitly, once per implementation, in the suite's container:

    run.sh bench/test_bench.py                                       # Python ycabled
    BB_YCABLED_CMD=/src/target/release/ycabled \\
        run.sh bench/test_bench.py                                   # ycabled-rs

Each run writes bench-<implementation>.json to the report directory;
bench/compare.py puts two of them side by side. Both implementations run
on the same fakes (fake cables over a unix socket, gRPC SoC doubles in the
test process), so the numbers compare the daemons, not real hardware.
"""

import contextlib
import datetime
import gc
import json
import math
import os
import re
import threading
import time

import pytest

from harness.dut import Dut
from harness.redisdb import HARNESS_ADDRESS
from harness.topology import Topology, soc_ip
from harness.util import wait_until
from harness.ycabled import implementation

CLK_TCK = os.sysconf("SC_CLK_TCK")
# Idle window: covers one telemetry pass (60 s after startup).
IDLE_WINDOW = 65

RESULTS = []


def impl_name():
    cmd = os.environ.get("BB_YCABLED_CMD", "")
    if implementation() == "python":
        return "python"
    name = os.path.basename(cmd.split()[0])
    # The Rust daemon's binary: ycabled, or ycabled-rs as it was first named.
    return "rust" if name in ("ycabled", "ycabled-rs") else name


@pytest.fixture(scope="module", autouse=True)
def report(request):
    yield
    out_dir = request.config.getoption("--bb-report-dir") or "/tmp/bb-report"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "bench-{}.json".format(impl_name()))
    with open(path, "w") as f:
        json.dump({"implementation": impl_name(), "results": RESULTS}, f, indent=2)


def record(scenario, **values):
    RESULTS.append(dict(scenario=scenario, **values))


def proc_stats(pid):
    with open("/proc/{}/status".format(pid)) as f:
        status = f.read()
    with open("/proc/{}/smaps_rollup".format(pid)) as f:
        rollup = f.read()
    with open("/proc/{}/stat".format(pid)) as f:
        stat = f.read().rsplit(")", 1)[1].split()
    return {
        "threads": int(re.search(r"^Threads:\s+(\d+)", status, re.M).group(1)),
        "rss_kb": int(re.search(r"^VmRSS:\s+(\d+)", status, re.M).group(1)),
        "pss_kb": int(re.search(r"^Pss:\s+(\d+)", rollup, re.M).group(1)),
        "hwm_kb": int(re.search(r"^VmHWM:\s+(\d+)", status, re.M).group(1)),
        "cpu_ticks": int(stat[11]) + int(stat[12]),
    }


def redis_connections(dut):
    return len([c for c in dut.config.r.client_list() if not c["addr"].startswith(HARNESS_ADDRESS + ":")])


def ports(n):
    return ["Ethernet{}".format(4 * i) for i in range(n)]


def steady_state(dut, scenario, startup):
    """Memory, threads and connections after startup, CPU over an idle
    window that includes one telemetry pass."""
    time.sleep(5)
    pid = dut.proc.pid
    before = proc_stats(pid)
    time.sleep(IDLE_WINDOW)
    after = proc_stats(pid)
    record(scenario,
           startup_s=round(startup, 3),
           rss_mb=round(after["rss_kb"] / 1024, 1),
           pss_mb=round(after["pss_kb"] / 1024, 1),
           threads=after["threads"],
           redis_connections=redis_connections(dut),
           cpu_percent=round((after["cpu_ticks"] - before["cpu_ticks"]) / CLK_TCK / IDLE_WINDOW * 100, 2))


def first_writes(dut, since, table, field, keys, db=None):
    """The first write of `field` to each key of `table` since `since`, or None."""
    firsts = {}
    for a in dut.writes(since=since, db=db, table=table, field=field):
        firsts.setdefault(a.key, a)
    return firsts if set(keys) <= set(firsts) else None


def request_ts(dut, mark, key):
    """When the suite's HSET of `key` reached redis (redis's clock)."""
    for op in dut.monitor.ops(mark, clients=("harness",)):
        if op.cmd == "HSET" and op.args and op.args[0] == key:
            return op.ts
    return None


def test_steady_state_active_standby(dut):
    """Resources at rest with 24 active-standby cables, as on a 24-port KVM
    dual ToR testbed.

    Steps:
    1. Configure 24 active-standby ports.
    2. Start ycabled, and time it until it is ready.
    3. Wait 5 s, then measure ycabled's CPU time over 65 s, which include one
       telemetry pass.
    4. Record the startup time, RSS, PSS, threads, redis connections and CPU
       percentage.
    """
    names = ports(24)
    dut.configure(Topology(nports=24).active_standby(*names))
    t0 = time.monotonic()
    dut.start(timeout=60)
    steady_state(dut, "steady-active-standby-24", time.monotonic() - t0)


def test_steady_state_active_active(dut):
    """Resources at rest with 32 active-active ports, each with its own SoC.

    Steps:
    1. Configure 32 active-active ports, each with its own SoC double, at
       192.168.0.3 to 192.168.0.65.
    2. Start ycabled, waiting up to 90 s, and time it until it is ready.
    3. Wait 5 s, then measure ycabled's CPU time over 65 s, which include one
       telemetry pass.
    4. Record the startup time, RSS, PSS, threads, redis connections and CPU
       percentage.
    """
    names = ports(32)
    topo = Topology(nports=32)
    for name in names:
        topo.active_active(name, soc_ipv4=soc_ip(name) + "/32")
    dut.configure(topo)
    for name in names:
        dut.soc(soc_ip(name))
    t0 = time.monotonic()
    dut.start(timeout=90)
    steady_state(dut, "steady-active-active-32", time.monotonic() - t0)


@pytest.mark.parametrize("n", [8, 32])
def test_startup_with_unreachable_socs(dut, n):
    """N3: from ycabled's first DB access to every port's first state, when no
    SoC answers (a TCP accept, then nothing).

    Cases: 8 or 32 ports.

    Steps:
    1. Configure the active-active ports, and blackhole each one's SoC address:
       it accepts TCP and never answers.
    2. Start ycabled without waiting until it is ready.
    3. Record the time from ycabled's first DB access to the last port's first
       HW_MUX_CABLE_TABLE state, on redis's clock.
    """
    names = ports(n)
    topo = Topology(nports=n)
    for name in names:
        topo.active_active(name, soc_ipv4=soc_ip(name) + "/32")
    dut.configure(topo)
    for name in names:
        dut.blackhole(soc_ip(name))
    dut.start(ready=False)
    first = wait_until(lambda: dut.monitor.ops(dut.start_mark), 30)[0]
    firsts = wait_until(lambda: first_writes(dut, dut.start_mark, "HW_MUX_CABLE_TABLE", "state", names), 4 * n)
    record("startup-unreachable-socs-{}".format(n), seconds=round(max(a.ts for a in firsts.values()) - first.ts, 3))


def test_toggle_latency_with_hanging_socs(dut):
    """N1: 8 active-active ports toggled at once; the SoCs of 4 of them never
    answer. Latency of the 4 healthy ports' answers.

    Steps:
    1. Configure 8 active-active ports, each with its own SoC double, and start
       ycabled.
    2. Make the SoCs of the first 4 ports answer SetAdminForwardingPortState
       after 3 s: those ports come first, the worst case for one serial loop.
    3. Toggle the 8 ports to standby in one transaction, and wait for every
       answer.
    4. Record the healthy ports' slowest and mean answer and the hanging ports'
       slowest, from the request, on redis's clock.
    """
    names = ports(8)
    topo = Topology(nports=8)
    for name in names:
        topo.active_active(name, soc_ipv4=soc_ip(name) + "/32")
    dut.configure(topo)
    socs = [dut.soc(soc_ip(name)) for name in names]
    dut.start(timeout=60)
    # The hanging ones first, as the worst case for one serial loop.
    hanging, healthy = names[:4], names[4:]
    for soc in socs[:4]:
        soc.script("SetAdminForwardingPortState", delay=3)
    mark = dut.monitor.mark()
    with dut.appl.r.pipeline(transaction=True) as p:
        for name in names:
            p.hset("HW_MUX_CABLE_TABLE:" + name, "state", "standby")
        p.execute()
    firsts = wait_until(lambda: first_writes(dut, mark, "HW_MUX_CABLE_TABLE", "state", names), 30)
    t_req = request_ts(dut, mark, "HW_MUX_CABLE_TABLE:" + names[0])
    lat = sorted(firsts[n].ts - t_req for n in healthy)
    record("toggle-latency-4-of-8-socs-hanging",
           healthy_max_ms=round(lat[-1] * 1000, 1),
           healthy_mean_ms=round(sum(lat) / len(lat) * 1000, 1),
           hanging_max_ms=round(max(firsts[n].ts - t_req for n in hanging) * 1000, 1))


def test_toggle_latency_with_hanging_cables(dut):
    """N1 for active-standby: 8 cables toggled at once; the toggles of 4 of
    them do not return for 3 s. Latency of the 4 healthy ports' answers.

    Steps:
    1. Configure 8 active-standby ports, and make toggle_mux_to_tor_b() of
       the first 4 cables take 3 s: those ports come first, the worst case
       for calls that take turns.
    2. Start ycabled.
    3. Toggle the 8 ports to standby in one transaction, and wait for every
       answer.
    4. Record the healthy ports' slowest and mean answer and the hanging
       ports' slowest, from the request, on redis's clock.
    """
    names = ports(8)
    dut.configure(Topology(nports=8).active_standby(*names))
    for i in range(1, 5):
        dut.hw.port(i).script("toggle_mux_to_tor_b", delay=3)
    dut.start(timeout=60)
    hanging, healthy = names[:4], names[4:]
    mark = dut.monitor.mark()
    with dut.appl.r.pipeline(transaction=True) as p:
        for name in names:
            p.hset("HW_MUX_CABLE_TABLE:" + name, "state", "standby")
        p.execute()
    firsts = wait_until(lambda: first_writes(dut, mark, "HW_MUX_CABLE_TABLE", "state", names), 60)
    t_req = request_ts(dut, mark, "HW_MUX_CABLE_TABLE:" + names[0])
    lat = sorted(firsts[n].ts - t_req for n in healthy)
    record("toggle-latency-4-of-8-cables-hanging",
           healthy_max_ms=round(lat[-1] * 1000, 1),
           healthy_mean_ms=round(sum(lat) / len(lat) * 1000, 1),
           hanging_max_ms=round(max(firsts[n].ts - t_req for n in hanging) * 1000, 1))


def test_probe_latency_active_standby(dut):
    """linkmgrd's probe of an active-standby cable, 200 times in a row.

    Steps:
    1. Configure Ethernet0 as the only active-standby port, and start ycabled.
    2. 200 times: probe Ethernet0, and wait for the answer.
    3. Record the median, p99 and maximum time from the request to the answer,
       on redis's clock.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    lat = []
    for _ in range(200):
        mark = dut.monitor.mark()
        dut.probe("Ethernet0", wait=False)
        dut.monitor.wait_access(mark, 5, kind="write", table="MUX_CABLE_RESPONSE_TABLE", key="Ethernet0",
                                field="response")
        t_req = request_ts(dut, mark, "MUX_CABLE_COMMAND_TABLE:Ethernet0")
        resp = dut.monitor.accesses(mark, kind="write", table="MUX_CABLE_RESPONSE_TABLE", key="Ethernet0")[0]
        lat.append(resp.ts - t_req)
    lat.sort()
    record("probe-latency-active-standby",
           median_ms=round(lat[len(lat) // 2] * 1000, 2),
           p99_ms=round(lat[int(len(lat) * 0.99) - 1] * 1000, 2),
           max_ms=round(lat[-1] * 1000, 2))


def test_toggle_throughput_active_standby(dut):
    """24 active-standby cables toggled together, 10 rounds. Each round is
    timed on redis's clock, from the suite's request to ycabled's last answer,
    so the suite's polling interval does not count.

    Steps:
    1. Configure 24 active-standby ports, and start ycabled.
    2. 10 rounds: request standby or active for every port in one transaction,
       and wait for every port's answer.
    3. Record the rounds' total time, and the toggles per second.
    """
    names = ports(24)
    dut.configure(Topology(nports=24).active_standby(*names))
    dut.start(timeout=60)
    elapsed = 0.0
    for rnd in range(10):
        state = "standby" if rnd % 2 == 0 else "active"
        mark = dut.monitor.mark()
        with dut.appl.r.pipeline(transaction=True) as p:
            for name in names:
                p.hset("HW_MUX_CABLE_TABLE:" + name, "state", state)
            p.execute()
        firsts = wait_until(lambda: first_writes(dut, mark, "HW_MUX_CABLE_TABLE", "state", names), 30)
        elapsed += max(a.ts for a in firsts.values()) - request_ts(dut, mark, "HW_MUX_CABLE_TABLE:" + names[0])
    record("toggle-throughput-active-standby-24x10", seconds=round(elapsed, 3),
           toggles_per_s=round(240 / elapsed, 1))


TELEMETRY_RPCS = ("QueryLinkState", "QueryOperationPortState", "QueryAdminForwardingPortState", "QueryServerVersion")


@pytest.mark.parametrize("fault", ["blackholed", "hanging"])
def test_telemetry_pass_faulty_socs(dut, fault):
    """32 active-active ports whose SoCs do not answer: how long the first
    telemetry pass takes, from its first MUX_CABLE_INFO write to its last.

    blackholed: the SoCs accept TCP and send nothing. grpc-core gives up
    connecting after 20 s and then fails RPCs at once (TRANSIENT_FAILURE), so
    Python's pass is quick; ycabled-rs waits for each RPC's deadline. hanging:
    the SoCs are connected but the four telemetry RPCs never answer in time, so
    every RPC waits for its 0.5 s deadline.

    Steps:
    1. Configure 32 active-active ports, each with its own SoC address, and
       blackhole each address or start a SoC double there.
    2. Start ycabled. With SoC doubles, make the four telemetry RPCs answer
       after 3 s.
    3. Wait up to 200 s for every port's grpc_connection_status in
       MUX_CABLE_INFO.
    4. Record the time from the pass's first such write to its last.
    """
    names = ports(32)
    topo = Topology(nports=32)
    for name in names:
        topo.active_active(name, soc_ipv4=soc_ip(name) + "/32")
    dut.configure(topo)
    if fault == "blackholed":
        for name in names:
            dut.blackhole(soc_ip(name))
    else:
        socs = [dut.soc(soc_ip(name)) for name in names]
    dut.start(timeout=60)
    if fault == "hanging":
        for soc in socs:
            for rpc in TELEMETRY_RPCS:
                soc.script(rpc, delay=3)
    mark = dut.monitor.mark()
    firsts = wait_until(lambda: first_writes(dut, mark, "MUX_CABLE_INFO", "grpc_connection_status", names), 200)
    ts = [a.ts for a in firsts.values()]
    record("telemetry-pass-32-{}-socs".format(fault), seconds=round(max(ts) - min(ts), 3))


TOGGLES = 1000


def _peak_threads(pid, stop, peak):
    while not stop.is_set():
        try:
            with open("/proc/{}/status".format(pid)) as f:
                peak[0] = max(peak[0], int(re.search(r"^Threads:\s+(\d+)", f.read(), re.M).group(1)))
        except (OSError, AttributeError):
            return
        stop.wait(0.02)


@contextlib.contextmanager
def resources(dut, scenario, toggles):
    """ycabled's CPU time, memory, threads and use of redis over the block:
    yields a dict for the block's own numbers, then records them all."""
    pid = dut.proc.pid
    before = proc_stats(pid)
    mark = dut.monitor.mark()
    stop, peak = threading.Event(), [0]
    sampler = threading.Thread(target=_peak_threads, args=(pid, stop, peak), daemon=True)
    sampler.start()
    t0 = time.monotonic()
    numbers = {}
    yield numbers
    wall = time.monotonic() - t0
    stop.set()
    sampler.join()
    after = proc_stats(pid)
    cpu = (after["cpu_ticks"] - before["cpu_ticks"]) / CLK_TCK
    record(scenario, **numbers,
           cpu_s=round(cpu, 2),
           cpu_percent=round(cpu / wall * 100, 1),
           rss_mb=round(after["rss_kb"] / 1024, 1),
           pss_mb=round(after["pss_kb"] / 1024, 1),
           peak_rss_mb=round(after["hwm_kb"] / 1024, 1),
           peak_threads=peak[0],
           redis_connections=redis_connections(dut),
           redis_commands_per_toggle=round(len(dut.monitor.ops(mark)) / toggles, 1))


def toggle_device(dut, cable_type, n):
    """`n` mux ports of one cable type, active-active ones with a SoC each,
    with telemetry off so that only the toggles are measured."""
    names = ports(n)
    topo = Topology(nports=max(n, 8))
    for name in names:
        if cable_type == "active-standby":
            topo.active_standby(name)
        else:
            topo.active_active(name, soc_ipv4=soc_ip(name) + "/32")
    topo.xcvrd_log = {"disable_telemetry": "True"}
    dut.configure(topo)
    if cable_type == "active-active":
        for name in names:
            dut.soc(soc_ip(name))
    dut.start(timeout=120)
    dut.monitor.wait_settled(quiet=1, timeout=30)
    return names


@pytest.mark.parametrize("cable_type", ["active-standby", "active-active"])
def test_thousand_toggles_burst(dut, cable_type):
    """1000 toggles as bursts: 100 ports toggled together, 10 rounds. Each
    round is timed on redis's clock, from the suite's requests to ycabled's
    last answer.

    Cases: active-standby, or active-active with a SoC per port.

    Steps:
    1. Configure 100 ports of the cable type, the active-active ones each with
       a SoC double, with telemetry disabled. Start ycabled, and wait until it
       has been quiet for 1 s.
    2. Measure ycabled's resources over the toggles: CPU time, memory, peak
       threads (sampled every 20 ms), redis connections, and redis commands per
       toggle.
    3. 10 rounds: request standby or active for every port in one transaction,
       wait for every port's answer, and expect each to be the requested state.
    4. Record the rounds' total time, the toggles per second, and the
       resources.
    """
    names = toggle_device(dut, cable_type, 100)
    elapsed = 0.0
    with resources(dut, "toggles-1000-burst-{}".format(cable_type), TOGGLES) as numbers:
        for rnd in range(TOGGLES // len(names)):
            state = "standby" if rnd % 2 == 0 else "active"
            mark = dut.monitor.mark()
            with dut.appl.r.pipeline(transaction=True) as p:
                for name in names:
                    p.hset("HW_MUX_CABLE_TABLE:" + name, "state", state)
                p.execute()
            firsts = wait_until(lambda: first_writes(dut, mark, "HW_MUX_CABLE_TABLE", "state", names), 60)
            assert {a.value for a in firsts.values()} == {state}
            elapsed += max(a.ts for a in firsts.values()) - request_ts(dut, mark, "HW_MUX_CABLE_TABLE:" + names[0])
        numbers.update(seconds=round(elapsed, 3), toggles_per_s=round(TOGGLES / elapsed, 1))


@pytest.mark.parametrize("cable_type", ["active-standby", "active-active"])
def test_thousand_toggles_sequential(dut, cable_type):
    """1000 toggles of one port, each sent once the previous one is answered:
    the latency of one toggle, on redis's clock.

    Cases: active-standby, or active-active with a SoC.

    Steps:
    1. Configure one port of the cable type, an active-active one with a SoC
       double, with telemetry disabled. Start ycabled, and wait until it has
       been quiet for 1 s.
    2. Measure ycabled's resources over the toggles: CPU time, memory, peak
       threads (sampled every 20 ms), redis connections, and redis commands per
       toggle.
    3. 1000 times: request the other state, wait for the answer, polling every
       1 ms, and expect the requested state.
    4. Record the latencies' sum, median, p99 and maximum, and the resources.
    """
    name = toggle_device(dut, cable_type, 1)[0]
    key = "HW_MUX_CABLE_TABLE:" + name
    lat = []
    with resources(dut, "toggles-1000-sequential-{}".format(cable_type), TOGGLES) as numbers:
        for i in range(TOGGLES):
            state = "standby" if i % 2 == 0 else "active"
            mark = dut.monitor.mark()
            dut.appl.set("HW_MUX_CABLE_TABLE", name, {"state": state})
            answer = wait_until(lambda: dut.monitor.accesses(mark, kind="write", db="STATE_DB",
                                                             table="HW_MUX_CABLE_TABLE", key=name, field="state"),
                                10, interval=0.001)[0]
            assert answer.value == state
            lat.append(answer.ts - request_ts(dut, mark, key))
        lat.sort()
        numbers.update(seconds=round(sum(lat), 3),
                       median_ms=round(lat[len(lat) // 2] * 1000, 2),
                       p99_ms=round(lat[int(len(lat) * 0.99) - 1] * 1000, 2),
                       max_ms=round(lat[-1] * 1000, 2))


# Requests as orchagent and linkmgrd write them to APPL_DB, (table, field), by
# the name of the Dut helper that sends the same request: Dut.ANSWERS says
# where ycabled answers each one.
REQUESTS = {
    "toggle": ("HW_MUX_CABLE_TABLE", "state"),
    "peer_toggle": ("HW_FORWARDING_STATE_PEER", "state"),
    "probe": ("MUX_CABLE_COMMAND_TABLE", "command"),
    "fwd_probe": ("FORWARDING_STATE_COMMAND", "command"),
}
# Each kind of request of a cable type alone, then all of them together, as
# when many ports switch over at once.
REQUEST_MIXES = {
    "active-standby": [("toggle",), ("probe",), ("toggle", "probe")],
    "active-active": [("toggle",), ("peer_toggle",), ("fwd_probe",), ("toggle", "peer_toggle", "fwd_probe")],
}
# The MUX_METRICS_TABLE side of the requests that ycabled records metrics for.
METRIC_SIDES = {("active-standby", "toggle"): "", ("active-active", "toggle"): "self_",
                ("active-active", "peer_toggle"): "peer_"}
# The SoC RPCs of active-active requests, and about the median time of
# SetAdminForwardingPortState on production SoCs (47-51 ms).
SOC_RPCS = ("SetAdminForwardingPortState", "QueryAdminForwardingPortState")
SOC_RPC_MS = 50
BURST_PORTS = 100
BURST_ROUNDS = 10
ALONE = 50


def request_value(kind, rnd):
    """Round `rnd`'s request: a toggle's state, alternating, and the opposite
    state for the peer; a probe's command."""
    state = "standby" if rnd % 2 == 0 else "active"
    if kind == "toggle":
        return state
    if kind == "peer_toggle":
        return "active" if state == "standby" else "standby"
    return "probe"


def metric_ts(value):
    """A MUX_METRICS_TABLE time (UTC) in seconds since the epoch, like MONITOR's."""
    return datetime.datetime.strptime(value, "%Y-%b-%d %H:%M:%S.%f").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def ms_stats(prefix, seconds, percentiles=(50, 99)):
    """The percentiles (nearest rank) and the maximum of `seconds`, in ms."""
    s = sorted(seconds)
    out = {"{}_p{}_ms".format(prefix, p): round(s[max(0, math.ceil(p / 100 * len(s)) - 1)] * 1000, 1)
           for p in percentiles}
    out["{}_max_ms".format(prefix)] = round(s[-1] * 1000, 1)
    return out


@contextlib.contextmanager
def harness_gc_off():
    """No garbage collection in the test process while it measures: a
    collection there stops the SoC doubles and the MONITOR reader."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


def alone_latencies(dut, name, kind):
    """`ALONE` requests of `kind` for port `name`, each sent once the previous
    one is answered: their latencies on redis's clock."""
    table, field = REQUESTS[kind]
    db, answer_table, answer_field = Dut.ANSWERS[kind]
    lat = []
    for i in range(ALONE):
        mark = dut.monitor.mark()
        dut.appl.set(table, name, {field: request_value(kind, i)})
        answer = wait_until(lambda: dut.writes(since=mark, db=db, table=answer_table, key=name, field=answer_field),
                            10, interval=0.001)[0]
        lat.append(answer.ts - request_ts(dut, mark, "{}:{}".format(table, name)))
    return lat


def request_round(dut, names, kinds, rnd):
    """Round `rnd`: the requests of `kinds` for every port, in one transaction.
    Once each has an answer, returns when each request reached redis, by key;
    each kind's first answer, by port; and ycabled's MUX_METRICS_TABLE writes
    since the requests, by (port, field)."""
    mark = dut.monitor.mark()
    with dut.appl.r.pipeline(transaction=True) as p:
        for name in names:
            for kind in kinds:
                table, field = REQUESTS[kind]
                p.hset("{}:{}".format(table, name), field, request_value(kind, rnd))
        p.execute()
    answers = {}
    for kind in kinds:
        db, table, field = Dut.ANSWERS[kind]
        answers[kind] = wait_until(lambda: first_writes(dut, mark, table, field, names, db=db), 60)
    requested = {op.args[0]: op.ts for op in dut.monitor.ops(mark, clients=("harness",)) if op.cmd == "HSET"}
    metrics = {(a.key, a.field): a.value for a in dut.writes(since=mark, db="STATE_DB", table="MUX_METRICS_TABLE")}
    return requested, answers, metrics


@pytest.mark.parametrize("cable_type,soc_ms,rounds", [
    ("active-standby", 0, BURST_ROUNDS), ("active-active", 0, BURST_ROUNDS), ("active-active", SOC_RPC_MS, 4),
], ids=["active-standby", "active-active", "active-active-soc-{}ms".format(SOC_RPC_MS)])
def test_request_latency(dut, cable_type, soc_ms, rounds):
    """What orchagent and linkmgrd wait for: the time from a request's write
    to APPL_DB to ycabled's answer, on redis's clock. That includes the time
    the request waits in ycabled before it is handled, which
    MUX_METRICS_TABLE does not show: its xcvrd_switch_*_start is when ycabled
    starts to handle a toggle.

    Cases: active-standby (toggles and probes), or active-active with a SoC
    per port (toggles, peer toggles and forwarding state probes), whose
    doubles answer at once, or after 50 ms as production SoCs do. At once,
    the doubles, all in the test process, limit a daemon that sends many RPCs
    together; after 50 ms, the time is the SoCs', as on a device.

    Steps:
    1. Configure 100 ports of the cable type, the active-active ones each with
       a SoC double, with telemetry disabled. Start ycabled, and wait until it
       has been quiet for 1 s. In the 50 ms case, make every SoC double answer
       SetAdminForwardingPortState and QueryAdminForwardingPortState after
       50 ms.
    2. Each kind of request alone: 50 times, send it for Ethernet0, and wait
       for the answer. Record the median and maximum latency.
    3. Each mix of requests, each kind alone and then all together: 10 rounds
       (4 in the 50 ms case), each with the mix's requests for every port in
       one transaction, waiting for every answer and then for ycabled to be
       quiet. Expect each toggle answered with the requested state.
    4. Record, for each mix: each kind's median, p99 and maximum latency; the
       rounds' mean and longest time, from the requests to the last answer;
       the requests per second. For toggles, from MUX_METRICS_TABLE, the
       median and maximum of the time from the request to xcvrd_switch_*_start
       (waited) and from _start to _end (handled).
    """
    names = toggle_device(dut, cable_type, BURST_PORTS)
    if soc_ms:
        for soc in dut.socs.values():
            for rpc in SOC_RPCS:
                soc.script(rpc, delay=soc_ms / 1000)
    label = cable_type + ("-soc-{}ms".format(soc_ms) if soc_ms else "")
    kinds = [k for k in REQUESTS if any(k in mix for mix in REQUEST_MIXES[cable_type])]
    numbers = {}
    with harness_gc_off():
        for kind in kinds:
            numbers.update(ms_stats(kind, alone_latencies(dut, names[0], kind), percentiles=(50,)))
    record("request-latency-alone-{}".format(label), **numbers)
    for mix in REQUEST_MIXES[cable_type]:
        lat, waited, handled = ({kind: [] for kind in mix} for _ in range(3))
        durations = []
        for rnd in range(rounds):
            dut.monitor.wait_settled(quiet=0.5, timeout=30)
            with harness_gc_off():
                requested, answers, metrics = request_round(dut, names, mix, rnd)
            for kind in mix:
                side = METRIC_SIDES.get((cable_type, kind))
                for name, answer in answers[kind].items():
                    t_req = requested["{}:{}".format(REQUESTS[kind][0], name)]
                    lat[kind].append(answer.ts - t_req)
                    if side is None:
                        continue
                    assert answer.value == request_value(kind, rnd), (kind, name, answer.value)
                    start, end = (metric_ts(metrics[(name, "xcvrd_switch_{}{}_{}".format(side, answer.value, edge))])
                                  for edge in ("start", "end"))
                    waited[kind].append(start - t_req)
                    handled[kind].append(end - start)
            durations.append(max(a.ts for kind in mix for a in answers[kind].values()) - min(requested.values()))
        requests = len(names) * len(mix) * rounds
        numbers = {"requests": requests,
                   "requests_per_s": round(requests / sum(durations), 1),
                   "round_mean_ms": round(sum(durations) / len(durations) * 1000, 1),
                   "round_max_ms": round(max(durations) * 1000, 1)}
        for kind in mix:
            numbers.update(ms_stats(kind, lat[kind]))
            if handled[kind]:
                numbers.update(ms_stats(kind + "_waited", waited[kind], percentiles=(50,)))
                numbers.update(ms_stats(kind + "_handled", handled[kind], percentiles=(50,)))
        record("request-latency-{}-{}x{}-{}".format(label, len(names), rounds, "+".join(mix)), **numbers)
