#!/usr/bin/env python3

"""
    common
    Common utilities for xcvrd daemon components
"""

try:
    import sys
    import functools
    import subprocess
    import traceback
    import threading
    from dataclasses import dataclass
    from types import MappingProxyType
    from typing import Any, Callable, Dict, FrozenSet, Mapping, Set
    from swsscommon import swsscommon
    from sonic_py_common import syslogger, daemon_base, device_info, multi_asic
    from . import sfp_status_helper
    from .port_event_helper import PortMapping
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

# Useful constants for interpreting the contents of cpo.json
CPO_DEVICE_TYPE_OE = 'optical_engine'
CPO_DEVICE_TYPE_ELSFP = 'external_laser_source'


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

def get_port_device(physical_port: int) -> Any:
    if platform_chassis is None:
        return None
    try:
        cpo = platform_chassis.get_cpo(physical_port)
        if cpo is not None:
            return cpo
    except (NotImplementedError, AttributeError, IndexError):
        pass
    try:
        return platform_chassis.get_sfp(physical_port)
    except (NotImplementedError, AttributeError, IndexError):
        return None

def is_cpo_port(physical_port: int) -> bool:
    if platform_chassis is None:
        return False
    try:
        return platform_chassis.get_cpo(physical_port) is not None
    except (NotImplementedError, AttributeError, IndexError):
        return False

def is_pluggable_port(physical_port: int) -> bool:
    return not is_cpo_port(physical_port)

def _get_port_obj_dict(port_mapping_data: PortMapping | None,
                       port_filter: Callable[[int], bool]) -> Dict[int, Any]:
    """
    Create a dictionary mapping physical ports to their corresponding device objects,
    restricted to the ports accepted by port_filter.

    Args:
        port_mapping_data (PortMapping): The port mapping data.
        port_filter (Callable[[int], bool]): Predicate selecting the physical ports to include.

    Returns:
        Dict[int, Any]: A dictionary mapping physical ports to device objects.
    """
    if port_mapping_data is None or port_mapping_data.physical_to_logical is None:
        helper_logger.log_error("PORT OBJ INIT: Failed to get port mapping data")
        return {}

    obj_dict = {}
    for physical_port in port_mapping_data.physical_to_logical.keys():
        try:
            if port_filter(physical_port):
                obj_dict[physical_port] = get_port_device(physical_port)
        except Exception as e:
            helper_logger.log_error(f"PORT OBJ INIT: Failed to get device object for port {physical_port} due to {repr(e)}")

    return obj_dict

def get_cpo_obj_dict(port_mapping_data: PortMapping | None) -> Dict[int, Any]:
    """Create a dictionary mapping physical ports to their corresponding CPO objects."""
    return _get_port_obj_dict(port_mapping_data, is_cpo_port)

def get_pluggable_obj_dict(port_mapping_data: PortMapping | None) -> Dict[int, Any]:
    """Create a dictionary mapping physical ports to their corresponding SFP objects."""
    return _get_port_obj_dict(port_mapping_data, is_pluggable_port)

@dataclass(frozen=True)
class CpoTopology:
    """
    The CPO devices of a platform and the physical ports they drive.

    Attributes:
        pports_by_device: {device type: {device id: physical ports the device drives}}
        devices_by_pport: {physical port: ids of the devices driving the port}
    """
    pports_by_device: Mapping[str, Mapping[str, FrozenSet[int]]]
    devices_by_pport: Mapping[int, FrozenSet[str]]

@functools.cache
def _build_cpo_topology() -> CpoTopology:
    """
    Build the CPO device topology of the platform.

    The topology is described by cpo.json, which associates each platform.json
    interface with the devices driving it, and platform.json, which maps
    each of those interfaces to a physical port index. Both describe static
    platform data, so the result is memoized for the lifetime of the process.

    Returns:
        CpoTopology: The platform topology, empty if cpo.json is not available.
    """
    def freeze_topology(pports_by_device: Dict[str, Dict[str, Set[int]]],
                        devices_by_pport: Dict[int, Set[str]]) -> CpoTopology:
        """Convert the mutable topology accumulators into an immutable CpoTopology."""
        frozen_pports_by_device = {}
        for device_type, devices_of_type in pports_by_device.items():
            frozen_devices_of_type = {device_id: frozenset(pports)
                                      for device_id, pports in devices_of_type.items()}
            frozen_pports_by_device[device_type] = MappingProxyType(frozen_devices_of_type)

        frozen_devices_by_pport = {pport: frozenset(device_ids)
                                   for pport, device_ids in devices_by_pport.items()}
        return CpoTopology(MappingProxyType(frozen_pports_by_device),
                           MappingProxyType(frozen_devices_by_pport))

    cpo_data = device_info.get_cpo_data()
    if not cpo_data:
        helper_logger.log_notice("CPO TOPOLOGY: no cpo.json data available")
        return freeze_topology({}, {})

    pports_by_device = {}
    devices_by_pport = {}

    platform_interfaces = (device_info.get_platform_json_data() or {}).get('interfaces', {})
    devices = cpo_data.get('devices') or {}

    for ifname, if_data in (cpo_data.get('interfaces') or {}).items():
        pport = int(str(platform_interfaces[ifname]['index']).split(',')[0].strip())

        for associated_device in (if_data or {}).get('associated_devices') or []:
            device_id = associated_device['device_id']
            pports = pports_by_device.setdefault(devices[device_id]['device_type'], {})
            pports.setdefault(device_id, set()).add(pport)
            devices_by_pport.setdefault(pport, set()).add(device_id)

    return freeze_topology(pports_by_device, devices_by_pport)

def get_cpo_devices_of_pport(physical_port: int, device_type: str) -> Dict[str, FrozenSet[int]]:
    """
    Get the CPO devices of the given type driving physical_port.

    Args:
        physical_port (int): Physical port index
        device_type (str): cpo.json device type

    Returns:
        Dict[str, FrozenSet[int]]: {device id: all physical ports the device drives}, taken
        straight from the immutable platform topology. Empty if the topology is not
        available or no such device drives physical_port.
    """
    topology = _build_cpo_topology()
    pports_by_device = topology.pports_by_device.get(device_type) or {}
    return {device_id: pports_by_device[device_id]
            for device_id in topology.devices_by_pport.get(physical_port, ())
            if device_id in pports_by_device}

def _get_sibling_pports(physical_port: int, device_type: str) -> FrozenSet[int]:
    """
    Get all physical ports sharing the given type of CPO device with physical_port.

    Args:
        physical_port (int): Physical port index
        device_type (str): cpo.json device type

    Returns:
        FrozenSet[int]: Physical port indexes, always including physical_port itself.
        Only physical_port is returned if the platform topology is not available.
    """
    siblings = {physical_port}
    for pports in get_cpo_devices_of_pport(physical_port, device_type).values():
        siblings.update(pports)
    return frozenset(siblings)

def get_oe_sibling_pports(physical_port: int) -> FrozenSet[int]:
    """
    Get all physical ports sharing an optical engine with physical_port, including
    physical_port itself.
    """
    return _get_sibling_pports(physical_port, CPO_DEVICE_TYPE_OE)

def get_elsfp_sibling_pports(physical_port: int) -> FrozenSet[int]:
    """
    Get all physical ports sharing an ELSFP with physical_port, including
    physical_port itself.
    """
    return _get_sibling_pports(physical_port, CPO_DEVICE_TYPE_ELSFP)

def is_copper(physical_port):
    """Check if the transceiver on the given physical port is copper"""
    if platform_chassis:
        try:
            return platform_chassis.get_sfp(physical_port).get_xcvr_api().is_copper()
        except (NotImplementedError, AttributeError):
            helper_logger.log_debug(f"No is_copper() defined for xcvr api on physical port {physical_port}, assuming Copper")
    return True

def _wrapper_get_presence(physical_port):
    """Wrapper function to get SFP presence status"""
    if platform_chassis is not None:
        try:
            device = get_port_device(physical_port)
            if device is not None:
                return device.get_presence()
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

def is_fast_reboot_enabled(namespace=''):
    """Check if fast reboot is enabled"""
    state_db = daemon_base.db_connect("STATE_DB", namespace=namespace)
    fastboot_enabled = state_db.hget("FAST_RESTART_ENABLE_TABLE|system", "enable")
    if isinstance(fastboot_enabled, str):
        return fastboot_enabled.strip().lower() == "true"
    return False

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
