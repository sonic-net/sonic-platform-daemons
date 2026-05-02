from sonic_py_common.logger import Logger


def db_connect(db_name):
    return db_name


class DaemonBase(Logger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def set_min_log_priority_info(self):
        pass

    def set_min_log_priority_debug(self):
        pass

    def run(self):
        pass

    def deinit(self):
        pass
