import os
import sys
import mock
import tempfile
import json
from imp import load_source

from mock import Mock, MagicMock, patch, mock_open
from sonic_py_common import daemon_base

from .mock_platform import MockChassis, MockSmartSwitchChassis, MockModule
from .mock_module_base import ModuleBase

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
load_source('chassisd', scripts_path + '/chassisd')
from chassisd import *


CHASSIS_MODULE_INFO_NAME_FIELD = 'name'
CHASSIS_MODULE_INFO_DESC_FIELD = 'desc'
CHASSIS_MODULE_INFO_SLOT_FIELD = 'slot'
CHASSIS_MODULE_INFO_OPERSTATUS_FIELD = 'oper_status'
CHASSIS_MODULE_INFO_SERIAL_FIELD = 'serial'

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
    assert desc == fvs[CHASSIS_MODULE_INFO_DESC_FIELD]
    assert status == fvs[CHASSIS_MODULE_INFO_OPERSTATUS_FIELD]
    assert serial == fvs[CHASSIS_MODULE_INFO_SERIAL_FIELD]

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

    # Mock dependent methods
    with patch.object(module_updater, 'retrieve_dpu_reboot_time', return_value="2024-11-19T00:00:00") \
            as mock_retrieve_reboot_time, \
         patch.object(module_updater, '_is_first_boot', return_value=False) as mock_is_first_boot, \
         patch.object(module_updater, 'persist_dpu_reboot_cause') as mock_persist_reboot_cause, \
         patch.object(module_updater, 'update_dpu_reboot_cause_to_db') as mock_update_reboot_db, \
         patch("os.makedirs") as mock_makedirs, \
         patch("builtins.open", mock_open()) as mock_file, \
         patch.object(module_updater, '_get_history_path',
                      return_value="/tmp/prev_reboot_time.txt") as mock_get_history_path:

        # Transition from ONLINE to OFFLINE
        offline_status = ModuleBase.MODULE_STATUS_OFFLINE
        module.set_oper_status(offline_status)
        module_updater.module_db_update()
        assert module.get_oper_status() == offline_status

        # Reset mocks for next transition
        mock_file.reset_mock()
        mock_makedirs.reset_mock()
        mock_persist_reboot_cause.reset_mock()
        mock_update_reboot_db.reset_mock()

        # Ensure ONLINE transition is handled correctly
        online_status = ModuleBase.MODULE_STATUS_ONLINE
        module.set_oper_status(online_status)
        module_updater.module_db_update()
        assert module.get_oper_status() == online_status

        # Validate mock calls for ONLINE transition
        mock_persist_reboot_cause.assert_called_once()
        mock_update_reboot_db.assert_called_once()

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

    config_updater = SmartSwitchModuleConfigUpdater(SYSLOG_IDENTIFIER, chassis)
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

    config_updater = SmartSwitchModuleConfigUpdater(SYSLOG_IDENTIFIER, chassis)
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

    config_updater = SmartSwitchModuleConfigUpdater(SYSLOG_IDENTIFIER, chassis)
    admin_state = 0
    config_updater.module_config_update(name, admin_state)
    assert module.get_admin_state() == admin_state

    admin_state = 1
    config_updater.module_config_update(name, admin_state)
    assert module.get_admin_state() == admin_state


    @patch("your_module.glob.glob")
    @patch("your_module.open", new_callable=mock_open)
    def test_update_dpu_reboot_cause_to_db(self, mock_open, mock_glob):
        # Set up the SmartSwitchModuleUpdater and test inputs
        module_updater = SmartSwitchModuleUpdater(SYSLOG_IDENTIFIER, chassis=MagicMock())
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
            mock_log_warning.assert_not_called()  # No warnings expected
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


def test_smartswitch_module_db_update():
    chassis = MockSmartSwitchChassis()
    reboot_cause = "Power loss"
    key = "DPU0"
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
    expected_path = "/host/reboot-cause/module/reboot_cause/dpu0/history/2024_11_13_15_06_40_reboot_cause.txt"
    symlink_path = "/host/reboot-cause/module/dpu0/previous-reboot-cause.json"

    with patch("os.path.exists", return_value=True), \
         patch("os.makedirs") as mock_makedirs, \
         patch("builtins.open", mock_open(read_data="Power loss")) as mock_file, \
         patch("os.remove") as mock_remove, \
         patch("os.symlink") as mock_symlink:

        # Call the function to test
        module_updater.persist_dpu_reboot_cause(reboot_cause, key)
        module_updater._is_first_boot(name)
        module_updater.persist_dpu_reboot_time(name)
        module_updater.update_dpu_reboot_cause_to_db(name)


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

