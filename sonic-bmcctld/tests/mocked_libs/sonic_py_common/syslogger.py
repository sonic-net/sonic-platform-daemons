import logging


class SysLogger(object):
    """Test stub matching sonic_py_common.syslogger.SysLogger."""

    def __init__(self, *_args, **_kwargs):
        self.logger = logging.getLogger("syslogger_stub")

    def log_debug(self, *_args, **_kwargs):
        pass

    def log_info(self, *_args, **_kwargs):
        pass

    def log_notice(self, *_args, **_kwargs):
        pass

    def log_warning(self, *_args, **_kwargs):
        pass

    def log_error(self, *_args, **_kwargs):
        pass
