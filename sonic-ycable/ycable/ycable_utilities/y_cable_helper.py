"""
    y_cable_helper.py
    helper utlities configuring y_cable for xcvrd daemon
"""

import datetime
import os
import re
import threading
import time

from importlib import import_module

from sonic_py_common import daemon_base, logger
from sonic_py_common import multi_asic
from sonic_y_cable import y_cable_vendor_mapping
from swsscommon import swsscommon


SELECT_TIMEOUT = 1000

y_cable_platform_sfputil = None
y_cable_platform_chassis = None

SYSLOG_IDENTIFIER = "y_cable_helper"

helper_logger = logger.Logger(SYSLOG_IDENTIFIER)


# SFP status definition, shall be aligned with the definition in get_change_event() of ChassisBase
SFP_STATUS_REMOVED = '0'
SFP_STATUS_INSERTED = '1'

# SFP error codes, stored as strings. Can add more as needed.
SFP_STATUS_ERR_I2C_STUCK = '2'
SFP_STATUS_ERR_BAD_EEPROM = '3'
SFP_STATUS_ERR_UNSUPPORTED_CABLE = '4'
SFP_STATUS_ERR_HIGH_TEMP = '5'
SFP_STATUS_ERR_BAD_CABLE = '6'

# Store the error codes in a set for convenience
errors_block_eeprom_reading = {
    SFP_STATUS_ERR_I2C_STUCK,
    SFP_STATUS_ERR_BAD_EEPROM,
    SFP_STATUS_ERR_UNSUPPORTED_CABLE,
    SFP_STATUS_ERR_HIGH_TEMP,
    SFP_STATUS_ERR_BAD_CABLE
}
y_cable_port_instances = {}
y_cable_port_locks = {}


Y_CABLE_STATUS_NO_TOR_ACTIVE = 0
Y_CABLE_STATUS_TORA_ACTIVE = 1
Y_CABLE_STATUS_TORB_ACTIVE = 2

y_cable_switch_state_values = {
    Y_CABLE_STATUS_NO_TOR_ACTIVE,
    Y_CABLE_STATUS_TORA_ACTIVE,
    Y_CABLE_STATUS_TORB_ACTIVE
}

MUX_CABLE_STATIC_INFO_TABLE = "MUX_CABLE_STATIC_INFO"
MUX_CABLE_INFO_TABLE = "MUX_CABLE_INFO"

PHYSICAL_PORT_MAPPING_ERROR = -1
PORT_INSTANCE_ERROR = -1

port_mapping_error_values = {
  PHYSICAL_PORT_MAPPING_ERROR,
  PORT_INSTANCE_ERROR
}

def format_mapping_identifier(string):
    """
    Takes an arbitrary string and creates a valid entity for port mapping file.
    The input could contain trailing and leading spaces, upper cases etc.
    Convert them to what is defined in the y_cable vendor_mapping file.

    """

    if not isinstance(string, str):
        helper_logger.log_warning(
            "Error: mapping identifier is not a string {}".format(string))
        return


    # create a working copy (and make it lowercase, while we're at it)
    s = string.lower()

    # remove leading and trailing whitespace
    s = s.strip()

    # Replace whitespace with underscores
    # Make spaces into underscores
    s = re.sub(r'\s+', '_', s)

    return s

# Find out the underneath physical port list by logical name


def logical_port_name_to_physical_port_list(port_name):
    if port_name.startswith("Ethernet"):
        if y_cable_platform_sfputil.is_logical_port(port_name):
            return y_cable_platform_sfputil.get_logical_to_physical(port_name)
        else:
            helper_logger.log_error("Invalid port '%s'" % port_name)
            return None
    else:
        return [int(port_name)]


def y_cable_wrapper_get_presence(physical_port):
    if y_cable_platform_chassis is not None:
        try:
            return y_cable_platform_chassis.get_sfp(physical_port).get_presence()
        except NotImplementedError:
            pass
    return y_cable_platform_sfputil.get_presence(physical_port)



def hook_y_cable_simulated(target):
    """
    Decorator to add hook for using the simulated y_cable driver.
    This decorator checks existence of the configuration file required by the simulated y_cable driver. If the
    configuration file is found, then override the "manufacturer" and "model" fields with value "microsoft" and
    "simulated" in the collected transceiver info dict. Consequently, instance of the simulated y_cable driver
    class will be initialized.
    When the configuration file is not found on system, then just return the original transceiver info to initialize
    instance of y_cable driver class of whatever actually plugged physical y_cable.
    For test systems using simulated y_cable, we can just inject the simulated y_cable driver config file then
    restart the pmon service before testing starts.

    Args:
        target (function): The function collecting transceiver info.
    """

    MUX_SIMULATOR_CONFIG_FILE = "/etc/sonic/mux_simulator.json"
    VENDOR = "microsoft"
    MODEL = "simulated"

    def wrapper(*args, **kwargs):
        res = target(*args, **kwargs)
        if os.path.exists(MUX_SIMULATOR_CONFIG_FILE):
            res["manufacturer"] = VENDOR
            res["model"] = MODEL
        return res

    wrapper.__name__ = target.__name__

    return wrapper

@hook_y_cable_simulated
def y_cable_wrapper_get_transceiver_info(physical_port):
    if y_cable_platform_chassis is not None:
        try:
            return y_cable_platform_chassis.get_sfp(physical_port).get_transceiver_info()
        except NotImplementedError:
            pass
    return y_cable_platform_sfputil.get_transceiver_info_dict(physical_port)

def get_ycable_physical_port_from_logical_port(logical_port_name):

    physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)

    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        if y_cable_wrapper_get_presence(physical_port):

            return physical_port
        else:
            helper_logger.log_warning(
                "Error: Could not establish presence for  Y cable port {} while retreiving physical port mapping".format(logical_port_name))
            return -1

    else:
        # Y cable ports should always have
        # one to one mapping of physical-to-logical
        # This should not happen
        helper_logger.log_warning(
            "Error: Retreived multiple ports for a Y cable table port {} while retreiving physical port mapping".format(logical_port_name))
        return -1

def get_ycable_port_instance_from_logical_port(logical_port_name):

    physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)

    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        if y_cable_wrapper_get_presence(physical_port):

            port_instance = y_cable_port_instances.get(physical_port)
            if port_instance is None:
                helper_logger.log_error(
                    "Error: Could not get port instance from the dict for Y cable port {}".format(logical_port_name))
                return PORT_INSTANCE_ERROR
            return port_instance
        else:
            helper_logger.log_warning(
                "Error: Could not establish presence for  Y cable port {} while trying to toggle the mux".format(logical_port_name))
            return PORT_INSTANCE_ERROR

    else:
        # Y cable ports should always have
        # one to one mapping of physical-to-logical
        # This should not happen
        helper_logger.log_warning(
            "Error: Retreived multiple ports for a Y cable table port {} while trying to toggle the mux".format(logical_port_name))
        return -1

def set_show_firmware_fields(port, mux_info_dict, xcvrd_show_fw_rsp_tbl):
    fvs = swsscommon.FieldValuePairs(
        [('version_self_active', str(mux_info_dict["version_self_active"])),
         ('version_self_inactive', str(mux_info_dict["version_self_inactive"])),
         ('version_self_next', str(mux_info_dict["version_self_next"])),
         ('version_peer_active', str(mux_info_dict["version_peer_active"])),
         ('version_peer_inactive', str(mux_info_dict["version_peer_inactive"])),
         ('version_peer_next', str(mux_info_dict["version_peer_next"])),
         ('version_nic_active', str(mux_info_dict["version_nic_active"])),
         ('version_nic_inactive', str(mux_info_dict["version_nic_inactive"])),
         ('version_nic_next', str(mux_info_dict["version_nic_next"]))
        ])
    xcvrd_show_fw_rsp_tbl.set(port, fvs)

    return 0


def set_result_and_delete_port(result, actual_result, command_table, response_table, port):
    fvs = swsscommon.FieldValuePairs([(result, str(actual_result))])
    response_table.set(port, fvs)
    command_table._del(port)

# Delete port from Y cable status table
def delete_port_from_y_cable_table(logical_port_name, y_cable_tbl):
    if y_cable_tbl is not None:
        y_cable_tbl._del(logical_port_name)


def update_table_mux_status_for_response_tbl(table_name, status, logical_port_name):
    fvs = swsscommon.FieldValuePairs([('response', status)])
    table_name.set(logical_port_name, fvs)

    helper_logger.log_debug("Y_CABLE_DEBUG: Successful in returning probe port status {}".format(logical_port_name))


def update_table_mux_status_for_statedb_port_tbl(table_name, status, read_side, active_side, logical_port_name):
    fvs = swsscommon.FieldValuePairs([('state', status),
                                      ('read_side', str(read_side)),
                                      ('active_side', str(active_side))])
    table_name.set(logical_port_name, fvs)


def y_cable_toggle_mux_torA(physical_port):
    port_instance = y_cable_port_instances.get(physical_port)
    if port_instance is None:
        helper_logger.log_error(
            "Error: Could not get port instance for read side for  Y cable port {} {}".format(physical_port, threading.currentThread().getName()))
        return -1

    try:
        update_status = port_instance.toggle_mux_to_tor_a()
    except Exception as e:
        update_status = -1
        helper_logger.log_warning("Failed to execute the toggle mux ToR A API for port {} due to {} {}".format(physical_port, repr(e) , threading.currentThread().getName()))

    helper_logger.log_debug("Y_CABLE_DEBUG: Status of toggling mux to ToR A for port {} status {} {}".format(physical_port, update_status, threading.currentThread().getName()))
    if update_status is True:
        return 1
    else:
        helper_logger.log_warning(
            "Error: Could not toggle the mux for port {} to torA write eeprom failed".format(physical_port))
        return -1


def y_cable_toggle_mux_torB(physical_port):
    port_instance = y_cable_port_instances.get(physical_port)
    if port_instance is None:
        helper_logger.log_error("Error: Could not get port instance for read side for  Y cable port {} {}".format(physical_port, threading.currentThread().getName()))
        return -1

    try:
        update_status = port_instance.toggle_mux_to_tor_b()
    except Exception as e:
        update_status = -1
        helper_logger.log_warning("Failed to execute the toggle mux ToR B API for port {} due to {} {}".format(physical_port,repr(e), threading.currentThread().getName()))

    helper_logger.log_debug("Y_CABLE_DEBUG: Status of toggling mux to ToR B for port {} {} {}".format(physical_port, update_status, threading.currentThread().getName()))
    if update_status is True:
        return 2
    else:
        helper_logger.log_warning(
            "Error: Could not toggle the mux for port {} to torB write eeprom failed".format(physical_port))
        return -1


def update_tor_active_side(read_side, state, logical_port_name):
    physical_port_list = logical_port_name_to_physical_port_list(
        logical_port_name)

    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        if y_cable_wrapper_get_presence(physical_port):
            if int(read_side) == 1:
                if state == "active":
                    return y_cable_toggle_mux_torA(physical_port)
                elif state == "standby":
                    return y_cable_toggle_mux_torB(physical_port)
            elif int(read_side) == 2:
                if state == "active":
                    return y_cable_toggle_mux_torB(physical_port)
                elif state == "standby":
                    return y_cable_toggle_mux_torA(physical_port)

            # TODO: Should we confirm that the mux was indeed toggled?

        else:
            helper_logger.log_warning(
                "Error: Could not establish presence for  Y cable port {} while trying to toggle the mux".format(logical_port_name))
            return -1

    else:
        # Y cable ports should always have
        # one to one mapping of physical-to-logical
        # This should not happen
        helper_logger.log_warning(
            "Error: Retreived multiple ports for a Y cable table port {} while trying to toggle the mux".format(logical_port_name))
        return -1


def update_appdb_port_mux_cable_response_table(logical_port_name, asic_index, appl_db, read_side):

    status = None
    y_cable_response_tbl = {}

    y_cable_response_tbl[asic_index] = swsscommon.Table(
        appl_db[asic_index], "MUX_CABLE_RESPONSE_TABLE")
    physical_port_list = logical_port_name_to_physical_port_list(
        logical_port_name)

    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        if y_cable_wrapper_get_presence(physical_port):

            port_instance = y_cable_port_instances.get(physical_port)
            if port_instance is None or port_instance == -1:
                status = 'unknown'
                update_table_mux_status_for_response_tbl(y_cable_response_tbl[asic_index], status, logical_port_name)
                helper_logger.log_error(
                    "Error: Could not get port instance to perform update appdb for read side for Y cable port {}".format(logical_port_name))
                return

            if read_side is None:

                status = 'unknown'
                update_table_mux_status_for_response_tbl(y_cable_response_tbl[asic_index], status, logical_port_name)
                helper_logger.log_warning(
                    "Error: Could not get read side to perform update appdb for mux cable port probe command logical port {} and physical port {}".format(logical_port_name, physical_port))
                return

            active_side = None
            try:
                active_side = port_instance.get_mux_direction()
            except Exception as e:
                active_side = -1
                helper_logger.log_warning("Failed to execute the get_mux_direction for port {} due to {}".format(physical_port,repr(e)))

            if active_side is None or active_side == port_instance.EEPROM_ERROR or active_side < 0 :

                status = 'unknown'
                update_table_mux_status_for_response_tbl(y_cable_response_tbl[asic_index], status, logical_port_name)
                helper_logger.log_warning(
                    "Error: Could not get active side to perform update appdb for mux cable port probe command logical port {} and physical port {}".format(logical_port_name, physical_port))
                return

            if read_side == active_side and (active_side == 1 or active_side == 2):
                status = 'active'
            elif read_side != active_side and (active_side == 1 or active_side == 2):
                status = 'standby'
            else:
                status = 'unknown'
                helper_logger.log_warning(
                    "Error: Could not get state to perform update appdb for mux cable port probe command logical port {} and physical port {}".format(logical_port_name, physical_port))

            helper_logger.log_debug("Y_CABLE_DEBUG: notifying a probe for port status {} {}".format(logical_port_name, status))

            update_table_mux_status_for_response_tbl(y_cable_response_tbl[asic_index], status, logical_port_name)

        else:

            status = 'unknown'
            update_table_mux_status_for_response_tbl(y_cable_response_tbl[asic_index], status, logical_port_name)
            helper_logger.log_warning(
                "Error: Could not establish presence for Y cable port {} while responding to command probe".format(logical_port_name))
    else:
        # Y cable ports should always have
        # one to one mapping of physical-to-logical
        # This should not happen

        status = 'unknown'
        update_table_mux_status_for_response_tbl(y_cable_response_tbl[asic_index], status, logical_port_name)
        helper_logger.log_warning(
            "Error: Retreived multiple ports for a Y cable port {} while responding to command probe".format(logical_port_name))


def read_y_cable_and_update_statedb_port_tbl(logical_port_name, mux_config_tbl):
    physical_port_list = logical_port_name_to_physical_port_list(
        logical_port_name)

    read_side = None
    active_side = None
    status = None
    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        if y_cable_wrapper_get_presence(physical_port):

            port_instance = y_cable_port_instances.get(physical_port)
            if port_instance is None or port_instance == -1:
                read_side = active_side = -1
                update_table_mux_status_for_statedb_port_tbl(
                    mux_config_tbl, "unknown", read_side, active_side, logical_port_name)
                helper_logger.log_error(
                    "Error: Could not get port instance to perform read_y_cable update state db for read side for  Y cable port {}".format(logical_port_name))
                return

            with y_cable_port_locks[physical_port]:
                try:
                    read_side = port_instance.get_read_side()
                except Exception as e:
                    read_side = None
                    helper_logger.log_warning("Failed to execute the get_read_side for port {} due to {}".format(physical_port,repr(e)))

            if read_side is None or read_side < 0 or read_side == port_instance.EEPROM_ERROR:
                read_side = active_side = -1
                update_table_mux_status_for_statedb_port_tbl(
                    mux_config_tbl, "unknown", read_side, active_side, logical_port_name)
                helper_logger.log_error(
                    "Error: Could not establish the read side for Y cable port {} to perform read_y_cable update state db".format(logical_port_name))
                return

            with y_cable_port_locks[physical_port]:
                try:
                    active_side = port_instance.get_mux_direction()
                except Exception as e:
                    active_side = None
                    helper_logger.log_warning("Failed to execute the get_mux_direction for port {} due to {}".format(physical_port,repr(e)))

            if active_side is None or active_side not in y_cable_switch_state_values:
                read_side = active_side = -1
                update_table_mux_status_for_statedb_port_tbl(
                    mux_config_tbl, "unknown", read_side, active_side, logical_port_name)
                helper_logger.log_error(
                    "Error: Could not establish the active side for  Y cable port {} to perform read_y_cable update state db".format(logical_port_name))
                return

            if read_side == active_side and (active_side == 1 or active_side == 2):
                status = 'active'
            elif read_side != active_side and (active_side == 1 or active_side == 2):
                status = 'standby'
            else:
                status = 'unknown'
                helper_logger.log_warning(
                    "Error: Could not establish the active status for Y cable port {} to perform read_y_cable update state db".format(logical_port_name))

            update_table_mux_status_for_statedb_port_tbl(
                mux_config_tbl, status, read_side, active_side, logical_port_name)
            return

        else:
            read_side = active_side = -1
            update_table_mux_status_for_statedb_port_tbl(
                mux_config_tbl, "unknown", read_side, active_side, logical_port_name)
            helper_logger.log_warning(
                "Error: Could not establish presence for  Y cable port {} to perform read_y_cable update state db".format(logical_port_name))
    else:
        # Y cable ports should always have
        # one to one mapping of physical-to-logical
        # This should not happen
        read_side = active_side = -1
        update_table_mux_status_for_statedb_port_tbl(
            mux_config_tbl, "unknown", read_side, active_side, logical_port_name)
        helper_logger.log_warning(
            "Error: Retreived multiple ports for a Y cable port {} to perform read_y_cable update state db".format(logical_port_name))

def create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name):

    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(
            namespace)
        state_db[asic_id] = daemon_base.db_connect(
            "STATE_DB", namespace)
        y_cable_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
        static_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], MUX_CABLE_STATIC_INFO_TABLE)
        mux_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], MUX_CABLE_INFO_TABLE)
    # fill the newly found entry
    read_y_cable_and_update_statedb_port_tbl(
        logical_port_name, y_cable_tbl[asic_index])

def check_identifier_presence_and_update_mux_table_entry(state_db, port_tbl, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name, y_cable_presence):

    global y_cable_port_instances
    global y_cable_port_locks
    (status, fvs) = port_tbl[asic_index].get(logical_port_name)
    if status is False:
        helper_logger.log_warning(
            "Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(logical_port_name, port_tbl[asic_index].getTableName()))
        return

    else:
        # Convert list of tuples to a dictionary
        mux_table_dict = dict(fvs)
        if "state" in mux_table_dict:

            val = mux_table_dict.get("state", None)

            if val in ["active", "auto", "manual", "standby"]:

                # import the module and load the port instance
                physical_port_list = logical_port_name_to_physical_port_list(
                    logical_port_name)

                if len(physical_port_list) == 1:

                    physical_port = physical_port_list[0]
                    if y_cable_wrapper_get_presence(physical_port):
                        port_info_dict = y_cable_wrapper_get_transceiver_info(
                            physical_port)
                        if port_info_dict is not None:
                            vendor = port_info_dict.get('manufacturer')

                            if vendor is None:
                                helper_logger.log_warning(
                                    "Error: Unable to find Vendor name for Transceiver for Y-Cable initiation {}".format(logical_port_name))
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                return

                            model = port_info_dict.get('model')

                            if model is None:
                                helper_logger.log_warning(
                                    "Error: Unable to find model name for Transceiver for Y-Cable initiation {}".format(logical_port_name))
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                return

                            vendor = format_mapping_identifier(vendor)
                            model = format_mapping_identifier(model)
                            module_dir = y_cable_vendor_mapping.mapping.get(vendor)

                            if module_dir is None:
                                helper_logger.log_warning(
                                    "Error: Unable to find module dir name from vendor for Y-Cable initiation {}".format(logical_port_name))
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                return

                            module = module_dir.get(model)
                            if module is None:
                                helper_logger.log_warning(
                                    "Error: Unable to find module name from model for Y-Cable initiation {}".format(logical_port_name))
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                return

                            attr_name = 'sonic_y_cable.' + module
                            try:
                                y_cable_attribute = getattr(import_module(attr_name), 'YCable')
                            except Exception as e:
                                helper_logger.log_warning("Failed to load the attr due to {}".format(repr(e)))
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                return
                            if y_cable_attribute is None:
                                helper_logger.log_warning(
                                    "Error: Unable to import attr name for Y-Cable initiation {}".format(logical_port_name))
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                return
 
                            y_cable_port_instances[physical_port] = y_cable_attribute(physical_port, helper_logger)
                            y_cable_port_locks[physical_port] = threading.Lock()
                            with y_cable_port_locks[physical_port]:
                                try:
                                    vendor_name_api = y_cable_port_instances.get(physical_port).get_vendor()
                                except Exception as e:
                                    helper_logger.log_warning("Failed to call the get_vendor API for port {} due to {}".format(physical_port,repr(e)))
                                    create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                    return

                            if format_mapping_identifier(vendor_name_api) != vendor:
                                y_cable_port_instances.pop(physical_port)
                                y_cable_port_locks.pop(physical_port)
                                create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)
                                helper_logger.log_warning("Error: Y Cable api does not work for {}, {} actual vendor name {}".format(
                                    logical_port_name, vendor_name_api, vendor))
                                return

                            y_cable_asic_table = y_cable_tbl.get(
                                asic_index, None)
                            mux_asic_table = mux_tbl.get(asic_index, None)
                            static_mux_asic_table = static_tbl.get(
                                asic_index, None)
                            if y_cable_presence[0] is True and y_cable_asic_table is not None and mux_asic_table is not None and static_mux_asic_table is not None:
                                # fill in the newly found entry
                                read_y_cable_and_update_statedb_port_tbl(
                                    logical_port_name, y_cable_tbl[asic_index])
                                post_port_mux_info_to_db(
                                    logical_port_name,  mux_tbl[asic_index])
                                post_port_mux_static_info_to_db(
                                    logical_port_name,  static_tbl[asic_index])

                            else:
                                # first create the state db y cable table and then fill in the entry
                                y_cable_presence[:] = [True]
                                namespaces = multi_asic.get_front_end_namespaces()
                                for namespace in namespaces:
                                    asic_id = multi_asic.get_asic_index_from_namespace(
                                        namespace)
                                    state_db[asic_id] = daemon_base.db_connect(
                                        "STATE_DB", namespace)
                                    y_cable_tbl[asic_id] = swsscommon.Table(
                                        state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
                                    static_tbl[asic_id] = swsscommon.Table(
                                        state_db[asic_id], MUX_CABLE_STATIC_INFO_TABLE)
                                    mux_tbl[asic_id] = swsscommon.Table(
                                        state_db[asic_id], MUX_CABLE_INFO_TABLE)
                                # fill the newly found entry
                                read_y_cable_and_update_statedb_port_tbl(
                                    logical_port_name, y_cable_tbl[asic_index])
                                post_port_mux_info_to_db(
                                    logical_port_name,  mux_tbl[asic_index])
                                post_port_mux_static_info_to_db(
                                    logical_port_name,  static_tbl[asic_index])
                        else:
                            helper_logger.log_warning(
                                "Error: Could not get transceiver info dict Y cable port {} while inserting entries".format(logical_port_name))
                            create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)

                    else:
                        helper_logger.log_warning(
                            "Error: Could not establish transceiver presence for a Y cable port {} while inserting entries".format(logical_port_name))
                        create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)

                else:
                    helper_logger.log_warning(
                        "Error: Retreived multiple ports for a Y cable port {} while inserting entries".format(logical_port_name))
                    create_tables_and_insert_mux_unknown_entries(state_db, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name)

            else:
                helper_logger.log_warning(
                    "Could not retreive active or auto value for state kvp for {}, inside MUX_CABLE table".format(logical_port_name))

        else:
            helper_logger.log_warning(
                "Could not retreive state value inside mux_info_dict for {}, inside MUX_CABLE table".format(logical_port_name))


def check_identifier_presence_and_delete_mux_table_entry(state_db, port_tbl, asic_index, logical_port_name, y_cable_presence, delete_change_event):

    y_cable_tbl = {}
    static_tbl, mux_tbl = {}, {}

    # if there is No Y cable do not do anything here
    if y_cable_presence[0] is False:
        return

    (status, fvs) = port_tbl[asic_index].get(logical_port_name)
    if status is False:
        helper_logger.log_warning(
            "Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(logical_port_name, port_tbl[asic_index].getTableName()))
        return

    else:
        # Convert list of tuples to a dictionary
        mux_table_dict = dict(fvs)
        if "state" in mux_table_dict:
            if y_cable_presence[0] is True:
                # delete this entry in the y cable table found and update the delete event
                namespaces = multi_asic.get_front_end_namespaces()
                for namespace in namespaces:
                    asic_id = multi_asic.get_asic_index_from_namespace(namespace)
                    state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
                    y_cable_tbl[asic_id] = swsscommon.Table(state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
                    static_tbl[asic_id] = swsscommon.Table(state_db[asic_id], MUX_CABLE_STATIC_INFO_TABLE)
                    mux_tbl[asic_id] = swsscommon.Table(state_db[asic_id], MUX_CABLE_INFO_TABLE)
                # fill the newly found entry
                delete_port_from_y_cable_table(logical_port_name, y_cable_tbl[asic_index])
                delete_port_from_y_cable_table(logical_port_name, static_tbl[asic_index])
                delete_port_from_y_cable_table(logical_port_name, mux_tbl[asic_index])
                delete_change_event[:] = [True]
                # delete the y_cable instance
                physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)

                if len(physical_port_list) == 1:

                    physical_port = physical_port_list[0]
                    y_cable_port_instances.pop(physical_port)
                    y_cable_port_locks.pop(physical_port)
                else:
                    helper_logger.log_warning(
                        "Error: Retreived multiple ports for a Y cable port {} while delete entries".format(logical_port_name))


def init_ports_status_for_y_cable(platform_sfp, platform_chassis, y_cable_presence, stop_event=threading.Event()):
    global y_cable_platform_sfputil
    global y_cable_platform_chassis
    global y_cable_port_instances
    # Connect to CONFIG_DB and create port status table inside state_db
    config_db, state_db, port_tbl, y_cable_tbl = {}, {}, {}, {}
    static_tbl, mux_tbl = {}, {}
    port_table_keys = {}
    xcvrd_log_tbl = {}

    y_cable_platform_sfputil = platform_sfp
    y_cable_platform_chassis = platform_chassis

    fvs_updated = swsscommon.FieldValuePairs([('log_verbosity', 'notice')])
    # Get the namespaces in the platform
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")
        port_table_keys[asic_id] = port_tbl[asic_id].getKeys()
        xcvrd_log_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "XCVRD_LOG")
        xcvrd_log_tbl[asic_id].set("Y_CABLE", fvs_updated)

    # Init PORT_STATUS table if ports are on Y cable
    logical_port_list = y_cable_platform_sfputil.logical
    for logical_port_name in logical_port_list:
        if stop_event.is_set():
            break

        # Get the asic to which this port belongs
        asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
            logical_port_name)
        if asic_index is None:
            helper_logger.log_warning(
                "Got invalid asic index for {}, ignored".format(logical_port_name))
            continue

        if logical_port_name in port_table_keys[asic_index]:
            check_identifier_presence_and_update_mux_table_entry(
                state_db, port_tbl, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name, y_cable_presence)
        else:
            # This port does not exist in Port table of config but is present inside
            # logical_ports after loading the port_mappings from port_config_file
            # This should not happen
            helper_logger.log_warning(
                "Could not retreive port inside config_db PORT table {} for Y-Cable initiation".format(logical_port_name))


def change_ports_status_for_y_cable_change_event(port_dict, y_cable_presence, stop_event=threading.Event()):
    # Connect to CONFIG_DB and create port status table inside state_db
    config_db, state_db, port_tbl, y_cable_tbl = {}, {}, {}, {}
    static_tbl, mux_tbl = {}, {}
    port_table_keys = {}
    delete_change_event = [False]

    # Get the namespaces in the platform
    namespaces = multi_asic.get_front_end_namespaces()
    # Get the keys from PORT table inside config db to prepare check for mux_cable identifier
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")
        port_table_keys[asic_id] = port_tbl[asic_id].getKeys()

    # Init PORT_STATUS table if ports are on Y cable and an event is received
    for key, value in port_dict.items():
        if stop_event.is_set():
            break
        logical_port_list = y_cable_platform_sfputil.get_physical_to_logical(int(key))
        if logical_port_list is None:
            helper_logger.log_warning("Got unknown FP port index {}, ignored".format(key))
            continue
        for logical_port_name in logical_port_list:

            # Get the asic to which this port belongs
            asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                helper_logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
                continue

            if logical_port_name in port_table_keys[asic_index]:
                if value == SFP_STATUS_INSERTED:
                    helper_logger.log_info("Got SFP inserted event")
                    check_identifier_presence_and_update_mux_table_entry(
                        state_db, port_tbl, y_cable_tbl, static_tbl, mux_tbl, asic_index, logical_port_name, y_cable_presence)
                elif value == SFP_STATUS_REMOVED or value in errors_block_eeprom_reading:
                    check_identifier_presence_and_delete_mux_table_entry(
                        state_db, port_tbl, asic_index, logical_port_name, y_cable_presence, delete_change_event)
                else:
                    # SFP return unkown event, just ignore for now.
                    helper_logger.log_warning("Got unknown event {}, ignored".format(value))
                    continue

    # If there was a delete event and y_cable_presence was true, reaccess the y_cable presence
    if y_cable_presence[0] is True and delete_change_event[0] is True:

        y_cable_presence[:] = [False]
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(
                namespace)
            y_cable_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            y_cable_table_size = len(y_cable_tbl[asic_id].getKeys())
            if y_cable_table_size > 0:
                y_cable_presence[:] = [True]
                break


def delete_ports_status_for_y_cable():

    state_db, config_db, port_tbl, y_cable_tbl = {}, {}, {}, {}
    y_cable_tbl_keys = {}
    static_tbl, mux_tbl = {}, {}
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        y_cable_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
        y_cable_tbl_keys[asic_id] = y_cable_tbl[asic_id].getKeys()
        static_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], MUX_CABLE_STATIC_INFO_TABLE)
        mux_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], MUX_CABLE_INFO_TABLE)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")

    # delete PORTS on Y cable table if ports on Y cable
    logical_port_list = y_cable_platform_sfputil.logical
    for logical_port_name in logical_port_list:

        # Get the asic to which this port belongs
        asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
            logical_port_name)
        if asic_index is None:
            helper_logger.log_warning(
                "Got invalid asic index for {}, ignored".format(logical_port_name))

        if logical_port_name in y_cable_tbl_keys[asic_index]:
            delete_port_from_y_cable_table(logical_port_name, y_cable_tbl[asic_index])
            delete_port_from_y_cable_table(logical_port_name, static_tbl[asic_index])
            delete_port_from_y_cable_table(logical_port_name, mux_tbl[asic_index])
            # delete the y_cable port instance
            physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)

            if len(physical_port_list) == 1:

                physical_port = physical_port_list[0]
                if y_cable_port_instances.get(physical_port) is not None:
                    y_cable_port_instances.pop(physical_port)
                    y_cable_port_locks.pop(physical_port)
            else:
                helper_logger.log_warning(
                    "Error: Retreived multiple ports for a Y cable port {} while deleting entries".format(logical_port_name))


def check_identifier_presence_and_update_mux_info_entry(state_db, mux_tbl, asic_index, logical_port_name):

    # Get the namespaces in the platform
    config_db, port_tbl = {}, {}
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")

    (status, fvs) = port_tbl[asic_index].get(logical_port_name)

    if status is False:
        helper_logger.log_debug("Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(logical_port_name, port_tbl[asic_index].getTableName()))
        return

    else:
        # Convert list of tuples to a dictionary
        mux_table_dict = dict(fvs)
        if "state" in mux_table_dict:
            val = mux_table_dict.get("state", None)
            if val in ["active", "auto", "manual", "standby"]:

                if mux_tbl.get(asic_index, None) is not None:
                    # fill in the newly found entry
                    post_port_mux_info_to_db(logical_port_name,  mux_tbl[asic_index])

                else:
                    # first create the state db y cable table and then fill in the entry
                    namespaces = multi_asic.get_front_end_namespaces()
                    for namespace in namespaces:
                        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
                        mux_tbl[asic_id] = swsscommon.Table(state_db[asic_id], MUX_CABLE_INFO_TABLE)
                    # fill the newly found entry
                    post_port_mux_info_to_db(logical_port_name,  mux_tbl[asic_index])
            else:
                helper_logger.log_warning(
                    "Could not retreive active or auto value for state kvp for {}, inside MUX_CABLE table".format(logical_port_name))


def get_firmware_dict(physical_port, port_instance, target, side, mux_info_dict, logical_port_name):

    result = {}
    if port_instance.download_firmware_status == port_instance.FIRMWARE_DOWNLOAD_STATUS_INPROGRESS:

        # if there is a firmware download in progress, retreive the last known firmware
        state_db, mux_tbl = {}, {}
        mux_firmware_dict = {}

        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            mux_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], MUX_CABLE_INFO_TABLE)

        asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
            logical_port_name)

        (status, fvs) = mux_tbl[asic_index].get(logical_port_name)
        if status is False:
            helper_logger.log_warning("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(logical_port_name, mux_tbl[asic_index].getTableName()))
            mux_info_dict[("version_{}_active".format(side))] = "N/A"
            mux_info_dict[("version_{}_inactive".format(side))] = "N/A"
            mux_info_dict[("version_{}_next".format(side))] = "N/A"
            return

        mux_firmware_dict = dict(fvs)

        mux_info_dict[("version_{}_active".format(side))] = mux_firmware_dict.get(("version_{}_active".format(side)), None)
        mux_info_dict[("version_{}_inactive".format(side))] = mux_firmware_dict.get(("version_{}_inactive".format(side)), None)
        mux_info_dict[("version_{}_next".format(side))] = mux_firmware_dict.get(("version_{}_next".format(side)), None)

        helper_logger.log_warning(
            "trying to get/post firmware info while download in progress returning with last known firmware without execute {}".format(physical_port))
        return

    elif port_instance.download_firmware_status == port_instance.FIRMWARE_DOWNLOAD_STATUS_FAILED:
        # if there is a firmware download failed, retreive the current MCU's firmware with a log message
        helper_logger.log_error(
            "Firmware Download API failed in the previous run, firmware download status was set to failed;retry required {}".format(physical_port))

    with y_cable_port_locks[physical_port]:
        try:
            result = port_instance.get_firmware_version(target)
        except Exception as e:
            result = None
            helper_logger.log_warning("Failed to execute the get_firmware_version API for port {} side {} due to {}".format(physical_port,side,repr(e)))

    if result is not None and isinstance(result, dict):
        mux_info_dict[("version_{}_active".format(side))] = result.get("version_active", None)
        mux_info_dict[("version_{}_inactive".format(side))] = result.get("version_inactive", None)
        mux_info_dict[("version_{}_next".format(side))] = result.get("version_next", None)

    else:
        mux_info_dict[("version_{}_active".format(side))] = "N/A"
        mux_info_dict[("version_{}_inactive".format(side))] = "N/A"
        mux_info_dict[("version_{}_next".format(side))] = "N/A"


def get_muxcable_info(physical_port, logical_port_name):

    mux_info_dict = {}
    y_cable_tbl, state_db = {}, {}

    port_instance = y_cable_port_instances.get(physical_port)
    if port_instance is None:
        helper_logger.log_error("Error: Could not get port instance for muxcable info for Y cable port {}".format(logical_port_name))
        return -1

    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        y_cable_tbl[asic_id] = swsscommon.Table(state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)

    asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
        logical_port_name)
    if asic_index is None:
        helper_logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
        return -1

    (status, fvs) = y_cable_tbl[asic_index].get(logical_port_name)
    if status is False:
        helper_logger.log_warning("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(logical_port_name, y_cable_tbl[asic_index].getTableName()))
        return -1

    mux_port_dict = dict(fvs)
    read_side = int(mux_port_dict.get("read_side"))

    active_side = None

    with y_cable_port_locks[physical_port]:
        try:
            active_side = port_instance.get_active_linked_tor_side()
        except Exception as e:
            helper_logger.log_warning("Failed to execute the get_active_side API for port {} due to {}".format(physical_port,repr(e)))

    if active_side is None or active_side == port_instance.EEPROM_ERROR or active_side < 0:
        tor_active = 'unknown'
    elif read_side == active_side and (active_side == 1 or active_side == 2):
        tor_active = 'active'
    elif read_side != active_side and (active_side == 1 or active_side == 2):
        tor_active = 'standby'
    else:
        tor_active = 'unknown'

    mux_info_dict["tor_active"] = tor_active

    mux_dir_val = None
    with y_cable_port_locks[physical_port]:
        try:
            mux_dir_val = port_instance.get_mux_direction()
        except Exception as e:
            helper_logger.log_warning("Failed to execute the get_mux_direction API for port {} due to {}".format(physical_port,repr(e)))

    if mux_dir_val is None or mux_dir_val == port_instance.EEPROM_ERROR or mux_dir_val < 0 or read_side == -1:
        mux_direction = 'unknown'
    else:
        if read_side == mux_dir_val:
            mux_direction = 'self'
        else:
            mux_direction = 'peer'

    mux_info_dict["mux_direction"] = mux_direction

    with y_cable_port_locks[physical_port]:
        try:
            manual_switch_cnt = port_instance.get_switch_count_total(port_instance.SWITCH_COUNT_MANUAL)
            auto_switch_cnt = port_instance.get_switch_count_total(port_instance.SWITCH_COUNT_AUTO)
        except Exception as e:
            manual_switch_cnt = None
            auto_switch_cnt = None
            helper_logger.log_warning("Failed to execute the get_switch_cnt API for port {} due to {}".format(physical_port,repr(e)))

    if manual_switch_cnt is None or manual_switch_cnt == port_instance.EEPROM_ERROR or manual_switch_cnt < 0:
        mux_info_dict["manual_switch_count"] = "N/A"
    else:
        mux_info_dict["manual_switch_count"] = manual_switch_cnt

    if auto_switch_cnt is None or auto_switch_cnt == port_instance.EEPROM_ERROR or auto_switch_cnt < 0:
        mux_info_dict["auto_switch_count"] = "N/A"
    else:
        mux_info_dict["auto_switch_count"] = auto_switch_cnt


    if read_side == 1:
        with y_cable_port_locks[physical_port]:
            try:
                eye_result_self = port_instance.get_eye_heights(port_instance.TARGET_TOR_A)
                eye_result_peer = port_instance.get_eye_heights(port_instance.TARGET_TOR_B)
            except Exception as e:
                eye_result_self = None
                eye_result_peer = None
                helper_logger.log_warning("Failed to execute the get_eye_heights API for port {} due to {}".format(physical_port,repr(e)))
    else:
        with y_cable_port_locks[physical_port]:
            try:
                eye_result_self = port_instance.get_eye_heights(port_instance.TARGET_TOR_B)
                eye_result_peer = port_instance.get_eye_heights(port_instance.TARGET_TOR_A)
            except Exception as e:
                eye_result_self = None
                eye_result_peer = None
                helper_logger.log_warning("Failed to execute the get_eye_heights API for port {} due to {}".format(physical_port,repr(e)))

    with y_cable_port_locks[physical_port]:
        try:
            eye_result_nic = port_instance.get_eye_heights(port_instance.TARGET_NIC)
        except Exception as e:
            eye_result_nic = None
            helper_logger.log_warning("Failed to execute the get_eye_heights nic side API for port {} due to {}".format(physical_port,repr(e)))

    if eye_result_self is not None and eye_result_self is not port_instance.EEPROM_ERROR and isinstance(eye_result_self, list):
        mux_info_dict["self_eye_height_lane1"] = eye_result_self[0]
        mux_info_dict["self_eye_height_lane2"] = eye_result_self[1]
    else:
        mux_info_dict["self_eye_height_lane1"] = "N/A"
        mux_info_dict["self_eye_height_lane2"] = "N/A"

    if eye_result_peer is not None and eye_result_peer is not port_instance.EEPROM_ERROR and isinstance(eye_result_peer, list):
        mux_info_dict["peer_eye_height_lane1"] = eye_result_peer[0]
        mux_info_dict["peer_eye_height_lane2"] = eye_result_peer[1]
    else:
        mux_info_dict["peer_eye_height_lane1"] = "N/A"
        mux_info_dict["peer_eye_height_lane2"] = "N/A"

    if eye_result_nic is not None and eye_result_nic is not port_instance.EEPROM_ERROR and isinstance(eye_result_nic, list):
        mux_info_dict["nic_eye_height_lane1"] = eye_result_nic[0]
        mux_info_dict["nic_eye_height_lane2"] = eye_result_nic[1]
    else:
        mux_info_dict["nic_eye_height_lane1"] = "N/A"
        mux_info_dict["nic_eye_height_lane2"] = "N/A"

    if read_side == 1:
        with y_cable_port_locks[physical_port]:
            try:
                link_state_tor_a = port_instance.is_link_active(port_instance.TARGET_TOR_A)
            except Exception as e:
                link_state_tor_a = False
                helper_logger.log_warning("Failed to execute the is_link_active TOR A side API for port {} due to {}".format(physical_port,repr(e)))

            if link_state_tor_a:
                mux_info_dict["link_status_self"] = "up"
            else:
                mux_info_dict["link_status_self"] = "down"
        with y_cable_port_locks[physical_port]:
            try:
                link_state_tor_b = port_instance.is_link_active(port_instance.TARGET_TOR_B)
            except Exception as e:
                link_state_tor_b = False
                helper_logger.log_warning("Failed to execute the is_link_active TOR B side API for port {} due to {}".format(physical_port,repr(e)))
            if link_state_tor_b:
                mux_info_dict["link_status_peer"] = "up"
            else:
                mux_info_dict["link_status_peer"] = "down"
    else:
        with y_cable_port_locks[physical_port]:
            try:
                link_state_tor_b = port_instance.is_link_active(port_instance.TARGET_TOR_B)
            except Exception as e:
                link_state_tor_b = False
                helper_logger.log_warning("Failed to execute the is_link_active TOR B side API for port {} due to {}".format(physical_port,repr(e)))

            if link_state_tor_b:
                mux_info_dict["link_status_self"] = "up"
            else:
                mux_info_dict["link_status_self"] = "down"

        with y_cable_port_locks[physical_port]:
            try:
                link_state_tor_a = port_instance.is_link_active(port_instance.TARGET_TOR_A)
            except Exception as e:
                link_state_tor_a = False
                helper_logger.log_warning("Failed to execute the is_link_active TOR A side API for port {} due to {}".format(physical_port,repr(e)))

            if link_state_tor_a:
                mux_info_dict["link_status_peer"] = "up"
            else:
                mux_info_dict["link_status_peer"] = "down"

    with y_cable_port_locks[physical_port]:
        if port_instance.is_link_active(port_instance.TARGET_NIC):
            mux_info_dict["link_status_nic"] = "up"
        else:
            mux_info_dict["link_status_nic"] = "down"

    get_firmware_dict(physical_port, port_instance, port_instance.TARGET_NIC, "nic", mux_info_dict, logical_port_name)
    if read_side == 1:
        get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_A, "self", mux_info_dict, logical_port_name)
        get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_B, "peer", mux_info_dict, logical_port_name)
    else:
        get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_A, "peer", mux_info_dict, logical_port_name)
        get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_B, "self", mux_info_dict, logical_port_name)

    with y_cable_port_locks[physical_port]:
        try:
            res = port_instance.get_local_temperature()
        except Exception as e:
            res = None
            helper_logger.log_warning("Failed to execute the get_local_temperature for port {} due to {}".format(physical_port,repr(e)))

    if res is not None and res is not port_instance.EEPROM_ERROR and isinstance(res, int) and res >= 0:
        mux_info_dict["internal_temperature"] = res
    else:
        mux_info_dict["internal_temperature"] = "N/A"

    with y_cable_port_locks[physical_port]:
        try:
            res = port_instance.get_local_voltage()
        except Exception as e:
            res = None
            helper_logger.log_warning("Failed to execute the get_local_voltage for port {} due to {}".format(physical_port,repr(e)))

    if res is not None and res is not port_instance.EEPROM_ERROR and isinstance(res, float):
        mux_info_dict["internal_voltage"] = res
    else:
        mux_info_dict["internal_voltage"] = "N/A"

    with y_cable_port_locks[physical_port]:
        try:
            res = port_instance.get_nic_voltage()
        except Exception as e:
            res = None
            helper_logger.log_warning("Failed to execute the get_nic_voltage for port {} due to {}".format(physical_port,repr(e)))

    if res is not None and res is not port_instance.EEPROM_ERROR and isinstance(res, float):
        mux_info_dict["nic_voltage"] = res
    else:
        mux_info_dict["nic_voltage"] = "N/A"

    with y_cable_port_locks[physical_port]:
        try:
            res = port_instance.get_nic_temperature()
        except Exception as e:
            res = None
            helper_logger.log_warning("Failed to execute the get_nic_temperature for port {} due to {}".format(physical_port,repr(e)))

    if res is not None and res is not port_instance.EEPROM_ERROR and isinstance(res, int) and res >= 0:
        mux_info_dict["nic_temperature"] = res
    else:
        mux_info_dict["nic_temperature"] = "N/A"

    return mux_info_dict


def get_muxcable_static_info(physical_port, logical_port_name):

    mux_static_info_dict = {}
    y_cable_tbl, state_db = {}, {}

    port_instance = y_cable_port_instances.get(physical_port)
    if port_instance is None:
        helper_logger.log_error("Error: Could not get port instance for muxcable info for Y cable port {}".format(logical_port_name))
        return -1

    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        y_cable_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)

    asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
        logical_port_name)
    if asic_index is None:
        helper_logger.log_warning(
            "Got invalid asic index for {}, ignored".format(logical_port_name))
        return -1

    (status, fvs) = y_cable_tbl[asic_index].get(logical_port_name)
    if status is False:
        helper_logger.log_warning("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(
            logical_port_name, y_cable_tbl[asic_index].getTableName()))
        return -1
    mux_port_dict = dict(fvs)
    read_side = int(mux_port_dict.get("read_side"))

    if read_side == 1:
        mux_static_info_dict["read_side"] = "tor1"
    else:
        mux_static_info_dict["read_side"] = "tor2"

    dummy_list = ["N/A", "N/A", "N/A", "N/A", "N/A"]
    cursor_nic_values = []
    cursor_tor1_values = []
    cursor_tor2_values = []
    for i in range(1, 3):
        try:
            cursor_values_nic = port_instance.get_target_cursor_values(i, port_instance.TARGET_NIC)
        except Exception as e:
            cursor_values_nic = None
            helper_logger.log_warning("Failed to execute the get_target_cursor_value NIC for port {} due to {}".format(physical_port,repr(e)))

        if cursor_values_nic is not None and cursor_values_nic is not port_instance.EEPROM_ERROR and isinstance(cursor_values_nic, list):
            cursor_nic_values.append(cursor_values_nic)
        else:
            cursor_nic_values.append(dummy_list)

        try:
            cursor_values_tor1 = port_instance.get_target_cursor_values(i, port_instance.TARGET_TOR_A)
        except Exception as e:
            cursor_values_tor1 = None
            helper_logger.log_warning("Failed to execute the get_target_cursor_value ToR 1 for port {} due to {}".format(physical_port,repr(e)))

        if cursor_values_tor1 is not None and cursor_values_tor1 is not port_instance.EEPROM_ERROR and isinstance(cursor_values_tor1, list):
            cursor_tor1_values.append(cursor_values_tor1)
        else:
            cursor_tor1_values.append(dummy_list)

        try:
            cursor_values_tor2 = port_instance.get_target_cursor_values(i, port_instance.TARGET_TOR_B)
        except Exception as e:
            cursor_values_tor2 = None
            helper_logger.log_warning("Failed to execute the get_target_cursor_value ToR 2 for port {} due to {}".format(physical_port,repr(e)))

        if cursor_values_tor2 is not None and cursor_values_tor2 is not port_instance.EEPROM_ERROR and isinstance(cursor_values_tor2, list):
            cursor_tor2_values.append(cursor_values_tor2)
        else:
            cursor_tor2_values.append(dummy_list)

    for i in range(1, 3):
        mux_static_info_dict[("nic_lane{}_precursor1".format(i))] = cursor_nic_values[i-1][0]
        mux_static_info_dict[("nic_lane{}_precursor2".format(i))] = cursor_nic_values[i-1][1]
        mux_static_info_dict[("nic_lane{}_maincursor".format(i))] = cursor_nic_values[i-1][2]
        mux_static_info_dict[("nic_lane{}_postcursor1".format(i))] = cursor_nic_values[i-1][3]
        mux_static_info_dict[("nic_lane{}_postcursor2".format(i))] = cursor_nic_values[i-1][4]

    if read_side == 1:
        for i in range(1, 3):
            mux_static_info_dict[("tor_self_lane{}_precursor1".format(i))] = cursor_tor1_values[i-1][0]
            mux_static_info_dict[("tor_self_lane{}_precursor2".format(i))] = cursor_tor1_values[i-1][1]
            mux_static_info_dict[("tor_self_lane{}_maincursor".format(i))] = cursor_tor1_values[i-1][2]
            mux_static_info_dict[("tor_self_lane{}_postcursor1".format(i))] = cursor_tor1_values[i-1][3]
            mux_static_info_dict[("tor_self_lane{}_postcursor2".format(i))] = cursor_tor1_values[i-1][4]

        for i in range(1, 3):
            mux_static_info_dict[("tor_peer_lane{}_precursor1".format(i))] = cursor_tor2_values[i-1][0]
            mux_static_info_dict[("tor_peer_lane{}_precursor2".format(i))] = cursor_tor2_values[i-1][1]
            mux_static_info_dict[("tor_peer_lane{}_maincursor".format(i))] = cursor_tor2_values[i-1][2]
            mux_static_info_dict[("tor_peer_lane{}_postcursor1".format(i))] = cursor_tor2_values[i-1][3]
            mux_static_info_dict[("tor_peer_lane{}_postcursor2".format(i))] = cursor_tor2_values[i-1][4]
    else:
        for i in range(1, 3):
            mux_static_info_dict[("tor_self_lane{}_precursor1".format(i))] = cursor_tor2_values[i-1][0]
            mux_static_info_dict[("tor_self_lane{}_precursor2".format(i))] = cursor_tor2_values[i-1][1]
            mux_static_info_dict[("tor_self_lane{}_maincursor".format(i))] = cursor_tor2_values[i-1][2]
            mux_static_info_dict[("tor_self_lane{}_postcursor1".format(i))] = cursor_tor2_values[i-1][3]
            mux_static_info_dict[("tor_self_lane{}_postcursor2".format(i))] = cursor_tor2_values[i-1][4]

        for i in range(1, 3):
            mux_static_info_dict[("tor_peer_lane{}_precursor1".format(i))] = cursor_tor1_values[i-1][0]
            mux_static_info_dict[("tor_peer_lane{}_precursor2".format(i))] = cursor_tor1_values[i-1][1]
            mux_static_info_dict[("tor_peer_lane{}_maincursor".format(i))] = cursor_tor1_values[i-1][2]
            mux_static_info_dict[("tor_peer_lane{}_postcursor1".format(i))] = cursor_tor1_values[i-1][3]
            mux_static_info_dict[("tor_peer_lane{}_postcursor2".format(i))] = cursor_tor1_values[i-1][4]

    return mux_static_info_dict


def post_port_mux_info_to_db(logical_port_name, table):

    physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)
    if physical_port_list is None:
        helper_logger.log_error("No physical ports found for logical port '{}'".format(logical_port_name))
        return -1

    if len(physical_port_list) > 1:
        helper_logger.log_warning("Error: Retreived multiple ports for a Y cable port {}".format(logical_port_name))
        return -1

    for physical_port in physical_port_list:

        if not y_cable_wrapper_get_presence(physical_port):
            helper_logger.log_warning("Error: trying to post mux info without presence of port {}".format(logical_port_name))
            continue

        mux_info_dict = get_muxcable_info(physical_port, logical_port_name)
        if mux_info_dict is not None and mux_info_dict is not -1:
            #transceiver_dict[physical_port] = port_info_dict
            fvs = swsscommon.FieldValuePairs(
                [('tor_active',  mux_info_dict["tor_active"]),
                 ('mux_direction',  str(mux_info_dict["mux_direction"])),
                 ('manual_switch_count', str(mux_info_dict["manual_switch_count"])),
                 ('auto_switch_count', str(mux_info_dict["auto_switch_count"])),
                 ('link_status_self', mux_info_dict["link_status_self"]),
                 ('link_status_peer', mux_info_dict["link_status_peer"]),
                 ('link_status_nic', mux_info_dict["link_status_nic"]),
                 ('self_eye_height_lane1', str(mux_info_dict["self_eye_height_lane1"])),
                 ('self_eye_height_lane2', str(mux_info_dict["self_eye_height_lane2"])),
                 ('peer_eye_height_lane1', str(mux_info_dict["peer_eye_height_lane1"])),
                 ('peer_eye_height_lane2', str(mux_info_dict["peer_eye_height_lane1"])),
                 ('nic_eye_height_lane1', str(mux_info_dict["nic_eye_height_lane1"])),
                 ('nic_eye_height_lane2', str(mux_info_dict["nic_eye_height_lane2"])),
                 ('internal_temperature', str(mux_info_dict["internal_temperature"])),
                 ('internal_voltage', str(mux_info_dict["internal_voltage"])),
                 ('nic_temperature', str(mux_info_dict["nic_temperature"])),
                 ('nic_voltage', str(mux_info_dict["nic_voltage"])),
                 ('version_self_active', str(mux_info_dict["version_self_active"])),
                 ('version_self_inactive', str(mux_info_dict["version_self_inactive"])),
                 ('version_self_next', str(mux_info_dict["version_self_next"])),
                 ('version_peer_active', str(mux_info_dict["version_peer_active"])),
                 ('version_peer_inactive', str(mux_info_dict["version_peer_inactive"])),
                 ('version_peer_next', str(mux_info_dict["version_peer_next"])),
                 ('version_nic_active', str(mux_info_dict["version_nic_active"])),
                 ('version_nic_inactive', str(mux_info_dict["version_nic_inactive"])),
                 ('version_nic_next', str(mux_info_dict["version_nic_next"]))
                 ])
            table.set(logical_port_name, fvs)
        else:
            return -1


def post_port_mux_static_info_to_db(logical_port_name, static_table):

    physical_port_list = logical_port_name_to_physical_port_list(
        logical_port_name)
    if physical_port_list is None:
        helper_logger.log_error("No physical ports found for logical port '{}'".format(logical_port_name))
        return -1

    if len(physical_port_list) > 1:
        helper_logger.log_warning(
            "Error: Retreived multiple ports for a Y cable port {}".format(logical_port_name))
        return -1

    for physical_port in physical_port_list:

        if not y_cable_wrapper_get_presence(physical_port):
            continue

        mux_static_info_dict = get_muxcable_static_info(physical_port, logical_port_name)

        if mux_static_info_dict is not None and mux_static_info_dict is not -1:
            #transceiver_dict[physical_port] = port_info_dict
            fvs = swsscommon.FieldValuePairs(
                [('read_side',  mux_static_info_dict["read_side"]),
                 ('nic_lane1_precursor1', str(mux_static_info_dict["nic_lane1_precursor1"])),
                 ('nic_lane1_precursor2', str(mux_static_info_dict["nic_lane1_precursor2"])),
                 ('nic_lane1_maincursor', str(mux_static_info_dict["nic_lane1_maincursor"])),
                 ('nic_lane1_postcursor1', str(mux_static_info_dict["nic_lane1_postcursor1"])),
                 ('nic_lane1_postcursor2', str(mux_static_info_dict["nic_lane1_postcursor2"])),
                 ('nic_lane2_precursor1', str(mux_static_info_dict["nic_lane2_precursor1"])),
                 ('nic_lane2_precursor2', str(mux_static_info_dict["nic_lane2_precursor2"])),
                 ('nic_lane2_maincursor', str(mux_static_info_dict["nic_lane2_maincursor"])),
                 ('nic_lane2_postcursor1', str(mux_static_info_dict["nic_lane2_postcursor1"])),
                 ('nic_lane2_postcursor2', str(mux_static_info_dict["nic_lane2_postcursor2"])),
                 ('tor_self_lane1_precursor1', str(mux_static_info_dict["tor_self_lane1_precursor1"])),
                 ('tor_self_lane1_precursor2', str(mux_static_info_dict["tor_self_lane1_precursor2"])),
                 ('tor_self_lane1_maincursor', str(mux_static_info_dict["tor_self_lane1_maincursor"])),
                 ('tor_self_lane1_postcursor1', str(mux_static_info_dict["tor_self_lane1_postcursor1"])),
                 ('tor_self_lane1_postcursor2', str(mux_static_info_dict["tor_self_lane1_postcursor2"])),
                 ('tor_self_lane2_precursor1', str(mux_static_info_dict["tor_self_lane2_precursor1"])),
                 ('tor_self_lane2_precursor2', str(mux_static_info_dict["tor_self_lane2_precursor2"])),
                 ('tor_self_lane2_maincursor', str(mux_static_info_dict["tor_self_lane2_maincursor"])),
                 ('tor_self_lane2_postcursor1', str(mux_static_info_dict["tor_self_lane2_postcursor1"])),
                 ('tor_self_lane2_postcursor2', str(mux_static_info_dict["tor_self_lane2_postcursor2"])),
                 ('tor_peer_lane1_precursor1', str(mux_static_info_dict["tor_peer_lane1_precursor1"])),
                 ('tor_peer_lane1_precursor2', str(mux_static_info_dict["tor_peer_lane1_precursor2"])),
                 ('tor_peer_lane1_maincursor', str(mux_static_info_dict["tor_peer_lane1_maincursor"])),
                 ('tor_peer_lane1_postcursor1', str(mux_static_info_dict["tor_peer_lane1_postcursor1"])),
                 ('tor_peer_lane1_postcursor2', str(mux_static_info_dict["tor_peer_lane1_postcursor2"])),
                 ('tor_peer_lane2_precursor1', str(mux_static_info_dict["tor_peer_lane2_precursor1"])),
                 ('tor_peer_lane2_precursor2', str(mux_static_info_dict["tor_peer_lane2_precursor2"])),
                 ('tor_peer_lane2_maincursor', str(mux_static_info_dict["tor_peer_lane2_maincursor"])),
                 ('tor_peer_lane2_postcursor1', str(mux_static_info_dict["tor_peer_lane2_postcursor1"])),
                 ('tor_peer_lane2_postcursor2', str(mux_static_info_dict["tor_peer_lane2_postcursor2"]))
                 ])
            static_table.set(logical_port_name, fvs)
        else:
            return -1


def post_mux_static_info_to_db(is_warm_start, stop_event=threading.Event()):
    # Connect to STATE_DB and create transceiver mux/static info tables
    state_db, static_tbl = {}, {}

    # Get the namespaces in the platform
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        static_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], MUX_CABLE_STATIC_INFO_TABLE)

    # Post all the current interface dom/sfp info to STATE_DB
    logical_port_list = y_cable_platform_sfputil.logical
    for logical_port_name in logical_port_list:
        if stop_event.is_set():
            break

        # Get the asic to which this port belongs
        asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
            logical_port_name)
        if asic_index is None:
            helper_logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
            continue
        post_port_mux_static_info_to_db(logical_port_name, mux_tbl[asic_index])


def post_mux_info_to_db(is_warm_start, stop_event=threading.Event()):
    # Connect to STATE_DB and create transceiver mux/static info tables
    state_db, mux_tbl, static_tbl = {}, {}, {}

    # Get the namespaces in the platform
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        mux_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], MUX_CABLE_INFO_TABLE)

    # Post all the current interface dom/sfp info to STATE_DB
    logical_port_list = y_cable_platform_sfputil.logical
    for logical_port_name in logical_port_list:
        if stop_event.is_set():
            break

        # Get the asic to which this port belongs
        asic_index = y_cable_platform_sfputil.get_asic_id_for_logical_port(
            logical_port_name)
        if asic_index is None:
            helper_logger.log_warning(
                "Got invalid asic index for {}, ignored".format(logical_port_name))
            continue
        post_port_mux_info_to_db(logical_port_name,  mux_tbl[asic_index])


def task_download_firmware_worker(port, physical_port, port_instance, file_full_path, xcvrd_down_fw_rsp_tbl, xcvrd_down_fw_cmd_sts_tbl, rc):
    helper_logger.log_debug("worker thread launched for downloading physical port {} path {}".format(physical_port, file_full_path))
    try:
        status = port_instance.download_firmware(file_full_path)
        time.sleep(5)
    except Exception as e:
        status = -1
        helper_logger.log_warning("Failed to execute the download firmware API for port {} due to {}".format(physical_port,repr(e)))

    set_result_and_delete_port('status', status, xcvrd_down_fw_cmd_sts_tbl, xcvrd_down_fw_rsp_tbl, port)
    helper_logger.log_debug(" downloading complete {} {} {}".format(physical_port, file_full_path, status))
    rc[0] = status
    helper_logger.log_debug("download thread finished port {} physical_port {}".format(port, physical_port))

# Thread wrapper class to update y_cable status periodically
class YCableTableUpdateTask(object):
    def __init__(self):
        self.task_thread = None
        self.task_cli_thread = None
        self.task_download_firmware_thread = {}
        self.task_stopping_event = threading.Event()

        if multi_asic.is_multi_asic():
            # Load the namespace details first from the database_global.json file.
            swsscommon.SonicDBConfig.initializeGlobalConfig()

    def task_worker(self):

        # Connect to STATE_DB and APPL_DB and get both the HW_MUX_STATUS_TABLE info
        appl_db, state_db, config_db, status_tbl, y_cable_tbl = {}, {}, {}, {}, {}
        y_cable_tbl_keys = {}
        mux_cable_command_tbl, y_cable_command_tbl = {}, {}
        mux_metrics_tbl = {}

        sel = swsscommon.Select()

        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            # Open a handle to the Application database, in all namespaces
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            appl_db[asic_id] = daemon_base.db_connect("APPL_DB", namespace)
            config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            status_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], swsscommon.APP_HW_MUX_CABLE_TABLE_NAME)
            mux_cable_command_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], swsscommon.APP_MUX_CABLE_COMMAND_TABLE_NAME)
            y_cable_command_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], swsscommon.APP_MUX_CABLE_COMMAND_TABLE_NAME)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            y_cable_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            mux_metrics_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], swsscommon.STATE_MUX_METRICS_TABLE_NAME)
            y_cable_tbl_keys[asic_id] = y_cable_tbl[asic_id].getKeys()
            sel.addSelectable(status_tbl[asic_id])
            sel.addSelectable(mux_cable_command_tbl[asic_id])

        # Listen indefinitely for changes to the HW_MUX_CABLE_TABLE in the Application DB's
        while True:
            # Use timeout to prevent ignoring the signals we want to handle
            # in signal_handler() (e.g. SIGTERM for graceful shutdown)

            if self.task_stopping_event.is_set():
                break

            (state, selectableObj) = sel.select(SELECT_TIMEOUT)

            if state == swsscommon.Select.TIMEOUT:
                # Do not flood log when select times out
                continue
            if state != swsscommon.Select.OBJECT:
                helper_logger.log_warning(
                    "sel.select() did not  return swsscommon.Select.OBJECT for sonic_y_cable updates")
                continue

            # Get the redisselect object  from selectable object
            redisSelectObj = swsscommon.CastSelectableToRedisSelectObj(
                selectableObj)
            # Get the corresponding namespace from redisselect db connector object
            namespace = redisSelectObj.getDbConnector().getNamespace()
            asic_index = multi_asic.get_asic_index_from_namespace(namespace)

            while True:
                (port, op, fvp) = status_tbl[asic_index].pop()
                if not port:
                    break

                helper_logger.log_debug("Y_CABLE_DEBUG: received an event for port transition {} {}".format(port, threading.currentThread().getName()))

                # entering this section signifies a start for xcvrd state
                # change request from swss so initiate recording in mux_metrics table
                time_start = datetime.datetime.utcnow().strftime("%Y-%b-%d %H:%M:%S.%f")
                if fvp:
                    # This check might be redundant, to check, the presence of this Port in keys
                    # in logical_port_list but keep for now for coherency
                    # also skip checking in logical_port_list inside sfp_util
                    if port not in y_cable_tbl_keys[asic_index]:
                        continue

                    fvp_dict = dict(fvp)

                    if "state" in fvp_dict:
                        # got a state change
                        new_status = fvp_dict["state"]
                        (status, fvs) = y_cable_tbl[asic_index].get(port)
                        if status is False:
                            helper_logger.log_warning("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(
                                port, y_cable_tbl[asic_index].getTableName()))
                            continue
                        mux_port_dict = dict(fvs)
                        old_status = mux_port_dict.get("state")
                        read_side = mux_port_dict.get("read_side")
                        # Now whatever is the state requested, toggle the mux appropriately
                        helper_logger.log_debug("Y_CABLE_DEBUG: xcvrd trying to transition port {} from {} to {}".format(port, old_status, new_status))
                        active_side = update_tor_active_side(read_side, new_status, port)
                        if active_side == -1:
                            helper_logger.log_warning("ERR: Got a change event for toggle but could not toggle the mux-direction for port {} state from {} to {}, writing unknown".format(
                                port, old_status, new_status))
                            new_status = 'unknown'

                        helper_logger.log_debug("Y_CABLE_DEBUG: xcvrd successful to transition port {} from {} to {} and write back to the DB {}".format(port, old_status, new_status, threading.currentThread().getName()))
                        helper_logger.log_notice("Got a change event for toggle the mux-direction active side for port {} state from {} to {} {}".format(
                            port, old_status, new_status, threading.currentThread().getName()))
                        time_end = datetime.datetime.utcnow().strftime("%Y-%b-%d %H:%M:%S.%f")
                        fvs_metrics = swsscommon.FieldValuePairs([('xcvrd_switch_{}_start'.format(new_status), str(time_start)),
                                                                  ('xcvrd_switch_{}_end'.format(new_status), str(time_end))])
                        mux_metrics_tbl[asic_index].set(port, fvs_metrics)

                        fvs_updated = swsscommon.FieldValuePairs([('state', new_status),
                                                                  ('read_side', read_side),
                                                                  ('active_side', str(active_side))])
                        y_cable_tbl[asic_index].set(port, fvs_updated)
                    else:
                        helper_logger.log_info("Got a change event on port {} of table {} that does not contain state".format(
                            port, swsscommon.APP_HW_MUX_CABLE_TABLE_NAME))

            while True:
                (port_m, op_m, fvp_m) = mux_cable_command_tbl[asic_index].pop()

                if not port_m:
                    break
                helper_logger.log_debug("Y_CABLE_DEBUG: received a probe for port status {} {}".format(port_m, threading.currentThread().getName()))

                if fvp_m:

                    if port_m not in y_cable_tbl_keys[asic_index]:
                        continue

                    fvp_dict = dict(fvp_m)

                    if "command" in fvp_dict:
                        # check if xcvrd got a probe command
                        probe_identifier = fvp_dict["command"]

                        if probe_identifier == "probe":
                            (status, fv) = y_cable_tbl[asic_index].get(port_m)
                            if status is False:
                                helper_logger.log_warning("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(
                                    port_m, y_cable_tbl[asic_index].getTableName()))
                                continue
                            mux_port_dict = dict(fv)
                            read_side = mux_port_dict.get("read_side")
                            update_appdb_port_mux_cable_response_table(port_m, asic_index, appl_db, int(read_side))


    def task_cli_worker(self):

        # Connect to STATE_DB and APPL_DB and get both the HW_MUX_STATUS_TABLE info
        appl_db, config_db , state_db, y_cable_tbl = {}, {}, {}, {}
        xcvrd_log_tbl = {}
        xcvrd_down_fw_cmd_tbl, xcvrd_down_fw_rsp_tbl, xcvrd_down_fw_cmd_sts_tbl = {}, {}, {}
        xcvrd_down_fw_status_cmd_tbl, xcvrd_down_fw_status_rsp_tbl, xcvrd_down_fw_status_cmd_sts_tbl = {}, {}, {}
        xcvrd_acti_fw_cmd_tbl, xcvrd_acti_fw_rsp_tbl, xcvrd_acti_fw_cmd_sts_tbl = {}, {}, {}
        xcvrd_roll_fw_cmd_tbl, xcvrd_roll_fw_rsp_tbl, xcvrd_roll_fw_cmd_sts_tbl = {}, {}, {}
        xcvrd_show_fw_cmd_tbl, xcvrd_show_fw_rsp_tbl, xcvrd_show_fw_cmd_sts_tbl, xcvrd_show_fw_res_tbl = {}, {}, {}, {}
        xcvrd_show_hwmode_dir_cmd_tbl, xcvrd_show_hwmode_dir_rsp_tbl, xcvrd_show_hwmode_dir_cmd_sts_tbl = {}, {}, {}
        xcvrd_show_hwmode_swmode_cmd_tbl, xcvrd_show_hwmode_swmode_rsp_tbl, xcvrd_show_hwmode_swmode_cmd_sts_tbl = {}, {}, {}
        xcvrd_config_hwmode_state_cmd_tbl, xcvrd_config_hwmode_state_rsp_tbl , xcvrd_config_hwmode_state_cmd_sts_tbl= {}, {}, {}
        xcvrd_config_hwmode_swmode_cmd_tbl, xcvrd_config_hwmode_swmode_rsp_tbl , xcvrd_config_hwmode_swmode_cmd_sts_tbl= {}, {}, {}


        sel = swsscommon.Select()


        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            # Open a handle to the Application database, in all namespaces
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            appl_db[asic_id] = daemon_base.db_connect("APPL_DB", namespace)
            config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            xcvrd_log_tbl[asic_id] = swsscommon.SubscriberStateTable(
                config_db[asic_id], "XCVRD_LOG")
            xcvrd_show_fw_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_SHOW_FW_CMD")
            xcvrd_show_fw_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_SHOW_FW_CMD")
            xcvrd_show_fw_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_SHOW_FW_RSP")
            xcvrd_show_fw_res_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_SHOW_FW_RES")
            xcvrd_down_fw_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_DOWN_FW_CMD")
            xcvrd_down_fw_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_DOWN_FW_CMD")
            xcvrd_down_fw_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_DOWN_FW_RSP")
            xcvrd_down_fw_status_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_DOWN_FW_STATUS_CMD")
            xcvrd_down_fw_status_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_DOWN_FW_STATUS_RSP")
            xcvrd_acti_fw_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_ACTI_FW_CMD")
            xcvrd_acti_fw_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_ACTI_FW_CMD")
            xcvrd_acti_fw_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_ACTI_FW_RSP")
            xcvrd_roll_fw_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_ROLL_FW_CMD")
            xcvrd_roll_fw_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_ROLL_FW_CMD")
            xcvrd_roll_fw_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_ROLL_FW_RSP")
            xcvrd_show_hwmode_dir_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_SHOW_HWMODE_DIR_CMD")
            xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_SHOW_HWMODE_DIR_CMD")
            xcvrd_show_hwmode_dir_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_SHOW_HWMODE_DIR_RSP")
            xcvrd_config_hwmode_state_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_CONFIG_HWMODE_DIR_CMD")
            xcvrd_config_hwmode_state_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_CONFIG_HWMODE_DIR_CMD")
            xcvrd_config_hwmode_state_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_CONFIG_HWMODE_DIR_RSP")
            xcvrd_config_hwmode_swmode_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_CONFIG_HWMODE_SWMODE_CMD")
            xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_CONFIG_HWMODE_SWMODE_CMD")
            xcvrd_config_hwmode_swmode_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_CONFIG_HWMODE_SWMODE_RSP")
            xcvrd_show_hwmode_swmode_cmd_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "XCVRD_SHOW_HWMODE_SWMODE_CMD")
            xcvrd_show_hwmode_swmode_cmd_sts_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "XCVRD_SHOW_HWMODE_SWMODE_CMD")
            xcvrd_show_hwmode_swmode_rsp_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], "XCVRD_SHOW_HWMODE_SWMODE_RSP")
            sel.addSelectable(xcvrd_log_tbl[asic_id])
            sel.addSelectable(xcvrd_down_fw_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_down_fw_status_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_acti_fw_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_roll_fw_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_show_fw_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_show_hwmode_dir_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_config_hwmode_state_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_show_hwmode_swmode_cmd_tbl[asic_id])
            sel.addSelectable(xcvrd_config_hwmode_swmode_cmd_tbl[asic_id])

        # Listen indefinitely for changes to the XCVRD_CMD_TABLE in the Application DB's
        while True:
            # Use timeout to prevent ignoring the signals we want to handle
            # in signal_handler() (e.g. SIGTERM for graceful shutdown)

            if self.task_stopping_event.is_set():
                break

            (state, selectableObj) = sel.select(SELECT_TIMEOUT)

            if state == swsscommon.Select.TIMEOUT:
                # Do not flood log when select times out
                continue
            if state != swsscommon.Select.OBJECT:
                helper_logger.log_warning(
                    "sel.select() did not  return swsscommon.Select.OBJECT for sonic_y_cable updates")
                continue

            # Get the redisselect object  from selectable object
            redisSelectObj = swsscommon.CastSelectableToRedisSelectObj(
                selectableObj)
            # Get the corresponding namespace from redisselect db connector object
            namespace = redisSelectObj.getDbConnector().getNamespace()
            asic_index = multi_asic.get_asic_index_from_namespace(namespace)

            while True:
                (key, op_m, fvp_m) = xcvrd_log_tbl[asic_index].pop()

                if not key:
                    break

                helper_logger.log_notice("Y_CABLE_DEBUG: trying to enable/disable debug logs")
                if fvp_m:

                    if key is "Y_CABLE":
                        continue

                    fvp_dict = dict(fvp_m)
                    if "log_verbosity" in fvp_dict:
                        # check if xcvrd got a probe command
                        probe_identifier = fvp_dict["log_verbosity"]

                        if probe_identifier == "debug":
                            helper_logger.set_min_log_priority_debug()

                        elif probe_identifier == "notice":
                            helper_logger.set_min_log_priority_notice()

            while True:
                # show muxcable hwmode state <port>
                (port, op, fvp) = xcvrd_show_hwmode_dir_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)

                    if "state" in fvp_dict:

                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            state = 'cable not present'
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get physical port for cli command show mux hwmode muxdirection Y cable port {}".format(port))
                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            state = 'not Y-Cable port'
                            helper_logger.log_error(
                                "Error: Could not get port instance for cli command show mux hwmode muxdirection Y cable port {}".format(port))
                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)
                            break

                        with y_cable_port_locks[physical_port]:
                            try:
                                read_side = port_instance.get_read_side()
                            except Exception as e:
                                read_side = None
                                helper_logger.log_warning("Failed to execute the get_read_side API for port {} due to {}".format(physical_port,repr(e)))

                        if read_side is None or read_side == port_instance.EEPROM_ERROR or read_side < 0:

                            state = 'unknown'
                            helper_logger.log_warning(
                                "Error: Could not get read side for cli command show mux hwmode muxdirection logical port {} and physical port {}".format(port, physical_port))
                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)
                            break

                        with y_cable_port_locks[physical_port]:
                            try:
                                active_side = port_instance.get_mux_direction()
                            except Exception as e:
                                active_side = None
                                helper_logger.log_warning("Failed to execute the get_mux_direction API for port {} due to {}".format(physical_port,repr(e)))

                        if active_side is None or active_side == port_instance.EEPROM_ERROR or active_side < 0:

                            state = 'unknown'
                            helper_logger.log_warning("Error: Could not get active side for cli command show mux hwmode muxdirection logical port {} and physical port {}".format(port, physical_port))

                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)
                            break

                        if read_side == active_side and (active_side == 1 or active_side == 2):
                            state = 'active'
                        elif read_side != active_side and (active_side == 1 or active_side == 2):
                            state = 'standby'
                        else:
                            state = 'unknown'
                            helper_logger.log_warning("Error: Could not get valid state for cli command show mux hwmode muxdirection logical port {} and physical port {}".format(port, physical_port))
                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)
                            break

                        set_result_and_delete_port('state', state, xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)
                    else:
                        helper_logger.log_warning("Error: Wrong input param for cli command show mux hwmode muxdirection logical port {}".format(port))
                        set_result_and_delete_port('state', 'unknown', xcvrd_show_hwmode_dir_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_dir_rsp_tbl[asic_index], port)

            while True:
                # Config muxcable hwmode state <active/standby> <port>
                (port, op, fvp) = xcvrd_config_hwmode_state_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)

                    if "config" in fvp_dict:
                        config_state = str(fvp_dict["config"])

                        status = 'False'
                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get physical port for cli command config mux hwmode state active/standby Y cable port {}".format(port))
                            set_result_and_delete_port('result', status, xcvrd_config_hwmode_state_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_state_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get port instance for cli command config mux hwmode state active/standby Y cable port {}".format(port))
                            set_result_and_delete_port('result', status, xcvrd_config_hwmode_state_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_state_rsp_tbl[asic_index], port)
                            break

                        with y_cable_port_locks[physical_port]:
                            try:
                                read_side = port_instance.get_read_side()
                            except Exception as e:
                                read_side = None
                                helper_logger.log_warning("Failed to execute the get_read_side API for port {} due to {}".format(physical_port,repr(e)))

                        if read_side is None or read_side is port_instance.EEPROM_ERROR or read_side < 0:

                            status = 'False'
                            helper_logger.log_error(
                                "Error: Could not get read side for cli command config mux hwmode state active/standby Y cable port {}".format(port))
                            set_result_and_delete_port('result', status, xcvrd_config_hwmode_state_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_state_rsp_tbl[asic_index], port)
                            break

                        if read_side is port_instance.TARGET_TOR_A:
                            if config_state == "active":
                                with y_cable_port_locks[physical_port]:
                                    try:
                                        status = port_instance.toggle_mux_to_tor_a()
                                    except Exception as e:
                                        status = -1
                                        helper_logger.log_warning("Failed to execute the toggle mux ToR A API for port {} due to {}".format(physical_port,repr(e)))
                            elif config_state == "standby":
                                with y_cable_port_locks[physical_port]:
                                    try:
                                        status = port_instance.toggle_mux_to_tor_b()
                                    except Exception as e:
                                        status = -1
                                        helper_logger.log_warning("Failed to execute the toggle mux ToR B API for port {} due to {}".format(physical_port,repr(e)))
                        elif read_side is port_instance.TARGET_TOR_B:
                            if config_state == 'active':
                                with y_cable_port_locks[physical_port]:
                                    try:
                                        status = port_instance.toggle_mux_to_tor_b()
                                    except Exception as e:
                                        status = -1
                                        helper_logger.log_warning("Failed to execute the toggle mux ToR B API for port {} due to {}".format(physical_port,repr(e)))
                            elif config_state == "standby":
                                with y_cable_port_locks[physical_port]:
                                    try:
                                        status = port_instance.toggle_mux_to_tor_a()
                                    except Exception as e:
                                        status = -1
                                        helper_logger.log_warning("Failed to execute the toggle mux ToR A API for port {} due to {}".format(physical_port,repr(e)))
                        else:
                            set_result_and_delete_port('result', status, xcvrd_show_hwmode_state_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_state_rsp_tbl[asic_index], port)
                            helper_logger.log_error(
                                "Error: Could not get valid config read side for cli command config mux hwmode state active/standby Y cable port {}".format(port))
                            break

                        set_result_and_delete_port('result', status, xcvrd_config_hwmode_state_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_state_rsp_tbl[asic_index], port)
                    else:
                        helper_logger.log_error("Error: Wrong input param for cli command config mux hwmode state active/standby logical port {}".format(port))
                        set_result_and_delete_port('result', 'False', xcvrd_show_hwmode_state_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_state_rsp_tbl[asic_index], port)
                        
            while True:
                # Config muxcable hwmode setswitchmode <auto/manual> <port>
                (port, op, fvp) = xcvrd_show_hwmode_swmode_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)

                    if "state" in fvp_dict:

                        state = 'unknown'
                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get physical port for cli cmd show mux hwmode switchmode Y cable port {}".format(port))
                            state = 'cable not present'
                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_swmode_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get port instance for cli cmd show mux hwmode switchmode Y cable port {}".format(port))
                            state = 'not Y-Cable port'
                            set_result_and_delete_port('state', state, xcvrd_show_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_swmode_rsp_tbl[asic_index], port)
                            break

                        with y_cable_port_locks[physical_port]:
                            try:
                                result = port_instance.get_switching_mode()
                            except Exception as e:
                                result = None
                                helper_logger.log_warning("Failed to execute the get_switching_mode for port {} due to {}".format(physical_port,repr(e)))

                            if result is None or result == port_instance.EEPROM_ERROR or result < 0:

                                helper_logger.log_error(
                                    "Error: Could not get read side for cli cmd show mux hwmode switchmode logical port {} and physical port {}".format(port, physical_port))
                                set_result_and_delete_port('state', state, xcvrd_show_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_swmode_rsp_tbl[asic_index], port)
                                break

                        if result == port_instance.SWITCHING_MODE_AUTO:
                            state = "auto"
                        elif result == port_instance.SWITCHING_MODE_MANUAL:
                            state = "manual"
                        else:
                            state = "unknown"

                        set_result_and_delete_port('state', state, xcvrd_show_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_swmode_rsp_tbl[asic_index], port)
                    else:
                        helper_logger.log_error("Error: Incorrect input param for cli cmd show mux hwmode switchmode logical port {}".format(port))
                        set_result_and_delete_port('state', 'unknown', xcvrd_show_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_show_hwmode_swmode_rsp_tbl[asic_index], port)



            while True:
                # Config muxcable hwmode setswitchmode <auto/manual> <port>
                (port, op, fvp) = xcvrd_config_hwmode_swmode_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)

                    if "config" in fvp_dict:
                        config_mode = str(fvp_dict["config"])

                        status = 'False'
                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get physical port for cli cmd config mux hwmode setswitchmode Y cable port {}".format(port))
                            set_result_and_delete_port('result', status, xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get port instance for cli cmd config mux hwmode setswitchmode Y cable port {}".format(port))
                            set_result_and_delete_port('result', status, xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)
                            break

                        if config_mode == "auto":
                            with y_cable_port_locks[physical_port]:
                                try:
                                    result = port_instance.set_switching_mode(port_instance.SWITCHING_MODE_AUTO)
                                except Exception as e:
                                    result = None
                                    helper_logger.log_warning("Failed to execute the set_switching_mode auto for port {} due to {}".format(physical_port,repr(e)))

                            if result is None or result == port_instance.EEPROM_ERROR or result < 0:

                                status = 'False'
                                helper_logger.log_error(
                                    "Error: Could not get read side for cli cmd config mux hwmode setswitchmode logical port {} and physical port {}".format(port, physical_port))
                                set_result_and_delete_port('result', status, xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)
                                break

                        elif config_mode == "manual":
                            with y_cable_port_locks[physical_port]:
                                try:
                                    result = port_instance.set_switching_mode(port_instance.SWITCHING_MODE_MANUAL)
                                except Exception as e:
                                    result = None
                                    helper_logger.log_warning("Failed to execute the set_switching_mode manual for port {} due to {}".format(physical_port,repr(e)))
                            if result is None or result is port_instance.EEPROM_ERROR or result < 0:

                                status = 'False'
                                helper_logger.log_error(
                                    "Error: Could not get read side for cli cmd config mux hwmode setswitchmode logical port {} and physical port {}".format(port, physical_port))
                                set_result_and_delete_port('result', status, xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)
                                break
                        else:
                            helper_logger.log_error(
                                "Error: Incorrect Config state for cli cmd config mux hwmode setswitchmode logical port {} and physical port {}".format(port, physical_port))
                            set_result_and_delete_port('result', status, xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)
                            break


                        set_result_and_delete_port('result', result, xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)

                    else:
                        helper_logger.log_error("Error: Incorrect input param for cli cmd config mux hwmode setswitchmode logical port {}".format(port))
                        set_result_and_delete_port('result', 'False', xcvrd_config_hwmode_swmode_cmd_sts_tbl[asic_index], xcvrd_config_hwmode_swmode_rsp_tbl[asic_index], port)



            while True:
                (port, op, fvp) = xcvrd_down_fw_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:
                    # This check might be redundant, to check, the presence of this Port in keys
                    # in logical_port_list but keep for now for coherency
                    # also skip checking in logical_port_list inside sfp_util

                    fvp_dict = dict(fvp)

                    if "download_firmware" in fvp_dict:

                        file_name = fvp_dict["download_firmware"]
                        file_full_path = '/usr/share/sonic/firmware/{}'.format(file_name)

                        status = -1

                        if not os.path.isfile(file_full_path):
                            helper_logger.log_error("Error: cli cmd download firmware file does not exist port {} file {}".format(port, file_name))
                            set_result_and_delete_port('status', status, xcvrd_down_fw_cmd_sts_tbl[asic_index], xcvrd_down_fw_rsp_tbl[asic_index], port)
                            break

                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get physical port for cli cmd download firmware cli Y cable port {}".format(port))
                            set_result_and_delete_port('status', status, xcvrd_down_fw_cmd_sts_tbl[asic_index], xcvrd_down_fw_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            helper_logger.log_error(
                                "Error: Could not get port instance for cli cmd download firmware Y cable port {}".format(port))
                            set_result_and_delete_port('status', status, xcvrd_down_fw_cmd_sts_tbl[asic_index], xcvrd_down_fw_rsp_tbl[asic_index], port)
                            break

                        rc = {}
                        self.task_download_firmware_thread[physical_port] = threading.Thread(target=task_download_firmware_worker, args=(port, physical_port, port_instance, file_full_path, xcvrd_down_fw_rsp_tbl[asic_index], xcvrd_down_fw_cmd_sts_tbl[asic_index], rc,))
                        self.task_download_firmware_thread[physical_port].start()
                    else:
                        helper_logger.log_error(
                            "Error: Wrong input parameter get for cli cmd download firmware Y cable port {}".format(port))
                        set_result_and_delete_port('status', '-1', xcvrd_down_fw_cmd_sts_tbl[asic_index], xcvrd_down_fw_rsp_tbl[asic_index], port)

            while True:
                (port, op, fvp) = xcvrd_show_fw_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)

                    mux_info_dict = {}
                    mux_info_dict['version_self_active'] = 'N/A'
                    mux_info_dict['version_self_inactive'] = 'N/A'
                    mux_info_dict['version_self_next'] = 'N/A'
                    mux_info_dict['version_peer_active'] = 'N/A'
                    mux_info_dict['version_peer_inactive'] = 'N/A'
                    mux_info_dict['version_peer_next'] = 'N/A'
                    mux_info_dict['version_nic_active'] = 'N/A'
                    mux_info_dict['version_nic_inactive'] = 'N/A'
                    mux_info_dict['version_nic_next'] = 'N/A'

                    if "firmware_version" in fvp_dict:


                        status = 'False'
                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_warning("Error: Could not get physical port for cli cmd show firmware port {}".format(port))
                            set_result_and_delete_port('status', status, xcvrd_show_fw_cmd_sts_tbl[asic_index], xcvrd_show_fw_rsp_tbl[asic_index], port)
                            set_show_firmware_fields(port, mux_info_dict, xcvrd_show_fw_res_tbl[asic_index])
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            helper_logger.log_warning("Error: Could not get port instance for cli cmd show firmware command port {}".format(port))
                            set_show_firmware_fields(port, mux_info_dict, xcvrd_show_fw_res_tbl[asic_index])
                            set_result_and_delete_port('status', status, xcvrd_show_fw_cmd_sts_tbl[asic_index], xcvrd_show_fw_rsp_tbl[asic_index], port)
                            break

                        with y_cable_port_locks[physical_port]:
                            try:
                                read_side = port_instance.get_read_side()
                            except Exception as e:
                                read_side = None
                                helper_logger.log_warning("Failed to execute the get_read_side API for port {} due to {}".format(physical_port,repr(e)))
                        if read_side is None or read_side is port_instance.EEPROM_ERROR or read_side < 0:

                            status = 'False'
                            helper_logger.log_warning("Error: Could not get read side for cli cmd show firmware port {}".format(port))
                            set_show_firmware_fields(port, mux_info_dict, xcvrd_show_fw_res_tbl[asic_index])
                            set_result_and_delete_port('status', status, xcvrd_show_fw_cmd_sts_tbl[asic_index], xcvrd_show_fw_rsp_tbl[asic_index], port)
                            break


                        get_firmware_dict(physical_port, port_instance, port_instance.TARGET_NIC, "nic", mux_info_dict, port)
                        if read_side == port_instance.TARGET_TOR_A:
                            get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_A, "self", mux_info_dict, port)
                            get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_B, "peer", mux_info_dict, port)
                        else:
                            get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_A, "peer", mux_info_dict, port)
                            get_firmware_dict(physical_port, port_instance, port_instance.TARGET_TOR_B, "self", mux_info_dict, port)

                        status = 'True'
                        set_show_firmware_fields(port, mux_info_dict, xcvrd_show_fw_res_tbl[asic_index])
                        set_result_and_delete_port('status', status, xcvrd_show_fw_cmd_sts_tbl[asic_index], xcvrd_show_fw_rsp_tbl[asic_index], port)
                    else:
                        helper_logger.log_error("Wrong param for cli cmd show firmware port {}".format(port))
                        set_show_firmware_fields(port, mux_info_dict, xcvrd_show_fw_res_tbl[asic_index])
                        set_result_and_delete_port('status', 'False', xcvrd_show_fw_cmd_sts_tbl[asic_index], xcvrd_show_fw_rsp_tbl[asic_index], port)



            while True:
                (port, op, fvp) = xcvrd_acti_fw_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)

                    if "activate_firmware" in fvp_dict:
                        file_name = fvp_dict["activate_firmware"]
                        status = 'False'

                        if file_name == 'null':
                            file_full_path = None
                        else:
                            file_full_path = '/usr/share/sonic/firmware/{}'.format(file_name)
                            if not os.path.isfile(file_full_path):
                                helper_logger.log_error("ERROR: cli cmd mux activate firmware file does not exist port {} file {}".format(port, file_name))
                                set_result_and_delete_port('status', status, xcvrd_down_fw_cmd_sts_tbl[asic_index], xcvrd_down_fw_rsp_tbl[asic_index], port)
                                break


                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_warning("Error: Could not get physical port for cli cmd mux activate firmware port {}".format(port))
                            set_result_and_delete_port('status', status, xcvrd_acti_fw_cmd_sts_tbl[asic_index], xcvrd_acti_fw_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            helper_logger.log_warning("Error: Could not get port instance for cli cmd mux activate firmware port {}".format(port))
                            # error scenario update table accordingly
                            set_result_and_delete_port('status', status, xcvrd_acti_fw_cmd_sts_tbl[asic_index], xcvrd_acti_fw_rsp_tbl[asic_index], port)
                            break


                        with y_cable_port_locks[physical_port]:
                            try:
                                status = port_instance.activate_firmware(file_full_path, True)
                            except Exception as e:
                                status = -1
                                helper_logger.log_warning("Failed to execute the activate_firmware API for port {} due to {}".format(physical_port,repr(e)))

                        set_result_and_delete_port('status', status, xcvrd_acti_fw_cmd_sts_tbl[asic_index], xcvrd_acti_fw_rsp_tbl[asic_index], port)
                    else:
                        helper_logger.log_error("Wrong param for cli cmd mux activate firmware port {}".format(port))
                        set_result_and_delete_port('status', 'False', xcvrd_acti_fw_cmd_sts_tbl[asic_index], xcvrd_acti_fw_rsp_tbl[asic_index], port)

            while True:
                (port, op, fvp) = xcvrd_roll_fw_cmd_tbl[asic_index].pop()

                if not port:
                    break

                if fvp:

                    fvp_dict = dict(fvp)


                    if "rollback_firmware" in fvp_dict:
                        file_name = fvp_dict["rollback_firmware"]
                        status = 'False'

                        if file_name == 'null':
                            file_full_path = None
                        else:
                            file_full_path = '/usr/share/sonic/firmware/{}'.format(file_name)
                            if not os.path.isfile(file_full_path):
                                helper_logger.log_error("Error: cli cmd mux rollback firmware file does not exist port {} file {}".format(port, file_name))
                                set_result_and_delete_port('status', status, xcvrd_down_fw_cmd_sts_tbl[asic_index], xcvrd_down_fw_rsp_tbl[asic_index], port)
                                break



                        physical_port = get_ycable_physical_port_from_logical_port(port)
                        if physical_port is None or physical_port == PHYSICAL_PORT_MAPPING_ERROR:
                            # error scenario update table accordingly
                            helper_logger.log_warning("Error: Could not get physical port for cli cmd mux rollback firmware port {}".format(port))
                            set_result_and_delete_port('status', status, xcvrd_roll_fw_cmd_sts_tbl[asic_index], xcvrd_roll_fw_rsp_tbl[asic_index], port)
                            break

                        port_instance = get_ycable_port_instance_from_logical_port(port)
                        if port_instance is None or port_instance in port_mapping_error_values:
                            # error scenario update table accordingly
                            helper_logger.log_warning("Error: Could not get port instance for cli cmd mux rollback firmware port {}".format(port))
                            set_result_and_delete_port('status', status, xcvrd_roll_fw_cmd_sts_tbl[asic_index], xcvrd_roll_fw_rsp_tbl[asic_index], port)

                        with y_cable_port_locks[physical_port]:
                            try:
                                status = port_instance.rollback_firmware(file_full_path)
                            except Exception as e:
                                status = -1
                                helper_logger.log_warning("Failed to execute the rollback_firmware API for port {} due to {}".format(physical_port,repr(e)))
                        set_result_and_delete_port('status', status, xcvrd_roll_fw_cmd_sts_tbl[asic_index], xcvrd_roll_fw_rsp_tbl[asic_index], port)
                    else:
                        helper_logger.log_error("Wrong param for cli cmd mux rollback firmware port {}".format(port))
                        set_result_and_delete_port('status', 'False', xcvrd_roll_fw_cmd_sts_tbl[asic_index], xcvrd_roll_fw_rsp_tbl[asic_index], port)


    def task_run(self):
        self.task_thread = threading.Thread(target=self.task_worker)
        self.task_cli_thread = threading.Thread(target=self.task_cli_worker)
        self.task_thread.start()
        self.task_cli_thread.start()

    def task_stop(self):

        self.task_stopping_event.set()
        helper_logger.log_info("stopping the cli and probing task threads xcvrd")
        self.task_thread.join()
        self.task_cli_thread.join()
        
        for key, value in self.task_download_firmware_thread.items():
            self.task_download_firmware_thread[key].join()
        helper_logger.log_info("stopped all thread")
