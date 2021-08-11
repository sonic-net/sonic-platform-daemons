class PortChangeEvent:
    PORT_ADD = 0
    PORT_REMOVE = 1
    def __init__(self, port_name, port_index, asic_id, event_type):
        # Logical port name, e.g. Ethernet0
        self.port_name = port_name
        # Physical port index, equals to "index" field of PORT table in CONFIG_DB
        self.port_index = int(port_index)
        # ASIC ID, for multi ASIC
        self.asic_id = asic_id
        # Port change event type
        self.event_type = event_type

    def __str__(self):
        return '{} - name={} index={} asic_id={}'.format('Add' if self.event_type == self.PORT_ADD else 'Remove',
                                                         self.port_name,
                                                         self.port_index,
                                                         self.asic_id)


class PortMapping:
    def __init__(self):
        # A list of logical port name, e.g. ["Ethernet0", "Ethernet4" ...]
        self.logical_port_list = []
        # Logical port name to physical port index mapping
        self.logical_to_physical = {}
        # Physical port index to logical port name mapping
        self.physical_to_logical = {}
        # Logical port name to ASIC ID mapping
        self.logical_to_asic = {}

    def handle_port_change_event(self, port_change_event):
        if port_change_event.event_type == PortChangeEvent.PORT_ADD:
            self._handle_port_add(port_change_event)
        elif port_change_event.event_type == PortChangeEvent.PORT_REMOVE:
            self._handle_port_remove(port_change_event)

    def _handle_port_add(self, port_change_event):
        port_name = port_change_event.port_name
        self.logical_port_list.append(port_name)
        self.logical_to_physical[port_name] = port_change_event.port_index
        if port_change_event.port_index not in self.physical_to_logical:
            self.physical_to_logical[port_change_event.port_index] = [port_name]
        else:
            self.physical_to_logical[port_change_event.port_index].append(port_name)
        self.logical_to_asic[port_name] = port_change_event.asic_id

    def _handle_port_remove(self, port_change_event):
        port_name = port_change_event.port_name
        self.logical_port_list.remove(port_name)
        self.logical_to_physical.pop(port_name)
        self.physical_to_logical[port_change_event.port_index].remove(port_name)
        if not self.physical_to_logical[port_change_event.port_index]:
            self.physical_to_logical.pop(port_change_event.port_index)
        self.logical_to_asic.pop(port_name)

    def get_asic_id_for_logical_port(self, port_name):
        return self.logical_to_asic.get(port_name)

    def is_logical_port(self, port_name):
        return port_name in self.logical_to_physical

    def get_logical_to_physical(self, port_name):
        port_index = self.logical_to_physical.get(port_name)
        return None if port_index is None else [port_index]

    def get_physical_to_logical(self, physical_port):
        return self.physical_to_logical.get(physical_port)

    def logical_port_name_to_physical_port_list(self, port_name):
        try:
            return [int(port_name)]
        except ValueError:
            if self.is_logical_port(port_name):
                return self.get_logical_to_physical(port_name)
            else:
                return None
