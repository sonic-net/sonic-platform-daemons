"""Driver for the SIGTERM test: run the real xcvrd daemon in this process against the emulated namespaces, logging to stdout.

usage: python3 xcvrd_it_driver.py <database_global.json>
"""

import logging
import os
import sys

logging.raiseExceptions = False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from swsscommon import swsscommon

swsscommon.SonicDBConfig.load_sonic_global_db_config(sys.argv[1])

from tests.integration import harness as h
from xcvrd.xcvrd import DaemonXcvrd, SYSLOG_IDENTIFIER
from xcvrd.xcvrd_utilities import media_settings_parser


class StdoutLogger:
    def __getattr__(self, name):
        if name.startswith('log_'):
            def f(msg, *args, **kwargs):
                print('[{}] {}'.format(name[4:].upper(), msg), flush=True)
            return f
        raise AttributeError(name)


def main():
    chassis = h.FakeChassis(event_period=1.0)
    h.install_fake_platform(chassis)
    for p in h.multi_asic_patches():
        p.start()
    media_settings_parser.load_media_settings = lambda: media_settings_parser.g_dict.update({'GLOBAL_MEDIA_SETTINGS': {'1-64': {}}})
    media_settings_parser.get_media_settings_key = lambda *a, **k: {'vendor_key': 'X', 'media_key': 'Y', 'lane_speed_key': 'speed:400GAUI-8', 'medium_lane_speed_key': 'UNKNOWN'}
    media_settings_parser.get_media_settings_value = lambda *a, **k: {'preemphasis': {'lane0': '0x1'}}
    media_settings_parser.get_custom_media_settings_value = lambda *a, **k: {}
    import xcvrd.xcvrd_utilities.optics_si_parser as osp
    osp.load_optics_si_settings = lambda: None
    import xcvrd.xcvrd as xm
    xm.helper_logger = StdoutLogger()
    daemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
    logger = StdoutLogger()
    for level in ('log_notice', 'log_warning', 'log_error', 'log_info', 'log_debug'):
        setattr(daemon, level, getattr(logger, level))
    import signal
    signal.signal(signal.SIGTERM, daemon.signal_handler)
    signal.signal(signal.SIGINT, daemon.signal_handler)
    daemon.run()


if __name__ == '__main__':
    main()
