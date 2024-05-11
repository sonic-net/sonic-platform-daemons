#
# ssd.py
#
# Generic implementation of the SSD health API
# SSD models supported:
#  - InnoDisk
#  - StorFly
#  - Virtium

try:
    import re
    import subprocess
    from .storage_base import StorageBase
except ImportError as e:
    raise ImportError (str(e) + "- required module not found")

SMARTCTL = "smartctl {} -a"
INNODISK = "iSmart -d {}"
VIRTIUM  = "SmartCmd -m {}"

NOT_AVAILABLE = "N/A"

# Set Vendor Specific IDs
INNODISK_HEALTH_ID = 169
INNODISK_TEMPERATURE_ID = 194
SWISSBIT_HEALTH_ID = 248
SWISSBIT_TEMPERATURE_ID = 194

class SsdUtil(StorageBase):
    """
    Generic implementation of the SSD health API
    """

    def __init__(self, diskdev):
        model = 'InnoDisk Corp. - mSATA 3IE3'
        serial = 'BCA11712190600251'
        firmware = 'S16425cG'
        temperature = 32.3
        health = 91.6
        ssd_info = NOT_AVAILABLE
        vendor_ssd_info = NOT_AVAILABLE
        io_reads = 20000
        io_writes = 20005
        reserved_blocks = 3746218

    def get_health(self):
        """
        Retrieves current disk health in percentages

        Returns:
            A float number of current ssd health
            e.g. 83.5
        """
        return self.health

    def get_temperature(self):
        """
        Retrieves current disk temperature in Celsius

        Returns:
            A float number of current temperature in Celsius
            e.g. 40.1
        """
        return self.temperature

    def get_model(self):
        """
        Retrieves model for the given disk device

        Returns:
            A string holding disk model as provided by the manufacturer
        """
        return self.model

    def get_firmware(self):
        """
        Retrieves firmware version for the given disk device

        Returns:
            A string holding disk firmware version as provided by the manufacturer
        """
        return self.firmware

    def get_serial(self):
        """
        Retrieves serial number for the given disk device

        Returns:
            A string holding disk serial number as provided by the manufacturer
        """
        return self.serial

    def get_vendor_output(self):
        """
        Retrieves vendor specific data for the given disk device

        Returns:
            A string holding some vendor specific disk information
        """
        return self.vendor_ssd_info

    def get_io_writes(self):
        """
        Retrieves the total number of Input/Output (I/O) writes done on an SSD

        Returns:
            An integer value of the total number of I/O writes
        """
        return self.io_writes

    def get_io_reads(self):
        """
        Retrieves the total number of Input/Output (I/O) writes done on an SSD

        Returns:
            An integer value of the total number of I/O writes
        """
        return self.io_reads

    def get_reserves_blocks(self):
        """
        Retrieves the total number of reserved blocks in an SSD

        Returns:
            An integer value of the total number of reserved blocks
        """
        return self.reserved_blocks
