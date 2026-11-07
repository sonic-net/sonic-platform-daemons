"""What ycabled should publish for a cable, derived from the fake hardware.

These encode the contract (HLD section 3.6.4): which side of the cable is
"self" and which is "peer" for a given read side, and how each driver value
is written to STATE_DB.
"""

from .fake_hw import TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B


def sides(read_side):
    """(self, peer) targets for a ToR that reads the cable from `read_side`."""
    return (TARGET_TOR_A, TARGET_TOR_B) if read_side == TARGET_TOR_A else (TARGET_TOR_B, TARGET_TOR_A)


def _up(value):
    return "up" if value else "down"


def mux_cable_info(model):
    """MUX_CABLE_INFO of an active-standby cable, without time_post."""
    self_t, peer_t = sides(model.read_side)
    active = model.mux_direction if model.active_linked_side is None else model.active_linked_side
    info = {
        "tor_active": "active" if active == model.read_side else "standby",
        "mux_direction": "self" if model.mux_direction == model.read_side else "peer",
        "manual_switch_count": str(model.switch_count["manual"]),
        "auto_switch_count": str(model.switch_count["auto"]),
        "link_status_self": _up(model.link_active[self_t]),
        "link_status_peer": _up(model.link_active[peer_t]),
        "link_status_nic": _up(model.link_active[TARGET_NIC]),
        "internal_temperature": str(model.local_temperature),
        "internal_voltage": str(model.local_voltage),
        "nic_temperature": str(model.nic_temperature),
        "nic_voltage": str(model.nic_voltage),
    }
    for name, target in (("self", self_t), ("peer", peer_t), ("nic", TARGET_NIC)):
        info["{}_eye_height_lane1".format(name)] = str(model.eye_heights[target][0])
        info["{}_eye_height_lane2".format(name)] = str(model.eye_heights[target][1])
        for v in ("active", "inactive", "next"):
            info["version_{}_{}".format(name, v)] = model.firmware[target]["version_" + v]
    return info


# MUX_CABLE_INFO of a port without a usable cable: absent, no driver, or an
# active-active port at initialization.
PLACEHOLDER_INFO = {
    "tor_active": "unknown", "mux_direction": "unknown",
    "manual_switch_count": "N/A", "auto_switch_count": "N/A",
    "link_status_self": "unknown", "link_status_peer": "unknown", "link_status_nic": "unknown",
    "internal_temperature": "N/A", "internal_voltage": "N/A", "nic_temperature": "N/A", "nic_voltage": "N/A",
}
for _name in ("self", "peer", "nic"):
    for _lane in (1, 2):
        PLACEHOLDER_INFO["{}_eye_height_lane{}".format(_name, _lane)] = "N/A"
    for _v in ("active", "inactive", "next"):
        PLACEHOLDER_INFO["version_{}_{}".format(_name, _v)] = "N/A"


def mux_cable_static_info(model):
    self_t, peer_t = sides(model.read_side)
    info = {"read_side": "tor1" if model.read_side == TARGET_TOR_A else "tor2"}
    names = ("precursor1", "precursor2", "maincursor", "postcursor1", "postcursor2")
    for lane in (1, 2):
        for prefix, target in (("nic", TARGET_NIC), ("tor_self", self_t), ("tor_peer", peer_t)):
            for name, value in zip(names, model.cursor_values[(lane, target)]):
                info["{}_lane{}_{}".format(prefix, lane, name)] = str(value)
    return info


def firmware_versions(model):
    """XCVRD_SHOW_FW_RES of `show muxcable firmware version`."""
    self_t, peer_t = sides(model.read_side)
    out = {}
    for name, target in (("self", self_t), ("peer", peer_t), ("nic", TARGET_NIC)):
        for v in ("active", "inactive", "next"):
            out["version_{}_{}".format(name, v)] = model.firmware[target]["version_" + v]
    return out


def subset(row, expected):
    """The fields of `row` that differ from `expected`, for assertion messages."""
    return {k: (row.get(k), v) for k, v in expected.items() if row.get(k) != v}
