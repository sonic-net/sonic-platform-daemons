try:
    from sonic_py_common import multi_asic
    from swsscommon import swsscommon
    from ..xcvrd_utilities.xcvr_table_helper import XcvrTableHelper, FlagTables, VDM_THRESHOLD_TYPES
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

TRANSCEIVER_ELS_INFO_TABLE = 'TRANSCEIVER_ELS_INFO'
TRANSCEIVER_ELS_FIRMWARE_INFO_TABLE = 'TRANSCEIVER_ELS_FIRMWARE_INFO'
TRANSCEIVER_ELS_DOM_SENSOR_TABLE = 'TRANSCEIVER_ELS_DOM_SENSOR'
TRANSCEIVER_ELS_DOM_TEMPERATURE_TABLE = 'TRANSCEIVER_ELS_DOM_TEMPERATURE'
TRANSCEIVER_ELS_DOM_THRESHOLD_TABLE = 'TRANSCEIVER_ELS_DOM_THRESHOLD'
TRANSCEIVER_ELS_DOM_FLAG_TABLE = 'TRANSCEIVER_ELS_DOM_FLAG'
TRANSCEIVER_ELS_DOM_FLAG_CHANGE_COUNT_TABLE = 'TRANSCEIVER_ELS_DOM_FLAG_CHANGE_COUNT'
TRANSCEIVER_ELS_DOM_FLAG_SET_TIME_TABLE = 'TRANSCEIVER_ELS_DOM_FLAG_SET_TIME'
TRANSCEIVER_ELS_DOM_FLAG_CLEAR_TIME_TABLE = 'TRANSCEIVER_ELS_DOM_FLAG_CLEAR_TIME'
TRANSCEIVER_ELS_STATUS_TABLE = 'TRANSCEIVER_ELS_STATUS'
TRANSCEIVER_ELS_STATUS_FLAG_TABLE = 'TRANSCEIVER_ELS_STATUS_FLAG'
TRANSCEIVER_ELS_STATUS_FLAG_CHANGE_COUNT_TABLE = 'TRANSCEIVER_ELS_STATUS_FLAG_CHANGE_COUNT'
TRANSCEIVER_ELS_STATUS_FLAG_SET_TIME_TABLE = 'TRANSCEIVER_ELS_STATUS_FLAG_SET_TIME'
TRANSCEIVER_ELS_STATUS_FLAG_CLEAR_TIME_TABLE = 'TRANSCEIVER_ELS_STATUS_FLAG_CLEAR_TIME'


class CpoXcvrTableHelper(XcvrTableHelper):
    """XcvrTableHelper extended with the ELS (external laser source) tables used on CPO platforms."""

    def __init__(self, namespaces):
        super().__init__(namespaces)
        self.els_info_tbl = {}
        self.els_firmware_info_tbl = {}
        self.els_dom_tbl = {}
        self.els_dom_temperature_tbl = {}
        self.els_dom_threshold_tbl = {}
        self.els_dom_flag_tbl = {}
        self.els_dom_flag_change_count_tbl = {}
        self.els_dom_flag_set_time_tbl = {}
        self.els_dom_flag_clear_time_tbl = {}
        self.els_status_tbl = {}
        self.els_status_flag_tbl = {}
        self.els_status_flag_change_count_tbl = {}
        self.els_status_flag_set_time_tbl = {}
        self.els_status_flag_clear_time_tbl = {}
        self.els_vdm_threshold_tbl = {f'els_vdm_{t}_threshold_tbl': {} for t in VDM_THRESHOLD_TYPES}
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            self.els_info_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_INFO_TABLE)
            self.els_firmware_info_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_FIRMWARE_INFO_TABLE)
            self.els_dom_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_SENSOR_TABLE)
            self.els_dom_temperature_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_TEMPERATURE_TABLE)
            self.els_dom_threshold_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_THRESHOLD_TABLE)
            self.els_dom_flag_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_FLAG_TABLE)
            self.els_dom_flag_change_count_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_FLAG_CHANGE_COUNT_TABLE)
            self.els_dom_flag_set_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_FLAG_SET_TIME_TABLE)
            self.els_dom_flag_clear_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_FLAG_CLEAR_TIME_TABLE)
            self.els_status_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_STATUS_TABLE)
            self.els_status_flag_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_STATUS_FLAG_TABLE)
            self.els_status_flag_change_count_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_STATUS_FLAG_CHANGE_COUNT_TABLE)
            self.els_status_flag_set_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_STATUS_FLAG_SET_TIME_TABLE)
            self.els_status_flag_clear_time_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_STATUS_FLAG_CLEAR_TIME_TABLE)
            for t in VDM_THRESHOLD_TYPES:
                self.els_vdm_threshold_tbl[f'els_vdm_{t}_threshold_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_ELS_VDM_{t.upper()}_THRESHOLD')

    def get_els_info_tbl(self, asic_id):
        return self.els_info_tbl[asic_id]

    def get_els_firmware_info_tbl(self, asic_id):
        return self.els_firmware_info_tbl[asic_id]

    def get_els_dom_tbl(self, asic_id):
        return self.els_dom_tbl[asic_id]

    def get_els_dom_temperature_tbl(self, asic_id):
        return self.els_dom_temperature_tbl[asic_id]

    def get_els_dom_threshold_tbl(self, asic_id):
        return self.els_dom_threshold_tbl[asic_id]

    def get_els_dom_flag_tbl(self, asic_id):
        return self.els_dom_flag_tbl[asic_id]

    def get_els_dom_flag_change_count_tbl(self, asic_id):
        return self.els_dom_flag_change_count_tbl[asic_id]

    def get_els_dom_flag_set_time_tbl(self, asic_id):
        return self.els_dom_flag_set_time_tbl[asic_id]

    def get_els_dom_flag_clear_time_tbl(self, asic_id):
        return self.els_dom_flag_clear_time_tbl[asic_id]

    def get_els_status_tbl(self, asic_id):
        return self.els_status_tbl[asic_id]

    def get_els_status_flag_tbl(self, asic_id):
        return self.els_status_flag_tbl[asic_id]

    def get_els_status_flag_change_count_tbl(self, asic_id):
        return self.els_status_flag_change_count_tbl[asic_id]

    def get_els_status_flag_set_time_tbl(self, asic_id):
        return self.els_status_flag_set_time_tbl[asic_id]

    def get_els_status_flag_clear_time_tbl(self, asic_id):
        return self.els_status_flag_clear_time_tbl[asic_id]

    def get_els_dom_flag_tables(self, asic_id):
        """Returns the ELS DOM flag table and its metadata tables as a FlagTables group."""
        return FlagTables(self.get_els_dom_flag_tbl(asic_id),
                          self.get_els_dom_flag_change_count_tbl(asic_id),
                          self.get_els_dom_flag_set_time_tbl(asic_id),
                          self.get_els_dom_flag_clear_time_tbl(asic_id))

    def get_els_status_flag_tables(self, asic_id):
        """Returns the ELS status flag table and its metadata tables as a FlagTables group."""
        return FlagTables(self.get_els_status_flag_tbl(asic_id),
                          self.get_els_status_flag_change_count_tbl(asic_id),
                          self.get_els_status_flag_set_time_tbl(asic_id),
                          self.get_els_status_flag_clear_time_tbl(asic_id))

    def get_els_vdm_threshold_tbl(self, asic_id, threshold_type):
        return self.els_vdm_threshold_tbl[f'els_vdm_{threshold_type}_threshold_tbl'][asic_id]

    def get_dom_tables(self, asic_id, include_thresholds):
        tables = super().get_dom_tables(asic_id, include_thresholds)
        tables.extend([
            self.get_els_firmware_info_tbl(asic_id),
            self.get_els_dom_tbl(asic_id),
            self.get_els_dom_temperature_tbl(asic_id),
            self.get_els_dom_flag_tbl(asic_id),
            self.get_els_dom_flag_change_count_tbl(asic_id),
            self.get_els_dom_flag_set_time_tbl(asic_id),
            self.get_els_dom_flag_clear_time_tbl(asic_id),
        ])
        if include_thresholds:
            tables.append(self.get_els_dom_threshold_tbl(asic_id))
        return tables

    def get_vdm_tables(self, asic_id, include_thresholds):
        tables = super().get_vdm_tables(asic_id, include_thresholds)
        if include_thresholds:
            tables.extend(self.get_els_vdm_threshold_tbl(asic_id, key) for key in VDM_THRESHOLD_TYPES)
        return tables

    def get_status_tables(self, asic_id, include_sw):
        return super().get_status_tables(asic_id, include_sw) + [
            self.get_els_status_tbl(asic_id),
            self.get_els_status_flag_tbl(asic_id),
            self.get_els_status_flag_change_count_tbl(asic_id),
            self.get_els_status_flag_set_time_tbl(asic_id),
            self.get_els_status_flag_clear_time_tbl(asic_id),
        ]

    def get_info_tables(self, asic_id):
        return super().get_info_tables(asic_id) + [self.get_els_info_tbl(asic_id)]
