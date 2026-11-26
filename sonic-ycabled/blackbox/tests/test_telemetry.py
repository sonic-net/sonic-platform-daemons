"""Periodic telemetry: MUX_CABLE_INFO every 60 s (HLD 3.3.8, 3.5.4).

The module waits for one telemetry pass of a single ycabled, then checks each
kind of port against what that pass wrote."""

import re
import time

import pytest

from harness import expect
from harness.certs import SOC_NAME, Pki
from harness.contract import MUX_CABLE_INFO_ACTIVE_ACTIVE, MUX_CABLE_INFO_ACTIVE_STANDBY
from harness.fake_hw import FIRMWARE_DOWNLOAD_STATUS_INPROGRESS, TARGET_LOWER_TOR
from harness.topology import LOOPBACK3_UPPER, Topology, soc_ip
from harness.util import TIMESTAMP_RE, wait_until

pytestmark = pytest.mark.slow

LAST_KNOWN = {"version_{}_{}".format(s, v): "9.9.{}".format(i)
              for i, (s, v) in enumerate((s, v) for s in ("self", "peer", "nic")
                                         for v in ("active", "inactive", "next"))}
PASS_TIMEOUT = 90
CERT_DIR = "/etc/sonic/bbcerts"

PORTS = {
    "Ethernet0": 1,    # active-standby, read from the upper ToR
    "Ethernet4": 2,    # active-standby, read from the lower ToR, driver calls fail
    "Ethernet8": 3,    # active-standby, cable absent
    "Ethernet12": 4,   # active-standby, firmware download in progress
    "Ethernet16": 5,   # active-active, SoC answers
    "Ethernet20": 6,   # active-active, SoC unreachable
    "Ethernet24": 7,   # active-standby, no driver for the cable
    "Ethernet28": 8,   # active-standby, read from the upper ToR, every driver call raises
}


# Ethernet28: per target, cursor values raise (the NIC, the lower ToR) or are not a list (the upper ToR).
def _cursors(model, args):
    lane, target = args
    if target == 1:
        return -1
    raise OSError("I2C failure")


@pytest.fixture(scope="module")
def run(module_dut):
    """The module's telemetry pass (_telemetry_pass), shared by its tests: the
    held download is released and ycabled stopped before they run, and the
    teardown's problems are kept for test_no_problems."""
    dut = module_dut
    try:
        result = _telemetry_pass(dut)
    except BaseException as e:
        # Stop now, also when the setup failed: the function-scoped tests below
        # share redis and the fake hardware (whose reset releases the gate).
        problems = dut.teardown()
        if problems and not isinstance(e, KeyboardInterrupt):
            # Not an AssertionError, which test_peer_eye_height_lane2 would take for its known issue.
            raise pytest.fail.Exception("telemetry pass failed with {!r}; ycabled problems:\n{}".format(
                e, "\n".join(problems))) from e
        raise
    result.pop("gate").set()
    result["problems"] = dut.teardown()
    yield result
    # test_no_problems reports them; in a run without it (-k), the fixture does.
    # pytest.fail: an AssertionError would pass as a known issue's xfail.
    if result["problems"] and not result.get("checked"):
        pytest.fail("ycabled problems in the telemetry pass:\n" + "\n".join(result["problems"]), pytrace=False)


def _telemetry_pass(dut):
    """One telemetry pass of a device with a port of each kind (PORTS), for the
    tests of the module.

    Steps:
    1. Configure six active-standby ports and two active-active ones, as PORTS
       lists them. Ethernet4's cable is read from the lower ToR, and some of
       its driver methods raise or return invalid values; the driver methods of
       Ethernet28's cable raise or return invalid values; Ethernet8's cable is
       absent; Ethernet24's has no driver. Ethernet16's SoC answers, with mixed
       link and operation states; nothing listens at Ethernet20's SoC address.
    2. Write Ethernet12's last known firmware versions (LAST_KNOWN) to its
       MUX_CABLE_INFO, as a previous ycabled would have, and write the firmware
       image bb-fw.bin.
    3. Start ycabled, and note MUX_CABLE_INFO as startup wrote it.
    4. Start a firmware download on Ethernet12, held in the driver, and send
       two forwarding state probes for Ethernet16.
    5. Wait up to 90 s for the pass's write of Ethernet20's
       grpc_connection_status, then for its write for Ethernet28, the last
       port, and for ycabled to go quiet.
    6. Return MUX_CABLE_INFO and MUX_CABLE_STATIC_INFO, the pass's writes,
       driver calls and RPCs, and the time from the start to the pass.
    """
    topo = Topology(nports=8)
    topo.active_standby("Ethernet0", "Ethernet4", "Ethernet8", "Ethernet12", "Ethernet24", "Ethernet28")
    topo.active_active("Ethernet16", soc_ipv4=soc_ip("Ethernet16") + "/32")
    topo.active_active("Ethernet20", soc_ipv4=soc_ip("Ethernet20") + "/32")
    dut.configure(topo)
    b = dut.hw.port(2)
    b.read_side = TARGET_LOWER_TOR
    b.script("get_eye_heights", raises="OSError")
    b.script("get_local_temperature", value=-1)
    b.script("get_nic_voltage", value="bad")
    b.script("get_switch_count_total", raises="OSError")
    b.script("get_active_linked_tor_side", value=-1)
    b.script("is_link_active", raises="OSError")
    b.script("get_firmware_version", value=None)
    h = dut.hw.port(8)
    for method in ("get_active_linked_tor_side", "get_mux_direction", "is_link_active", "get_local_temperature",
                   "get_local_voltage", "get_nic_voltage", "get_nic_temperature", "get_switch_count_total"):
        h.script(method, raises="OSError: I2C failure")
    h.script("get_eye_heights", value=-1)
    h.script("get_target_cursor_values", func=_cursors)
    dut.hw.port(3).present = False
    dut.hw.port(7).transceiver_info = {"manufacturer": "Nobody", "model": "X1"}
    soc = dut.soc(soc_ip("Ethernet16"))
    soc.admin, soc.link, soc.oper = [True, False], [True, False], [False, True]
    # What a previous ycabled left in STATE_DB for the cable being upgraded.
    dut.state.set("MUX_CABLE_INFO", "Ethernet12", LAST_KNOWN)
    dut.firmware_file("bb-fw.bin")
    dut.start()
    started = time.time()
    init_info = dut.state.table("MUX_CABLE_INFO")

    gate = dut.hw.port(4).script("download_firmware", block=True)
    dut.cli("XCVRD_DOWN_FW_CMD", "bb-fw.bin", "Ethernet12", wait=False)
    wait_until(lambda: dut.hw.calls(port=4, method="download_firmware"), 5)
    assert dut.hw.port(4).download_firmware_status == FIRMWARE_DOWNLOAD_STATUS_INPROGRESS
    for _ in range(2):
        dut.fwd_probe("Ethernet16")

    mark, hw_mark, soc_mark = dut.monitor.mark(), dut.hw.mark(), soc.mark()
    dut.monitor.wait_access(mark, PASS_TIMEOUT, kind="write", table="MUX_CABLE_INFO", key="Ethernet20",
                            field="grpc_connection_status")
    passed = time.time()
    # The pass goes through the ports in order; Ethernet28 is the last one.
    dut.monitor.wait_access(mark, 10, kind="write", table="MUX_CABLE_INFO", key="Ethernet28")
    dut.monitor.wait_settled()
    return {
        "gate": gate,
        "init": init_info,
        "static": dut.state.table("MUX_CABLE_STATIC_INFO"),
        "info": dut.state.table("MUX_CABLE_INFO"),
        "writes": dut.monitor.accesses(mark, kind="write"),
        "hw_calls": dut.hw.calls(hw_mark),
        "soc_rpcs": soc.rpcs(since=soc_mark),
        "delay": passed - started,
        "expected": expect.mux_cable_info(dut.hw.port(1)),
        "eye_lower_tor": list(dut.hw.port(1).eye_heights[TARGET_LOWER_TOR]),
    }


def test_no_problems(run):
    """The pass leaves nothing for the teardown to report: ycabled did not exit
    or crash, and wrote nothing outside the DB contract.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect no problem from the teardown that followed it.
    """
    run["checked"] = True
    assert not run["problems"], run["problems"]


def test_first_pass_after_60s(run):
    """The first telemetry pass comes 60 s after startup.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect its write for Ethernet20 59 s to 70 s after ycabled's start.
    """
    assert 59 <= run["delay"] < 70, run["delay"]


def test_active_standby(run):
    """The pass writes an active-standby cable's MUX_CABLE_INFO with the values
    its driver reads.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect the MUX_CABLE_INFO of Ethernet0, read from the upper ToR, whose
       driver answers every call: a time_post timestamp and the cable's
       values, besides peer_eye_height_lane2 (test_peer_eye_height_lane2).
    """
    info = dict(run["info"]["Ethernet0"])
    assert re.match(TIMESTAMP_RE, info.pop("time_post"))
    wanted = dict(run["expected"])
    wanted.pop("peer_eye_height_lane2")  # a known issue, see test_peer_eye_height_lane2 below
    info.pop("peer_eye_height_lane2")
    assert info == wanted, expect.subset(info, wanted)


@pytest.mark.known_issue("peer_eye_height_lane2 is written from lane 1")
def test_peer_eye_height_lane2(run):
    """peer_eye_height_lane2 is the peer's eye height on lane 2.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect Ethernet0's peer_eye_height_lane2 to be the second eye height its
       cable reports for the lower ToR, the peer of the upper ToR it is read
       from.
    """
    assert run["info"]["Ethernet0"]["peer_eye_height_lane2"] == str(run["eye_lower_tor"][1])


def test_active_standby_driver_failures(run):
    """Values the driver cannot read are N/A, unknown or down; the values it
    reads are still written.

    Steps:
    1. Take the module's telemetry pass (the run fixture). Ethernet4's cable is
       read from the lower ToR. Its get_eye_heights(), get_switch_count_total()
       and is_link_active() raise; get_local_temperature() returns -1,
       get_nic_voltage() 'bad', get_active_linked_tor_side() -1, and
       get_firmware_version() None.
    2. Expect N/A in the switch counts, internal_temperature, nic_voltage,
       every eye height and every version, and in no other field.
    3. Expect tor_active unknown, the three link statuses down, mux_direction
       peer (the mux points at the upper ToR), and the values the driver read:
       internal_voltage 3.3 and nic_temperature 45.
    """
    info = run["info"]["Ethernet4"]
    na = [k for k, v in info.items() if v == "N/A"]
    assert set(na) == {"manual_switch_count", "auto_switch_count", "internal_temperature", "nic_voltage",
                       "self_eye_height_lane1", "self_eye_height_lane2", "peer_eye_height_lane1",
                       "peer_eye_height_lane2", "nic_eye_height_lane1", "nic_eye_height_lane2",
                       "version_self_active", "version_self_inactive", "version_self_next",
                       "version_peer_active", "version_peer_inactive", "version_peer_next",
                       "version_nic_active", "version_nic_inactive", "version_nic_next"}, na
    assert info["tor_active"] == "unknown"
    assert info["link_status_self"] == info["link_status_peer"] == info["link_status_nic"] == "down"
    # Read from the lower ToR: the mux points to the upper ToR, the peer.
    assert info["mux_direction"] == "peer"
    assert info["internal_voltage"] == "3.3" and info["nic_temperature"] == "45"


def test_absent_cable(run):
    """The pass writes the placeholder MUX_CABLE_INFO of an absent cable again.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect Ethernet8's MUX_CABLE_INFO with a time_post timestamp and the
       placeholder values, and a write for Ethernet8 during the pass.
    """
    info = dict(run["info"]["Ethernet8"])
    assert re.match(TIMESTAMP_RE, info.pop("time_post"))
    assert info == expect.PLACEHOLDER_INFO
    assert any(a.key == "Ethernet8" for a in run["writes"]), "rewritten by the pass"


def test_download_in_progress_keeps_last_known_versions(run):
    """While a firmware download runs on a cable, the pass keeps the last known
    versions in MUX_CABLE_INFO without asking the cable; the other fields are
    read as usual.

    Steps:
    1. Take the module's telemetry pass (the run fixture). Ethernet12's
       download is held throughout.
    2. Expect Ethernet12's version fields as LAST_KNOWN, no
       get_firmware_version() call to its cable during the pass, and
       mux_direction self.
    """
    info = run["info"]["Ethernet12"]
    assert {k: info[k] for k in LAST_KNOWN} == LAST_KNOWN
    assert not [c for c in run["hw_calls"] if c.port == 4 and c.method == "get_firmware_version"]
    assert info["mux_direction"] == "self"


def test_active_active(run):
    """For an active-active port, the pass asks the SoC for its link, operation
    and admin forwarding states and its server version, and writes them to
    MUX_CABLE_INFO with the probe counts and the connection status, besides the
    placeholder active-standby fields from startup.

    Steps:
    1. Take the module's telemetry pass (the run fixture). Ethernet16's SoC
       reports, for port ids 0 and 1: admin active and standby, link up and
       down, operation down and up.
    2. Expect MUX_CABLE_INFO with the fields of both cable types: self and peer
       link state up and down, operation state down and up, mux direction
       active and standby, server_version bb-soc-1.0, grpc_connection_status
       READY.
    3. Expect the probe counts from before the pass: 3 for the mux directions,
       the startup query and 2 probes; unknown for the link and operation
       states, never queried before.
    4. Expect the startup placeholders in the active-standby fields.
    5. Expect QueryLinkState, QueryOperationPortState,
       QueryAdminForwardingPortState and QueryServerVersion, in that order, for
       port ids [0, 1] and version 1.0.
    """
    info = run["info"]["Ethernet16"]
    assert set(info) == set(MUX_CABLE_INFO_ACTIVE_STANDBY) | set(MUX_CABLE_INFO_ACTIVE_ACTIVE)
    assert {k: info[k] for k in MUX_CABLE_INFO_ACTIVE_ACTIVE if k != "time_post"} == {
        "self_link_state": "up", "peer_link_state": "down",
        "self_oper_state": "down", "peer_oper_state": "up",
        "self_mux_direction": "active", "peer_mux_direction": "standby",
        "server_version": "bb-soc-1.0", "grpc_connection_status": "READY",
        # The counts before this pass: the initial query and 2 probes. Link
        # and operation states were never queried before.
        "mux_direction_probe_count": "3", "peer_mux_direction_probe_count": "3",
        "link_state_probe_count": "unknown", "peer_link_state_probe_count": "unknown",
        "operation_state_probe_count": "unknown", "peer_operation_state_probe_count": "unknown",
    }
    # The placeholder active-standby fields from startup stay.
    assert all(info[k] == v for k, v in expect.PLACEHOLDER_INFO.items())
    assert [r.name for r in run["soc_rpcs"]] == ["QueryLinkState", "QueryOperationPortState",
                                                 "QueryAdminForwardingPortState", "QueryServerVersion"]
    assert [r.request for r in run["soc_rpcs"]] == [{"portid": [0, 1]}, {"portid": [0, 1]},
                                                    {"portid": [0, 1], "state": [False, False]}, {"version": "1.0"}]


def test_active_active_soc_unreachable(run):
    """For an active-active port whose SoC cannot be reached, the pass writes
    unknown states, server_version N/A, and grpc_connection_status
    TRANSIENT_FAILURE, which grpc-core keeps while it retries: the pass's
    failing RPCs do not report the failure to linkmgrd again.

    Steps:
    1. Take the module's telemetry pass (the run fixture). Nothing listens at
       Ethernet20's SoC address: its connections are refused.
    2. Expect the self and peer link, operation and mux direction states
       unknown, server_version N/A, and grpc_connection_status
       TRANSIENT_FAILURE.
    3. Expect no FORWARDING_STATE_RESPONSE write for Ethernet20 during the
       pass.
    """
    info = run["info"]["Ethernet20"]
    assert {k: info[k] for k in ("self_link_state", "peer_link_state", "self_oper_state", "peer_oper_state",
                                 "self_mux_direction", "peer_mux_direction")} == dict.fromkeys(
        ("self_link_state", "peer_link_state", "self_oper_state", "peer_oper_state", "self_mux_direction",
         "peer_mux_direction"), "unknown")
    assert info["server_version"] == "N/A"
    assert info["grpc_connection_status"] == "TRANSIENT_FAILURE"
    assert not [a for a in run["writes"] if a.table == "FORWARDING_STATE_RESPONSE" and a.key == "Ethernet20"]


def test_active_standby_driver_exceptions(run):
    """When the driver calls raise, the pass writes unknown, down or N/A for
    them, and still writes the port.

    Steps:
    1. Take the module's telemetry pass (the run fixture). The driver methods
       of Ethernet28's cable that the pass calls raise OSError, or return
       invalid values.
    2. Expect tor_active and mux_direction unknown, the three link statuses
       down, and N/A in the switch counts, temperatures, voltages and the eye
       heights checked.
    """
    info = run["info"]["Ethernet28"]
    assert {k: info[k] for k in ("tor_active", "mux_direction", "link_status_self", "link_status_peer",
                                 "link_status_nic")} == {
        "tor_active": "unknown", "mux_direction": "unknown", "link_status_self": "down",
        "link_status_peer": "down", "link_status_nic": "down"}
    for k in ("manual_switch_count", "auto_switch_count", "internal_temperature", "internal_voltage",
              "nic_temperature", "nic_voltage", "self_eye_height_lane1", "nic_eye_height_lane2"):
        assert info[k] == "N/A", k


def test_static_info_driver_failures(run):
    """MUX_CABLE_STATIC_INFO, written at startup, of a cable whose cursor
    values cannot be read holds its read side and N/A in every other field.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
       get_target_cursor_values() of Ethernet28's cable raises for the NIC and
       the lower ToR, and returns -1 for the upper ToR.
    2. Expect Ethernet28's MUX_CABLE_STATIC_INFO read_side tor1, and N/A in
       every other field.
    """
    static = dict(run["static"]["Ethernet28"])
    assert static.pop("read_side") == "tor1"
    assert set(static.values()) == {"N/A"}


def test_cable_without_driver_is_skipped(run):
    """No driver: the pass logs an error and leaves the startup placeholders.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect Ethernet24's MUX_CABLE_INFO as startup wrote it, and no write for
       Ethernet24 during the pass.
    """
    assert run["info"]["Ethernet24"] == run["init"]["Ethernet24"]
    assert not [a for a in run["writes"] if a.key == "Ethernet24"]


def test_static_info_only_at_startup(run):
    """MUX_CABLE_STATIC_INFO is written at startup only: the pass does not
    write it.

    Steps:
    1. Take the module's telemetry pass (the run fixture).
    2. Expect no MUX_CABLE_STATIC_INFO write during the pass.
    """
    assert not [a for a in run["writes"] if a.table == "MUX_CABLE_STATIC_INFO"]


def _short_reply(request, pb2):
    return pb2.OperationReply(portid=[0], state=[True])


def test_disable_telemetry(dut):
    """XCVRD_LOG|Y_CABLE disable_telemetry True stops the telemetry passes, and
    False starts them again. Startup's reset of the log level leaves the field
    alone.

    Also: telemetry of the upper ToR's active-active port (read side 1), whose
    SoC sends a malformed operation state reply.

    Steps:
    1. Configure the upper ToR with Ethernet0 as active-standby, Ethernet4 as
       active-active with a SoC double at 192.168.0.5, and XCVRD_LOG|Y_CABLE
       disable_telemetry=True. The SoC's link is down for port id 0 and up for
       port id 1, and it answers QueryOperationPortState for port id 0 only.
    2. Start ycabled, and expect XCVRD_LOG|Y_CABLE {disable_telemetry: True,
       log_verbosity: notice}.
    3. Wait 65 s, and expect no MUX_CABLE_INFO write.
    4. Set disable_telemetry=False. Expect a pass to write Ethernet0's
       MUX_CABLE_INFO within 65 s, then Ethernet4's grpc_connection_status
       within 10 s.
    5. Expect Ethernet4's self and peer link states up and down (self is index
       1 of the SoC's lists), operation states unknown, and mux directions
       standby and active.
    """
    topo = Topology(loopback3=LOOPBACK3_UPPER).active_standby("Ethernet0")
    topo.active_active("Ethernet4", soc_ipv4=soc_ip("Ethernet4") + "/32")
    topo.xcvrd_log = {"disable_telemetry": "True"}
    dut.configure(topo)
    soc = dut.soc(soc_ip("Ethernet4"))
    soc.link = [False, True]
    soc.script("QueryOperationPortState", reply=_short_reply)
    dut.start()
    # The startup reset of the log level leaves the field alone.
    assert dut.config.get("XCVRD_LOG", "Y_CABLE") == {"disable_telemetry": "True", "log_verbosity": "notice"}
    mark = dut.monitor.mark()
    time.sleep(65)
    assert not dut.writes(since=mark, table="MUX_CABLE_INFO")
    dut.config.set("XCVRD_LOG", "Y_CABLE", {"disable_telemetry": "False"})
    dut.monitor.wait_access(mark, 65, kind="write", table="MUX_CABLE_INFO", key="Ethernet0")
    dut.monitor.wait_access(mark, 10, kind="write", table="MUX_CABLE_INFO", key="Ethernet4",
                            field="grpc_connection_status")
    info = dut.state.get("MUX_CABLE_INFO", "Ethernet4")
    # Read side 1: self is index 1 of the SoC's lists.
    assert (info["self_link_state"], info["peer_link_state"]) == ("up", "down")
    assert (info["self_oper_state"], info["peer_oper_state"]) == ("unknown", "unknown")
    assert (info["self_mux_direction"], info["peer_mux_direction"]) == ("standby", "active")


@pytest.mark.known_issue("telemetry of an active-active port without HW_MUX_CABLE_TABLE (no SoC "
                         "address) raises NameError and kills the telemetry thread")
def test_active_active_port_without_soc_address(dut):
    """The pass skips an active-active port without a SoC address and goes on
    to the next port, without a crash.

    Steps:
    1. Configure Ethernet0 as active-active without a SoC address, and
       Ethernet4 as active-standby.
    2. Start ycabled. On Python ycabled, let the teardown accept the crash and
       the exit of the known issue.
    3. Expect the pass to write Ethernet4's MUX_CABLE_INFO, after Ethernet0's
       turn, within 90 s.
    4. Wait for syslog to catch up, and expect no crash in syslog or in
       ycabled's output.
    """
    dut.configure(Topology().active_active("Ethernet0").active_standby("Ethernet4"))
    dut.start()
    dut.tolerate_python_failure(crash=True, exit=True)
    mark = dut.monitor.mark()
    # Ethernet4 comes after Ethernet0 in the pass.
    assert dut.monitor.eventually(mark, PASS_TIMEOUT, kind="write", table="MUX_CABLE_INFO", key="Ethernet4"), \
        "the telemetry pass did not reach Ethernet4"
    dut.syslog.barrier()
    assert not dut.crash_evidence()


def test_active_active_connection_status_follows_soc(dut):
    """grpc_connection_status, which `show muxcable hwmode muxdirection` shows
    as ConnectivityState, is the state of the port's channel at the pass, as
    grpc-core reports it: TRANSIENT_FAILURE for a SoC that went away after
    startup and for one that accepts TCP connections but never answers, READY
    for one that came up after startup.

    Steps:
    1. Configure Ethernet0, Ethernet4 and Ethernet8 as active-active ports,
       with a SoC double at Ethernet0's SoC address, nothing listening at
       Ethernet4's, and a blackhole, which accepts TCP connections and sends
       nothing, at Ethernet8's.
    2. Start ycabled, and expect a forwarding state probe of Ethernet0
       answered active: its channel is up.
    3. Stop Ethernet0's SoC double. Start one at Ethernet4's SoC address, and
       send forwarding state probes of Ethernet4, as linkmgrd keeps doing,
       until one is answered active, within 15 s.
    4. Wait up to 90 s for the pass's writes of grpc_connection_status for
       the three ports.
    5. Expect Ethernet0's and Ethernet8's TRANSIENT_FAILURE, and Ethernet4's
       READY.
    """
    ports = ("Ethernet0", "Ethernet4", "Ethernet8")
    topo = Topology()
    for port in ports:
        topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
    dut.configure(topo)
    gone = dut.soc(soc_ip("Ethernet0"))
    dut.blackhole(soc_ip("Ethernet8"))
    dut.start()
    assert dut.fwd_probe("Ethernet0")["response"] == "active"
    gone.stop()
    dut.soc(soc_ip("Ethernet4"))
    # gRPC reconnects with backoff; linkmgrd keeps probing.
    wait_until(lambda: dut.fwd_probe("Ethernet4")["response"] == "active", 15)
    mark = dut.monitor.mark()
    for port in ports:
        dut.monitor.wait_access(mark, PASS_TIMEOUT, kind="write", table="MUX_CABLE_INFO", key=port,
                                field="grpc_connection_status")
    assert {port: dut.state.field("MUX_CABLE_INFO", port, "grpc_connection_status") for port in ports} == {
        "Ethernet0": "TRANSIENT_FAILURE", "Ethernet4": "READY", "Ethernet8": "TRANSIENT_FAILURE"}


def test_active_active_connection_status_tls_handshake_failure(dut):
    """A channel whose TLS handshake fails is not up: its
    grpc_connection_status is TRANSIENT_FAILURE, as grpc-core reports it, while
    a channel whose handshake succeeds is READY.

    Steps:
    1. Configure Ethernet0 and Ethernet4 as active-active ports, and
       GRPCCLIENT {type: secure, auth_level: server} with the test PKI's CA,
       the SoC's name and a client certificate and key.
    2. Start TLS SoC doubles with the PKI's server certificate at both SoC
       addresses. Ethernet4's requires a client certificate, which auth_level
       server does not present, so its handshakes fail.
    3. Start ycabled.
    4. Wait up to 90 s for the pass's writes of grpc_connection_status for
       both ports.
    5. Expect Ethernet0's READY and Ethernet4's TRANSIENT_FAILURE.
    """
    ports = ("Ethernet0", "Ethernet4")
    pki = Pki(CERT_DIR, soc_ips=[soc_ip(port) for port in ports])
    dut.files.append(CERT_DIR)
    topo = Topology()
    for port in ports:
        topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
    topo.grpcclient = {"config": {"type": "secure", "auth_level": "server"},
                       "certs": {"ca_crt": pki.paths["ca.crt"], "grpc_ssl_credential": SOC_NAME,
                                 "client_crt": pki.paths["client.crt"], "client_key": pki.paths["client.key"]}}
    dut.configure(topo)
    dut.soc(soc_ip("Ethernet0"), tls=pki.server_tls())
    dut.soc(soc_ip("Ethernet4"), tls=pki.server_tls(require_client_auth=True))
    dut.start()
    mark = dut.monitor.mark()
    for port in ports:
        dut.monitor.wait_access(mark, PASS_TIMEOUT, kind="write", table="MUX_CABLE_INFO", key=port,
                                field="grpc_connection_status")
    assert {port: dut.state.field("MUX_CABLE_INFO", port, "grpc_connection_status") for port in ports} == {
        "Ethernet0": "READY", "Ethernet4": "TRANSIENT_FAILURE"}
