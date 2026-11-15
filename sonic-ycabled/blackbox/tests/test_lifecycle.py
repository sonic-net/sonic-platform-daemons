"""Startup, restart and shutdown (HLD 3.2; C4, C7, N8, N9)."""

import os
import re
import signal
import time

import pytest

from harness import expect
from harness.fake_hw import TARGET_TOR_B
from harness.topology import CONTAINER_PLATFORM_DIR, Topology, soc_ip
from harness.util import TIMESTAMP_RE, eventually, wait_until

AS_ROW_KEYS = ("HW_MUX_CABLE_TABLE", "MUX_CABLE_INFO", "MUX_CABLE_STATIC_INFO")


def test_startup_active_standby(dut):
    """At startup, ycabled publishes each active-standby cable's state, read
    side and active side in HW_MUX_CABLE_TABLE, and its MUX_CABLE_STATIC_INFO.
    As Python does, it writes MUX_CABLE_INFO at startup for the first
    active-standby port only; the first telemetry pass writes the others. It
    writes no HW_MUX_CABLE_TABLE_PEER, and sets XCVRD_LOG|Y_CABLE log_verbosity
    to notice.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4. Both muxes point
       at ToR A, and this ToR reads Ethernet4's cable as ToR B.
    2. Start ycabled.
    3. Expect HW_MUX_CABLE_TABLE Ethernet0 {state: active, read_side: 1,
       active_side: 1} and Ethernet4 {state: standby, read_side: 2,
       active_side: 1}, and each port's MUX_CABLE_STATIC_INFO from its cable.
    4. Expect Ethernet0's MUX_CABLE_INFO with a time_post timestamp and the
       cable's values, besides peer_eye_height_lane2 (a known issue, see
       test_telemetry.py::test_peer_eye_height_lane2).
    5. Expect no HW_MUX_CABLE_TABLE_PEER, and CONFIG_DB XCVRD_LOG|Y_CABLE
       {log_verbosity: notice}.
    """
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
    wanted.pop("peer_eye_height_lane2")  # a known issue, see test_telemetry.py::test_peer_eye_height_lane2
    info.pop("peer_eye_height_lane2")
    assert info == wanted, expect.subset(info, wanted)
    assert not dut.state.keys("HW_MUX_CABLE_TABLE_PEER")
    assert dut.config.get("XCVRD_LOG", "Y_CABLE") == {"log_verbosity": "notice"}


def test_startup_active_active(dut):
    """At startup, ycabled queries an active-active port's admin forwarding
    states from the SoC and publishes this ToR's in HW_MUX_CABLE_TABLE and the
    peer's in HW_MUX_CABLE_TABLE_PEER. It writes placeholder MUX_CABLE_INFO and
    no MUX_CABLE_STATIC_INFO, and subscribes to the SoC's GracefulRestart
    notifications.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, with a
       SoC double at 192.168.0.3: port id 0 active, port id 1 standby.
    2. Start ycabled.
    3. Expect HW_MUX_CABLE_TABLE {state: active, read_side: 0, active_side:
       active} and HW_MUX_CABLE_TABLE_PEER {state: standby, read_side: 0,
       active_side: standby}, and a first QueryAdminForwardingPortState
       {portid: [0, 1], state: [False, False]}.
    4. Expect MUX_CABLE_INFO with a time_post timestamp and the placeholder
       values, and no MUX_CABLE_STATIC_INFO.
    5. Expect the NotifyGracefulRestartStart request {tor: 1}: Python sends
       UPPER_TOR from read side 0, the lower ToR.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=soc_ip("Ethernet0") + "/32"))
    soc = dut.soc(soc_ip("Ethernet0"))
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
    # GracefulRestart: Python sends UPPER_TOR (1) from read side 0, the lower ToR.
    assert soc.wait_rpcs("NotifyGracefulRestartStart")[0].request == {"tor": 1}


def test_startup_mixed_cable_types(dut):
    """A device with both cable types initializes and serves both.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       with a SoC double at 192.168.0.5.
    2. Start ycabled.
    3. Expect both ports' state active.
    4. Toggle each port to standby, and expect standby.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4=soc_ip("Ethernet4") + "/32")
    dut.configure(topo)
    dut.soc(soc_ip("Ethernet4"))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet4")["state"] == "active"
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


def test_mux_cable_without_state_is_ignored(dut):
    """A MUX_CABLE entry without a state is skipped at startup: no
    HW_MUX_CABLE_TABLE row, and no call to its cable.

    Steps:
    1. Configure active-standby ports Ethernet0, whose MUX_CABLE entry has no
       state, and Ethernet4.
    2. Start ycabled.
    3. Expect HW_MUX_CABLE_TABLE rows for Ethernet4 only, and no driver call to
       Ethernet0's cable.
    """
    topo = Topology().active_standby("Ethernet0", "Ethernet4")
    topo.mux["Ethernet0"].state = None
    dut.configure(topo)
    dut.start()
    assert dut.state.keys("HW_MUX_CABLE_TABLE") == ["Ethernet4"]
    assert not dut.hw.calls(port=1)


def test_startup_without_mux_cables(dut):
    """On a device without MUX_CABLE entries, ycabled writes only XCVRD_LOG,
    and answers no CLI command.

    Steps:
    1. Configure a device without mux ports.
    2. Start ycabled. It counts as ready once it has subscribed to
       TRANSCEIVER_INFO. Wait 1 s.
    3. Expect ycabled's writes in CONFIG_DB XCVRD_LOG only.
    4. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0 without waiting, and
       expect no response within 2 s.
    """
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
    """ycabled waits for CONFIG_DB_INITIALIZED before it starts its work,
    without exiting, and goes on once the flag is set.

    Steps:
    1. Configure Ethernet0 as the only active-standby port, without
       CONFIG_DB_INITIALIZED.
    2. Start ycabled without waiting until it is ready. Wait until it has
       looked at CONFIG_DB_INITIALIZED and gone quiet.
    3. Expect ycabled running, and no HW_MUX_CABLE_TABLE row for Ethernet0.
    4. Set CONFIG_DB_INITIALIZED to 1, wait until ycabled is ready, and expect
       Ethernet0's state active.
    """
    topo = Topology().active_standby("Ethernet0")
    topo.config_db_initialized = False
    dut.configure(topo)
    dut.start(ready=False)
    # ycabled is waiting once it has looked at the flag and gone quiet.
    dut.monitor.wait_access(dut.start_mark, 15, db="CONFIG_DB", table="CONFIG_DB_INITIALIZED")
    dut.monitor.wait_settled()
    assert dut.proc.running()
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet0")
    dut.config.r.set("CONFIG_DB_INITIALIZED", "1")
    dut.wait_ready()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


@pytest.mark.known_issue("a MUX_CABLE state outside the YANG enum makes check_mux_cable_port_type "
                         "return None; the caller dies unpacking it, so ycabled exits at startup")
def test_mux_cable_state_outside_yang_enum(dut):
    """sonic-mux-cable.yang allows auto, manual, detach, active and standby. A
    port with another state is skipped, and the other ports are served.

    Steps:
    1. Configure active-standby ports Ethernet0, whose MUX_CABLE state is
       bogus, and Ethernet4.
    2. On Python ycabled, let the teardown accept the crash and the exit of the
       known issue. Start ycabled without waiting until it is ready, and wait
       up to 15 s for it to exit or to publish Ethernet4.
    3. Expect ycabled still running, and wait until it is ready.
    4. Expect no HW_MUX_CABLE_TABLE row for Ethernet0; toggle Ethernet4 to
       standby and expect standby.
    """
    topo = Topology().active_standby("Ethernet0", "Ethernet4")
    topo.mux["Ethernet0"].state = "bogus"
    dut.configure(topo)
    dut.tolerate_python_failure(crash=True, exit=True)
    dut.start(ready=False)
    eventually(lambda: not dut.proc.running() or dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4"), 15)
    assert dut.proc.running(), dut.proc.output()[-2000:]
    dut.wait_ready()
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet0")
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


def test_startup_platform_api_failure_exits(dut):
    """When the platform API cannot be loaded, ycabled exits non-zero, for
    supervisord to start it again: with neither the platform API nor an sfputil
    plugin, Python exits with SFPUTIL_LOAD_ERROR (1).

    Steps:
    1. Configure Ethernet0 as the only active-standby port. The fake platform
       API's Platform() raises RuntimeError.
    2. Allow the exit, and start ycabled without waiting until it is ready.
    3. Expect it to exit within 20 s, with a code other than 0.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.platform_init_error = "RuntimeError: platform API broken"
    dut.allow_exit = True
    dut.start(ready=False)
    # No platform API and no sfputil plugin either: exit, for supervisord to
    # start it again. Python exits with SFPUTIL_LOAD_ERROR (1).
    assert dut.proc.wait_exit(20) not in (None, 0)


def test_startup_port_map_from_config_db(dut):
    """The port map comes from CONFIG_DB PORT; port_config.ini is not needed.

    Steps:
    1. Configure Ethernet0 as the only active-standby port, and delete
       port_config.ini.
    2. Start ycabled.
    3. Expect Ethernet0's state active.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    os.unlink(os.path.join(CONTAINER_PLATFORM_DIR, dut.topo.hwsku, "port_config.ini"))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


def test_startup_port_map_from_port_config_file(dut):
    """Without CONFIG_DB PORT, the port map comes from port_config.ini.

    Steps:
    1. Configure Ethernet0 as the only active-standby port, without CONFIG_DB
       PORT.
    2. Start ycabled.
    3. Expect Ethernet0's state active.
    """
    topo = Topology().active_standby("Ethernet0")
    topo.port_table = False
    dut.configure(topo)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


@pytest.mark.python_only
def test_startup_without_port_map_exits(dut):
    """No CONFIG_DB PORT and no port_config.ini: Python exits, so that
    supervisord starts it again. The code is 1, from SfpUtilHelper's own
    sys.exit, not ycabled's PORT_CONFIG_LOAD_ERROR (2); only that it is not 0
    matters to supervisord (autorestart=unexpected).

    Steps:
    1. Configure Ethernet0 as the only active-standby port, without CONFIG_DB
       PORT, and delete port_config.ini.
    2. Allow the exit, and start ycabled without waiting until it is ready.
    3. Expect it to exit within 20 s, with a code other than 0.
    """
    topo = Topology().active_standby("Ethernet0")
    topo.port_table = False
    dut.configure(topo)
    os.unlink(os.path.join(CONTAINER_PLATFORM_DIR, dut.topo.hwsku, "port_config.ini"))
    dut.allow_exit = True
    dut.start(ready=False)
    assert dut.proc.wait_exit(20) not in (None, 0)


def test_stop_keeps_tables(dut):
    """Stopping ycabled leaves its tables in place, so the last state stays
    visible to orchagent, linkmgrd and `show muxcable` across a restart (HLD
    C7). This departs on purpose from what Python intends: its deinit() would
    delete the HW_MUX_CABLE_TABLE, MUX_CABLE_STATIC_INFO and MUX_CABLE_INFO
    rows, and the GRPCCLIENT keys it copied from grpc_secrets.json, but it
    never runs, because SIGTERM never completes (the known issue of
    test_sigterm_exits_promptly). A Python fix for that issue has to keep the
    tables to pass.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       with a SoC double at 192.168.0.5.
    2. Start ycabled, and take a snapshot of STATE_DB.
    3. Stop ycabled as supervisord does: SIGTERM, then SIGKILL if it is still
       running after the timeout.
    4. Expect every HW_MUX_CABLE_TABLE, HW_MUX_CABLE_TABLE_PEER, MUX_CABLE_INFO
       and MUX_CABLE_STATIC_INFO row as in the snapshot, and no delete by
       ycabled in STATE_DB or CONFIG_DB since it started.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4=soc_ip("Ethernet4") + "/32")
    dut.configure(topo)
    dut.soc(soc_ip("Ethernet4"))
    dut.start()
    before = dut.dbs.snapshot()["STATE_DB"]
    dut.stop()
    after = dut.dbs.snapshot()["STATE_DB"]
    for key in before:
        if key.split("|")[0] in AS_ROW_KEYS + ("HW_MUX_CABLE_TABLE_PEER",):
            assert after.get(key) == before[key], key
    deletes = [a for a in dut.monitor.accesses(dut.start_mark, kind="delete") if a.db in ("STATE_DB", "CONFIG_DB")]
    assert not deletes, deletes


@pytest.mark.known_issue("SIGTERM never completes: run() joins threads that never stop")
def test_sigterm_exits_promptly(dut):
    """SIGTERM ends ycabled, with exit code 0, within 2 s (N9).

    Steps:
    1. Configure Ethernet0 as the only active-standby port.
    2. Start ycabled.
    3. Send SIGTERM, with SIGKILL after 5 s.
    4. Expect ycabled to have exited on SIGTERM, with code 0, within 2 s.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    result = dut.stop(sigterm_timeout=5)
    assert result.exited_on_sigterm and result.returncode == 0
    assert result.sigterm_to_exit <= 2


def test_sighup_is_ignored(dut):
    """SIGHUP is ignored, as in Python: ycabled keeps running and serving.

    Steps:
    1. Configure Ethernet0 as the only active-standby port.
    2. Start ycabled, and send it SIGHUP.
    3. Probe Ethernet0 and expect active, and ycabled still running.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    os.kill(dut.proc.pid, signal.SIGHUP)
    assert dut.probe("Ethernet0") == "active"
    assert dut.proc.running()


def test_restart_reads_cable_then_replays_last_request(dut):
    """At startup ycabled publishes the cable's state, then applies the last
    request still in APPL_DB HW_MUX_CABLE_TABLE, since its subscription reads
    the existing entries first. Startup also sets XCVRD_LOG|Y_CABLE
    log_verbosity back to notice.

    Steps:
    1. Configure Ethernet0 as the only active-standby port.
    2. Start ycabled, toggle Ethernet0 to standby and expect standby, then stop
       ycabled.
    3. While it is down, point the mux back at this ToR, ToR A, and set
       XCVRD_LOG|Y_CABLE log_verbosity to debug.
    4. Start ycabled again, and wait for two HW_MUX_CABLE_TABLE state writes
       for Ethernet0.
    5. Expect active, read from the cable, then standby, from the replayed
       request, with one toggle_mux_to_tor_b() call; and log_verbosity notice
       again.
    """
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
    so supervisord restarts it. The worker is killed with the crash of the
    known issue of test_toggle_to_other_state_is_ignored.

    Steps:
    1. Configure Ethernet0 as the only active-standby port.
    2. Start ycabled, and allow the exit and the crash.
    3. Write HW_MUX_CABLE_TABLE state=init for Ethernet0, and expect 'Exception
       occured at child thread YCableTableUpdateTask' in syslog within 5 s.
    4. Save Python's coverage data, and expect ycabled killed by SIGKILL within
       75 s.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    dut.allow_exit = dut.allow_crash = True
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"state": "init"})
    dut.syslog.wait_for(dut.log_mark, 5, contains="Exception occured at child thread YCableTableUpdateTask")
    dut.proc.save_coverage()
    assert dut.proc.wait_exit(75) == -signal.SIGKILL


@pytest.mark.known_issue("the syslog identity changes with import order (pmon#bmc_base)")
def test_syslog_identity(dut):
    """ycabled logs as `ycable`: sonic-mgmt's log analyzer ignore rules match
    `pmon#ycable`. Python's identity is process-wide (`openlog`), so a module
    that creates a `Logger` later replaces it, such as sonic_platform_base's
    bmc_base: the messages then appear as `pmon#bmc_base`, and the rules miss
    them.

    Steps:
    1. Configure Ethernet0 as the only active-standby port. Its cable's
       toggle_mux_to_tor_b() returns False, so that a toggle logs a warning.
    2. Start ycabled, and toggle Ethernet0 to standby.
    3. Wait until ycabled has logged, and expect the identity ycable on every
       message.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.port(1).script("toggle_mux_to_tor_b", value=False)
    dut.start()
    dut.toggle("Ethernet0", "standby")
    wait_until(lambda: dut.logs(), 5)
    idents = {m.ident for m in dut.logs()}
    assert idents == {"ycable"}, idents
