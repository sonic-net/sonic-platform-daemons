"""
    Mock implementation of sonic_platform package for unit testing
"""

from sonic_platform_base.ssd_base import SsdBase


class Ssd(SsdBase):
    def __init__(self):
        self.platform_ssdutil = "/tmp/Ssd"
    
    def __str__(self):
        return self.platform_ssdutil
