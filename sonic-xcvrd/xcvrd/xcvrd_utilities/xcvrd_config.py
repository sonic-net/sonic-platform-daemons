"""
Resolves xcvrd runtime tunables from the per-platform daemon control file so
that adding a new knob no longer requires threading a new command-line flag
through the supervisord template, argparse, and the daemon constructor.

Precedence, highest wins:
  1. Per-platform / per-hwsku file - the "xcvrd" section of pmon_daemon_control.json
  2. Built-in defaults            - the dataclass field defaults below

The per-platform file is read from the same device directories (and with the
same hwsku-over-platform precedence) that docker_init.j2 uses and that the
existing media_settings.json / optics_si_settings.json parsers already read.

A new tunable is added by declaring one field on XcvrdConfig (and, if it needs
type coercion, one entry in _FIELD_CASTERS). Platform owners set it in the
"xcvrd" section they already maintain; no template or constructor change.
"""

import json
import os

from dataclasses import dataclass, fields
from typing import Optional

from sonic_py_common import device_info, syslogger

SYSLOG_IDENTIFIER = "xcvrd_config"
helper_logger = syslogger.SysLogger(SYSLOG_IDENTIFIER, enable_runtime_config=True)

# Per-platform daemon control file; xcvrd tunables live under the "xcvrd" key.
PMON_DAEMON_CONTROL_FILE = "pmon_daemon_control.json"
XCVRD_SECTION = "xcvrd"

# Coercion applied to file values before they are stored. JSON numbers already
# arrive as the right type; this guards against a value given as a string
# (e.g. "20") and mirrors the int parsing the old --flag arguments did. None
# values are never coerced or stored - they mean "no override".
_FIELD_CASTERS = {
    'dom_temperature_poll_interval': int,
    'dom_update_interval': int,
}


@dataclass
class XcvrdConfig:
    # Built-in defaults (lowest precedence). None is meaningful and must be
    # preserved: downstream a None dom_temperature_poll_interval disables the
    # thermal poll thread, and a None dom_update_interval lets DomInfoUpdateTask
    # fall back to its own DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS.
    dom_temperature_poll_interval: Optional[int] = None
    dom_update_interval: Optional[int] = None

    @classmethod
    def resolve(cls, platform_section=None):
        """Build a config by layering the platform file over the built-in defaults.

        platform_section is exposed for tests so the merge logic can be exercised
        without touching the filesystem; in production it is read from disk.
        """
        cfg = cls()
        if platform_section is None:
            platform_section = cls._read_platform_section()
        cfg._merge(platform_section)
        return cfg

    def _merge(self, overrides):
        valid = {f.name for f in fields(self)}
        for key, value in overrides.items():
            if key not in valid:
                helper_logger.log_notice(
                    "xcvrd config: ignoring unknown key '{}' in {}".format(
                        key, PMON_DAEMON_CONTROL_FILE))
                continue
            if value is None:
                # An absent override never clobbers the default.
                continue
            caster = _FIELD_CASTERS.get(key)
            if caster is not None:
                try:
                    value = caster(value)
                except (TypeError, ValueError):
                    helper_logger.log_warning(
                        "xcvrd config: invalid value {!r} for '{}' in {}; keeping "
                        "default".format(value, key, PMON_DAEMON_CONTROL_FILE))
                    continue
            setattr(self, key, value)

    @staticmethod
    def _read_platform_section():
        """Return the "xcvrd" dict from pmon_daemon_control.json, or {} if absent.

        Mirrors docker_init.j2: the hwsku file takes precedence over the platform
        file, and only the first existing file is consulted (no cross-file merge).
        Any failure degrades to {} so xcvrd always starts on its built-in defaults.
        """
        try:
            platform_path, hwsku_path = device_info.get_paths_to_platform_and_hwsku_dirs()
        except Exception as exc:  # device_info can raise if platform is undetermined
            helper_logger.log_warning(
                "xcvrd config: unable to determine platform/hwsku dirs: {}".format(exc))
            return {}

        for directory in (hwsku_path, platform_path):
            if not directory:
                continue
            path = os.path.join(directory, PMON_DAEMON_CONTROL_FILE)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as control_file:
                    data = json.load(control_file)
            except (OSError, ValueError) as exc:
                helper_logger.log_warning(
                    "xcvrd config: failed to read {}: {}".format(path, exc))
                return {}
            section = data.get(XCVRD_SECTION, {})
            if not isinstance(section, dict):
                helper_logger.log_warning(
                    "xcvrd config: '{}' section in {} is not an object; ignoring".format(
                        XCVRD_SECTION, path))
                return {}
            return section
        return {}
