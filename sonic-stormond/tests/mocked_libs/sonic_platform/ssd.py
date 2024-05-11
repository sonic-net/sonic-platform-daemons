"""
    Mock implementation of sonic_platform package for unit testing
"""

from sonic_platform_base.storage_base import StorageBase


class Storage(StorageBase):
    def __init__(self):
        self.platform_Storageutil = "/tmp/Storage"
    
    def __str__(self):
        return self.platform_Storageutil
