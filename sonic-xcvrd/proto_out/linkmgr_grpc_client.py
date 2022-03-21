import os
import time
import grpc
import linkmgr_grpc_driver_pb2_grpc 
import linkmgr_grpc_driver_pb2

host = "localhost"

def get_dualtor_active_side(_portid):
    "The  method, that sends gRPC messsages to the server to get active side"
    pid = os.getpid()
    with grpc.insecure_channel("{}:50075".format(host)) as channel:
        stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)
        response = stub.QuerySide(linkmgr_grpc_driver_pb2.SideRequest(portid = _portid))
        return response.side

def get_dualtor_admin_port_state(port_list):
    "The  method, that sends gRPC messsages to the server to get active side"
    pid = os.getpid()
    with grpc.insecure_channel("{}:50075".format(host)) as channel:
        stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)
        port_ids_list = linkmgr_grpc_driver_pb2.AdminRequest()
        port_ids_list.extend(port_list)
        response = stub.QueryAdminPortState(port_ids_list)
        response_port_ids = response.portid 
        response_port_ids_state = response.state

def set_dualtor_admin_port_forwarding_state(port_list):
    "The  method, that sends gRPC messsages to the server to get active side"
    pid = os.getpid()
    with grpc.insecure_channel("{}:50075".format(host)) as channel:
        stub = linkmgr_grpc_driver_pb2_grpc.DualToRActiveStub(channel)
        port_ids_list = linkmgr_grpc_driver_pb2.AdminRequest()
        port_ids_list.extend(port_list)
        response = stub.QueryAdminPortState(port_ids_list)
        response_port_ids = response.portid 
        response_port_ids_state = response.state
        return (response_port_ids, response_port_ids_state)




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
            helper_logger.log_debug("Could not retreive fieldvalue pairs for {}, inside config_db table {}".format('localhost', metadata_tbl[0].getTableName()))
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
                    platform_sfputil = self.load_platform_util(PLATFORM_SPECIFIC_MODULE_NAME, PLATFORM_SPECIFIC_CLASS_NAME)
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


        #y_cable_helper.init_ports_status_for_y_cable(
        #   platform_sfputil, platform_chassis, self.y_cable_presence, self.stop_event, is_vs)

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

        #if self.y_cable_presence[0] is True:
        #    y_cable_helper.delete_ports_status_for_y_cable()

        global_values = globals()
        val = global_values.get('platform_chassis')
        if val is not None:
            del global_values['platform_chassis']

    # Run daemon

    def run(self):
        self.log_info("Starting up...")

        # Start daemon initialization sequence
        self.init()



        # Start main loop
        self.log_info("Start daemon main loop")
        get_dualtor_active_side(_portid)

        while not self.stop_event.wait(self.timeout):
            self.log_info("gRPC main loop")

        self.log_info("Stop daemon main loop")


        # Start daemon deinitialization sequence
        self.deinit()

        self.log_info("Shutting down...")

        if self.sfp_error_event.is_set():
            sys.exit(SFP_SYSTEM_ERROR)

#
# Main =========================================================================
#


def main():
    grpc_driver = DaemonGrpcDriver(SYSLOG_IDENTIFIER)
    grpc_driver.run()


if __name__ == '__main__':
    main()
