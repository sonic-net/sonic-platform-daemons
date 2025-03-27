from swsscommon import swsscommon
from xcvrd.xcvrd_utilities.utils import XCVRDUtils

class DBUtils:
    """
    This class contains utility functions to interact with the redis database.
    """
    def __init__(self, sfp_obj_dict, logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.xcvrd_utils = XCVRDUtils(sfp_obj_dict, logger)
        self.logger = logger

    def post_diagnostic_values_to_db(self, logical_port_name, table, get_values_func, db_cache=None):
        """
        Posts the diagnostic values to the database.
        """
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

    """
    Updates the metadata tables for flag table
    As part of the metadata update, the following tables are updated:
    - Change Count Table
    - Last Set Time Table
    - Last Clear Time Table
    """
    def update_flag_metadata_tables(self, logical_port_name, field_name, current_value,
                                    flag_values_dict_update_time,
                                    flag_value_table,
                                    flag_change_count_table, flag_last_set_time_table, flag_last_clear_time_table,
                                    table_name_for_logging):
        if flag_value_table is None:
            self.logger.log_error(f"flag_value_table {table_name_for_logging} is None for port {logical_port_name}")
            return

        found, db_flags_value_dict = flag_value_table.get(logical_port_name)
        # Table is empty, this is the first update to the metadata tables (this also means that the transceiver was detected for the first time)
        # Initialize the change count to 0 and last set and clear times to 'never'
        if not found:
            flag_change_count_table.set(logical_port_name, swsscommon.FieldValuePairs([(field_name, '0')]))
            flag_last_set_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(field_name, 'never')]))
            flag_last_clear_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(field_name, 'never')]))
            return
        else:
            db_flags_value_dict = dict(db_flags_value_dict)

        # No metadata update required if the value is 'N/A'
        if str(current_value).strip() == 'N/A':
            return

        # Update metadata if the value of flag has changed from the previous value
        if field_name in db_flags_value_dict and db_flags_value_dict[field_name] != str(current_value):
            found, db_change_count_dict = flag_change_count_table.get(logical_port_name)
            if not found:
                self.logger.log_error(f"Failed to get the change count for table {table_name_for_logging} port {logical_port_name}")
                return
            db_change_count_dict = dict(db_change_count_dict)
            db_change_count = int(db_change_count_dict[field_name])
            db_change_count += 1
            flag_change_count_table.set(logical_port_name, swsscommon.FieldValuePairs([(field_name, str(db_change_count))]))
            if current_value:
                flag_last_set_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(field_name, flag_values_dict_update_time)]))
            else:
                flag_last_clear_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(field_name, flag_values_dict_update_time)]))

    def beautify_info_dict(self, info_dict):
        for k, v in info_dict.items():
            if not isinstance(v, str):
                info_dict[k] = str(v)
