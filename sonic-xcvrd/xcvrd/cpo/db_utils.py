#!/usr/bin/env python3

try:
    from ..dom.utilities.db.utils import DBUtils
    from ..dom.utilities.dom_sensor.beautify import DOMBeautifyMixin
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CPODOMDBUtils(DOMBeautifyMixin, DBUtils):
    """
    This class provides utility functions for managing DB operations
    related to DOM on CPO (co-packaged optics) modules.
    """

    def __init__(self, port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(port_obj_dict, port_mapping, task_stopping_event, logger)
        self.xcvr_table_helper = xcvr_table_helper
        self.logger = logger

    def post_port_dom_thresholds_to_db(self, logical_port_name, db_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error(f"CPO: Post port dom thresholds to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        physical_port = self._validate_and_get_physical_port(logical_port_name)
        if physical_port is None:
            return

        # Read OE thresholds and publish to DB
        oe_dom_thresholds = self.port_obj_dict[physical_port].oe.get_api().get_transceiver_threshold_info()
        self.post_diagnostic_values_from_dict_to_db(logical_port_name,
                                                    self.xcvr_table_helper.get_dom_threshold_tbl(asic_index),
                                                    oe_dom_thresholds,
                                                    beautify_func=self._beautify_dom_info_dict)
        # Read ELSFP thresholds and publish to DB
        elsfp_dom_thresholds = self.port_obj_dict[physical_port].elsfp.get_api().get_elsfp_threshold_info()
        self.post_diagnostic_values_from_dict_to_db(logical_port_name,
                                                    self.xcvr_table_helper.get_els_dom_threshold_tbl(asic_index),
                                                    elsfp_dom_thresholds,
                                                    beautify_func=self._beautify_dom_info_dict)


class CPOVDMDBUtils(DBUtils):
    """
    This class provides utility functions for managing DB operations
    related to VDM on CPO (co-packaged optics) modules.
    """

    def __init__(self, port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(port_obj_dict, port_mapping, task_stopping_event, logger)
        self.xcvr_table_helper = xcvr_table_helper
        self.logger = logger

    def post_port_vdm_thresholds_to_db(self, logical_port_name, db_cache=None):
        pass
