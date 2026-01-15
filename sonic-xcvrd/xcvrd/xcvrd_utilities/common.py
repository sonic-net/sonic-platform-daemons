#!/usr/bin/env python3

"""
    common
    Common utilities for xcvrd daemon components
"""

try:
    import sys
    import subprocess
    import traceback
    import threading
    from swsscommon import swsscommon
    from sonic_py_common import syslogger, daemon_base, multi_asic
    from . import sfp_status_helper
    from sonic_platform_base.sonic_xcvr.api.public.c_cmis import CmisApi

except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


# CMIS States
CMIS_STATE_UNKNOWN = 'UNKNOWN'
CMIS_STATE_INSERTED = 'INSERTED'
CMIS_STATE_DP_PRE_INIT_CHECK = 'DP_PRE_INIT_CHECK'
CMIS_STATE_DP_DEINIT = 'DP_DEINIT'
CMIS_STATE_AP_CONF = 'AP_CONFIGURED'
CMIS_STATE_DP_ACTIVATE = 'DP_ACTIVATION'
CMIS_STATE_DP_INIT = 'DP_INIT'
CMIS_STATE_DP_TXON = 'DP_TXON'
CMIS_STATE_READY = 'READY'
CMIS_STATE_REMOVED = 'REMOVED'
CMIS_STATE_FAILED = 'FAILED'

CMIS_TERMINAL_STATES = {
    CMIS_STATE_FAILED,
    CMIS_STATE_READY,
    CMIS_STATE_REMOVED
}

# Global variables that will be injected from the parent module
platform_chassis = None
platform_sfputil = None

# Cache for thread-specific loggers to avoid creating multiple loggers for the same thread
thread_loggers = {}

def get_syslog_identifier_common():
    """Get syslog identifier based on current thread name, fallback to 'xcvrd_common'"""
    try:
        current_thread = threading.current_thread()
        thread_name = getattr(current_thread, 'name', None)
        if thread_name and thread_name != 'MainThread':
            return thread_name
    except Exception:
        pass
    return "xcvrd_common"

def get_helper_logger():
    """Get a thread-specific logger, creating one if it doesn't exist"""
    thread_id = threading.current_thread().ident
    thread_name = get_syslog_identifier_common()

    # Use thread_id as key to ensure thread safety
    if thread_id not in thread_loggers:
        thread_loggers[thread_id] = syslogger.SysLogger(thread_name, enable_runtime_config=True)

    return thread_loggers[thread_id]

# Create a module-level attribute that acts like a dynamic property
class HelperLoggerProxy:
    def __getattr__(self, name):
        return getattr(get_helper_logger(), name)

helper_logger = HelperLoggerProxy()

NOT_IMPLEMENTED_ERROR = 3

def init_globals(chassis, sfputil):
    """Initialize global variables with injected dependencies"""
    global platform_chassis, platform_sfputil
    platform_chassis = chassis
    platform_sfputil = sfputil

def get_namespace_from_asic_id(asic_id):
    """
    Get namespace string from ASIC ID.
    
    For single-ASIC systems, returns empty string.
    For multi-ASIC systems, returns 'asicN' where N is the asic_id.
    
    Args:
        asic_id: Integer ASIC ID (e.g., 0, 1, 2)
    
    Returns:
        str: Namespace string ('' for single-ASIC, 'asicN' for multi-ASIC)
    """
    if multi_asic.is_multi_asic():
        return 'asic{}'.format(asic_id)
    return ''

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
            if platform_sfputil is not None:
                try:
                    return platform_sfputil.get_presence(physical_port)
                except NotImplementedError:
                    pass
    else:
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

def is_syncd_warm_restore_complete(namespace=''):
    """
    This function determines whether syncd's restore count is not 0, which indicates warm-reboot
    to avoid premature config push by xcvrd that caused port flaps.
    
    Args:
        namespace: The namespace (asic) to check. Empty string for single-ASIC or default namespace.
                   For multi-ASIC systems, pass the specific namespace (e.g., 'asic0', 'asic1').
    """
    state_db = daemon_base.db_connect("STATE_DB", namespace=namespace)
    restore_count = state_db.hget("WARM_RESTART_TABLE|syncd", "restore_count")
    system_enabled = state_db.hget("WARM_RESTART_ENABLE_TABLE|system", "enable")
    try:
        # --- Handle restore_count (could be int, str, or None) ---
        if restore_count is not None:
            if isinstance(restore_count, int):
                if restore_count > 0:
                    return True
            elif isinstance(restore_count, str):
                if restore_count.strip().isdigit() and int(restore_count.strip()) > 0:
                    return True

        # --- Handle system_enabled (only care about "true"/"false"/None) ---
        if isinstance(system_enabled, str):
            if system_enabled.strip().lower() == "true":
                return True

    except Exception as e:
        helper_logger.log_warning(f"Unexpected value: restore_count={restore_count}, system_enabled={system_enabled}, namespace={namespace}, error={e}")
        log_exception_traceback()
    return False

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

    helper_logger.log_notice(f'No application found from {appl_dict} with host_lane_count={host_lane_count} speed={speed}')
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
            sys.exit(NOT_IMPLEMENTED_ERROR)

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
