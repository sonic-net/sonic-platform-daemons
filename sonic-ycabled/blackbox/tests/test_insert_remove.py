"""Cable insertion and removal, from STATE_DB TRANSCEIVER_INFO (HLD 3.4.7)."""

import pytest

from harness import expect
from harness.topology import Topology
from harness.util import eventually, wait_until

SOC = "127.0.0.2"


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
    two_cables(dut)
    dut.start()
    info = dut.state.get("MUX_CABLE_INFO", "Ethernet0")
    static = dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0")
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    # The read side is kept; MUX_CABLE_INFO and MUX_CABLE_STATIC_INFO too.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {"state": "unknown", "read_side": "1", "active_side": "-1"}
    assert dut.state.get("MUX_CABLE_INFO", "Ethernet0") == info
    assert dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0") == static
    assert dut.probe("Ethernet0") == "unknown"
    assert dut.toggle("Ethernet0", "standby") == {"state": "unknown", "read_side": "-1", "active_side": "-1"}
    # The other cable is not affected.
    assert dut.toggle("Ethernet4", "standby")["state"] == "standby"


def test_insert_after_remove(dut):
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
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {"state": "standby", "read_side": "2", "active_side": "1"}
    assert dut.state.get("MUX_CABLE_STATIC_INFO", "Ethernet0") == expect.mux_cable_static_info(cable)
    assert dut.toggle("Ethernet0", "active")["state"] == "active"


def test_update_for_initialized_cable_is_ignored(dut):
    """sonic-platform-daemons#882: a TRANSCEIVER_INFO update for a port whose
    driver is initialized does not load it again."""
    two_cables(dut)
    dut.start()
    hw_mark = dut.hw.mark()
    mark = dut.monitor.mark()
    dut.state.set("TRANSCEIVER_INFO", "Ethernet0", {"serial": "BB-NEW"})
    remove_ethernet4(dut)
    assert not dut.writes(since=mark, key="Ethernet0")
    assert not dut.hw.calls(hw_mark, port=1, op=None)


def test_update_for_uninitialized_driver_loads_it_again(dut):
    """A driver that reported itself not initialized (like the simulated
    driver without its config file) is loaded again on the next update."""
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
    cable = two_cables(dut)
    cable.present = False
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {"state": "unknown", "read_side": "-1", "active_side": "-1"}
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


@pytest.mark.known_issue("3.8-16", "if no cable initialized at startup, the toggle, probe and CLI threads "
                                    "never start, so cables inserted later are never served")
def test_first_cable_inserted_after_startup(dut):
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


@pytest.mark.known_issue("3.8-5", "the toggle thread reads the ports' HW_MUX_CABLE_TABLE keys once, at its "
                                   "start, so a port that gets its entry later is never toggled or probed")
def test_port_initialized_after_startup_is_served(dut):
    """Ethernet0's MUX_CABLE entry has no state at startup, so Python skips
    it (YANG defaults the state to auto). Once the state is set, the next
    insertion initializes the port, and its toggles and probes must then be
    served."""
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


@pytest.mark.known_issue("3.8-15", "an update without fields (a DEL, or a SET whose row is already gone) "
                                    "as the first TRANSCEIVER_INFO event kills the insertion thread")
def test_first_transceiver_event_without_fields(dut):
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
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    dut.start()
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {"state": "unknown", "read_side": "0", "active_side": "-1"}
    # The gRPC channel stays: the SoC still answers through the other link.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.known_issue("3.8-21", "a re-inserted active-active cable keeps HW_MUX_CABLE_TABLE unknown "
                                    "until the next toggle")
def test_reinsert_active_active_refreshes_state(dut):
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    dut.soc(SOC)
    dut.start()
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
    dut.insert("Ethernet0")
    assert eventually(lambda: dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "active", 5)


@pytest.mark.known_issue("3.8-22", "removing a port that has no HW_MUX_CABLE_TABLE row creates one with "
                                    "read_side 'None'")
def test_remove_port_without_state(dut):
    """Ethernet4 (active-active, no SoC address) has no HW_MUX_CABLE_TABLE
    row; its removal creates none."""
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
