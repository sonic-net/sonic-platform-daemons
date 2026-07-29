import os
import sys
import mock
import tempfile
import json
import pytest
import time
import importlib.util
import importlib.machinery

from mock import Mock, MagicMock, patch, mock_open
from sonic_py_common import daemon_base
from sonic_platform_base.chassis_base import ChassisBase

from .mock_platform import MockChassis, MockSmartSwitchChassis, MockModule
from .mock_module_base import ModuleBase

# imp is deprecated in Python 3.12
def load_source(module_name, file_path):
    loader = importlib.machinery.SourceFileLoader(module_name, file_path)
    spec = importlib.util.spec_from_file_location(module_name, file_path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module   # required: `from chassisd import *` relies on this
    loader.exec_module(module)
    return module

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../scripts"))

# Assuming OBJECT should be a specific value, define it manually
SELECT_OBJECT = 1  # Replace with the actual value for OBJECT if know

SYSLOG_IDENTIFIER = 'chassisd_test'
NOT_AVAILABLE = 'N/A'

daemon_base.db_connect = MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(test_path, 'mocked_libs')
sys.path.insert(0, mocked_libs_path)

modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

os.environ["CHASSISD_UNIT_TESTING"] = "1"
from chassisd import *


CHASSIS_MODULE_INFO_NAME_FIELD = 'name'
CHASSIS_MODULE_INFO_DESC_FIELD = 'desc'
CHASSIS_MODULE_INFO_SLOT_FIELD = 'slot'
CHASSIS_MODULE_INFO_OPERSTATUS_FIELD = 'oper_status'
CHASSIS_MODULE_INFO_SERIAL_FIELD = 'serial'
CHASSIS_MODULE_INFO_PRESENCE_FIELD = 'presence'
CHASSIS_MODULE_INFO_MODEL_FIELD = 'model'
CHASSIS_MODULE_INFO_REPLACEABLE_FIELD = 'is_replaceable'

CHASSIS_INFO_KEY_TEMPLATE = 'CHASSIS {}'
CHASSIS_INFO_CARD_NUM_FIELD = 'module_num'

CHASSIS_ASIC_PCI_ADDRESS_FIELD = 'asic_pci_address'
CHASSIS_ASIC_ID_IN_MODULE_FIELD = 'asic_id_in_module'

CHASSIS_MODULE_REBOOT_TIMESTAMP_FIELD = 'timestamp'
CHASSIS_MODULE_REBOOT_REBOOT_FIELD = 'reboot'
PLATFORM_ENV_CONF_FILE = "/usr/share/sonic/platform/platform_env.conf"
PLATFORM_JSON_FILE = "/usr/share/sonic/platform/platform.json"
DEFAULT_DPU_REBOOT_TIMEOUT = 360

def setup_function():
    ModuleUpdater.log_notice = MagicMock()
    ModuleUpdater.log_warning = MagicMock()


def teardown_function():
    ModuleUpdater.log_notice.reset()
    ModuleUpdater.log_warning.reset()


def test_moduleupdater_check_valid_fields():
    chassis = MockChassis()
    index = 0
    name = "FABRIC-CARD0"
    desc = "Switch Fabric Module"
    slot = 10
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    module = MockModule(index, name, desc, module_type, slot, serial)
    replaceable = True
    presence = True
    model = 'N/A'

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    module.set_replaceable(replaceable)
    module.set_presence(presence)
    module.set_model(model)

    chassis.module_list.append(module)

    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert desc == fvs[CHASSIS_MODULE_INFO_DESC_FIELD]
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]
    assert serial == fvs[CHASSIS_MODULE_INFO_SERIAL_FIELD]
    assert model == fvs[CHASSIS_MODULE_INFO_MODEL_FIELD]
    assert str(replaceable) == fvs[CHASSIS_MODULE_INFO_REPLACEABLE_FIELD]
    assert str(presence) == fvs[CHASSIS_MODULE_INFO_PRESENCE_FIELD]

def test_moduleupdater_check_phyentity_fields():
    chassis = MockChassis()
    index = 0
    name = "FABRIC-CARD0"
    desc = "Switch Fabric Module"
    slot = 10
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    module = MockModule(index, name, desc, module_type, slot, serial)
    replaceable = True
    presence = True
    model = 'N/A'
    parent_name = 'chassis 1'

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    module.set_replaceable(replaceable)
    module.set_presence(presence)
    module.set_model(model)

    chassis.module_list.append(module)

    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.module_db_update()
    fvs = module_updater.phy_entity_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert str(index) == fvs['position_in_parent']
    assert parent_name == fvs['parent_name']
    assert serial == fvs[CHASSIS_MODULE_INFO_SERIAL_FIELD]
    assert model == fvs[CHASSIS_MODULE_INFO_MODEL_FIELD]
    assert str(replaceable) == fvs[CHASSIS_MODULE_INFO_REPLACEABLE_FIELD]

def test_moduleupdater_check_phyentity_entry_after_fabric_removal():
    chassis = MockChassis()
    index = 0
    name = "FABRIC-CARD0"
    desc = "Switch Fabric Module"
    slot = 10
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    module = MockModule(index, name, desc, module_type, slot, serial)
    replaceable = True
    presence = True
    model = 'N/A'
    parent_name = 'chassis 1'

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    module.set_replaceable(replaceable)
    module.set_presence(presence)
    module.set_model(model)

    chassis.module_list.append(module)

    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.module_db_update()
    fvs = module_updater.phy_entity_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert str(index) == fvs['position_in_parent']
    assert parent_name == fvs['parent_name']
    assert serial == fvs[CHASSIS_MODULE_INFO_SERIAL_FIELD]
    assert model == fvs[CHASSIS_MODULE_INFO_MODEL_FIELD]
    assert str(replaceable) == fvs[CHASSIS_MODULE_INFO_REPLACEABLE_FIELD]

    presence = False
    module.set_presence(presence)
    module_updater.module_db_update()
    fvs = module_updater.phy_entity_table.get(name)
    assert fvs == None
    
def test_smartswitch_moduleupdater_check_valid_fields():
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)

    chassis.module_list.append(module)

    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert desc == fvs[CHASSIS_MODULE_INFO_DESC_FIELD]
    assert NOT_AVAILABLE == fvs[CHASSIS_MODULE_INFO_SLOT_FIELD]
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]
    assert serial == fvs[CHASSIS_MODULE_INFO_SERIAL_FIELD]

def test_smartswitch_moduleupdater_status_transitions():
    # Mock the chassis and module
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Add module to chassis and initialize with ONLINE status
    initial_status_online = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(initial_status_online)
    chassis.module_list.append(module)

    # Create the updater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)

    # Transition from ONLINE to OFFLINE
    offline_status = ModuleBase.MODULE_STATUS_OFFLINE
    module.set_oper_status(offline_status)
    module_updater.module_db_update()
    assert module.get_oper_status() == offline_status

    # Ensure ONLINE transition is handled correctly
    online_status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(online_status)
    module_updater.module_db_update()
    assert module.get_oper_status() == online_status


def _make_boot_id_updater():
    """Helper: build a SmartSwitchModuleUpdater with one DPU for boot_id consumer tests."""
    chassis = MockSmartSwitchChassis()
    module = MockModule(0, "DPU0", "DPU Module 0", ModuleBase.MODULE_TYPE_DPU, 0, "DPU0-0000")
    chassis.module_list.append(module)
    updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    return updater


def _patch_persisted_boot_id(updater, boot_id):
    """Make the persisted record report boot_id as the baseline."""
    return patch.object(updater, 'retrieve_dpu_reboot_info',
                        return_value=("Kernel Panic", "2026_05_19_10_00_00", boot_id))


def test_dpu_boot_id_update_new_boot():
    """New boot_id -> reboot cause captured."""
    updater = _make_boot_id_updater()

    with _patch_persisted_boot_id(updater, "old-boot-id"), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist, \
         patch.object(updater, 'update_dpu_reboot_cause_to_db') as mock_update_db:
        updater.dpu_boot_id_update("DPU0", "new-boot-id")

        mock_persist.assert_called_once()
        # boot_id must be forwarded to persist so it lands in the json/db.
        assert mock_persist.call_args.kwargs.get("boot_id") == "new-boot-id"
        mock_update_db.assert_called_once_with("DPU0")


def test_dpu_boot_id_update_same_boot():
    """Unchanged boot_id -> nothing captured (avoids duplicate on every event)."""
    updater = _make_boot_id_updater()

    with _patch_persisted_boot_id(updater, "same-boot-id"), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist, \
         patch.object(updater, 'update_dpu_reboot_cause_to_db') as mock_update_db:
        updater.dpu_boot_id_update("DPU0", "same-boot-id")

        mock_persist.assert_not_called()
        mock_update_db.assert_not_called()


@pytest.mark.parametrize("boot_id", [None, ""])
def test_dpu_boot_id_update_no_boot_id(boot_id):
    """Empty/None boot_id -> nothing captured."""
    updater = _make_boot_id_updater()

    with _patch_persisted_boot_id(updater, "old-boot-id"), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist, \
         patch.object(updater, 'update_dpu_reboot_cause_to_db') as mock_update_db:
        updater.dpu_boot_id_update("DPU0", boot_id)

        mock_persist.assert_not_called()
        mock_update_db.assert_not_called()


@pytest.mark.parametrize("persisted, expect_marker", [
    (None, True),
    ("", True),
    ("old-boot-id", False),
])
def test_dpu_boot_id_update_missing_baseline_marker(persisted, expect_marker):
    """The record is flagged as having no known baseline only when no usable boot_id was stored."""
    updater = _make_boot_id_updater()

    with _patch_persisted_boot_id(updater, persisted), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist, \
         patch.object(updater, 'update_dpu_reboot_cause_to_db'):
        updater.dpu_boot_id_update("DPU0", "new-boot-id")

        mock_persist.assert_called_once()
        assert mock_persist.call_args.kwargs.get("no_previous_boot_id") is expect_marker


def test_dpu_boot_id_update_db_failure_keeps_record(tmp_path):
    """A failed DB refresh is logged, and the record it persisted still becomes the baseline.

    retrieve_dpu_reboot_info is left unmocked and the record is written to a real directory, so
    the second event has to read the file back from disk. Feeding the baseline in from a mock
    would assert nothing about whether the DB failure cost us the record.
    """
    updater = _make_boot_id_updater()
    history_dir = tmp_path / "dpu0" / "history"
    history_dir.mkdir(parents=True)

    with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", str(tmp_path)), \
         patch.object(updater, 'update_dpu_reboot_cause_to_db', side_effect=Exception("db down")), \
         patch.object(updater, 'log_error') as mock_log_error:
        updater.dpu_boot_id_update("DPU0", "new-boot-id")

        assert mock_log_error.called
        records = list(history_dir.glob("*_reboot_cause.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_text())["boot_id"] == "new-boot-id"

    # The same boot_id again: the record on disk is now the baseline, so nothing is re-captured
    # even though the DB never received the first one. Those rows are restored by the next
    # capture or at NPU boot.
    with patch("chassisd.MODULE_REBOOT_CAUSE_DIR", str(tmp_path)), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist:
        updater.dpu_boot_id_update("DPU0", "new-boot-id")

        mock_persist.assert_not_called()


def test_dpu_boot_id_update_unknown_module():
    """Unknown DPU name (no module index) -> nothing captured."""
    updater = _make_boot_id_updater()

    with _patch_persisted_boot_id(updater, "old-boot-id"), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist, \
         patch.object(updater, 'update_dpu_reboot_cause_to_db') as mock_update_db:
        updater.dpu_boot_id_update("DPU_NONEXISTENT", "new-boot-id")

        mock_persist.assert_not_called()
        mock_update_db.assert_not_called()


@pytest.mark.parametrize("lookup, expected_log", [
    ({"target": "get_module_index", "side_effect": KeyError("DPU0")}, "Failed to look up module"),
    ({"target": "get_module_index", "side_effect": RuntimeError("platform not ready")}, "Failed to look up module"),
    ({"target": "get_module", "side_effect": IndexError("out of range")}, "Failed to look up module"),
    ({"target": "get_module", "return_value": None}, "No module object"),
])
def test_dpu_boot_id_update_module_lookup_failure_is_contained(lookup, expected_log):
    """A platform lookup that raises or yields no module is logged, never propagated.

    The subscriber loop that calls this has no exception boundary, so an escaping exception
    would silently end DPU reboot-cause capture for the rest of the daemon's lifetime.

    A platform that returns no module must be reported as such: without an explicit check it
    surfaces one step later as an AttributeError blamed on reading the reboot cause.
    """
    updater = _make_boot_id_updater()
    behavior = dict(lookup)
    target = behavior.pop("target")

    with _patch_persisted_boot_id(updater, "old-boot-id"), \
         patch.object(updater.chassis, target, **behavior), \
         patch.object(updater, 'persist_dpu_reboot_cause') as mock_persist, \
         patch.object(updater, 'update_dpu_reboot_cause_to_db') as mock_update_db, \
         patch.object(updater, 'log_error') as mock_log_error:
        updater.dpu_boot_id_update("DPU0", "new-boot-id")

        mock_persist.assert_not_called()
        mock_update_db.assert_not_called()
        assert expected_log in mock_log_error.call_args.args[0]


@pytest.mark.parametrize("failing_call, expected_log", [
    ("module.get_reboot_cause", "Failed to get reboot cause"),
    ("updater.persist_dpu_reboot_cause", "Failed to persist reboot cause"),
])
def test_dpu_boot_id_update_capture_failure_is_contained(failing_call, expected_log):
    """A failing capture step is logged and abandons the capture, leaving the DB untouched."""
    updater = _make_boot_id_updater()
    owner_name, attr = failing_call.split(".")
    owner = updater if owner_name == "updater" else updater.chassis.get_module(0)

    with _patch_persisted_boot_id(updater, "old-boot-id"), \
         patch.object(owner, attr, side_effect=Exception("boom")), \
         patch.object(updater, 'update_dpu_reboot_cause_to_db') as mock_update_db, \
         patch.object(updater, 'log_error') as mock_log_error:
        updater.dpu_boot_id_update("DPU0", "new-boot-id")

        mock_update_db.assert_not_called()
        assert expected_log in mock_log_error.call_args.args[0]


def test_retrieve_dpu_reboot_info_success():
    class DummyChassis:
        def get_num_modules(self): return 0
        def init_midplane_switch(self): return False

    updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, DummyChassis())
    sample_json = {"cause": "Switch rebooted DPU", "name": "2025_06_25_17_18_52", "boot_id": "e4252288-be0d-40ec-8338-d1e5ec206771"}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(sample_json))):
        cause, time_str, boot_id = updater.retrieve_dpu_reboot_info("dpu0")
        assert cause == "Switch rebooted DPU"
        assert time_str == "2025_06_25_17_18_52"
        assert boot_id == "e4252288-be0d-40ec-8338-d1e5ec206771"

def test_retrieve_dpu_reboot_info_file_missing():
    class DummyChassis:
        def get_num_modules(self): return 0
        def init_midplane_switch(self): return False  # required for SmartSwitchModuleUpdater

    updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, DummyChassis())
    with patch("os.path.exists", return_value=False):
        cause, time_str, boot_id = updater.retrieve_dpu_reboot_info("dpu0")
        assert cause is None
        assert time_str is None
        assert boot_id is None


def test_reboot_cause_subscriber_processes_boot_id():
    """Subscriber creates its updater in the child and forwards a valid boot_id event."""
    module_updater = MagicMock(spec=SmartSwitchModuleUpdater)
    chassis = MagicMock()
    subscriber = RebootCauseSubscriberTask()
    subscriber_db = MagicMock()
    mock_select = MagicMock()
    mock_sst = MagicMock()
    select_object = swsscommon.Select.OBJECT
    select_timeout = swsscommon.Select.TIMEOUT

    mock_select.select.side_effect = [(select_object, None), KeyboardInterrupt]
    mock_sst.pop.return_value = ("DPU0", "SET", (("boot_id", "new-boot-id"),))

    with patch("chassisd.get_chassis", return_value=chassis) as mock_get_chassis, \
         patch("chassisd.SmartSwitchModuleUpdater", return_value=module_updater) as mock_updater_class, \
         patch("chassisd.daemon_base.db_connect", return_value=subscriber_db) as mock_db_connect, \
         patch("chassisd.swsscommon.Select", return_value=mock_select) as mock_select_class, \
         patch("chassisd.swsscommon.SubscriberStateTable", return_value=mock_sst) as mock_sst_class:
        mock_select_class.TIMEOUT = select_timeout
        mock_select_class.OBJECT = select_object
        subscriber.task_worker()

    mock_get_chassis.assert_called_once_with()
    mock_updater_class.assert_called_once_with(SYSLOG_IDENTIFIER, chassis)
    mock_db_connect.assert_called_once_with("CHASSIS_STATE_DB")
    mock_sst_class.assert_called_once_with(subscriber_db, "DPU_STATE")
    mock_select.addSelectable.assert_called_once_with(mock_sst)
    module_updater.dpu_boot_id_update.assert_called_once_with("DPU0", "new-boot-id")

def test_atomic_write_json_replaces_content(tmp_path):
    """The final path holds the new content and no temporary file is left behind."""
    target = tmp_path / "record.json"
    target.write_text('{"cause": "old"}')

    SmartSwitchModuleUpdater._atomic_write_json(str(target), {"cause": "new"})

    assert json.loads(target.read_text()) == {"cause": "new"}
    assert list(p.name for p in tmp_path.iterdir()) == ["record.json"]


def test_atomic_write_json_keeps_previous_content_on_failure(tmp_path):
    """A failed write leaves the previous record intact and removes the temporary file."""
    target = tmp_path / "record.json"
    target.write_text('{"cause": "old"}')

    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            SmartSwitchModuleUpdater._atomic_write_json(str(target), {"cause": "new"})

    assert json.loads(target.read_text()) == {"cause": "old"}
    assert not (tmp_path / "record.json.tmp").exists()


def test_atomic_replace_symlink_never_leaves_link_absent(tmp_path):
    """Replacing the link repoints it in one step instead of removing and recreating it."""
    old_record = tmp_path / "old_reboot_cause.json"
    new_record = tmp_path / "new_reboot_cause.json"
    old_record.write_text("{}")
    new_record.write_text("{}")
    link = tmp_path / "previous-reboot-cause.json"
    os.symlink(str(old_record), str(link))

    removed = []
    real_remove = os.remove

    def tracking_remove(path):
        removed.append(path)
        real_remove(path)

    with patch("chassisd.os.remove", side_effect=tracking_remove):
        SmartSwitchModuleUpdater._atomic_replace_symlink(str(new_record), str(link))

    assert os.path.realpath(str(link)) == os.path.realpath(str(new_record))
    # Removing the live link, even briefly, would lose the persisted baseline on a crash.
    assert str(link) not in removed


def test_get_boot_id_reads_kernel_boot_id():
    """get_boot_id returns the stripped kernel boot ID."""
    updater = DpuStateUpdater.__new__(DpuStateUpdater)
    updater._syslog = MagicMock()

    with patch("builtins.open", mock_open(read_data="test-boot-id\n")):
        assert updater.get_boot_id() == "test-boot-id"


def test_get_boot_id_returns_none_on_oserror():
    """get_boot_id returns None and logs a warning when the file cannot be read."""
    updater = DpuStateUpdater.__new__(DpuStateUpdater)
    updater._syslog = MagicMock()
    updater.log_warning = MagicMock()

    with patch("builtins.open", side_effect=OSError("boot ID unavailable")):
        assert updater.get_boot_id() is None

    updater.log_warning.assert_called_once()


def test_smartswitch_moduleupdater_check_invalid_name():
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "TEST-CARD0"
    desc = "36 port 400G card"
    slot = 2
    serial = "TS1000101"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)

    chassis.module_list.append(module)

    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    assert fvs == None

    config_updater = SmartSwitchModuleConfigUpdater(
        SYSLOG_IDENTIFIER,
        chassis,
    )
    admin_state = 0
    config_updater.module_config_update(name, admin_state)

    # No change since invalid key
    assert module.get_admin_state() != admin_state

def test_smartswitch_moduleupdater_check_invalid_admin_state():
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)

    chassis.module_list.append(module)

    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)

    config_updater = SmartSwitchModuleConfigUpdater(
        SYSLOG_IDENTIFIER,
        chassis,
    )
    admin_state = 2
    config_updater.module_config_update(name, admin_state)

    # No change since invalid key
    assert module.get_admin_state() != admin_state

def test_smartswitch_moduleupdater_check_invalid_slot():
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = -1
    serial = "TS1000101"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)

    chassis.module_list.append(module)

    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    assert fvs != None

def test_moduleupdater_check_invalid_name():
    chassis = MockChassis()
    index = 0
    name = "TEST-CARD0"
    desc = "36 port 400G card"
    slot = 2
    serial = "TS1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)

    chassis.module_list.append(module)

    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    assert fvs == None

def test_smartswitch_moduleupdater_check_invalid_index():
    chassis = MockSmartSwitchChassis()
    index = -1
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "TS1000101"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)

    chassis.module_list.append(module)

    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    assert fvs != None

    # Run chassis db clean up
    module_updater.module_down_chassis_db_cleanup()

def test_moduleupdater_check_status_update():
    chassis = MockChassis()
    index = 0
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    print('Initial DB-entry {}'.format(fvs))
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # Update status
    status = ModuleBase.MODULE_STATUS_OFFLINE
    module.set_oper_status(status)
    fvs = module_updater.module_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    print('Not updated DB-entry {}'.format(fvs))
    assert status != fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # Update status and db
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    print('Updated DB-entry {}'.format(fvs))
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # Run chassis db clean up from LC.
    module_updater.module_down_chassis_db_cleanup()

def test_moduleupdater_check_deinit():
    chassis = MockChassis()
    index = 0
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.modules_num_update()
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    module_table = module_updater.module_table
    module_updater.deinit()
    fvs = module_table.get(name)
    assert fvs == None

def test_smartswitch_moduleupdater_check_deinit():
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.modules_num_update()
    module_updater.module_db_update()
    fvs = module_updater.module_table.get(name)
    # if isinstance(fvs, list):
    #    fvs = dict(fvs[-1])
    # assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    module_table = module_updater.module_table
    module_updater.deinit()
    fvs = module_table.get(name)
    assert fvs == None

def test_configupdater_check_valid_names():
    chassis = MockChassis()
    index = 0
    name = "TEST-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "TC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    config_updater = ModuleConfigUpdater(SYSLOG_IDENTIFIER, chassis)
    admin_state = 0
    config_updater.module_config_update(name, admin_state)

    # No change since invalid key
    assert module.get_admin_state() != admin_state


def test_configupdater_check_valid_index():
    chassis = MockChassis()
    index = -1
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    config_updater = ModuleConfigUpdater(SYSLOG_IDENTIFIER, chassis)
    admin_state = 0
    config_updater.module_config_update(name, admin_state)

    # No change since invalid index
    assert module.get_admin_state() != admin_state


def test_configupdater_check_admin_state():
    chassis = MockChassis()
    index = 0
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    config_updater = ModuleConfigUpdater(SYSLOG_IDENTIFIER, chassis)
    admin_state = 0
    config_updater.module_config_update(name, admin_state)
    assert module.get_admin_state() == admin_state

    admin_state = 1
    config_updater.module_config_update(name, admin_state)
    assert module.get_admin_state() == admin_state


def test_smartswitch_configupdater_check_admin_state():
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 1
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    config_updater = SmartSwitchModuleConfigUpdater(
        SYSLOG_IDENTIFIER,
        chassis
    )

    # Test setting admin state to down
    admin_state = 0
    with patch.object(module, 'set_admin_state_gracefully') as mock_set_admin_state_gracefully:
        config_updater.module_config_update(name, admin_state)
        mock_set_admin_state_gracefully.assert_called_once_with(admin_state)

    # Test setting admin state to up
    admin_state = 1
    with patch.object(module, 'set_admin_state_gracefully') as mock_set_admin_state_gracefully:
        config_updater.module_config_update(name, admin_state)
        mock_set_admin_state_gracefully.assert_called_once_with(admin_state)


@patch("chassisd.glob.glob")
@patch("chassisd.open", new_callable=mock_open)
def test_update_dpu_reboot_cause_to_db(mock_open, mock_glob):
    module_updater = SmartSwitchModuleUpdater("TEST_LOG", chassis=MagicMock())
    module = "dpu0"
    module_updater.chassis_state_db = MagicMock()

    # Case 1: No history files found
    mock_glob.return_value = []
    with patch.object(module_updater, "log_warning") as mock_log_warning:
        module_updater.update_dpu_reboot_cause_to_db(module)
        mock_log_warning.assert_called_once_with(f"No reboot cause history files found for module: {module}")

    # Case 2: Valid JSON file with reboot cause
    mock_glob.return_value = ["/host/reboot-cause/module/dpu0/history/file1.txt"]
    mock_open().read.return_value = json.dumps({"name": "reboot_2024", "reason": "Power loss"})
    with patch.object(module_updater, "log_warning") as mock_log_warning:
        module_updater.update_dpu_reboot_cause_to_db(module)
        mock_log_warning.assert_not_called()
        module_updater.chassis_state_db.hset.assert_any_call("REBOOT_CAUSE|DPU0|reboot_2024", "name", "reboot_2024")
        module_updater.chassis_state_db.hset.assert_any_call("REBOOT_CAUSE|DPU0|reboot_2024", "reason", "Power loss")

    # Case 3: Empty JSON object in file
    mock_open().read.return_value = json.dumps({})
    with patch.object(module_updater, "log_warning") as mock_log_warning:
        module_updater.update_dpu_reboot_cause_to_db(module)
        mock_log_warning.assert_any_call(f"{module} reboot_cause_dict is empty")

    # Case 4: Invalid JSON in file
    mock_open().read.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    with patch.object(module_updater, "log_warning") as mock_log_warning:
        module_updater.update_dpu_reboot_cause_to_db(module)
        mock_log_warning.assert_any_call("Failed to decode JSON from file: /host/reboot-cause/module/dpu0/history/file1.txt")

    # Case 5: General exception handling
    mock_open.side_effect = IOError("Unable to read file")
    with patch.object(module_updater, "log_warning") as mock_log_warning:
        module_updater.update_dpu_reboot_cause_to_db(module)
        mock_log_warning.assert_any_call("Error processing file /host/reboot-cause/module/dpu0/history/file1.txt: Unable to read file")


def test_platform_json_file_exists_and_valid():
    """Test case where the platform JSON file exists with valid data."""
    chassis = MockSmartSwitchChassis()

    # Define the custom mock_open function to handle specific file paths
    def custom_mock_open(*args, **kwargs):
        if args and args[0] == PLATFORM_JSON_FILE:
            return mock_open(read_data='{"dpu_reboot_timeout": 360}')(*args, **kwargs)
        return open(*args, **kwargs)  # Call the real open for other files

    with patch("os.path.isfile", return_value=True), \
        patch("builtins.open", custom_mock_open):

        # Initialize the updater; it should read the mocked JSON data
        updater = SmartSwitchModuleUpdater("SYSLOG", chassis)

        # Check that the extracted dpu_reboot_timeout value is as expected
        assert updater.dpu_reboot_timeout == 360


def test_platform_json_file_exists_fail_init():
    """Test case where the platform JSON file exists with valid data."""
    chassis = MockSmartSwitchChassis()

    # Define the custom mock_open function to handle specific file paths
    def custom_mock_open(*args, **kwargs):
        if args and args[0] == PLATFORM_JSON_FILE:
            return mock_open(read_data='{"dpu_reboot_timeout": 360}')(*args, **kwargs)
        return open(*args, **kwargs)  # Call the real open for other files

    with patch("os.path.isfile", return_value=True), \
        patch("builtins.open", custom_mock_open):

        # Initialize the updater; it should read the mocked JSON data
        updater = SmartSwitchModuleUpdater("SYSLOG", chassis)
        updater.midplane_initialized = False

        # Check that the extracted dpu_reboot_timeout value is as expected
        assert updater.dpu_reboot_timeout == 360


def test_configupdater_check_num_modules():
    chassis = MockChassis()
    index = 0
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)

    # No modules
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.modules_num_update()
    fvs = module_updater.chassis_table.get(CHASSIS_INFO_KEY_TEMPLATE.format(1))
    assert fvs == None

    # Add a module
    chassis.module_list.append(module)
    module_updater.modules_num_update()
    fvs = module_updater.chassis_table.get(CHASSIS_INFO_KEY_TEMPLATE.format(1))
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert chassis.get_num_modules() == int(fvs[CHASSIS_INFO_CARD_NUM_FIELD])

    module_updater.deinit()
    fvs = module_updater.chassis_table.get(CHASSIS_INFO_KEY_TEMPLATE.format(1))
    assert fvs == None

def test_moduleupdater_check_string_slot():
    chassis = MockChassis()

    #Supervisor
    index = 0
    name = "SUPERVISOR0"
    desc = "Supervisor card"
    slot = "A"
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, name, desc, module_type, slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard
    index = 1
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = "1"
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Fabric-card
    index = 1
    name = "FABRIC-CARD0"
    desc = "Switch fabric card"
    slot = "17"
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    fabric = MockModule(index, name, desc, module_type, slot, serial)
    chassis.module_list.append(fabric)

    #Run on supervisor
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.supervisor_slot = supervisor.get_slot()
    module_updater.my_slot = supervisor.get_slot()
    module_updater.modules_num_update()
    module_updater.module_db_update()
    module_updater.check_midplane_reachability()

    midplane_table = module_updater.midplane_table
    #Check only one entry in database
    assert 1 == midplane_table.size()
    
def test_midplane_presence_modules():
    chassis = MockChassis()

    #Supervisor
    index = 0
    name = "SUPERVISOR0"
    desc = "Supervisor card"
    slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, name, desc, module_type, slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard
    index = 1
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Fabric-card
    index = 1
    name = "FABRIC-CARD0"
    desc = "Switch fabric card"
    slot = 17
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    fabric = MockModule(index, name, desc, module_type, slot, serial)
    chassis.module_list.append(fabric)

    #Run on supervisor
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.supervisor_slot = supervisor.get_slot()
    module_updater.my_slot = supervisor.get_slot()
    module_updater.modules_num_update()
    module_updater.module_db_update()
    module_updater.check_midplane_reachability()

    midplane_table = module_updater.midplane_table
    #Check only one entry in database
    assert 1 == midplane_table.size()

    #Check fields in database
    name = "LINE-CARD0"
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Set access of line-card to Up (midplane connectivity is down initially)
    module.set_midplane_reachable(True)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Set access of line-card to Down (to mock midplane connectivity state change)
    module.set_midplane_reachable(False)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Deinit
    module_updater.deinit()
    fvs = midplane_table.get(name)
    assert fvs == None


@patch('os.makedirs')
@patch('builtins.open', new_callable=mock_open)
def test_midplane_presence_dpu_modules(mock_open, mock_makedirs):
    with tempfile.TemporaryDirectory() as temp_dir:
        # Assume your method uses a path variable that you can set for testing
        path = os.path.join(temp_dir, 'subdir')

        # Set up your mock or variable to use temp_dir
        mock_makedirs.side_effect = lambda x, **kwargs: None  # Prevent actual call

        chassis = MockSmartSwitchChassis()

        #DPU0
        index = 0
        name = "DPU0"
        desc = "DPU Module 0"
        slot = 0
        sup_slot = 0
        serial = "DPU0-0000"
        module_type = ModuleBase.MODULE_TYPE_DPU
        module = MockModule(index, name, desc, module_type, slot, serial)
        module.set_midplane_ip()
        module.prev_reboot_time = "2024_10_30_02_44_50"
        chassis.module_list.append(module)

        #Run on supervisor
        module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
        module_updater.midplane_initialized = True
        module_updater.modules_num_update()
        module_updater.module_db_update()
        module_updater.check_midplane_reachability()

        midplane_table = module_updater.midplane_table
        #Check only one entry in database
        assert 1 == midplane_table.size()

        #Check fields in database
        fvs = midplane_table.get(name)
        assert fvs != None
        if isinstance(fvs, list):
            fvs = dict(fvs[-1])
        assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
        assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

        #Set access of DPU0 to Up (midplane connectivity is down initially)
        module.set_midplane_reachable(True)
        module_updater.check_midplane_reachability()
        fvs = midplane_table.get(name)
        assert fvs != None
        if isinstance(fvs, list):
            fvs = dict(fvs[-1])
        assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
        assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

        #Set access of DPU0 to Down (to mock midplane connectivity state change)
        module.set_midplane_reachable(False)
        module_updater.check_midplane_reachability()
        fvs = midplane_table.get(name)
        assert fvs != None
        if isinstance(fvs, list):
            fvs = dict(fvs[-1])
        assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
        assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

        # Run chassis db clean up
        module_updater.module_down_chassis_db_cleanup()
        module_updater.chassis_state_db = None
        module_updater.module_down_chassis_db_cleanup()

        #Deinit
        module_updater.deinit()
        fvs = midplane_table.get(name)
        assert fvs == None


@patch('os.makedirs')
@patch('builtins.open', new_callable=mock_open)
def test_midplane_presence_uninitialized_dpu_modules(mock_open, mock_makedirs):
    with tempfile.TemporaryDirectory() as temp_dir:
        # Assume your method uses a path variable that you can set for testing
        path = os.path.join(temp_dir, 'subdir')

        # Set up your mock or variable to use temp_dir
        mock_makedirs.side_effect = lambda x, **kwargs: None  # Prevent actual call

        chassis = MockSmartSwitchChassis()

        #DPU0
        index = 0
        name = "DPU0"
        desc = "DPU Module 0"
        slot = 0
        sup_slot = 0
        serial = "DPU0-0000"
        module_type = ModuleBase.MODULE_TYPE_DPU
        module = MockModule(index, name, desc, module_type, slot, serial)
        module.set_midplane_ip()
        module.prev_reboot_time = "2024_10_30_02_44_50"
        chassis.module_list.append(module)

        #Run on supervisor
        module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
        module_updater.midplane_initialized = False
        module_updater.modules_num_update()
        module_updater.module_db_update()
        module_updater.check_midplane_reachability()

        midplane_table = module_updater.midplane_table
        #Check only one entry in database
        assert 1 != midplane_table.size()

builtin_open = open  # save the unpatched version
def lc_mock_open(*args, **kwargs):
    if args and args[0] == PLATFORM_ENV_CONF_FILE:
        return mock.mock_open(read_data="dummy=1\nlinecard_reboot_timeout=240\n")(*args, **kwargs)
    # unpatched version for every other path
    return builtin_open(*args, **kwargs)

@patch("builtins.open", lc_mock_open)
@patch('os.path.isfile', MagicMock(return_value=True))
def test_midplane_presence_modules_linecard_reboot():
    chassis = MockChassis()
        
    #Supervisor
    index = 0
    name = "SUPERVISOR0"
    desc = "Supervisor card"
    slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, name, desc, module_type, slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard
    index = 1
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Fabric-card
    index = 1
    name = "FABRIC-CARD0"
    desc = "Switch fabric card"
    slot = 17
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    fabric = MockModule(index, name, desc, module_type, slot, serial)
    chassis.module_list.append(fabric)

    #Run on supervisor
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.supervisor_slot = supervisor.get_slot()
    module_updater.my_slot = supervisor.get_slot()
    module_updater.modules_num_update()
    module_updater.module_db_update()
    module_updater.check_midplane_reachability()

    midplane_table = module_updater.midplane_table
    #Check only one entry in database
    assert 1 == midplane_table.size()

    #Check fields in database
    name = "LINE-CARD0"
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Set access of line-card to Up (midplane connectivity is down initially)
    module.set_midplane_reachable(True)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    
    #Set access of line-card to Down (to mock midplane connectivity state change)
    module.set_midplane_reachable(False)
    # set expected reboot of linecard
    module_reboot_table = module_updater.module_reboot_table
    linecard_fvs = swsscommon.FieldValuePairs([("reboot", "expected")])
    module_reboot_table.set(name,linecard_fvs)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Set access of line-card to up on time (to mock midplane connectivity state change)
    module.set_midplane_reachable(True)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    # test linecard reboot midplane connectivity restored timeout
    # Set access of line-card to Down (to mock midplane connectivity state change)
    module.set_midplane_reachable(False)
    linecard_fvs = swsscommon.FieldValuePairs([("reboot", "expected")])
    module_reboot_table.set(name,linecard_fvs)
    module_updater.check_midplane_reachability()
    time_now= time.time() - module_updater.linecard_reboot_timeout
    linecard_fvs = swsscommon.FieldValuePairs([(CHASSIS_MODULE_REBOOT_TIMESTAMP_FIELD, str(time_now))])
    module_reboot_table.set(name,linecard_fvs)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert module.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(module.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]   
    assert module_updater.linecard_reboot_timeout == 240    
    
def test_midplane_presence_supervisor():
    chassis = MockChassis()

    #Supervisor
    index = 0
    name = "SUPERVISOR0"
    desc = "Supervisor card"
    slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, name, desc, module_type, slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard
    index = 1
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Fabric-card
    index = 1
    name = "FABRIC-CARD0"
    desc = "Switch fabric card"
    slot = 17
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    fabric = MockModule(index, name, desc, module_type, slot, serial)
    chassis.module_list.append(fabric)

    #Run on supervisor
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, slot,
                                   module.supervisor_slot)
    module_updater.supervisor_slot = supervisor.get_slot()
    module_updater.my_slot = module.get_slot()
    module_updater.modules_num_update()
    module_updater.module_db_update()
    module_updater.check_midplane_reachability()

    midplane_table = module_updater.midplane_table
    #Check only one entry in database
    assert 1 == midplane_table.size()

    #Check fields in database
    name = "SUPERVISOR0"
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert supervisor.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(supervisor.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Set access of line-card to down
    supervisor.set_midplane_reachable(False)
    module_updater.check_midplane_reachability()
    fvs = midplane_table.get(name)
    assert fvs != None
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert supervisor.get_midplane_ip() == fvs[CHASSIS_MIDPLANE_INFO_IP_FIELD]
    assert str(supervisor.is_midplane_reachable()) == fvs[CHASSIS_MIDPLANE_INFO_ACCESS_FIELD]

    #Deinit
    module_updater.deinit()
    fvs = midplane_table.get(name)
    assert fvs == None

def verify_asic(asic_name, asic_pci_address, module_name, asic_id_in_module, asic_table):
    fvs = asic_table.get(asic_name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert fvs[CHASSIS_ASIC_PCI_ADDRESS_FIELD] == asic_pci_address
    assert fvs[CHASSIS_MODULE_INFO_NAME_FIELD] == module_name
    assert fvs[CHASSIS_ASIC_ID_IN_MODULE_FIELD] == asic_id_in_module

def verify_asic_in_module_table(lc, slot, num_asics, chassis_module_table):
    fvs = chassis_module_table.get(lc)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert fvs['slot'] == str(slot)
    assert fvs['num_asics'] == str(num_asics)

def test_asic_presence():
    chassis = MockChassis()

    #Supervisor
    index = 0
    name = "SUPERVISOR0"
    desc = "Supervisor card"
    slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, name, desc, module_type, slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard
    index = 1
    name = "LINE-CARD0"
    desc = "36 port 400G card"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Fabric-card with asics
    index = 1
    name = "FABRIC-CARD0"
    desc = "Switch fabric card"
    slot = 17
    serial = "FC1000101"
    module_type = ModuleBase.MODULE_TYPE_FABRIC
    fabric_asic_list = [("4", "0000:04:00.0"), ("5", "0000:05:00.0")]
    fabric = MockModule(index, name, desc, module_type, slot, serial, fabric_asic_list)
    chassis.module_list.append(fabric)

    #Run on supervisor
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis,
                                   module.supervisor_slot,
                                   module.supervisor_slot)
    module_updater.modules_num_update()
    module_updater.module_db_update()
    module_updater.check_midplane_reachability()

    #Asic presence on fabric module
    fabric.set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
    module_updater.module_db_update()
    fabric_asic_table = module_updater.asic_table
    assert len(fabric_asic_table.getKeys()) == 2

    verify_asic("asic4", "0000:04:00.0", name, "0", fabric_asic_table)
    verify_asic("asic5", "0000:05:00.0", name, "1", fabric_asic_table)

    #Card goes down and asics should be gone
    fabric.set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
    module_updater.module_db_update()
    assert len(fabric_asic_table.getKeys()) == 0

    #Deinit
    fabric.set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
    module_updater.module_db_update()
    module_updater.deinit()
    midplane_table = module_updater.midplane_table
    fvs = midplane_table.get(name)
    assert fvs == None
    verify_asic("asic4", "0000:04:00.0", name, "0", fabric_asic_table)
    verify_asic("asic5", "0000:05:00.0", name, "1", fabric_asic_table)

def test_forwarding_asic_presence():
    chassis = MockChassis()

    #Supervisor
    index = 0
    name = "SUPERVISOR0"
    desc = "Supervisor card"
    slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, name, desc, module_type, slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard
    index = 1
    name = "LINE-CARD0"
    desc = "36 port 400G card with 2 ASICs"
    slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    asic_list = [("4", "0000:04:00.0"), ("5", "0000:05:00.0")]
    module = MockModule(index, name, desc, module_type, slot, serial, asic_list)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Run on linecard
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis,
                                   slot,
                                   module.supervisor_slot)

    module_updater.modules_num_update()
    module_updater.check_midplane_reachability()
    module.set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
    module_updater.module_db_update()
    asic_table = module_updater.asic_table
    assert len(asic_table.getKeys()) == 2

    # Check CHASSIS_ASIC_TABLE
    verify_asic("LINE-CARD0|asic4", "0000:04:00.0", name, "0", asic_table)
    verify_asic("LINE-CARD0|asic5", "0000:05:00.0", name, "1", asic_table)

    # Card goes down and asics should be gone
    module.set_oper_status(ModuleBase.MODULE_STATUS_OFFLINE)
    module_updater.module_db_update()
    assert len(asic_table.getKeys()) == 0

    module.set_oper_status(ModuleBase.MODULE_STATUS_ONLINE)
    module_updater.module_db_update()
    assert len(asic_table.getKeys()) == 2

    verify_asic("LINE-CARD0|asic4", "0000:04:00.0", name, "0", asic_table)
    verify_asic("LINE-CARD0|asic5", "0000:05:00.0", name, "1", asic_table)

    # Check CHASSIS_MODULE_TABLE
    verify_asic_in_module_table(name, slot, len(asic_list), module_updater.hostname_table)

def test_signal_handler():
    exit_code = 0
    chassis = MockChassis()
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.stop.set = MagicMock()
    daemon_chassisd.log_info = MagicMock()
    daemon_chassisd.log_warning = MagicMock()

    # Test SIGHUP
    daemon_chassisd.signal_handler(signal.SIGHUP, None)
    assert daemon_chassisd.log_info.call_count == 1
    daemon_chassisd.log_info.assert_called_with("Caught signal 'SIGHUP' - ignoring...")
    assert daemon_chassisd.log_warning.call_count == 0
    assert daemon_chassisd.stop.set.call_count == 0
    assert exit_code == 0

    # Reset
    daemon_chassisd.log_info.reset_mock()
    daemon_chassisd.log_warning.reset_mock()
    daemon_chassisd.stop.set.reset_mock()

    # Test SIGINT
    test_signal = signal.SIGINT
    daemon_chassisd.signal_handler(test_signal, None)
    assert daemon_chassisd.log_info.call_count == 1
    daemon_chassisd.log_info.assert_called_with("Caught {} signal 'SIGINT' - exiting...".format(128 + test_signal))
    assert daemon_chassisd.log_warning.call_count == 0
    assert daemon_chassisd.stop.set.call_count == 1

    # Reset
    daemon_chassisd.log_info.reset_mock()
    daemon_chassisd.log_warning.reset_mock()
    daemon_chassisd.stop.set.reset_mock()

    # Test SIGTERM
    test_signal = signal.SIGTERM
    daemon_chassisd.signal_handler(test_signal, None)
    assert daemon_chassisd.log_info.call_count == 1
    daemon_chassisd.log_info.assert_called_with("Caught {} signal 'SIGTERM' - exiting...".format(128 + test_signal))
    assert daemon_chassisd.log_warning.call_count == 0
    assert daemon_chassisd.stop.set.call_count == 1

    # Reset
    daemon_chassisd.log_info.reset_mock()
    daemon_chassisd.log_warning.reset_mock()
    daemon_chassisd.stop.set.reset_mock()
    exit_code = 0

    # Test an unhandled signal
    daemon_chassisd.signal_handler(signal.SIGUSR1, None)
    assert daemon_chassisd.log_warning.call_count == 1
    daemon_chassisd.log_warning.assert_called_with("Caught unhandled signal 'SIGUSR1' - ignoring...")
    assert daemon_chassisd.log_info.call_count == 0
    assert daemon_chassisd.stop.set.call_count == 0
    assert exit_code == 0

def test_daemon_run_smartswitch():
    # Test the chassisd run
    chassis = MockSmartSwitchChassis()

    # DPU0
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    sup_slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)
    chassis.module_list.append(module)

    # Supervisor ModuleUpdater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    module_updater.modules_num_update()

    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.stop = MagicMock()
    daemon_chassisd.stop.wait.return_value = True
    daemon_chassisd.smartswitch = True

    import sonic_platform.platform
    with patch.object(sonic_platform.platform.Chassis, 'is_smartswitch') as mock_is_smartswitch:
        mock_is_smartswitch.return_value = True

        with patch.object(module_updater, 'num_modules', 1):
            daemon_chassisd.run()

def test_set_initial_dpu_admin_state_up():
    """Test set_initial_dpu_admin_state when admin state is up"""
    chassis = MockSmartSwitchChassis()
   
    # DPU0 details
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()

    # Set initial state for DPU0 - ONLINE
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)
   
    # Supervisor ModuleUpdater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    module_updater.modules_num_update()
   
    # ChassisdDaemon setup
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.module_updater = module_updater
    daemon_chassisd.platform_chassis = chassis
    daemon_chassisd.smartswitch = True

    # Mock the necessary methods
    with patch.object(module_updater, 'get_module_admin_status', return_value='up'), \
         patch.object(module_updater, 'update_dpu_state') as mock_update_dpu_state, \
         patch.object(daemon_chassisd, 'submit_dpu_callback') as mock_submit_callback, \
         patch.object(module, 'clear_module_state_transition') as mock_clear_transition, \
         patch.object(module, 'clear_module_gnoi_halt_in_progress') as mock_clear_gnoi:

        # Run the function
        daemon_chassisd.set_initial_dpu_admin_state()

        # Verify state transition flags were cleared
        mock_clear_transition.assert_called_once()
        mock_clear_gnoi.assert_called_once()

        # Verify DPU state was updated with 'up' since operational state is ONLINE
        mock_update_dpu_state.assert_called_once_with("DPU_STATE|DPU0", 'up')

        # Verify callback was NOT submitted since admin state is 'up'
        mock_submit_callback.assert_not_called()


def test_set_initial_dpu_admin_state_empty_offline(midplane_reason_dir):
    """Test set_initial_dpu_admin_state when admin state is empty and operational state is offline"""
    chassis = MockSmartSwitchChassis()
   
    # DPU0 details
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()

    # Set initial state for DPU0 - OFFLINE
    status = ModuleBase.MODULE_STATUS_OFFLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)
   
    # Supervisor ModuleUpdater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    module_updater.modules_num_update()
   
    # ChassisdDaemon setup
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.module_updater = module_updater
    daemon_chassisd.platform_chassis = chassis
    daemon_chassisd.smartswitch = True

    reason_dir = midplane_reason_dir / "dpu0"
    reason_dir.mkdir()
    (reason_dir / "midplane-down-reason.txt").write_text("Unplanned: 'Thermal Overload: ASIC'\n")

    # Mock the necessary methods - admin state is EMPTY, operational state is OFFLINE
    with patch.object(module_updater, 'get_module_admin_status', return_value=ModuleBase.MODULE_STATUS_EMPTY), \
         patch.object(module_updater, 'update_dpu_state') as mock_update_dpu_state, \
         patch.object(daemon_chassisd, 'submit_dpu_callback') as mock_submit_callback, \
         patch.object(module, 'clear_module_state_transition') as mock_clear_transition, \
         patch.object(module, 'clear_module_gnoi_halt_in_progress') as mock_clear_gnoi:

        # Run the function
        daemon_chassisd.set_initial_dpu_admin_state()

        # Verify state transition flags were cleared
        mock_clear_transition.assert_called_once()
        mock_clear_gnoi.assert_called_once()

        # Verify the persisted reason is restored with the down state.
        mock_update_dpu_state.assert_called_once_with(
            "DPU_STATE|DPU0", 'down', "Unplanned: 'Thermal Overload: ASIC'")

        # Verify callback was submitted with MODULE_ADMIN_DOWN when admin state is EMPTY
        mock_submit_callback.assert_called_once_with(0, MODULE_ADMIN_DOWN)


def test_set_initial_dpu_admin_state_empty_not_offline():
    """Test set_initial_dpu_admin_state when admin state is empty but operational state is not offline"""
    chassis = MockSmartSwitchChassis()

    # DPU0 details
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()

    # Set initial state for DPU0 - PRESENT (not OFFLINE)
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)
    chassis.module_list.append(module)

    # Supervisor ModuleUpdater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    module_updater.modules_num_update()

    # ChassisdDaemon setup
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.module_updater = module_updater
    daemon_chassisd.platform_chassis = chassis
    daemon_chassisd.smartswitch = True

    # Mock the necessary methods - admin state is EMPTY, operational state is PRESENT
    with patch.object(module_updater, 'get_module_admin_status', return_value=ModuleBase.MODULE_STATUS_EMPTY), \
         patch.object(module_updater, '_read_midplane_down_reason', return_value=None), \
         patch.object(module_updater, 'update_dpu_state') as mock_update_dpu_state, \
         patch.object(daemon_chassisd, 'submit_dpu_callback') as mock_submit_callback, \
         patch.object(module, 'clear_module_state_transition') as mock_clear_transition, \
         patch.object(module, 'clear_module_gnoi_halt_in_progress') as mock_clear_gnoi:

        # Run the function
        daemon_chassisd.set_initial_dpu_admin_state()

        # Verify state transition flags were cleared
        mock_clear_transition.assert_called_once()
        mock_clear_gnoi.assert_called_once()

        # Verify DPU state was updated with 'down' since operational state is not ONLINE
        mock_update_dpu_state.assert_called_once_with("DPU_STATE|DPU0", 'down')

        # Verify callback was submitted with MODULE_ADMIN_DOWN when admin state is EMPTY
        mock_submit_callback.assert_called_once_with(0, MODULE_ADMIN_DOWN)


def test_set_initial_dpu_admin_state_exception():
    """Test set_initial_dpu_admin_state handles exceptions gracefully"""
    chassis = MockSmartSwitchChassis()
   
    # DPU0 details
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()

    # Set initial state for DPU0
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    # Supervisor ModuleUpdater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    module_updater.modules_num_update()

    # ChassisdDaemon setup
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.module_updater = module_updater
    daemon_chassisd.platform_chassis = chassis
    daemon_chassisd.smartswitch = True

    # Mock the necessary methods to raise an exception
    with patch.object(module_updater, 'get_module_admin_status', side_effect=Exception("Test error")), \
         patch.object(daemon_chassisd, 'log_error') as mock_log_error, \
         patch.object(module, 'clear_module_state_transition') as mock_clear_transition, \
         patch.object(module, 'clear_module_gnoi_halt_in_progress') as mock_clear_gnoi:

        # Run the function - should not raise exception
        daemon_chassisd.set_initial_dpu_admin_state()

        # Verify state transition flags were cleared before exception
        mock_clear_transition.assert_called_once()
        mock_clear_gnoi.assert_called_once()

        # Verify error was logged
        mock_log_error.assert_called_once()
        assert "Error in run: Test error" in str(mock_log_error.call_args)


def test_set_initial_dpu_admin_state_threading():
    """Test that set_initial_dpu_admin_state creates and waits for threads correctly"""
    chassis = MockSmartSwitchChassis()

    # DPU0 details
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()

    # Set initial state for DPU0 - PRESENT
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)
    chassis.module_list.append(module)

    # Supervisor ModuleUpdater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.module_db_update()
    module_updater.modules_num_update()

    # ChassisdDaemon setup
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.module_updater = module_updater
    daemon_chassisd.platform_chassis = chassis
    daemon_chassisd.smartswitch = True

    # Mock thread
    mock_thread = MagicMock()

    # Mock the necessary methods
    with patch.object(module_updater, 'get_module_admin_status', return_value=ModuleBase.MODULE_STATUS_EMPTY), \
         patch.object(module_updater, 'update_dpu_state') as mock_update_dpu_state, \
         patch.object(daemon_chassisd, 'submit_dpu_callback') as mock_submit_callback, \
         patch.object(module, 'clear_module_state_transition') as mock_clear_transition, \
         patch.object(module, 'clear_module_gnoi_halt_in_progress') as mock_clear_gnoi, \
         patch('threading.Thread', return_value=mock_thread) as mock_thread_class:

        # Run the function
        daemon_chassisd.set_initial_dpu_admin_state()

        # Verify thread was created with correct arguments
        mock_thread_class.assert_called_once()
        call_args = mock_thread_class.call_args
        assert call_args[1]['target'] == daemon_chassisd.submit_dpu_callback
        assert call_args[1]['args'] == (0, MODULE_ADMIN_DOWN)

        # Verify thread was started and joined
        mock_thread.start.assert_called_once()
        mock_thread.join.assert_called_once()

        # Verify daemon flag was set
        assert mock_thread.daemon == True


def test_daemon_run_supervisor_invalid_slot():
    chassis = MockChassis()
    #Supervisor
    index = 0
    sup_slot = -1
    # Supervisor ModuleUpdater
    module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, sup_slot, sup_slot)

    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.stop = MagicMock()
    daemon_chassisd.stop.wait.return_value = True
    module_updater.my_slot = ModuleBase.MODULE_INVALID_SLOT
    module_updater.supervisor_slot = ModuleBase.MODULE_INVALID_SLOT
    daemon_chassisd.run()

def test_daemon_run_supervisor():
    # Test the chassisd run
    chassis = MockChassis()

    chassis.get_supervisor_slot = Mock()
    chassis.get_supervisor_slot.return_value = 0
    chassis.get_my_slot = Mock()
    chassis.get_my_slot.return_value = 0

    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.stop = MagicMock()
    daemon_chassisd.stop.wait.return_value = True
    daemon_chassisd.run()

def import_mock_swsscommon():
    return importlib.import_module('tests.mock_swsscommon')

def test_task_worker_loop():
    # Create a mock for the Select object
    mock_select = MagicMock()

    # Set up the mock to raise a KeyboardInterrupt after the first call
    mock_select.select.side_effect = [(mock_select.TIMEOUT, None), KeyboardInterrupt]

    # Patch the swsscommon.Select to use this mock
    with patch('tests.mock_swsscommon.Select', return_value=mock_select):
        config_manager = SmartSwitchConfigManagerTask()

        config_manager.config_updater = MagicMock()

        try:
            config_manager.task_worker()
        except KeyboardInterrupt:
            pass  # Handle the KeyboardInterrupt as expected

def test_daemon_run_linecard():
    # Test the chassisd run
    chassis = MockChassis()

    chassis.get_supervisor_slot = Mock()
    chassis.get_supervisor_slot.return_value = 0
    chassis.get_my_slot = Mock()
    chassis.get_my_slot.return_value = 1

    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.stop = MagicMock()
    daemon_chassisd.stop.wait.return_value = True
    daemon_chassisd.run()

def test_chassis_db_cleanup():
    chassis = MockChassis()

    #Supervisor
    index = 0
    sup_name = "SUPERVISOR0"
    desc = "Supervisor card"
    sup_slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, sup_name, desc, module_type, sup_slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard 0. Host name will be pushed for this to make clean up happen
    index = 1
    lc_name = "LINE-CARD0"
    desc = "36 port 400G card"
    lc_slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, lc_name, desc, module_type, lc_slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    #Linecard 1. Host name will not be pushed for this so that clean up will not happen
    index = 2
    lc2_name = "LINE-CARD1"
    desc = "36 port 400G card"
    lc2_slot = 2
    serial = "LC2000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module2 = MockModule(index, lc2_name, desc, module_type, lc2_slot, serial)
    module2.set_midplane_ip()
    chassis.module_list.append(module2)

    # Supervisor ModuleUpdater
    sup_module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, sup_slot, sup_slot)
    sup_module_updater.modules_num_update()
    # Mock hostname table update for the line card LINE-CARD0
    hostname = "lc1-host-00"
    num_asics = 1
    hostname_fvs = swsscommon.FieldValuePairs([(CHASSIS_MODULE_INFO_SLOT_FIELD, str(lc_slot)), 
                                    (CHASSIS_MODULE_INFO_HOSTNAME_FIELD, hostname),
                                    (CHASSIS_MODULE_INFO_NUM_ASICS_FIELD, str(num_asics))])
    sup_module_updater.hostname_table.set(lc_name, hostname_fvs)

    # Set linecard initial state to ONLINE
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    sup_module_updater.module_db_update()

    fvs = sup_module_updater.module_table.get(lc_name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # Change linecard module status to OFFLINE
    status = ModuleBase.MODULE_STATUS_OFFLINE
    module.set_oper_status(status)
    sup_module_updater.module_db_update()

    fvs = sup_module_updater.module_table.get(lc_name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # Mock >= CHASSIS_DB_CLEANUP_MODULE_DOWN_PERIOD module down period for LINE-CARD0
    down_module_key = lc_name+"|"+hostname
    module_down_time = sup_module_updater.down_modules[down_module_key]["down_time"]
    sup_module_updater.down_modules[down_module_key]["down_time"] = module_down_time - ((CHASSIS_DB_CLEANUP_MODULE_DOWN_PERIOD+10)*60)

    # Mock >= CHASSIS_DB_CLEANUP_MODULE_DOWN_PERIOD module down period for LINE-CARD1
    down_module_key = lc2_name+"|"
    assert  down_module_key not in sup_module_updater.down_modules.keys()
    
    sup_module_updater.module_down_chassis_db_cleanup()

def test_chassis_db_bootup_with_empty_slot():
    chassis = MockChassis()

    #Supervisor
    index = 0
    sup_name = "SUPERVISOR0"
    desc = "Supervisor card"
    sup_slot = 16
    serial = "RP1000101"
    module_type = ModuleBase.MODULE_TYPE_SUPERVISOR
    supervisor = MockModule(index, sup_name, desc, module_type, sup_slot, serial)
    supervisor.set_midplane_ip()
    chassis.module_list.append(supervisor)

    #Linecard 0. Host name will be pushed for this to make clean up happen
    index = 1
    lc_name = "LINE-CARD0"
    desc = "36 port 400G card"
    lc_slot = 1
    serial = "LC1000101"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module = MockModule(index, lc_name, desc, module_type, lc_slot, serial)
    module.set_midplane_ip()
    status = ModuleBase.MODULE_STATUS_ONLINE
    module.set_oper_status(status)
    chassis.module_list.append(module)

    #Linecard 1. Host name will not be pushed for this so that clean up will not happen
    index = 2
    lc2_name = u"LINE-CARD1"
    desc = "Unavailable'"
    lc2_slot = 2
    serial = "N/A"
    module_type = ModuleBase.MODULE_TYPE_LINE
    module2 = MockModule(index, lc2_name, desc, module_type, lc2_slot, serial)
    module2.set_midplane_ip()
    status = ModuleBase.MODULE_STATUS_EMPTY
    module2.set_oper_status(status)
    chassis.module_list.append(module2)

    # Supervisor ModuleUpdater
    sup_module_updater = ModuleUpdater(SYSLOG_IDENTIFIER, chassis, sup_slot, sup_slot)
    sup_module_updater.modules_num_update()
    
    sup_module_updater.module_db_update()

    # check LC1 STATUS ONLINE in module table
    fvs = sup_module_updater.module_table.get(lc_name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert ModuleBase.MODULE_STATUS_ONLINE == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # check LC2 STATUS EMPTY in module table 
    fvs = sup_module_updater.module_table.get(lc2_name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert ModuleBase.MODULE_STATUS_EMPTY == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]

    # Both should no tbe in down_module keys.
    
    down_module_lc1_key = lc_name+"|"
    assert  down_module_lc1_key not in sup_module_updater.down_modules.keys()
    down_module_lc2_key = lc_name+"|"
    assert  down_module_lc2_key not in sup_module_updater.down_modules.keys()

    # Change linecard module1 status to OFFLINE
    status = ModuleBase.MODULE_STATUS_OFFLINE
    module.set_oper_status(status)
    sup_module_updater.module_db_update()

    fvs = sup_module_updater.module_table.get(lc_name)
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]
    assert down_module_lc1_key in sup_module_updater.down_modules.keys()


def test_smartswitch_time_format():
    chassis = MockSmartSwitchChassis()
    chassis_state_db = MagicMock()
    mod_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    mod_updater.chassis_state_db = chassis_state_db
    mod_updater.chassis_state_db.hgetall = MagicMock(return_value={})
    mod_updater.chassis_state_db.hset = MagicMock()
    date_format = "%a %b %d %I:%M:%S %p UTC %Y"
    def is_valid_date(date_str):
            try:
                datetime.strptime(date_str, date_format)
            except ValueError:
                # Parsing failed and we are unable to obtain the time
                return False
            return True
    mod_updater.update_dpu_state("DPU1", 'up')
    date_value = None
    for args in (mod_updater.chassis_state_db.hset.call_args_list):
        if args[0][0] == "DPU1" and args[0][1] == "dpu_midplane_link_time":
            date_value = args[0][2]
    if not date_value:
        AssertionError("Date is not set!")
    assert is_valid_date(date_value)

def test_smartswitch_moduleupdater_midplane_state_change(midplane_reason_dir):
    """Test that when midplane goes down, control plane and data plane states are set to down"""
    chassis = MockSmartSwitchChassis()
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)
    module.set_midplane_ip()
    chassis.module_list.append(module)

    # Create the updater
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.midplane_initialized = True

    # Mock chassis_state_db
    chassis_state_db = {}
    def mock_hset(key, field, value):
        if key not in chassis_state_db:
            chassis_state_db[key] = {}
        chassis_state_db[key][field] = value

    def mock_hget(key, field):
        if key in chassis_state_db and field in chassis_state_db[key]:
            return chassis_state_db[key][field]
        return None

    with patch.object(module_updater, 'chassis_state_db') as mock_db:
        mock_db.hset = MagicMock(side_effect=mock_hset)
        mock_db.hget = MagicMock(side_effect=mock_hget)

        # Initially set midplane as up
        module.set_midplane_reachable(True)
        module_updater.check_midplane_reachability()

        # Verify initial state
        key = "DPU_STATE|" + name
        assert chassis_state_db[key]["dpu_midplane_link_state"] == "up"
        chassis_state_db[key].update({
            CP_UPDATE_TIME: "original-cp-time",
            "dpu_control_plane_reason": "original-cp-reason",
            DP_UPDATE_TIME: "original-dp-time",
            "dpu_data_plane_reason": "original-dp-reason",
        })

        # Now set midplane as down
        module.set_midplane_reachable(False)
        module_updater.check_midplane_reachability()

        # Verify all states are set to down
        assert chassis_state_db[key]["dpu_midplane_link_state"] == "down"
        assert chassis_state_db[key]["dpu_control_plane_state"] == "down"
        assert chassis_state_db[key]["dpu_data_plane_state"] == "down"
        assert chassis_state_db[key][CP_UPDATE_TIME] == "original-cp-time"
        assert chassis_state_db[key]["dpu_control_plane_reason"] == "original-cp-reason"
        assert chassis_state_db[key][DP_UPDATE_TIME] == "original-dp-time"
        assert chassis_state_db[key]["dpu_data_plane_reason"] == "original-dp-reason"

        # Verify timestamps are set
        assert "dpu_midplane_link_time" in chassis_state_db[key]

        # Verify time format
        date_format = "%a %b %d %I:%M:%S %p UTC %Y"
        def is_valid_date(date_str):
            try:
                datetime.strptime(date_str, date_format)
                return True
            except ValueError:
                return False

        assert is_valid_date(chassis_state_db[key]["dpu_midplane_link_time"])


def _make_smartswitch_updater_with_dpu(name="DPU0"):
    """Helper: build a SmartSwitchModuleUpdater with a single DPU module."""
    chassis = MockSmartSwitchChassis()
    module = MockModule(0, name, "DPU Module 0", ModuleBase.MODULE_TYPE_DPU, 0, "{}-0000".format(name))
    module.set_midplane_ip()
    chassis.module_list.append(module)
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    module_updater.midplane_initialized = True
    return module_updater, module


@pytest.fixture
def midplane_reason_dir(tmp_path, monkeypatch):
    """Redirect persisted midplane-down-reason files to a per-test temp dir."""
    import chassisd
    monkeypatch.setattr(chassisd, "MODULE_REBOOT_CAUSE_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("platform_reason, expected", [
    # (major, "") -> only the major part is rendered
    ((ChassisBase.REBOOT_CAUSE_THERMAL_OVERLOAD_ASIC, ""),
     "Unplanned: 'Thermal Overload: ASIC'"),
    # (major, minor) -> both parts are rendered
    ((ChassisBase.REBOOT_CAUSE_HARDWARE_OTHER, "kernel panic"),
     "Unplanned: 'Hardware - Other, kernel panic'"),
    # falsy-but-valid minor (0) is kept; guards against `if minor` truthiness,
    # only None/"" should omit the minor part.
    ((ChassisBase.REBOOT_CAUSE_HARDWARE_OTHER, 0),
     "Unplanned: 'Hardware - Other, 0'"),
])
def test_resolve_midplane_down_reason_unplanned(platform_reason, expected, midplane_reason_dir):
    """Unplanned down: platform reason tuple is rendered as Unplanned: '<reason>'."""
    module_updater, module = _make_smartswitch_updater_with_dpu()
    module.clear_module_state_transition("DPU0")
    module.set_midplane_down_reason(platform_reason)

    reason = module_updater._resolve_midplane_down_reason(module, "DPU0")
    assert reason == expected


def test_resolve_midplane_down_reason_unplanned_unknown(midplane_reason_dir):
    """Unplanned down: no platform reason falls back to Unknown."""
    module_updater, module = _make_smartswitch_updater_with_dpu()
    module.clear_module_state_transition("DPU0")
    module.set_midplane_down_reason(None)

    reason = module_updater._resolve_midplane_down_reason(module, "DPU0")
    assert reason == "Unplanned: 'Unknown'"


def test_resolve_midplane_down_reason_planned(midplane_reason_dir):
    """Planned down: transition flag set -> Planned: '<transition_type>'."""
    module_updater, module = _make_smartswitch_updater_with_dpu()
    module.set_module_state_transition("DPU0", "shutdown")

    module_updater.state_db.hget = MagicMock(return_value="shutdown")
    reason = module_updater._resolve_midplane_down_reason(module, "DPU0")

    assert reason == "Planned: 'shutdown'"


def test_resolve_midplane_down_reason_missing_transition_type(midplane_reason_dir):
    """A disappearing transition type must not produce Planned: 'unknown'."""
    module_updater, module = _make_smartswitch_updater_with_dpu()
    module.set_module_state_transition("DPU0", "shutdown")
    module.set_midplane_down_reason((ChassisBase.REBOOT_CAUSE_HARDWARE_OTHER, "link failure"))
    module_updater.state_db.hget = MagicMock(return_value=None)

    reason = module_updater._resolve_midplane_down_reason(module, "DPU0")

    assert reason == "Unplanned: 'Hardware - Other, link failure'"


def test_midplane_down_state_retried_after_partial_db_failure(midplane_reason_dir):
    """A partial DB write leaves state unchanged so the next poll retries the full update."""
    module_updater, module = _make_smartswitch_updater_with_dpu()
    module.clear_module_state_transition("DPU0")
    module.set_midplane_down_reason((ChassisBase.REBOOT_CAUSE_HARDWARE_OTHER, "link failure"))
    module.set_midplane_reachable(False)
    key = "DPU_STATE|DPU0"
    chassis_state_db = {key: {"dpu_midplane_link_state": "up"}}
    fail_cp_write = [True]

    def mock_hset(db_key, field, value):
        if field == CP_STATE and fail_cp_write[0]:
            fail_cp_write[0] = False
            raise RuntimeError("DB write failed")
        chassis_state_db.setdefault(db_key, {})[field] = value

    def mock_hget(db_key, field):
        return chassis_state_db.get(db_key, {}).get(field)

    with patch.object(module_updater, 'chassis_state_db') as mock_db:
        mock_db.hset = MagicMock(side_effect=mock_hset)
        mock_db.hget = MagicMock(side_effect=mock_hget)

        module_updater.check_midplane_reachability()
        assert chassis_state_db[key]["dpu_midplane_link_state"] == "up"

        module_updater.check_midplane_reachability()
        assert chassis_state_db[key]["dpu_midplane_link_state"] == "down"
        assert chassis_state_db[key]["dpu_midplane_link_reason"] == "Unplanned: 'Hardware - Other, link failure'"
        assert chassis_state_db[key][CP_STATE] == "down"
        assert chassis_state_db[key][DP_STATE] == "down"


def test_midplane_down_reason_persisted_to_file_and_cleared(midplane_reason_dir):
    """Full lifecycle: down persists the reason to file, restart reads it back, up clears it."""
    module_updater, module = _make_smartswitch_updater_with_dpu()
    module.clear_module_state_transition("DPU0")
    module.set_midplane_down_reason((ChassisBase.REBOOT_CAUSE_THERMAL_OVERLOAD_ASIC, ""))
    path = module_updater._midplane_reason_path("DPU0")

    chassis_state_db = {}

    def mock_hset(key, field, value):
        chassis_state_db.setdefault(key, {})[field] = value

    def mock_hget(key, field):
        return chassis_state_db.get(key, {}).get(field)

    with patch.object(module_updater, 'chassis_state_db') as mock_db:
        mock_db.hset = MagicMock(side_effect=mock_hset)
        mock_db.hget = MagicMock(side_effect=mock_hget)

        module.set_midplane_reachable(False)
        module_updater.check_midplane_reachability()
        key = "DPU_STATE|DPU0"
        with open(path) as f:
            assert f.read().strip() == "Unplanned: 'Thermal Overload: ASIC'"
        assert chassis_state_db[key]["dpu_midplane_link_state"] == "down"
        assert chassis_state_db[key]["dpu_midplane_link_reason"] == "Unplanned: 'Thermal Overload: ASIC'"

        # Repeated down polls keep the first reason even if the live reason changes.
        module.set_midplane_down_reason((ChassisBase.REBOOT_CAUSE_HARDWARE_OTHER, "boom"))
        module_updater.check_midplane_reachability()
        assert chassis_state_db[key]["dpu_midplane_link_reason"] == "Unplanned: 'Thermal Overload: ASIC'"
        with open(path) as f:
            assert f.read().strip() == "Unplanned: 'Thermal Overload: ASIC'"

        restarted_updater, restarted_module = _make_smartswitch_updater_with_dpu()
        restarted_module.clear_module_state_transition("DPU0")
        restarted_module.set_midplane_down_reason((ChassisBase.REBOOT_CAUSE_HARDWARE_OTHER, "boom"))
        resolved = restarted_updater._resolve_midplane_down_reason(restarted_module, "DPU0")
        assert resolved == "Unplanned: 'Thermal Overload: ASIC'"

        # Up: persisted reason file removed.
        module.set_midplane_reachable(True)
        module_updater.check_midplane_reachability()
        assert not os.path.exists(path)
        assert chassis_state_db[key]["dpu_midplane_link_state"] == "up"
        assert chassis_state_db[key]["dpu_midplane_link_reason"] == ""


def test_submit_dpu_callback():
    """Test that submit_dpu_callback calls the right functions in the correct order"""
    chassis = MockSmartSwitchChassis()

    # DPU0 details
    index = 0
    name = "DPU0"
    desc = "DPU Module 0"
    slot = 0
    serial = "DPU0-0000"
    module_type = ModuleBase.MODULE_TYPE_DPU
    module = MockModule(index, name, desc, module_type, slot, serial)

    # Set initial state
    status = ModuleBase.MODULE_STATUS_PRESENT
    module.set_oper_status(status)
    chassis.module_list.append(module)

    # Create module updater and daemon
    module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
    daemon_chassisd.module_updater = module_updater
    module_updater.module_table.get = MagicMock(return_value=(True, []))

    # Test MODULE_ADMIN_DOWN scenario - set_admin_state_gracefully is called
    with patch.object(module, 'set_admin_state_gracefully') as mock_set_admin_state_gracefully:
        daemon_chassisd.submit_dpu_callback(index, MODULE_ADMIN_DOWN)
        mock_set_admin_state_gracefully.assert_called_once_with(MODULE_ADMIN_DOWN)

def test_chassis_daemon_assertion():
    chassis = MockChassis()

    # Needs to be supervisor slot for config_manager thread to be spawned
    chassis.get_supervisor_slot = Mock()
    chassis.get_supervisor_slot.return_value = 0
    chassis.get_my_slot = Mock()
    chassis.get_my_slot.return_value = 0

    daemon_chassisd = ChassisdDaemon(SYSLOG_IDENTIFIER, chassis)

    # Reduce wait time from 10s to 1s to speed up test
    daemon_chassisd.loop_interval=1

    # Simulate an Assertion occurring in the forever loop
    with patch('chassisd.ModuleUpdater.module_db_update', MagicMock(side_effect=AssertionError)):
        with pytest.raises(AssertionError):
            daemon_chassisd.run()

    # Wait for the child thread to die
    start = time.time()
    timeout = 30
    while time.time() - start < timeout:
        if not daemon_chassisd.config_manager._task_process.is_alive():
            break
        time.sleep(1)
    else:
        assert False, "config_manager thread never died"
