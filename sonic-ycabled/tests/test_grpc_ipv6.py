"""Exercise IPv6 selection and channel setup without real gRPC or Redis I/O."""

from unittest.mock import MagicMock

import pytest

from ycable.ycable_utilities import y_cable_helper as helper


@pytest.mark.parametrize('row, field', [
    ({'soc_ipv4': 'invalid', 'soc_ipv6': 'fc02:1000::1/128'}, 'soc_ipv4'),
    ({'soc_ipv6': 'invalid/128'}, 'soc_ipv6'),
    ({'soc_ipv4': ''}, 'soc_ipv4'),
])
def test_invalid_soc_address_does_not_fall_back(monkeypatch, row, field):
    """Reject malformed configured addresses rather than silently changing family."""
    logger = MagicMock()
    monkeypatch.setattr(helper, 'helper_logger', logger)

    assert helper.get_soc_ip_for_grpc(row) == (None, field)
    logger.log_warning.assert_called_once_with(
        'Invalid {} address {} for gRPC channel setup'.format(field, row[field]))


@pytest.mark.parametrize('row', [{}, {'soc_ipv4': None, 'soc_ipv6': None}])
def test_missing_soc_addresses(row):
    """Return no selected family when neither address is available."""
    assert helper.get_soc_ip_for_grpc(row) == (None, None)


@pytest.mark.parametrize('row', [
    {'state': 'auto'},
    {'state': 'auto', 'soc_ipv6': 'invalid'},
    {'soc_ipv6': 'fc02:1000::1/128'},
])
def test_retry_channel_rejects_incomplete_config(monkeypatch, row):
    """Do not create or cache a channel without a valid address and state."""
    table = MagicMock()
    table.get.return_value = (True, list(row.items()))
    setup = MagicMock()
    logger = MagicMock()
    monkeypatch.setattr(helper, 'setup_grpc_channel_for_port', setup)
    monkeypatch.setattr(helper, 'helper_logger', logger)
    monkeypatch.setattr(helper, 'grpc_port_channels', {})
    monkeypatch.setattr(helper, 'grpc_port_stubs', {})

    assert helper.retry_setup_grpc_channel_for_port('Ethernet0', 0, {0: table}, {}) is False
    setup.assert_not_called()
    assert helper.grpc_port_channels == {}
    assert helper.grpc_port_stubs == {}
    logger.log_warning.assert_any_call(
        'No SoC IP address found for gRPC channel setup on port Ethernet0')


def test_retry_channel_uses_ipv6(monkeypatch):
    """Retry strips the IPv6 prefix and caches the returned channel and stub."""
    table = MagicMock()
    table.get.return_value = (True, [('state', 'auto'), ('soc_ipv6', 'fc02:1000::1/128')])
    channel, stub = MagicMock(), MagicMock()
    setup = MagicMock(return_value=(channel, stub))
    client = MagicMock()
    monkeypatch.setattr(helper, 'setup_grpc_channel_for_port', setup)
    monkeypatch.setattr(helper, 'grpc_port_channels', {})
    monkeypatch.setattr(helper, 'grpc_port_stubs', {})

    assert helper.retry_setup_grpc_channel_for_port('Ethernet0', 0, {0: table}, client) is True
    setup.assert_called_once_with('Ethernet0', 'fc02:1000::1', 0, client, False)
    assert helper.grpc_port_channels == {'Ethernet0': channel}
    assert helper.grpc_port_stubs == {'Ethernet0': stub}


@pytest.mark.parametrize('soc_ip, target', [
    ('fc02:1000::1', '[fc02:1000::1]:50075'),
    ('192.168.0.1', '192.168.0.1:50075'),
])
@pytest.mark.parametrize('secure', [True, False])
@pytest.mark.parametrize('is_async', [True, False])
def test_channel_factories_use_formatted_target(monkeypatch, soc_ip, target, secure, is_async):
    """All four channel factories get the right target, credentials and options."""
    grpc = MagicMock()
    credentials = MagicMock()
    get_credentials = MagicMock(return_value=credentials)
    stub_factory = MagicMock()
    callback = MagicMock()
    options = [('grpc.keepalive_time_ms', 4000)]
    monkeypatch.setattr(helper, 'grpc', grpc)
    monkeypatch.setattr(helper, 'get_grpc_credentials', get_credentials)
    monkeypatch.setattr(helper.linkmgr_grpc_driver_pb2_grpc, 'DualToRActiveStub', stub_factory)
    monkeypatch.setattr(helper, 'wait_for_state_change', callback)
    monkeypatch.setattr(helper, 'GRPC_CLIENT_OPTIONS', options[:])
    config = {'grpc_ssl_credential': 'nic.example'}
    api = grpc.aio if is_async else grpc
    factory = api.secure_channel if secure else api.insecure_channel

    channel, stub = helper.create_channel(
        'secure' if secure else 'insecure', 'server', config, soc_ip, 'Ethernet0', 0, is_async)

    if secure:
        get_credentials.assert_called_once_with('server', config)
        expected_options = [] if is_async else options
        factory.assert_called_once_with(
            target, credentials,
            options=expected_options + [('grpc.ssl_target_name_override', 'nic.example')])
    else:
        get_credentials.assert_not_called()
        if is_async:
            factory.assert_called_once_with(target)
        else:
            factory.assert_called_once_with(target, options=options)
    assert sum(item.call_count for item in (
        grpc.secure_channel, grpc.insecure_channel,
        grpc.aio.secure_channel, grpc.aio.insecure_channel)) == 1
    assert channel is factory.return_value
    stub_factory.assert_called_once_with(channel)
    assert stub is stub_factory.return_value
    if is_async:
        channel.subscribe.assert_not_called()
    else:
        channel.subscribe.assert_called_once()
        channel.subscribe.call_args[0][0](grpc.ChannelConnectivity.READY)
        callback.assert_called_once_with(grpc.ChannelConnectivity.READY, 'Ethernet0')


@pytest.mark.parametrize('has_address', [True, False])
def test_port_initialization_uses_ipv6_or_skips_missing_address(monkeypatch, has_address):
    """Initialize a real IPv6-only config path, but skip one without a SoC address."""
    row = {'state': 'auto', 'cable_type': 'active-active'}
    if has_address:
        row['soc_ipv6'] = 'fc02:1000::1/128'
    table = MagicMock()
    table.get.return_value = (True, list(row.items()))
    channel, stub = MagicMock(), MagicMock()
    setup = MagicMock(return_value=(channel, stub))
    initialize = MagicMock()
    logger = MagicMock()
    client = MagicMock()
    presence = [False]
    monkeypatch.setattr(helper, 'grpc_port_channels', {})
    monkeypatch.setattr(helper, 'grpc_port_stubs', {})
    monkeypatch.setattr(helper, 'setup_grpc_channel_for_port', setup)
    monkeypatch.setattr(helper, 'logical_port_name_to_physical_port_list', MagicMock(return_value=[1]))
    monkeypatch.setattr(helper, 'y_cable_wrapper_get_presence', MagicMock(return_value=True))
    monkeypatch.setattr(helper, 'post_port_mux_info_to_db', MagicMock())
    monkeypatch.setattr(helper, 'put_init_values_for_grpc_states', initialize)
    monkeypatch.setattr(helper, 'helper_logger', logger)

    helper.check_identifier_presence_and_setup_channel(
        'Ethernet0', {0: table}, {}, {}, 0, 1, {}, presence, client)

    if has_address:
        setup.assert_called_once_with('Ethernet0', 'fc02:1000::1', 0, client, False)
        assert helper.grpc_port_channels == {'Ethernet0': channel}
        assert helper.grpc_port_stubs == {'Ethernet0': stub}
        assert presence == [True]
        initialize.assert_called_once_with('Ethernet0', 1, {}, {}, 0)
    else:
        setup.assert_not_called()
        initialize.assert_not_called()
        assert presence == [False]
        assert helper.grpc_port_channels == {}
        assert helper.grpc_port_stubs == {}
        logger.log_warning.assert_called_once_with(
            'No SoC IP address found for gRPC channel setup on port Ethernet0')