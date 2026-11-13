import bb_hw_client


class Sfp(object):
    def __init__(self, index):
        self.index = index

    def get_presence(self):
        return bb_hw_client.call("platform", self.index, "get_presence")

    def get_transceiver_info(self):
        return bb_hw_client.call("platform", self.index, "get_transceiver_info")
