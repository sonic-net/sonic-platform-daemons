class XCVRDUtils:
    """
    This class provides utility functions for managing XCVRD operations on transceivers
    and call the corresponding methods in the SFP object.
    """
    def __init__(self, sfp_obj_dict, logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.logger = logger

    def get_transceiver_presence(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_presence()
        except (KeyError, NotImplementedError):
            self.logger.log_error(f"Failed to get presence for port {physical_port}")
            return False
