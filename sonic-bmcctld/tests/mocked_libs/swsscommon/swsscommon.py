"""
    Mock implementation of swsscommon.swsscommon for unit testing.
    Provides stubs needed by sonic_py_common.device_info and by bmcctld.
"""


# Stubs consumed by sonic_py_common.device_info on import
class ConfigDBConnector:
    def connect(self, *args, **kwargs):
        pass

    def get_table(self, *args, **kwargs):
        return {}


class SonicV2Connector:
    def __init__(self, *args, **kwargs):
        pass

    def connect(self, *args, **kwargs):
        pass

    def keys(self, *args, **kwargs):
        return []

    def get(self, *args, **kwargs):
        return None

    def get_all(self, *args, **kwargs):
        return {}


class SonicDBConfig:
    @staticmethod
    def load(*args, **kwargs):
        pass

    @staticmethod
    def isInit():
        return False


STATE_DB = ''
CONFIG_DB = ''


class Table:
    def __init__(self, *argv):
        self.db_or_pipe = argv[0] if argv else None
        self.table_name = argv[1] if len(argv) > 1 else ''
        self.mock_dict = {}

    def _del(self, key):
        if key in self.mock_dict:
            del self.mock_dict[key]

    def set(self, key, fvs):
        # Merge into existing entry (matches real Redis HSET field-level update semantics)
        if key not in self.mock_dict:
            self.mock_dict[key] = {}
        if isinstance(fvs, list):
            self.mock_dict[key].update(dict(fvs))
        elif hasattr(fvs, 'fv_dict'):
            self.mock_dict[key].update(fvs.fv_dict)
        else:
            raise ValueError("Unsupported fvs format: {}".format(type(fvs)))

    def get(self, key):
        if key in self.mock_dict:
            return [True, list(self.mock_dict[key].items())]
        return [False, []]

    def hget(self, key, field):
        if key not in self.mock_dict or field not in self.mock_dict[key]:
            return [False, None]
        return [True, self.mock_dict[key][field]]

    def hset(self, key, field, value):
        if key not in self.mock_dict:
            self.mock_dict[key] = {}
        self.mock_dict[key][field] = value

    def hdel(self, key, field):
        if key in self.mock_dict and field in self.mock_dict[key]:
            del self.mock_dict[key][field]

    def getKeys(self):
        return list(self.mock_dict)

    def size(self):
        return len(self.mock_dict)


class FieldValuePairs:
    def __init__(self, fvs):
        self.fv_dict = dict(fvs)

    def __iter__(self):
        return iter(self.fv_dict.items())

    def __repr__(self):
        return repr(self.fv_dict)


class Select:
    OBJECT = 0
    TIMEOUT = 1

    def addSelectable(self, selectable):
        pass

    def removeSelectable(self, selectable):
        pass

    def select(self, timeout=-1, interrupt_on_signal=False):
        return self.TIMEOUT, None


class SubscriberStateTable(Table):

    def getFd(self):
        return id(self)

    def pop(self):
        return '', '', []

    def pops(self):
        return None

    def getDbConnector(self):
        return _MockDbConnector()


class _MockDbConnector:
    def getDbName(self):
        return 'STATE_DB'


class RedisPipeline:
    def __init__(self, db):
        self.db = db

    def loadRedisScript(self, script):
        return 'mocksha'
