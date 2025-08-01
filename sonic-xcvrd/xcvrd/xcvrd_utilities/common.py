#!/usr/bin/env python3

"""
    common
    Common utilities for xcvrd daemon components
"""

try:
    import sys
    import subprocess
    import traceback
    from swsscommon import swsscommon
    from sonic_py_common import syslogger
    from . import sfp_status_helper
    from sonic_platform_base.sonic_xcvr.api.public.c_cmis import CmisApi

except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

SYSLOG_IDENTIFIER_COMMON = "common"

# Global variables that will be injected from the parent module
platform_chassis = None
platform_sfputil = None
helper_logger = syslogger.SysLogger(SYSLOG_IDENTIFIER_COMMON, enable_runtime_config=True)

def init_globals(chassis, sfputil):
    """Initialize global variables with injected dependencies"""
    global platform_chassis, platform_sfputil, helper_logger
    platform_chassis = chassis
    platform_sfputil = sfputil

def log_exception_traceback():
    """Log exception traceback using the helper logger"""
    exc_type, exc_value, exc_traceback = sys.exc_info()
    msg = traceback.format_exception(exc_type, exc_value, exc_traceback)
    for tb_line in msg:
        for tb_line_split in tb_line.splitlines():
            helper_logger.log_error(tb_line_split)

def update_port_transceiver_status_table_sw(logical_port_name, status_sw_tbl, status, error_descriptions='N/A'):
    """Update port SFP status table for SW fields on receiving SFP change event"""
    fvs = swsscommon.FieldValuePairs([('status', status), ('error', error_descriptions)])
    status_sw_tbl.set(logical_port_name, fvs)

def _wrapper_get_presence(physical_port):
    """Wrapper function to get SFP presence status"""
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).get_presence()
        except NotImplementedError:
            pass
    if platform_sfputil is not None:
        try:
            return platform_sfputil.get_presence(physical_port)
        except NotImplementedError:
            pass
    return False

def is_fast_reboot_enabled():
    """Check if fast reboot is enabled"""
    fastboot_enabled = subprocess.check_output('sonic-db-cli STATE_DB hget "FAST_RESTART_ENABLE_TABLE|system" enable', shell=True, universal_newlines=True)
    return "true" in fastboot_enabled

def is_warm_reboot_enabled():
    """Check if warm reboot is enabled"""
    warmstart = swsscommon.WarmStart()
    warmstart.initialize("xcvrd", "pmon")
    warmstart.checkWarmStart("xcvrd", "pmon", False)
    is_warm_start = warmstart.isWarmStart()
    return is_warm_start

#
# CMIS Helper Functions ========================================================
#

def is_cmis_api(api):
    """Check if the API is a CMIS API"""
    return isinstance(api, CmisApi)

def get_interface_speed(ifname):
    """
    Get the port speed from the host interface name

    Args:
        ifname: String, interface name

    Returns:
        Integer, the port speed if success otherwise 0
    """
    # see HOST_ELECTRICAL_INTERFACE of sff8024.py
    speed = 0
    if '1.6T' in ifname:
        speed = 1600000
    elif '800G' in ifname:
        speed = 800000
    elif '400G' in ifname:
        speed = 400000
    elif '200G' in ifname:
        speed = 200000
    elif '100G' in ifname or 'CAUI-4' in ifname:
        speed = 100000
    elif '50G' in ifname or 'LAUI-2' in ifname:
        speed = 50000
    elif '40G' in ifname or 'XLAUI' in ifname or 'XLPPI' in ifname:
        speed = 40000
    elif '25G' in ifname:
        speed = 25000
    elif '10G' in ifname or 'SFI' in ifname or 'XFI' in ifname:
        speed = 10000
    elif '1000BASE' in ifname:
        speed = 1000

    return speed

def get_cmis_application_desired(api, host_lane_count, speed):
    """
    Get the CMIS application code that matches the specified host side configurations

    Args:
        api:
            XcvrApi object
        host_lane_count:
            Number of lanes on the host side
        speed:
            Integer, the port speed of the host interface

    Returns:
        Integer, the transceiver-specific application code
    """

    if speed == 0 or host_lane_count == 0:
        return None

    if not is_cmis_api(api):
        return None

    appl_dict = api.get_application_advertisement()
    for index, app_info in appl_dict.items():
        if (app_info.get('host_lane_count') == host_lane_count and
        get_interface_speed(app_info.get('host_electrical_interface_id')) == speed):
            return (index & 0xf)

    # Note: helper_logger is not available here, so we don't log
    return None

def get_cmis_state_from_state_db(lport, status_sw_tbl):
    """Get CMIS state from STATE_DB for a given logical port"""
    found, cmis_state = status_sw_tbl.hget(lport, 'cmis_state')
    return cmis_state if found else 'UNKNOWN'

#
# Physical Port Name Functions =================================================
#

def get_physical_port_name(logical_port, physical_port, ganged):
    """Get physical port name based on logical port and ganged status"""
    if ganged:
        return logical_port + ":{} (ganged)".format(physical_port)
    else:
        return logical_port

def get_physical_port_name_dict(logical_port_name, port_mapping):
    """Get physical port name dict (port_idx to port_name)"""
    ganged_port = False
    ganged_member_num = 1

    physical_port_list = port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
    if physical_port_list is None:
        helper_logger.log_error("No physical ports found for logical port '{}'".format(logical_port_name))
        return {}

    if len(physical_port_list) > 1:
        ganged_port = True

    port_name_dict = {}
    for physical_port in physical_port_list:
        port_name = get_physical_port_name(logical_port_name, ganged_member_num, ganged_port)
        ganged_member_num += 1
        port_name_dict[physical_port] = port_name

    return port_name_dict

#
# Wrapper Functions for Platform API ==========================================
#

def _wrapper_is_flat_memory(physical_port):
    """Check if transceiver is flat memory"""
    if platform_chassis is not None:
        try:
            sfp = platform_chassis.get_sfp(physical_port)
            api = sfp.get_xcvr_api()
            if not api:
                return True
            return api.is_flat_memory()
        except NotImplementedError:
            pass
    return None

def _wrapper_get_transceiver_firmware_info(physical_port):
    """Get transceiver firmware info"""
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).get_transceiver_info_firmware_versions()
        except NotImplementedError:
            pass
    return {}

def _wrapper_get_transceiver_pm(physical_port):
    """Get transceiver PM info"""
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).get_transceiver_pm()
        except NotImplementedError:
            pass
    return {}

#
# Database Helper Functions ===================================================
#

def del_port_sfp_dom_info_from_db(logical_port_name, port_mapping, tbl_to_del_list):
    """Delete port dom/sfp info from db"""
    physical_port_names = get_physical_port_name_dict(logical_port_name, port_mapping).values()
    for physical_port_name in physical_port_names:
        try:
            for tbl in filter(None, tbl_to_del_list):
                tbl._del(physical_port_name)
        except NotImplementedError:
            helper_logger.log_error("This functionality is currently not implemented for this platform")
            sys.exit(2)  # NOT_IMPLEMENTED_ERROR

#
# Utility Functions ===========================================================
#

def check_port_in_range(range_str, physical_port):
    """Check if physical port is in the specified range"""
    RANGE_SEPARATOR = '-'
    
    range_list = range_str.split(RANGE_SEPARATOR)
    start_num = int(range_list[0].strip())
    end_num = int(range_list[1].strip())
    if start_num <= physical_port <= end_num:
        return True
    return False
