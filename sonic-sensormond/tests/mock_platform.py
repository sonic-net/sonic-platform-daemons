from sonic_platform_base import chassis_base
from sonic_platform_base import module_base
from sonic_platform_base import sensor_base

class MockVsensor(sensor_base.VsensorBase):
    def __init__(self, index=None):
        super(MockVsensor, self).__init__()
        self._name = 'Vsensor {}'.format(index) if index != None else None
        self._presence = True
        self._model = 'Vsensor Model'
        self._serial = 'Vsensor Serial'
        self._status = True
        self._position_in_parent = 1
        self._replaceable = False

        self._value = 2
        self._minimum_value = 1
        self._maximum_value = 5
        self._high_threshold = 3
        self._low_threshold = 1
        self._high_critical_threshold = 4
        self._low_critical_threshold = 0

    def get_value(self):
        return self._value

    def get_minimum_recorded(self):
        return self._minimum_value

    def get_maximum_recorded(self):
        return self._maximum_value

    def get_high_threshold(self):
        return self._high_threshold

    def get_low_threshold(self):
        return self._low_threshold

    def get_high_critical_threshold(self):
        return self._high_critical_threshold

    def get_low_critical_threshold(self):
        return self._low_critical_threshold

    def make_over_threshold(self):
        self._high_threshold = 2
        self._value = 3
        self._low_threshold = 1

    def make_under_threshold(self):
        self._high_threshold = 3
        self._value = 1
        self._low_threshold = 2

    def make_normal_value(self):
        self._high_threshold = 3
        self._value = 2
        self._low_threshold = 1

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self._name

    def get_presence(self):
        return self._presence

    def set_presence(self, presence):
        self._presence = presence

    def get_model(self):
        return self._model

    def get_serial(self):
        return self._serial

    def get_status(self):
        return self._status

    def set_status(self, status):
        self._status = status

    def get_position_in_parent(self):
        return self._position_in_parent

    def is_replaceable(self):
        return self._replaceable

class MockIsensor(sensor_base.IsensorBase):
    def __init__(self, index=None):
        super(MockIsensor, self).__init__()
        self._name = 'Isensor {}'.format(index) if index != None else None
        self._presence = True
        self._model = 'Isensor Model'
        self._serial = 'Isensor Serial'
        self._status = True
        self._position_in_parent = 1
        self._replaceable = False

        self._value = 2
        self._minimum_value = 1
        self._maximum_value = 5
        self._high_threshold = 3
        self._low_threshold = 1
        self._high_critical_threshold = 4
        self._low_critical_threshold = 0

    def get_value(self):
        return self._value

    def get_minimum_recorded(self):
        return self._minimum_value

    def get_maximum_recorded(self):
        return self._maximum_value

    def get_high_threshold(self):
        return self._high_threshold

    def get_low_threshold(self):
        return self._low_threshold

    def get_high_critical_threshold(self):
        return self._high_critical_threshold

    def get_low_critical_threshold(self):
        return self._low_critical_threshold

    def make_over_threshold(self):
        self._high_threshold = 2
        self._value = 3
        self._low_threshold = 1

    def make_under_threshold(self):
        self._high_threshold = 3
        self._value = 1
        self._low_threshold = 2

    def make_normal_value(self):
        self._high_threshold = 3
        self._value = 2
        self._low_threshold = 1

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self._name

    def get_presence(self):
        return self._presence

    def set_presence(self, presence):
        self._presence = presence

    def get_model(self):
        return self._model

    def get_serial(self):
        return self._serial

    def get_status(self):
        return self._status

    def set_status(self, status):
        self._status = status

    def get_position_in_parent(self):
        return self._position_in_parent

    def is_replaceable(self):
        return self._replaceable

class MockErrorVsensor(MockVsensor):
    def get_value(self):
        raise Exception('Failed to get voltage')

class MockErrorIsensor(MockIsensor):
    def get_value(self):
        raise Exception('Failed to get current')

class MockChassis(chassis_base.ChassisBase):
    def __init__(self):
        super(MockChassis, self).__init__()
        self._name = None
        self._presence = True
        self._model = 'Chassis Model'
        self._serial = 'Chassis Serial'
        self._status = True
        self._position_in_parent = 1
        self._replaceable = False

        self._is_chassis_system = False
        self._my_slot = module_base.ModuleBase.MODULE_INVALID_SLOT

    def make_over_threshold_vsensor(self):
        vsensor = MockVsensor()
        vsensor.make_over_threshold()
        self._vsensor_list.append(vsensor)

    def make_under_threshold_vsensor(self):
        vsensor = MockVsensor()
        vsensor.make_under_threshold()
        self._vsensor_list.append(vsensor)

    def make_error_vsensor(self):
        vsensor = MockErrorVsensor()
        self._vsensor_list.append(vsensor)

    def make_module_vsensor(self):
        module = MockModule()
        self._module_list.append(module)
        module._vsensor_list.append(MockVsensor())

    def make_over_threshold_isensor(self):
        isensor = MockIsensor()
        isensor.make_over_threshold()
        self._isensor_list.append(isensor)

    def make_under_threshold_isensor(self):
        isensor = MockIsensor()
        isensor.make_under_threshold()
        self._isensor_list.append(isensor)

    def make_error_isensor(self):
        isensor = MockErrorIsensor()
        self._isensor_list.append(isensor)

    def make_module_isensor(self):
        module = MockModule()
        self._module_list.append(module)
        module._isensor_list.append(MockIsensor())

    def is_modular_chassis(self):
        return self._is_chassis_system

    def set_modular_chassis(self, is_true):
        self._is_chassis_system = is_true

    def set_my_slot(self, my_slot):
        self._my_slot = my_slot

    def get_my_slot(self):
        return self._my_slot

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self._name

    def get_presence(self):
        return self._presence

    def set_presence(self, presence):
        self._presence = presence

    def get_model(self):
        return self._model

    def get_serial(self):
        return self._serial

    def get_status(self):
        return self._status

    def set_status(self, status):
        self._status = status

    def get_position_in_parent(self):
        return self._position_in_parent

    def is_replaceable(self):
        return self._replaceable


class MockModule(module_base.ModuleBase):
    def __init__(self):
        super(MockModule, self).__init__()
