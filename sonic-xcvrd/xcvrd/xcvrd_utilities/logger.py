import os
from sonic_py_common.logger import Logger

# Global logger instance for xcvrd, the argument "enable_set_log_level_on_fly"
# will start a thread to detect CONFIG DB LOGGER table change. The logger instance 
# allow user to set log level via swssloglevel command at real time. This instance 
# should be shared by all modules of xcvrd to avoid starting too many logger thread.
if os.environ.get("XCVRD_UNIT_TESTING") != "1":
    logger = Logger(log_identifier='xcvrd', enable_set_log_level_on_fly=True)
else:
    # for unit test, there is no redis, don't set enable_set_log_level_on_fly=True
    logger = Logger(log_identifier='xcvrd')
