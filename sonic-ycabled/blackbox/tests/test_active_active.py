"""Active-active cables: toggles, probes and the SoC connection (HLD 3.3.3 to
3.3.6, 3.4.1; F3, C2, N1, N3)."""

import re
import time

import grpc
import pytest

from harness.certs import SOC_NAME, Pki
from harness.topology import LOOPBACK3_UPPER, LOOPBACK3_LOWER, Topology, soc_ip
from harness.util import TIMESTAMP_RE, stays_true, wait_until

# Ethernet0's SoC, 192.168.0.3 and fc02:1000::3.
SOC = soc_ip("Ethernet0")
SOC_V6 = soc_ip("Ethernet0", ipv6=True)
CERT_DIR = "/etc/sonic/bbcerts"

# read_side: index of this ToR in the SoC's port lists.
READ_SIDES = [(LOOPBACK3_UPPER, 1), (LOOPBACK3_LOWER, 0)]


def add_cable_aa(dut, loopback3=LOOPBACK3_LOWER, soc=True, **kw):
    """Configure Ethernet0 as an active-active port, its SoC at 192.168.0.3,
    and return a plaintext SoC double started there. soc=False starts none,
    for the tests that start a TLS double or need none: gRPC lets a second
    double listen on the same address, and the two would share ycabled's
    connections."""
    dut.configure(Topology(loopback3=loopback3).active_active("Ethernet0", soc_ipv4=SOC + "/32", **kw))
    return dut.soc(SOC) if soc else None


@pytest.fixture
def pki(dut):
    p = Pki(CERT_DIR, soc_ips=[SOC])
    dut.files.append(CERT_DIR)
    return p


@pytest.mark.parametrize("loopback3,read_side", READ_SIDES)
@pytest.mark.parametrize("state", ["active", "standby"])
def test_toggle_self(dut, loopback3, read_side, state):
    """orchagent's toggle of an active-active port sets this ToR's admin
    forwarding state on the SoC with one SetAdminForwardingPortState RPC for
    this ToR's port id, and ycabled answers in STATE_DB HW_MUX_CABLE_TABLE with
    the state, the read side, and the state again as active_side. It records
    the switch's start and end in MUX_METRICS_TABLE.

    Cases: the upper ToR (read side 1) or the lower ToR (read side 0), from
    Loopback3, toggled to active or standby.

    Steps:
    1. Configure Ethernet0 as an active-active port with the case's Loopback3
       and a SoC double at 192.168.0.3, both of whose admin states are opposite
       to the requested state.
    2. Start ycabled.
    3. Toggle Ethernet0 to the requested state.
    4. Expect the STATE_DB row {state: the requested state, read_side: 0 or 1,
       active_side: the requested state}.
    5. Expect one SetAdminForwardingPortState RPC since the toggle, {portid:
       [the read side], state: [True for active]}, and the SoC's admin state of
       this ToR's port id changed.
    6. Expect MUX_METRICS_TABLE xcvrd_switch_self_<state>_start and _end in
       ycabled's timestamp format.
    """
    soc = add_cable_aa(dut, loopback3)
    soc.admin = [state != "active"] * 2
    dut.start()
    mark = soc.mark()
    row = dut.toggle("Ethernet0", state)
    assert row == {"state": state, "read_side": str(read_side), "active_side": state}
    rpcs = soc.rpcs("SetAdminForwardingPortState", mark)
    assert [r.request for r in rpcs] == [{"portid": [read_side], "state": [state == "active"]}]
    assert soc.admin[read_side] == (state == "active")
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    for edge in ("start", "end"):
        assert re.match(TIMESTAMP_RE, metrics["xcvrd_switch_self_{}_{}".format(state, edge)])


@pytest.mark.parametrize("loopback3,read_side", READ_SIDES)
@pytest.mark.parametrize("state", ["active", "standby"])
def test_toggle_peer(dut, loopback3, read_side, state):
    """linkmgrd's peer toggle (APPL_DB HW_FORWARDING_STATE_PEER) sets the peer
    ToR's admin forwarding state on the SoC, the port id that is not this ToR's
    read side. ycabled answers in STATE_DB HW_MUX_CABLE_TABLE_PEER, records the
    switch in MUX_METRICS_TABLE, and leaves this ToR's own HW_MUX_CABLE_TABLE
    row alone.

    Cases: the upper ToR (read side 1) or the lower ToR (read side 0), from
    Loopback3, with the peer toggled to active or standby.

    Steps:
    1. Configure Ethernet0 as an active-active port with the case's Loopback3
       and a SoC double at 192.168.0.3, both of whose admin states are opposite
       to the requested state.
    2. Start ycabled, and note Ethernet0's STATE_DB HW_MUX_CABLE_TABLE row.
    3. Send a peer toggle for Ethernet0 to the requested state, as linkmgrd
       does.
    4. Expect the STATE_DB HW_MUX_CABLE_TABLE_PEER row {state: the requested
       state, read_side: this ToR's read side, active_side: the requested
       state}.
    5. Expect one SetAdminForwardingPortState RPC since the request, {portid:
       [the peer's port id], state: [True for active]}, and the SoC's admin
       state of the peer's port id changed.
    6. Expect MUX_METRICS_TABLE xcvrd_switch_peer_<state>_start and _end, and
       the HW_MUX_CABLE_TABLE row unchanged.
    """
    soc = add_cable_aa(dut, loopback3)
    soc.admin = [state != "active"] * 2
    dut.start()
    self_before = dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")
    mark = soc.mark()
    row = dut.peer_toggle("Ethernet0", state)
    assert row == {"state": state, "read_side": str(read_side), "active_side": state}
    assert [r.request for r in soc.rpcs("SetAdminForwardingPortState", mark)] == [
        {"portid": [1 - read_side], "state": [state == "active"]}]
    assert soc.admin[1 - read_side] is (state == "active")
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    for edge in ("start", "end"):
        assert re.match(TIMESTAMP_RE, metrics["xcvrd_switch_peer_{}_{}".format(state, edge)])
    # The self side is not touched.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == self_before


def test_toggle_peer_failure(dut):
    """A peer toggle that the SoC fails is answered unknown in
    HW_MUX_CABLE_TABLE_PEER, with active_side unknown, and its metrics are
    recorded under unknown.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Script the SoC's SetAdminForwardingPortState to fail with UNAVAILABLE.
    4. Send a peer toggle to standby for Ethernet0.
    5. Expect the STATE_DB HW_MUX_CABLE_TABLE_PEER row {state: unknown,
       read_side: 0, active_side: unknown}, and only
       xcvrd_switch_peer_unknown_start and _end in MUX_METRICS_TABLE.
    """
    soc = add_cable_aa(dut)
    dut.start()
    soc.script("SetAdminForwardingPortState", code=grpc.StatusCode.UNAVAILABLE)
    row = dut.peer_toggle("Ethernet0", "standby")
    assert row == {"state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert set(dut.state.get("MUX_METRICS_TABLE", "Ethernet0")) == {
        "xcvrd_switch_peer_unknown_start", "xcvrd_switch_peer_unknown_end"}


def _reply_wrong_port(request, pb2):
    return pb2.AdminReply(portid=[7], state=[True])


def _reply_empty(request, pb2):
    return pb2.AdminReply()


@pytest.mark.parametrize("script", [
    {"code": grpc.StatusCode.UNAVAILABLE},
    {"code": grpc.StatusCode.INTERNAL},
    {"code": grpc.StatusCode.CANCELLED},
    {"code": grpc.StatusCode.INVALID_ARGUMENT},
    {"reply": _reply_wrong_port},
    {"reply": _reply_empty},
], ids=["unavailable", "internal", "cancelled", "invalid-argument", "wrong-port", "empty-reply"])
def test_toggle_failure(dut, script):
    """A toggle that the SoC fails, or answers with a reply that does not
    confirm this ToR's port, is answered unknown, and its metrics are recorded
    under unknown.

    Cases: SetAdminForwardingPortState fails with UNAVAILABLE, INTERNAL,
    CANCELLED or INVALID_ARGUMENT, or replies for port id 7 only (wrong-port),
    or with an empty reply (empty-reply).

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Script the SoC's SetAdminForwardingPortState that way.
    4. Toggle Ethernet0 to standby.
    5. Expect the STATE_DB row {state: unknown, read_side: 0, active_side:
       unknown}, and only xcvrd_switch_self_unknown_start and _end in
       MUX_METRICS_TABLE.
    """
    soc = add_cable_aa(dut)
    dut.start()
    soc.script("SetAdminForwardingPortState", **script)
    assert dut.toggle("Ethernet0", "standby") == {"state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert set(dut.state.get("MUX_METRICS_TABLE", "Ethernet0")) == {
        "xcvrd_switch_self_unknown_start", "xcvrd_switch_self_unknown_end"}


def test_toggle_soc_hangs(dut):
    """The SoC does not answer: the request fails at the 0.5 s deadline, and
    the toggle is answered unknown.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Script the SoC's SetAdminForwardingPortState to answer after 3 s.
    4. Toggle Ethernet0 to standby, and expect state unknown, answered 0.4 s to
       2 s after the request.
    """
    soc = add_cable_aa(dut)
    dut.start()
    soc.script("SetAdminForwardingPortState", delay=3)
    t0 = time.monotonic()
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
    assert 0.4 <= time.monotonic() - t0 < 2.0


def test_toggle_ignores_other_states(dut):
    """A toggle request with a state other than active or standby, such as
    orchagent's init, is ignored: no HW_MUX_CABLE_TABLE write, no RPC to the
    SoC, and toggles are still served.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Write HW_MUX_CABLE_TABLE state=init for Ethernet0 to APPL_DB.
    4. Expect, for 1.5 s, no HW_MUX_CABLE_TABLE write by ycabled, and no
       SetAdminForwardingPortState RPC.
    5. Toggle Ethernet0 to standby and expect state standby.
    """
    soc = add_cable_aa(dut)
    dut.start()
    mark = dut.monitor.mark()
    soc_mark = soc.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"state": "init"})
    assert stays_true(lambda: not dut.writes(since=mark, table="HW_MUX_CABLE_TABLE"), 1.5)
    assert not soc.rpcs("SetAdminForwardingPortState", soc_mark)
    # The toggle thread is still serving.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"


@pytest.mark.parametrize("loopback3,read_side", READ_SIDES)
def test_forwarding_state_probe(dut, loopback3, read_side):
    """linkmgrd's forwarding state probe queries the admin forwarding state of
    both ports on the SoC with one QueryAdminForwardingPortState RPC, and is
    answered in APPL_DB FORWARDING_STATE_RESPONSE: response is this ToR's
    state, response_peer the peer's. A probe only answers: HW_MUX_CABLE_TABLE
    is not written.

    Cases: the upper ToR (read side 1) or the lower ToR (read side 0), from
    Loopback3.

    Steps:
    1. Configure Ethernet0 as an active-active port with the case's Loopback3
       and a SoC double at 192.168.0.3, whose admin state is standby for port
       id 1, the upper ToR, and active for port id 0, the lower ToR.
    2. Start ycabled.
    3. Send a forwarding state probe for Ethernet0, as linkmgrd does through
       FORWARDING_STATE_COMMAND.
    4. Expect {response: standby, response_peer: active} on the upper ToR, and
       the reverse on the lower ToR.
    5. Expect one QueryAdminForwardingPortState RPC since the probe, {portid:
       [0, 1], state: [False, False]}, and no HW_MUX_CABLE_TABLE write.
    """
    soc = add_cable_aa(dut, loopback3)
    soc.admin = [True, False]
    dut.start()
    mark = soc.mark()
    db_mark = dut.monitor.mark()
    row = dut.fwd_probe("Ethernet0")
    self_state, peer_state = ("active", "standby") if read_side == 0 else ("standby", "active")
    assert row == {"response": self_state, "response_peer": peer_state}
    assert [r.request for r in soc.rpcs("QueryAdminForwardingPortState", mark)] == [
        {"portid": [0, 1], "state": [False, False]}]
    # A probe only answers; HW_MUX_CABLE_TABLE is not touched.
    assert not dut.writes(since=db_mark, table="HW_MUX_CABLE_TABLE")


def _reply_short(request, pb2):
    return pb2.AdminReply(portid=[0], state=[True])


@pytest.mark.parametrize("script", [{"code": grpc.StatusCode.UNAVAILABLE}, {"reply": _reply_short},
                                    {"delay": 3}], ids=["unavailable", "short-reply", "hang"])
def test_forwarding_state_probe_failure(dut, script):
    """A forwarding state probe that the SoC fails, answers for one port only,
    or does not answer within the deadline is answered unknown for both sides.

    Cases: QueryAdminForwardingPortState fails with UNAVAILABLE (unavailable),
    replies for port id 0 only (short-reply), or answers after 3 s (hang).

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Script the SoC's QueryAdminForwardingPortState that way.
    4. Send a forwarding state probe for Ethernet0, and expect {response:
       unknown, response_peer: unknown}.
    """
    soc = add_cable_aa(dut)
    dut.start()
    soc.script("QueryAdminForwardingPortState", **script)
    assert dut.fwd_probe("Ethernet0") == {"response": "unknown", "response_peer": "unknown"}


def test_forwarding_state_probe_ignores_other_commands(dut):
    """A FORWARDING_STATE_COMMAND other than probe is ignored: no
    FORWARDING_STATE_RESPONSE write, and probes are still served.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Write FORWARDING_STATE_COMMAND command=reset for Ethernet0 to APPL_DB.
    4. Expect no FORWARDING_STATE_RESPONSE write for 1.5 s.
    5. Send a forwarding state probe for Ethernet0 and expect response active.
    """
    add_cable_aa(dut)
    dut.start()
    mark = dut.monitor.mark()
    dut.appl.set("FORWARDING_STATE_COMMAND", "Ethernet0", {"command": "reset"})
    assert stays_true(lambda: not dut.writes(since=mark, table="FORWARDING_STATE_RESPONSE"), 1.5)
    # The probe thread is still serving.
    assert dut.fwd_probe("Ethernet0")["response"] == "active"


def test_probes_of_other_cable_types_do_not_block(dut):
    """A forwarding probe or peer toggle for an active-standby port is skipped,
    and the active-active requests queued with it still run.

    Python stops reading the table at the first such port (`break`, not
    `continue`) and leaves the entries after it in the subscriber's buffer.
    They still run, one more pass of its loop later: a subscriber that
    swsscommon's Select returns with more than one entry pending stays in
    Select's ready set (SubscriberStateTable::hasCachedData), and the next
    select() returns it without waiting.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       with a SoC double at 192.168.0.5.
    2. Start ycabled.
    3. In one transaction, write FORWARDING_STATE_COMMAND command=probe and
       HW_FORWARDING_STATE_PEER state=active for both ports, Ethernet0's first.
    4. Expect ycabled's FORWARDING_STATE_RESPONSE and HW_MUX_CABLE_TABLE_PEER
       writes for Ethernet4, each within 3 s.
    5. Expect no write for Ethernet0.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4=soc_ip("Ethernet4") + "/32")
    dut.configure(topo)
    dut.soc(soc_ip("Ethernet4"))
    dut.start()
    mark = dut.monitor.mark()
    with dut.appl.r.pipeline(transaction=True) as p:
        p.hset("FORWARDING_STATE_COMMAND:Ethernet0", "command", "probe")
        p.hset("FORWARDING_STATE_COMMAND:Ethernet4", "command", "probe")
        p.hset("HW_FORWARDING_STATE_PEER:Ethernet0", "state", "active")
        p.hset("HW_FORWARDING_STATE_PEER:Ethernet4", "state", "active")
        p.execute()
    dut.monitor.wait_access(mark, 3, kind="write", table="FORWARDING_STATE_RESPONSE", key="Ethernet4")
    dut.monitor.wait_access(mark, 3, kind="write", table="HW_MUX_CABLE_TABLE_PEER", key="Ethernet4")
    assert not dut.monitor.accesses(mark, kind="write", key="Ethernet0")


def test_port_without_soc_address(dut):
    """An active-active port without a SoC address is not served: it gets no
    HW_MUX_CABLE_TABLE entry, and its requests are ignored.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       without soc_ipv4 or soc_ipv6.
    2. Start ycabled.
    3. Expect no STATE_DB HW_MUX_CABLE_TABLE entry for Ethernet4, and 'No SoC
       IP address found for gRPC channel setup on port Ethernet4' in the log.
    4. Write a toggle (HW_MUX_CABLE_TABLE state=standby) and a forwarding state
       probe for Ethernet4, and expect no write for Ethernet4 for 1.5 s.
    5. Toggle Ethernet0 to standby and expect state standby: toggles are still
       served.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4")
    dut.configure(topo)
    dut.start()
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4")
    assert dut.logs(contains="No SoC IP address found for gRPC channel setup on port Ethernet4")
    mark = dut.monitor.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet4", {"state": "standby"})
    dut.appl.set("FORWARDING_STATE_COMMAND", "Ethernet4", {"command": "probe"})
    assert stays_true(lambda: not dut.writes(since=mark, key="Ethernet4"), 1.5)
    # The toggle thread is still serving.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"


def test_cable_absent_at_startup(dut):
    """No channel is set up for an absent cable, and the port is published
    unknown, for this ToR and the peer; the first toggle sets the channel up.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3. Its cable is absent: the platform
       API reports it absent, and no TRANSCEIVER_INFO is published for it.
    2. Start ycabled.
    3. Expect 'DAC cable not present while Channel setup Port Ethernet0' in the
       log, and the STATE_DB HW_MUX_CABLE_TABLE and HW_MUX_CABLE_TABLE_PEER
       rows {state: unknown, read_side: 0, active_side: unknown}.
    4. Expect no QueryAdminForwardingPortState RPC at the SoC.
    5. Toggle Ethernet0 to standby, and expect state standby and the SoC's
       admin state of port id 0 off.
    """
    soc = add_cable_aa(dut)
    dut.hw.port(1).present = False
    dut.start()
    assert dut.logs(contains="DAC cable not present while Channel setup Port Ethernet0")
    unknown = {"state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == unknown
    assert dut.state.get("HW_MUX_CABLE_TABLE_PEER", "Ethernet0") == unknown
    assert not soc.rpcs("QueryAdminForwardingPortState")
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.parametrize("fields", [{"soc_ipv6": SOC_V6 + "/128"},
                                    {"soc_ipv4": SOC + "/32", "soc_ipv6": SOC_V6 + "/128"}],
                         ids=["ipv6-only", "ipv4-preferred"])
def test_soc_address_family(dut, fields):
    """ycabled reaches the SoC at its IPv6 address when MUX_CABLE has only
    soc_ipv6, and at its IPv4 address when it has both.

    Cases: soc_ipv6 fc02:1000::3 only (ipv6-only), or soc_ipv4 192.168.0.3 and
    soc_ipv6 fc02:1000::3 (ipv4-preferred).

    Steps:
    1. Configure Ethernet0 as active-active with the case's SoC addresses, and
       start a SoC double at fc02:1000::3 or at 192.168.0.3.
    2. Start ycabled.
    3. Expect Ethernet0's STATE_DB state active, and
       QueryAdminForwardingPortState RPCs at the SoC double.
    """
    port = Topology().active_active("Ethernet0", **fields)
    dut.configure(port)
    soc = dut.soc(SOC_V6 if "soc_ipv4" not in fields else SOC)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert soc.rpcs("QueryAdminForwardingPortState")


def test_invalid_soc_address(dut):
    """An invalid soc_ipv4 is logged, and its port is not served: it gets no
    HW_MUX_CABLE_TABLE entry.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       with soc_ipv4 192.168.0/32.
    2. Start ycabled.
    3. Expect 'Invalid soc_ipv4 address 192.168.0/32' in the log, and no
       STATE_DB HW_MUX_CABLE_TABLE entry for Ethernet4.
    """
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4="192.168.0/32")
    dut.configure(topo)
    dut.start()
    assert dut.logs(contains="Invalid soc_ipv4 address 192.168.0/32")
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4")


def test_soc_comes_up_later(dut):
    """A SoC that is down at startup leaves the port unknown; when it comes up,
    gRPC reconnects, and the port's probes and toggles go through.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, where nothing listens yet.
    2. Start ycabled.
    3. Expect the STATE_DB row {state: unknown, read_side: 0, active_side:
       unknown}, and a forwarding state probe answered unknown or failure.
    4. Start the SoC double, and send forwarding state probes, as linkmgrd
       keeps doing, until one is answered active, within 15 s: gRPC reconnects
       with backoff.
    5. Toggle Ethernet0 to standby, and expect state standby and the SoC's
       admin state of port id 0 off.
    """
    add_cable_aa(dut, soc=False)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert dut.fwd_probe("Ethernet0")["response"] in ("unknown", "failure")
    soc = dut.soc(SOC)
    # gRPC reconnects with backoff; linkmgrd keeps probing.
    wait_until(lambda: dut.fwd_probe("Ethernet0")["response"] == "active", 15)
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


def test_connection_loss_reports_failure(dut):
    """C2: when the channel fails, FORWARDING_STATE_RESPONSE gets
    response=failure, which linkmgrd treats as a connection failure. When the
    SoC is back, probes are answered again.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3.
    2. Start ycabled, send a forwarding state probe and expect response active:
       the channel is up.
    3. Stop the SoC double.
    4. Expect, within 10 s, a FORWARDING_STATE_RESPONSE write for Ethernet0
       whose first field is response=failure.
    5. Start the SoC double again, and expect a forwarding state probe answered
       active within 15 s.
    """
    soc = add_cable_aa(dut)
    dut.start()
    assert dut.fwd_probe("Ethernet0")["response"] == "active"
    mark = dut.monitor.mark()
    soc.stop()
    writes = dut.monitor.wait_access(mark, 10, kind="write", table="FORWARDING_STATE_RESPONSE", key="Ethernet0")
    assert [(a.field, a.value) for a in writes][:1] == [("response", "failure")]
    soc.start()
    wait_until(lambda: dut.fwd_probe("Ethernet0")["response"] == "active", 15)


def tls_config(dut, pki, auth_level="server", ca="ca.crt", client=True, target=SOC_NAME, certs_row=True):
    """`client`: True for a client certificate and key, "cert-only", or False."""
    certs = {"ca_crt": pki.paths[ca] if ca in pki.paths else ca}
    if target:
        certs["grpc_ssl_credential"] = target
    if client:
        certs["client_crt"] = pki.paths["client.crt"]
    if client is True:
        certs["client_key"] = pki.paths["client.key"]
    dut.topo.grpcclient = {"config": {"type": "secure", "auth_level": auth_level}}
    if certs_row:
        dut.topo.grpcclient["certs"] = certs
    dut.configure(dut.topo)


@pytest.mark.parametrize("auth_level,require_client_auth", [("server", False), ("mutual", True)])
def test_tls(dut, pki, auth_level, require_client_auth):
    """With GRPCCLIENT type secure, ycabled connects to the SoC over TLS, with
    the CA and the SoC's name from GRPCCLIENT, and for mutual authentication
    the client certificate and key too. The port initializes, and its toggles
    go through.

    Cases: auth_level server, with a SoC that does not ask for a client
    certificate, or mutual, with a SoC that requires one.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, and GRPCCLIENT {type: secure, auth_level} with the test
       PKI's CA, the SoC's name and a client certificate and key.
    2. Start a TLS SoC double at 192.168.0.3 with the PKI's server certificate,
       which requires a client certificate for mutual.
    3. Start ycabled.
    4. Expect Ethernet0's STATE_DB state active.
    5. Toggle Ethernet0 to standby, and expect state standby and the SoC's
       admin state of port id 0 off.
    """
    add_cable_aa(dut, soc=False)
    tls_config(dut, pki, auth_level)
    soc = dut.soc(SOC, tls=pki.server_tls(require_client_auth))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.parametrize("case", ["wrong-ca", "server-needs-client-cert", "wrong-target-name"])
def test_tls_handshake_failure(dut, pki, case):
    """A TLS handshake that fails leaves the port unknown, and its toggles are
    answered unknown.

    Cases: the CA in GRPCCLIENT did not sign the SoC's certificate (wrong-ca);
    the SoC requires a client certificate, which auth_level server does not
    present (server-needs-client-cert); grpc_ssl_credential names another host
    than the SoC's certificate (wrong-target-name).

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, and GRPCCLIENT {type: secure, auth_level: server} with
       the case's CA and SoC name.
    2. Start a TLS SoC double at 192.168.0.3, which requires a client
       certificate in the server-needs-client-cert case.
    3. Start ycabled.
    4. Expect Ethernet0's STATE_DB state unknown; toggle it to standby and
       expect unknown.
    """
    add_cable_aa(dut, soc=False)
    tls_config(dut, pki, "server", ca="other-ca.crt" if case == "wrong-ca" else "ca.crt",
               target="other.bb.test" if case == "wrong-target-name" else SOC_NAME)
    dut.soc(SOC, tls=pki.server_tls(require_client_auth=case == "server-needs-client-cert"))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "unknown"
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"


@pytest.mark.parametrize("case", ["missing-ca-file", "no-target-name", "mutual-without-client-cert",
                                  "mutual-without-client-key", "no-certs-row"])
def test_tls_misconfiguration(dut, pki, case):
    """No channel can be set up: the port is reported unknown, and every toggle
    or probe tries to set up the channel again, from CONFIG_DB as it is then.

    Cases: the CA file does not exist (missing-ca-file); no grpc_ssl_credential
    (no-target-name); auth_level mutual without a client certificate
    (mutual-without-client-cert) or without a client key
    (mutual-without-client-key); no certs row in GRPCCLIENT (no-certs-row).

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, and GRPCCLIENT type secure with the case's
       misconfiguration. Start a TLS SoC double at 192.168.0.3.
    2. Start ycabled. On Python ycabled, let the teardown accept the crash of
       its GracefulRestart task (the known issue of
       test_tls_misconfiguration_keeps_running).
    3. Expect the STATE_DB row {state: unknown, read_side: 0, active_side:
       unknown}.
    4. Toggle Ethernet0 to standby and expect state unknown, with only
       xcvrd_switch_self_unknown_start and _end in MUX_METRICS_TABLE. Send a
       forwarding state probe and expect {response: unknown, response_peer:
       unknown}.
    5. Expect no RPC at the SoC.
    6. Write a usable GRPCCLIENT to CONFIG_DB: auth_level server, with the CA,
       the SoC's name and the client certificate and key.
    7. Toggle Ethernet0 to standby, and expect state standby and the SoC's
       admin state of port id 0 off.
    """
    add_cable_aa(dut, soc=False)
    client = {"mutual-without-client-cert": False, "mutual-without-client-key": "cert-only"}.get(case, True)
    tls_config(dut, pki, "mutual" if case.startswith("mutual") else "server",
               ca="/etc/sonic/bbcerts/missing.crt" if case == "missing-ca-file" else "ca.crt",
               client=client, target=None if case == "no-target-name" else SOC_NAME,
               certs_row=case != "no-certs-row")
    soc = dut.soc(SOC, tls=pki.server_tls())
    dut.tolerate_python_failure(crash=True)  # a known issue, see test_tls_misconfiguration_keeps_running
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
    assert set(dut.state.get("MUX_METRICS_TABLE", "Ethernet0")) == {
        "xcvrd_switch_self_unknown_start", "xcvrd_switch_self_unknown_end"}
    assert dut.fwd_probe("Ethernet0") == {"response": "unknown", "response_peer": "unknown"}
    assert not soc.rpcs()
    # Each request sets the channel up again from CONFIG_DB: with a usable
    # configuration, the next toggle goes through.
    dut.config.set("GRPCCLIENT", "config", {"type": "secure", "auth_level": "server"})
    dut.config.set("GRPCCLIENT", "certs", {"ca_crt": pki.paths["ca.crt"], "grpc_ssl_credential": SOC_NAME,
                                           "client_crt": pki.paths["client.crt"],
                                           "client_key": pki.paths["client.key"]})
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.slow
@pytest.mark.known_issue("without a usable TLS channel the GracefulRestart task dies on a None "
                         "channel, and ycabled kills itself within 60 s")
def test_tls_misconfiguration_keeps_running(dut, pki):
    """A TLS configuration with which no channel can be set up leaves the port
    unknown, and nothing more: ycabled is still running after 75 s, past
    Python's 60 s check of its threads.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, and GRPCCLIENT {type: secure, auth_level: server}
       without grpc_ssl_credential. No SoC double.
    2. Start ycabled. On Python ycabled, let the teardown accept the crash and
       the exit of the known issue.
    3. Expect ycabled still running 75 s later.
    """
    add_cable_aa(dut, soc=False)
    tls_config(dut, pki, "server", target=None)
    dut.tolerate_python_failure(crash=True, exit=True)
    dut.start()
    assert dut.proc.wait_exit(75) is None


@pytest.mark.known_issue("auth_level 'client' (the YANG value) is not recognized; only 'mutual' "
                         "enables mutual TLS")
def test_tls_auth_level_client(dut, pki):
    """auth_level client means mutual TLS: ycabled presents the client
    certificate, so a SoC that requires one accepts the connection, and the
    port initializes.

    sonic-grpcclient.yang allows auth_level server or client:
    https://github.com/sonic-net/sonic-buildimage/blob/master/src/sonic-yang-models/yang-models/sonic-grpcclient.yang

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, and GRPCCLIENT {type: secure, auth_level: client} with
       the test PKI's CA, the SoC's name and a client certificate and key.
    2. Start a TLS SoC double at 192.168.0.3 that requires a client
       certificate.
    3. Start ycabled. On Python ycabled, let the teardown accept the crash of
       the known issue.
    4. Expect Ethernet0's STATE_DB state active.
    """
    add_cable_aa(dut, soc=False)
    tls_config(dut, pki, "client")
    dut.soc(SOC, tls=pki.server_tls(require_client_auth=True))
    dut.tolerate_python_failure(crash=True)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


@pytest.mark.parametrize("config", [{"auth_level": "server"}, {"type": "insecure", "auth_level": "server"}],
                         ids=["no-type", "insecure"])
def test_tls_type_insecure_and_missing(dut, config):
    """GRPCCLIENT config without type, or with type insecure, means plaintext:
    ycabled reaches a plaintext SoC, and the port initializes.

    Cases: config {auth_level: server} (no-type) or {type: insecure,
    auth_level: server} (insecure).

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, its SoC
       at 192.168.0.3, with the case's GRPCCLIENT config, and start a plaintext
       SoC double there.
    2. Start ycabled.
    3. Expect Ethernet0's STATE_DB state active, and
       QueryAdminForwardingPortState RPCs at the SoC.
    """
    soc = add_cable_aa(dut)
    dut.topo.grpcclient = {"config": config}
    dut.configure(dut.topo)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert soc.rpcs("QueryAdminForwardingPortState")


@pytest.mark.parametrize("loopback3,tor", [(LOOPBACK3_UPPER, 0), (LOOPBACK3_LOWER, 1)])
def test_graceful_restart_subscription(dut, loopback3, tor):
    """One GracefulRestart stream per SoC. The tor that Python ycabled sends is
    the opposite of the names: the upper ToR (read side 1) sends LOWER_TOR (0),
    and the lower ToR (read side 0) UPPER_TOR (1). Which one the SoC expects is
    not confirmed, so this keeps what Python sends. When the stream ends
    ycabled subscribes again after the `period` of the last response.

    Cases: the upper ToR, which sends tor 0, or the lower ToR, which sends tor
    1.

    Steps:
    1. Configure Ethernet0 as an active-active port with the case's Loopback3
       and a SoC double at 192.168.0.3, whose GracefulRestart streams send one
       response with period 1 s and then end.
    2. Start ycabled.
    3. Wait up to 10 s for two NotifyGracefulRestartStart RPCs at the SoC.
    4. Expect both with the request {tor: 0} on the upper ToR, or {tor: 1} on
       the lower ToR, and the second 0.8 s to 4 s after the first.
    """
    soc = add_cable_aa(dut, loopback3)
    soc.gr_responses = [{"period": 1, "guid": "bb-1", "msgtype": 1, "notifytype": 2}]
    soc.gr_hold = False
    dut.start()
    first, second = soc.wait_rpcs("NotifyGracefulRestartStart", 2, timeout=10)[:2]
    assert first.request == second.request == {"tor": tor}
    assert 0.8 <= second.ts - first.ts < 4


def test_graceful_restart_held_stream(dut):
    """While the SoC holds the GracefulRestart stream open, ycabled does not
    subscribe again.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3, which holds each GracefulRestart
       stream open without a response.
    2. Start ycabled, and wait for its NotifyGracefulRestartStart RPC.
    3. Expect still only one such RPC 3 s later.
    """
    soc = add_cable_aa(dut)
    dut.start()
    soc.wait_rpcs("NotifyGracefulRestartStart", 1)
    time.sleep(3)
    assert len(soc.rpcs("NotifyGracefulRestartStart")) == 1


@pytest.mark.slow
@pytest.mark.parametrize("case", ["no-response", "error"])
def test_graceful_restart_retry_after_20s(dut, case):
    """When a GracefulRestart stream ends without a response, or fails, ycabled
    subscribes again 20 s later.

    Cases: the stream ends without a response (no-response), or the RPC fails
    with UNAVAILABLE (error).

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR (read side
       0), with a SoC double at 192.168.0.3, whose GracefulRestart streams end
       at once or fail.
    2. Start ycabled.
    3. Wait up to 30 s for two NotifyGracefulRestartStart RPCs at the SoC, and
       expect the second 19 s to 25 s after the first.
    """
    soc = add_cable_aa(dut)
    soc.gr_hold = False
    if case == "error":
        soc.script("NotifyGracefulRestartStart", code=grpc.StatusCode.UNAVAILABLE)
    dut.start()
    first, second = soc.wait_rpcs("NotifyGracefulRestartStart", 2, timeout=30)[:2]
    assert 19 <= second.ts - first.ts < 25


def test_graceful_restart_for_every_soc_address(dut):
    """Python subscribes for every MUX_CABLE port with a SoC address, also
    active-standby ones, once any port is active-active. The active-standby
    port's SoC gets only that stream.

    Steps:
    1. Configure Ethernet0 as active-standby with soc_ipv4 192.168.0.3, and
       Ethernet4 as active-active with soc_ipv4 192.168.0.5. Start a SoC
       double at each address.
    2. Start ycabled.
    3. Expect a NotifyGracefulRestartStart RPC at Ethernet0's SoC, and no
       QueryAdminForwardingPortState there.
    """
    topo = Topology().active_standby("Ethernet0", soc_ipv4=soc_ip("Ethernet0") + "/32")
    topo.active_active("Ethernet4", soc_ipv4=soc_ip("Ethernet4") + "/32")
    dut.configure(topo)
    dut.soc(soc_ip("Ethernet4"))
    as_soc = dut.soc(soc_ip("Ethernet0"))
    dut.start()
    as_soc.wait_rpcs("NotifyGracefulRestartStart", 1)
    assert not as_soc.rpcs("QueryAdminForwardingPortState")


@pytest.mark.known_issue("one thread serves every port: a SoC that does not answer delays the "
                         "toggles of the other ports by up to 0.5 s each")
def test_slow_soc_does_not_delay_other_ports(dut):
    """A SoC that does not answer delays only its own port's requests (N1).

    Every port's SoC hangs, so the order the ports are served in does not
    matter: in parallel, all toggles end at the 0.5 s deadline; one after
    another, the last ends after 2 s.

    Steps:
    1. Configure Ethernet0, Ethernet4, Ethernet8 and Ethernet12 as
       active-active, each with its own SoC double, at 192.168.0.3 to
       192.168.0.9.
    2. Start ycabled, then script every SoC's SetAdminForwardingPortState to
       answer after 3 s.
    3. Toggle the four ports to standby in one transaction.
    4. Expect ycabled's STATE_DB HW_MUX_CABLE_TABLE write for every port within
       1.2 s of the request.
    """
    ports = ["Ethernet{}".format(4 * i) for i in range(4)]
    topo = Topology()
    for port in ports:
        topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
    dut.configure(topo)
    socs = [dut.soc(soc_ip(port)) for port in ports]
    dut.start()
    for soc in socs:
        soc.script("SetAdminForwardingPortState", delay=3)
    mark = dut.monitor.mark()
    t0 = time.monotonic()
    with dut.appl.r.pipeline(transaction=True) as p:
        for port in ports:
            p.hset("HW_MUX_CABLE_TABLE:" + port, "state", "standby")
        p.execute()
    for port in ports:
        dut.monitor.wait_access(mark, 10, kind="write", db="STATE_DB", table="HW_MUX_CABLE_TABLE", key=port)
   # ycabled give each gRPC a 0.5s deadline, so the total delay for four
   # ports is 2 s if served sequentially, but in this test we expect
   # parallel handling within a cutoff of 1.2 s.
    assert time.monotonic() - t0 < 1.2


@pytest.mark.known_issue("without a usable Loopback3 address read_side is -1, and toggles still go to "
                         "the SoC, with portid [-1] for this ToR and [2] for the peer")
def test_no_toggle_without_read_side(dut):
    """Without Loopback3, ycabled does not know which ToR it is: it toggles
    neither side on the SoC, and answers unknown.

    Steps:
    1. Configure Ethernet0 as an active-active port with a SoC double at
       192.168.0.3, and no Loopback3.
    2. Start ycabled.
    3. Toggle Ethernet0 to standby and expect state unknown; send a peer toggle
       to standby and expect state unknown.
    4. Expect no SetAdminForwardingPortState RPC at the SoC.
    """
    soc = add_cable_aa(dut, loopback3=None)
    dut.start()
    soc_mark = soc.mark()
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
    assert dut.peer_toggle("Ethernet0", "standby")["state"] == "unknown"
    assert not soc.rpcs("SetAdminForwardingPortState", soc_mark)


@pytest.mark.known_issue("ports are initialized one after another; every unreachable SoC adds 0.5 s")
def test_parallel_startup_with_unreachable_socs(dut):
    """Ports are initialized in parallel (N3): with every SoC unreachable, the
    four ports publish their first state within 1.5 s, where initializing them
    one after another takes the 0.5 s deadline for each.

    Steps:
    1. Configure Ethernet0 to Ethernet12 as active-active, with SoC addresses
       192.168.0.3 to 192.168.0.9, and blackhole each address: it accepts TCP
       and never answers.
    2. Start ycabled without waiting until it is ready.
    3. Take ycabled's first DB access as the start, on redis's clock, so that
       the interpreter's start does not count.
    4. Wait for each port's first STATE_DB HW_MUX_CABLE_TABLE state, and expect
       the last within 1.5 s of the start.
    """
    ports = ["Ethernet{}".format(4 * i) for i in range(4)]
    topo = Topology()
    for port in ports:
        topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
    dut.configure(topo)
    for port in ports:
        dut.blackhole(soc_ip(port))
    dut.start(ready=False)
    # From ycabled's first DB access (redis's clock), so that the
    # interpreter's start does not count, to its last port's first state.
    first = wait_until(lambda: dut.monitor.ops(dut.start_mark), 15)[0]

    def published():
        firsts = {}
        for a in dut.writes(since=dut.start_mark, table="HW_MUX_CABLE_TABLE", field="state"):
            firsts.setdefault(a.key, a)
        return firsts if len(firsts) == 4 else None
    elapsed = max(a.ts for a in wait_until(published, 15).values()) - first.ts
    assert elapsed < 1.5, elapsed
