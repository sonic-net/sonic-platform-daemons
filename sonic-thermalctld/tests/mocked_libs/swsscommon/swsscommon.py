'''
    Mock implementation of swsscommon package for unit testing
'''

from swsssdk import ConfigDBConnector, SonicDBConfig, SonicV2Connector

STATE_DB = ''

class RedisPipeline:
    def __init__(self, db, batch_size=128):
        self.db = db
        self.batch_size = batch_size
        self.queue = []

    def flush(self):
        # Mock flush operation - just clear the queue
        self.queue.clear()
        pass

class Table:
    def __init__(self, db_or_redispipeline, table_name, buffered=False):
        self.table_name = table_name
        self.mock_dict = {}

    def _del(self, key):
        del self.mock_dict[key]
        pass

    def set(self, key, fvs):
        self.mock_dict[key] = fvs.fv_dict
        pass

    def get(self, key):
        if key in self.mock_dict:
            return self.mock_dict[key]
        return None

    def get_size(self):
        return (len(self.mock_dict))


class FieldValuePairs:

    def __init__(self, tuple_list=None):
        if tuple_list is None:
            self.fv_dict = {}
        elif isinstance(tuple_list, list) and isinstance(tuple_list[0], tuple):
            self.fv_dict = dict(tuple_list)
        else:
            self.fv_dict = dict()

    def append(self, kv_tuple):
        self.fv_dict[kv_tuple[0]] = kv_tuple[1]

    def __setitem__(self, key, kv_tuple):
        self.fv_dict[kv_tuple[0]] = kv_tuple[1]

    def __len__(self):
        return len(self.fv_dict)

    def __getitem__(self, key):
        return self.fv_dict[key]

    def __eq__(self, other):
        if not isinstance(other, FieldValuePairs):
            # don't attempt to compare against unrelated types
            return NotImplemented

        return self.fv_dict == other.fv_dict

    def __repr__(self):
        return repr(self.fv_dict)

    def __str__(self):
        return repr(self.fv_dict)
