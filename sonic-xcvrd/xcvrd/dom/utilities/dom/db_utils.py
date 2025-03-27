from xcvrd.dom.utilities.dom.utils import DOMUtils
from xcvrd.dom.utilities.db.utils import DBUtils


class DOMDBUtils(DBUtils):
    """
    This class provides utility functions for managing DB operations
    related to DOM on transceivers.
    """
    def __init__(self, sfp_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        super().__init__(sfp_obj_dict, logger)
        self.port_mapping = port_mapping
        self.task_stopping_event = task_stopping_event
        self.xcvr_table_helper = xcvr_table_helper
        self.dom_utils = DOMUtils(self.sfp_obj_dict, logger)
        self.logger = logger

    def post_port_dom_info_to_db(self, logical_port_name, dom_info_cache=None):
        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.logger.log_error("Post port dom info to db failed for {logical_port_name} "
                                  "as no asic index found")
            return

        return self.post_diagnostic_values_to_db(logical_port_name,
                                                 self.xcvr_table_helper.get_dom_tbl(asic_index),
                                                 self.dom_utils.get_transceiver_dom_info, dom_info_cache)
