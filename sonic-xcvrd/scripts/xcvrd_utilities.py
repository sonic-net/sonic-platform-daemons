"""
    xcvrd_utilities.py
    helper utlities configuring y_cable for xcvrd daemon
"""


try:
    import threading

    from sonic_py_common import daemon_base, logger
    from swsscommon import swsscommon
    from sonic_py_common import multi_asic
    from sonic_y_cable import y_cable
except ImportError, e:
    raise ImportError (str(e) + " - required module not found")


platform_sfputil = None

# Find out the underneath physical port list by logical name
def logical_port_name_to_physical_port_list(port_name):
    if port_name.startswith("Ethernet"):
        if platform_sfputil.is_logical_port(port_name):
            return platform_sfputil.get_logical_to_physical(port_name)
        else:
            helper_logger.log_error("Invalid port '%s'" % port_name)
            return None
    else:
        return [int(port_name)]

# Delete port from Y cable status table
def delete_port_from_y_cable_table(logical_port_name, y_cable_tbl):
    y_cable_tbl._del(logical_port_name)


def update_tor_active_side(read_side, status, logical_port_name):
    physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)

    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        if read_side == 1:
            if status == "ACTIVE":
                y_cable.toggle_mux_to_torA(physical_port)
            elif status == "STANDBY":
                y_cable.toggle_mux_to_torB(physical_port)
        elif read_side == 2:
            if status == "ACTIVE":
                y_cable.toggle_mux_to_torB(physical_port)
            elif status == "STANDBY":
                y_cable.toggle_mux_to_torA(physical_port)

        #Now that mux has been toggled check to see if 
        #mux has indeed been toggled
        # might not be neccessary
        #active_side = y_cable.check_active_linked_tor_side(physical_port)

    else:
        '''
           Y cable ports should always have 
           one to one mapping of physical-to-logical
           This should not happen'''
        logger.log_warning("Error: Retreived multiple ports for a Y cable table".format(logical_port_name))


def update_port_mux_status_table(logical_port_name, mux_config_tbl):
    physical_port_list = logical_port_name_to_physical_port_list(logical_port_name)

    if len(physical_port_list) == 1:

        physical_port = physical_port_list[0]
        read_side = y_cable.check_read_side(physical_port)
        active_side = y_cable.check_active_linked_tor_side(physical_port)
        if read_side == active_side:
            status = 'ACTIVE'
        elif active_side == 0:
            status = 'INACTIVE'
        else: 
            status = 'STANDBY'

        fvs = swsscommon.FieldValuePairs([('status', status),
                                          ('read_side', str(read_side)),
                                          ('active_side',str(active_side))])
        mux_config_tbl.set(logical_port_name, fvs)
    else:
        '''
           Y cable ports should always have 
           one to one mapping of physical-to-logical
           This should not happen'''
        logger.log_warning("Error: Retreived multiple ports for a Y cable table".format(logical_port_name))

def init_ports_status_for_y_cable(platform_sfp, stop_event=threading.Event()):
    global platform_sfputil
    # Connect to CONFIG_DB and create port status table inside state_db
    config_db, state_db, port_tbl , y_cable_tbl= {}, {}, {}, {}
    port_table_keys = {}
    state_db_created = False
    platform_sfputil = platform_sfp

    # Get the namespaces in the platform
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        config_db[asic_id] = daemon_base.db_connect("CONFIG_DB", namespace)
        port_tbl[asic_id] = swsscommon.Table(config_db[asic_id], "PORT")
        port_table_keys[asic_id] = config_db[asic_id].get_keys("PORT")

    # Init PORT_STATUS table if ports are on Y cable
    logical_port_list = platform_sfputil.logical
    for logical_port_name in logical_port_list:
        if stop_event.is_set():
            break

        # Get the asic to which this port belongs
        asic_index = platform_sfputil.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
            continue

        if logical_port_name in port_table_keys[asic_index]:
            (status, fvs) = port_tbl[asic_index].get(logical_port_name)
            if status is False:
                logger.log_warning("Could not retreive fieldvalue pairs for {}, inside config_db".format(logical_port_name))
                continue

            else:
                # Convert list of tuples to a dictionary
                mux_table_dict = dict(fvp)
                if "mux_cable" in mux_table_dict:
                    if state_db_created:
                        #fill in the newly found entry
                        update_port_mux_status_table(logical_port_name,y_cable_tbl[asic_index])

                    else:
                        #first create the db and then fill in the entry
                        state_db_created = True
                        namespaces = multi_asic.get_front_end_namespaces()
                        for namespace in namespaces:
                            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
                            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
                            y_cable_tbl[asic_id] = swsscommon.Table(state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
                        # fill the newly found entry    
                        update_port_mux_status_table(logical_port_name,y_cable_tbl[asic_index])
                else:
                    logger.log_info("Port is not connected on a Y cable")

        else:
            ''' This port does not exist in Port table of config but is present inside
                logical_ports after loading the port_mappings from port_config_file
                This should not happen
            '''
            logger.log_warning("Could not retreive port inside config_db PORT table ".format(logical_port_name))

    return state_db_created                


def delete_ports_status_for_y_cable():

    state_db, port_tbl , y_cable_tbl= {}, {}, {}
    y_cable_tbl_keys = {}
    namespaces = multi_asic.get_front_end_namespaces()
    for namespace in namespaces:
        asic_id = multi_asic.get_asic_index_from_namespace(namespace)
        state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
        y_cable_tbl[asic_id] = swsscommon.Table(state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
        y_cable_tbl_keys[asic_id] = state_db[asic_id].get_keys(swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)

    # delete PORTS on Y cable table if ports on Y cable
    logical_port_list = platform_sfputil.logical
    for logical_port_name in logical_port_list:

        # Get the asic to which this port belongs
        asic_index = platform_sfputil.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))

        if logical_port_name in y_cable_tbl_keys[asic_index]:
            delete_port_from_y_cable_table(physical_port, y_cable_tbl[asic_index])


# Thread wrapper class to update y_cable status periodically
class YCableTableUpdateTask(object):
    def __init__(self):
        self.task_thread = None

        # Load the namespace details first from the database_global.json file.
        swsscommon.SonicDBConfig.initializeGlobalConfig()

    def task_worker(self):

        # Connect to STATE_DB and create transceiver dom info table
        app_db, state_db, status_tbl,y_cable_tbl = {}, {}, {},{}
        y_cable_tbl_keys = {}

        # Get the namespaces in the platform
        sel = swsscommon.Select()

        #logical_port_list = platform_sfputil.logical
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            # Open a handle to the Application database, in all namespaces
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            app_db[asic_id] = daemon_base.db_connect("APPL_DB", namespace)
            status_tbl[asic_id] = swsscommon.SubscriberStateTable(appl_db[asic_id], swsscommon.APP_HW_MUX_CABLE_TABLE_NAME)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            y_cable_tbl[asic_id] = swsscommon.Table(state_db[asic_id], swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)
            sel.addSelectable(status_tbl[asic_id])


        # Listen indefinitely for changes to the APP_MUX_CABLE_TABLE in the Application DB's
        while True:
            # Use timeout to prevent ignoring the signals we want to handle
            # in signal_handler() (e.g. SIGTERM for graceful shutdown)
            (state, selectableObj) = sel.select(SELECT_TIMEOUT)

            if state == swsscommon.Select.TIMEOUT:
                # Do not flood log when select times out
                continue
            if state != swsscommon.Select.OBJECT:
                self.log_warning("sel.select() did not  return swsscommon.Select.OBJECT")
                continue

            # Get the redisselect object  from selectable object
            redisSelectObj = swsscommon.CastSelectableToRedisSelectObj(selectableObj)
            # Get the corresponding namespace from redisselect db connector object
            namespace = redisSelectObj.getDbConnector().getNamespace()
            asic_index = multi_asic.get_asic_index_from_namespace(namespace)
            y_cable_tbl_keys[asic_id] = state_db[asic_index].get_keys(swsscommon.STATE_HW_MUX_CABLE_TABLE_NAME)

            (port, op, fvp) = status_tbl[asic_id].pop()
            if fvp:
                #Might need to check the presence of this Port
                #in logical_port_list but keep for now for coherency
                if port not in y_cable_table_keys[asic_id]:
                    continue

                fvp_dict = dict(fvp)

                if op == "status" in fvp_dict:
                    #got a status change
                    new_status = fvp_dict["status"]
                    (status, fvs) = y_cable_tbl[asic_index].get(port)
                    if status is False:
                        logger.log_warning("Could not retreive fieldvalue pairs for {}, inside config_db".format(logical_port_name))
                        continue
                    mux_port_dict = dict(fvs)
                    old_status = mux_port_dict.get("status") 
                    read_side = mux_port_dict.get("read_side")
                    active_side = mux_port_dict.get("active_side")
                    if old_status != new_staus:
                        update_tor_active_side(read_side, new_status, port)
                        fvs_updated = swsscommon.FieldValuePairs([('status', new_status),
                                                          ('read_side', read_side),
                                                      ('active_side',active_side)])
                        mux_config_tbl.set(logical_port_name, fvs_updated)
                        #nothing to do since no status change
                    else:
                        logger.log_warning("Got a change event on _MUX_TABLE that does not update the current status".format(logical_port_name))


    def task_stop(self):
        self.task_thread.join()

