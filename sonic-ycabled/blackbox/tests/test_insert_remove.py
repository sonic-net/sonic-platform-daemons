"""Cable insertion and removal, from STATE_DB TRANSCEIVER_INFO (HLD 3.4.7)."""

import time

import pytest

from harness import expect
from harness.topology import Topology
from harness.util import stays_true, wait_until

SOC = "127.0.0.2"


def two_cables(dut):
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    return dut.hw.port(1)


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
    assert stays_true(lambda: not dut.writes(since=mark, key="Ethernet0"), 1.5)
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
    time.sleep(1)
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
    assert dut.toggle("Ethernet0", "standby", timeout=3)["state"] == "standby"


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
    dut.monitor.wait_access(mark, 3, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")
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
    mark = dut.monitor.mark()
    dut.insert("Ethernet0")
    wait_until(lambda: dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "active", 3)


@pytest.mark.known_issue("3.8-22", "removing a port that has no HW_MUX_CABLE_TABLE row creates one with "
                                    "read_side 'None'")
def test_remove_port_without_state(dut):
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4")
    dut.configure(topo)
    dut.start()
    dut.tolerate_python_failure(contract=True)
    mark = dut.monitor.mark()
    dut.remove("Ethernet4")
    time.sleep(1.5)
    bad = [a for a in dut.writes(since=mark, key="Ethernet4") if a.field == "read_side" and a.value == "None"]
    assert not bad
