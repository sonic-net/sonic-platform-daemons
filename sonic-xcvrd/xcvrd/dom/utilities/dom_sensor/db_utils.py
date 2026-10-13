from xcvrd.dom.utilities.db.utils import DBUtils
from xcvrd.dom.utilities.dom_sensor.beautify import DOMBeautifyMixin
from xcvrd.dom.utilities.dom_sensor.utils import DOMUtils
from swsscommon import swsscommon


class DOMDBUtils(DOMBeautifyMixin, DBUtils):
    """
    This class provides utility functions for managing DB operations
    related to DOM on transceivers.
    Handles data related to the following tables:
        - TRANSCEIVER_DOM_SENSOR
        - TRANSCEIVER_DOM_FLAG and its corresponding metadata tables (change count, set time, clear time)
        - TRANSCEIVER_DOM_THRESHOLD
    """
    def __init__(self, port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(port_obj_dict, port_mapping, task_stopping_event, logger)
        self.xcvr_table_helper = xcvr_table_helper
        self.dom_utils = DOMUtils(self.port_obj_dict, logger)
        self.logger = logger

    def post_port_dom_temperature_info_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port dom sensor info to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        return self.post_diagnostic_values_to_db(logical_port_name,
                                                 self.xcvr_table_helper.get_dom_temperature_tbl(asic_index),
                                                 self.dom_utils.get_transceiver_dom_temperature,
                                                 db_cache=db_cache,
                                                 beautify_func=self._beautify_dom_info_dict)

    def post_port_dom_sensor_info_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port dom sensor info to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        return self.post_diagnostic_values_to_db(logical_port_name,
                                                 self.xcvr_table_helper.get_dom_tbl(asic_index),
                                                 self.dom_utils.get_transceiver_dom_sensor_real_value,
                                                 db_cache=db_cache,
                                                 beautify_func=self._beautify_dom_info_dict)

    def post_port_dom_flags_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port dom flags to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        return self.post_flag_values_to_db(logical_port_name,
                                           self.dom_utils.get_transceiver_dom_flags,
                                           self.xcvr_table_helper.get_dom_flag_tbl(asic_index),
                                           self.xcvr_table_helper.get_dom_flag_change_count_tbl(asic_index),
                                           self.xcvr_table_helper.get_dom_flag_set_time_tbl(asic_index),
                                           self.xcvr_table_helper.get_dom_flag_clear_time_tbl(asic_index),
                                           "DOM flags",
                                           db_cache=db_cache,
                                           beautify_func=self._beautify_dom_info_dict)

    def post_port_dom_thresholds_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"Post port dom thresholds to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        return self.post_diagnostic_values_to_db(logical_port_name,
                                                 self.xcvr_table_helper.get_dom_threshold_tbl(asic_index),
                                                 self.dom_utils.get_transceiver_dom_thresholds,
                                                 db_cache=db_cache,
                                                 beautify_func=self._beautify_dom_info_dict)
