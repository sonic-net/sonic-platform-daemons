from swsscommon import swsscommon

class DBUtils:
    """
    This class contains utility functions to interact with the redis database.
    """
    def __init__(self, logger):
        self.logger = logger

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
