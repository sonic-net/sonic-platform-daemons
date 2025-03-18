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
    """
    def __init__(self, sfp_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(logger)
        self.sfp_obj_dict = sfp_obj_dict
        self.port_mapping = port_mapping
        self.task_stopping_event = task_stopping_event
        self.xcvr_table_helper = xcvr_table_helper
        self.xcvrd_utils = XCVRDUtils(sfp_obj_dict, logger)
        self.vdm_utils = VDMUtils(sfp_obj_dict, logger)
        self.logger = logger

    def post_port_vdm_real_values_to_db(self, logical_port_name, table, get_values_func, db_cache=None):
        if self.task_stopping_event.is_set():
            return

        pport_list = self.port_mapping.get_logical_to_physical(logical_port_name)
        if not pport_list:
            self.logger.log_error(f"Post port diagnostic values to db failed for {logical_port_name} "
                                         "as no physical port found")
            return
        physical_port = pport_list[0]

        if physical_port not in self.sfp_obj_dict:
            self.logger.log_error(f"Post port diagnostic values to db failed for {logical_port_name} "
                                         "as no sfp object found")
            return

        if not self.xcvrd_utils.get_transceiver_presence(physical_port):
            return

        try:
            if db_cache is not None and physical_port in db_cache:
                # If cache is enabled and diagnostic values are in cache, just read from cache, no need read from EEPROM
                diagnostic_values_dict = db_cache[physical_port]
            else:
                diagnostic_values_dict = get_values_func(physical_port)
                if db_cache is not None:
                    # If cache is enabled, put diagnostic values to cache
                    db_cache[physical_port] = diagnostic_values_dict
            if diagnostic_values_dict is not None:
                if not diagnostic_values_dict:
                    return
                self.beautify_info_dict(diagnostic_values_dict)
                fvs = swsscommon.FieldValuePairs([(k, v) for k, v in diagnostic_values_dict.items()])
                table.set(logical_port_name, fvs)
            else:
                return

        except NotImplementedError:
            self.logger.log_error(f"Post port diagnostic values to db failed for {logical_port_name} "
                                         "as functionality is not implemented")
            return

    def post_port_vdm_flags_to_db(self, logical_port_name, db_cache=None):
        return self._post_port_vdm_thresholds_or_flags_to_db(logical_port_name, self.xcvr_table_helper.get_vdm_flag_tbl,
                                                            self.vdm_utils.get_vdm_flags, flag_data=True, db_cache=db_cache)

    def post_port_vdm_thresholds_to_db(self, logical_port_name, db_cache=None):
        return self._post_port_vdm_thresholds_or_flags_to_db(logical_port_name, self.xcvr_table_helper.get_vdm_threshold_tbl,
                                                            self.vdm_utils.get_vdm_thresholds, flag_data=False, db_cache=db_cache)

    # Update transceiver VDM threshold or flag info to db
    def _post_port_vdm_thresholds_or_flags_to_db(self, logical_port_name, get_vdm_table_func,
                                                 get_vdm_values_func, flag_data=False, db_cache=None):
        if self.task_stopping_event.is_set():
            return

        pport_list = self.port_mapping.get_logical_to_physical(logical_port_name)
        if not pport_list:
            self.logger.log_error(f"Post port vdm thresholds or flags to db failed for {logical_port_name} "
                                         "as no physical port found with flag_data {flag_data}")
            return
        physical_port = pport_list[0]

        if physical_port not in self.sfp_obj_dict:
            self.logger.log_error(f"Post port vdm thresholds or flags to db failed for {logical_port_name} "
                                         "as no sfp object found with flag_data {flag_data}")
            return

        if not self.xcvrd_utils.get_transceiver_presence(physical_port):
            return

        if self.xcvrd_utils.is_transceiver_flat_memory(physical_port):
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
                vdm_values_dict_update_time = datetime.datetime.now().strftime('%a %b %d %H:%M:%S %Y')
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

                            # If the current update is a flag update, then update the metadata tables
                            # for the flags
                            if flag_data:
                                asic_id = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
                                self.update_flag_metadata_tables(logical_port_name, new_key, value,
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
                    fvs = swsscommon.FieldValuePairs([(k, v) for k, v in threshold_value_dict.items()])
                    table = get_vdm_table_func(self.port_mapping.get_asic_id_for_logical_port(logical_port_name), threshold_type)
                    table.set(logical_port_name, fvs)
                else:
                    return
        except NotImplementedError:
            self.logger.log_error(f"Post port vdm thresholds or flags to db failed for {logical_port_name} "
                                         "as functionality is not implemented with flag_data {flag_data}")
            return
