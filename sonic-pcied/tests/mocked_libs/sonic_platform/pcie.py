"""
    Mock implementation of sonic_platform package for unit testing
"""

from sonic_platform_base.sonic_pcie.pcie_base import PcieBase


class Pcie(PcieBase):
    def __init__(self):
        self.platform_pcieutil = "/tmp/Pcie"
    
    def __str__(self):
        return self.platform_pcieutil
