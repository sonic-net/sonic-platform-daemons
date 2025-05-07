import os
import sys
from imp import load_source

import pytest
from unittest import mock
from sonic_py_common import daemon_base
from swsscommon import swsscommon

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

load_source('ledd', scripts_path + '/ledd')

import ledd

daemon_base.db_connect = mock.MagicMock()
swsscommon.Table = mock.MagicMock()
swsscommon.ProducerStateTable = mock.MagicMock()
swsscommon.SubscriberStateTable = mock.MagicMock()
swsscommon.SonicDBConfig = mock.MagicMock()

def test_help_args(capsys):
    for flag in ['-h', '--help']:
        with mock.patch.object(sys, 'argv', ['ledd', flag]):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                ledd.main()
            assert pytest_wrapped_e.type == SystemExit
            assert pytest_wrapped_e.value.code == 0
            out, err = capsys.readouterr()
            assert out.rstrip() == ledd.USAGE_HELP.rstrip()


def test_version_args(capsys):
    for flag in ['-v', '--version']:
        with mock.patch.object(sys, 'argv', ['ledd', flag]):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                ledd.main()
            assert pytest_wrapped_e.type == SystemExit
            assert pytest_wrapped_e.value.code == 0
            out, err = capsys.readouterr()
            assert out.rstrip() == 'ledd version {}'.format(ledd.VERSION)


def test_bad_args(capsys):
    for flag in ['-n', '--nonexistent']:
        with mock.patch.object(sys, 'argv', ['ledd', flag]):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                ledd.main()
            assert pytest_wrapped_e.type == SystemExit
            assert pytest_wrapped_e.value.code == 1
            out, err = capsys.readouterr()
            assert out.rstrip().endswith(ledd.USAGE_HELP.rstrip())
# Test Port class
def test_port_initialization():
    port = ledd.Port("Ethernet0", 1, ledd.Port.PORT_DOWN, 0, "front-panel")
    assert port._name == "Ethernet0"
    assert port._index == 1
    assert port._state == ledd.Port.PORT_DOWN
    assert port._subport == 0
    assert port._role == "front-panel"
    assert port.isFrontPanelPort() is True


# Test FrontPanelPorts class
def test_front_panel_ports_initialization():
    fp_list = [set() for _ in range(ledd.MAX_FRONT_PANEL_PORTS)]
    up_subports = [0] * ledd.MAX_FRONT_PANEL_PORTS
    logical_pmap = {}
    led_control = mock.Mock()

    fp_ports = ledd.FrontPanelPorts(fp_list, up_subports, logical_pmap, led_control)
    assert fp_ports.fp_port_up_subports == up_subports
    assert fp_ports.fp_port_list == fp_list
    assert fp_ports.logical_port_mapping == logical_pmap
    assert fp_ports.led_control == led_control

def test_front_panel_ports_update_port_led():
    led_control = mock.Mock()
    fp_ports = ledd.FrontPanelPorts([], [], {}, led_control)

    fp_ports.updatePortLed("Ethernet0", ledd.Port.PORT_UP)
    led_control.port_link_state_change.assert_called_once_with("Ethernet0", ledd.Port.PORT_UP)


def test_front_panel_ports_update_port_state():
    port = ledd.Port("Ethernet0", 1, ledd.Port.PORT_DOWN, 0, "front-panel")
    fp_list = [set() for _ in range(ledd.MAX_FRONT_PANEL_PORTS)]
    up_subports = [0] * ledd.MAX_FRONT_PANEL_PORTS
    logical_pmap = {"Ethernet0": port}
    led_control = mock.Mock()

    fp_ports = ledd.FrontPanelPorts(fp_list, up_subports, logical_pmap, led_control)
    assert fp_ports.updatePortState("Ethernet0", ledd.Port.PORT_UP) is True
    assert port._state == ledd.Port.PORT_UP


# Test PortStateObserver class
@mock.patch("ledd.swsscommon.Select")
def test_port_state_observer_initialization(mock_select):
    observer = ledd.PortStateObserver()
    assert observer.sel == mock_select.return_value
    assert observer.tables == {}


@mock.patch("ledd.swsscommon.Table")
@mock.patch("ledd.daemon_base.db_connect")
def test_port_state_observer_get_database_table(mock_db_connect, mock_table):
    observer = ledd.PortStateObserver()
    table = observer.getDatabaseTable("STATE_DB", "PORT_TABLE", "namespace")
    mock_db_connect.assert_called_once_with("STATE_DB", namespace="namespace")
    mock_table.assert_called_once_with(mock_db_connect.return_value, "PORT_TABLE")
    assert table == mock_table.return_value

# Test DaemonLedd class
@mock.patch("ledd.DaemonLedd.load_platform_util")
@mock.patch("ledd.multi_asic.get_front_end_namespaces")
@mock.patch("ledd.PortStateObserver")
@mock.patch("ledd.FrontPanelPorts")
def test_daemon_ledd_initialization(mock_fp_ports, mock_port_observer, mock_get_namespaces, mock_load_platform_util):
    mock_get_namespaces.return_value = ["namespace1", "namespace2"]
    daemon_ledd = ledd.DaemonLedd()

    mock_load_platform_util.assert_called_once_with("led_control", "LedControl")
    mock_port_observer.return_value.subscribePortTable.assert_called_once_with(["namespace1", "namespace2"])
    mock_fp_ports.return_value.initPortLeds.assert_called_once()

@mock.patch('swsscommon.swsscommon.Select.addSelectable', mock.MagicMock())
@mock.patch("ledd.DaemonLedd.load_platform_util")
@mock.patch("ledd.PortStateObserver.getSelectEvent")
@mock.patch("ledd.DaemonLedd.findFrontPanelPorts")
@mock.patch("ledd.FrontPanelPorts")
def test_daemon_ledd_run_timeout(mock_fp_ports, mock_find_front_panel_ports, mock_get_select_event, mock_load_platform_util):
    """
    Test that DaemonLedd.run() handles a timeout from the select method correctly.
    """
    # Mock getSelectEvent to return a timeout
    mock_get_select_event.return_value = (swsscommon.Select.TIMEOUT, None)

    # Mock load_platform_util to prevent actual loading of the LedControl module
    mock_load_platform_util.return_value = mock.Mock()

    # Mock findFrontPanelPorts to return dummy data
    mock_find_front_panel_ports.return_value = ([], [], {})

    # Mock FrontPanelPorts to avoid side effects
    mock_fp_ports.return_value.initPortLeds.return_value = None

    # Create an instance of DaemonLedd
    daemon_ledd = ledd.DaemonLedd()

    # Call the run method
    ret = daemon_ledd.run()

    # Assert that the return value is 0 (indicating successful handling of timeout)
    assert ret == 0

    # Verify that initPortLeds was called during initialization
    mock_fp_ports.return_value.initPortLeds.assert_called_once()

@mock.patch('swsscommon.swsscommon.Select.addSelectable', mock.MagicMock())
@mock.patch("ledd.DaemonLedd.load_platform_util")
@mock.patch("ledd.PortStateObserver.getDatabaseTable")
def test_find_front_panel_ports(mock_get_database_table, mock_load_platform_util):
    """
    Test DaemonLedd.findFrontPanelPorts to ensure it correctly processes namespaces and returns
    the expected front panel port data.
    """
    # Mock the database table behavior
    mock_config_table = mock.Mock()
    mock_state_table = mock.Mock()

    # Mock load_platform_util to prevent actual loading of the LedControl module
    mock_load_platform_util.return_value = mock.Mock()

    # Mock the return values for CONFIG_DB and STATE_DB tables
    mock_get_database_table.side_effect = lambda dbname, tblname, namespace: (
        mock_config_table if dbname == "CONFIG_DB" else mock_state_table
    )

    # Mock the keys and values for the CONFIG_DB and STATE_DB tables
    mock_config_table.getKeys.return_value = ["Ethernet0", "Ethernet1"]
    mock_config_table.get.side_effect = lambda key: (
        key,
        [
            ("index", "0" if key == "Ethernet0" else "1"),
            ("subport", "0"),
            ("role", "front-panel"),
        ],
    )
    mock_state_table.get.side_effect = lambda key: (
        key,
        [("netdev_oper_status", "up" if key == "Ethernet0" else "down")],
    )

    # Create an instance of DaemonLedd
    daemon_ledd = ledd.DaemonLedd()

    # Call the method under test
    namespaces = ["namespace1"]
    fp_port_list, fp_port_up_subports, logical_port_mapping = daemon_ledd.findFrontPanelPorts(namespaces)

    # Assertions
    assert len(fp_port_list) == ledd.MAX_FRONT_PANEL_PORTS
    assert len(fp_port_list[0]) == 1  # Ethernet0 is in index 0
    assert len(fp_port_list[1]) == 1  # Ethernet1 is in index 1
    assert "Ethernet0" in logical_port_mapping
    assert "Ethernet1" in logical_port_mapping
    assert logical_port_mapping["Ethernet0"]._state == ledd.Port.PORT_UP
    assert logical_port_mapping["Ethernet1"]._state == ledd.Port.PORT_DOWN
    assert fp_port_up_subports[0] == 1  # Ethernet0 is up
    assert fp_port_up_subports[1] == 0  # Ethernet1 is down

    daemon_ledd.processPortStateChange("Ethernet0", ledd.Port.PORT_DOWN)

@mock.patch("ledd.swsscommon.SubscriberStateTable")
@mock.patch("ledd.swsscommon.CastSelectableToRedisSelectObj")
def test_get_port_table_event(mock_cast_selectable, mock_subscriber_table):
    """
    Test PortStateObserver.getPortTableEvent to ensure it correctly processes events from the PORT table.
    """
    # Mock the selectable object and namespace
    mock_redis_select_obj = mock.Mock()
    mock_cast_selectable.return_value = mock_redis_select_obj
    mock_redis_select_obj.getDbConnector.return_value.getNamespace.return_value = "namespace1"

    # Mock the SubscriberStateTable behavior
    mock_table = mock.Mock()
    mock_subscriber_table.return_value = mock_table
    mock_table.pop.return_value = ("Ethernet0", "SET", [("netdev_oper_status", ledd.Port.PORT_UP)])

    # Create an instance of PortStateObserver
    observer = ledd.PortStateObserver()
    observer.tables["namespace1"] = mock_table

    # Call the method under test
    event = observer.getPortTableEvent(mock.Mock())

    # Assertions
    assert event is not None
    assert event[0] == "Ethernet0"  # Port name
    assert event[1] == ledd.Port.PORT_UP  # Port state

    # Verify that the mock methods were called
    mock_cast_selectable.assert_called_once()
    mock_table.pop.assert_called_once()

@mock.patch("ledd.swsscommon.SubscriberStateTable")
@mock.patch("ledd.swsscommon.CastSelectableToRedisSelectObj")
def test_get_port_table_event_no_key(mock_cast_selectable, mock_subscriber_table):
    """
    Test PortStateObserver.getPortTableEvent to handle cases where no key is returned.
    """
    # Mock the selectable object and namespace
    mock_redis_select_obj = mock.Mock()
    mock_cast_selectable.return_value = mock_redis_select_obj
    mock_redis_select_obj.getDbConnector.return_value.getNamespace.return_value = "namespace1"

    # Mock the SubscriberStateTable behavior
    mock_table = mock.Mock()
    mock_subscriber_table.return_value = mock_table
    mock_table.pop.return_value = (None, None, None)

    # Create an instance of PortStateObserver
    observer = ledd.PortStateObserver()
    observer.tables["namespace1"] = mock_table

    # Call the method under test
    event = observer.getPortTableEvent(mock.Mock())

    # Assertions
    assert event is None

    # Verify that the mock methods were called
    mock_cast_selectable.assert_called_once()
    mock_table.pop.assert_called_once()

@mock.patch("ledd.DaemonLedd")
def test_port_does_not_exist(mock_daemon_ledd_class):
    """Test behavior when the port does not exist."""
    mock_daemon_ledd = mock_daemon_ledd_class.return_value
    mock_daemon_ledd.fp_ports.getPort.return_value = None

    # Provide the required arguments for ledd.Port
    mock_port = ledd.Port("Ethernet0", 0, ledd.Port.PORT_DOWN, 0, "front-panel")
    mock_daemon_ledd.processPortStateChange("Ethernet0", mock_port.PORT_DOWN)

    mock_daemon_ledd.fp_ports.getPort.assert_not_called()
    mock_daemon_ledd.fp_ports.updatePortState.assert_not_called()
    mock_daemon_ledd.log_notice.assert_not_called()
    mock_daemon_ledd.fp_ports.updatePortLed.assert_not_called()