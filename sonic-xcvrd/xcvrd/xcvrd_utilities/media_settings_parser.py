"""
This parser is responsible for parsing the ASIC side SerDes custom SI settings.
"""

import json
import os
import ast

from sonic_py_common import device_info, logger
from swsscommon import swsscommon
from xcvrd import xcvrd

g_dict = {}

LANE_SPEED_KEY_PREFIX = "speed:"
VENDOR_KEY = 'vendor_key'
MEDIA_KEY = 'media_key'
LANE_SPEED_KEY = 'lane_speed_key'
SYSLOG_IDENTIFIER = "xcvrd"
helper_logger = logger.Logger(SYSLOG_IDENTIFIER)


def load_media_settings():
    global g_dict
    (platform_path, _) = device_info.get_paths_to_platform_and_hwsku_dirs()

    media_settings_file_path = os.path.join(platform_path, "media_settings.json")
    if not os.path.isfile(media_settings_file_path):
        helper_logger.log_info("xcvrd: No media file exists")
        return {}

    with open(media_settings_file_path, "r") as media_file:
        g_dict = json.load(media_file)


def media_settings_present():
    if g_dict:
        return True
    return False


def get_lane_speed_key(physical_port, port_speed, lane_count):
    sfp = xcvrd.platform_chassis.get_sfp(physical_port)
    api = sfp.get_xcvr_api()
    
    lane_speed_key = None
    if xcvrd.is_cmis_api(api):
        appl_adv_dict = api.get_application_advertisement()
        app_id = xcvrd.get_cmis_application_desired(api, int(lane_count), int(port_speed))
        if app_id and app_id in appl_adv_dict:
            host_electrical_interface_id = appl_adv_dict[app_id].get('host_electrical_interface_id')
            if host_electrical_interface_id:
                lane_speed_key = LANE_SPEED_KEY_PREFIX + host_electrical_interface_id.split()[0]

    return lane_speed_key


def get_media_settings_key(physical_port, transceiver_dict, port_speed, lane_count):
    sup_compliance_str = '10/40G Ethernet Compliance Code'
    sup_len_str = 'Length Cable Assembly(m)'
    vendor_name_str = transceiver_dict[physical_port]['manufacturer']
    vendor_pn_str = transceiver_dict[physical_port]['model']
    vendor_key = vendor_name_str.upper() + '-' + vendor_pn_str

    media_len = ''
    if transceiver_dict[physical_port]['cable_type'] == sup_len_str:
        media_len = transceiver_dict[physical_port]['cable_length']

    media_compliance_dict_str = transceiver_dict[physical_port]['specification_compliance']
    media_compliance_code = ''
    media_type = ''
    media_key = ''
    media_compliance_dict = {}

    try:
        sfp = xcvrd.platform_chassis.get_sfp(physical_port)
        api = sfp.get_xcvr_api()
        if xcvrd.is_cmis_api(api):
            media_compliance_code = media_compliance_dict_str
        else:
            media_compliance_dict = ast.literal_eval(media_compliance_dict_str)
            if sup_compliance_str in media_compliance_dict:
                media_compliance_code = media_compliance_dict[sup_compliance_str]
    except ValueError as e:
        helper_logger.log_error("Invalid value for port {} 'specification_compliance': {}".format(physical_port, media_compliance_dict_str))

    media_type = transceiver_dict[physical_port]['type_abbrv_name']

    if len(media_type) != 0:
        media_key += media_type
    if len(media_compliance_code) != 0:
        media_key += '-' + media_compliance_code
        sfp = xcvrd.platform_chassis.get_sfp(physical_port)
        api = sfp.get_xcvr_api()
        if xcvrd.is_cmis_api(api):
            if media_compliance_code == "passive_copper_media_interface":
                if media_len != 0:
                    media_key += '-' + str(media_len) + 'M'
        else:
            if media_len != 0:
                media_key += '-' + str(media_len) + 'M'
    else:
        media_key += '-' + '*'

    lane_speed_key = get_lane_speed_key(physical_port, port_speed, lane_count)
    # return (vendor_key, media_key, lane_speed_key)
    return {
        VENDOR_KEY: vendor_key,
        MEDIA_KEY: media_key,
        LANE_SPEED_KEY: lane_speed_key
    }


def is_si_per_speed_supported(media_dict):
    return LANE_SPEED_KEY_PREFIX in list(media_dict.keys())[0]


def get_media_val_str_from_dict(media_dict):
    LANE_STR = 'lane'
    LANE_SEPARATOR = ','

    media_str = ''
    tmp_dict = {}

    for keys in media_dict:
        lane_num = int(keys.strip()[len(LANE_STR):])
        tmp_dict[lane_num] = media_dict[keys]

    for key in range(0, len(tmp_dict)):
        media_str += tmp_dict[key]
        if key != list(tmp_dict.keys())[-1]:
            media_str += LANE_SEPARATOR
    return media_str


def get_media_val_str(num_logical_ports, lane_dict, logical_idx):
    LANE_STR = 'lane'

    logical_media_dict = {}
    num_lanes_on_port = len(lane_dict)

    # The physical ports has more than one logical port meaning it is
    # in breakout mode. So fetch the corresponding lanes from the file
    media_val_str = ''
    if (num_logical_ports > 1) and \
       (num_lanes_on_port >= num_logical_ports):
        num_lanes_per_logical_port = num_lanes_on_port//num_logical_ports
        start_lane = logical_idx * num_lanes_per_logical_port

        for lane_idx in range(start_lane, start_lane +
                              num_lanes_per_logical_port):
            lane_idx_str = LANE_STR + str(lane_idx)
            logical_lane_idx_str = LANE_STR + str(lane_idx - start_lane)
            logical_media_dict[logical_lane_idx_str] = lane_dict[lane_idx_str]

        media_val_str = get_media_val_str_from_dict(logical_media_dict)
    else:
        media_val_str = get_media_val_str_from_dict(lane_dict)
    return media_val_str


def get_media_settings_value(physical_port, key):
    GLOBAL_MEDIA_SETTINGS_KEY = 'GLOBAL_MEDIA_SETTINGS'
    PORT_MEDIA_SETTINGS_KEY = 'PORT_MEDIA_SETTINGS'
    DEFAULT_KEY = 'Default'
    RANGE_SEPARATOR = '-'
    COMMA_SEPARATOR = ','
    media_dict = {}
    default_dict = {}

    # Keys under global media settings can be a list or range or list of ranges
    # of physical port numbers. Below are some examples
    # 1-32
    # 1,2,3,4,5
    # 1-4,9-12

    if GLOBAL_MEDIA_SETTINGS_KEY in g_dict:
        for keys in g_dict[GLOBAL_MEDIA_SETTINGS_KEY]:
            if COMMA_SEPARATOR in keys:
                port_list = keys.split(COMMA_SEPARATOR)
                for port in port_list:
                    if RANGE_SEPARATOR in port:
                        if xcvrd.check_port_in_range(port, physical_port):
                            media_dict = g_dict[GLOBAL_MEDIA_SETTINGS_KEY][keys]
                            break
                    elif str(physical_port) == port:
                        media_dict = g_dict[GLOBAL_MEDIA_SETTINGS_KEY][keys]
                        break

            elif RANGE_SEPARATOR in keys:
                if xcvrd.check_port_in_range(keys, physical_port):
                    media_dict = g_dict[GLOBAL_MEDIA_SETTINGS_KEY][keys]

            # If there is a match in the global profile for a media type,
            # fetch those values
            if key[VENDOR_KEY] in media_dict: # e.g: 'AMPHENOL-1234'
                if is_si_per_speed_supported(media_dict[key[VENDOR_KEY]]):
                    if key[LANE_SPEED_KEY] is not None and key[LANE_SPEED_KEY] in media_dict[key[VENDOR_KEY]]: # e.g: 'speed:400GAUI-8'
                        return media_dict[key[VENDOR_KEY]][key[LANE_SPEED_KEY]]
                    else:
                        return {}
                else:
                    return media_dict[key[VENDOR_KEY]]
            elif key[VENDOR_KEY].split('-')[0] in media_dict:
                if is_si_per_speed_supported(media_dict[key[VENDOR_KEY].split('-')[0]]):
                    if key[LANE_SPEED_KEY] is not None and key[LANE_SPEED_KEY] in media_dict[key[VENDOR_KEY].split('-')[0]]:
                        return media_dict[key[VENDOR_KEY].split('-')[0]][key[LANE_SPEED_KEY]]
                    else:
                        return {}
                else:
                    return media_dict[key[VENDOR_KEY].split('-')[0]]
            elif key[MEDIA_KEY] in media_dict: # e.g: 'QSFP28-40GBASE-CR4-1M'
                if is_si_per_speed_supported(media_dict[key[MEDIA_KEY]]):
                    if key[LANE_SPEED_KEY] is not None and key[LANE_SPEED_KEY] in media_dict[key[MEDIA_KEY]]:
                        return media_dict[key[MEDIA_KEY]][key[LANE_SPEED_KEY]]
                    else:
                        return {}
                else:
                    return media_dict[key[MEDIA_KEY]]
            elif DEFAULT_KEY in media_dict:
                default_dict = media_dict[DEFAULT_KEY]

    media_dict = {}

    if PORT_MEDIA_SETTINGS_KEY in g_dict:
        for keys in g_dict[PORT_MEDIA_SETTINGS_KEY]:
            if int(keys) == physical_port:
                media_dict = g_dict[PORT_MEDIA_SETTINGS_KEY][keys]
                break

        if len(media_dict) == 0:
            if len(default_dict) != 0:
                return default_dict
            else:
                helper_logger.log_info("Error: No values for physical port '{}'".format(physical_port))
            return {}

        if key[VENDOR_KEY] in media_dict:
            if is_si_per_speed_supported(media_dict[key[VENDOR_KEY]]):
                if key[LANE_SPEED_KEY] is not None and key[LANE_SPEED_KEY] in media_dict[key[VENDOR_KEY]]:
                    return media_dict[key[VENDOR_KEY]][key[LANE_SPEED_KEY]]
                else:
                    return {}
            else:
                return media_dict[key[VENDOR_KEY]]
        if key[VENDOR_KEY].split('-')[0] in media_dict:
            if is_si_per_speed_supported(media_dict[key[VENDOR_KEY].split('-')[0]]):
                if key[LANE_SPEED_KEY] is not None and key[LANE_SPEED_KEY] in media_dict[key[VENDOR_KEY].split('-')[0]]:
                    return media_dict[key[VENDOR_KEY].split('-')[0]][key[LANE_SPEED_KEY]]
                else:
                    return {}
            else:
                return media_dict[key[VENDOR_KEY].split('-')[0]]
        elif key[MEDIA_KEY] in media_dict:
            if is_si_per_speed_supported(media_dict[key[MEDIA_KEY]]):
                if key[LANE_SPEED_KEY] is not None and key[LANE_SPEED_KEY] in media_dict[key[MEDIA_KEY]]:
                    return media_dict[key[MEDIA_KEY]][key[LANE_SPEED_KEY]]
                else:
                    return {}
            else:
                return media_dict[key[MEDIA_KEY]]
        elif DEFAULT_KEY in media_dict:
            return media_dict[DEFAULT_KEY]
        elif len(default_dict) != 0:
            return default_dict
    else:
        if len(default_dict) != 0:
            return default_dict

    return {}


def get_speed_and_lane_count(port, cfg_port_tbl):
    port_speed, lane_count = '0', 0
    found, port_info = cfg_port_tbl.get(port)
    port_info_dict = dict(port_info)
    if found and 'speed' in port_info_dict and 'lanes' in port_info_dict:
        port_speed = port_info_dict['speed']
        lanes = port_info_dict['lanes']
        lane_count = len(lanes.split(','))
    return port_speed, lane_count


"""
    This function returns the NPU SI settings for a given logical port
    Args:
        lport:
            logical port name
        transceiver_dict:
            A dictionary containing the vendor specific transceiver information
        cfg_port_tbl:
            A ConfigDBConnector object
        port_mapping:
            A PortMapping object
    Returns:
        A dictionary containing the NPU SI settings for the logical port
        Returns empty dictionary if the NPU SI settings are not found
"""
def get_npu_si_settings_dict(lport, transceiver_dict, cfg_port_tbl, port_mapping):
    if not media_settings_present():
        return {}

    port_speed, lane_count = get_speed_and_lane_count(lport, cfg_port_tbl)

    physical_port = port_mapping.get_logical_to_physical(lport)[0]
    if not xcvrd._wrapper_get_presence(physical_port):
        helper_logger.log_info("Get NPU SI settings: Media {} presence not detected during notify".format(physical_port))
        return {}
    if physical_port not in transceiver_dict:
        helper_logger.log_error("Get NPU SI settings: Media {} eeprom not populated in transceiver dict".format(physical_port))
        return {}

    key = get_media_settings_key(physical_port, transceiver_dict, port_speed, lane_count)
    helper_logger.log_debug("Get NPU SI settings: Retrieving media settings for port {}, operating at a speed "
                            "of {} with a lane count of {}, using the following lookup keys: {}".format(lport, port_speed, lane_count, key))
    media_dict = get_media_settings_value(physical_port, key)

    if len(media_dict) == 0:
        helper_logger.log_info("Get NPU SI settings: Error in obtaining media setting for {}".format(lport))
        return {}
    else:
        helper_logger.log_info("Get NPU SI settings: Media settings for {} obtained successfully".format(lport))
        return media_dict

"""
    This function updates the NPU SI settings for a given logical port to APPL_DB
    It then sets the value of NPU_SI_SETTINGS_SYNC_STATUS to NPU_SI_SETTINGS_NOTIFIED in STATE_DB|PORT_TABLE
    Args:
        lport:
            logical port name
        transceiver_dict:
            A dictionary containing the vendor specific transceiver information
        xcvr_table_helper:
            A XcvrTableHelper object
        port_mapping:
            A PortMapping object
"""
def post_npu_si_settings_to_appl_db(lport, transceiver_dict,
                                     xcvr_table_helper, port_mapping):
    asic_index = port_mapping.get_asic_id_for_logical_port(lport)

    media_dict = get_npu_si_settings_dict(lport, transceiver_dict, xcvr_table_helper.get_cfg_port_tbl(asic_index), port_mapping)
    if len(media_dict) == 0:
        helper_logger.log_error("NPI SI settings post: Media dict is empty for {}".format(lport))
        return

    physical_port = port_mapping.get_logical_to_physical(lport)[0]
    logical_port_list = port_mapping.get_physical_to_logical(physical_port)
    num_logical_ports = len(logical_port_list)
    logical_idx = logical_port_list.index(lport)

    fvs = swsscommon.FieldValuePairs(len(media_dict))

    index = 0
    helper_logger.log_debug("NPU SI settings post: Publishing ASIC-side SI setting for port {} in APP_DB:".format(lport))
    for media_key in media_dict:
        if type(media_dict[media_key]) is dict:
            media_val_str = get_media_val_str(num_logical_ports,
                                                media_dict[media_key],
                                                logical_idx)
        else:
            media_val_str = media_dict[media_key]
        helper_logger.log_debug("{}:({},{}) ".format(index, str(media_key), str(media_val_str)))
        fvs[index] = (str(media_key), str(media_val_str))
        index += 1

    xcvr_table_helper.get_app_port_tbl(asic_index).set(lport, fvs)
    state_port_table = xcvr_table_helper.get_state_port_tbl(asic_index)
    state_port_table.set(lport, [("NPU_SI_SETTINGS_SYNC_STATUS", "NPU_SI_SETTINGS_NOTIFIED")])
    helper_logger.log_notice("{}: Posted NPU settings to APPL_DB".format(lport))
