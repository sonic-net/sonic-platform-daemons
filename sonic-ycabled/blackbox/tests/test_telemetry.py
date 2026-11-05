"""Periodic telemetry: MUX_CABLE_INFO every 60 s (HLD 3.4.8, 3.6.4).

The module waits for one telemetry pass of a single ycabled, then checks
each kind of port against what that pass wrote.
"""

import re
import time

import pytest

from harness import expect
from harness.contract import MUX_CABLE_INFO_ACTIVE_ACTIVE, MUX_CABLE_INFO_ACTIVE_STANDBY
from harness.fake_hw import FIRMWARE_DOWNLOAD_STATUS_INPROGRESS, TARGET_TOR_B
from harness.topology import LOOPBACK3_LOWER, Topology
from harness.util import TIMESTAMP_RE, wait_until

pytestmark = pytest.mark.slow

SOC_UP = "127.0.0.2"
SOC_DOWN = "127.0.0.3"
LAST_KNOWN = {"version_{}_{}".format(s, v): "9.9.{}".format(i)
              for i, (s, v) in enumerate((s, v) for s in ("self", "peer", "nic") for v in ("active", "inactive", "next"))}
PASS_TIMEOUT = 90

PORTS = {
    "Ethernet0": 1,    # active-standby, read from ToR A
    "Ethernet4": 2,    # active-standby, read from ToR B, driver calls fail
    "Ethernet8": 3,    # active-standby, cable absent
    "Ethernet12": 4,   # active-standby, firmware download in progress
    "Ethernet16": 5,   # active-active, SoC answers
    "Ethernet20": 6,   # active-active, SoC unreachable
    "Ethernet24": 7,   # active-standby, no driver for the cable
    "Ethernet28": 8,   # active-standby, read from ToR A, every driver call raises
}

# Ethernet28: per target, cursor values raise (NIC, ToR B) or are not a list (ToR A).
def _cursors(model, args):
    lane, target = args
    if target == 1:
        return -1
    raise OSError("I2C failure")


@pytest.fixture(scope="module")
def run(module_dut):
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
    return result


def _telemetry_pass(dut):
    topo = Topology(nports=8)
    topo.active_standby("Ethernet0", "Ethernet4", "Ethernet8", "Ethernet12", "Ethernet24", "Ethernet28")
    topo.active_active("Ethernet16", soc_ipv4=SOC_UP + "/32").active_active("Ethernet20", soc_ipv4=SOC_DOWN + "/32")
    dut.configure(topo)
    b = dut.hw.port(2)
    b.read_side = TARGET_TOR_B
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
    soc = dut.soc(SOC_UP)
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
    time.sleep(1)
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
        "eye_tor_b": list(dut.hw.port(1).eye_heights[TARGET_TOR_B]),
    }


def test_no_problems(run):
    assert not run["problems"], run["problems"]


def test_first_pass_after_60s(run):
    assert 59 <= run["delay"] < 70, run["delay"]


def test_active_standby(run):
    info = dict(run["info"]["Ethernet0"])
    assert re.match(TIMESTAMP_RE, info.pop("time_post"))
    wanted = dict(run["expected"])
    wanted.pop("peer_eye_height_lane2")  # known issue 3.8-4, below
    info.pop("peer_eye_height_lane2")
    assert info == wanted, expect.subset(info, wanted)


@pytest.mark.known_issue("3.8-4", "peer_eye_height_lane2 is written from lane 1")
def test_peer_eye_height_lane2(run):
    assert run["info"]["Ethernet0"]["peer_eye_height_lane2"] == str(run["eye_tor_b"][1])


def test_active_standby_driver_failures(run):
    """Values the driver cannot read are N/A, unknown or down."""
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
    # read side B: the mux points to A, the peer.
    assert info["mux_direction"] == "peer"
    assert info["internal_voltage"] == "3.3" and info["nic_temperature"] == "45"


def test_absent_cable(run):
    info = dict(run["info"]["Ethernet8"])
    assert re.match(TIMESTAMP_RE, info.pop("time_post"))
    assert info == expect.PLACEHOLDER_INFO
    assert any(a.key == "Ethernet8" for a in run["writes"]), "rewritten by the pass"


def test_download_in_progress_keeps_last_known_versions(run):
    info = run["info"]["Ethernet12"]
    assert {k: info[k] for k in LAST_KNOWN} == LAST_KNOWN
    assert not [c for c in run["hw_calls"] if c.port == 4 and c.method == "get_firmware_version"]
    assert info["mux_direction"] == "self"


def test_active_active(run):
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
    info = run["info"]["Ethernet20"]
    assert {k: info[k] for k in ("self_link_state", "peer_link_state", "self_oper_state", "peer_oper_state",
                                 "self_mux_direction", "peer_mux_direction")} == dict.fromkeys(
        ("self_link_state", "peer_link_state", "self_oper_state", "peer_oper_state", "self_mux_direction",
         "peer_mux_direction"), "unknown")
    assert info["server_version"] == "N/A"
    assert info["grpc_connection_status"] in ("TRANSIENT_FAILURE", "CONNECTING", "IDLE")


def test_active_standby_driver_exceptions(run):
    info = run["info"]["Ethernet28"]
    assert {k: info[k] for k in ("tor_active", "mux_direction", "link_status_self", "link_status_peer",
                                 "link_status_nic")} == {
        "tor_active": "unknown", "mux_direction": "unknown", "link_status_self": "down",
        "link_status_peer": "down", "link_status_nic": "down"}
    for k in ("manual_switch_count", "auto_switch_count", "internal_temperature", "internal_voltage",
              "nic_temperature", "nic_voltage", "self_eye_height_lane1", "nic_eye_height_lane2"):
        assert info[k] == "N/A", k


def test_static_info_driver_failures(run):
    static = dict(run["static"]["Ethernet28"])
    assert static.pop("read_side") == "tor1"
    assert set(static.values()) == {"N/A"}


def test_cable_without_driver_is_skipped(run):
    """No driver: the pass logs an error and leaves the startup placeholders."""
    assert run["info"]["Ethernet24"] == run["init"]["Ethernet24"]
    assert not [a for a in run["writes"] if a.key == "Ethernet24"]


def test_static_info_only_at_startup(run):
    assert not [a for a in run["writes"] if a.table == "MUX_CABLE_STATIC_INFO"]


def _short_reply(request, pb2):
    return pb2.OperationReply(portid=[0], state=[True])


def test_disable_telemetry(dut):
    """Also: telemetry of the lower ToR's active-active port, whose SoC sends
    a malformed operation state reply."""
    topo = Topology(loopback3=LOOPBACK3_LOWER).active_standby("Ethernet0")
    topo.active_active("Ethernet4", soc_ipv4=SOC_UP + "/32")
    topo.xcvrd_log = {"disable_telemetry": "True"}
    dut.configure(topo)
    soc = dut.soc(SOC_UP)
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


@pytest.mark.known_issue("3.8-3", "telemetry of an active-active port without HW_MUX_CABLE_TABLE (no SoC "
                                   "address) raises NameError and kills the telemetry thread")
def test_active_active_port_without_soc_address(dut):
    dut.configure(Topology().active_active("Ethernet0").active_standby("Ethernet4"))
    dut.start()
    dut.tolerate_python_failure(crash=True, exit=True)
    mark = dut.monitor.mark()
    dut.monitor.wait_access(mark, PASS_TIMEOUT, kind="write", table="MUX_CABLE_INFO", key="Ethernet4")
    time.sleep(1)
    assert not dut.crash_evidence()
