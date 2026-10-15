"""Test fixtures: a fake AC climate platform and a fake heater switch the
proxy can drive."""

import pytest
from pytest_homeassistant_custom_component.common import (
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

pytest_plugins = "pytest_homeassistant_custom_component"


class FakeAC(ClimateEntity):
    """Minimal AC that records every command it receives."""

    _attr_name = "AC"
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
    _attr_fan_modes = ["quiet", "low", "mid", "high"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = 16
    _attr_max_temp = 30
    _attr_target_temperature_step = 1

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = "mid"
        self._attr_target_temperature = 22

    async def async_set_hvac_mode(self, hvac_mode):
        self.calls.append(("mode", hvac_mode))
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        self.calls.append(("temp", kwargs[ATTR_TEMPERATURE]))
        self._attr_target_temperature = kwargs[ATTR_TEMPERATURE]
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode):
        self.calls.append(("fan", fan_mode))
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_turn_on(self):
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self):
        await self.async_set_hvac_mode(HVACMode.OFF)


class FakeSwitch(SwitchEntity):
    """Smart plug for a space heater that records every command it receives."""

    _attr_name = "Heater"
    _attr_should_poll = False

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._attr_is_on = False

    async def async_turn_on(self, **kwargs):
        self.calls.append("on")
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self.calls.append("off")
        self._attr_is_on = False
        self.async_write_ha_state()


def _register(hass, integration: str, domain: str, entity) -> None:
    """Register an `integration` that sets up `entity` on its `domain` platform."""

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([entity])

    mock_integration(hass, MockModule(integration))
    mock_platform(
        hass, f"{integration}.{domain}", MockPlatform(async_setup_platform=_setup_platform)
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load integrations from custom_components/ in every test."""
    yield


@pytest.fixture
def fake_ac(hass) -> FakeAC:
    """Register a `fake_ac` climate platform that provides climate.ac."""
    entity = FakeAC()
    _register(hass, "fake_ac", "climate", entity)
    return entity


@pytest.fixture
def fake_switch(hass) -> FakeSwitch:
    """Register a `fake_switch` switch platform that provides switch.heater."""
    entity = FakeSwitch()
    _register(hass, "fake_switch", "switch", entity)
    return entity
