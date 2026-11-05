"""Startup, restart and shutdown (HLD 3.3; C4, C7, N8, N9)."""

import os
import re
import signal
import time

import pytest

from harness import expect
from harness.fake_hw import TARGET_TOR_B
from harness.topology import CONTAINER_PLATFORM_DIR, Topology
from harness.util import TIMESTAMP_RE, wait_until

AS_ROW_KEYS = ("HW_MUX_CABLE_TABLE", "MUX_CABLE_INFO", "MUX_CABLE_STATIC_INFO")


def test_startup_active_standby(dut):
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    # Ethernet4: this ToR reads the cable as ToR B while the mux points to ToR A.
    dut.hw.port(2).read_side = TARGET_TOR_B
    dut.start()

    assert dut.state.table("HW_MUX_CABLE_TABLE") == {
        "Ethernet0": {"state": "active", "read_side": "1", "active_side": "1"},
        "Ethernet4": {"state": "standby", "read_side": "2", "active_side": "1"},
    }
    for name, index in (("Ethernet0", 1), ("Ethernet4", 2)):
        static = dut.state.get("MUX_CABLE_STATIC_INFO", name)
        assert static == expect.mux_cable_static_info(dut.hw.port(index)), name
    # Python writes MUX_CABLE_INFO at startup for the first active-standby
    # port only; the first telemetry pass writes the others.
    info = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    assert re.match(TIMESTAMP_RE, info.pop("time_post"))
    wanted = expect.mux_cable_info(dut.hw.port(1))
    wanted.pop("peer_eye_height_lane2")  # known issue 3.8-4, see test_telemetry.py
    info.pop("peer_eye_height_lane2")
    assert info == wanted, expect.subset(info, wanted)
    assert not dut.state.keys("HW_MUX_CABLE_TABLE_PEER")
    assert dut.config.get("XCVRD_LOG", "Y_CABLE") == {"log_verbosity": "notice"}


def test_startup_active_active(dut):
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4="127.0.0.2/32"))
    soc = dut.soc("127.0.0.2")
    dut.start()

    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "active", "read_side": "0", "active_side": "active"}
    assert dut.state.get("HW_MUX_CABLE_TABLE_PEER", "Ethernet0") == {
        "state": "standby", "read_side": "0", "active_side": "standby"}
    assert soc.rpcs("QueryAdminForwardingPortState")[0].request == {"portid": [0, 1], "state": [False, False]}
    # Placeholder cable info, no static info.
    info = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    assert re.match(TIMESTAMP_RE, info.pop("time_post"))
    assert info == expect.PLACEHOLDER_INFO
    assert not dut.state.exists("MUX_CABLE_STATIC_INFO", "Ethernet0")
    # The GracefulRestart stream of the upper ToR.
    assert soc.wait_rpcs("NotifyGracefulRestartStart")[0].request == {"tor": 1}


def test_startup_mixed_cable_types(dut):
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4="127.0.0.2/32")
    dut.configure(topo)
    dut.soc("127.0.0.2")
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet4")["state"] == "active"
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


def test_mux_cable_without_state_is_ignored(dut):
    topo = Topology().active_standby("Ethernet0", "Ethernet4")
    topo.mux["Ethernet0"].state = None
    dut.configure(topo)
    dut.start()
    assert dut.state.keys("HW_MUX_CABLE_TABLE") == ["Ethernet4"]
    assert not dut.hw.calls(port=1)


def test_startup_without_mux_cables(dut):
    dut.configure(Topology())
    dut.start(expect_mux=False)
    time.sleep(1)
    tables = {(a.db, a.table) for a in dut.writes()}
    assert tables == {("CONFIG_DB", "XCVRD_LOG")}, tables
    # Nothing answers the CLI.
    mark = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0", wait=False)
    time.sleep(2)
    assert not dut.monitor.accesses(mark, kind="write", table="XCVRD_SHOW_HWMODE_DIR_RSP")


def test_startup_waits_for_config_db_initialized(dut):
    topo = Topology().active_standby("Ethernet0")
    topo.config_db_initialized = False
    dut.configure(topo)
    dut.start(ready=False)
    time.sleep(2)
    assert dut.proc.running()
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet0")
    dut.config.r.set("CONFIG_DB_INITIALIZED", "1")
    dut.wait_ready()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


def test_startup_platform_api_failure_exits(dut):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.platform_init_error = "RuntimeError: platform API broken"
    dut.allow_exit = True
    dut.start(ready=False)
    # No platform API and no sfputil plugin either: exit, for supervisord to
    # start it again. Python exits with SFPUTIL_LOAD_ERROR (1).
    assert dut.proc.wait_exit(20) not in (None, 0)


def test_startup_port_map_from_config_db(dut):
    """The port map comes from CONFIG_DB PORT; port_config.ini is not needed."""
    dut.configure(Topology().active_standby("Ethernet0"))
    os.unlink(os.path.join(CONTAINER_PLATFORM_DIR, dut.topo.hwsku, "port_config.ini"))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


def test_startup_port_map_from_port_config_file(dut):
    """Without CONFIG_DB PORT, the port map comes from port_config.ini."""
    topo = Topology().active_standby("Ethernet0")
    topo.port_table = False
    dut.configure(topo)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


@pytest.mark.python_only
def test_startup_without_port_map_exits(dut):
    topo = Topology().active_standby("Ethernet0")
    topo.port_table = False
    dut.configure(topo)
    os.unlink(os.path.join(CONTAINER_PLATFORM_DIR, dut.topo.hwsku, "port_config.ini"))
    dut.allow_exit = True
    dut.start(ready=False)
    assert dut.proc.wait_exit(20) == 1


def test_stop_keeps_tables(dut):
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4="127.0.0.2/32")
    dut.configure(topo)
    dut.soc("127.0.0.2")
    dut.start()
    before = dut.dbs.snapshot()["STATE_DB"]
    dut.stop()
    after = dut.dbs.snapshot()["STATE_DB"]
    for key in before:
        if key.split("|")[0] in AS_ROW_KEYS + ("HW_MUX_CABLE_TABLE_PEER",):
            assert after.get(key) == before[key], key
    deletes = dut.monitor.accesses(dut.start_mark, kind="delete", db="STATE_DB")
    assert not deletes, deletes


@pytest.mark.known_issue("3.8-1", "SIGTERM never completes: run() joins threads that never stop")
def test_sigterm_exits_promptly(dut):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    result = dut.stop(sigterm_timeout=5)
    assert result.exited_on_sigterm and result.returncode == 0
    assert result.sigterm_to_exit <= 2


def test_sighup_is_ignored(dut):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    os.kill(dut.proc.pid, signal.SIGHUP)
    time.sleep(1)
    assert dut.proc.running()
    assert dut.probe("Ethernet0") == "active"


def test_restart_reads_cable_then_replays_last_request(dut):
    """At startup ycabled publishes the cable's state, then applies the last
    request still in APPL_DB HW_MUX_CABLE_TABLE, since its subscription reads
    the existing entries first."""
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    dut.stop()
    # While ycabled is down the mux goes back to this ToR, and someone turns on
    # debug logs.
    dut.hw.port(1).mux_direction = 1
    dut.config.set("XCVRD_LOG", "Y_CABLE", {"log_verbosity": "debug"})
    hw_mark = dut.hw.mark()
    dut.start()
    wait_until(lambda: len(dut.writes(table="HW_MUX_CABLE_TABLE", key="Ethernet0", field="state")) >= 2, 5)
    states = [a.value for a in dut.writes(table="HW_MUX_CABLE_TABLE", key="Ethernet0", field="state")]
    assert states == ["active", "standby"], states
    assert [c.method for c in dut.hw.calls(hw_mark, method="toggle_mux_to_tor_b")] == ["toggle_mux_to_tor_b"]
    assert dut.config.field("XCVRD_LOG", "Y_CABLE", "log_verbosity") == "notice"


@pytest.mark.slow
@pytest.mark.python_only
def test_dead_worker_kills_process(dut):
    """Python checks its threads every 60 s and SIGKILLs itself if one ended,
    so supervisord restarts it. The worker is killed with the crash of known
    issue 3.8-17."""
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    dut.allow_exit = dut.allow_crash = True
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"state": "init"})
    dut.syslog.wait_for(dut.log_mark, 5, contains="Exception occured at child thread YCableTableUpdateTask")
    dut.proc.save_coverage()
    assert dut.proc.wait_exit(75) == -signal.SIGKILL


@pytest.mark.known_issue("3.8-2", "the syslog identity changes with import order (pmon#bmc_base)")
def test_syslog_identity(dut):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.port(1).script("toggle_mux_to_tor_b", value=False)
    dut.start()
    dut.toggle("Ethernet0", "standby")
    wait_until(lambda: dut.logs(), 5)
    idents = {m.ident for m in dut.logs()}
    assert idents == {"ycable"}, idents
