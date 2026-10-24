"""
xcvrd's schema for the shared pmon daemon configuration resolver.

The mechanism - locating pmon_daemon_control.json, extracting a daemon's
section, layering it over the built-in defaults, coercing types, validating
ranges, and degrading safely on any error - lives in
sonic_py_common.pmon_daemon_config and is shared with the other pmon daemons.
This module only declares what xcvrd accepts.

Precedence, highest wins:
  1. Nested keys in the "xcvrd" section of pmon_daemon_control.json
     (e.g. xcvrd.dom.update_interval, xcvrd.cmis_mgr.enabled)
  2. Legacy aliases - the deprecated flat dom_* keys and the top-level
     skip_xcvrd_cmis_mgr / enable_xcvrd_sff_mgr keys, honored for a
     compatibility window with a deprecation warning
  3. Built-in defaults - the dataclass field defaults below

The per-platform file is read from the same device directories (and with the
same hwsku-over-platform precedence) that docker_init.j2 uses and that the
existing media_settings.json / optics_si_settings.json parsers already read.

xcvrd's tunables are grouped into one-level-deep subsections: dom holds the
cadence tunables and cmis_mgr / sff_mgr / cpo_mgr each hold a single enabled
toggle. A new tunable is added by declaring one field on the relevant subsection
schema plus one FIELD_SPECS entry giving its coercion and valid range; platform
owners set it in the "xcvrd" section they already maintain, with no template,
argparse, or constructor change.
"""

from dataclasses import dataclass, field
from typing import Optional

from sonic_py_common.pmon_daemon_config import (
    FieldSpec, LegacyAlias, PmonDaemonConfig, to_bool)

XCVRD_SECTION = "xcvrd"

# Shared upper bound for the cadence tunables. A poll interval longer than a day
# is operationally indistinguishable from "disabled" and is far more likely a
# units mistake (milliseconds entered where seconds are expected) than an intent,
# so it is rejected rather than obeyed. Not a functional limit.
MAX_INTERVAL_SECS = 86400


@dataclass
class DomConfig(PmonDaemonConfig):
    """The "dom" subsection: transceiver DOM polling cadence tunables.

    None is meaningful and must be preserved: a None temperature_poll_interval
    disables the DOM thermal poll thread, and a None update_interval lets
    DomInfoUpdateTask fall back to its own DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS.
    The bounds reject values that coerce cleanly but are not valid configuration
    - notably a negative cadence, which DomThermalInfoUpdateTask would otherwise
    turn into an undelayed poll loop.
    """

    SECTION_NAME = 'dom'
    FIELD_SPECS = {
        'temperature_poll_interval': FieldSpec(caster=int, minimum=0, maximum=MAX_INTERVAL_SECS),
        'update_interval': FieldSpec(caster=int, minimum=0, maximum=MAX_INTERVAL_SECS),
    }

    temperature_poll_interval: Optional[int] = None
    update_interval: Optional[int] = None


@dataclass
class MgrConfig(PmonDaemonConfig):
    """Reused for cmis_mgr / sff_mgr / cpo_mgr - a single enabled toggle.

    to_bool (not bool) is the caster because bool("false") is True; a platform
    writing the string "false" must disable, not silently enable, the manager.
    """

    FIELD_SPECS = {'enabled': FieldSpec(caster=to_bool)}

    enabled: Optional[bool] = None


@dataclass
class XcvrdConfig(PmonDaemonConfig):
    SECTION_NAME = XCVRD_SECTION
    SUBSECTIONS = {
        'dom': DomConfig,
        'cmis_mgr': MgrConfig,
        'sff_mgr': MgrConfig,
        'cpo_mgr': MgrConfig,
    }
    LEGACY_ALIASES = {
        # Flat dom_* keys that used to live directly in the xcvrd section. The
        # `v or None` transform reproduces the old Jinja truthy gate
        # ({% if xcvrd.dom_update_interval %}) exactly: a flat 0 (or empty
        # value) was never emitted as a flag, so the daemon fell back to its
        # default. Mapping falsy -> None keeps that parity for the deprecated
        # flat form, while the nested dom.* form treats 0 as a real value
        # (continuous polling), the intended new semantics.
        'dom_temperature_poll_interval': LegacyAlias('dom.temperature_poll_interval',
                                                     transform=lambda v: v or None),
        'dom_update_interval': LegacyAlias('dom.update_interval',
                                           transform=lambda v: v or None),
        # Top-level capability keys (scope='file'); skip_* inverts to enabled.
        'skip_xcvrd_cmis_mgr': LegacyAlias('cmis_mgr.enabled', scope='file',
                                           transform=lambda v: not to_bool(v)),
        'enable_xcvrd_sff_mgr': LegacyAlias('sff_mgr.enabled', scope='file'),
    }

    # Built-in defaults (lowest precedence) preserve today's behavior for a
    # platform that overrides nothing: CMIS and CPO managers enabled, SFF
    # manager disabled.
    dom: DomConfig = field(default_factory=DomConfig)
    cmis_mgr: MgrConfig = field(default_factory=lambda: MgrConfig(enabled=True))
    sff_mgr: MgrConfig = field(default_factory=lambda: MgrConfig(enabled=False))
    cpo_mgr: MgrConfig = field(default_factory=lambda: MgrConfig(enabled=True))
