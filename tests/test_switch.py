"""Behaviour tests for the proxy climate entity with a switch source."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from homeassistant.core import CoreState, State
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.proxy_climate import DOMAIN

BASE = {
    "platform": DOMAIN,
    "name": "Proxy",
    "source": "switch.heater",
    "sensor": "sensor.room",
    "target_temp": 21,
}


async def _setup(hass, **extra):
    hass.states.async_set("sensor.room", "21.0")
    assert await async_setup_component(
        hass, "switch", {"switch": [{"platform": "fake_switch"}]}
    )
    assert await async_setup_component(hass, "climate", {"climate": [{**BASE, **extra}]})
    await hass.async_block_till_done()
    await hass.async_start()
    await hass.async_block_till_done()


async def _room(hass, value):
    hass.states.async_set("sensor.room", str(value))
    await hass.async_block_till_done()


async def _call(hass, service, **data):
    await hass.services.async_call(
        "climate", service, {"entity_id": "climate.proxy", **data}, blocking=True
    )
    await hass.async_block_till_done()


async def _flip(hass, service):
    """Switch the heater by hand, bypassing the proxy."""
    await hass.services.async_call(
        "switch", service, {"entity_id": "switch.heater"}, blocking=True
    )
    await hass.async_block_till_done()


async def _tick(hass, minutes=2):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


def _switch(hass):
    return hass.states.get("switch.heater").state


def _proxy(hass):
    return hass.states.get("climate.proxy")


async def test_heat_cycle(hass, fake_switch):
    await _setup(hass)
    proxy = _proxy(hass)
    assert proxy.state == "off"
    assert proxy.attributes["hvac_modes"] == ["off", "heat"]
    assert "fan_mode" not in proxy.attributes
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    assert _proxy(hass).attributes["hvac_action"] == "idle"

    steps = [
        (20.6, "off"),
        (20.5, "on"),
        (20.0, "on"),
        (21.4, "on"),  # deadband: still running past target
        (21.5, "off"),
        (21.0, "off"),
        (20.6, "off"),  # deadband: still idle below target
        (20.5, "on"),
    ]
    for temp, expected in steps:
        await _room(hass, temp)
        assert _switch(hass) == expected, temp
    assert _proxy(hass).attributes["hvac_action"] == "heating"


async def test_cooler(hass, fake_switch):
    await _setup(hass, hvac_mode="cool")
    assert _proxy(hass).attributes["hvac_modes"] == ["off", "cool"]
    await _room(hass, 22)
    await _call(hass, "turn_on")
    assert _proxy(hass).state == "cool"
    assert _proxy(hass).attributes["hvac_action"] == "cooling"
    assert _switch(hass) == "on"
    await _room(hass, 20.5)
    assert _switch(hass) == "off"


async def test_min_cycle(hass, fake_switch, freezer):
    await _setup(hass, min_cycle_duration={"minutes": 5})
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    assert _switch(hass) == "on"
    await _room(hass, 21.6)
    assert _switch(hass) == "on", "min on-time keeps it running"
    freezer.tick(timedelta(minutes=6))
    await _tick(hass, 0)
    assert _switch(hass) == "off"
    await _room(hass, 20.0)
    assert _switch(hass) == "off", "min off-time blocks restart"
    freezer.tick(timedelta(minutes=6))
    await _tick(hass, 0)
    assert _switch(hass) == "on"


async def test_off_sensor_loss_and_manual_switching(hass, fake_switch):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    assert _switch(hass) == "on"
    await _room(hass, "unavailable")
    assert _switch(hass) == "off"
    assert _proxy(hass).attributes["hvac_action"] == "idle"
    await _room(hass, 20.0)
    assert _switch(hass) == "on"

    # While heating, the proxy owns the switch.
    await _flip(hass, "turn_off")
    await _tick(hass)
    assert _switch(hass) == "on"

    await _call(hass, "turn_off")
    assert _switch(hass) == "off"
    # When the proxy is off, the switch is left alone.
    await _flip(hass, "turn_on")
    await _tick(hass)
    assert _switch(hass) == "on"
    await _room(hass, 22)
    await _call(hass, "turn_on")
    assert _proxy(hass).state == "heat"
    assert _switch(hass) == "off"


async def test_no_redundant_commands(hass, fake_switch):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    sent = len(fake_switch.calls)
    await _room(hass, 19.9)
    await _room(hass, 21.0)
    await _tick(hass)
    assert fake_switch.calls[sent:] == []


async def test_restore_resumes_cycle(hass, fake_switch):
    mock_restore_cache(
        hass,
        [State("climate.proxy", "heat", {"temperature": 21.0, "hvac_action": "heating"})],
    )
    hass.set_state(CoreState.not_running)
    await _setup(hass, target_temp=19)
    state = _proxy(hass)
    assert state.state == "heat"
    assert state.attributes["temperature"] == 21.0
    assert _switch(hass) == "on", "a cycle in progress resumes inside the deadband"


async def test_input_boolean_source(hass, fake_switch):
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"heater": {"name": "Heater"}}}
    )
    await _setup(hass, source="input_boolean.heater")
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    assert hass.states.get("input_boolean.heater").state == "on"
    await _room(hass, 21.5)
    assert hass.states.get("input_boolean.heater").state == "off"


@pytest.mark.parametrize(
    "extra",
    [
        {"source_fan_modes": ["low"]},  # only valid for a climate source
        {"hvac_mode": "dry"},
        {"source": "light.lamp"},
        {"source": "climate.ac"},  # a climate source needs source_fan_modes
    ],
)
async def test_invalid_config(hass, fake_switch, extra):
    await _setup(hass, **extra)
    assert _proxy(hass) is None
