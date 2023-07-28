#!/usr/bin/env python3

"""
    Performance Monitoring statistics manager 
"""

try:
    import ast
    import copy
    import sys
    import threading
    import time
    import traceback
    import sched

    from swsscommon import swsscommon
    from .xcvrd_utilities import sfp_status_helper
    from .xcvrd_utilities.xcvr_table_helper import XcvrTableHelper
    from .xcvrd_utilities import port_mapping
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

#
# Constants ====================================================================
#

PM_INFO_UPDATE_PERIOD_SECS = 60

MAX_NUM_60SEC_WINDOW = 15
MAX_NUM_15MIN_WINDOW = 12
MAX_NUM_24HRS_WINDOW = 2

WINDOW_60SEC_START_NUM = 1
WINDOW_15MIN_START_NUM = 16
WINDOW_24HRS_START_NUM = 28

WINDOW_60SEC_END_NUM = 15
WINDOW_15MIN_END_NUM = 27
WINDOW_24HRS_END_NUM = 29

TIME_60SEC_IN_SECS = 60 
TIME_15MIN_IN_SECS = 900
TIME_24HRS_IN_SECS = 86400 

class PmUpdateTask(threading.Thread):

    # Subscribe to below table in Redis DB
    PORT_TBL_MAP = [
        {'STATE_DB': 'TRANSCEIVER_INFO', 'FILTER': ['media_interface_code']},
    ]

    def __init__(self, namespaces, port_mapping_data, main_thread_stop_event, platform_chassis, helper_logger, process_start_option, pm_interval):
        threading.Thread.__init__(self)
        self.name = "PmUpdateTask"
        self.exc = None
        self.task_stopping_event = threading.Event()
        self.main_thread_stop_event = main_thread_stop_event
        self.port_mapping_data = copy.deepcopy(port_mapping_data)
        self.namespaces = namespaces
        self.pm_interval = pm_interval 
        self.helper_logger = helper_logger
        self.platform_chassis = platform_chassis
        self.process_start_option = process_start_option
        self.xcvr_table_helper = XcvrTableHelper(namespaces)
        #Port numbers that gets iterated for PM update.
        self.pm_port_list = []
    
    def log_notice(self, message):
        self.helper_logger.log_notice("PM: {}".format(message))

    def log_warning(self, message):
        self.helper_logger.log_warning("PM: {}".format(message))

    def log_error(self, message):
        self.helper_logger.log_error("PM: {}".format(message))

    def get_transceiver_pm(self, physical_port):
        if self.platform_chassis is not None:
            try:
                return self.platform_chassis.get_sfp(physical_port).get_transceiver_pm()
            except NotImplementedError:
                pass
            return {}

    def get_port_admin_status(self, lport):
        admin_status = 'down'

        asic_index = self.port_mapping_data.get_asic_id_for_logical_port(lport)
        cfg_port_tbl = self.xcvr_table_helper.get_cfg_port_tbl(asic_index)

        found, port_info = cfg_port_tbl.get(lport)
        if found:
            admin_status = dict(port_info).get('admin_status', 'down')
        return admin_status

    def on_port_update_event(self, port_change_event):
        """Invoked when there is a change in TRANSCIEVER_INFO for the port.
           Initialize the PM window slots to default when the TRANSCIEVER_INFO table is created.
           Delete the PM window slots when the TRANSCIEVER_INFO table is deleted.

        Args:
            port_change_event (object): port change event
        """
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
           
        physical_port = self.port_mapping_data.get_logical_to_physical(lport)[0]
        self.log_notice("PM port event type:{} for lport: {} table name: {}".format(port_change_event.event_type, lport, port_change_event.table_name))
        if port_change_event.event_type == port_change_event.PORT_SET:
            if (True if "ZR" not in str(port_change_event.port_dict.get('media_interface_code', None)) else False):
                return
            
            if not self.platform_chassis.get_sfp(physical_port).get_presence():
                self.log_error("Change in TRANSCIEVER_INFO table for the port {}, but transciever is not present".format(lport))
                return
            
            #update the port list
            if lport not in self.pm_port_list:
                self.pm_port_list.append(lport)
            else:
                self.log_notice("Port number:{} already present in port list :: Spurious request\n".format(lport))
                return
            pm_win_dict = {}
            # Set the TRANSCEIVER_PM_WINDOW_STATS keys for the port with default value
            for key in ["window{}".format(win_num) for win_num in range(WINDOW_60SEC_START_NUM, WINDOW_24HRS_END_NUM + 1)]:
                pm_win_dict[key] = "N/A" 
        
            #Update the table in DB    
            state_pm_win_stats_tbl = self.xcvr_table_helper.get_pm_window_stats_tbl(asic_id)
            fvs = swsscommon.FieldValuePairs([(k, v) for k, v in pm_win_dict.items()])
            state_pm_win_stats_tbl.set(lport, fvs)
            self.log_notice("Port add event:{} added to pm port list".format(port_change_event.port_name))
        elif port_change_event.event_type == port_change_event.PORT_DEL and port_change_event.table_name == 'TRANSCEIVER_INFO':

            if self.platform_chassis.get_sfp(physical_port).get_presence():
                self.log_error("TRANSCIEVER_INFO table for the port {} is deleted but transciever is present".format(lport))
                return
            #Remove the port from port list and delete the TRANSCEIVER_PM_WINDOW_STATS table for the port 
            self.pm_port_list.remove(lport)
            state_pm_win_stats_tbl = self.xcvr_table_helper.get_pm_window_stats_tbl(asic_id)
            if state_pm_win_stats_tbl:
                state_pm_win_stats_tbl._del(lport)
                self.log_notice("Port delete event:{} deleted from pm port list".format(port_change_event.port_name))

    # Update the PM statistics in string format to respective window key to 
    # TRANSCEIVER_PM_WINDOW_STATS_TABLE in state DB.
    def set_pm_stats_in_pm_win_stats_tbl(self, asic, lport, window, pm_data_str):
        state_pm_win_stats_tbl = self.xcvr_table_helper.get_pm_window_stats_tbl(asic)
        fvs = swsscommon.FieldValuePairs([(window, str(pm_data_str))])
        state_pm_win_stats_tbl.set(lport, fvs)

    # Retrieve the PM statistics from respective window key from.
    # TRANSCEIVER_PM_WINDOW_STATS_TABLE - state DB.
    def get_pm_stats_in_pm_win_stats_tbl(self, asic, lport, window):
        state_pm_win_stats_tbl = self.xcvr_table_helper.get_pm_window_stats_tbl(asic)
        status, fvs = state_pm_win_stats_tbl.get(str(lport))
        stats_tbl_tuple = dict((k, v) for k, v in fvs)
        if stats_tbl_tuple != "":
            return (stats_tbl_tuple.get(window, ""))
        else:
            return None

    # 
    # input args: asic index, logical port, start and end window number of a PM window granularity, 
    # PM window granularity in secs, pm stats read from module.
    #
    # Algorithm to sample and update the PM stats to the appropriate PM window in TRANSCEIVER_PM_WINDOW_STATS_TABLE.
    def pm_window_update_to_DB(self, asic, lport, start_window, end_window, pm_win_itrvl_in_secs, pm_hw_data):
        window_num = start_window
        while window_num < (end_window + 1):
            #Retrieve PM data from DB
            pm_data = {}
            pm_data = self.get_pm_stats_in_pm_win_stats_tbl(lport, asic, "window"+str(window_num))
            if pm_data == 'N/A' and window_num == start_window:
                pm_data_dict = {} 
                for key, value in pm_hw_data.items():
                    pm_data_dict[key] = value
                #First PM data read from the module 
                pm_data_dict['pm_win_start_time'] = pm_hw_data.get('pm_win_end_time')
                pm_data_dict['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time')
                pm_data_dict['pm_win_current'] = 'true'
                pm_data_str = str(pm_data_dict) 
                self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(window_num), pm_data_str)
                break;
            elif pm_data != 'N/A':
                #retrieve pm_win_current index pm data from DB-the start of PM time window slot
                pm_data_dic = ast.literal_eval(pm_data)
                
                if (float(pm_data_dic.get('pm_win_end_time')) == float(pm_data_dic.get('pm_win_start_time'))):
                    #discard the 1st set of PM data retrieved from the module.
                    pm_data_dic1 = {} 
                    for key, value in pm_hw_data.items():
                        pm_data_dic1[key] = value
                    #First PM data read from the module 
                    pm_data_dic1['pm_win_start_time'] = pm_data_dic.get('pm_win_start_time')
                    pm_data_dic1['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time')
                    pm_data_dic1['pm_win_current'] = 'true'
                    pm_data_str = str(pm_data_dic1) 
                    self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(window_num), pm_data_str)
                    break

                if (float(pm_data_dic.get('pm_win_end_time')) - float(pm_data_dic.get('pm_win_start_time'))) < pm_win_itrvl_in_secs:
                    sampled_pm_data = {}
                    #Sample the data between pm_data and pm_hw_data.
                    sampled_pm_data = self.pm_data_sampling(pm_data_dic, pm_hw_data)
                    sampled_pm_data['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time')
                    sampled_pm_data['pm_win_start_time'] = pm_data_dic.get('pm_win_start_time')
                    sampled_pm_data['pm_win_current'] = pm_data_dic.get('pm_win_current')
                    #update the window with sampled data in DB.
                    self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(window_num), sampled_pm_data)
                    break

                #set current to false in current pm window as this time window is completed.
                pm_data_dic['pm_win_current'] = "false"
                #update the current field in the current window_num key of pm_data
                self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(window_num), pm_data_dic)

                #retrieve next pm window number and its data
                if window_num == end_window:
                    next_window_num = start_window 
                else:
                    next_window_num = window_num + 1
                next_pm_data = {}
                next_pm_data_dic = {}
                next_pm_data = self.get_pm_stats_in_pm_win_stats_tbl(lport, asic, "window"+str(next_window_num))
                # If next window pm_data is empty, then fill the next window with pm_hw_data and 
                # set the curremt, start and end time, 
                # update the pm_win_current to false in current_pm_data and write to DB
                if next_pm_data == 'N/A':
                    # fill the hw data to the next window
                    for key, value in pm_hw_data.items():
                        next_pm_data_dic[key] = value
                    
                    next_pm_data_dic['pm_win_current'] = "true"
                    next_pm_data_dic['pm_win_start_time'] = pm_data_dic.get('pm_win_end_time')
                    next_pm_data_dic['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time') 
                    #convert to string and update current value of next pm data to DB
                    self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(next_window_num), str(next_pm_data_dic))
                    break;     
                else:
                    #next window pm data is not empty, check which window is current and udpate next window
                    next_pm_data_dic = ast.literal_eval(next_pm_data)

                    if (float(next_pm_data_dic.get('pm_win_end_time')) == float(next_pm_data_dic.get('pm_win_start_time'))):
                         #discard the 1st set of PM data retrieved from the module.
                         next_pm_data_dic1 = {}
                         next_pm_data_dic1_str =""
                         for key, value in pm_hw_data.items():
                             next_pm_data_dic1[key] = value
                         #First PM data read from the module 
                         next_pm_data_dic1['pm_win_start_time'] = next_pm_data.get('pm_win_start_time')
                         next_pm_data_dic1['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time')
                         next_pm_data_dic1['pm_win_current'] = 'true'
                         next_pm_data_str = str(next_pm_data_dic1) 
                         self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(next_window_num), next_pm_data_str)
                         break


                    if (float(next_pm_data_dic.get('pm_win_end_time')) - float(next_pm_data_dic.get('pm_win_start_time'))) < pm_win_itrvl_in_secs:
                        #Sample the data between pm_data and pm_hw_data.
                        sampled_pm_data = self.pm_data_sampling(next_pm_data_dic, pm_hw_data)
                        sampled_pm_data['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time')
                        sampled_pm_data['pm_win_start_time'] = pm_data_dic.get('pm_win_end_time')
                        sampled_pm_data['pm_win_current'] = next_pm_data_dic.get('pm_win_current')
                        #update the window with sampled data in DB.
                        self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(next_window_num), str(sampled_pm_data))
                        break
                    else: 
                        if float(pm_data_dic.get('pm_win_start_time')) < float(next_pm_data_dic.get('pm_win_start_time')):
                            window_num = window_num + 1
                            continue
                        else:
                            #Fill the pm_hw_data into DB with key:window+1, with start and end time updated .
                            next_pm_data_dic = {} 
                            for key, value in pm_hw_data.items():
                                next_pm_data_dic[key] = value
                        
                            next_pm_data_dic['pm_win_current'] = "true"
                            next_pm_data_dic['pm_win_start_time'] = pm_data_dic.get('pm_win_end_time')
                            next_pm_data_dic['pm_win_end_time'] = pm_hw_data.get('pm_win_end_time') 
                            #convert to string and update current value of prev pm data to DB
                            self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(next_window_num), next_pm_data_dic)
                            #pm_win_stat_from_db["window{}".format(next_window_num)] = str(next_pm_data_dic)
                        
                            #update the current field in the current window_num key of pm_data
                            pm_data_dic['pm_win_current'] = 'false'
                            self.set_pm_stats_in_pm_win_stats_tbl(lport, asic, "window{}".format(window_num), str(pm_data_dic))
                            break;     


    def average_of_two_val(self, val1, val2):
        return((val1+val2)/2)
    
    # perform min, max and average of relevant PM stats parameter between two dictionary
    def pm_data_sampling(self, pm_data_dict1, pm_data_dict2):
        sampled_pm_dict = {}
        try:
            sampled_pm_dict['prefec_ber_avg'] = self.average_of_two_val(float(pm_data_dict1['prefec_ber_avg']), float(pm_data_dict2['prefec_ber_avg']))
            sampled_pm_dict['prefec_ber_min'] = min(float(pm_data_dict1['prefec_ber_min']), float(pm_data_dict2['prefec_ber_min']))
            sampled_pm_dict['prefec_ber_max'] = max(float(pm_data_dict1['prefec_ber_max']), float(pm_data_dict2['prefec_ber_max']))
         
            sampled_pm_dict['uncorr_frames_avg'] = self.average_of_two_val(float(pm_data_dict1['uncorr_frames_avg']), float(pm_data_dict2['uncorr_frames_avg']))
            sampled_pm_dict['uncorr_frames_min'] = min(float(pm_data_dict1['uncorr_frames_min']), float(pm_data_dict2['uncorr_frames_min']))
            sampled_pm_dict['uncorr_frames_max'] = max(float(pm_data_dict1['uncorr_frames_max']), float(pm_data_dict2['uncorr_frames_max']))
         
            sampled_pm_dict['cd_avg'] = self.average_of_two_val(float(pm_data_dict1['cd_avg']), float(pm_data_dict2['cd_avg']))
            sampled_pm_dict['cd_min'] = min(float(pm_data_dict1['cd_min']), float(pm_data_dict2['cd_min']))
            sampled_pm_dict['cd_max'] = max(float(pm_data_dict1['cd_max']), float(pm_data_dict2['cd_max']))
         
            sampled_pm_dict['dgd_avg']   = self.average_of_two_val(float(pm_data_dict1['dgd_avg']), float(pm_data_dict2['dgd_avg']))
            sampled_pm_dict['dgd_min']   = min(float(pm_data_dict1['dgd_min']), float(pm_data_dict2['dgd_min']))
            sampled_pm_dict['dgd_max']   = max(float(pm_data_dict1['dgd_max']), float(pm_data_dict2['dgd_max']))
         
            sampled_pm_dict['sopmd_avg'] = self.average_of_two_val(float(pm_data_dict1['sopmd_avg']), float(pm_data_dict2['sopmd_avg']))
            sampled_pm_dict['sopmd_min'] = min(float(pm_data_dict1['sopmd_min']), float(pm_data_dict2['sopmd_min']))
            sampled_pm_dict['sopmd_max'] = max(float(pm_data_dict1['sopmd_max']), float(pm_data_dict2['sopmd_max']))
         
            sampled_pm_dict['pdl_avg']   = self.average_of_two_val(float(pm_data_dict1['pdl_avg']), float(pm_data_dict2['pdl_avg']))
            sampled_pm_dict['pdl_min']   = min(float(pm_data_dict1['pdl_min']), float(pm_data_dict2['pdl_min']))
            sampled_pm_dict['pdl_max']   = max(float(pm_data_dict1['pdl_max']), float(pm_data_dict2['pdl_max']))
         
            sampled_pm_dict['osnr_avg']  = self.average_of_two_val(float(pm_data_dict1['osnr_avg']), float(pm_data_dict2['osnr_avg']))
            sampled_pm_dict['osnr_min']  = min(float(pm_data_dict1['osnr_min']), float(pm_data_dict2['osnr_min']))
            sampled_pm_dict['osnr_max']  = max(float(pm_data_dict1['osnr_max']), float(pm_data_dict2['osnr_max']))
         
            sampled_pm_dict['esnr_avg']  = self.average_of_two_val(float(pm_data_dict1['esnr_avg']), float(pm_data_dict2['esnr_avg']))
            sampled_pm_dict['esnr_min']  = min(float(pm_data_dict1['esnr_min']), float(pm_data_dict2['esnr_min']))
            sampled_pm_dict['esnr_max']  = max(float(pm_data_dict1['esnr_max']), float(pm_data_dict2['esnr_max']))
         
            sampled_pm_dict['cfo_avg']   = self.average_of_two_val(float(pm_data_dict1['cfo_avg']), float(pm_data_dict2['cfo_avg']))
            sampled_pm_dict['cfo_min']   = min(float(pm_data_dict1['cfo_min']), float(pm_data_dict2['cfo_min']))
            sampled_pm_dict['cfo_max']   = max(float(pm_data_dict1['cfo_max']), float(pm_data_dict2['cfo_max']))
         
            sampled_pm_dict['evm_avg']   = self.average_of_two_val(float(pm_data_dict1['evm_avg']), float(pm_data_dict2['evm_avg']))
            sampled_pm_dict['evm_min']   = min(float(pm_data_dict1['evm_min']), float(pm_data_dict2['evm_min']))
            sampled_pm_dict['evm_max']   = max(float(pm_data_dict1['evm_max']), float(pm_data_dict2['evm_max']))
         
            sampled_pm_dict['soproc_avg'] = self.average_of_two_val(float(pm_data_dict1['soproc_avg']), float(pm_data_dict2['soproc_avg']))
            sampled_pm_dict['soproc_min'] = min(float(pm_data_dict1['soproc_min']), float(pm_data_dict2['soproc_min']))
            sampled_pm_dict['soproc_max'] = max(float(pm_data_dict1['soproc_max']), float(pm_data_dict2['soproc_max']))
         
            sampled_pm_dict['tx_power_avg']  = self.average_of_two_val(float(pm_data_dict1['tx_power_avg']), float(pm_data_dict2['tx_power_avg']))
            sampled_pm_dict['tx_power_min']  = min(float(pm_data_dict1['tx_power_min']), float(pm_data_dict2['tx_power_min']))
            sampled_pm_dict['tx_power_max']  = max(float(pm_data_dict1['tx_power_max']), float(pm_data_dict2['tx_power_max']))
         
            sampled_pm_dict['rx_tot_power_avg']  = self.average_of_two_val(float(pm_data_dict1['rx_tot_power_avg']), float(pm_data_dict2['rx_tot_power_avg']))
            sampled_pm_dict['rx_tot_power_min']  = min(float(pm_data_dict1['rx_tot_power_min']), float(pm_data_dict2['rx_tot_power_min']))
            sampled_pm_dict['rx_tot_power_max']  = max(float(pm_data_dict1['rx_tot_power_max']), float(pm_data_dict2['rx_tot_power_max']))
         
            sampled_pm_dict['rx_sig_power_avg'] = self.average_of_two_val(float(pm_data_dict1['rx_sig_power_avg']), float(pm_data_dict2['rx_sig_power_avg']))
            sampled_pm_dict['rx_sig_power_min'] =  min(float(pm_data_dict1['rx_sig_power_min']), float(pm_data_dict2['rx_sig_power_min']))
            sampled_pm_dict['rx_sig_power_max'] =  max(float(pm_data_dict1['rx_sig_power_max']), float(pm_data_dict2['rx_sig_power_max']))
        except ValueError as e:
            self.log_error("Value Error: {}".format(e))
        return sampled_pm_dict

    def is_current_window(self, pm_data_dict):
        return (True if (pm_data_dic.get('pm_win_current', false) == 'true') else False)

    def beautify_pm_info_dict(self, pm_info_dict, physical_port):
        for k, v in pm_info_dict.items():
            if type(v) is str:
                continue
            pm_info_dict[k] = str(v)

    def task_worker(self):
        self.log_notice("Start Performance monitoring for 400G ZR modules")
        sel, asic_context = port_mapping.subscribe_port_update_event(self.namespaces, self, self.PORT_TBL_MAP)
        #Schedule the PM 
        self.scheduler = sched.scheduler(time.time, time.sleep)
        self.scheduler.enter(self.pm_interval, 1, self.pm_update_task_worker) 

        #Run all scheduled events and handle port events
        while not self.task_stopping_event.is_set():
            port_mapping.handle_port_update_event(sel, asic_context, self.task_stopping_event, self.helper_logger, self.on_port_update_event)
            self.scheduler.run(blocking=False)

    def pm_update_task_worker(self):
       
        # Schedule the next run with pm_interval
        self.scheduler.enter(self.pm_interval, 1, self.pm_update_task_worker) 
        #Iterate the ZR-400G port list for pm window update
        for lport in self.pm_port_list:
            asic_index = self.port_mapping_data.get_asic_id_for_logical_port(lport)
            if asic_index is None:
                continue
            
            physical_port = self.port_mapping_data.get_logical_to_physical(lport)[0]
            if not physical_port:
                continue

            #skip if sfp obj or sfp is not present
            sfp = self.platform_chassis.get_sfp(physical_port)
            if sfp is not None:
                if not sfp.get_presence():
                    continue
            else:
                continue

            if self.get_port_admin_status(lport) != 'up':
                continue

            if not sfp_status_helper.detect_port_in_error_status(lport, self.xcvr_table_helper.get_status_tbl(asic_index)):
                try:
                    pm_hw_data = self.get_transceiver_pm(physical_port)
                except (KeyError, TypeError) as e:
                    #continue to process next port since execption could be raised due to port reset, transceiver removal
                    self.log_warning("Got exception {} while reading pm stats for port {}, ignored".format(repr(e), lport))
                    continue

            if not pm_hw_data:
                continue

            self.beautify_pm_info_dict(pm_hw_data, physical_port)

            # Update 60Sec PM time window slots
            start_window = WINDOW_60SEC_START_NUM
            end_window = WINDOW_60SEC_END_NUM 
            pm_win_itrvl_in_secs = TIME_60SEC_IN_SECS
            self.pm_window_update_to_DB(lport, asic_index, start_window, end_window, pm_win_itrvl_in_secs, pm_hw_data)

            # Update 15min PM time window slots
            start_window = WINDOW_15MIN_START_NUM
            end_window = WINDOW_15MIN_END_NUM 
            pm_win_itrvl_in_secs = TIME_15MIN_IN_SECS
            self.pm_window_update_to_DB(lport, asic_index, start_window, end_window, pm_win_itrvl_in_secs, pm_hw_data)

            # Update 24hrs PM time window slots
            start_window = WINDOW_24HRS_START_NUM
            end_window = WINDOW_24HRS_END_NUM 
            pm_win_itrvl_in_secs = TIME_24HRS_IN_SECS
            self.pm_window_update_to_DB(lport, asic_index, start_window, end_window, pm_win_itrvl_in_secs, pm_hw_data)

    def run(self):
        if self.task_stopping_event.is_set():
            return

        try:
            self.task_worker()
        except Exception as e:
            self.log_error("Exception occured at {} thread due to {}".format(threading.current_thread().getName(), repr(e)))
            exc_type, exc_value, exc_traceback = sys.exc_info()
            msg = traceback.format_exception(exc_type, exc_value, exc_traceback)
            for tb_line in msg:
                for tb_line_split in tb_line.splitlines():
                    self.log_error(tb_line_split)
            self.exc = e
            self.main_thread_stop_event.set()

    def join(self):
        self.task_stopping_event.set()
        threading.Thread.join(self)
        if self.exc:
            raise self.exc


