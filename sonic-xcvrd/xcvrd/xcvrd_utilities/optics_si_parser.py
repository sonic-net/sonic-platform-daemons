import json
import os
import re

from sonic_py_common import device_info, syslogger
from xcvrd import xcvrd
from . import common

g_optics_si_dict = {}

SYSLOG_IDENTIFIER = "xcvrd"
helper_logger = syslogger.SysLogger(SYSLOG_IDENTIFIER, enable_runtime_config=True)

def _match_optics_si_key(dict_key, key, vendor_name_str):
    """
    Helper function to match optics SI key using regex patterns or string comparison

    Args:
        dict_key: Key from optics SI settings (can be regex pattern)
        key: Vendor key to match (e.g., "ABCDE-1234")
        vendor_name_str: Vendor name string (e.g., "ABCDE")

    Returns:
        True if match found, False otherwise
    """
    try:
        # Use re.fullmatch to match the entire string with regex patterns
        # This supports patterns like "ABCDE-(1234|56789)"
        if (re.fullmatch(dict_key, key) or \
            re.fullmatch(dict_key, key.split('-')[0]) or \
            re.fullmatch(dict_key, vendor_name_str)):
            return True
    except re.error:
        # If regex pattern is invalid, fall back to simple string comparison
        if (dict_key == key or \
            dict_key == key.split('-')[0] or \
            dict_key == vendor_name_str):
            return True
    return False

def _get_global_media_settings(physical_port, lane_speed, key, vendor_name_str):
    """
    Get optics SI settings from global media settings

    Args:
        physical_port: Physical port number
        lane_speed: Lane speed value
        key: Vendor key to match
        vendor_name_str: Vendor name string

    Returns:
        Tuple of (settings_dict, default_dict)
    """
    GLOBAL_MEDIA_SETTINGS_KEY = 'GLOBAL_MEDIA_SETTINGS'
    DEFAULT_KEY = 'Default'
    SPEED_KEY = str(lane_speed) + 'G_SPEED'
    RANGE_SEPARATOR = '-'
    COMMA_SEPARATOR = ','
    default_dict = {}
    optics_si_dict = {}

    if GLOBAL_MEDIA_SETTINGS_KEY in g_optics_si_dict:
        for keys in g_optics_si_dict[GLOBAL_MEDIA_SETTINGS_KEY]:
            if COMMA_SEPARATOR in keys:
                port_list = keys.split(COMMA_SEPARATOR)
                for port in port_list:
                    if RANGE_SEPARATOR in port:
                        if common.check_port_in_range(port, physical_port):
                            optics_si_dict = g_optics_si_dict[GLOBAL_MEDIA_SETTINGS_KEY][keys]
                            break
                    elif str(physical_port) == port:
                        optics_si_dict = g_optics_si_dict[GLOBAL_MEDIA_SETTINGS_KEY][keys]
                        break

            elif RANGE_SEPARATOR in keys:
                if common.check_port_in_range(keys, physical_port):
                    optics_si_dict = g_optics_si_dict[GLOBAL_MEDIA_SETTINGS_KEY][keys]

            if SPEED_KEY in optics_si_dict:
                # Iterate through each key in optics_si_dict[SPEED_KEY] and use regex matching
                for dict_key in optics_si_dict[SPEED_KEY].keys():
                    if _match_optics_si_key(dict_key, key, vendor_name_str):
                        return optics_si_dict[SPEED_KEY][dict_key], default_dict

                # If no match found, try default
                if DEFAULT_KEY in optics_si_dict[SPEED_KEY]:
                    default_dict = optics_si_dict[SPEED_KEY][DEFAULT_KEY]

    return None, default_dict

def _get_port_media_settings(physical_port, lane_speed, key, vendor_name_str, default_dict):
    """
    Get optics SI settings from port-specific media settings

    Args:
        physical_port: Physical port number
        lane_speed: Lane speed value
        key: Any key to match
        vendor_name_str: Vendor name string
        default_dict: Default settings from global media settings

    Returns:
        Settings dictionary or default_dict
    """
    PORT_MEDIA_SETTINGS_KEY = 'PORT_MEDIA_SETTINGS'
    DEFAULT_KEY = 'Default'
    SPEED_KEY = str(lane_speed) + 'G_SPEED'
    optics_si_dict = {}

    if PORT_MEDIA_SETTINGS_KEY in g_optics_si_dict:
        for keys in g_optics_si_dict[PORT_MEDIA_SETTINGS_KEY]:
            if int(keys) == physical_port:
                optics_si_dict = g_optics_si_dict[PORT_MEDIA_SETTINGS_KEY][keys]
                break

        if len(optics_si_dict) == 0:
            if len(default_dict) != 0:
                return default_dict
            else:
                helper_logger.log_info("No values for physical port '{}' lane speed '{}' "
                                       "key '{}' vendor '{}'".format(
                                       physical_port, lane_speed, key, vendor_name_str))
            return {}

        if SPEED_KEY in optics_si_dict:
            # Iterate through each key in optics_si_dict[SPEED_KEY] and use regex matching
            for dict_key in optics_si_dict[SPEED_KEY].keys():
                if _match_optics_si_key(dict_key, key, vendor_name_str):
                    return optics_si_dict[SPEED_KEY][dict_key]

            # If no match found, try default
            if DEFAULT_KEY in optics_si_dict[SPEED_KEY]:
                return optics_si_dict[SPEED_KEY][DEFAULT_KEY]
            elif len(default_dict) != 0:
                return default_dict

    return default_dict

def get_optics_si_settings_value(physical_port, lane_speed, key, vendor_name_str):
    """
    Get optics SI settings value for the given parameters

    Args:
        physical_port: Physical port number
        lane_speed: Lane speed value
        key: Any key to match
        vendor_name_str: Vendor name string

    Returns:
        Settings dictionary
    """
    # Try to get settings from global media settings first
    global_settings, default_dict = _get_global_media_settings(physical_port, lane_speed, key, vendor_name_str)
    if global_settings is not None:
        return global_settings

    # If not found in global settings, try port-specific settings
    port_settings = _get_port_media_settings(physical_port, lane_speed, key, vendor_name_str, default_dict)
    return port_settings

def get_module_vendor_key(physical_port, sfp):
    api = sfp.get_xcvr_api()
    if api is None:
        helper_logger.log_info("Module {} xcvrd api not found".format(physical_port))
        return None

    vendor_name = api.get_manufacturer()
    if vendor_name is None:
        helper_logger.log_info("Module {} vendor name not found".format(physical_port))
        return None

    vendor_pn = api.get_model()
    if vendor_pn is None:
        helper_logger.log_info("Module {} vendor part number not found".format(physical_port))
        return None

    return vendor_name.upper().strip() + '-' + vendor_pn.upper().strip(), vendor_name.upper().strip()

def fetch_optics_si_setting(physical_port, lane_speed, sfp):
    if not g_optics_si_dict:
        return

    optics_si = {}

    if not common._wrapper_get_presence(physical_port):
        helper_logger.log_info("Module {} presence not detected during notify".format(physical_port))
        return optics_si
    vendor_key, vendor_name = get_module_vendor_key(physical_port, sfp)
    if vendor_key is None or vendor_name is None:
        helper_logger.log_error("Error: No Vendor Key found for Module {}".format(physical_port))
        return optics_si
    optics_si = get_optics_si_settings_value(physical_port, lane_speed, vendor_key, vendor_name)
    return optics_si

def load_optics_si_settings():
    global g_optics_si_dict
    (platform_path, hwsku_path) = device_info.get_paths_to_platform_and_hwsku_dirs()

    # Support to fetch optics_si_settings.json both from platform folder and HWSKU folder
    optics_si_settings_file_path_platform = os.path.join(platform_path, "optics_si_settings.json")
    optics_si_settings_file_path_hwsku = os.path.join(hwsku_path, "optics_si_settings.json")

    if os.path.isfile(optics_si_settings_file_path_hwsku):
        optics_si_settings_file_path = optics_si_settings_file_path_hwsku
    elif os.path.isfile(optics_si_settings_file_path_platform):
        optics_si_settings_file_path = optics_si_settings_file_path_platform
    else:
        helper_logger.log_info("No optics SI file exists")
        return {}

    with open(optics_si_settings_file_path, "r") as optics_si_file:
        g_optics_si_dict = json.load(optics_si_file)

def optics_si_present():
    if g_optics_si_dict:
        return True
    return False

