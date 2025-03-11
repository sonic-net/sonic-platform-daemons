import os
import sys
import mock
import pytest
import signal
import threading
from imp import load_source
import re

from mock import MagicMock
from sonic_py_common import daemon_base

from .mock_platform import MockDpuChassis
from chassisd import *


SYSLOG_IDENTIFIER = 'dpu_chassisd_test'
daemon_base.db_connect = MagicMock()
test_path = os.path.dirname(os.path.abspath(__file__))
os.environ["CHASSISD_UNIT_TESTING"] = "1"


@pytest.mark.parametrize('conf_db, app_db, expected_state', [
    ({'Ethernet0': {}}, {'Ethernet0': [True, 'up']}, 'up'),
    ({'Ethernet0': {}}, {'Ethernet0': [True, 'down']}, 'down'),
    ({'Ethernet0': {}}, {'Ethernet0': [False, None]}, 'down'),
    ({'Ethernet0': {}, 'Ethernet4': {}}, {'Ethernet0': [True, 'up'], 'Ethernet4': [True, 'up']}, 'up'),
    ({'Ethernet0': {}, 'Ethernet4': {}}, {'Ethernet0': [True, 'up'], 'Ethernet4': [True, 'down']}, 'down'),
    ({'Ethernet0': {}, 'Ethernet4': {}}, {'Ethernet0': [True, 'up'], 'Ethernet4': [False, None]}, 'down'),
])
def test_dpu_dataplane_state_update_common(conf_db, app_db, expected_state):
    chassis = MockDpuChassis()

    with mock.patch.object(swsscommon.ConfigDBConnector, 'get_table', side_effect=lambda *args: conf_db):
        with mock.patch.object(swsscommon.Table, 'hget', side_effect=lambda intf, _: app_db[intf]):
            dpu_updater = DpuStateUpdater(SYSLOG_IDENTIFIER, chassis)

            state = dpu_updater.get_dp_state()

            assert state == expected_state


@pytest.mark.parametrize('db, expected_state', [
    ([True, 'UP'], 'up'),
    ([True, 'DOWN'], 'down'),
    ([False, None], 'down'),
])
def test_dpu_controlplane_state_update_common(db, expected_state):
    chassis = MockDpuChassis()

    with mock.patch.object(swsscommon.Table, 'hget', side_effect=lambda *args: db):
        dpu_updater = DpuStateUpdater(SYSLOG_IDENTIFIER, chassis)

        state = dpu_updater.get_cp_state()

        assert state == expected_state


@pytest.mark.parametrize('state, expected_state', [
    (True, 'up'),
    (False, 'down'),
])
def test_dpu_state_update_api(state, expected_state):
    chassis = MockDpuChassis()
    chassis.get_controlplane_state = MagicMock(return_value=state)
    chassis.get_dataplane_state = MagicMock(return_value=state)

    dpu_updater = DpuStateUpdater(SYSLOG_IDENTIFIER, chassis)

    state = dpu_updater.get_cp_state()
    assert state == expected_state

    state = dpu_updater.get_dp_state()
    assert state == expected_state


@pytest.mark.parametrize('dpu_id, dp_state, cp_state, expected_state', [
    (0, False, False, {'DPU0': 
        {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
         'dpu_control_plane_state': 'down', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}),
    (0, False, True, {'DPU0': 
        {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
         'dpu_control_plane_state': 'up', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}),
    (0, True, True, {'DPU0': 
        {'dpu_data_plane_state': 'up', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
         'dpu_control_plane_state': 'up', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}),
])
def test_dpu_state_update(dpu_id, dp_state, cp_state, expected_state):
    chassis = MockDpuChassis()

    chassis.get_dpu_id = MagicMock(return_value=dpu_id)
    chassis.get_dataplane_state = MagicMock(return_value=dp_state)
    chassis.get_controlplane_state = MagicMock(return_value=cp_state)

    chassis_state_db = {}

    def hset(key, field, value):
        print(key, field, value)
        if key not in chassis_state_db:
            chassis_state_db[key] = {}

        chassis_state_db[key][field] = value

    with mock.patch.object(swsscommon.Table, 'hset', side_effect=hset) as hset_mock:
            dpu_updater = DpuStateUpdater(SYSLOG_IDENTIFIER, chassis)
            dpu_updater._time_now = MagicMock(return_value='Sat Jan 01 12:00:00 AM UTC 2000')

            dpu_updater.update_state()

            assert chassis_state_db == expected_state

            dpu_updater.deinit()

            # After the deinit we assume that the DPU state is down.
            assert chassis_state_db == {'DPU0': 
                {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
                 'dpu_control_plane_state': 'down', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}


@pytest.mark.parametrize('dpu_id, dp_state, cp_state, expected_state', [
    (0, False, False, {'DPU0':
        {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
         'dpu_control_plane_state': 'down', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}),
    (0, False, True, {'DPU0':
        {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
         'dpu_control_plane_state': 'up', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}),
    (0, True, True, {'DPU0':
        {'dpu_data_plane_state': 'up', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
         'dpu_control_plane_state': 'up', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}),
])
def test_dpu_state_manager(dpu_id, dp_state, cp_state, expected_state):
    chassis = MockDpuChassis()

    chassis.get_dpu_id = MagicMock(return_value=dpu_id)
    chassis.get_dataplane_state = MagicMock(return_value=dp_state)
    chassis.get_controlplane_state = MagicMock(return_value=cp_state)

    chassis_state_db = {}

    def hset(key, field, value):
        print(key, field, value)
        if key not in chassis_state_db:
            chassis_state_db[key] = {}

        chassis_state_db[key][field] = value

    with mock.patch.object(swsscommon.Table, 'hset', side_effect=hset):
        with mock.patch.object(swsscommon.Select, 'select', side_effect=((swsscommon.Select.OBJECT, None), (swsscommon.Select.OBJECT, None), KeyboardInterrupt)):
            dpu_updater = DpuStateUpdater(SYSLOG_IDENTIFIER, chassis)
            dpu_updater._time_now = MagicMock(return_value='Sat Jan 01 12:00:00 AM UTC 2000')

            dpu_state_mng = DpuStateManagerTask(SYSLOG_IDENTIFIER, dpu_updater)

            dpu_state_mng.task_worker()

            assert chassis_state_db == expected_state

            dpu_updater.deinit()

            # After the deinit we assume that the DPU state is down.
            assert chassis_state_db == {'DPU0':
                {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
                 'dpu_control_plane_state': 'down', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}


def test_dpu_chassis_daemon():
    # Test the chassisd run
    chassis = MockDpuChassis()

    chassis.get_dpu_id = MagicMock(return_value=1)
    chassis.get_dataplane_state = MagicMock(return_value=True)
    chassis.get_controlplane_state = MagicMock(return_value=True)

    chassis_state_db = {}

    def hset(key, field, value):
        print(key, field, value)
        if key not in chassis_state_db:
            chassis_state_db[key] = {}

        chassis_state_db[key][field] = value

    with mock.patch.object(swsscommon.Table, 'hset', side_effect=hset) as hset_mock:
            with mock.patch.object(DpuStateUpdater, '_time_now', side_effect=lambda: 'Sat Jan 01 12:00:00 AM UTC 2000') as mock_time_now:

                daemon_chassisd = DpuChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
                daemon_chassisd.CHASSIS_INFO_UPDATE_PERIOD_SECS = MagicMock(return_value=1)

                daemon_chassisd.stop = MagicMock()
                daemon_chassisd.stop.wait.return_value = False

                thread = threading.Thread(target=daemon_chassisd.run)

                thread.start()
                # Wait for thread to start and update DB
                time.sleep(3)

                assert chassis_state_db == {'DPU1':
                    {'dpu_data_plane_state': 'up', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
                    'dpu_control_plane_state': 'up', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}

                daemon_chassisd.signal_handler(signal.SIGINT, None)
                daemon_chassisd.stop.wait.return_value = True

                thread.join()

                assert chassis_state_db == {'DPU1':
                    {'dpu_data_plane_state': 'down', 'dpu_data_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000',
                    'dpu_control_plane_state': 'down', 'dpu_control_plane_time': 'Sat Jan 01 12:00:00 AM UTC 2000'}}
    with mock.patch.object(swsscommon.Table, 'hset', side_effect=hset):
        daemon_chassisd = DpuChassisdDaemon(SYSLOG_IDENTIFIER, chassis)
        daemon_chassisd.CHASSIS_INFO_UPDATE_PERIOD_SECS = MagicMock(return_value=1)

        daemon_chassisd.stop = MagicMock()
        daemon_chassisd.stop.wait.return_value = False

        thread = threading.Thread(target=daemon_chassisd.run)
        thread.start()
        # Wait for thread to start and update DB
        time.sleep(3)
        date_format = "%a %b %d %I:%M:%S %p UTC %Y"

        def is_valid_date(date_str):
            try:
                datetime.strptime(date_str, date_format)
            except ValueError:
                # Parsing failed and we are unable to obtain the time
                return False
            return True
        assert is_valid_date(chassis_state_db['DPU1']['dpu_data_plane_time'])
        assert is_valid_date(chassis_state_db['DPU1']['dpu_control_plane_time'])
        daemon_chassisd.signal_handler(signal.SIGINT, None)
        daemon_chassisd.stop.wait.return_value = True

        thread.join()
