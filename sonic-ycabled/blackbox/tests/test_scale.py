"""Many mux ports, each toggled many times: every request is answered with the
requested state, each cable and SoC gets the calls the requests ask for, in
order, and two calls to one cable never overlap."""

import bisect

import pytest

from harness.fake_hw import TARGET_UPPER_TOR, TARGET_LOWER_TOR
from harness.topology import Topology, soc_ip
from harness.util import wait_until

pytestmark = pytest.mark.slow

PORTS = 100
ROUNDS = 100
# Python ycabled handles the requests of every port one at a time.
ROUND_TIMEOUT = 60


def port_names(n):
    return ["Ethernet{}".format(4 * i) for i in range(n)]


def round_state(r):
    """The state requested in round `r`. The ports start active."""
    return "standby" if r % 2 == 0 else "active"


def opposite(state):
    return "active" if state == "standby" else "standby"


def send(dut, requests):
    """Write the requests of a round in one transaction, as when many ports
    switch over at once. `requests`: (APPL_DB table, port, state)."""
    with dut.appl.r.pipeline(transaction=True) as p:
        for table, port, state in requests:
            p.hset("{}:{}".format(table, port), "state", state)
        p.execute()


def answers(dut, since, table, ports):
    """The states ycabled wrote to STATE_DB `table` for each port after
    `since`, once every port has one."""
    def collect():
        got = {}
        for a in dut.monitor.accesses(since, kind="write", db="STATE_DB", table=table, field="state"):
            got.setdefault(a.key, []).append(a.value)
        return got if all(port in got for port in ports) else None
    return wait_until(collect, ROUND_TIMEOUT,
                      message=lambda: "ycabled did not answer every port's {} within {}s".format(table, ROUND_TIMEOUT))


def check_round(got, r, table, state):
    # One answer per port and request: a second one would show a request
    # handled twice, or handled with the state of another round.
    wrong = {port: states for port, states in got.items() if states != [state]}
    assert not wrong, "round {}, {} {}: {}".format(r, table, state, wrong)


def settle(dut):
    """Wait until ycabled has sent nothing for 1 s, and the recorder has all it
    sent."""
    assert dut.monitor.wait_settled(quiet=1, timeout=ROUND_TIMEOUT), "ycabled did not go quiet"
    dut.monitor.barrier()


def check_rounds(dut, marks, table, state_of, ports):
    """Check each round again, once ycabled has settled: every answer from the
    round's request (`marks[r]`) to the next round's, or to the end. An answer
    written again after its round was checked, or after the last round, fails
    here."""
    got = [{port: [] for port in ports} for _ in marks]
    for a in dut.monitor.accesses(marks[0], kind="write", db="STATE_DB", table=table, field="state"):
        got[bisect.bisect_right(marks, a.seq) - 1].setdefault(a.key, []).append(a.value)
    for r, round_got in enumerate(got):
        check_round(round_got, r, table, state_of(r))


def test_active_standby_ports_toggled_many_times(dut):
    """100 active-standby cables, half of them read from the lower ToR, all
    toggled together 100 times. Every toggle is answered once, with the
    requested state; each cable gets one toggle call per round, toward the side
    the round asks for; two calls to one cable never overlap.

    Steps:
    1. Configure 100 active-standby ports, Ethernet0 to Ethernet396, with
       telemetry disabled, so that only the toggles reach the cables. Half of
       the cables are read from the upper ToR and half from the lower ToR, each
       with its mux on its own side.
    2. Start ycabled, waiting up to 120 s until it is ready.
    3. 100 rounds: in one transaction, request standby (even rounds) or active
       (odd rounds) for every port. Expect, within 60 s, exactly one
       HW_MUX_CABLE_TABLE state answer per port, with that state.
    4. Once ycabled has sent nothing for 1 s, expect the same of each round
       again, counting every answer from its request to the next round's, or
       to the end: none was written twice after its round was checked.
    5. Expect each port's final row {state: active, read_side: its read side,
       active_side: the same side}, and its mux on that side.
    6. Expect each cable's toggle calls to be the 100 the rounds ask for, in
       order, and no overlapping calls to any cable.
    """
    ports = port_names(PORTS)
    topo = Topology(nports=PORTS).active_standby(*ports)
    # Only the toggles reach the cables.
    topo.xcvrd_log = {"disable_telemetry": "True"}
    dut.configure(topo)
    read_sides = {}
    for i, port in enumerate(ports):
        cable = dut.hw.port(topo.index(port))
        cable.read_side = cable.mux_direction = TARGET_UPPER_TOR if i % 2 == 0 else TARGET_LOWER_TOR
        read_sides[port] = cable.read_side
    dut.start(timeout=120)
    hw_mark = dut.hw.mark()

    marks = []
    for r in range(ROUNDS):
        state = round_state(r)
        marks.append(dut.monitor.mark())
        send(dut, [("HW_MUX_CABLE_TABLE", port, state) for port in ports])
        check_round(answers(dut, marks[r], "HW_MUX_CABLE_TABLE", ports), r, "HW_MUX_CABLE_TABLE", state)
    settle(dut)
    check_rounds(dut, marks, "HW_MUX_CABLE_TABLE", round_state, ports)

    final = round_state(ROUNDS - 1)
    for port in ports:
        read_side = read_sides[port]
        other = TARGET_LOWER_TOR if read_side == TARGET_UPPER_TOR else TARGET_UPPER_TOR

        def side(state):
            return read_side if state == "active" else other
        assert dut.state.get("HW_MUX_CABLE_TABLE", port) == {
            "state": final, "read_side": str(read_side), "active_side": str(side(final))}, port
        assert dut.hw.port(topo.index(port)).mux_direction == side(final), port
        expected = ["toggle_mux_to_tor_a" if side(round_state(r)) == TARGET_UPPER_TOR else "toggle_mux_to_tor_b"
                    for r in range(ROUNDS)]
        toggles = [c.method for c in dut.hw.calls(hw_mark, port=topo.index(port))
                   if c.method.startswith("toggle_mux_to_tor_")]
        assert toggles == expected, port
    overlaps = dut.hw.overlaps(hw_mark)
    assert not overlaps, overlaps[:5]


def test_active_active_ports_toggled_many_times(dut):
    """100 active-active ports, each with its own SoC: this ToR's side and the
    peer's toggled together 100 times. Every request is answered once, with the
    requested state, and each SoC gets one RPC per request, in order.

    Steps:
    1. Configure 100 active-active ports of the lower ToR, Ethernet0 to
       Ethernet396, each with its own SoC double, at 192.168.0.3 to
       192.168.0.201, with telemetry disabled.
    2. Start ycabled, waiting up to 120 s until it is ready.
    3. 100 rounds: in one transaction, request standby (even rounds) or active
       (odd rounds) for every port in HW_MUX_CABLE_TABLE, and the opposite
       state for the peer in HW_FORWARDING_STATE_PEER. Expect, within 60 s,
       exactly one answer per port in HW_MUX_CABLE_TABLE and in
       HW_MUX_CABLE_TABLE_PEER, with the requested states.
    4. Once ycabled has sent nothing for 1 s, expect the same of each round
       again, counting every answer from its request to the next round's, or
       to the end: none was written twice after its round was checked.
    5. Expect each port's final rows, active for this ToR and standby for the
       peer, with read_side 0, and the SoC's admin states to match.
    6. Expect 200 SetAdminForwardingPortState RPCs at each SoC: for port id 0,
       the states of this ToR's requests, and for port id 1, those of the
       peer's, each in order.
    """
    ports = port_names(PORTS)
    topo = Topology(nports=PORTS)
    for port in ports:
        topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
    topo.xcvrd_log = {"disable_telemetry": "True"}
    dut.configure(topo)
    socs = {port: dut.soc(soc_ip(port)) for port in ports}
    dut.start(timeout=120)
    soc_marks = {port: soc.mark() for port, soc in socs.items()}

    marks = []
    for r in range(ROUNDS):
        state = round_state(r)
        marks.append(dut.monitor.mark())
        send(dut, [(table, port, s) for port in ports
                   for table, s in (("HW_MUX_CABLE_TABLE", state), ("HW_FORWARDING_STATE_PEER", opposite(state)))])
        check_round(answers(dut, marks[r], "HW_MUX_CABLE_TABLE", ports), r, "HW_MUX_CABLE_TABLE", state)
        check_round(answers(dut, marks[r], "HW_MUX_CABLE_TABLE_PEER", ports), r, "HW_MUX_CABLE_TABLE_PEER",
                    opposite(state))
    settle(dut)
    check_rounds(dut, marks, "HW_MUX_CABLE_TABLE", round_state, ports)
    check_rounds(dut, marks, "HW_MUX_CABLE_TABLE_PEER", lambda r: opposite(round_state(r)), ports)

    final = round_state(ROUNDS - 1)
    for port, soc in socs.items():
        assert dut.state.get("HW_MUX_CABLE_TABLE", port) == {
            "state": final, "read_side": "0", "active_side": final}, port
        assert dut.state.get("HW_MUX_CABLE_TABLE_PEER", port) == {
            "state": opposite(final), "read_side": "0", "active_side": opposite(final)}, port
        # The device is the lower ToR: port id 0 is its side, 1 the peer's.
        assert soc.admin == [final == "active", final != "active"], port
        sets = [rpc.request for rpc in soc.rpcs("SetAdminForwardingPortState", since=soc_marks[port])]
        assert len(sets) == 2 * ROUNDS, (port, len(sets))
        for portid, active_in in ((0, "active"), (1, "standby")):
            states = [req["state"] for req in sets if req["portid"] == [portid]]
            assert states == [[round_state(r) == active_in] for r in range(ROUNDS)], (port, portid)
