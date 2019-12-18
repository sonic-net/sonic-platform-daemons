import os
import sys
from mock import Mock, MagicMock, patch
from sonic_daemon_base import daemon_base
from .mock_platform import MockChassis, MockFan

daemon_base.db_connect = MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

from imp import load_source

load_source('thermalctld', scripts_path + '/thermalctld')
from thermalctld import *


def setup_function():
    logger.log_notice = MagicMock()
    logger.log_warning = MagicMock()


def teardown_function():
    logger.log_notice.reset()
    logger.log_warning.reset()


def test_fanstatus_set_presence():
    fan_status = FanStatus()
    ret = fan_status.set_presence(True)
    assert fan_status.presence
    assert not ret

    ret = fan_status.set_presence(False)
    assert not fan_status.presence
    assert ret


def test_fanstatus_set_under_speed():
    fan_status = FanStatus()
    ret = fan_status.set_under_speed(NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE)
    assert not ret

    ret = fan_status.set_under_speed(NOT_AVAILABLE, NOT_AVAILABLE, 0)
    assert not ret

    ret = fan_status.set_under_speed(NOT_AVAILABLE, 0, 0)
    assert not ret

    ret = fan_status.set_under_speed(0, 0, 0)
    assert not ret

    ret = fan_status.set_under_speed(100, 120, 19)
    assert ret
    assert fan_status.under_speed
    assert not fan_status.is_ok()

    ret = fan_status.set_under_speed(100, 120, 21)
    assert ret
    assert not fan_status.under_speed
    assert fan_status.is_ok()


def test_fanstatus_set_over_speed():
    fan_status = FanStatus()
    ret = fan_status.set_over_speed(NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE)
    assert not ret

    ret = fan_status.set_over_speed(NOT_AVAILABLE, NOT_AVAILABLE, 0)
    assert not ret

    ret = fan_status.set_over_speed(NOT_AVAILABLE, 0, 0)
    assert not ret

    ret = fan_status.set_over_speed(0, 0, 0)
    assert not ret

    ret = fan_status.set_over_speed(120, 100, 19)
    assert ret
    assert fan_status.over_speed
    assert not fan_status.is_ok()

    ret = fan_status.set_over_speed(120, 100, 21)
    assert ret
    assert not fan_status.over_speed
    assert fan_status.is_ok()


def test_fanupdater_fan_absence():
    chassis = MockChassis()
    chassis.make_absence_fan()
    fan_updater = FanUpdater(chassis)
    fan_updater.update()
    fan_list = chassis.get_all_fans()
    assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
    logger.log_warning.assert_called_once()

    fan_list[0].presence = True
    fan_updater.update()
    assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
    logger.log_notice.assert_called_once()


def test_fanupdater_fan_under_speed():
    chassis = MockChassis()
    chassis.make_under_speed_fan()
    fan_updater = FanUpdater(chassis)
    fan_updater.update()
    fan_list = chassis.get_all_fans()
    assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
    logger.log_warning.assert_called_once()

    fan_list[0].make_normal_speed()
    fan_updater.update()
    assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
    logger.log_notice.assert_called_once()


def test_fanupdater_fan_over_speed():
    chassis = MockChassis()
    chassis.make_over_speed_fan()
    fan_updater = FanUpdater(chassis)
    fan_updater.update()
    fan_list = chassis.get_all_fans()
    assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_RED
    logger.log_warning.assert_called_once()

    fan_list[0].make_normal_speed()
    fan_updater.update()
    assert fan_list[0].get_status_led() == MockFan.STATUS_LED_COLOR_GREEN
    logger.log_notice.assert_called_once()


def test_temperstatus_set_over_temper():
    temper_status = TemperStatus()
    ret = temper_status.set_over_temper(NOT_AVAILABLE, NOT_AVAILABLE)
    assert not ret

    ret = temper_status.set_over_temper(NOT_AVAILABLE, 0)
    assert not ret

    ret = temper_status.set_over_temper(0, NOT_AVAILABLE)
    assert not ret

    ret = temper_status.set_over_temper(2, 1)
    assert ret
    assert temper_status.over_temper

    ret = temper_status.set_over_temper(1, 2)
    assert ret
    assert not temper_status.over_temper


def test_temperstatus_set_under_temper():
    temper_status = TemperStatus()
    ret = temper_status.set_under_temper(NOT_AVAILABLE, NOT_AVAILABLE)
    assert not ret

    ret = temper_status.set_under_temper(NOT_AVAILABLE, 0)
    assert not ret

    ret = temper_status.set_under_temper(0, NOT_AVAILABLE)
    assert not ret

    ret = temper_status.set_under_temper(1, 2)
    assert ret
    assert temper_status.under_temper

    ret = temper_status.set_under_temper(2, 1)
    assert ret
    assert not temper_status.under_temper


def test_temperupdater_over_temper():
    chassis = MockChassis()
    chassis.make_over_temper_thermal()
    temper_updater = TemperUpdater(chassis)
    temper_updater.update()
    thermal_list = chassis.get_all_thermals()
    logger.log_warning.assert_called_once()

    thermal_list[0].make_normal_temper()
    temper_updater.update()
    logger.log_notice.assert_called_once()


def test_temperupdater_under_temper():
    chassis = MockChassis()
    chassis.make_under_temper_thermal()
    temper_updater = TemperUpdater(chassis)
    temper_updater.update()
    thermal_list = chassis.get_all_thermals()
    logger.log_warning.assert_called_once()

    thermal_list[0].make_normal_temper()
    temper_updater.update()
    logger.log_notice.assert_called_once()

