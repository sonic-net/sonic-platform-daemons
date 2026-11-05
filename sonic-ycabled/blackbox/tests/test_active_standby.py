"""Active-standby cables: toggles, probes and driver selection (HLD 3.4.1,
3.4.2, 3.5.2; F2)."""

import re
import time

import pytest

from harness import expect
from harness.fake_hw import MODEL, TARGET_TOR_A, TARGET_TOR_B
from harness.mux_simulator import LOWER_TOR, UPPER_TOR
from harness.topology import VS_PLATFORM, Topology
from harness.util import TIMESTAMP_RE, stays_true, wait_until

UNKNOWN_ROW = {"state": "unknown", "read_side": "-1", "active_side": "-1"}


def two_cables(dut, **kw):
    """Ethernet0 under test, Ethernet4 healthy: Python serves toggles and
    probes only if at least one cable initialized."""
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    for k, v in kw.items():
        setattr(dut.hw.port(1), k, v)
    return dut.hw.port(1)


@pytest.mark.parametrize("read_side,request_state,method,active_side", [
    (TARGET_TOR_A, "active", "toggle_mux_to_tor_a", "1"),
    (TARGET_TOR_A, "standby", "toggle_mux_to_tor_b", "2"),
    (TARGET_TOR_B, "active", "toggle_mux_to_tor_b", "2"),
    (TARGET_TOR_B, "standby", "toggle_mux_to_tor_a", "1"),
])
def test_toggle(dut, read_side, request_state, method, active_side):
    # The mux starts on the other side.
    cable = two_cables(dut, read_side=read_side, mux_direction=3 - int(active_side))
    dut.start()
    mark = dut.hw.mark()
    row = dut.toggle("Ethernet0", request_state)
    assert row == {"state": request_state, "read_side": str(read_side), "active_side": active_side}
    calls = [c.method for c in dut.hw.calls(mark, port=1)]
    # The mux direction is not read back after the toggle.
    assert calls == [method], calls
    assert cable.mux_direction == int(active_side)
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    start = metrics["xcvrd_switch_{}_start".format(request_state)]
    end = metrics["xcvrd_switch_{}_end".format(request_state)]
    assert re.match(TIMESTAMP_RE, start) and re.match(TIMESTAMP_RE, end)
    assert start <= end


@pytest.mark.parametrize("request_state,method,side", [("active", "toggle_mux_to_tor_a", "torA"),
                                                       ("standby", "toggle_mux_to_tor_b", "torB")])
@pytest.mark.parametrize("failure", [{"value": False}, {"raises": "OSError: I2C failure"}])
def test_toggle_failure(dut, request_state, method, side, failure):
    cable = two_cables(dut, mux_direction=2 if request_state == "active" else 1)
    cable.script(method, **failure)
    dut.start()
    mark = dut.syslog.mark()
    assert dut.toggle("Ethernet0", request_state) == {"state": "unknown", "read_side": "1", "active_side": "-1"}
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    assert set(metrics) == {"xcvrd_switch_unknown_start", "xcvrd_switch_unknown_end"}
    dut.syslog.wait_for(mark, 5, contains="Could not toggle the mux for port 1 to {}".format(side))


def test_toggle_reads_side_again_when_unknown(dut):
    """If the read side was unknown at startup, a toggle asks the cable again."""
    cable = two_cables(dut)
    cable.script("get_read_side", raises="OSError: EEPROM busy", times=1)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW
    mark = dut.hw.mark()
    assert dut.toggle("Ethernet0", "standby") == {"state": "standby", "read_side": "1", "active_side": "2"}
    assert [c.method for c in dut.hw.calls(mark, port=1)] == ["get_read_side", "toggle_mux_to_tor_b"]


@pytest.mark.parametrize("script", [{"raises": "OSError: EEPROM busy"}, {"value": 0}, {"value": None}],
                         ids=["raises", "nic-side", "none"])
def test_toggle_read_side_still_unreadable(dut, script):
    cable = two_cables(dut)
    cable.script("get_read_side", **script)
    dut.start()
    mark = dut.hw.mark()
    assert dut.toggle("Ethernet0", "standby") == UNKNOWN_ROW
    assert [c.method for c in dut.hw.calls(mark, port=1)] == ["get_read_side"]


def test_startup_read_side_nic(dut):
    """A cable read from the NIC side (0): Python publishes it as standby."""
    two_cables(dut, read_side=0)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {"state": "standby", "read_side": "0", "active_side": "1"}


@pytest.mark.parametrize("script,row", [
    ({"raises": "OSError: I2C failure", "times": 1}, {"state": "unknown", "read_side": "1", "active_side": "-1"}),
    ({"value": 0, "times": 1}, {"state": "unknown", "read_side": "1", "active_side": "0"}),
    ({"value": 7, "times": 1}, {"state": "unknown", "read_side": "1", "active_side": "-1"}),
], ids=["raises", "no-tor", "out-of-range"])
def test_startup_mux_direction_unreadable(dut, script, row):
    cable = two_cables(dut)
    cable.script("get_mux_direction", **script)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == row


def test_toggle_request_without_state_is_ignored(dut):
    two_cables(dut)
    dut.start()
    mark = dut.monitor.mark()
    hw_mark = dut.hw.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"mode": "auto"})
    assert stays_true(lambda: not dut.writes(since=mark, key="Ethernet0"), 1.5)
    assert not dut.hw.calls(hw_mark, port=1)


def test_toggle_cable_gone(dut):
    """The cable is gone but xcvrd has not removed it yet."""
    cable = two_cables(dut)
    dut.start()
    cable.present = False
    assert dut.toggle("Ethernet0", "standby") == UNKNOWN_ROW


def test_toggle_without_driver(dut):
    two_cables(dut, transceiver_info={"manufacturer": "Nobody", "model": "X1"})
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW
    assert dut.toggle("Ethernet0", "active") == UNKNOWN_ROW


@pytest.mark.known_issue("3.8-17", "a HW_MUX_CABLE_TABLE state other than active or standby (orchagent "
                                    "writes 'init' after a failed first switch) kills the toggle thread")
def test_toggle_to_other_state_is_ignored(dut):
    two_cables(dut)
    dut.start()
    dut.tolerate_python_failure(crash=True)
    mark = dut.monitor.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"state": "init"})
    time.sleep(1)
    assert dut.toggle("Ethernet4", "standby", timeout=3)["state"] == "standby"
    assert not dut.monitor.accesses(mark, kind="write", key="Ethernet0")
    assert not dut.crash_evidence()


@pytest.mark.parametrize("read_side,mux_direction,response", [
    (TARGET_TOR_A, TARGET_TOR_A, "active"),
    (TARGET_TOR_A, TARGET_TOR_B, "standby"),
    (TARGET_TOR_B, TARGET_TOR_B, "active"),
    (TARGET_TOR_B, TARGET_TOR_A, "standby"),
])
def test_probe(dut, read_side, mux_direction, response):
    cable = two_cables(dut, read_side=read_side)
    dut.start()
    cable.mux_direction = mux_direction
    mark = dut.hw.mark()
    db_mark = dut.monitor.mark()
    assert dut.probe("Ethernet0") == response
    assert [c.method for c in dut.hw.calls(mark, port=1)] == ["get_mux_direction"]
    # A probe only answers; HW_MUX_CABLE_TABLE is not touched.
    assert not dut.writes(since=db_mark, table="HW_MUX_CABLE_TABLE")


@pytest.mark.parametrize("script", [{"raises": "OSError: I2C failure"}, {"value": -1}, {"value": 0}, {"value": None}])
def test_probe_unreadable_mux(dut, script):
    cable = two_cables(dut)
    dut.start()
    cable.script("get_mux_direction", **script)
    assert dut.probe("Ethernet0") == "unknown"


def test_probe_cable_gone(dut):
    cable = two_cables(dut)
    dut.start()
    cable.present = False
    assert dut.probe("Ethernet0") == "unknown"


def test_probe_without_driver(dut):
    two_cables(dut, transceiver_info={"manufacturer": "Nobody", "model": "X1"})
    dut.start()
    assert dut.probe("Ethernet0") == "unknown"


def test_probe_ignores_other_commands_and_ports(dut):
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4="127.0.0.2/32")
    dut.configure(topo)
    dut.soc("127.0.0.2")
    dut.start()
    mark = dut.monitor.mark()
    dut.appl.set("MUX_CABLE_COMMAND_TABLE", "Ethernet0", {"command": "reset"})
    dut.appl.set("MUX_CABLE_COMMAND_TABLE", "Ethernet4", {"command": "probe"})
    dut.appl.set("MUX_CABLE_COMMAND_TABLE", "Ethernet8", {"command": "probe"})
    assert stays_true(lambda: not dut.monitor.accesses(mark, kind="write", table="MUX_CABLE_RESPONSE_TABLE"), 1.5)
    assert dut.probe("Ethernet0") == "active"


def test_vendor_and_model_are_normalized(dut):
    cable = two_cables(dut)
    cable.transceiver_info = dict(cable.transceiver_info, manufacturer="  bb   VENDOR ", model=" " + MODEL.upper())
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.hw.calls(port=1, method="get_vendor")


@pytest.mark.parametrize("info,vendor", [
    ({"manufacturer": "Nobody", "model": MODEL}, None),
    ({"manufacturer": "BB Vendor", "model": "Unknown Model"}, None),
    ({"model": MODEL}, None),
    ({"manufacturer": "BB Vendor"}, None),
    ({"manufacturer": "BB Vendor", "model": "BB Model NoModule"}, None),
    ({"manufacturer": "BB Vendor", "model": "BB Model NoClass"}, None),
    ({"manufacturer": 123, "model": MODEL}, None),
    ("none", None),
    (None, "Other Vendor"),
], ids=["unknown-vendor", "unknown-model", "no-vendor", "no-model", "no-module", "no-class", "vendor-not-a-string",
        "no-transceiver-info", "vendor-mismatch"])
def test_no_usable_driver(dut, info, vendor):
    """Without a matching driver, the port is reported unknown and never
    toggled."""
    cable = two_cables(dut)
    if info is not None:
        cable.transceiver_info = None if info == "none" else info
    if vendor is not None:
        cable.vendor = vendor
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW
    info_row = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    info_row.pop("time_post")
    assert info_row == expect.PLACEHOLDER_INFO
    assert not dut.state.exists("MUX_CABLE_STATIC_INFO", "Ethernet0")
    calls = {c.method for c in dut.hw.calls(port=1)}
    assert calls <= {"get_vendor"}, calls
    assert dut.toggle("Ethernet0", "standby") == UNKNOWN_ROW
    assert dut.probe("Ethernet0") == "unknown"


@pytest.mark.known_issue("3.8-24", "a get_vendor() exception deadlocks startup: the port lock is taken again "
                                    "while held, so no port is ever published or served")
def test_get_vendor_failure(dut):
    """The cable is reported unknown, like the other driver-load failures, and
    the other cables are served."""
    cable = two_cables(dut)
    cable.script("get_vendor", raises="OSError: EEPROM busy")
    dut.start(ready=False)
    wait_until(lambda: len(dut.state.keys("HW_MUX_CABLE_TABLE")) == 2, 5)
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW
    dut.wait_ready()
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


@pytest.mark.known_issue("3.8-18", "an exception from the vendor driver's constructor is not caught; "
                                    "ycabled exits during startup")
def test_driver_constructor_failure(dut):
    cable = two_cables(dut)
    cable.script("__init__", raises="OSError: EEPROM unreadable")
    dut.tolerate_python_failure(crash=True, exit=True)
    dut.start(ready=False)
    time.sleep(3)
    assert dut.proc.running()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet4")["state"] == "active"


def test_only_unusable_cables(dut):
    """With no cable initialized at startup, Python serves no toggles, probes
    or CLI commands (see also test_insert_remove.py)."""
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.port(1).transceiver_info = {"manufacturer": "Nobody", "model": "X1"}
    dut.start(expect_mux=False)
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW


@pytest.mark.parametrize("side,read_side", [(UPPER_TOR, "1"), (LOWER_TOR, "2")])
def test_mux_simulator_driver_on_vs(dut, side, read_side):
    """KVM testbeds: /etc/sonic/mux_simulator.json selects the real simulated
    driver, which talks to the mux simulator over HTTP."""
    dut.configure(Topology(platform=VS_PLATFORM).active_standby("Ethernet0", "Ethernet4"))
    sim = dut.mux_simulator(side)
    sim.set_active_side(0, UPPER_TOR)
    dut.start()
    expected = "active" if side == UPPER_TOR else "standby"
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": expected, "read_side": read_side, "active_side": "1"}
    # VS: no platform API at all.
    assert not dut.hw.calls(op=None)
    other = "standby" if expected == "active" else "active"
    row = dut.toggle("Ethernet0", other)
    assert row["state"] == other
    assert sim.status(0)["active_side"] == LOWER_TOR
    assert dut.probe("Ethernet0") == other
    assert any(r[0] == "POST" and r[1].endswith("/0") for r in sim.requests)


def test_vs_without_mux_simulator(dut):
    dut.configure(Topology(platform=VS_PLATFORM).active_standby("Ethernet0"))
    dut.start(expect_mux=False)
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_ROW
    assert not dut.hw.calls(op=None)
