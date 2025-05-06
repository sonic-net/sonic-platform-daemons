import re
from xcvrd.dom.utilities.db.utils import DBUtils
from xcvrd.dom.utilities.dom_sensor.utils import DOMUtils
from swsscommon import swsscommon


class DOMDBUtils(DBUtils):
    """
    This class provides utility functions for managing DB operations
    related to DOM on transceivers.
    Handles data related to the following tables:
        - TRANSCEIVER_DOM_SENSOR
        - TRANSCEIVER_DOM_FLAG and its corresponding metadata tables (change count, set time, clear time)
        - TRANSCEIVER_DOM_THRESHOLD
    """
    TEMP_UNIT = 'C'
    VOLT_UNIT = 'Volts'
    POWER_UNIT = 'dBm'
    BIAS_UNIT = 'mA'

    def __init__(self, sfp_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(sfp_obj_dict, port_mapping, task_stopping_event, logger)
        self.xcvr_table_helper = xcvr_table_helper
        self.dom_utils = DOMUtils(self.sfp_obj_dict, logger)
        self.logger = logger

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

        physical_port = self._validate_and_get_physical_port(logical_port_name)
        if physical_port is None:
            return

        try:
            if db_cache is not None and physical_port in db_cache:
                # If cache is enabled and dom flag values are in cache, just read from cache, no need read from EEPROM
                dom_flags_dict = db_cache[physical_port]
            else:
                # Reading from the EEPROM as the cache is empty
                dom_flags_dict = self.dom_utils.get_transceiver_dom_flags(physical_port)
                if dom_flags_dict is None:
                    self.logger.log_error(f"Post port dom flags to db failed for {logical_port_name} "
                                          "as no dom flags found")
                    return
                if dom_flags_dict:
                    dom_flags_dict_update_time = self.get_current_time()
                    self._update_flag_metadata_tables(logical_port_name, dom_flags_dict,
                                                     dom_flags_dict_update_time,
                                                     self.xcvr_table_helper.get_dom_flag_tbl(asic_index),
                                                     self.xcvr_table_helper.get_dom_flag_change_count_tbl(asic_index),
                                                     self.xcvr_table_helper.get_dom_flag_set_time_tbl(asic_index),
                                                     self.xcvr_table_helper.get_dom_flag_clear_time_tbl(asic_index),
                                                     "DOM flags")

                if db_cache is not None:
                    # If cache is enabled, put dom flag values to cache
                    db_cache[physical_port] = dom_flags_dict

            if dom_flags_dict is not None:
                if not dom_flags_dict:
                    return

                self._beautify_dom_info_dict(dom_flags_dict)
                fvs = swsscommon.FieldValuePairs(
                    [(k, v) for k, v in dom_flags_dict.items()] +
                    [("last_update_time", self.get_current_time())]
                )
                self.xcvr_table_helper.get_dom_flag_tbl(asic_index).set(logical_port_name, fvs)
            else:
                return

        except NotImplementedError:
            self.logger.log_error(f"Post port dom flags to db failed for {logical_port_name} "
                                  "as no dom flags found")
            return

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

    def _strip_unit(self, value, unit):
        # Strip unit from raw data
        if isinstance(value, str) and value.endswith(unit):
            return value[:-len(unit)]
        return str(value)

    # Remove unnecessary unit from the raw data
    def _beautify_dom_info_dict(self, dom_info_dict):
        if dom_info_dict is None:
            self.logger.log_warning("DOM info dict is None while beautifying")
            return

        for k, v in dom_info_dict.items():
            if k == 'temperature':
                dom_info_dict[k] = self._strip_unit(v, self.TEMP_UNIT)
            elif k == 'voltage':
                dom_info_dict[k] = self._strip_unit(v, self.VOLT_UNIT)
            elif re.match('^(tx|rx)[1-8]power$', k):
                dom_info_dict[k] = self._strip_unit(v, self.POWER_UNIT)
            elif re.match('^(tx|rx)[1-8]bias$', k):
                dom_info_dict[k] = self._strip_unit(v, self.BIAS_UNIT)
            elif type(v) is not str:
                # For all the other keys:
                dom_info_dict[k] = str(v)
