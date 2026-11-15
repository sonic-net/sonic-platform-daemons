"""Cable insertion and removal, from STATE_DB TRANSCEIVER_INFO (HLD 3.3.7)."""

import pytest

from harness import expect
from harness.topology import Topology, soc_ip
from harness.util import eventually, wait_until

SOC = soc_ip("Ethernet0")


def two_cables(dut):
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    return dut.hw.port(1)


def remove_ethernet4(dut):
    """Remove Ethernet4 and wait until ycabled has handled it. Python handles
    TRANSCEIVER_INFO events one at a time, in order, so the events written
    before it have been handled too: a barrier for checks that something did
    not happen."""
    mark = dut.monitor.mark()
    dut.remove("Ethernet4")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet4")


def test_remove_active_standby(dut):
    """When xcvrd removes an active-standby cable, ycabled writes
    HW_MUX_CABLE_TABLE unknown, keeping the read side, and keeps MUX_CABLE_INFO
    and MUX_CABLE_STATIC_INFO. The port's probes and toggles are answered
    unknown, and the other cable is not affected.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4.
    2. Start ycabled, and note Ethernet0's MUX_CABLE_INFO and
       MUX_CABLE_STATIC_INFO.
    3. Remove Ethernet0's cable, as xcvrd reports it (TRANSCEIVER_INFO deleted,
       the cable absent), and wait for ycabled's HW_MUX_CABLE_TABLE write for
       it.
    4. Expect Ethernet0's row {state: unknown, read_side: 1, active_side: -1},
       and MUX_CABLE_INFO and MUX_CABLE_STATIC_INFO unchanged.
    5. Probe Ethernet0 and expect unknown; toggle it to standby and expect
       {state: unknown, read_side: -1, active_side: -1}.
    6. Toggle Ethernet4 to standby and expect standby.
    """
    two_cables(dut)
    dut.start()
    info = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    static = dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0")
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    # The read side is kept; MUX_CABLE_INFO and MUX_CABLE_STATIC_INFO too.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "1", "active_side": "-1"}
    assert dut.state.get("MUX_CABLE_INFO", "Ethernet0") == info
    assert dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0") == static
    assert dut.probe("Ethernet0") == "unknown"
    assert dut.toggle("Ethernet0", "standby") == {"state": "unknown", "read_side": "-1", "active_side": "-1"}
    # The other cable is not affected.
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


def test_insert_after_remove(dut):
    """A cable plugged in after a removal is initialized again: its driver is
    loaded anew, and HW_MUX_CABLE_TABLE and MUX_CABLE_STATIC_INFO describe the
    new cable, here one read from the other side. Its toggles are served.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4.
    2. Start ycabled. Remove Ethernet0's cable, as xcvrd reports it
       (TRANSCEIVER_INFO deleted, the cable absent), and wait for ycabled's
       HW_MUX_CABLE_TABLE write for it.
    3. Plug in a cable read from the other side, read side B: insert it, as
       xcvrd reports it, and wait for ycabled's MUX_CABLE_STATIC_INFO write.
    4. Expect the driver loaded again (a constructor call), Ethernet0's row
       {state: standby, read_side: 2, active_side: 1}, and the new cable's
       MUX_CABLE_STATIC_INFO.
    5. Toggle Ethernet0 to active and expect active.
    """
    cable = two_cables(dut)
    dut.start()
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    # A cable read from the other side is plugged in.
    cable.read_side = 2
    hw_mark = dut.hw.mark()
    mark = dut.monitor.mark()
    dut.insert("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="MUX_CABLE_STATIC_INFO", key="Ethernet0")
    assert dut.hw.calls(hw_mark, port=1, op="init"), "the driver is loaded again"
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "standby", "read_side": "2", "active_side": "1"}
    assert dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0") == expect.mux_cable_static_info(cable)
    assert dut.toggle("Ethernet0", "active")["state"] == "active"


def test_update_for_initialized_cable_is_ignored(dut):
    """sonic-platform-daemons#882: a TRANSCEIVER_INFO update for a port whose
    driver is initialized does not load it again.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4.
    2. Start ycabled.
    3. Update Ethernet0's TRANSCEIVER_INFO row (serial BB-NEW).
    4. Remove Ethernet4's cable and wait for ycabled's write for it:
       TRANSCEIVER_INFO events are handled in order, so the update before it
       has been handled too.
    5. Expect no write for Ethernet0, and no call to its cable, through the
       driver or the platform API, since the update.
    """
    two_cables(dut)
    dut.start()
    hw_mark = dut.hw.mark()
    mark = dut.monitor.mark()
    dut.state.set("TRANSCEIVER_INFO", "Ethernet0", {"serial": "BB-NEW"})
    remove_ethernet4(dut)
    assert not dut.writes(since=mark, key="Ethernet0")
    assert not dut.hw.calls(hw_mark, port=1, op=None)


def test_update_for_uninitialized_driver_loads_it_again(dut):
    """A driver that reported itself not initialized (like the simulated driver
    without its config file) is loaded again on the next update; once it is
    initialized, further updates are ignored.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4. Ethernet0's
       driver reports itself not initialized.
    2. Start ycabled, then make the driver report itself initialized.
    3. Update Ethernet0's TRANSCEIVER_INFO row. Expect ycabled's
       HW_MUX_CABLE_TABLE write for Ethernet0, and the driver's constructor
       called again.
    4. Update the row once more. Remove Ethernet4's cable and wait for
       ycabled's write for it: TRANSCEIVER_INFO events are handled in order, so
       the update before it has been handled too.
    5. Expect no constructor call since the second update.
    """
    cable = two_cables(dut)
    cable.initialized = False
    dut.start()
    cable.initialized = True
    hw_mark = dut.hw.mark()
    mark = dut.monitor.mark()
    dut.state.set("TRANSCEIVER_INFO", "Ethernet0", {"serial": "BB-NEW"})
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    assert dut.hw.calls(hw_mark, port=1, op="init")
    # Loaded and initialized now: the next update is ignored.
    hw_mark = dut.hw.mark()
    dut.state.set("TRANSCEIVER_INFO", "Ethernet0", {"serial": "BB-NEWER"})
    remove_ethernet4(dut)
    assert not dut.hw.calls(hw_mark, port=1, op="init")


def test_absent_at_startup_then_inserted(dut):
    """A cable absent at startup is published unknown, with placeholder
    MUX_CABLE_INFO and MUX_CABLE_STATIC_INFO. When it is plugged in, it is
    initialized, and its toggles are served.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4. Ethernet0's
       cable is absent, without TRANSCEIVER_INFO.
    2. Start ycabled.
    3. Expect Ethernet0's row {state: unknown, read_side: -1, active_side: -1},
       MUX_CABLE_INFO with the placeholder values besides time_post, and
       MUX_CABLE_STATIC_INFO with read_side -1 and every other field N/A.
    4. Insert the cable, as xcvrd reports it, and wait for ycabled's
       MUX_CABLE_STATIC_INFO write.
    5. Expect Ethernet0's state active and the cable's MUX_CABLE_STATIC_INFO;
       toggle Ethernet0 to standby and expect standby.
    """
    cable = two_cables(dut)
    cable.present = False
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "-1", "active_side": "-1"}
    info = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    info.pop("time_post")
    assert info == expect.PLACEHOLDER_INFO
    static = dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0")
    assert static.pop("read_side") == "-1" and set(static.values()) == {"N/A"}
    mark = dut.monitor.mark()
    dut.insert("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="MUX_CABLE_STATIC_INFO", key="Ethernet0")
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0") == expect.mux_cable_static_info(cable)
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"


@pytest.mark.known_issue("if no cable initialized at startup, the toggle, probe and CLI threads "
                         "never start, so cables inserted later are never served")
def test_first_cable_inserted_after_startup(dut):
    """A device whose only cable is plugged in after startup serves that cable:
    the cable is initialized, and its toggles are answered.

    Steps:
    1. Configure Ethernet0 as the only active-standby port, its cable absent.
    2. Start ycabled. It counts as ready once it has subscribed to
       TRANSCEIVER_INFO.
    3. Insert the cable, as xcvrd reports it, and expect ycabled's
       HW_MUX_CABLE_TABLE write within 5 s, with state active.
    4. Toggle Ethernet0 to standby without waiting, and expect ycabled to
       answer it, with state standby.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.hw.port(1).present = False
    dut.start(expect_mux=False)
    mark = dut.monitor.mark()
    dut.insert("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    mark = dut.monitor.mark()
    dut.toggle("Ethernet0", "standby", wait=False)
    assert dut.answered("toggle", "Ethernet0", mark), "the toggle was not answered"
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "standby"


@pytest.mark.known_issue("the toggle thread reads the ports' HW_MUX_CABLE_TABLE keys once, at its "
                         "start, so a port that gets its entry later is never toggled or probed")
def test_port_initialized_after_startup_is_served(dut):
    """Ethernet0's MUX_CABLE entry has no state at startup, so Python skips it
    (YANG defaults the state to auto). Once the state is set, the next
    insertion initializes the port, and its toggles and probes must then be
    served.

    Steps:
    1. Configure active-standby ports Ethernet0, whose MUX_CABLE entry has no
       state, and Ethernet4.
    2. Start ycabled.
    3. Set Ethernet0's MUX_CABLE state to auto, insert its cable, as xcvrd
       reports it, and wait up to 5 s for its HW_MUX_CABLE_TABLE state active.
    4. Toggle Ethernet0 to standby without waiting, and expect ycabled to
       answer it, with state standby.
    5. Probe Ethernet0 and expect standby.
    """
    topo = Topology().active_standby("Ethernet0", "Ethernet4")
    topo.mux["Ethernet0"].state = None
    dut.configure(topo)
    dut.start()
    dut.config.set("MUX_CABLE", "Ethernet0", {"state": "auto"})
    dut.insert("Ethernet0")
    wait_until(lambda: dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "active", 5)
    mark = dut.monitor.mark()
    dut.toggle("Ethernet0", "standby", wait=False)
    assert dut.answered("toggle", "Ethernet0", mark), "the toggle was not answered"
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "standby"
    assert dut.probe("Ethernet0") == "standby"


@pytest.mark.known_issue("an update without fields (a DEL, or a SET whose row is already gone) "
                         "as the first TRANSCEIVER_INFO event kills the insertion thread")
def test_first_transceiver_event_without_fields(dut):
    """The first TRANSCEIVER_INFO event, for a row that is gone by the time
    ycabled reads it, is handled as a removal, without a crash.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4.
    2. Start ycabled without publishing TRANSCEIVER_INFO first. On Python
       ycabled, let the teardown accept the crash of the known issue.
    3. In one transaction, write Ethernet0's TRANSCEIVER_INFO row and delete it
       again, as for a cable plugged and pulled before ycabled reads the row;
       make the cable absent.
    4. Expect ycabled's HW_MUX_CABLE_TABLE write for Ethernet0 within 5 s, with
       active_side -1.
    5. Expect no crash in syslog or in ycabled's output.
    """
    two_cables(dut)
    dut.start(publish_transceivers=False)
    dut.tolerate_python_failure(crash=True)
    mark = dut.monitor.mark()
    # A cable is plugged and pulled before ycabled reads the row.
    with dut.state.r.pipeline(transaction=True) as p:
        p.hset("TRANSCEIVER_INFO|Ethernet0", "type", "QSFP28 or later")
        p.delete("TRANSCEIVER_INFO|Ethernet0")
        p.execute()
    dut.hw.port(1).present = False
    assert dut.monitor.eventually(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0"), \
        "the removal was not handled"
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["active_side"] == "-1"
    assert not dut.crash_evidence()


def test_remove_active_active(dut):
    """Removing an active-active cable writes HW_MUX_CABLE_TABLE unknown, with
    the read side and active_side -1. The gRPC channel stays: the SoC still
    answers through the other link, so a toggle goes through.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, with a
       SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Remove Ethernet0's cable, as xcvrd reports it (TRANSCEIVER_INFO deleted,
       the cable absent), and wait for ycabled's HW_MUX_CABLE_TABLE write for
       it.
    4. Expect the row {state: unknown, read_side: 0, active_side: -1}.
    5. Toggle Ethernet0 to standby, and expect state standby and the SoC's
       admin state of port id 0 off.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    dut.start()
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "0", "active_side": "-1"}
    # The gRPC channel stays: the SoC still answers through the other link.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.known_issue("a re-inserted active-active cable keeps HW_MUX_CABLE_TABLE unknown "
                         "until the next toggle")
def test_reinsert_active_active_refreshes_state(dut):
    """A re-inserted active-active cable gets its state from the SoC again:
    HW_MUX_CABLE_TABLE goes back to active without waiting for a toggle.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, with a
       SoC double at 192.168.0.3.
    2. Start ycabled. Remove Ethernet0's cable, as xcvrd reports it
       (TRANSCEIVER_INFO deleted, the cable absent), and wait for ycabled's
       HW_MUX_CABLE_TABLE write for it.
    3. Insert the cable again, and expect Ethernet0's state active within 5 s.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    dut.soc(SOC)
    dut.start()
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    dut.insert("Ethernet0")
    assert eventually(lambda: dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "active", 5)


@pytest.mark.known_issue("removing a port that has no HW_MUX_CABLE_TABLE row creates one with "
                         "read_side 'None'")
def test_remove_port_without_state(dut):
    """Ethernet4 (active-active, no SoC address) has no HW_MUX_CABLE_TABLE row;
    its removal creates none.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       without a SoC address.
    2. Start ycabled. On Python ycabled, let the teardown accept the DB
       contract violation of the known issue.
    3. Expect no HW_MUX_CABLE_TABLE row for Ethernet4.
    4. Remove Ethernet4's cable, then Ethernet0's, and wait for ycabled's write
       for Ethernet0: removals are handled in order, so Ethernet4's has been
       handled.
    5. Expect no write for Ethernet4, and still no HW_MUX_CABLE_TABLE row for
       it.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4")
    dut.configure(topo)
    dut.start()
    dut.tolerate_python_failure(contract=True)
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4")
    mark = dut.monitor.mark()
    dut.remove("Ethernet4")
    # Barrier: Ethernet0's removal is handled after Ethernet4's.
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    assert not dut.writes(since=mark, key="Ethernet4")
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4")
