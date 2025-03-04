class XCVRDUtils:
    """
    This class provides utility functions for managing VDM operations on transceivers.
    """
    def __init__(self, sfp_obj_dict, helper_logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.helper_logger = helper_logger

    def get_transceiver_presence(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_presence()
        except (KeyError, NotImplementedError):
            self.helper_logger.log_error(f"Failed to get presence for port {physical_port}")
            return False
