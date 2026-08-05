"""
xcvrd's schema for the shared pmon daemon configuration resolver.

The mechanism - locating pmon_daemon_control.json, extracting a daemon's
section, layering it over the built-in defaults, coercing types, validating
ranges, and degrading safely on any error - lives in
sonic_py_common.pmon_daemon_config and is shared with the other pmon daemons.
This module only declares what xcvrd accepts.

Precedence, highest wins:
  1. Per-platform / per-hwsku file - the "xcvrd" section of pmon_daemon_control.json
  2. Built-in defaults            - the dataclass field defaults below

The per-platform file is read from the same device directories (and with the
same hwsku-over-platform precedence) that docker_init.j2 uses and that the
existing media_settings.json / optics_si_settings.json parsers already read.

A new tunable is added by declaring one field on XcvrdConfig plus one
_FIELD_SPECS entry giving its type coercion and valid range. Platform owners set
it in the "xcvrd" section they already maintain; no template, argparse, or
constructor change.
"""

from dataclasses import dataclass
from typing import Optional

from sonic_py_common.pmon_daemon_config import FieldSpec, PmonDaemonConfig

XCVRD_SECTION = "xcvrd"

# Shared upper bound for the cadence tunables. A poll interval longer than a day
# is operationally indistinguishable from "disabled" and is far more likely a
# units mistake (milliseconds entered where seconds are expected) than an intent,
# so it is rejected rather than obeyed. Not a functional limit.
MAX_INTERVAL_SECS = 86400

# Coercion and validation applied to file values before they are stored. JSON
# numbers already arrive as the right type; the caster guards against a value
# given as a string (e.g. "20") and mirrors the int parsing the old --flag
# arguments did. The bounds then reject values that coerce cleanly but are not
# valid configuration - notably a negative cadence, which DomThermalInfoUpdateTask
# would otherwise turn into an undelayed poll loop. A rejected value keeps the
# built-in default and logs a warning; it never stops xcvrd from starting. None
# values are never coerced or stored - they mean "no override".
_FIELD_SPECS = {
    'dom_temperature_poll_interval': FieldSpec(caster=int, minimum=0, maximum=MAX_INTERVAL_SECS),
    'dom_update_interval': FieldSpec(caster=int, minimum=0, maximum=MAX_INTERVAL_SECS),
}


@dataclass
class XcvrdConfig(PmonDaemonConfig):
    SECTION_NAME = XCVRD_SECTION
    FIELD_SPECS = _FIELD_SPECS

    # Built-in defaults (lowest precedence). None is meaningful and must be
    # preserved: downstream a None dom_temperature_poll_interval disables the
    # thermal poll thread, and a None dom_update_interval lets DomInfoUpdateTask
    # fall back to its own DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS.
    dom_temperature_poll_interval: Optional[int] = None
    dom_update_interval: Optional[int] = None
