"""ycabled's contract with the SONiC databases.

Every DB access ycabled makes must match an entry here. Each test fails if
ycabled writes or deletes something the contract does not allow, or writes a
value outside the field's domain. At the end of a run, `coverage_report`
shows which entries the suite exercised.

Field names may be exact, "*" (any field), or "re:<regex>". Tables are
hashes, so a string value (SET) matches no entry. A field's values may
depend on the port's cable type: pairs (cable type, pattern).

Stopping ycabled deletes nothing (HLD C7). This departs on purpose from what
Python's deinit() intends: it would delete the HW_MUX_CABLE_TABLE,
MUX_CABLE_STATIC_INFO and MUX_CABLE_INFO rows and the GRPCCLIENT keys it
copied, but it never runs, because SIGTERM never completes (HLD 3.8 issue 1).
"""

import re
from collections import namedtuple

from .util import TIMESTAMP_RE

Entry = namedtuple("Entry", "db table kind field values note")

ANY = None
STATE = r"^(active|standby|unknown)$"
# HW_MUX_CABLE_TABLE and HW_MUX_CABLE_TABLE_PEER. An active-standby port has
# the cable's sides: ToR A 1, ToR B 2, the NIC 0, or -1. An active-active port
# has this ToR's index in the SoC's lists (0 or 1, -1 without Loopback3) and
# the SoC's states. A removal writes active_side -1 for both.
# Pairs, not a dict: entries are hashable.
READ_SIDE = (("active-standby", r"^(-1|0|1|2)$"), ("active-active", r"^(-1|0|1)$"))
ACTIVE_SIDE = (("active-standby", r"^(-1|0|1|2)$"), ("active-active", r"^(-1|active|standby|unknown)$"))
UP_DOWN = r"^(up|down|unknown)$"
# Driver values are written with Python's str(), so a driver's None is "None".
NUM_OR_NA = r"^(-?\d+(\.\d+)?(e-?\d+)?|N/A|None)$"
COUNT = r"^(\d+|N/A|unknown)$"
CLI_STATUS = r"^(True|False|-?\d+)$"

MUX_CABLE_INFO_ACTIVE_STANDBY = {
    "tor_active": STATE,
    "time_post": TIMESTAMP_RE,
    "mux_direction": r"^(self|peer|unknown)$",
    "manual_switch_count": COUNT,
    "auto_switch_count": COUNT,
    "link_status_self": UP_DOWN,
    "link_status_peer": UP_DOWN,
    "link_status_nic": UP_DOWN,
    "self_eye_height_lane1": NUM_OR_NA, "self_eye_height_lane2": NUM_OR_NA,
    "peer_eye_height_lane1": NUM_OR_NA, "peer_eye_height_lane2": NUM_OR_NA,
    "nic_eye_height_lane1": NUM_OR_NA, "nic_eye_height_lane2": NUM_OR_NA,
    "internal_temperature": NUM_OR_NA,
    "internal_voltage": NUM_OR_NA,
    "nic_temperature": NUM_OR_NA,
    "nic_voltage": NUM_OR_NA,
}
for _side in ("self", "peer", "nic"):
    for _v in ("active", "inactive", "next"):
        MUX_CABLE_INFO_ACTIVE_STANDBY["version_{}_{}".format(_side, _v)] = ANY

MUX_CABLE_INFO_ACTIVE_ACTIVE = {
    "time_post": TIMESTAMP_RE,
    "self_link_state": UP_DOWN, "peer_link_state": UP_DOWN,
    "self_oper_state": UP_DOWN, "peer_oper_state": UP_DOWN,
    "self_mux_direction": STATE, "peer_mux_direction": STATE,
    "server_version": ANY,
    "grpc_connection_status": r"^(READY|CONNECTING|IDLE|TRANSIENT_FAILURE|SHUTDOWN|unknown)$",
    "mux_direction_probe_count": COUNT, "peer_mux_direction_probe_count": COUNT,
    "link_state_probe_count": COUNT, "peer_link_state_probe_count": COUNT,
    "operation_state_probe_count": COUNT, "peer_operation_state_probe_count": COUNT,
}

MUX_CABLE_STATIC_INFO = {"read_side": r"^(tor1|tor2|-1)$"}
for _t in ("nic", "tor_self", "tor_peer"):
    for _lane in (1, 2):
        for _c in ("precursor1", "precursor2", "maincursor", "postcursor1", "postcursor2"):
            MUX_CABLE_STATIC_INFO["{}_lane{}_{}".format(_t, _lane, _c)] = NUM_OR_NA

FIRMWARE_FIELDS = ["version_{}_{}".format(s, v) for s in ("self", "peer", "nic")
                   for v in ("active", "inactive", "next")]

# CLI command tables: (command table, argument table, response table,
# response field, response values, result table, result fields).
CLI_TABLES = [
    ("XCVRD_SHOW_HWMODE_DIR_CMD", None, "XCVRD_SHOW_HWMODE_DIR_RSP", "state",
     r"^(active|standby|unknown|not Y-Cable port)$", "XCVRD_SHOW_HWMODE_DIR_RES", {"presence": r"^(True|False)$"}),
    ("XCVRD_CONFIG_HWMODE_DIR_CMD", None, "XCVRD_CONFIG_HWMODE_DIR_RSP", "result", CLI_STATUS, None, None),
    ("XCVRD_SHOW_HWMODE_SWMODE_CMD", None, "XCVRD_SHOW_HWMODE_SWMODE_RSP", "state",
     r"^(auto|manual|unknown|cable not present|not Y-Cable port)$", None, None),
    ("XCVRD_CONFIG_HWMODE_SWMODE_CMD", None, "XCVRD_CONFIG_HWMODE_SWMODE_RSP", "result", CLI_STATUS, None, None),
    ("XCVRD_SHOW_FW_CMD", None, "XCVRD_SHOW_FW_RSP", "status", CLI_STATUS, "XCVRD_SHOW_FW_RES",
     {f: ANY for f in FIRMWARE_FIELDS}),
    ("XCVRD_DOWN_FW_CMD", None, "XCVRD_DOWN_FW_RSP", "status", CLI_STATUS, None, None),
    ("XCVRD_ACTI_FW_CMD", "XCVRD_ACTI_FW_CMD_ARG", "XCVRD_ACTI_FW_RSP", "status", CLI_STATUS, None, None),
    ("XCVRD_ROLL_FW_CMD", None, "XCVRD_ROLL_FW_RSP", "status", CLI_STATUS, None, None),
    ("XCVRD_CONFIG_PRBS_CMD", "XCVRD_CONFIG_PRBS_CMD_ARG", "XCVRD_CONFIG_PRBS_RSP", "status", CLI_STATUS, None, None),
    ("XCVRD_CONFIG_LOOP_CMD", "XCVRD_CONFIG_LOOP_CMD_ARG", "XCVRD_CONFIG_LOOP_RSP", "status", CLI_STATUS, None, None),
    ("XCVRD_EVENT_LOG_CMD", None, "XCVRD_EVENT_LOG_RSP", "status", CLI_STATUS, "XCVRD_EVENT_LOG_RES",
     {"re:^\\d+$": ANY}),
    ("XCVRD_GET_FEC_CMD", None, "XCVRD_GET_FEC_RSP", "status", CLI_STATUS, "XCVRD_GET_FEC_RES",
     {f: ANY for f in ("fec_nic", "fec_tor_a", "fec_tor_b", "speed", "anlt_nic", "anlt_tor_a", "anlt_tor_b")}),
    ("XCVRD_GET_BER_CMD", "XCVRD_GET_BER_CMD_ARG", "XCVRD_GET_BER_RSP", "status", CLI_STATUS, "XCVRD_GET_BER_RES",
     {"*": ANY}),
]


def _entries():
    e = []

    def add(db, table, kind, field=None, values=ANY, note=""):
        e.append(Entry(db, table, kind, field, values, note))

    # CONFIG_DB
    add("CONFIG_DB", "CONFIG_DB_INITIALIZED", "read", note="sonic_py_common waits for config load")
    add("CONFIG_DB", "CONFIG_DB_INITIALIZED", "subscribe", note="only while CONFIG_DB is not initialized")
    add("CONFIG_DB", "DEVICE_METADATA", "read")
    add("CONFIG_DB", "PORT", "read", note="port map; port_config.ini only if PORT is empty")
    add("CONFIG_DB", "MUX_CABLE", "read")
    add("CONFIG_DB", "LOOPBACK_INTERFACE", "read")
    add("CONFIG_DB", "GRPCCLIENT", "read")
    add("CONFIG_DB", "GRPCCLIENT", "write", "*", note="fields of /etc/sonic/grpc_secrets.json that CONFIG_DB lacks")
    add("CONFIG_DB", "XCVRD_LOG", "read")
    add("CONFIG_DB", "XCVRD_LOG", "subscribe")
    add("CONFIG_DB", "XCVRD_LOG", "write", "log_verbosity", r"^notice$", "reset to notice at every start")

    # STATE_DB
    add("STATE_DB", "TRANSCEIVER_INFO", "subscribe")
    add("STATE_DB", "TRANSCEIVER_INFO", "read")
    add("STATE_DB", "HW_MUX_CABLE_TABLE", "read")
    add("STATE_DB", "HW_MUX_CABLE_TABLE_PEER", "read", note="read_side for peer toggles")
    for table in ("HW_MUX_CABLE_TABLE", "HW_MUX_CABLE_TABLE_PEER"):
        add("STATE_DB", table, "write", "state", STATE)
        add("STATE_DB", table, "write", "read_side", READ_SIDE)
        add("STATE_DB", table, "write", "active_side", ACTIVE_SIDE)
    add("STATE_DB", "MUX_CABLE_INFO", "read", note="last known firmware versions during a download")
    for field, values in MUX_CABLE_INFO_ACTIVE_STANDBY.items():
        add("STATE_DB", "MUX_CABLE_INFO", "write", field, values, "active-standby")
    for field, values in MUX_CABLE_INFO_ACTIVE_ACTIVE.items():
        if field != "time_post":
            add("STATE_DB", "MUX_CABLE_INFO", "write", field, values, "active-active")
    for field, values in MUX_CABLE_STATIC_INFO.items():
        add("STATE_DB", "MUX_CABLE_STATIC_INFO", "write", field, values)
    for side in ("", "self_", "peer_"):
        for state in ("active", "standby", "unknown"):
            for edge in ("start", "end"):
                add("STATE_DB", "MUX_METRICS_TABLE", "write",
                    "xcvrd_switch_{}{}_{}".format(side, state, edge), TIMESTAMP_RE)

    # APPL_DB
    for table in ("HW_MUX_CABLE_TABLE", "MUX_CABLE_COMMAND_TABLE", "FORWARDING_STATE_COMMAND",
                  "HW_FORWARDING_STATE_PEER"):
        add("APPL_DB", table, "subscribe")
        add("APPL_DB", table, "read")
    add("APPL_DB", "MUX_CABLE_RESPONSE_TABLE", "write", "response", STATE)
    add("APPL_DB", "FORWARDING_STATE_RESPONSE", "write", "response", r"^(active|standby|unknown|failure)$")
    add("APPL_DB", "FORWARDING_STATE_RESPONSE", "write", "response_peer", STATE)

    # CLI
    add("APPL_DB", "XCVRD_DOWN_FW_STATUS_CMD", "subscribe", note="subscribed, never used")
    add("APPL_DB", "XCVRD_DOWN_FW_STATUS_CMD", "read", note="the subscription's initial read")
    for cmd, arg, rsp, rsp_field, rsp_values, res, res_fields in CLI_TABLES:
        add("APPL_DB", cmd, "subscribe")
        add("APPL_DB", cmd, "read")
        add("APPL_DB", cmd, "delete", note="after the response is written")
        if arg:
            add("APPL_DB", arg, "read")
        add("STATE_DB", rsp, "write", rsp_field, rsp_values)
        for field, values in (res_fields or {}).items():
            add("STATE_DB", res, "write", field, values)
    return e


CONTRACT = _entries()


def _field_matches(pattern, field):
    if pattern is None or pattern == "*":
        return True
    if pattern.startswith("re:"):
        return field is not None and re.fullmatch(pattern[3:], field) is not None
    return pattern == field


def match(access):
    """Contract entries the access falls under."""
    out = []
    for e in CONTRACT:
        if e.db != access.db or e.table != access.table or e.kind != access.kind:
            continue
        if e.kind == "write" and not _field_matches(e.field, access.field):
            continue
        out.append(e)
    return out


def _domain(values, cable_type):
    if not isinstance(values, tuple):
        return values
    by_type = dict(values)
    if cable_type in by_type:
        return by_type[cable_type]
    # A port the test did not configure: any cable type's values.
    return "|".join("(?:{})".format(v) for v in by_type.values())


def violations(accesses, cable_types=None):
    """Writes, deletes and field removals the contract does not allow, and
    written values outside their domain. `cable_types`: port name to cable
    type, for the fields whose values depend on it."""
    cable_types = cable_types or {}
    out = []
    for a in accesses:
        if a.kind.startswith("other:"):
            out.append("unexpected command {} on {} {}".format(a.kind[6:], a.db, a.table))
            continue
        # "set": a string value where SONiC keeps hashes; no entry allows one.
        if a.kind not in ("write", "set", "delete", "hdel"):
            continue
        entries = match(a)
        if not entries:
            out.append("{} {} {} key={!r} field={!r} value={!r} is not in the contract".format(
                a.kind, a.db, a.table, a.key, a.field, a.value))
            continue
        domain = _domain(entries[0].values, cable_types.get(a.key))
        # fullmatch: with re.match, "$" also matches before a final newline.
        if a.kind == "write" and domain is not None and (a.value is None or not re.fullmatch(domain, a.value)):
            out.append("{} {}|{} {}={!r} is outside {}".format(a.db, a.table, a.key, a.field, a.value, domain))
    return out


def coverage(accesses):
    """For each contract entry, how often the suite saw ycabled make it."""
    hits = {e: 0 for e in CONTRACT}
    unexpected_reads = {}
    for a in accesses:
        entries = match(a)
        for e in entries:
            hits[e] += 1
        if not entries and a.kind in ("read", "subscribe"):
            key = (a.db, a.table, a.kind)
            unexpected_reads[key] = unexpected_reads.get(key, 0) + 1
    return hits, unexpected_reads


def coverage_report(accesses):
    hits, unexpected_reads = coverage(accesses)
    covered = [e for e, n in hits.items() if n]
    missing = [e for e, n in hits.items() if not n]
    lines = ["# ycabled DB contract coverage", "",
             "{} of {} contract entries exercised.".format(len(covered), len(CONTRACT)), ""]
    lines += ["| DB | Table | Access | Field | Seen | Note |", "| --- | --- | --- | --- | ---: | --- |"]
    for e in CONTRACT:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            e.db, e.table, e.kind, e.field or "", hits[e], e.note))
    if missing:
        lines += ["", "## Not exercised", ""]
        lines += ["- {} {} {} {}".format(e.db, e.table, e.kind, e.field or "") for e in missing]
    if unexpected_reads:
        lines += ["", "## Reads and subscriptions outside the contract", ""]
        lines += ["- {} {} {} ({})".format(db, table, kind, n)
                  for (db, table, kind), n in sorted(unexpected_reads.items())]
    return "\n".join(lines) + "\n", missing
