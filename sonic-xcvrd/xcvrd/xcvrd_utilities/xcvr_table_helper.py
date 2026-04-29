try:
    from sonic_py_common import daemon_base, logger
    from sonic_py_common import multi_asic
    from swsscommon import swsscommon
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

SYSLOG_IDENTIFIER = "xcvrd"
helper_logger = logger.Logger(SYSLOG_IDENTIFIER)

TRANSCEIVER_INFO_TABLE = 'TRANSCEIVER_INFO'
TRANSCEIVER_FIRMWARE_INFO_TABLE = 'TRANSCEIVER_FIRMWARE_INFO'
TRANSCEIVER_DOM_SENSOR_TABLE = 'TRANSCEIVER_DOM_SENSOR'
TRANSCEIVER_DOM_FLAG_TABLE = 'TRANSCEIVER_DOM_FLAG'
TRANSCEIVER_DOM_FLAG_CHANGE_COUNT_TABLE = 'TRANSCEIVER_DOM_FLAG_CHANGE_COUNT'
TRANSCEIVER_DOM_FLAG_SET_TIME_TABLE = 'TRANSCEIVER_DOM_FLAG_SET_TIME'
TRANSCEIVER_DOM_FLAG_CLEAR_TIME_TABLE = 'TRANSCEIVER_DOM_FLAG_CLEAR_TIME'
TRANSCEIVER_DOM_THRESHOLD_TABLE = 'TRANSCEIVER_DOM_THRESHOLD'
TRANSCEIVER_DOM_TEMPERATURE_TABLE = 'TRANSCEIVER_DOM_TEMPERATURE'
TRANSCEIVER_STATUS_TABLE = 'TRANSCEIVER_STATUS'
TRANSCEIVER_STATUS_FLAG_TABLE = 'TRANSCEIVER_STATUS_FLAG'
TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT_TABLE = 'TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT'
TRANSCEIVER_STATUS_FLAG_SET_TIME_TABLE = 'TRANSCEIVER_STATUS_FLAG_SET_TIME'
TRANSCEIVER_STATUS_FLAG_CLEAR_TIME_TABLE = 'TRANSCEIVER_STATUS_FLAG_CLEAR_TIME'
TRANSCEIVER_STATUS_SW_TABLE = 'TRANSCEIVER_STATUS_SW'
TRANSCEIVER_VDM_REAL_VALUE_TABLE = 'TRANSCEIVER_VDM_REAL_VALUE'
TRANSCEIVER_VDM_HALARM_THRESHOLD_TABLE = 'TRANSCEIVER_VDM_HALARM_THRESHOLD'
TRANSCEIVER_VDM_LALARM_THRESHOLD_TABLE = 'TRANSCEIVER_VDM_LALARM_THRESHOLD'
TRANSCEIVER_VDM_HWARN_THRESHOLD_TABLE = 'TRANSCEIVER_VDM_HWARN_THRESHOLD'
TRANSCEIVER_VDM_LWARN_THRESHOLD_TABLE = 'TRANSCEIVER_VDM_LWARN_THRESHOLD'
TRANSCEIVER_VDM_HALARM_FLAG = 'TRANSCEIVER_VDM_HALARM_FLAG'
TRANSCEIVER_VDM_LALARM_FLAG = 'TRANSCEIVER_VDM_LALARM_FLAG'
TRANSCEIVER_VDM_HWARN_FLAG = 'TRANSCEIVER_VDM_HWARN_FLAG'
TRANSCEIVER_VDM_LWARN_FLAG = 'TRANSCEIVER_VDM_LWARN_FLAG'
TRANSCEIVER_VDM_HALARM_FLAG_CHANGE_COUNT = 'TRANSCEIVER_VDM_HALARM_FLAG_CHANGE_COUNT'
TRANSCEIVER_VDM_LALARM_FLAG_CHANGE_COUNT = 'TRANSCEIVER_VDM_LALARM_FLAG_CHANGE_COUNT'
TRANSCEIVER_VDM_HWARN_FLAG_CHANGE_COUNT = 'TRANSCEIVER_VDM_HWARN_FLAG_CHANGE_COUNT'
TRANSCEIVER_VDM_LWARN_FLAG_CHANGE_COUNT = 'TRANSCEIVER_VDM_LWARN_FLAG_CHANGE_COUNT'
TRANSCEIVER_VDM_HALARM_FLAG_SET_TIME = 'TRANSCEIVER_VDM_HALARM_FLAG_SET_TIME'
TRANSCEIVER_VDM_LALARM_FLAG_SET_TIME = 'TRANSCEIVER_VDM_LALARM_FLAG_SET_TIME'
TRANSCEIVER_VDM_HWARN_FLAG_SET_TIME = 'TRANSCEIVER_VDM_HWARN_FLAG_SET_TIME'
TRANSCEIVER_VDM_LWARN_FLAG_SET_TIME = 'TRANSCEIVER_VDM_LWARN_FLAG_SET_TIME'
TRANSCEIVER_VDM_HALARM_FLAG_CLEAR_TIME = 'TRANSCEIVER_VDM_HALARM_FLAG_CLEAR_TIME'
TRANSCEIVER_VDM_LALARM_FLAG_CLEAR_TIME = 'TRANSCEIVER_VDM_LALARM_FLAG_CLEAR_TIME'
TRANSCEIVER_VDM_HWARN_FLAG_CLEAR_TIME = 'TRANSCEIVER_VDM_HWARN_FLAG_CLEAR_TIME'
TRANSCEIVER_VDM_LWARN_FLAG_CLEAR_TIME = 'TRANSCEIVER_VDM_LWARN_FLAG_CLEAR_TIME'
TRANSCEIVER_PM_TABLE = 'TRANSCEIVER_PM'

VDM_THRESHOLD_TYPES = ['halarm', 'lalarm', 'hwarn', 'lwarn']

class XcvrTableHelper:
    def __init__(self, namespaces):
        self.int_tbl, self.dom_tbl, self.dom_threshold_tbl, self.status_tbl, self.app_port_tbl, \
		self.cfg_port_tbl, self.state_port_tbl, self.pm_tbl, self.firmware_info_tbl = {}, {}, {}, {}, {}, {}, {}, {}, {}
        self.state_db = {}
        self.appl_db = {}
        self.app_port_read_tbl = {}
        self.cfg_db = {}
        self.dom_temperature_tbl = {}
        self.dom_flag_tbl = {}
        self.dom_flag_change_count_tbl = {}
        self.dom_flag_set_time_tbl = {}
        self.dom_flag_clear_time_tbl = {}
        self.status_flag_tbl = {}
        self.status_flag_change_count_tbl = {}
        self.status_flag_set_time_tbl = {}
        self.status_flag_clear_time_tbl = {}
        self.status_sw_tbl = {}
        self.vdm_real_value_tbl = {}
        VDM_THRESHOLD_TYPES = ['halarm', 'lalarm', 'hwarn', 'lwarn']
        self.vdm_threshold_tbl = {f'vdm_{t}_threshold_tbl': {} for t in VDM_THRESHOLD_TYPES}
        self.vdm_flag_tbl = {f'vdm_{t}_flag_tbl': {} for t in VDM_THRESHOLD_TYPES}
        self.vdm_flag_change_count_tbl = {f'vdm_{t}_flag_change_count_tbl': {} for t in VDM_THRESHOLD_TYPES}
        self.vdm_flag_set_time_tbl = {f'vdm_{t}_flag_set_time_tbl': {} for t in VDM_THRESHOLD_TYPES}
        self.vdm_flag_clear_time_tbl = {f'vdm_{t}_flag_clear_time_tbl': {} for t in VDM_THRESHOLD_TYPES}
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            self.state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            self.int_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_INFO_TABLE)
            self.dom_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_SENSOR_TABLE)
            self.dom_flag_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_FLAG_TABLE)
            self.dom_flag_change_count_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_FLAG_CHANGE_COUNT_TABLE)
            self.dom_flag_set_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_FLAG_SET_TIME_TABLE)
            self.dom_flag_clear_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_FLAG_CLEAR_TIME_TABLE)
            self.dom_threshold_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_THRESHOLD_TABLE)
            self.dom_temperature_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_DOM_TEMPERATURE_TABLE)
            self.status_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_TABLE)
            self.status_flag_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_FLAG_TABLE)
            self.status_flag_change_count_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT_TABLE)
            self.status_flag_set_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_FLAG_SET_TIME_TABLE)
            self.status_flag_clear_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_FLAG_CLEAR_TIME_TABLE)
            self.status_sw_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_SW_TABLE)
            self.pm_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_PM_TABLE)
            self.firmware_info_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_FIRMWARE_INFO_TABLE)
            self.state_port_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], swsscommon.STATE_PORT_TABLE_NAME)
            self.appl_db[asic_id] = daemon_base.db_connect("APPL_DB", namespace)
            self.app_port_tbl[asic_id] = swsscommon.ProducerStateTable(self.appl_db[asic_id], swsscommon.APP_PORT_TABLE_NAME)
            self.app_port_read_tbl[asic_id] = swsscommon.Table(self.appl_db[asic_id], swsscommon.APP_PORT_TABLE_NAME)
            self.cfg_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            self.cfg_port_tbl[asic_id] = swsscommon.Table(self.cfg_db[asic_id], swsscommon.CFG_PORT_TABLE_NAME)
            self.vdm_real_value_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_VDM_REAL_VALUE_TABLE)
            for t in VDM_THRESHOLD_TYPES:
                self.vdm_threshold_tbl[f'vdm_{t}_threshold_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_VDM_{t.upper()}_THRESHOLD')
                self.vdm_flag_tbl[f'vdm_{t}_flag_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_VDM_{t.upper()}_FLAG')
                self.vdm_flag_change_count_tbl[f'vdm_{t}_flag_change_count_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_VDM_{t.upper()}_FLAG_CHANGE_COUNT')
                self.vdm_flag_set_time_tbl[f'vdm_{t}_flag_set_time_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_VDM_{t.upper()}_FLAG_SET_TIME')
                self.vdm_flag_clear_time_tbl[f'vdm_{t}_flag_clear_time_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_VDM_{t.upper()}_FLAG_CLEAR_TIME')

    def get_intf_tbl(self, asic_id):
        return self.int_tbl[asic_id]

    def get_dom_tbl(self, asic_id):
        return self.dom_tbl[asic_id]

    def get_dom_flag_tbl(self, asic_id):
        return self.dom_flag_tbl[asic_id]

    def get_dom_flag_change_count_tbl(self, asic_id):
        return self.dom_flag_change_count_tbl[asic_id]

    def get_dom_flag_set_time_tbl(self, asic_id):
        return self.dom_flag_set_time_tbl[asic_id]

    def get_dom_flag_clear_time_tbl(self, asic_id):
        return self.dom_flag_clear_time_tbl[asic_id]

    def get_dom_threshold_tbl(self, asic_id):
        return self.dom_threshold_tbl[asic_id]

    def get_dom_temperature_tbl(self, asic_id):
        return self.dom_temperature_tbl[asic_id]

    def get_status_tbl(self, asic_id):
        return self.status_tbl[asic_id]

    def get_status_flag_tbl(self, asic_id):
        return self.status_flag_tbl[asic_id]

    def get_status_flag_change_count_tbl(self, asic_id):
        return self.status_flag_change_count_tbl[asic_id]

    def get_status_flag_set_time_tbl(self, asic_id):
        return self.status_flag_set_time_tbl[asic_id]

    def get_status_flag_clear_time_tbl(self, asic_id):
        return self.status_flag_clear_time_tbl[asic_id]

    def get_status_sw_tbl(self, asic_id):
        return self.status_sw_tbl[asic_id]

    def get_vdm_threshold_tbl(self, asic_id, threshold_type):
        return self.vdm_threshold_tbl[f'vdm_{threshold_type}_threshold_tbl'][asic_id]

    def get_vdm_real_value_tbl(self, asic_id):
        return self.vdm_real_value_tbl[asic_id]

    def get_vdm_flag_tbl(self, asic_id, threshold_type):
        return self.vdm_flag_tbl[f'vdm_{threshold_type}_flag_tbl'][asic_id]

    def get_vdm_flag_change_count_tbl(self, asic_id, threshold_type):
        return self.vdm_flag_change_count_tbl[f'vdm_{threshold_type}_flag_change_count_tbl'][asic_id]

    def get_vdm_flag_set_time_tbl(self, asic_id, threshold_type):
        return self.vdm_flag_set_time_tbl[f'vdm_{threshold_type}_flag_set_time_tbl'][asic_id]

    def get_vdm_flag_clear_time_tbl(self, asic_id, threshold_type):
        return self.vdm_flag_clear_time_tbl[f'vdm_{threshold_type}_flag_clear_time_tbl'][asic_id]

    def get_pm_tbl(self, asic_id):
        return self.pm_tbl[asic_id]

    def get_firmware_info_tbl(self, asic_id):
        return self.firmware_info_tbl[asic_id]

    def get_app_port_tbl(self, asic_id):
        return self.app_port_tbl[asic_id]

    def get_appl_db(self, asic_id):
        return self.appl_db[asic_id]

    def get_app_port_read_tbl(self, asic_id):
        return self.app_port_read_tbl[asic_id]

    def get_next_si_notification_number(self, port_name, asic_id):
        """
        Return the next SI notification number for port_name by reading the
        current si_settings_notification from APPL_DB and incrementing its counter.
        Returns 1 if no prior value exists or the format is unrecognised.
        """
        found, fvs = self.app_port_read_tbl[asic_id].get(port_name)
        if not found:
            return 1
        fvs_dict = dict(fvs)
        current_value = fvs_dict.get('si_settings_notification')
        if not current_value:
            return 1
        try:
            parts = current_value.split(':')
            if len(parts) == 2:
                return int(parts[1]) + 1
        except (ValueError, IndexError):
            pass
        return 1

    def get_state_db(self, asic_id):
        return self.state_db[asic_id]

    def get_cfg_port_tbl(self, asic_id):
        return self.cfg_port_tbl[asic_id]

    def get_state_port_tbl(self, asic_id):
        return self.state_port_tbl[asic_id]

    def get_gearbox_line_lanes_dict(self):
        """
        Retrieves the gearbox line lanes dictionary from APPL_DB

        This method scans all ASICs for gearbox interface configurations and extracts
        the line_lanes count for each logical port. The line_lanes represent the
        number of lanes on the line side (towards the optical module) which is the
        correct count to use for CMIS host lane configuration.

        Returns:
            dict: A dictionary where:
                - key (str): logical port name (e.g., "Ethernet0")
                - value (int): number of line-side lanes for that port

        Example:
            {"Ethernet0": 2, "Ethernet200": 4}

        Note:
            - Returns empty dict if no gearbox configuration is found
            - Silently skips invalid or malformed entries
            - Only processes keys that start with "interface:"
        """
        gearbox_line_lanes_dict = {}
        try:
            for asic_id in self.appl_db:
                appl_db = self.appl_db[asic_id]
                gearbox_table = swsscommon.Table(appl_db, "_GEARBOX_TABLE")
                interface_keys = gearbox_table.getKeys()
                for key in interface_keys:
                    if key.startswith("interface:"):
                        (found, fvs) = gearbox_table.get(key)
                        if found:
                            fvs_dict = dict(fvs)
                            interface_name = fvs_dict.get('name', '')
                            line_lanes_str = fvs_dict.get('line_lanes', '')
                            if interface_name and line_lanes_str:
                                line_lanes_count = len(line_lanes_str.split(','))
                                gearbox_line_lanes_dict[interface_name] = line_lanes_count
                            else:
                                if not interface_name:
                                    helper_logger.log_warning("get_gearbox_line_lanes_dict: ASIC {}: Interface {} missing 'name' field".format(asic_id, key))
                                if not line_lanes_str:
                                    helper_logger.log_debug("get_gearbox_line_lanes_dict: ASIC {}: Interface {} has empty 'line_lanes' field".format(asic_id, interface_name))
        except Exception as e:
            helper_logger.log_error("Error in get_gearbox_line_lanes_dict: {}".format(str(e)))
            return gearbox_line_lanes_dict

        return gearbox_line_lanes_dict
