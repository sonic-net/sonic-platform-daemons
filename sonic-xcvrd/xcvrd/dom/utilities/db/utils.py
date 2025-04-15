from datetime import datetime
from swsscommon import swsscommon
from xcvrd.xcvrd_utilities.utils import XCVRDUtils

class DBUtils:
    """
    This class contains utility functions to interact with the redis database.
    """
    NEVER = "never"
    NOT_AVAILABLE = "N/A"

    def __init__(self, sfp_obj_dict, port_mapping, task_stopping_event, logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.port_mapping = port_mapping
        self.task_stopping_event = task_stopping_event
        self.xcvrd_utils = XCVRDUtils(sfp_obj_dict, logger)
        self.logger = logger

    def post_diagnostic_values_to_db(self, logical_port_name, table, get_values_func,
                                     db_cache=None, beautify_func=None, enable_flat_memory_check=False):
        """
        Posts the diagnostic values to the database.

        Args:
            logical_port_name (str): Logical port name.
            table (object): Database table object.
            get_values_func (function): Function to get diagnostic values.
            db_cache (dict, optional): Cache for diagnostic values.
            beautify_func (function, optional): Function to beautify the diagnostic values. Defaults to self.beautify_info_dict.
            enable_flat_memory_check (bool, optional): Flag to check for flat memory support. Defaults to False.
        """
        physical_port = self._validate_and_get_physical_port(logical_port_name, enable_flat_memory_check)
        if physical_port is None:
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

                # Use the provided beautify function or default to self.beautify_info_dict
                (beautify_func or self.beautify_info_dict)(diagnostic_values_dict)
                fvs = swsscommon.FieldValuePairs(
                    [(k, v) for k, v in diagnostic_values_dict.items()] +
                    [("last_update_time", self.get_current_time())]
                )
                table.set(logical_port_name, fvs)

        except NotImplementedError:
            self.logger.log_error(f"Post port diagnostic values to db failed for {logical_port_name} "
                                         "as functionality is not implemented")
            return

    def _validate_and_get_physical_port(self, logical_port_name, enable_flat_memory_check=False):
        """
        Validates the logical port and retrieves the corresponding physical port.

        Validation Steps:
        1. Ensures `task_stopping_event` is not set.
        2. Checks if the logical port maps to a physical port.
        3. Checks if the physical port has an associated SFP object.
        4. Checks if the transceiver is present.
        5. (Optional) Ensures the transceiver is not flat memory if `enable_flat_memory_check` is True.

        If any of these checks fail, an error message is logged and `None` is returned.
        If all checks pass, the physical port number is returned.

        Args:
            logical_port_name (str): Logical port name.
            enable_flat_memory_check (bool): Flag to check for flat memory support.

        Returns:
            int: The physical port number if validation succeeds, or None if validation fails.
        """
        if self.task_stopping_event.is_set():
            return None

        pport_list = self.port_mapping.get_logical_to_physical(logical_port_name)
        if not pport_list:
            self.logger.log_error(f"Validate and get physical port failed for {logical_port_name} "
                                   "as no physical port found")
            return None

        physical_port = pport_list[0]

        if physical_port not in self.sfp_obj_dict:
            self.logger.log_error(f"Validate and get physical port failed for {logical_port_name} "
                                   "as no sfp object found")
            return None

        if not self.xcvrd_utils.get_transceiver_presence(physical_port):
            return None

        if enable_flat_memory_check and self.xcvrd_utils.is_transceiver_flat_memory(physical_port):
            return None

        return physical_port

    def _update_flag_metadata_tables(self, logical_port_name, curr_flag_dict,
                                    flag_values_dict_update_time,
                                    flag_value_table,
                                    flag_change_count_table, flag_last_set_time_table, flag_last_clear_time_table,
                                    table_name_for_logging):
        """
        Updates the metadata tables for a flag table.

        This method compares the current flag values with the values stored in the database.
        If there are changes, it updates the metadata tables accordingly, including:
        - Change count
        - Last set time
        - Last clear time

        Args:
            logical_port_name (str): Logical port name.
            curr_flag_dict (dict): Current flag values.
            flag_values_dict_update_time (str): Timestamp of the update.
            flag_value_table (swsscommon.Table): Table containing flag values.
            flag_change_count_table (swsscommon.Table): Table for change counts.
            flag_last_set_time_table (swsscommon.Table): Table for last set times.
            flag_last_clear_time_table (swsscommon.Table): Table for last clear times.
            table_name_for_logging (str): Name of the table for logging purposes.
        """
        if flag_value_table is None:
            self.logger.log_error(f"flag_value_table {table_name_for_logging} is None for port {logical_port_name}")
            return

        # Retrieve existing flag values from the database
        found, db_flags_value_dict = flag_value_table.get(logical_port_name)
        if not found:
            # Initialize metadata tables for the first update
            self._initialize_metadata_tables(logical_port_name, curr_flag_dict,
                                            flag_change_count_table, flag_last_set_time_table, flag_last_clear_time_table)
            return

        db_flags_value_dict = dict(db_flags_value_dict)

        # Update metadata for each flag
        for flag_key, curr_flag_value in curr_flag_dict.items():
            if str(curr_flag_value).strip() == self.NOT_AVAILABLE:
                continue  # Skip "N/A" values

            if flag_key in db_flags_value_dict and db_flags_value_dict[flag_key] != str(curr_flag_value):
                self._update_flag_metadata(logical_port_name, flag_key, curr_flag_value,
                                           flag_values_dict_update_time, flag_change_count_table,
                                           flag_last_set_time_table, flag_last_clear_time_table,
                                           table_name_for_logging)

    def beautify_info_dict(self, info_dict):
        for k, v in info_dict.items():
            if not isinstance(v, str):
                info_dict[k] = str(v)

    def get_current_time(self, time_format="%a %b %d %H:%M:%S %Y"):
        """
        Returns the current time in the specified format (UTC time).

        Args:
            time_format (str): The format in which to return the time. Defaults to "Day Mon DD HH:MM:SS YYYY".

        Returns:
            str: The current time in UTC.
        """
        return datetime.utcnow().strftime(time_format)

    def _initialize_metadata_tables(self, logical_port_name, curr_flag_dict,
                                    flag_change_count_table, flag_last_set_time_table,
                                    flag_last_clear_time_table):
        """
        Initializes metadata tables for the first update.

        Args:
            logical_port_name (str): Logical port name.
            curr_flag_dict (dict): Current flag values.
            flag_change_count_table (swsscommon.Table): Table for change counts.
            flag_last_set_time_table (swsscommon.Table): Table for last set times.
            flag_last_clear_time_table (swsscommon.Table): Table for last clear times.
        """
        for key in curr_flag_dict.keys():
            flag_change_count_table.set(logical_port_name, swsscommon.FieldValuePairs([(key, '0')]))
            flag_last_set_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(key, self.NEVER)]))
            flag_last_clear_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(key, self.NEVER)]))

    def _update_flag_metadata(self, logical_port_name, flag_key, curr_flag_value,
                              flag_values_dict_update_time, flag_change_count_table,
                              flag_last_set_time_table, flag_last_clear_time_table,
                              table_name_for_logging):
        """
        Updates metadata for a single flag.

        Args:
            logical_port_name (str): Logical port name.
            flag_key (str): The flag key.
            curr_flag_value (str): The current flag value.
            flag_values_dict_update_time (str): Timestamp of the update.
            flag_change_count_table (swsscommon.Table): Table for change counts.
            flag_last_set_time_table (swsscommon.Table): Table for last set times.
            flag_last_clear_time_table (swsscommon.Table): Table for last clear times.
            table_name_for_logging (str): Name of the table for logging purposes.
        """
        # Retrieve the current change count
        found, db_change_count_dict = flag_change_count_table.get(logical_port_name)
        if not found:
            self.logger.log_warning(f"Failed to get the change count for table {table_name_for_logging} port {logical_port_name}")
            return

        db_change_count_dict = dict(db_change_count_dict)
        db_change_count = int(db_change_count_dict.get(flag_key, 0)) + 1

        # Update the change count
        flag_change_count_table.set(logical_port_name, swsscommon.FieldValuePairs([(flag_key, str(db_change_count))]))

        # Update the last set or clear time
        if curr_flag_value:
            flag_last_set_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(flag_key, flag_values_dict_update_time)]))
        else:
            flag_last_clear_time_table.set(logical_port_name, swsscommon.FieldValuePairs([(flag_key, flag_values_dict_update_time)]))
