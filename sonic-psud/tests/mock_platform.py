from sonic_platform_base import chassis_base
from sonic_platform_base import fan_base
from sonic_platform_base import fan_drawer_base
from sonic_platform_base import module_base
from sonic_platform_base import psu_base


class MockChassis(chassis_base.ChassisBase):
    def __init__(self,
                 name='Fixed Chassis',
                 position_in_parent=0,
                 presence=True,
                 model='Module Model',
                 serial='Module Serial',
                 status=True):
        super(MockChassis, self).__init__()
        self.name = name
        self.position_in_parent = position_in_parent
        self.presence = presence
        self.model = model
        self.serial = serial
        self.status = status
        self.psu_list = []
        self.fan_drawer_list = []
        self.module_list = []

    def get_num_psus(self):
        return len(self.psu_list)

    def get_all_psus(self):
        return self.psu_list

    def get_psu(self, index):
        return self.psu_list[index]

    def get_num_fan_drawers(self):
        return len(self.fan_drawer_list)

    def get_all_fan_drawers(self):
        return self.fan_drawer_list

    def get_num_modules(self):
        return len(self.module_list)

    def get_all_modules(self):
        return self.module_list

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self.name

    def get_presence(self):
        return self.presence

    def set_presence(self, presence):
        self.presence = presence

    def get_model(self):
        return self.model

    def get_serial(self):
        return self.serial

    def get_status(self):
        return self.status

    def set_status(self, status):
        self.status = status

    def get_position_in_parent(self):
        return self.position_in_parent

    def is_replaceable(self):
        return self.replaceable

    def get_status_led(self):
        return self.status_led_color

    def set_status_led(self, color):
        self.status_led_color = color
        return True

    def get_position_in_parent(self):
        return self.position_in_parent


class MockFan(fan_base.FanBase):
    def __init__(self,
                 name,
                 position_in_parent,
                 presence=True,
                 model='Module Model',
                 serial='Module Serial',
                 status=True,
                 direction=fan_base.FanBase.FAN_DIRECTION_INTAKE,
                 speed=50):
        super(MockFan, self).__init__()
        self.status_led_color = self.STATUS_LED_COLOR_OFF
        self.name = name
        self.position_in_parent = position_in_parent
        self.presence = presence
        self.model = model
        self.serial = serial
        self.status = status
        self.direction = direction
        self.speed = speed

    def get_direction(self):
        return self.direction

    def get_speed(self):
        return self.speed

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self.name

    def get_presence(self):
        return self.presence

    def set_presence(self, presence):
        self.presence = presence

    def get_model(self):
        return self.model

    def get_serial(self):
        return self.serial

    def get_status(self):
        return self.status

    def set_status(self, status):
        self.status = status

    def get_position_in_parent(self):
        return self.position_in_parent

    def is_replaceable(self):
        return self.replaceable

    def get_status_led(self):
        return self.status_led_color

    def set_status_led(self, color):
        self.status_led_color = color
        return True

    def get_position_in_parent(self):
        return self.position_in_parent


class MockFanDrawer(fan_drawer_base.FanDrawerBase):
    def __init__(self,
                 name,
                 position_in_parent,
                 presence=True,
                 model='Module Model',
                 serial='Module Serial',
                 status=True):
        super(MockFanDrawer, self).__init__()
        self.name = name
        self.position_in_parent = position_in_parent
        self.presence = presence
        self.model = model
        self.serial = serial
        self.status = status
        self.max_consumed_power = 500.0

    def get_status(self):
        return self.status

    def set_status(self, status):
        self.status = status

    def get_maximum_consumed_power(self):
        return self.max_consumed_power

    def set_maximum_consumed_power(self, consumed_power):
        self.max_consumed_power = consumed_power

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self.name

    def get_presence(self):
        return self.presence

    def set_presence(self, presence):
        self.presence = presence

    def get_model(self):
        return self.model

    def get_serial(self):
        return self.serial

    def get_status(self):
        return self.status

    def set_status(self, status):
        self.status = status

    def get_position_in_parent(self):
        return self.position_in_parent

    def is_replaceable(self):
        return self.replaceable

    def get_status_led(self):
        return self.status_led_color

    def set_status_led(self, color):
        self.status_led_color = color
        return True

    def get_position_in_parent(self):
        return self.position_in_parent


class MockModule(module_base.ModuleBase):
    def __init__(self,
                 name,
                 position_in_parent,
                 presence=True,
                 model='Module Model',
                 serial='Module Serial',
                 status=True):
        super(MockModule, self).__init__()
        self.name = name
        self.position_in_parent = position_in_parent
        self.presence = presence
        self.model = model
        self.serial = serial
        self.status = status
        self.max_consumed_power = 500.0

    def set_maximum_consumed_power(self, consumed_power):
        self.max_consumed_power = consumed_power

    def get_maximum_consumed_power(self):
        return self.max_consumed_power

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self.name

    def get_presence(self):
        return self.presence

    def set_presence(self, presence):
        self.presence = presence

    def get_model(self):
        return self.model

    def get_serial(self):
        return self.serial

    def get_status(self):
        return self.status

    def set_status(self, status):
        self.status = status

    def get_position_in_parent(self):
        return self.position_in_parent

    def is_replaceable(self):
        return self.replaceable

    def get_status_led(self):
        return self.status_led_color

    def set_status_led(self, color):
        self.status_led_color = color
        return True

    def get_position_in_parent(self):
        return self.position_in_parent


class MockPsu(psu_base.PsuBase):
    def __init__(self,
                 name,
                 position_in_parent,
                 presence=True,
                 model='Module Model',
                 serial='Module Serial',
                 status=True,
                 voltage=12.0,
                 current=8.0,
                 power=100.0,
                 temp=30.00,
                 temp_high_th=50.0,
                 voltage_low_th=11.0,
                 voltage_high_th=13.0,
                 replaceable=True):
        super(MockPsu, self).__init__()
        self.status_led_color = self.STATUS_LED_COLOR_OFF
        self.name = name
        self.position_in_parent = position_in_parent
        self.presence = presence
        self.model = model
        self.serial = serial
        self.status = status
        self.voltage = voltage
        self.current = current
        self.power = power
        self.temp = temp
        self.temp_high_th = temp_high_th
        self.voltage_low_th = voltage_low_th
        self.voltage_high_th = voltage_high_th
        self.replaceable = replaceable

    def get_voltage(self):
        return self.voltage

    def set_voltage(self, voltage):
        self.voltage = voltage

    def get_current(self):
        return self.current

    def set_current(self, current):
        self.current = current

    def get_power(self):
        return self.power

    def set_power(self, power):
        self.power = power

    def get_powergood_status(self):
        return self.status

    def get_temperature(self):
        return self.temp

    def set_temperature(self, power):
        self.temp = temp

    def get_temperature_high_threshold(self):
        return self.temp_high_th

    def get_voltage_high_threshold(self):
        return self.voltage_high_th

    def get_voltage_low_threshold(self):
        return self.voltage_low_th

    def get_maximum_supplied_power(self):
        return self.max_supplied_power

    def set_maximum_supplied_power(self, supplied_power):
        self.max_supplied_power = supplied_power

    # Methods inherited from DeviceBase class and related setters
    def get_name(self):
        return self.name

    def get_presence(self):
        return self.presence

    def set_presence(self, presence):
        self.presence = presence

    def get_model(self):
        return self.model

    def get_serial(self):
        return self.serial

    def get_status(self):
        return self.status

    def set_status(self, status):
        self.status = status

    def get_position_in_parent(self):
        return self.position_in_parent

    def is_replaceable(self):
        return self.replaceable

    def get_status_led(self):
        return self.status_led_color

    def set_status_led(self, color):
        self.status_led_color = color
        return True

    def get_position_in_parent(self):
        return self.position_in_parent
