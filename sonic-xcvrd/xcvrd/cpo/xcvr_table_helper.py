try:
    from sonic_py_common import multi_asic
    from swsscommon import swsscommon
    from ..xcvrd_utilities.xcvr_table_helper import XcvrTableHelper, VDM_THRESHOLD_TYPES
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

TRANSCEIVER_ELS_INFO_TABLE = 'TRANSCEIVER_ELS_INFO'
TRANSCEIVER_ELS_DOM_THRESHOLD_TABLE = 'TRANSCEIVER_ELS_DOM_THRESHOLD'


class CpoXcvrTableHelper(XcvrTableHelper):
    """XcvrTableHelper extended with the ELS (external laser source) tables used on CPO platforms."""

    def __init__(self, namespaces):
        super().__init__(namespaces)
        self.els_info_tbl = {}
        self.els_dom_threshold_tbl = {}
        self.els_vdm_threshold_tbl = {f'els_vdm_{t}_threshold_tbl': {} for t in VDM_THRESHOLD_TYPES}
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            self.els_info_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_INFO_TABLE)
            self.els_dom_threshold_tbl[asic_id] = swsscommon.Table(self.state_db[asic_id], TRANSCEIVER_ELS_DOM_THRESHOLD_TABLE)
            for t in VDM_THRESHOLD_TYPES:
                self.els_vdm_threshold_tbl[f'els_vdm_{t}_threshold_tbl'][asic_id] = swsscommon.Table(self.state_db[asic_id], f'TRANSCEIVER_ELS_VDM_{t.upper()}_THRESHOLD')

    def get_els_info_tbl(self, asic_id):
        return self.els_info_tbl[asic_id]

    def get_els_dom_threshold_tbl(self, asic_id):
        return self.els_dom_threshold_tbl[asic_id]

    def get_els_vdm_threshold_tbl(self, asic_id, threshold_type):
        return self.els_vdm_threshold_tbl[f'els_vdm_{threshold_type}_threshold_tbl'][asic_id]

    def get_dom_tables(self, asic_id):
        return super().get_dom_tables(asic_id) + [self.get_els_dom_threshold_tbl(asic_id)]

    def get_vdm_tables(self, asic_id):
        return super().get_vdm_tables(asic_id) + \
            [self.get_els_vdm_threshold_tbl(asic_id, key) for key in VDM_THRESHOLD_TYPES]

    def get_info_tables(self, asic_id):
        return super().get_info_tables(asic_id) + [self.get_els_info_tbl(asic_id)]
