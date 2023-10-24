"""
    SFF task manager
    Deterministic link bring-up task manager for SFF compliant modules, running
    as a thread inside xcvrd
"""

try:
    import copy
    import sys
    import threading
    import traceback

    from swsscommon import swsscommon

    from .xcvrd_utilities import port_mapping
    from .xcvrd_utilities.xcvr_table_helper import XcvrTableHelper
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


# Thread wrapper class for SFF compliant transceiver management


class SffManagerTask(threading.Thread):
    # Subscribe to below tables in Redis DB
    PORT_TBL_MAP = [
        {
            'CONFIG_DB': swsscommon.CFG_PORT_TABLE_NAME
        },
        {
            'STATE_DB': 'TRANSCEIVER_INFO',
            'FILTER': ['type']
        },
        {
            'STATE_DB': 'PORT_TABLE',
            'FILTER': ['host_tx_ready']
        },
    ]
    # Default number of channels for QSFP28/QSFP+ transceiver
    DEFAULT_NUM_CHANNELS = 4

    def __init__(self, namespaces, main_thread_stop_event, platform_chassis, helper_logger):
        threading.Thread.__init__(self)
        self.name = "SffManagerTask"
        self.exc = None
        self.task_stopping_event = threading.Event()
        self.main_thread_stop_event = main_thread_stop_event
        self.helper_logger = helper_logger
        self.platform_chassis = platform_chassis
        # port_dict holds data per port entry with logical_port_name as key, it
        # maintains local copy of the following DB fields:
        #   CONFIG_DB PORT_TABLE 'index', 'channel', 'admin_status'
        #   STATE_DB PORT_TABLE 'host_tx_ready'
        #   STATE_DB TRANSCEIVER_INFO 'type'
        # plus 'asic_id' from PortChangeEvent.asic_id (asic_id always gets
        # filled in handle_port_update_event function based on asic_context)
        # Its port entry will get deleted upon CONFIG_DB PORT_TABLE DEL.
        # Port entry's 'type' field will get deleted upon STATE_DB TRANSCEIVER_INFO DEL.
        self.port_dict = {}
        # port_dict snapshot captured in the previous event update loop
        self.port_dict_prev = {}
        self.xcvr_table_helper = XcvrTableHelper(namespaces)
        self.namespaces = namespaces

    def log_notice(self, message):
        self.helper_logger.log_notice("SFF: {}".format(message))

    def log_warning(self, message):
        self.helper_logger.log_warning("SFF: {}".format(message))

    def log_error(self, message):
        self.helper_logger.log_error("SFF: {}".format(message))

    def on_port_update_event(self, port_change_event):
        if (port_change_event.event_type
                not in [port_change_event.PORT_SET, port_change_event.PORT_DEL]):
            return

        lport = port_change_event.port_name
        pport = port_change_event.port_index
        asic_id = port_change_event.asic_id

        # Skip if it's not a physical port
        if not lport.startswith('Ethernet'):
            return

        # Skip if the physical index is not available
        if pport is None:
            return

        if port_change_event.port_dict is None:
            return

        if port_change_event.event_type == port_change_event.PORT_SET:
            if lport not in self.port_dict:
                self.port_dict[lport] = {}
            if pport >= 0:
                self.port_dict[lport]['index'] = pport
            # This field comes from CONFIG_DB PORT_TABLE. This is the channel
            # that blongs to this logical port, 0 means all channels. tx_disable
            # API needs to know which channels to disable/enable for a
            # particular physical port.
            if 'channel' in port_change_event.port_dict:
                self.port_dict[lport]['channel'] = port_change_event.port_dict['channel']
            # This field comes from STATE_DB PORT_TABLE
            if 'host_tx_ready' in port_change_event.port_dict:
                self.port_dict[lport]['host_tx_ready'] = \
                        port_change_event.port_dict['host_tx_ready']
            # This field comes from CONFIG_DB PORT_TABLE
            if 'admin_status' in port_change_event.port_dict and \
                port_change_event.db_name and \
                port_change_event.db_name == 'CONFIG_DB':
                # Only consider admin_status from CONFIG_DB.
                # Ignore admin_status from STATE_DB, which may have
                # different value.
                self.port_dict[lport]['admin_status'] = \
                        port_change_event.port_dict['admin_status']
            # This field comes from STATE_DB TRANSCEIVER_INFO table.
            # TRANSCEIVER_INFO has the same life cycle as a transceiver, if
            # transceiver is inserted/removed, TRANSCEIVER_INFO is also
            # created/deleted. Thus this filed can used to determine
            # insertion/removal event.
            if 'type' in port_change_event.port_dict:
                self.port_dict[lport]['type'] = port_change_event.port_dict['type']
            self.port_dict[lport]['asic_id'] = asic_id
        # CONFIG_DB PORT_TABLE DEL case:
        elif port_change_event.db_name and \
                port_change_event.db_name == 'CONFIG_DB':
            # Only when port is removed from CONFIG, we consider this entry as deleted.
            if lport in self.port_dict:
                del self.port_dict[lport]
        # STATE_DB TRANSCEIVER_INFO DEL case:
        elif port_change_event.table_name and \
                port_change_event.table_name == 'TRANSCEIVER_INFO':
            # TRANSCEIVER_INFO DEL corresponds to transceiver removal (not
            # port/interface removal), in this case, remove 'type' field from
            # self.port_dict
            if lport in self.port_dict and 'type' in self.port_dict[lport]:
                del self.port_dict[lport]['type']

    def get_host_tx_status(self, lport, asic_index):
        host_tx_ready = 'false'

        state_port_tbl = self.xcvr_table_helper.get_state_port_tbl(asic_index)

        found, port_info = state_port_tbl.get(lport)
        if found and 'host_tx_ready' in dict(port_info):
            host_tx_ready = dict(port_info)['host_tx_ready']
        return host_tx_ready

    def get_admin_status(self, lport, asic_index):
        admin_status = 'down'

        cfg_port_tbl = self.xcvr_table_helper.get_cfg_port_tbl(asic_index)

        found, port_info = cfg_port_tbl.get(lport)
        if found and 'admin_status' in dict(port_info):
            admin_status = dict(port_info)['admin_status']
        return admin_status

    def run(self):
        if self.platform_chassis is None:
            self.log_notice("Platform chassis is not available, stopping...")
            return

        try:
            self.task_worker()
        except Exception as e:
            self.helper_logger.log_error("Exception occured at {} thread due to {}".format(
                threading.current_thread().getName(), repr(e)))
            exc_type, exc_value, exc_traceback = sys.exc_info()
            msg = traceback.format_exception(exc_type, exc_value, exc_traceback)
            for tb_line in msg:
                for tb_line_split in tb_line.splitlines():
                    self.helper_logger.log_error(tb_line_split)
            self.exc = e
            self.main_thread_stop_event.set()

    def join(self):
        self.task_stopping_event.set()
        threading.Thread.join(self)
        if self.exc:
            raise self.exc

    def calculate_tx_disable_delta_array(self, cur_tx_disable_array, tx_disable_flag, channel):
        """
        Calculate the delta between the current transmitter (TX) disable array
        and a new TX disable flag for a specific channel. The function returns a
        new array (delta_array) where each entry indicates whether there's a
        difference for each corresponding channel in the TX disable array.

        Args:
            cur_tx_disable_array (list): Current array of TX disable flags for all channels.
            tx_disable_flag (bool): The new TX disable flag that needs to be compared
                                    with the current flags.
            channel (int): The specific channel that needs to be checked. If channel is 0, all
                                    channels are checked.

        Returns:
            list: A boolean array where each entry indicates whether there's a
                  change for the corresponding channel in the TX disable array.
                  True means there's a change, False means no change.
        """
        delta_array = []
        for i, cur_flag in enumerate(cur_tx_disable_array):
            is_different = (tx_disable_flag != cur_flag) if channel in [i + 1, 0] else False
            delta_array.append(is_different)
        return delta_array

    def convert_bool_array_to_bit_mask(self, bool_array):
        """
        Convert a boolean array into a bitmask. If a value in the boolean array
        is True, the corresponding bit in the bitmask is set to 1, otherwise
        it's set to 0. The function starts from the least significant bit for
        the first item in the boolean array.

        Args:
            bool_array (list): An array of boolean values.

        Returns:
            int: A bitmask corresponding to the input boolean array.
        """
        mask = 0
        for i, flag in enumerate(bool_array):
            mask += (1 << i if flag else 0)
        return mask

    def task_worker(self):
        '''
        The goal of sff_mgr is to make sure SFF compliant modules are brought up
        in a deterministc way, meaning TX is enabled only after host_tx_ready
        becomes True, and TX will be disabled when host_tx_ready becomes False.
        This will help eliminate link stability issue and potential interface
        flap, also turning off TX reduces the power consumption and avoid any
        lab hazard for admin shut interface.

        Platform can decide whether to enable sff_mgr via platform
        enable_sff_mgr flag. If enable_sff_mgr is False or not present, sff_mgr
        will not run. By default, it's disabled.

        There is a pre-requisite for the platforms that
        enable this sff_mgr feature: platform needs to keep TX in disabled state
        after module coming out-of-reset, in either module insertion or bootup
        cases. This is to make sure the module is not transmitting with TX
        enabled before host_tx_ready is True. No impact for the platforms in
        current deployment (since they don't enable it explictly.)

        '''

        # CONFIG updates, and STATE_DB for insertion/removal, and host_tx_ready change
        sel, asic_context = port_mapping.subscribe_port_update_event(self.namespaces, self,
                                                                     self.PORT_TBL_MAP)

        # This thread doesn't need to expilictly wait on PortInitDone and
        # PortConfigDone events, as xcvrd main thread waits on them before
        # spawrning this thread.
        while not self.task_stopping_event.is_set():
            # Internally, handle_port_update_event will block for up to
            # SELECT_TIMEOUT_MSECS until a message is received(in select
            # function). A message is received when there is a Redis SET/DEL
            # operation in the DB tables. Upon process restart, messages will be
            # replayed for all fields, no need to explictly query the DB tables
            # here.
            if not port_mapping.handle_port_update_event(
                    sel, asic_context, self.task_stopping_event, self, self.on_port_update_event):
                # In the case of no real update, go back to the beginning of the loop
                continue

            for lport in list(self.port_dict.keys()):
                if self.task_stopping_event.is_set():
                    break
                data = self.port_dict[lport]
                pport = int(data.get('index', '-1'))
                channel = int(data.get('channel', '0'))
                xcvr_type = data.get('type', None)
                xcvr_inserted = False
                host_tx_ready_changed = False
                admin_status_changed = False
                if pport < 0 or channel < 0:
                    continue

                if xcvr_type is None:
                    # TRANSCEIVER_INFO table's 'type' is not ready, meaning xcvr is not present
                    continue

                # Procced only for 100G/40G
                if not (xcvr_type.startswith('QSFP28') or xcvr_type.startswith('QSFP+')):
                    continue

                # Handle the case that host_tx_ready value in the local cache hasn't
                # been updated via PortChangeEvent:
                if 'host_tx_ready' not in data:
                    # Fetch host_tx_ready status from STATE_DB (if not present
                    # in DB, treat it as false), and update self.port_dict
                    data['host_tx_ready'] = self.get_host_tx_status(lport, data['asic_id'])
                    self.log_notice("{}: fetched DB and updated host_tx_ready={} locally".format(
                        lport, data['host_tx_ready']))
                # Handle the case that admin_status value in the local cache hasn't
                # been updated via PortChangeEvent:
                if 'admin_status' not in data:
                    # Fetch admin_status from CONFIG_DB (if not present in DB,
                    # treat it as false), and update self.port_dict
                    data['admin_status'] = self.get_admin_status(lport, data['asic_id'])
                    self.log_notice("{}: fetched DB and updated admin_status={} locally".format(
                        lport, data['admin_status']))

                # Check if there's a diff between current and previous 'type'
                # It's a xcvr insertion case if TRANSCEIVER_INFO 'type' doesn't exist
                # in previous port_dict sanpshot
                if lport not in self.port_dict_prev or 'type' not in self.port_dict_prev[lport]:
                    xcvr_inserted = True
                # Check if there's a diff between current and previous host_tx_ready
                if (lport not in self.port_dict_prev or
                        'host_tx_ready' not in self.port_dict_prev[lport] or
                        self.port_dict_prev[lport]['host_tx_ready'] != data['host_tx_ready']):
                    host_tx_ready_changed = True
                # Check if there's a diff between current and previous admin_status
                if (lport not in self.port_dict_prev or
                    'admin_status' not in self.port_dict_prev[lport] or
                    self.port_dict_prev[lport]['admin_status'] != data['admin_status']):
                    admin_status_changed = True
                # Skip if neither of below cases happens:
                # 1) xcvr insertion
                # 2) host_tx_ready getting changed
                # 3) admin_status getting changed
                # In addition to handle_port_update_event()'s internal filter,
                # this check serves as additional filter to ignore irrelevant
                # event, such as CONFIG_DB change other than admin_status field.
                if ((not xcvr_inserted) and
                    (not host_tx_ready_changed) and
                    (not admin_status_changed)):
                    continue
                self.log_notice(("{}: xcvr=present(inserted={}), "
                                 "host_tx_ready={}(changed={}), "
                                 "admin_status={}(changed={})").format(
                    lport,
                    xcvr_inserted,
                    data['host_tx_ready'], host_tx_ready_changed,
                    data['admin_status'], admin_status_changed))

                # double-check the HW presence before moving forward
                sfp = self.platform_chassis.get_sfp(pport)
                if not sfp.get_presence():
                    self.log_error("{}: module not present!".format(lport))
                    del self.port_dict[lport]
                    continue
                try:
                    # Skip if XcvrApi is not supported
                    api = sfp.get_xcvr_api()
                    if api is None:
                        self.log_error(
                            "{}: skipping sff_mgr since no xcvr api!".format(lport))
                        continue

                    # Skip if it's not a paged memory device
                    if api.is_flat_memory():
                        self.log_notice(
                            "{}: skipping sff_mgr for flat memory xcvr".format(lport))
                        continue

                    # Skip if it's a copper cable
                    if api.is_copper():
                        self.log_notice(
                            "{}: skipping sff_mgr for copper cable".format(lport))
                        continue

                    # Skip if tx_disable action is not supported for this xcvr
                    if not api.get_tx_disable_support():
                        self.log_notice(
                            "{}: skipping sff_mgr due to tx_disable not supported".format(
                                lport))
                        continue
                except (AttributeError, NotImplementedError):
                    # Skip if these essential routines are not available
                    continue

                # Only turn on TX if both host_tx_ready is true and admin_status is up
                target_tx_disable_flag = not (data['host_tx_ready'] == 'true'
                                              and data['admin_status'] == 'up')
                # get_tx_disable API returns an array of bool, with tx_disable flag on each channel.
                # True means tx disabled; False means tx enabled.
                cur_tx_disable_array = api.get_tx_disable()
                if cur_tx_disable_array is None:
                    self.log_error("{}: Failed to get current tx_disable value".format(lport))
                    # If reading current tx_disable/enable value failed (could be due to
                    # read error), then set this variable to the opposite value of
                    # target_tx_disable_flag, to let detla array to be True on
                    # all the interested channels, to try best-effort TX disable/enable.
                    cur_tx_disable_array = [not target_tx_disable_flag] * self.DEFAULT_NUM_CHANNELS
                # Get an array of bool, where it's True only on the channels that need change.
                delta_array = self.calculate_tx_disable_delta_array(cur_tx_disable_array,
                                                                    target_tx_disable_flag, channel)
                mask = self.convert_bool_array_to_bit_mask(delta_array)
                if mask == 0:
                    self.log_notice("{}: No change is needed for tx_disable value".format(lport))
                    continue
                if api.tx_disable_channel(mask, target_tx_disable_flag):
                    self.log_notice("{}: TX was {} with channel mask: {}".format(
                        lport, "disabled" if target_tx_disable_flag else "enabled", bin(mask)))
                else:
                    self.log_error("{}: Failed to {} TX with channel mask: {}".format(
                        lport, "disable" if target_tx_disable_flag else "enable", bin(mask)))

            # Take a snapshot for port_dict, this will be used to calculate diff
            # later in the while loop to determine if there's really a value
            # change on the fields related to the events we care about.
            self.port_dict_prev = copy.deepcopy(self.port_dict)