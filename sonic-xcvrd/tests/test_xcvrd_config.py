"""
Unit tests for XcvrdConfig: the layered resolver for xcvrd's tunables.

Precedence under test (highest wins):
  1. nested keys in the "xcvrd" section of pmon_daemon_control.json
     (xcvrd.dom.*, xcvrd.cmis_mgr.enabled, ...)
  2. legacy aliases - deprecated flat dom_* keys and the top-level
     skip_xcvrd_cmis_mgr / enable_xcvrd_sff_mgr keys
  3. built-in dataclass defaults
"""
import json
import os
import sys

from unittest import mock
from unittest.mock import patch

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
sys.path.insert(0, modules_path)

from sonic_py_common import pmon_daemon_config
from sonic_py_common.pmon_daemon_config import PMON_DAEMON_CONTROL_FILE
from xcvrd.xcvrd_utilities.xcvrd_config import XcvrdConfig, MAX_INTERVAL_SECS

# Path patched in the shared base's module so no real device dir is touched; the
# read lives in sonic_py_common, not in xcvrd's schema module.
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
        assert cfg.dom.temperature_poll_interval is None
        assert cfg.dom.update_interval is None
        assert cfg.cmis_mgr.enabled is True
        assert cfg.sff_mgr.enabled is False
        assert cfg.cpo_mgr.enabled is True

    def test_bare_construction_matches_defaults(self):
        # Legacy path: DaemonXcvrd builds XcvrdConfig() directly.
        cfg = XcvrdConfig()
        assert cfg.dom.temperature_poll_interval is None
        assert cfg.dom.update_interval is None
        assert cfg.cmis_mgr.enabled is True
        assert cfg.sff_mgr.enabled is False
        assert cfg.cpo_mgr.enabled is True


class TestDomSubsection:
    def test_dom_overrides_defaults(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom": {"temperature_poll_interval": 5, "update_interval": 30}})
        assert cfg.dom.temperature_poll_interval == 5
        assert cfg.dom.update_interval == 30

    def test_partial_dom_leaves_other_field_at_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": 30}})
        assert cfg.dom.update_interval == 30
        assert cfg.dom.temperature_poll_interval is None

    def test_none_value_does_not_override(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": None}})
        assert cfg.dom.update_interval is None

    def test_zero_is_preserved(self):
        # 0 is a meaningful value (continuous polling) and must not be dropped.
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": 0}})
        assert cfg.dom.update_interval == 0

    def test_string_value_is_coerced_to_int(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": "30"}})
        assert cfg.dom.update_interval == 30
        assert isinstance(cfg.dom.update_interval, int)

    def test_invalid_value_is_ignored_and_keeps_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": "not-a-number"}})
        assert cfg.dom.update_interval is None

    def test_unknown_key_in_dom_is_ignored(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom": {"update_interval": 30, "some_future_key": 99}})
        assert cfg.dom.update_interval == 30
        assert not hasattr(cfg.dom, "some_future_key")

    def test_unknown_top_level_key_is_ignored(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom": {"update_interval": 30}, "some_future_unknown_key": 99})
        assert cfg.dom.update_interval == 30
        assert not hasattr(cfg, "some_future_unknown_key")

    def test_non_dict_dom_keeps_defaults(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": "oops-not-an-object"})
        assert cfg.dom.update_interval is None
        assert cfg.dom.temperature_poll_interval is None


class TestManagerToggles:
    def test_cmis_mgr_disabled_via_nested(self):
        cfg = XcvrdConfig.resolve(platform_section={"cmis_mgr": {"enabled": False}})
        assert cfg.cmis_mgr.enabled is False

    def test_sff_mgr_enabled_via_nested(self):
        cfg = XcvrdConfig.resolve(platform_section={"sff_mgr": {"enabled": True}})
        assert cfg.sff_mgr.enabled is True

    def test_enabled_parses_via_to_bool(self):
        # bool("false") is True in Python; to_bool must resolve it to False.
        cfg = XcvrdConfig.resolve(platform_section={"cmis_mgr": {"enabled": "false"}})
        assert cfg.cmis_mgr.enabled is False

    def test_cpo_mgr_toggle(self):
        cfg = XcvrdConfig.resolve(platform_section={"cpo_mgr": {"enabled": False}})
        assert cfg.cpo_mgr.enabled is False

    def test_unset_managers_keep_defaults(self):
        cfg = XcvrdConfig.resolve(platform_section={"cmis_mgr": {"enabled": False}})
        assert cfg.sff_mgr.enabled is False
        assert cfg.cpo_mgr.enabled is True


class TestRangeValidation:
    """Values that coerce cleanly but are not valid configuration are rejected.

    A rejected value keeps the built-in default and logs a warning; it never
    stops xcvrd from starting.
    """

    def test_negative_temperature_poll_interval_keeps_default(self):
        # DomThermalInfoUpdateTask never validated poll_interval, so a negative
        # value left its next-poll time in the past and the sweep ran back-to-back.
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"temperature_poll_interval": -60}})
        assert cfg.dom.temperature_poll_interval is None

    def test_negative_update_interval_keeps_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": -1}})
        assert cfg.dom.update_interval is None

    def test_negative_interval_as_string_keeps_default(self):
        # Validation runs after coercion, so the stringified form is caught too.
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": "-1"}})
        assert cfg.dom.update_interval is None

    def test_interval_above_maximum_keeps_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {"update_interval": MAX_INTERVAL_SECS + 1}})
        assert cfg.dom.update_interval is None

    def test_interval_boundaries_are_accepted(self):
        # Bounds are inclusive; 0 stays meaningful (continuous polling).
        assert XcvrdConfig.resolve(
            platform_section={"dom": {"update_interval": 0}}).dom.update_interval == 0
        assert XcvrdConfig.resolve(
            platform_section={"dom": {"update_interval": MAX_INTERVAL_SECS}}
        ).dom.update_interval == MAX_INTERVAL_SECS

    def test_both_fields_are_bounded(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {
            "temperature_poll_interval": MAX_INTERVAL_SECS + 1,
            "update_interval": MAX_INTERVAL_SECS + 1}})
        assert cfg.dom.temperature_poll_interval is None
        assert cfg.dom.update_interval is None

    def test_one_rejected_field_does_not_drop_the_others(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom": {
            "update_interval": -1, "temperature_poll_interval": 5}})
        assert cfg.dom.update_interval is None
        assert cfg.dom.temperature_poll_interval == 5


class TestLegacyAliases:
    """Deprecated flat dom_* keys and top-level manager keys still take effect."""

    def test_flat_dom_update_interval_alias(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": 30})
        assert cfg.dom.update_interval == 30

    def test_flat_dom_temperature_poll_interval_alias(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom_temperature_poll_interval": 5})
        assert cfg.dom.temperature_poll_interval == 5

    def test_flat_alias_out_of_range_keeps_default(self):
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": -1})
        assert cfg.dom.update_interval is None

    def test_flat_dom_update_interval_zero_falls_back_to_default(self):
        # Master's template gated the flag with a truthy check, so a flat 0 was
        # never emitted and the daemon used its own default. The `v or None`
        # transform preserves that: flat 0 -> unset -> default (None here).
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": 0})
        assert cfg.dom.update_interval is None

    def test_flat_dom_temperature_poll_interval_zero_falls_back_to_default(self):
        # Same truthy-gate parity: a flat 0 never started the thermal thread on
        # master, so it must resolve to the default (None) here, not to 0.
        cfg = XcvrdConfig.resolve(platform_section={"dom_temperature_poll_interval": 0})
        assert cfg.dom.temperature_poll_interval is None

    def test_nested_zero_wins_over_flat_zero(self):
        # The nested form is the new explicit API where 0 means continuous
        # polling; it must be honored even when the flat alias is also 0.
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_update_interval": 0, "dom": {"update_interval": 0}})
        assert cfg.dom.update_interval == 0

    def test_flat_string_zero_inherits_master_truthy_quirk(self):
        # Jinja treated the string "0" as truthy (flag emitted) while int 0 was
        # falsy; `v or None` reproduces that quirk faithfully, so a flat "0"
        # coerces through to int 0 (continuous) rather than the default.
        cfg = XcvrdConfig.resolve(platform_section={"dom_update_interval": "0"})
        assert cfg.dom.update_interval == 0

    def test_skip_xcvrd_cmis_mgr_file_alias_inverts(self):
        # Top-level skip_xcvrd_cmis_mgr=True inverts to cmis_mgr.enabled=False.
        cfg = XcvrdConfig.resolve(platform_section={},
                                  platform_file={"skip_xcvrd_cmis_mgr": True})
        assert cfg.cmis_mgr.enabled is False

    def test_enable_xcvrd_sff_mgr_file_alias(self):
        cfg = XcvrdConfig.resolve(platform_section={},
                                  platform_file={"enable_xcvrd_sff_mgr": True})
        assert cfg.sff_mgr.enabled is True

    def test_nested_form_wins_over_flat_alias(self):
        cfg = XcvrdConfig.resolve(platform_section={
            "dom_update_interval": 30, "dom": {"update_interval": 60}})
        assert cfg.dom.update_interval == 60

    def test_nested_form_wins_over_file_alias(self):
        cfg = XcvrdConfig.resolve(platform_section={"cmis_mgr": {"enabled": True}},
                                  platform_file={"skip_xcvrd_cmis_mgr": True})
        assert cfg.cmis_mgr.enabled is True

    def test_using_an_alias_logs_a_deprecation_warning(self):
        logger = mock.MagicMock()
        with mock.patch.object(pmon_daemon_config, 'get_config_logger', return_value=logger):
            XcvrdConfig.resolve(platform_section={"dom_update_interval": 30})
        assert logger.log_warning.called
        assert any("deprecated" in call.args[0]
                   for call in logger.log_warning.call_args_list)


class TestReadPlatformSection:
    def test_missing_files_yield_empty(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {}

    def test_reads_platform_file_when_no_hwsku_file(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {"dom": {"update_interval": 30}}})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            assert XcvrdConfig._read_platform_section() == {"dom": {"update_interval": 30}}

    def test_hwsku_file_takes_precedence_over_platform_file(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {"dom": {"update_interval": 30}}})
        write_control_file(hwsku_dir, {"xcvrd": {"dom": {"update_interval": 99}}})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            # Mirrors docker_init: the hwsku file wins; no cross-file merge.
            assert XcvrdConfig._read_platform_section() == {"dom": {"update_interval": 99}}

    def test_hwsku_file_without_xcvrd_section_does_not_fall_back(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {"dom": {"update_interval": 30}}})
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
        write_control_file(platform_dir, {"xcvrd": {"dom": {"update_interval": 30}}})
        with patch(PATHS_FN, return_value=(platform_dir, "")):
            assert XcvrdConfig._read_platform_section() == {"dom": {"update_interval": 30}}

    def test_read_platform_control_returns_whole_file(self, tmp_path):
        # scope='file' aliases need the sibling top-level keys, not just section.
        platform_dir = str(tmp_path / "platform")
        write_control_file(platform_dir, {
            "skip_xcvrd_cmis_mgr": True, "xcvrd": {"dom": {"update_interval": 30}}})
        with patch(PATHS_FN, return_value=(platform_dir, "")):
            section, whole = XcvrdConfig._read_platform_control()
        assert section == {"dom": {"update_interval": 30}}
        assert whole["skip_xcvrd_cmis_mgr"] is True


class TestResolveEndToEnd:
    def test_resolve_reads_from_disk(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        write_control_file(platform_dir, {"xcvrd": {
            "dom": {"temperature_poll_interval": 5, "update_interval": 30},
            "cmis_mgr": {"enabled": False}}})
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            cfg = XcvrdConfig.resolve()
        assert cfg.dom.temperature_poll_interval == 5
        assert cfg.dom.update_interval == 30
        assert cfg.cmis_mgr.enabled is False

    def test_resolve_defaults_when_nothing_on_disk(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        hwsku_dir = str(tmp_path / "hwsku")
        with patch(PATHS_FN, return_value=(platform_dir, hwsku_dir)):
            cfg = XcvrdConfig.resolve()
        assert cfg.dom.temperature_poll_interval is None
        assert cfg.dom.update_interval is None
        assert cfg.cmis_mgr.enabled is True

    def test_resolve_applies_file_scope_alias_from_disk(self, tmp_path):
        platform_dir = str(tmp_path / "platform")
        write_control_file(platform_dir, {"skip_xcvrd_cmis_mgr": True, "xcvrd": {}})
        with patch(PATHS_FN, return_value=(platform_dir, "")):
            cfg = XcvrdConfig.resolve()
        assert cfg.cmis_mgr.enabled is False
