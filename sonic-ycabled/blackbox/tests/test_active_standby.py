"""Active-standby cables: toggles, probes and driver selection (HLD 3.3.1,
3.3.2, 3.4.2; F2)."""

import re

import pytest

from harness import expect
from harness.fake_hw import MODEL, TARGET_TOR_A, TARGET_TOR_B
from harness.mux_simulator import LOWER_TOR, UPPER_TOR
from harness.topology import VS_PLATFORM, Topology, soc_ip
from harness.util import TIMESTAMP_RE, eventually, stays_true

# STATE_DB HW_MUX_CABLE_TABLE of a port whose state is unknown: what a toggle
# answers, and what startup publishes for a cable it cannot read.
UNKNOWN_TOGGLE_RESULT = {"state": "unknown", "read_side": "-1", "active_side": "-1"}


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
    """orchagent's toggle request moves the mux to the requested side with a
    single driver call, and ycabled answers in STATE_DB HW_MUX_CABLE_TABLE with
    the new state, the cable's read side and the side the mux now points to. It
    records when the switch started and ended in MUX_METRICS_TABLE.

    Cases: the cable's read side (ToR A or B) and the requested state (active
    or standby), which together select toggle_mux_to_tor_a() or
    toggle_mux_to_tor_b() and the active side.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable. Ethernet0's cable has the given read side, and its
       mux points at the side opposite the one the toggle selects.
    2. Start ycabled.
    3. Toggle Ethernet0 to the requested state, as orchagent does through
       APPL_DB HW_MUX_CABLE_TABLE, and wait for ycabled's answer.
    4. Expect the STATE_DB row {state: the requested state, read_side: the
       cable's read side, active_side: the side toggled to}.
    5. Expect the toggle method as the only driver call since the request
       (ycabled does not read the direction back), and the cable's mux on the
       new side.
    6. Expect MUX_METRICS_TABLE xcvrd_switch_<state>_start and
       xcvrd_switch_<state>_end in ycabled's timestamp format, the start no
       later than the end.
    """
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
    """A toggle that the cable rejects or fails is answered unknown, with the
    read side kept and active side -1; its metrics are recorded under unknown,
    and the failure is logged.

    Cases: a toggle to active (toggle_mux_to_tor_a(), logged as torA) or to
    standby (toggle_mux_to_tor_b(), torB); the driver returns False or raises
    OSError.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable, with Ethernet0's mux on the side opposite the
       requested one.
    2. Script the toggle method of Ethernet0's cable to return False or raise
       OSError.
    3. Start ycabled.
    4. Toggle Ethernet0 to the requested state.
    5. Expect the STATE_DB row {state: unknown, read_side: 1, active_side: -1}.
    6. Expect only xcvrd_switch_unknown_start and xcvrd_switch_unknown_end in
       MUX_METRICS_TABLE.
    7. Expect 'Could not toggle the mux for port 1 to torA' (or torB) in syslog
       within 5 s.
    """
    cable = two_cables(dut, mux_direction=2 if request_state == "active" else 1)
    cable.script(method, **failure)
    dut.start()
    mark = dut.syslog.mark()
    assert dut.toggle("Ethernet0", request_state) == {"state": "unknown", "read_side": "1", "active_side": "-1"}
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    assert set(metrics) == {"xcvrd_switch_unknown_start", "xcvrd_switch_unknown_end"}
    dut.syslog.wait_for(mark, 5, contains="Could not toggle the mux for port 1 to {}".format(side))


def test_toggle_reads_side_again_when_unknown(dut):
    """If the read side was unknown at startup, a toggle asks the cable for it
    again before it toggles, and the answer carries the read side it got.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Script get_read_side() of Ethernet0's cable to raise OSError once, so
       that startup cannot read it.
    3. Start ycabled, and expect Ethernet0's STATE_DB row {state: unknown,
       read_side: -1, active_side: -1}.
    4. Toggle Ethernet0 to standby.
    5. Expect the row {state: standby, read_side: 1, active_side: 2}, and the
       driver calls get_read_side() and toggle_mux_to_tor_b(), in that order,
       and no other.
    """
    cable = two_cables(dut)
    cable.script("get_read_side", raises="OSError: EEPROM busy", times=1)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT
    mark = dut.hw.mark()
    assert dut.toggle("Ethernet0", "standby") == {"state": "standby", "read_side": "1", "active_side": "2"}
    assert [c.method for c in dut.hw.calls(mark, port=1)] == ["get_read_side", "toggle_mux_to_tor_b"]


@pytest.mark.parametrize("script", [{"raises": "OSError: EEPROM busy"}, {"value": 0}, {"value": None}],
                         ids=["raises", "nic-side", "none"])
def test_toggle_read_side_still_unreadable(dut, script):
    """A toggle that cannot get a valid read side from the cable does not
    toggle: it answers unknown.

    Cases: get_read_side() raises OSError (raises), returns 0, the NIC side
    (nic-side), or returns None (none), on every call.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Script get_read_side() of Ethernet0's cable to fail that way, so that
       the read side is unknown from startup on.
    3. Start ycabled.
    4. Toggle Ethernet0 to standby.
    5. Expect the row {state: unknown, read_side: -1, active_side: -1}, and
       get_read_side() as the only driver call since the request.
    """
    cable = two_cables(dut)
    cable.script("get_read_side", **script)
    dut.start()
    mark = dut.hw.mark()
    assert dut.toggle("Ethernet0", "standby") == UNKNOWN_TOGGLE_RESULT
    assert [c.method for c in dut.hw.calls(mark, port=1)] == ["get_read_side"]


@pytest.mark.known_issue("a cable read from the NIC side (read side 0) is published and probed as "
                         "standby, while a toggle treats that read side as invalid and writes unknown")
def test_read_side_nic(dut):
    """A ToR never reads a Y-cable from the NIC side, so read side 0 means the
    read side is not known: the port is unknown, as Python's toggle already
    reports it. Its state, its probes and its toggles are all unknown.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable, with Ethernet0's cable reporting read side 0.
    2. Start ycabled.
    3. Expect Ethernet0's STATE_DB HW_MUX_CABLE_TABLE state unknown.
    4. Probe Ethernet0 and expect unknown.
    5. Toggle Ethernet0 to standby and expect state unknown.
    """
    two_cables(dut, read_side=0)
    dut.start()
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "unknown"
    assert dut.probe("Ethernet0") == "unknown"
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"


@pytest.mark.parametrize("script,row", [
    ({"raises": "OSError: I2C failure", "times": 1}, {"state": "unknown", "read_side": "1", "active_side": "-1"}),
    ({"value": 0, "times": 1}, {"state": "unknown", "read_side": "1", "active_side": "0"}),
    ({"value": 7, "times": 1}, {"state": "unknown", "read_side": "1", "active_side": "-1"}),
], ids=["raises", "no-tor", "out-of-range"])
def test_startup_mux_direction_unreadable(dut, script, row):
    """When the mux direction cannot be read at startup, the port is published
    unknown, with the read side it has and with active_side -1, or 0 when the
    cable points at neither ToR.

    Cases: get_mux_direction() raises OSError (raises), returns 0 (no-tor) or
    returns 7 (out-of-range), once, at startup.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Script get_mux_direction() of Ethernet0's cable to fail that way once.
    3. Start ycabled.
    4. Expect Ethernet0's STATE_DB row {state: unknown, read_side: 1,
       active_side: -1}, or active_side 0 when the cable returned 0.
    """
    cable = two_cables(dut)
    cable.script("get_mux_direction", **script)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == row


def test_toggle_request_without_state_is_ignored(dut):
    """A HW_MUX_CABLE_TABLE change without a state field is not a toggle:
    ycabled writes nothing for the port, does not call its cable, and keeps
    serving toggles.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Start ycabled.
    3. Write HW_MUX_CABLE_TABLE {mode: auto} for Ethernet0 to APPL_DB.
    4. Expect, for 1.5 s, no write by ycabled for Ethernet0 and no driver call
       to its cable.
    5. Toggle Ethernet0 to standby and expect state standby: the toggles are
       still served.
    """
    two_cables(dut)
    dut.start()
    mark = dut.monitor.mark()
    hw_mark = dut.hw.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"mode": "auto"})
    assert stays_true(lambda: not dut.writes(since=mark, key="Ethernet0"), 1.5)
    assert not dut.hw.calls(hw_mark, port=1)
    # The toggle thread is still serving.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"


def test_toggle_cable_gone(dut):
    """The cable is gone but xcvrd has not removed it yet: a toggle finds no
    cable in the platform API and answers unknown.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Start ycabled.
    3. Make Ethernet0's cable absent in the platform API, leaving its
       TRANSCEIVER_INFO in place.
    4. Toggle Ethernet0 to standby and expect the row {state: unknown,
       read_side: -1, active_side: -1}.
    """
    cable = two_cables(dut)
    dut.start()
    cable.present = False
    assert dut.toggle("Ethernet0", "standby") == UNKNOWN_TOGGLE_RESULT


def test_toggle_without_driver(dut):
    """A port whose cable has no driver is published unknown at startup, and
    its toggles are answered unknown.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable. Ethernet0's transceiver info names a vendor and
       model that sonic_y_cable does not map to a driver.
    2. Start ycabled.
    3. Expect Ethernet0's STATE_DB row {state: unknown, read_side: -1,
       active_side: -1}.
    4. Toggle Ethernet0 to active and expect the same row.
    """
    two_cables(dut, transceiver_info={"manufacturer": "Nobody", "model": "X1"})
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT
    assert dut.toggle("Ethernet0", "active") == UNKNOWN_TOGGLE_RESULT


@pytest.mark.known_issue("a HW_MUX_CABLE_TABLE state other than active or standby (orchagent "
                         "writes 'init' after a failed first switch) kills the toggle thread")
def test_toggle_to_other_state_is_ignored(dut):
    """orchagent writes state init after a failed first switch. A state other
    than active or standby is not a toggle: ycabled ignores it, writes nothing
    for the port, and keeps serving the other ports.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Start ycabled. On Python ycabled, let the teardown accept the crash of
       the known issue.
    3. Write HW_MUX_CABLE_TABLE state=init for Ethernet0.
    4. Toggle Ethernet4 to standby without waiting, and expect ycabled to
       answer it, with state standby.
    5. Expect no write for Ethernet0 since the init request, and no crash in
       syslog or in ycabled's output.
    """
    two_cables(dut)
    dut.start()
    dut.tolerate_python_failure(crash=True)
    mark = dut.monitor.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"state": "init"})
    # A toggle sent after it shows whether the toggle thread is still serving.
    dut.toggle("Ethernet4", "standby", wait=False)
    assert dut.answered("toggle", "Ethernet4", mark), "the toggle of Ethernet4 was not answered"
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet4", "state") == "standby"
    assert not dut.monitor.accesses(mark, kind="write", key="Ethernet0")
    assert not dut.crash_evidence()


@pytest.mark.parametrize("read_side,mux_direction,response", [
    (TARGET_TOR_A, TARGET_TOR_A, "active"),
    (TARGET_TOR_A, TARGET_TOR_B, "standby"),
    (TARGET_TOR_B, TARGET_TOR_B, "active"),
    (TARGET_TOR_B, TARGET_TOR_A, "standby"),
])
def test_probe(dut, read_side, mux_direction, response):
    """linkmgrd's probe reads the mux direction from the cable and is answered
    in APPL_DB MUX_CABLE_RESPONSE_TABLE: active when the mux points at this
    ToR's read side, standby otherwise. A probe only answers:
    HW_MUX_CABLE_TABLE is not written.

    Cases: read side A or B, with the mux on the same side or the other one.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable, with Ethernet0's cable on the given read side.
    2. Start ycabled, then point the mux of Ethernet0's cable at the given
       side.
    3. Probe Ethernet0, as linkmgrd does through MUX_CABLE_COMMAND_TABLE, and
       expect the response.
    4. Expect get_mux_direction() as the only driver call since the probe, and
       no write to HW_MUX_CABLE_TABLE.
    """
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
    """A probe whose mux direction cannot be read, or points at neither ToR, is
    answered unknown.

    Cases: get_mux_direction() raises OSError, or returns -1 (the driver's
    EEPROM_ERROR), 0 or None.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Start ycabled.
    3. Script get_mux_direction() of Ethernet0's cable to fail that way.
    4. Probe Ethernet0 and expect unknown.
    """
    cable = two_cables(dut)
    dut.start()
    cable.script("get_mux_direction", **script)
    assert dut.probe("Ethernet0") == "unknown"


def test_probe_cable_gone(dut):
    """A probe for a cable that is gone, before xcvrd has removed it, is
    answered unknown.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Start ycabled.
    3. Make Ethernet0's cable absent in the platform API, leaving its
       TRANSCEIVER_INFO in place.
    4. Probe Ethernet0 and expect unknown.
    """
    cable = two_cables(dut)
    dut.start()
    cable.present = False
    assert dut.probe("Ethernet0") == "unknown"


def test_probe_without_driver(dut):
    """A probe for a port whose cable has no driver is answered unknown.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable. Ethernet0's transceiver info names a vendor and
       model that sonic_y_cable does not map to a driver.
    2. Start ycabled.
    3. Probe Ethernet0 and expect unknown.
    """
    two_cables(dut, transceiver_info={"manufacturer": "Nobody", "model": "X1"})
    dut.start()
    assert dut.probe("Ethernet0") == "unknown"


def test_probe_ignores_other_commands_and_ports(dut):
    """Only the probe command for an active-standby port is answered in
    MUX_CABLE_RESPONSE_TABLE: another command, a probe for an active-active
    port (linkmgrd probes those through FORWARDING_STATE_COMMAND) and a probe
    for a port without a mux cable get no answer.

    Steps:
    1. Configure Ethernet0 as active-standby and Ethernet4 as active-active,
       with a SoC double at 192.168.0.5. Ethernet8 has no MUX_CABLE entry.
    2. Start ycabled.
    3. Write MUX_CABLE_COMMAND_TABLE command=reset for Ethernet0, and
       command=probe for Ethernet4 and Ethernet8.
    4. Expect no MUX_CABLE_RESPONSE_TABLE write for 1.5 s.
    5. Probe Ethernet0 and expect active: probes are still served.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4=soc_ip("Ethernet4") + "/32")
    dut.configure(topo)
    dut.soc(soc_ip("Ethernet4"))
    dut.start()
    mark = dut.monitor.mark()
    dut.appl.set("MUX_CABLE_COMMAND_TABLE", "Ethernet0", {"command": "reset"})
    dut.appl.set("MUX_CABLE_COMMAND_TABLE", "Ethernet4", {"command": "probe"})
    dut.appl.set("MUX_CABLE_COMMAND_TABLE", "Ethernet8", {"command": "probe"})
    assert stays_true(lambda: not dut.monitor.accesses(mark, kind="write", table="MUX_CABLE_RESPONSE_TABLE"), 1.5)
    assert dut.probe("Ethernet0") == "active"


def test_vendor_and_model_are_normalized(dut):
    """The vendor and model from the transceiver info are normalized before the
    driver lookup, as Python ycabled does: lowercased, trimmed, and each run of
    spaces replaced with an underscore. A cable whose info differs from the
    mapping only in case and spaces gets its driver.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable. Ethernet0's transceiver info gives the vendor as '
       bb VENDOR ' and the model in upper case after a space.
    2. Start ycabled.
    3. Expect Ethernet0's STATE_DB state active, read through the driver, and a
       get_vendor() call to the cable: the driver was loaded and checked.
    """
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
    toggled: its HW_MUX_CABLE_TABLE row is unknown, MUX_CABLE_INFO holds the
    placeholder values, MUX_CABLE_STATIC_INFO is not written, and its toggles
    and probes are answered unknown.

    Cases: the vendor or the model is not in sonic_y_cable's mapping
    (unknown-vendor, unknown-model) or missing from the transceiver info
    (no-vendor, no-model); the mapping names a module that does not exist or
    has no YCable class (no-module, no-class); the vendor is not a string
    (vendor-not-a-string); there is no transceiver info (no-transceiver-info);
    the driver's get_vendor() names another vendor (vendor-mismatch).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable. Ethernet0's cable gets the case's transceiver info
       or vendor.
    2. Start ycabled.
    3. Expect Ethernet0's STATE_DB HW_MUX_CABLE_TABLE row {state: unknown,
       read_side: -1, active_side: -1}, MUX_CABLE_INFO with the placeholder
       values besides time_post, and no MUX_CABLE_STATIC_INFO.
    4. Expect no driver call to the cable other than get_vendor().
    5. Toggle Ethernet0 to standby and expect the same unknown row; probe it
       and expect unknown.
    """
    cable = two_cables(dut)
    if info is not None:
        cable.transceiver_info = None if info == "none" else info
    if vendor is not None:
        cable.vendor = vendor
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT
    info_row = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    info_row.pop("time_post")
    assert info_row == expect.PLACEHOLDER_INFO
    assert not dut.state.exists("MUX_CABLE_STATIC_INFO", "Ethernet0")
    calls = {c.method for c in dut.hw.calls(port=1)}
    assert calls <= {"get_vendor"}, calls
    assert dut.toggle("Ethernet0", "standby") == UNKNOWN_TOGGLE_RESULT
    assert dut.probe("Ethernet0") == "unknown"


@pytest.mark.known_issue("a get_vendor() exception deadlocks startup: the port lock is taken again "
                         "while held, so no port is ever published or served. The unknown row "
                         "expected is a choice: with a reentrant lock, Python's own fallback would "
                         "read the cable through the unverified driver and publish active")
def test_get_vendor_failure(dut):
    """A get_vendor() exception leaves the cable without a driver: the cable is
    reported unknown, like a cable whose get_vendor() names another vendor, and
    the other cables are served.

    Reporting it unknown is a choice: Python falls back to
    create_tables_and_insert_mux_unknown_entries, which reads the cable through
    the driver that just failed the check, so with a reentrant lock it would
    publish the direction that driver reads (active here).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Script get_vendor() of Ethernet0's cable to raise OSError.
    3. Start ycabled without waiting until it is ready.
    4. Expect both ports in STATE_DB HW_MUX_CABLE_TABLE within 5 s, Ethernet0's
       row {state: unknown, read_side: -1, active_side: -1}.
    5. Wait until ycabled is ready, toggle Ethernet4 to standby and expect
       state standby.
    """
    cable = two_cables(dut)
    cable.script("get_vendor", raises="OSError: EEPROM busy")
    dut.start(ready=False)
    assert eventually(lambda: len(dut.state.keys("HW_MUX_CABLE_TABLE")) == 2, 5), "startup did not publish both ports"
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT
    dut.wait_ready()
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


@pytest.mark.known_issue("an exception from the vendor driver's constructor is not caught; "
                         "ycabled exits during startup")
def test_driver_constructor_failure(dut):
    """An exception from the vendor driver's constructor leaves that cable
    without a driver, and nothing else: ycabled keeps running, publishes the
    cable unknown, and initializes the other cables.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4, a
       healthy second cable.
    2. Script the constructor of Ethernet0's driver to raise OSError. On Python
       ycabled, let the teardown accept the crash and the exit of the known
       issue.
    3. Start ycabled without waiting until it is ready, and wait up to 10 s for
       it to exit or to publish both ports.
    4. Expect ycabled still running, Ethernet0's STATE_DB row {state: unknown,
       read_side: -1, active_side: -1}, and Ethernet4's state active.
    """
    cable = two_cables(dut)
    cable.script("__init__", raises="OSError: EEPROM unreadable")
    dut.tolerate_python_failure(crash=True, exit=True)
    dut.start(ready=False)
    eventually(lambda: not dut.proc.running() or len(dut.state.keys("HW_MUX_CABLE_TABLE")) == 2, 10)
    assert dut.proc.running(), dut.proc.output()[-2000:]
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet4")["state"] == "active"


def test_only_unusable_cables(dut):
    """A device whose only active-standby cable has no driver starts, and
    publishes the port unknown.

    With no cable initialized at startup, Python serves no toggles, probes or
    CLI commands, so the test does not wait for the request tables to be
    subscribed (see also test_insert_remove.py).

    Steps:
    1. Configure Ethernet0 as the only active-standby port. Its transceiver
       info names a vendor and model that sonic_y_cable does not map to a
       driver.
    2. Start ycabled. It counts as ready once it has subscribed to
       TRANSCEIVER_INFO.
    3. Expect Ethernet0's STATE_DB row {state: unknown, read_side: -1,
       active_side: -1}.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.port(1).transceiver_info = {"manufacturer": "Nobody", "model": "X1"}
    dut.start(expect_mux=False)
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT


@pytest.mark.parametrize("side,read_side", [(UPPER_TOR, "1"), (LOWER_TOR, "2")])
def test_mux_simulator_driver_on_vs(dut, side, read_side):
    """KVM testbeds: /etc/sonic/mux_simulator.json selects the real simulated
    driver, which talks to the mux simulator over HTTP. ycabled publishes,
    toggles and probes the mux through it, and calls no platform API: VS has
    none.

    Cases: this device is the upper ToR (read side 1) or the lower ToR (read
    side 2).

    Steps:
    1. Configure the KVM platform with active-standby ports Ethernet0 and
       Ethernet4.
    2. Start a mux simulator, write mux_simulator.json for it with this ToR's
       side, and point the simulator's port 0, Ethernet0's mux, at the upper
       ToR.
    3. Start ycabled.
    4. Expect Ethernet0's STATE_DB row {state: active on the upper ToR or
       standby on the lower one, read_side: 1 or 2, active_side: 1}.
    5. Expect no call to the fake cables or the fake platform API.
    6. Toggle Ethernet0 to the other state, and expect that state and the
       simulator's port 0 on the lower ToR.
    7. Probe Ethernet0 and expect the new state. Expect a POST for port 0 among
       the simulator's requests.
    """
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
    """On VS without /etc/sonic/mux_simulator.json no driver is selected: the
    port is published unknown, and no platform API or driver is called.

    Steps:
    1. Configure the KVM platform with Ethernet0 as the only active-standby
       port, and no mux simulator.
    2. Start ycabled. It counts as ready once it has subscribed to
       TRANSCEIVER_INFO.
    3. Expect Ethernet0's STATE_DB row {state: unknown, read_side: -1,
       active_side: -1}, and no call to the fake cables or the fake platform
       API.
    """
    dut.configure(Topology(platform=VS_PLATFORM).active_standby("Ethernet0"))
    dut.start(expect_mux=False)
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == UNKNOWN_TOGGLE_RESULT
    assert not dut.hw.calls(op=None)
