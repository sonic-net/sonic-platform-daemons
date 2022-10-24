"""
    y_cable_table_helper.py
    helper utlities configuring y_cable tables for ycabled daemon
"""


from sonic_py_common import daemon_base
from sonic_py_common import multi_asic
from swsscommon import swsscommon

class YcableInfoUpdateTableHelper(object):
    def __init__(self):

        self.state_db = {}
        self.config_db = {}
        self.port_tbl = {}
        self.status_tbl = {}
        self.y_cable_tbl = {} 
        self.mux_tbl = {}

        # Get the namespaces in the platform
        fvs_updated = swsscommon.FieldValuePairs([('log_verbosity', 'notice')])
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            self.state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            self.config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            self.port_tbl[asic_id] = swsscommon.Table(self.config_db[asic_id], "MUX_CABLE")
            self.status_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_STATUS_TABLE)
            self.y_cable_tbl[asic_id] = swsscommon.Table(
                self.state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            self.mux_tbl[asic_id] = swsscommon.Table(
                self.state_db[asic_id], MUX_CABLE_INFO_TABLE)

    def get_state_db(self):
        return self.state_db

    def get_config_db(self):
        return self.config_db

    def get_port_tbl(self):
        return self.port_tbl

    def get_status_tbl(self):
        return self.status_tbl

    def get_y_cable_tbl(self):
        return self.y_cable_tbl

    def get_mux_tbl(self):
        return self.mux_tbl


class YcableStateUpdateTableHelper(object):
    def __init__(self):

        self.state_db = {}
        self.sub_status_tbl = {}

        # Get the namespaces in the platform
        fvs_updated = swsscommon.FieldValuePairs([('log_verbosity', 'notice')])
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            self.state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            self.sub_status_tbl[asic_id] = swsscommon.SubscriberStateTable(
                self.state_db[asic_id], TRANSCEIVER_STATUS_TABLE)



    def get_sub_status_tbl(self):
        return self.sub_status_tbl



class DaemonYcableTableHelper(object):
    def __init__(self):

        self.state_db = {}
        self.config_db = {}
        self.port_tbl = {}
        self.y_cable_tbl = {} 
        self.metadata_tbl = {}
        self.static_tbl, self.mux_tbl = {}, {}
        self.port_table_keys = {}
        self.xcvrd_log_tbl = {}
        self.loopback_tbl= {}
        self.loopback_keys = {}
        self.hw_mux_cable_tbl = {}
        self.hw_mux_cable_tbl_peer = {}
        self.grpc_config_tbl = {}

        # Get the namespaces in the platform
        fvs_updated = swsscommon.FieldValuePairs([('log_verbosity', 'notice')])
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            self.state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            self.config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
            self.port_tbl[asic_id] = swsscommon.Table(self.config_db[asic_id], "MUX_CABLE")
            self.y_cable_tbl[asic_id] = swsscommon.Table(
                self.state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            self.mux_tbl[asic_id] = swsscommon.Table(
                self.state_db[asic_id], MUX_CABLE_INFO_TABLE)
            self.metadata_tbl[asic_id] = swsscommon.Table(
                self.config_db[asic_id], "DEVICE_METADATA")
            self.port_table_keys[asic_id] = self.port_tbl[asic_id].getKeys()
            self.xcvrd_log_tbl[asic_id] = swsscommon.Table(self.config_db[asic_id], "XCVRD_LOG")
            self.xcvrd_log_tbl[asic_id].set("Y_CABLE", fvs_updated)
            self.loopback_tbl[asic_id] = swsscommon.Table(
                self.config_db[asic_id], "LOOPBACK_INTERFACE")
            self.loopback_keys[asic_id] = self.loopback_tbl[asic_id].getKeys()
            self.hw_mux_cable_tbl[asic_id] = swsscommon.Table(
                self.state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            self.hw_mux_cable_tbl_peer[asic_id] = swsscommon.Table(
                self.state_db[asic_id], "HW_MUX_CABLE_TABLE_PEER")
            self.static_tbl[asic_id] = swsscommon.Table(
                self.state_db[asic_id], MUX_CABLE_STATIC_INFO_TABLE)
            self.grpc_config_tbl[asic_id] = swsscommon.Table(self.config_db[asic_id], "GRPCCLIENT")


    def get_state_db(self):
        return self.state_db

    def get_config_db(self):
        return self.config_db

    def get_port_tbl(self):
        return self.port_tbl

    def get_y_cable_tbl(self):
        return self.y_cable_tbl

    def get_mux_tbl(self):
        return self.mux_tbl

    def get_metadata_tbl(self):
        return self.metadata_tbl

    def get_xcvrd_log_tbl(self):
        return self.xcvrd_log_tbl

    def get_loopback_tbl(self):
        return self.loopback_tbl

    def get_hw_mux_cable_tbl(self):
        return self.hw_mux_cable_tbl

    def get_hw_mux_cable_tbl_peer(self):
        return self.hw_mux_cable_tbl_peer

    def get_static_tbl(self):
        return self.static_tbl

    def get_grpc_config_tbl(self):
        return self.grpc_config_tbl


