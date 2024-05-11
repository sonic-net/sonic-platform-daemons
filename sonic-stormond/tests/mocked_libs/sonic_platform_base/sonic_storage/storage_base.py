#
# storage_base.py
#
# Abstract base class for implementing platform-specific
#  Storage information gathering functionality for SONiC
#

try:
    import abc
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

#
# storage_base.py
#
# Base class for implementing common Storage Device health features
#


class StorageBase(object):
    """
    Base class for interfacing with a SSD
    """
    def __init__(self, diskdev):
        """
        Constructor

        Args:
            diskdev: Linux device name to get parameters for
        """
        pass

    @abc.abstractmethod
    def get_health(self):
        """
        Retrieves current disk health in percentages

        Returns:
            A float number of current ssd health
            e.g. 83.5
        """
        return 91.6

    @abc.abstractmethod
    def get_temperature(self):
        """
        Retrieves current disk temperature in Celsius

        Returns:
            A float number of current temperature in Celsius
            e.g. 40.1
        """
        return 32.3

    @abc.abstractmethod
    def get_model(self):
        """
        Retrieves model for the given disk device

        Returns:
            A string holding disk model as provided by the manufacturer
        """
        return ''

    @abc.abstractmethod
    def get_firmware(self):
        """
        Retrieves firmware version for the given disk device

        Returns:
            A string holding disk firmware version as provided by the manufacturer
        """
        return ''

    @abc.abstractmethod
    def get_serial(self):
        """
        Retrieves serial number for the given disk device

        Returns:
            A string holding disk serial number as provided by the manufacturer
        """
        return ''

    @abc.abstractmethod
    def get_vendor_output(self):
        """
        Retrieves vendor specific data for the given disk device

        Returns:
            A string holding some vendor specific disk information
        """
        return ''

    def get_io_reads(self):
        """
        Retrieves the total number of Input/Output (I/O) reads done on an SSD

        Returns:
            An integer value of the total number of I/O reads
        """
        return 20000

    @abc.abstractmethod
    def get_io_writes(self):
        """
        Retrieves the total number of Input/Output (I/O) writes done on an SSD

        Returns:
            An integer value of the total number of I/O writes
        """
        return 20005

    @abc.abstractmethod
    def get_reserves_blocks(self):
        """
        Retrieves the total number of reserved blocks in an SSD

        Returns:
            An integer value of the total number of reserved blocks
        """
        return 3746218