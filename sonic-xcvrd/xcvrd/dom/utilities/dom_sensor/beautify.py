import re


class DOMBeautifyMixin:
    """
    Strips units from raw DOM values so they are stored in the DB as bare numerics.

    Mixed into the DB utility classes that publish DOM data. Kept separate from
    DOMDBUtils so that classes needing only the formatting (e.g. the CPO DOM
    utilities) do not also inherit its table-writing methods.

    Requires the host class to provide self.logger.
    """
    TEMP_UNIT = 'C'
    VOLT_UNIT = 'Volts'
    POWER_UNIT = 'dBm'
    BIAS_UNIT = 'mA'

    def _strip_unit(self, value, unit):
        # Strip unit from raw data
        if isinstance(value, str) and value.endswith(unit):
            return value[:-len(unit)]
        return str(value)

    # Remove unnecessary unit from the raw data
    def _beautify_dom_info_dict(self, dom_info_dict):
        if dom_info_dict is None:
            self.logger.log_warning("DOM info dict is None while beautifying")
            return

        for k, v in dom_info_dict.items():
            if k == 'temperature':
                dom_info_dict[k] = self._strip_unit(v, self.TEMP_UNIT)
            elif k == 'voltage':
                dom_info_dict[k] = self._strip_unit(v, self.VOLT_UNIT)
            elif re.match('^(tx|rx)[1-8]power$', k):
                dom_info_dict[k] = self._strip_unit(v, self.POWER_UNIT)
            elif re.match('^(tx|rx)[1-8]bias$', k):
                dom_info_dict[k] = self._strip_unit(v, self.BIAS_UNIT)
            elif type(v) is not str:
                # For all the other keys:
                dom_info_dict[k] = str(v)
