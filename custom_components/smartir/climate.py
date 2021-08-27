import asyncio
import functools
import json
import logging
import os.path

import voluptuous as vol

from homeassistant.components.climate import ClimateEntity, PLATFORM_SCHEMA
from homeassistant.components.climate.const import (
    HVAC_MODE_OFF, HVAC_MODE_HEAT, HVAC_MODE_COOL,
    HVAC_MODE_DRY, HVAC_MODE_FAN_ONLY, HVAC_MODE_AUTO,
    SUPPORT_TARGET_TEMPERATURE, SUPPORT_FAN_MODE,
    SUPPORT_SWING_MODE, HVAC_MODES, ATTR_HVAC_MODE)
from homeassistant.const import (
    CONF_NAME, STATE_ON, STATE_OFF, STATE_UNKNOWN, ATTR_TEMPERATURE,
    PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from . import COMPONENT_ABS_DIR, Helper
from .controller import get_controller

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Climate"
DEFAULT_DELAY = 0.5

CONF_UNIQUE_ID = 'unique_id'
CONF_DEVICE_CODE = 'device_code'
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_TEMPERATURE_SENSOR = 'temperature_sensor'
CONF_HUMIDITY_SENSOR = 'humidity_sensor'
CONF_POWER_SENSOR = 'power_sensor'
CONF_POWER_SENSOR_RESTORE_STATE = 'power_sensor_restore_state'
CONF_DEFAULT_ON_OP_MODE = 'default_operation_mode'
CONF_DEFAULT_SWING_MODE = 'default_swing_mode'
CONF_DEFAULT_FAN_MODE = 'default_fan_mode'

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_FAN_MODE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_DEVICE_CODE): cv.positive_int,
    vol.Required(CONF_CONTROLLER_DATA): cv.string,
    vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.positive_float,
    vol.Optional(CONF_TEMPERATURE_SENSOR): cv.entity_id,
    vol.Optional(CONF_HUMIDITY_SENSOR): cv.entity_id,
    vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
    vol.Optional(CONF_POWER_SENSOR_RESTORE_STATE, default=False): cv.boolean,
    vol.Optional(CONF_DEFAULT_ON_OP_MODE): cv.string,
    vol.Optional(CONF_DEFAULT_SWING_MODE): cv.string,
    vol.Optional(CONF_DEFAULT_FAN_MODE): cv.string,
})


def handle_exception_deco(exception=Exception, error_msg=None):
    def deco(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except exception:
                exception_msg = error_msg or 'Exception happen during execution'
                _LOGGER.exception(exception_msg)
        return wrapped
    return deco


def async_handle_exception_deco(exception=Exception, error_msg=None):
    def deco(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except exception:
                exception_msg = error_msg or 'Exception happen during execution'
                _LOGGER.exception(exception_msg)
        return wrapped
    return deco


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the IR Climate platform."""
    device_code = config.get(CONF_DEVICE_CODE)
    device_files_subdir = os.path.join('codes', 'climate')
    device_files_absdir = os.path.join(COMPONENT_ABS_DIR, device_files_subdir)

    if not os.path.isdir(device_files_absdir):
        os.makedirs(device_files_absdir)

    device_json_filename = str(device_code) + '.json'
    device_json_path = os.path.join(device_files_absdir, device_json_filename)

    if not os.path.exists(device_json_path):
        _LOGGER.warning("Couldn't find the device Json file. The component will "
                        "try to download it from the GitHub repo.")

        try:
            codes_source = ("https://raw.githubusercontent.com/"
                            "smartHomeHub/SmartIR/master/"
                            "codes/climate/{}.json")

            await Helper.downloader(codes_source.format(device_code), device_json_path)
        except Exception:
            _LOGGER.error("There was an error while downloading the device Json file. "
                          "Please check your internet connection or if the device code "
                          "exists on GitHub. If the problem still exists please "
                          "place the file manually in the proper directory.")
            return

    with open(device_json_path) as j:
        try:
            device_data = json.load(j)
        except Exception:
            _LOGGER.error("The device Json file is invalid")
            return

    async_add_entities([SmartIRClimate(hass, config, device_data)])


class SmartIRClimate(ClimateEntity, RestoreEntity):

    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._temperature_sensor = config.get(CONF_TEMPERATURE_SENSOR)
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_sensor_restore_state = config.get(CONF_POWER_SENSOR_RESTORE_STATE)

        self._temp_lock = asyncio.Lock()
        self._hvac_mode = HVAC_MODE_OFF
        self._support_flags = SUPPORT_FLAGS
        self._unit = hass.config.units.temperature_unit

        self._manufacturer = device_data['manufacturer']
        self._supported_models = device_data['supportedModels']
        self._supported_controller = device_data['supportedController']
        self._commands_encoding = device_data['commandsEncoding']
        self._min_temperature = device_data['minTemperature']
        self._max_temperature = device_data['maxTemperature']
        self._target_temperature = self._min_temperature
        self._precision = device_data['precision']
        self._operation_modes = [HVAC_MODE_OFF] + [x for x in device_data['operationModes'] if x in HVAC_MODES]
        self._fan_modes = device_data['fanModes']
        self._commands = device_data['commands']
        self._swing_modes = device_data.get('swingModes')
        self._current_swing_mode = None
        self._last_on_operation = None
        self._current_temperature = None
        self._current_humidity = None

        self._default_on_operation_mode = None

        # Init the IR/RF controller
        self._controller = get_controller(
            self.hass,
            self._supported_controller,
            self._commands_encoding,
            self._controller_data,
            self._delay)

        # set defaults
        defaults = device_data.get('defaults', {})
        self._default_on_operation_mode = config.get(CONF_DEFAULT_ON_OP_MODE) or defaults.get('operationMode')
        if not self._default_on_operation_mode or self._default_on_operation_mode not in self._operation_modes:
            _LOGGER.warning('Can\'t find operation mode %s, using a first one. Possible values: %s.',
                            self._default_on_operation_mode, self._operation_modes)
            self._default_on_operation_mode = self._operation_modes[0]

        self._current_fan_mode = config.get(CONF_DEFAULT_FAN_MODE) or defaults.get('fanMode')
        if not self._current_fan_mode or self._current_fan_mode not in self._fan_modes:
            _LOGGER.warning('Can\'t find fan mode %s, using a first one. Possible values: %s.',
                            self._current_fan_mode, self._fan_modes)
            self._current_fan_mode = self._fan_modes[0]

        if self.is_swing_supported:
            self._support_flags = self._support_flags | SUPPORT_SWING_MODE
            self._current_swing_mode = config.get(CONF_DEFAULT_SWING_MODE) or defaults.get('swingMode')
            if not self._current_swing_mode or self._current_swing_mode not in self._swing_modes:
                _LOGGER.warning('Can\'t find swing mode %s, using a first one. Possible values: %s.',
                                self._current_fan_mode, self._fan_modes)
                self._current_swing_mode = self._swing_modes[0]

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if last_state:
            self._hvac_mode = last_state.state
            self._current_fan_mode = last_state.attributes['fan_mode']
            self._target_temperature = last_state.attributes['temperature']
            self._current_swing_mode = last_state.attributes.get('swing_mode')
            self._last_on_operation = last_state.attributes.get('last_on_operation')

        if self._temperature_sensor:
            async_track_state_change(self.hass, self._temperature_sensor,
                                     self._async_temp_sensor_changed)

            temp_sensor_state = self.hass.states.get(self._temperature_sensor)
            if temp_sensor_state and temp_sensor_state.state != STATE_UNKNOWN:
                self._async_update_temp(temp_sensor_state)

        if self._humidity_sensor:
            async_track_state_change(self.hass, self._humidity_sensor,
                                     self._async_humidity_sensor_changed)

            humidity_sensor_state = self.hass.states.get(self._humidity_sensor)
            if humidity_sensor_state and humidity_sensor_state.state != STATE_UNKNOWN:
                self._async_update_humidity(humidity_sensor_state)

        if self._power_sensor:
            async_track_state_change(self.hass, self._power_sensor,
                                     self._async_power_sensor_changed)

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the climate device."""
        return self._name

    @property
    def state(self):
        """Return the current state."""
        if self.hvac_mode != HVAC_MODE_OFF:
            return self.hvac_mode
        return HVAC_MODE_OFF

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def min_temp(self):
        """Return the polling state."""
        return self._min_temperature

    @property
    def max_temp(self):
        """Return the polling state."""
        return self._max_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return self._precision

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        return self._operation_modes

    @property
    def hvac_mode(self):
        """Return hvac mode ie. heat, cool."""
        return self._hvac_mode

    @property
    def last_on_operation(self):
        """Return the last non-idle operation ie. heat, cool."""
        return self._last_on_operation

    @property
    def fan_modes(self):
        """Return the list of available fan modes."""
        return self._fan_modes

    @property
    def fan_mode(self):
        """Return the fan setting."""
        return self._current_fan_mode

    @property
    def swing_modes(self):
        """Return the swing modes currently supported for this device."""
        return self._swing_modes

    @property
    def swing_mode(self):
        """Return the current swing mode."""
        return self._current_swing_mode

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temperature

    @property
    def current_humidity(self):
        """Return the current humidity."""
        return self._current_humidity

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    @property
    def device_state_attributes(self) -> dict:
        """Platform specific attributes."""
        return {
            'last_on_operation': self._last_on_operation,
            'device_code': self._device_code,
            'manufacturer': self._manufacturer,
            'supported_models': self._supported_models,
            'supported_controller': self._supported_controller,
            'commands_encoding': self._commands_encoding
        }

    @property
    def is_swing_supported(self):
        return bool(self._swing_modes)

    async def async_set_temperature(self, **kwargs):
        """Set new target temperatures."""
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        temperature = kwargs.get(ATTR_TEMPERATURE)

        if temperature is None:
            return

        if temperature < self._min_temperature or temperature > self._max_temperature:
            _LOGGER.warning('The temperature value is out of min/max range')
            return

        if self._precision == PRECISION_WHOLE:
            self._target_temperature = round(temperature)
        else:
            self._target_temperature = round(temperature, 1)

        if hvac_mode:
            await self.async_set_hvac_mode(hvac_mode)
            return

        if self._hvac_mode.lower() != HVAC_MODE_OFF:
            await self.send_command()

        await self.async_update_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set operation mode."""
        await self.send_command(hvac_mode)

        self._hvac_mode = hvac_mode
        self._last_on_operation = hvac_mode
        if self._hvac_mode.lower() == HVAC_MODE_OFF:
            self._last_on_operation = None

        await self.async_update_ha_state()

    async def async_set_fan_mode(self, fan_mode):
        """Set fan mode."""
        self._current_fan_mode = fan_mode
        if not self._hvac_mode.lower() != HVAC_MODE_OFF:
            await self.send_command()

        await self.async_update_ha_state()

    async def async_set_swing_mode(self, swing_mode):
        """Set swing mode."""
        self._current_swing_mode = swing_mode
        if not self._hvac_mode.lower() != HVAC_MODE_OFF:
            await self.send_command()

        await self.async_update_ha_state()

    async def async_turn_off(self):
        """Turn off."""
        await self.async_set_hvac_mode(HVAC_MODE_OFF)

    async def async_turn_on(self):
        """Turn on."""
        cmd = self._default_on_operation_mode
        if self._last_on_operation:
            cmd = self._last_on_operation

        await self.async_set_hvac_mode(cmd)

    @async_handle_exception_deco(error_msg="Send command failed")
    async def send_command(self, new_op_mode=None):
        async with self._temp_lock:
            operation_mode = new_op_mode or self._hvac_mode
            target_temperature = '{0:g}'.format(self._target_temperature)

            if operation_mode.lower() == HVAC_MODE_OFF:
                if self._hvac_mode != HVAC_MODE_OFF:
                    await self._controller.send(self._commands['off'])

                return

            if 'on' in self._commands:
                await self._controller.send(self._commands['on'])
                await asyncio.sleep(self._delay)

            op_mode_data = self._commands[operation_mode]
            fan_mode_data = op_mode_data.get(self._current_fan_mode) or op_mode_data.get('default')
            if self.is_swing_supported:
                swing_mode_data = fan_mode_data[self._current_swing_mode]

                cmd = swing_mode_data
                if isinstance(swing_mode_data, dict):
                    cmd = swing_mode_data[target_temperature]

                await self._controller.send(cmd)
            else:
                await self._controller.send(fan_mode_data[target_temperature])

    async def _async_temp_sensor_changed(self, entity_id, old_state, new_state):
        """Handle temperature sensor changes."""
        if new_state is None:
            return

        self._async_update_temp(new_state)
        await self.async_update_ha_state()

    async def _async_humidity_sensor_changed(self, entity_id, old_state, new_state):
        """Handle humidity sensor changes."""
        if new_state is None:
            return

        self._async_update_humidity(new_state)
        await self.async_update_ha_state()

    async def _async_power_sensor_changed(self, entity_id, old_state, new_state):
        """Handle power sensor changes."""
        if new_state is None:
            return

        if new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON and self._hvac_mode == HVAC_MODE_OFF:
            self._hvac_mode = STATE_ON
            if self._power_sensor_restore_state and self._last_on_operation:
                self._hvac_mode = self._last_on_operation

        elif new_state.state == STATE_OFF:
            self._hvac_mode = HVAC_MODE_OFF

        await self.async_update_ha_state()

    @callback
    @handle_exception_deco(exception=ValueError, error_msg='Unable to update from temperature sensor')
    def _async_update_temp(self, state):
        """Update thermostat with latest state from temperature sensor."""
        if state.state != STATE_UNKNOWN:
            self._current_temperature = float(state.state)

    @callback
    @handle_exception_deco(exception=ValueError, error_msg='Unable to update from humidity sensor')
    def _async_update_humidity(self, state):
        """Update thermostat with latest state from humidity sensor."""
        if state.state != STATE_UNKNOWN:
            self._current_humidity = float(state.state)
