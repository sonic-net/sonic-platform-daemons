class DOMUtils:
    """
    This class provides utility functions for managing DOM operations on transceivers
    and call the corresponding methods in the SFP object.
    """
    def __init__(self, sfp_obj_dict, logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.logger = logger

    def get_transceiver_dom_sensor_real_value(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_dom_real_value()
        except (NotImplementedError):
            return {}

    def get_transceiver_dom_flags(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_dom_flags()
        except (NotImplementedError):
            return {}

    def get_transceiver_dom_thresholds(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_threshold_info()
        except (NotImplementedError):
            return {}
