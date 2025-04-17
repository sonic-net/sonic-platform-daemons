from xcvrd.dom.utilities.db.utils import DBUtils
from xcvrd.dom.utilities.status.utils import StatusUtils
from swsscommon import swsscommon


class StatusDBUtils(DBUtils):
    """
    This class provides utility functions for managing DB operations
    related to transceiver status (specifically, all the hardware related fields of transceiver status).
    Handles data related to the following tables:
        - TRANSCEIVER_STATUS
        - TRANSCEIVER_STATUS_FLAG and its corresponding metadata tables (change count, set time, clear time)
    """

    def __init__(self, sfp_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(sfp_obj_dict, port_mapping, task_stopping_event, logger)
        self.xcvr_table_helper = xcvr_table_helper
        self.status_utils = StatusUtils(self.sfp_obj_dict, logger)
        self.logger = logger

    def post_port_transceiver_hw_status_to_db(self, logical_port_name, db_cache=None):
        """
        Posts the hardware status of a transceiver to the database.

        Args:
            logical_port_name (str): Logical port name.
            db_cache (dict, optional): Cache for storing transceiver hardware status.

        """
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port transceiver hw status to db failed for {logical_port_name} "
                                    "as no asic index found")
            return

        return self.post_diagnostic_values_to_db(logical_port_name,
                                                 self.xcvr_table_helper.get_status_tbl(asic_index),
                                                 self.status_utils.get_transceiver_status,
                                                 db_cache=db_cache)

    def post_port_transceiver_hw_status_flags_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port transceiver hw status flags to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        physical_port = self._validate_and_get_physical_port(logical_port_name)
        if physical_port is None:
            return

        try:
            if db_cache is not None and physical_port in db_cache:
                # If cache is enabled and status flag values are in cache, just read from cache, no need read from EEPROM
                status_flags_dict = db_cache[physical_port]
            else:
                # Reading from the EEPROM as the cache is empty
                status_flags_dict = self.status_utils.get_transceiver_status_flags(physical_port)
                if status_flags_dict is None:
                    self.logger.log_error(f"Post port transceiver hw status flags to db failed for {logical_port_name} "
                                            "as no status flags found")
                    return
                if status_flags_dict:
                    self._update_flag_metadata_tables(logical_port_name, status_flags_dict,
                                                     self.get_current_time(),
                                                     self.xcvr_table_helper.get_status_flag_tbl(asic_index),
                                                     self.xcvr_table_helper.get_status_flag_change_count_tbl(asic_index),
                                                     self.xcvr_table_helper.get_status_flag_set_time_tbl(asic_index),
                                                     self.xcvr_table_helper.get_status_flag_clear_time_tbl(asic_index),
                                                     "Status flags")

                if db_cache is not None:
                    # If cache is enabled, put status flag values to cache
                    db_cache[physical_port] = status_flags_dict
            if status_flags_dict is not None:
                if not status_flags_dict:
                    return

                self.beautify_info_dict(status_flags_dict)
                fvs = swsscommon.FieldValuePairs(
                    [(k, v) for k, v in status_flags_dict.items()] +
                    [("last_update_time", self.get_current_time())]
                )
                self.xcvr_table_helper.get_status_flag_tbl(asic_index).set(logical_port_name, fvs)
            else:
                return

        except NotImplementedError:
            self.logger.log_notice(f"Post port transceiver hw status flags to db failed for {logical_port_name} "
                                   "as functionality is not implemented")
            return
