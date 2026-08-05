"""
Unit tests for XcvrdConfig: the layered resolver for xcvrd's dom_* tunables.

Precedence under test (highest wins):
  1. "xcvrd" section of pmon_daemon_control.json (hwsku file over platform file)
  2. built-in dataclass defaults
"""
import json
import os
import sys

from unittest.mock import patch

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
sys.path.insert(0, modules_path)

from sonic_py_common.pmon_daemon_config import PMON_DAEMON_CONTROL_FILE
from xcvrd.xcvrd_utilities.xcvrd_config import XcvrdConfig, MAX_INTERVAL_SECS

# Path patched in _read_platform_section's module so no real device dir is
# touched. The read lives in the shared base, not in xcvrd's schema module.
PATHS_FN = "sonic_py_common.pmon_daemon_config.device_info.get_paths_to_platform_and_hwsku_dirs"


def write_control_file(directory, payload):
    """Write a pmon_daemon_control.json with the given dict into directory."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, PMON_DAEMON_CONTROL_FILE)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


class TestXcvrdConfigDefaults:
    def test_defaults_when_no_overrides(self):
        cfg = XcvrdConfig.resolve(platform_section={})
        assert cfg.dom_temperature_poll_interval is None
        assert cfg.dom_update_interval is None

    def test_bare_construction_matches_defaults(self):
        # Legacy path: DaemonXcvrd builds XcvrdConfig() directly.
        cfg = XcvrdConfig()
        assert cfg.dom_temperature_poll_interval is None
        assert cfg.dom_update_interval is None


class TestXcvrdConfigMerge:
    def test_platform_section_overrides_defaults(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_temperature_poll_interval": 5,
            "dom_update_interval": 30,
        })
        assert cfg.dom_temperature_poll_interval == 5
        assert cfg.dom_update_interval == 30

    def test_partial_section_leaves_other_field_at_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": 30})
        assert cfg.dom_update_interval == 30
        assert cfg.dom_temperature_poll_interval is None

    def test_none_value_does_not_override(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": None})
        assert cfg.dom_update_interval is None

    def test_zero_is_preserved(self):
        # 0 is a meaningful value (continuous polling) and must not be dropped.
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": 0})
        assert cfg.dom_update_interval == 0

    def test_string_value_is_coerced_to_int(self):
        # JSON could carry a stringified number; mirror the old argparse type=int.
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": "30"})
        assert cfg.dom_update_interval == 30
        assert isinstance(cfg.dom_update_interval, int)

    def test_invalid_value_is_ignored_and_keeps_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": "not-a-number"})
        assert cfg.dom_update_interval is None

    def test_unknown_key_is_ignored(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_update_interval": 30,
            "some_future_unknown_key": 99,
        })
        assert cfg.dom_update_interval == 30
        assert not hasattr(cfg, "some_future_unknown_key")


class TestReadPlatformSection:
    def test_missing_files_yield_empty(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {}

    def test_reads_platform_file_when_no_hwsku_file(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {"dom_update_interval": 30}})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {"dom_update_interval": 30}

    def test_hwsku_file_takes_precedence_over_platform_file(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {"dom_update_interval": 30}})
        write_control_file(hwsku_dir, {"xcvrd": {"dom_update_interval": 99}})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            # Mirrors docker_init: the hwsku file wins; no cross-file merge.
            assert XcvrdConfig._read_platform_section() == {"dom_update_interval": 99}

    def test_hwsku_file_without_xcvrd_section_does_not_fall_back(self, tmp_path):
        # docker_init consults only the first existing file; if the hwsku file
        # exists but lacks an "xcvrd" section, we do not read the platform file.
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {"dom_update_interval": 30}})
        write_control_file(hwsku_dir, {"skip_xcvrd": False})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {}

    def test_no_xcvrd_section_yields_empty(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"skip_ledd": True})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {}

    def test_malformed_json_yields_empty(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        os.makedirs(platform_dir)
        with open(os.path.join(platform_dir, PMON_DAEMON_CONTROL_FILE), "w") as f:
            f.write("{ this is not valid json")
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {}

    def test_non_dict_xcvrd_section_yields_empty(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": "oops-not-an-object"})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {}

    def test_device_info_failure_yields_empty(self):
        with patch(PATHS_FN, side_effect=RuntimeError("platform undetermined")):
            assert XcvrdConfig._read_platform_section() == {}

    def test_empty_dir_path_is_skipped(self, tmp_path):
        # get_paths_to_platform_and_hwsku_dirs may return an empty hwsku path;
        # that entry is skipped rather than joined into a bogus path.
        platform_dir = str(tmp_path / "platform")
        write_control_file(platform_dir, {"xcvrd": {"dom_update_interval": 30}})
        with patch(PATHS_FN, return_value=(platform_dir, "")):
            assert XcvrdConfig._read_platform_section() == {"dom_update_interval": 30}


class TestResolveEndToEnd:
    def test_resolve_reads_from_disk(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {
            "dom_temperature_poll_interval": 5,
            "dom_update_interval": 30,
        }})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            cfg = XcvrdConfig.resolve()
        assert cfg.dom_temperature_poll_interval == 5
        assert cfg.dom_update_interval == 30

    def test_resolve_defaults_when_nothing_on_disk(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            cfg = XcvrdConfig.resolve()
        assert cfg.dom_temperature_poll_interval is None
        assert cfg.dom_update_interval is None


class TestRangeValidation:
    """Values that coerce cleanly but are not valid configuration are rejected.

    A rejected value keeps the built-in default and logs a warning; it never
    stops xcvrd from starting.
    """

    def test_negative_dom_temperature_poll_interval_keeps_default(self):
        # This is the case the range check exists for: DomThermalInfoUpdateTask
        # never validated poll_interval, so a negative value left its next-poll
        # time permanently in the past and the sweep ran back-to-back instead of
        # once a minute. Default None means the thermal thread is not started.
        cfg = XcvrdConfig.resolve(platform_section={"dom_temperature_poll_interval": -60})
        assert cfg.dom_temperature_poll_interval is None

    def test_negative_dom_update_interval_keeps_default(self):
        # DomInfoUpdateTask already guarded against this one and fell back to its
        # 60s default; the check now happens once, before the value is handed out.
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": -1})
        assert cfg.dom_update_interval is None

    def test_negative_interval_as_string_keeps_default(self):
        # Validation runs after coercion, so the stringified form is caught too.
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": "-1"})
        assert cfg.dom_update_interval is None

    def test_interval_above_maximum_keeps_default(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_update_interval": MAX_INTERVAL_SECS + 1})
        assert cfg.dom_update_interval is None

    def test_interval_boundaries_are_accepted(self):
        # Bounds are inclusive; 0 stays meaningful (continuous polling).
        assert XcvrdConfig.resolve(
            platform_section={"dom_update_interval": 0}).dom_update_interval == 0
        assert XcvrdConfig.resolve(
            platform_section={"dom_update_interval": MAX_INTERVAL_SECS}
        ).dom_update_interval == MAX_INTERVAL_SECS

    def test_both_fields_are_bounded(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_temperature_poll_interval": MAX_INTERVAL_SECS + 1,
            "dom_update_interval": MAX_INTERVAL_SECS + 1,
        })
        assert cfg.dom_temperature_poll_interval is None
        assert cfg.dom_update_interval is None

    def test_one_rejected_field_does_not_drop_the_others(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_update_interval": -1,
            "dom_temperature_poll_interval": 5,
        })
        assert cfg.dom_update_interval is None
        assert cfg.dom_temperature_poll_interval == 5
