"""Test fixtures: a fake AC climate platform the proxy can drive."""

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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load integrations from custom_components/ in every test."""
    yield


@pytest.fixture
def fake_ac(hass) -> FakeAC:
    """Register a `fake_ac` climate platform that provides climate.ac."""
    entity = FakeAC()

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([entity])

    mock_integration(hass, MockModule("fake_ac"))
    mock_platform(hass, "fake_ac.climate", MockPlatform(async_setup_platform=_setup_platform))
    return entity
