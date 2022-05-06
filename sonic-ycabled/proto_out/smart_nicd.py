#!/usr/bin/env python3

"""
    smart_nic
    smart_nic interface/update daemon for SONiC
"""
import datetime
import os
import signal
import sys
import threading
import grpc
import linkmgr_grpc_driver_pb2_grpc
import linkmgr_grpc_driver_pb2
from sonic_py_common import daemon_base, device_info, logger
from sonic_py_common import multi_asic
from swsscommon import swsscommon
from collections import namedtuple

host = "localhost"
SYSLOG_IDENTIFIER = "grpc_driver"

SELECT_TIMEOUT = 1000
# Global platform specific sfputil class instance
platform_sfputil = None
# Global chassis object based on new platform api
platform_chassis = None

# Global port channels for gRPC RPC's
grpc_port_channels = {}
# Global port channel stubs for gRPC RPC's
grpc_port_stubs = {}

GRPC_PORT = 50075

DEFAULT_NAMESPACE = ""

LOOPBACK_INTERFACE_T0 = "10.212.64.1"
LOOPBACK_INTERFACE_LT0 = "10.212.64.2"
# rename and put in right place
# port id 0 -> maps to  T0
# port id 1 -> maps to  LT0

helper_logger = logger.Logger(SYSLOG_IDENTIFIER)


class MetadataInterceptor(grpc.UnaryUnaryClientInterceptor):

    class _ClientCallDetails(
            namedtuple(
                '_ClientCallDetails',
                ('method', 'timeout', 'metadata', 'credentials')),
            grpc.ClientCallDetails):
        """Wrapper class for initializing a new ClientCallDetails instance.
        """
        pass

    def __init__(self, injected_meta):
        self.injected_meta = injected_meta

    def intercept_unary_unary(self, continuation, client_call_details, request):

        if client_call_details.metadata is None:
            metadata = []
        else:
            metadata = list(client_call_details.metadata)

        metadata.append(self.injected_meta)

        client_call_details = self._ClientCallDetails(
            client_call_details.method,
            client_call_details.timeout,
            metadata,
            client_call_details.credentials
        )
        return continuation(client_call_details, request)


def logical_port_name_to_physical_port_list(port_name):
    if port_name.startswith("Ethernet"):
        if platform_sfputil.is_logical_port(port_name):
            return platform_sfputil.get_logical_to_physical(port_name)
        else:
            helper_logger.log_error("Invalid port for logical to physical port list '%s'" % port_name)
            return None
    else:
        return [int(port_name)]


def y_cable_wrapper_get_presence(physical_port):
    if platform_chassis is not None:
        try:
            return y_cable_platform_chassis.get_sfp(physical_port).get_presence()
        except NotImplementedError:
            pass
    if y_cable_is_platform_vs is True:
        return True
    return platform_sfputil.get_presence(physical_port)

"""
Helper Code for reference

def get_dualtor_active_side(_portid):
    "The  method, that sends gRPC messsages to the server to get active side"
    pid = os.getpid()
    with grpc.insecure_channel("{}:{}".format(host, GRPC_PORT)) as channel:
        stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)
        response = stub.QuerySide(linkmgr_grpc_driver_pb2.SideRequest(portid=_portid))
        return response.side

def get_dualtor_admin_port_state(port_list):
    "The  method sends gRPC messsages to the server to get active side"
    pid = os.getpid()
    root_cert = open('/etc/sonic/credentials/ca-chain-bundle.cert.pem', 'rb').read()
    key = open('/etc/sonic/credentials/client.key.pem', 'rb').read()
    cert_chain = open('/etc/sonic/credentials/client.cert.pem', 'rb').read()
    credential = grpc.ssl_channel_credentials(
        root_certificates=root_cert,
        private_key=key,
        certificate_chain=cert_chain)

    with grpc.insecure_channel("{}:{}".format(soc_op, GRPC_PORT)) as channel:
        stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)
        port_ids_list = linkmgr_grpc_driver_pb2.AdminRequest(portid=[0])
        operation_ids_list = linkmgr_grpc_driver_pb2.OperationRequest(portid=[0])
        operation_ids_dummy = linkmgr_grpc_driver_pb2.OperationRequest()
        dummy = linkmgr_grpc_driver_pb2.AdminRequest(portid=[0, 1], state=[0, 1])
        # print('port_ids_list dir = {}'.format(type(port_ids_list))
        # print('port_ids_list dir = {}'.format(dir(port_ids_list))
        #response = stub.QueryAdminForwardingPortState(port_ids_list)
        response = stub.QueryAdminForwardingPortState(dummy)
        #response = stub.QueryOperationPortState(operation_ids_dummy)
        #response = stub.QueryOperationPortState(operation_ids_list)
        response_port_ids = response.portid
        response_port_ids_state = response.state
        helper_logger.log_notice("response port ids = {}".format(response_port_ids))
        helper_logger.log_notice("response state ids = {}".format(response_port_ids_state))


def set_dualtor_admin_port_forwarding_state(port, port_list):
    "The  method sends gRPC messsages to the server to get active side"
    pid = os.getpid()
    root_cert = open('/etc/sonic/credentials/ca-chain-bundle.cert.pem', 'rb').read()
    key = open('/etc/sonic/credentials/client.key.pem', 'rb').read()
    cert_chain = open('/etc/sonic/credentials/client.cert.pem', 'rb').read()
    root_cert = open('/home/admin/proto_out1/proto_out/ca-chain-bundle.cert.pem', 'rb').read()
    key = open('/home/admin/proto_out1/proto_out/client.key.pem', 'rb').read()
    cert_chain = open('/home/admin/proto_out1/proto_out/client.cert.pem', 'rb').read()
    
    credential = grpc.ssl_channel_credentials(
        root_certificates=root_cert,
        private_key=key,
        certificate_chain=cert_chain)
    with grpc.insecure_channel("{}:GRPC_PORT".format(host)) as channel:
        stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)
        port_ids_list = linkmgr_grpc_driver_pb2.AdminRequest(portid=[0])
        operation_ids_list = linkmgr_grpc_driver_pb2.OperationRequest(portid=[0])
        dummy = linkmgr_grpc_driver_pb2.AdminRequest(portid=[0, 1], state=[0, 1])
        # print('port_ids_list dir = {}'.format(type(port_ids_list))
        # print('port_ids_list dir = {}'.format(dir(port_ids_list))
        #response = stub.QueryAdminForwardingPortState(port_ids_list)
        response = stub.QueryAdminForwardingPortState(dummy)
        #response = stub.QueryOperationPortState(operation_ids_dummy)
        #response = stub.QueryOperationPortState(operation_ids_list)
        response_port_ids = response.portid
        response_port_ids_state = response.state
        helper_logger.log_notice("response port ids = {}".format(response_port_ids))
        helper_logger.log_notice("response state ids = {}".format(response_port_ids_state))

"""

def check_mux_cable_port_type(logical_port_name, port_tbl, asic_index):

    (status, fvs) = port_tbl[asic_index].get(logical_port_name)
    if status is False:
        helper_logger.log_warning(
            "Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(logical_port_name, port_tbl[asic_index].getTableName()))
        return False

    else:
        # Convert list of tuples to a dictionary
        mux_table_dict = dict(fvs)
        if "state" in mux_table_dict and "soc_ipv4" in mux_table_dict:

            val = mux_table_dict.get("state", None)
            soc_ipv4 = mux_table_dict.get("soc_ipv4", None)
            cable_type = mux_table_dict.get("cable_type", None)
            if val in ["active", "standby", "auto", "manual"] and cable_type == "active-active":
                return True
            else:
                return False
        else:
            return False


"""
Read Side to port Mapping for reference
read_side == 0
self, send query port id 0 -> response port id 0 and state 1/0 (active/standby);
peer, send query port id 1 -> response port id 1 and state 1/0 (active/standby);
if both =
self , peer send query port id 0 and 1 and state active/standby -> response port id 0 , 1 and state 0 and 1

"""


def parse_grpc_response_hw_mux_cable_change_state(ret, response, portid, port):
    state = 'unknown'
    "return a list of states"
    if ret is True:
        if response.portid[0] == portid:
            if response.state[0] == True:
                state = 'active'
            # No other values expected
            elif response.state[0] == False:
                state = 'standby'
            else:
                helper_logger.log_warning("recieved an error state while parsing response hw mux no response state for port".format(port))
        else:
            helper_logger.log_warning("recieved an error portid while parsing response hw mux no portid for port".format(port))

    else:
        helper_logger.log_warning("recieved an error state while parsing response hw mux for port".format(port))
        state = 'unknown'

    return state


def parse_grpc_response_forwarding_state(ret, response, read_side):
    self_state = peer_state = 'unknown'

    if ret is True and response is not None:
        if int(read_side) == 0:
            if response.state[0] == True:
                self_state = 'active'
            elif response.state[0] == False:
                self_state = 'standby'
            # No other values expected, should we raise exception/msg
            # TODO handle other responses
            if response.state[1] == True:
                peer_state = 'active'
            elif response.state[1] == False:
                peer_state = 'standby'

        elif int(read_side) == 1:
            if response.state[1] == True:
                self_state = 'active'
            elif response.state[0] == False:
                self_state = 'standby'
            if response.state[0] == True:
                peer_state = 'active'
            elif response.state[0] == False:
                peer_state = 'standby'
    else:
        self_state = 'unknown'
        peer_state = 'unknown'

    return (self_state, peer_state)


def handle_fwd_state_command_grpc_notification(fvp_m, hw_mux_cable_tbl, fwd_state_response_tbl, asic_index, port, appl_db):

    helper_logger.log_debug("recevied the notification fwd state")
    fvp_dict = dict(fvp_m)

    if "command" in fvp_dict:
        # check if xcvrd got a probe command
        probe_identifier = fvp_dict["command"]

        if probe_identifier == "probe":
            helper_logger.log_debug("processing the notification fwd state")
            (status, fv) = hw_mux_cable_tbl[asic_index].get(port)
            if status is False:
                helper_logger.log_warning("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(
                    port, hw_mux_cable_tbl[asic_index].getTableName()))
                return False
            mux_port_dict = dict(fv)
            read_side = mux_port_dict.get("read_side")
            helper_logger.log_debug("while invoking fwd_state read_side = {}".format(read_side))
            # TODO state only for dummy value in this request MSG remove this
            request = linkmgr_grpc_driver_pb2.AdminRequest(portid=[int(read_side), 1 - int(read_side)], state=[0, 0])
            helper_logger.log_debug(
                "calling RPC for getting forwarding state read_side portid = {} Ethernet port {}".format(read_side, port))

            self_state = "unknown"
            peer_state = "unknown"
            stub = grpc_port_stubs.get("port", None)
            if stub is None:
                helper_logger.log_notice("stub is None for getting forwarding state RPC port {}".format(port))
                retry_setup_grpc_channel_for_port(port, asic_index)
                stub = grpc_port_stubs.get(port, None)
                if stub is None:
                    helper_logger.log_debug(
                        "stub was None for performing fwd mux RPC port {}, setting it up again did not work".format(port))
                    fvs_updated = swsscommon.FieldValuePairs([('response', str(self_state)),
                                                              ('response_peer', str(peer_state))])
                    fwd_state_response_tbl[asic_index].set(port, fvs_updated)
                    return

            ret, response = try_grpc(stub.QueryAdminForwardingPortState, request)

            (self_state, peer_state) = parse_grpc_response_forwarding_state(ret, response, read_side)
            if response is not None:
                # Debug only, remove this section once Server side is Finalized
                fwd_response_port_ids = response.portid
                fwd_response_port_ids_state = response.state
                helper_logger.log_notice(
                    "forwarding state RPC received response port ids = {}".format(fwd_response_port_ids))
                helper_logger.log_notice(
                    "forwarding state RPC received response state values = {}".format(fwd_response_port_ids_state))
            fvs_updated = swsscommon.FieldValuePairs([('response', str(self_state)),
                                                      ('response_peer', str(peer_state))])
            fwd_state_response_tbl[asic_index].set(port, fvs_updated)
            helper_logger.log_debug("processed the notification fwd state cleanly")
            return True


def handle_hw_mux_cable_table_grpc_notification(fvp, hw_mux_cable_tbl, asic_index, grpc_metrics_tbl, peer, port):

    # entering this section signifies a gRPC start for state
    # change request from swss so initiate recording in mux_metrics table
    time_start = datetime.datetime.utcnow().strftime("%Y-%b-%d %H:%M:%S.%f")
    # This check might be redundant, to check, the presence of this Port in keys
    # in logical_port_list but keep for now for coherency
    # also skip checking in logical_port_list inside sfp_util

    helper_logger.log_debug("recevied the notification mux hw state")
    fvp_dict = dict(fvp)
    toggle_side = "self"

    if "state" in fvp_dict:
        # got a state change
        new_state = fvp_dict["state"]
        requested_status = new_state
        if requested_status in ["active", "standby"]:

            (status, fvs) = hw_mux_cable_tbl[asic_index].get(port)
            if status is False:
                helper_logger.log_debug("Could not retreive fieldvalue pairs for {}, inside state_db table {}".format(
                    port, hw_mux_cable_tbl[asic_index].getTableName()))
                return
            helper_logger.log_debug("processing the notification mux hw state")
            mux_port_dict = dict(fvs)
            old_state = mux_port_dict.get("state", None)
            read_side = mux_port_dict.get("read_side", None)
            curr_read_side = int(read_side)
            # Now whatever is the state requested, call gRPC to update the soc state appropriately
            if peer == True:
                curr_read_side = 1-int(read_side)
                toggle_side = "peer"

            if new_state == "active":
                state_req = 1
            elif new_state == "standby":
                state_req = 0

            helper_logger.log_notice(
                "calling RPC for hw mux_cable set state state peer = {} portid {} Ethernet port".format(peer, port))

            request = linkmgr_grpc_driver_pb2.AdminRequest(portid=[curr_read_side], state=[state_req])

            stub = grpc_port_stubs.get(port, None)
            if stub is None:
                helper_logger.log_debug("stub is None for performing hw mux RPC port {}".format(port))
                retry_setup_grpc_channel_for_port(port, asic_index)
                stub = grpc_port_stubs.get(port, None)
                if stub is None:
                    helper_logger.log_notice(
                        "stub was None for performing hw mux RPC port {}, setting it up again did not work".format(port))
                    return

            ret, response = try_grpc(stub.SetAdminForwardingPortState, request, timeout=10)
            if response is not None:
                # Debug only, remove this section once Server side is Finalized
                hw_response_port_ids = response.portid
                hw_response_port_ids_state = response.state
                helper_logger.log_notice(
                    "Set admin state RPC received response port ids = {}".format(hw_response_port_ids))
                helper_logger.log_notice(
                    "Set admin state RPC received response state values = {}".format(hw_response_port_ids_state))

            active_side = parse_grpc_response_hw_mux_cable_change_state(ret, response, curr_read_side, port)

            if active_side == "unknown":
                helper_logger.log_debug(
                    "ERR: Got a change event for updating gRPC but could not toggle the mux-direction for port {} state from {} to {}, writing unknown".format(port, old_state, new_state))
                new_state = 'unknown'

            time_end = datetime.datetime.utcnow().strftime("%Y-%b-%d %H:%M:%S.%f")
            fvs_metrics = swsscommon.FieldValuePairs([('grpc_switch_{}_{}_start'.format(toggle_side, new_state), str(time_start)),
                                                      ('grpc_switch_{}_{}_end'.format(toggle_side, new_state), str(time_end))])
            grpc_metrics_tbl[asic_index].set(port, fvs_metrics)

            fvs_updated = swsscommon.FieldValuePairs([('state', new_state),
                                                      ('read_side', read_side),
                                                      ('active_side', str(active_side))])
            hw_mux_cable_tbl[asic_index].set(port, fvs_updated)
            helper_logger.log_debug("processed the notification hw mux state cleanly")
        else:
            helper_logger.log_info("Got a change event on port {} of table {} that does not contain state".format(
                port, swsscommon.APP_HW_MUX_CABLE_TABLE_NAME))


# Thread wrapper class to update/serve gRPC queries/actions status periodically
class GrpcTableUpdateTask(object):
    def __init__(self):
        self.task_thread = None
        self.task_stopping_event = threading.Event()

        if multi_asic.is_multi_asic():
            # Load the namespace details first from the database_global.json file.
            swsscommon.SonicDBConfig.initializeGlobalConfig()

    def task_worker(self):

        # Connect to STATE_DB and APPL_DB and get both the HW_MUX_STATUS_TABLE info
        appl_db, state_db, config_db, status_tbl, status_tbl_peer, hw_mux_cable_tbl, hw_mux_cable_tbl_peer = {}, {}, {}, {}, {}, {}, {}
        hw_mux_cable_tbl_keys = {}
        port_tbl, port_table_keys = {}, {}
        fwd_state_command_tbl, fwd_state_response_tbl, mux_command_tbl = {}, {}, {}
        grpc_metrics_tbl = {}

        sel = swsscommon.Select()

        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            # Open a handle to the Application database, in all namespaces
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            appl_db[asic_id] = daemon_base.db_connect("APPL_DB", namespace)
            config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            status_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], swsscommon.APP_HW_MUX_CABLE_TABLE_NAME)
            status_tbl_peer[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "HW_MUX_CABLE_TABLE_PEER")
            # TODO add definition inside app DB
            fwd_state_command_tbl[asic_id] = swsscommon.SubscriberStateTable(
                appl_db[asic_id], "FORWARDING_STATE_COMMAND")
            fwd_state_response_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], "FORWARDING_STATE_RESPONSE")
            mux_command_tbl[asic_id] = swsscommon.Table(
                appl_db[asic_id], swsscommon.APP_MUX_CABLE_COMMAND_TABLE_NAME)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            hw_mux_cable_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            hw_mux_cable_tbl_peer[asic_id] = swsscommon.Table(
                state_db[asic_id], "HW_MUX_CABLE_TABLE_PEER")
            grpc_metrics_tbl[asic_id] = swsscommon.Table(
                state_db[asic_id], swsscommon.STATE_MUX_METRICS_TABLE_NAME)
            hw_mux_cable_tbl_keys[asic_id] = hw_mux_cable_tbl[asic_id].getKeys()
            port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")
            port_table_keys[asic_id] = port_tbl[asic_id].getKeys()
            sel.addSelectable(status_tbl[asic_id])
            sel.addSelectable(status_tbl_peer[asic_id])
            sel.addSelectable(fwd_state_command_tbl[asic_id])

        # Listen indefinitely for changes to the HW_MUX_CABLE_TABLE in the Application DB's
        while True:
            # Use timeout to prevent ignoring the signals we want to handle
            # in signal_handler() (e.g. SIGTERM for graceful shutdown)

            if self.task_stopping_event.is_set():
                break

            (state, selectableObj) = sel.select(SELECT_TIMEOUT)

            if state == swsscommon.Select.TIMEOUT:
                # Do not flood log when select times out
                continue
            if state != swsscommon.Select.OBJECT:
                helper_logger.log_debug(
                    "sel.select() did not  return swsscommon.Select.OBJECT for sonic_y_cable updates")
                continue

            # Get the redisselect object  from selectable object
            redisSelectObj = swsscommon.CastSelectableToRedisSelectObj(
                selectableObj)
            # Get the corresponding namespace from redisselect db connector object
            namespace = redisSelectObj.getDbConnector().getNamespace()
            asic_index = multi_asic.get_asic_index_from_namespace(namespace)

            while True:
                (port, op, fvp) = status_tbl[asic_index].pop()
                if not port:
                    break
                if not check_mux_cable_port_type(port, port_tbl, asic_index):
                    break

                if fvp:
                    handle_hw_mux_cable_table_grpc_notification(
                        fvp, hw_mux_cable_tbl, asic_index, grpc_metrics_tbl, False, port)

            while True:
                (port_m, op_m, fvp_m) = fwd_state_command_tbl[asic_index].pop()

                if not port_m:
                    break
                if not check_mux_cable_port_type(port_m, port_tbl, asic_index):
                    break

                if fvp_m:
                    handle_fwd_state_command_grpc_notification(
                        fvp_m, hw_mux_cable_tbl, fwd_state_response_tbl, asic_index, port_m, appl_db)

            while True:
                (port_n, op_n, fvp_n) = status_tbl_peer[asic_index].pop()
                if not port_n:
                    break
                if not check_mux_cable_port_type(port_n, port_tbl, asic_index):
                    break

                if fvp_n:
                    handle_hw_mux_cable_table_grpc_notification(
                        fvp_n, hw_mux_cable_tbl_peer, asic_index, grpc_metrics_tbl, True, port_n)

    def task_run(self):
        self.task_thread = threading.Thread(target=self.task_worker)
        self.task_thread.start()

    def task_stop(self):

        self.task_stopping_event.set()
        self.task_thread.join()


def hook_grpc_nic_simulated(target, soc_ip):
    """
    Args:
        target (function): The function collecting transceiver info.
    """

    NIC_SIMULATOR_CONFIG_FILE = "/etc/sonic/nic_simulator.json"

    def wrapper(*args, **kwargs):
        res = target(*args, **kwargs)
        if os.path.exists(MUX_SIMULATOR_CONFIG_FILE):
            """setup channels for all downlinks
            NIC simulator will run on same port number
            Todo put a task for secure channel"""
            channel = grpc.insecure_channel("server_ip:GRPC_PORT".format(host))
            stub = None
            #metadata_interceptor = MetadataInterceptor(("grpc_server", soc_ipv4))
            #intercept_channel = grpc.intercept_channel(channel, metadata_interceptor)
            #stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(intercept_channel)
            # TODO hook the interceptor appropriately
        return channel, stub

    wrapper.__name__ = target.__name__

    return wrapper


def retry_setup_grpc_channel_for_port(port, asic_index):

    config_db, port_tbl = {}, {}
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")

    (status, fvs) = port_tbl[asic_index].get(port)
    if status is False:
        helper_logger.log_warning(
            "Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(port, port_tbl[asic_index].getTableName()))
        return

    else:
        # Convert list of tuples to a dictionary
        mux_table_dict = dict(fvs)
        if "state" in mux_table_dict and "soc_ipv4" in mux_table_dict:
            soc_ipv4 = mux_table_dict.get("soc_ipv4", None)

            channel, stub = setup_grpc_channel_for_port(port, soc_ipv4)
            if channel is None or stub is None:
                helper_logger.log_notice(
                    "stub is None, while reattempt setting up channels did not work{}".format(port))
            return
        else:
            grpc_port_channels[port] = channel
            grpc_port_stubs[port] = stub

# @hook_grpc_nic_simulated


def setup_grpc_channel_for_port(port, soc_ip):
    "TODO make these configurable like RESTAPI"
    """
    root_cert = open('/etc/sonic/credentials/ca-chain-bundle.cert.pem', 'rb').read()
    key = open('/etc/sonic/credentials/client.key.pem', 'rb').read()
    cert_chain = open('/etc/sonic/credentials/client.cert.pem', 'rb').read()

    """
    """
    Dummy values for lab for now
    TODO remove these once done
    root_cert = open('/home/admin/proto_out1/proto_out/ca-chain-bundle.cert.pem', 'rb').read()
    key = open('/home/admin/proto_out1/proto_out/client.key.pem', 'rb').read()
    cert_chain = open('/home/admin/proto_out1/proto_out/client.cert.pem', 'rb').read()
    """
    """credential = grpc.ssl_channel_credentials(
            root_certificates=root_cert,
            private_key=key,
            certificate_chain=cert_chain)
    """
    helper_logger.log_debug("setting up gRPC channels for RPC's")
    channel = grpc.insecure_channel("{}:{}".format(soc_ip, GRPC_PORT), options=[('grpc.keepalive_timeout_ms', 1000)])
    stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)

    channel_ready = grpc.channel_ready_future(channel)

    try:
        channel_ready.result(timeout=0.5)
    except grpc.FutureTimeoutError:
        channel = None
        stub = None
        return channel, stub

    if stub is not None:
        helper_logger.log_warning("channel was setup gRPC ip {} port {}".format(soc_ip, port))
    else:
        helper_logger.log_warning("channel was not setup gRPC ip {} port {} no gRPC server running".format(soc_ip, port))

    return channel, stub


def channel_shutdown(_channel):
    _channel.close()


def process_loopback_interface_and_get_read_side(loopback_keys):

    asic_index = multi_asic.get_asic_index_from_namespace(DEFAULT_NAMESPACE)

    for key in loopback_keys[asic_index]:
        helper_logger.log_debug("key = {} ".format(key))
        if key.startswith("Loopback3|") and "/" in key and "::" not in key:
            helper_logger.log_debug("Loopback split  {} ".format(key))
            temp_list = key.split('|')
            addr = temp_list[1].split('/')
            helper_logger.log_debug("Loopback split 2  {} ".format(addr))
            if addr[0] == LOOPBACK_INTERFACE_LT0:
                return 0
            elif addr[0] == LOOPBACK_INTERFACE_T0:
                return 1
            else:
                # Loopback3 should be present, if not present log a warning
                helper_logger.log_warning("Could not get any address associated with Loopback3")
                return -1

    return -1


def check_identfier_presence_and_setup_channel(logical_port_name, port_tbl, hw_mux_cable_tbl, hw_mux_cable_tbl_peer, asic_index, read_side):

    global grpc_port_stubs
    global grpc_port_channels

    (status, fvs) = port_tbl[asic_index].get(logical_port_name)
    if status is False:
        helper_logger.log_warning(
            "Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(logical_port_name, port_tbl[asic_index].getTableName()))
        return

    else:
        # Convert list of tuples to a dictionary
        mux_table_dict = dict(fvs)
        if "state" in mux_table_dict and "soc_ipv4" in mux_table_dict:

            val = mux_table_dict.get("state", None)
            soc_ipv4 = mux_table_dict.get("soc_ipv4", None)
            cable_type = mux_table_dict.get("cable_type", None)

            if val in ["active", "standby", "auto", "manual"] and cable_type == "active-active":

                # import the module and load the port instance
                physical_port_list = logical_port_name_to_physical_port_list(
                    logical_port_name)

                if len(physical_port_list) == 1:

                    physical_port = physical_port_list[0]
                    if True:
                        channel, stub = setup_grpc_channel_for_port(logical_port_name, soc_ipv4)
                        if channel is not None:
                            grpc_port_channels[logical_port_name] = channel
                            helper_logger.log_notice(
                                "channel is None, first time setting up channels did not work {}".format(logical_port_name))
                        if stub is not None:
                            grpc_port_stubs[logical_port_name] = stub
                            helper_logger.log_notice(
                                "stub is None, first time setting up channels did not work {}".format(logical_port_name))
                        fvs_updated = swsscommon.FieldValuePairs([('read_side', str(read_side))])
                        hw_mux_cable_tbl[asic_index].set(logical_port_name, fvs_updated)
                        hw_mux_cable_tbl_peer[asic_index].set(logical_port_name, fvs_updated)
                    else:
                        helper_logger.log_warning(
                            "DAC cable not present while Channel setup Port {} for gRPC channel initiation".format(logical_port_name))

                else:
                    helper_logger.log_warning(
                        "DAC cable logical to physical port mapping returned more than one physical ports while Channel setup Port {}".format(logical_port_name))
            else:
                helper_logger.log_warning(
                    "DAC cable logical to physical port mapping returned more than one physical ports while Channel setup Port {}".format(logical_port_name))


def setup_grpc_channels(stop_event):

    helper_logger.log_debug("setting up channels for active-active")
    config_db, state_db, port_tbl, loopback_tbl, port_table_keys = {}, {}, {}, {}, {}
    loopback_keys = {}
    hw_mux_cable_tbl = {}
    hw_mux_cable_tbl_peer = {}

    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "MUX_CABLE")
        loopback_tbl[asic_id] = swsscommon.Table(
            config_db[asic_id], "LOOPBACK_INTERFACE")
        loopback_keys[asic_id] = loopback_tbl[asic_id].getKeys()
        port_table_keys[asic_id] = port_tbl[asic_id].getKeys()
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        hw_mux_cable_tbl[asic_id] = swsscommon.Table(
            state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
        hw_mux_cable_tbl_peer[asic_id] = swsscommon.Table(
            state_db[asic_id], "HW_MUX_CABLE_TABLE_PEER")

    read_side = process_loopback_interface_and_get_read_side(loopback_keys)

    helper_logger.log_debug("while setting up grpc channels read side = {}".format(read_side))

    # Init PORT_STATUS table if ports are on Y cable
    logical_port_list = platform_sfputil.logical
    for logical_port_name in logical_port_list:
        if stop_event.is_set():
            break

        # Get the asic to which this port belongs
        asic_index = platform_sfputil.get_asic_id_for_logical_port(
            logical_port_name)
        if asic_index is None:
            helper_logger.log_warning(
                "Got invalid asic index for {}, ignored".format(logical_port_name))
            continue

        if logical_port_name in port_table_keys[asic_index]:
            check_identfier_presence_and_setup_channel(
                logical_port_name, port_tbl, hw_mux_cable_tbl, hw_mux_cable_tbl_peer, asic_index, read_side)
        else:
            # This port does not exist in Port table of config but is present inside
            # logical_ports after loading the port_mappings from port_config_file
            # This should not happen
            helper_logger.log_warning(
                "Could not retreive port inside config_db PORT table {} for gRPC channel initiation".format(logical_port_name))


def try_grpc(callback, *args, **kwargs):
    """
    Handy function to invoke the callback and catch NotImplementedError
    :param callback: Callback to be invoked
    :param args: Arguments to be passed to callback
    :param kwargs: Default return value if exception occur
    :return: Default return value if exception occur else return value of the callback
    """

    return_val = True
    try:
        resp = callback(*args)
        if resp is None:
            return_val = False
    except grpc.RpcError as e:
        err_msg = 'Grpc error code '+str(e.code())
        if e.code() == grpc.StatusCode.CANCELLED:
            helper_logger.log_notice("rpc cancelled for port= {}".format("0"))
        elif e.code() == grpc.StatusCode.UNAVAILABLE:
            helper_logger.log_notice("rpc unavailable for port= {}".format("0"))
        elif e.code() == grpc.StatusCode.INVALID_ARGUMENT:
            helper_logger.log_notice("rpc unavailable for port= {}".format("0"))
        resp = None
        return_val = False

    return return_val, resp


def close(channel):
    "Close the channel"
    channel.close()


class DaemonGrpcDriver(daemon_base.DaemonBase):
    def __init__(self, log_identifier):
        super(DaemonGrpcDriver, self).__init__(log_identifier)

        self.num_asics = multi_asic.get_num_asics()
        self.stop_event = threading.Event()

    # Signal handler
    def signal_handler(self, sig, frame):
        if sig == signal.SIGHUP:
            self.log_info("Caught SIGHUP - ignoring...")
        elif sig == signal.SIGINT:
            self.log_info("Caught SIGINT - exiting...")
            self.stop_event.set()
        elif sig == signal.SIGTERM:
            self.log_info("Caught SIGTERM - exiting...")
            self.stop_event.set()
        else:
            self.log_warning("Caught unhandled signal '" + sig + "'")

    # Initialize daemon
    def init(self):
        global platform_sfputil
        global platform_chassis

        helper_logger.log_warning("starting daemon inside init")
        self.log_info("Start daemon init...")
        config_db, metadata_tbl, metadata_dict = {}, {}, {}
        is_vs = False

        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            metadata_tbl[asic_id] = swsscommon.Table(
                config_db[asic_id], "DEVICE_METADATA")

        (status, fvs) = metadata_tbl[0].get("localhost")

        if status is False:
            helper_logger.log_debug("Could not retreive fieldvalue pairs for {}, inside config_db table {}".format(
                'localhost', metadata_tbl[0].getTableName()))
            return

        else:
            # Convert list of tuples to a dictionary
            metadata_dict = dict(fvs)
            if "platform" in metadata_dict:
                val = metadata_dict.get("platform", None)
                if val == "x86_64-kvm_x86_64-r0":
                    is_vs = True

        # Load new platform api class
        try:
            if is_vs is False:
                import sonic_platform.platform
                platform_chassis = sonic_platform.platform.Platform().get_chassis()
                self.log_info("chassis loaded {}".format(platform_chassis))
            # we have to make use of sfputil for some features
            # even though when new platform api is used for all vendors.
            # in this sense, we treat it as a part of new platform api.
            # we have already moved sfputil to sonic_platform_base
            # which is the root of new platform api.
            import sonic_platform_base.sonic_sfp.sfputilhelper
            platform_sfputil = sonic_platform_base.sonic_sfp.sfputilhelper.SfpUtilHelper()
        except Exception as e:
            self.log_warning("Failed to load chassis due to {}".format(repr(e)))

        # Load platform specific sfputil class
        if platform_chassis is None or platform_sfputil is None:
            if is_vs is False:
                try:
                    platform_sfputil = self.load_platform_util(
                        PLATFORM_SPECIFIC_MODULE_NAME, PLATFORM_SPECIFIC_CLASS_NAME)
                except Exception as e:
                    self.log_error("Failed to load sfputil: {}".format(str(e)), True)
                    sys.exit(SFPUTIL_LOAD_ERROR)

        if multi_asic.is_multi_asic():
            # Load the namespace details first from the database_global.json file.
            swsscommon.SonicDBConfig.initializeGlobalConfig()

        # Load port info
        try:
            if multi_asic.is_multi_asic():
                # For multi ASIC platforms we pass DIR of port_config_file_path and the number of asics
                (platform_path, hwsku_path) = device_info.get_paths_to_platform_and_hwsku_dirs()
                platform_sfputil.read_all_porttab_mappings(hwsku_path, self.num_asics)
            else:
                # For single ASIC platforms we pass port_config_file_path and the asic_inst as 0
                port_config_file_path = device_info.get_path_to_port_config_file()
                platform_sfputil.read_porttab_mappings(port_config_file_path, 0)
        except Exception as e:
            self.log_error("Failed to read port info: {}".format(str(e)), True)
            sys.exit(PORT_CONFIG_LOAD_ERROR)

        state_db = {}

        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)

        # Make sure this daemon started after all port configured
        self.log_info("Wait for port config is done")
        setup_grpc_channels(self.stop_event)

    # Deinitialize daemon

    def deinit(self):
        self.log_info("Start daemon deinit...")

        # Delete all the information from DB and then exit
        logical_port_list = platform_sfputil.logical
        for logical_port_name in logical_port_list:
            # Get the asic to which this port belongs
            asic_index = platform_sfputil.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
                continue

        global_values = globals()
        val = global_values.get('platform_chassis')
        if val is not None:
            del global_values['platform_chassis']

    # Run daemon

    def run(self):
        self.log_info("Starting up...")

        # Start daemon initialization sequence

        #port_list = ["0"]

        # get_dualtor_admin_port_state(port_list)

        # Start main loop
        #self.log_info("Start daemon main loop")
        # get_dualtor_active_side(_portid)
        self.init()
        grpc_info_update = GrpcTableUpdateTask()
        grpc_info_update.task_run()

        # Start daemon deinitialization sequence


#
# Main =========================================================================
#


def main():
    grpc_driver = DaemonGrpcDriver(SYSLOG_IDENTIFIER)
    grpc_driver.run()


if __name__ == '__main__':
    main()
