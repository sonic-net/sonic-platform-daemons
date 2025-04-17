class StatusUtils:
    """
    This class provides utility functions for managing transceiver status operations on transceivers
    and call the corresponding methods in the SFP object.
    """
    def __init__(self, sfp_obj_dict, logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.logger = logger

    def get_transceiver_status(self, physical_port):
        """
        Get the transceiver status for the given physical port.
        """
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_status()
        except (NotImplementedError):
            return {}

    def get_transceiver_status_flags(self, physical_port):
        """
        Get the transceiver status flags for the given physical port.
        """
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_status_flags()
        except (NotImplementedError):
            return {}
