import datetime
from xcvrd.xcvrd_utilities.utils import XCVRDUtils
from xcvrd.xcvrd_utilities.xcvr_table_helper import VDM_THRESHOLD_TYPES
from xcvrd.dom.utilities.db.utils import DBUtils
from xcvrd.dom.utilities.vdm.utils import VDMUtils
from swsscommon import swsscommon

class VDMDBUtils(DBUtils):
    """
    This class provides utility functions for managing
    DB operations related to VDM on transceivers.
    Handles data related to the following tables:
        - TRANSCEIVER_VDM_REAL_VALUE
        - TRANSCEIVER_VDM_XXXX_FLAG and its corresponding metadata tables (change count, set time, clear time)
            - XXXX refers to HALARM, LALARM, HWARN or LWARN
        - TRANSCEIVER_VDM_XXXX_THRESHOLD
            - XXXX refers to HALARM, LALARM, HWARN or LWARN
    """
    def __init__(self, sfp_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(sfp_obj_dict, port_mapping, task_stopping_event, logger)
        self.xcvr_table_helper = xcvr_table_helper
        self.vdm_utils = VDMUtils(self.sfp_obj_dict, logger)
        self.logger = logger

    def post_port_vdm_real_values_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port vdm real values to db failed for {logical_port_name} "
                                    "as no asic index found")
            return

        return self.post_diagnostic_values_to_db(logical_port_name,
                                                 self.xcvr_table_helper.get_vdm_real_value_tbl(asic_index),
                                                 self.vdm_utils.get_vdm_real_values,
                                                 db_cache=db_cache,
                                                 enable_flat_memory_check=True)

    def post_port_vdm_flags_to_db(self, logical_port_name, db_cache=None):
        return self._post_port_vdm_thresholds_or_flags_to_db(logical_port_name, self.xcvr_table_helper.get_vdm_flag_tbl,
                                                            self.vdm_utils.get_vdm_flags, flag_data=True, db_cache=db_cache)

    def post_port_vdm_thresholds_to_db(self, logical_port_name, db_cache=None):
        return self._post_port_vdm_thresholds_or_flags_to_db(logical_port_name, self.xcvr_table_helper.get_vdm_threshold_tbl,
                                                            self.vdm_utils.get_vdm_thresholds, flag_data=False, db_cache=db_cache)

    # Update transceiver VDM threshold or flag info to db
    def _post_port_vdm_thresholds_or_flags_to_db(self, logical_port_name, get_vdm_table_func,
                                                 get_vdm_values_func, flag_data=False, db_cache=None):
        physical_port = self._validate_and_get_physical_port(logical_port_name, enable_flat_memory_check=True)
        if physical_port is None:
            return

        try:
            if db_cache is not None and physical_port in db_cache:
                vdm_threshold_type_value_dict = db_cache[physical_port]
            else:
                # Reading from the EEPROM as the cache is empty
                # The vdm_values_dict contains the threshold type in the key for all the VDM observable types
                vdm_values_dict = get_vdm_values_func(physical_port)
                if vdm_values_dict is None:
                    self.logger.log_error(f"Post port vdm thresholds or flags to db failed for {logical_port_name} "
                                                 "as no vdm values found with flag_data {flag_data}")
                    return
                vdm_values_dict_update_time = self.get_current_time()
                # Creating a dict with the threshold type as the key
                # This is done so that a separate redis-db table is created for each threshold type
                vdm_threshold_type_value_dict = {threshold_type: {} for threshold_type in VDM_THRESHOLD_TYPES}
                for key, value in vdm_values_dict.items():
                    for threshold_type in VDM_THRESHOLD_TYPES:
                        if f'_{threshold_type}' in key:
                            # The vdm_values_dict contains the threshold type in the key. Hence, remove the
                            # threshold type from the key since the tables are already separated by threshold type
                            new_key = key.replace(f'_{threshold_type}', '')
                            vdm_threshold_type_value_dict[threshold_type][new_key] = value

                for threshold_type, threshold_value_dict in vdm_threshold_type_value_dict.items():
                    # If the current update is a flag update, then update the metadata tables
                    # for the flags
                    if flag_data and threshold_value_dict:
                            asic_id = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
                            self._update_flag_metadata_tables(logical_port_name, threshold_value_dict,
                                                             vdm_values_dict_update_time,
                                                             self.xcvr_table_helper.get_vdm_flag_tbl(asic_id, threshold_type),
                                                             self.xcvr_table_helper.get_vdm_flag_change_count_tbl(asic_id, threshold_type),
                                                             self.xcvr_table_helper.get_vdm_flag_set_time_tbl(asic_id, threshold_type),
                                                             self.xcvr_table_helper.get_vdm_flag_clear_time_tbl(asic_id, threshold_type),
                                                             f"VDM {threshold_type}")

                if db_cache is not None:
                    # If cache is enabled, put vdm values to cache
                    # VDM metadata tables are stored only in one of the logical ports for a port breakout group. This
                    # is done since the tables are planned to be created only for one of the logical ports for a port breakout group in future.
                    db_cache[physical_port] = vdm_threshold_type_value_dict

            for threshold_type, threshold_value_dict in vdm_threshold_type_value_dict.items():
                if threshold_value_dict:
                    self.beautify_info_dict(threshold_value_dict)
                    fvs = swsscommon.FieldValuePairs(
                        [(k, v) for k, v in threshold_value_dict.items()] +
                        [("last_update_time", self.get_current_time())]
                    )
                    
                    table = get_vdm_table_func(self.port_mapping.get_asic_id_for_logical_port(logical_port_name), threshold_type)
                    table.set(logical_port_name, fvs)
                else:
                    return
        except NotImplementedError:
            self.logger.log_error(f"Post port vdm thresholds or flags to db failed for {logical_port_name} "
                                         "as functionality is not implemented with flag_data {flag_data}")
            return
