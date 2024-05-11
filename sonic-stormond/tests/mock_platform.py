
"""
    Mock implementation of sonic_platform package for unit testing
"""

# TODO: Clean this up once we no longer need to support Python 2
import sys
if sys.version_info.major == 3:
    from unittest import mock
else:
    import mock

class MockStorageDevice():
    def __init__(self):
        super(MockStorageDevice, self).__init__()