# SFP status definition, shall be aligned with the definition in get_change_event() of ChassisBase
SFP_STATUS_REMOVED = '0'
SFP_STATUS_INSERTED = '1'

# SFP error code dictinary, new elements can be added if new errors need to be supported.
SFP_STATUS_ERR_DICT = {
    2: 'SFP_STATUS_ERR_I2C_STUCK',
    4: 'SFP_STATUS_ERR_BAD_EEPROM',
    8: 'SFP_STATUS_ERR_UNSUPPORTED_CABLE',
    16: 'SFP_STATUS_ERR_HIGH_TEMP',
    32: 'SFP_STATUS_ERR_BAD_CABLE'
}

error_code_block_eeprom_reading = set((error_code for error_code in SFP_STATUS_ERR_DICT.keys()))
error_str_block_eeprom_reading = set((error for error in SFP_STATUS_ERR_DICT.values()))


def is_error_block_eeprom_reading(status):
    int_status = int(status)
    for error_code in error_code_block_eeprom_reading:
        if int_status & error_code:
            return True
    return False


def detect_port_in_error_status(logical_port_name, status_tbl):
    rec, fvp = status_tbl.get(logical_port_name)
    if rec:
        status_dict = dict(fvp)
        if 'error' in status_dict:
            for error in error_str_block_eeprom_reading:
                if error in status_dict['error']:
                    return True
    return False

