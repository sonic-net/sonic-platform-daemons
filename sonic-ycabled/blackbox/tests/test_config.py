"""Configuration inputs: Loopback3, GRPCCLIENT and grpc_secrets.json,
XCVRD_LOG, the VS platform, and syslog compatibility (HLD 3.4.10, 3.5.1; C5,
N5)."""

import pytest

from harness.certs import SOC_NAME, Pki
from harness.dut import GRPC_SECRETS
from harness.redisdb import HARNESS_ADDRESS
from harness.topology import (LOOPBACK3_LOWER, LOOPBACK3_LOWER_NIC, LOOPBACK3_UPPER, LOOPBACK3_UPPER_NIC,
                              VS_PLATFORM, Topology)
from harness.util import eventually

SOC = "127.0.0.2"
CERT_DIR = "/etc/sonic/bbcerts"


@pytest.mark.parametrize("loopback3,read_side", [
    (LOOPBACK3_UPPER, 0), (LOOPBACK3_LOWER, 1), (LOOPBACK3_UPPER_NIC, 0), (LOOPBACK3_LOWER_NIC, 1),
    ("10.212.64.9/32", -1), (None, -1), ("fc00::1/128", -1),
], ids=["upper", "lower", "upper-nic", "lower-nic", "other-address", "none", "ipv6-only"])
def test_read_side_from_loopback3(dut, loopback3, read_side):
    dut.configure(Topology(loopback3=loopback3).active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    soc.admin = [True, False]
    dut.start()
    if read_side == -1:
        self_state = peer_state = "unknown"
    else:
        self_state, peer_state = ("active", "standby") if read_side == 0 else ("standby", "active")
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": self_state, "read_side": str(read_side), "active_side": self_state}
    assert dut.state.get("HW_MUX_CABLE_TABLE_PEER", "Ethernet0") == {
        "state": peer_state, "read_side": str(read_side), "active_side": peer_state}
    assert dut.fwd_probe("Ethernet0") == {"response": self_state, "response_peer": peer_state}
    # GracefulRestart: UPPER_TOR (1) for read side 0, LOWER_TOR (0) otherwise.
    assert soc.wait_rpcs("NotifyGracefulRestartStart")[0].request == {"tor": 1 if read_side == 0 else 0}


@pytest.fixture
def pki(dut):
    p = Pki(CERT_DIR, soc_ips=[SOC])
    dut.files.append(CERT_DIR)
    return p


def secrets(pki, **config):
    return {"GRPCCLIENT": {
        "config": dict({"type": "secure", "auth_level": "server", "log_level": "info"}, **config),
        "certs": {"ca_crt": pki.paths["ca.crt"], "client_crt": pki.paths["client.crt"],
                  "client_key": pki.paths["client.key"], "grpc_ssl_credential": SOC_NAME}}}


def test_grpc_secrets_fill_config_db(dut, pki):
    """/etc/sonic/grpc_secrets.json fills GRPCCLIENT in CONFIG_DB, and the
    channel uses it."""
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    dut.write_file(GRPC_SECRETS, secrets(pki))
    soc = dut.soc(SOC, tls=pki.server_tls())
    dut.start()
    assert dut.config.get("GRPCCLIENT", "config") == secrets(pki)["GRPCCLIENT"]["config"]
    assert dut.config.get("GRPCCLIENT", "certs") == secrets(pki)["GRPCCLIENT"]["certs"]
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert soc.rpcs("QueryAdminForwardingPortState")


def test_grpc_secrets_do_not_override_config_db(dut, pki):
    """Only fields CONFIG_DB lacks are copied; null values are skipped."""
    topo = Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32")
    topo.grpcclient = {"config": {"type": "insecure"}}
    dut.configure(topo)
    data = secrets(pki, log_level=None)
    dut.write_file(GRPC_SECRETS, data)
    dut.soc(SOC)
    dut.start()
    assert dut.config.get("GRPCCLIENT", "config") == {"type": "insecure", "auth_level": "server"}
    assert dut.config.get("GRPCCLIENT", "certs") == data["GRPCCLIENT"]["certs"]
    # Still plaintext.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    writes = {(a.key, a.field) for a in dut.writes(table="GRPCCLIENT")}
    assert ("config", "type") not in writes and ("config", "log_level") not in writes


def test_grpc_secrets_with_empty_section(dut, pki):
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    data = secrets(pki)
    data["GRPCCLIENT"]["config"] = {}
    dut.write_file(GRPC_SECRETS, data)
    dut.soc(SOC)
    dut.start()
    assert not dut.config.exists("GRPCCLIENT", "config")
    assert dut.config.get("GRPCCLIENT", "certs") == data["GRPCCLIENT"]["certs"]
    # No config row: plaintext.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


def test_grpc_secrets_without_active_active_ports(dut, pki):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.write_file(GRPC_SECRETS, secrets(pki))
    dut.start()
    assert dut.config.get("GRPCCLIENT", "certs") == secrets(pki)["GRPCCLIENT"]["certs"]


def set_log_verbosity(dut, key, level):
    """Set XCVRD_LOG|<key> log_verbosity, then wait until ycabled has read
    the entry, if it reads it at all, and has gone quiet."""
    mark = dut.monitor.mark()
    dut.config.set("XCVRD_LOG", key, {"log_verbosity": level})
    eventually(lambda: dut.monitor.accesses(mark, kind="read", db="CONFIG_DB", table="XCVRD_LOG", key=key), 3)
    dut.monitor.wait_settled()


def probe_logs_debug(dut):
    """Whether a probe logs at DEBUG. ycabled logs the probe before it
    answers, and the syslog barrier makes sure those lines have arrived."""
    mark = dut.syslog.mark()
    dut.probe("Ethernet0")
    dut.syslog.barrier()
    return [m.text for m in dut.syslog.messages(mark, severity="DEBUG")]


def test_log_verbosity(dut):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    assert not probe_logs_debug(dut)
    set_log_verbosity(dut, "Y_CABLE", "debug")
    assert any("received a probe for port status Ethernet0" in t for t in probe_logs_debug(dut))
    set_log_verbosity(dut, "Y_CABLE", "notice")
    assert not probe_logs_debug(dut)


@pytest.mark.known_issue("3.8-25", "the fields of any XCVRD_LOG key, not only Y_CABLE, set the log level and "
                                    "the telemetry flag")
def test_log_verbosity_other_key_is_ignored(dut):
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    set_log_verbosity(dut, "OTHER", "debug")
    assert not probe_logs_debug(dut)


def test_active_active_on_vs(dut):
    """VS: no platform API; active-active cables are always present."""
    dut.configure(Topology(platform=VS_PLATFORM).active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    dut.soc(SOC)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert not dut.hw.calls(op=None)


# sonic-mgmt's loganalyzer ignores these ERR messages by text, so they must
# keep it (loganalyzer_common_ignore.txt). The identity part of those rules is
# covered by test_lifecycle.py::test_syslog_identity.
def test_messages_matched_by_sonic_mgmt(dut):
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    dut.hw.port(1).transceiver_info = {"manufacturer": "Nobody", "model": "X1"}
    dut.start()
    msgs = dut.logs(regex=r"Error: Could not get port instance for muxcable info for Y cable port Ethernet")
    assert msgs and {m.severity for m in msgs} == {"ERR"}


@pytest.mark.known_issue("3.8-13", "redis connections grow with the number of active-active ports")
def test_redis_connections_bounded(dut):
    topo = Topology(nports=16)
    for i in range(16):
        topo.active_active("Ethernet{}".format(4 * i), soc_ipv4="127.0.2.{}/32".format(i + 1))
    dut.configure(topo)
    for i in range(16):
        dut.soc("127.0.2.{}".format(i + 1))
    dut.start(timeout=60)
    clients = [c for c in dut.config.r.client_list() if not c["addr"].startswith(HARNESS_ADDRESS + ":")]
    assert len(clients) <= 48, len(clients)
