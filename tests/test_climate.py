"""Behaviour tests for the proxy climate entity."""

from datetime import timedelta

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
    "source": "climate.ac",
    "sensor": "sensor.room",
    "source_fan_modes": ["quiet", "low", "mid", "high"],
    "target_temp": 21,
}


async def _setup(hass, **extra):
    hass.states.async_set("sensor.room", "21.0")
    assert await async_setup_component(
        hass, "climate", {"climate": [{"platform": "fake_ac"}, {**BASE, **extra}]}
    )
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


async def _tick(hass, minutes=2):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


def _ac(hass):
    s = hass.states.get("climate.ac")
    return s.state, s.attributes["temperature"], s.attributes["fan_mode"]


def _proxy(hass):
    return hass.states.get("climate.proxy")


async def test_heat_cycle_and_fan_curve(hass, fake_ac):
    await _setup(hass)
    assert _proxy(hass).state == "off"
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    assert _proxy(hass).attributes["hvac_action"] == "idle"
    assert _ac(hass)[0] == "off"

    steps = [
        (20.6, "off", None),
        (20.5, "heat", "low"),
        (20.0, "heat", "mid"),
        (19.5, "heat", "high"),
        (19.6, "heat", "high"),  # hysteresis holds high
        (19.8, "heat", "mid"),
        (20.1, "heat", "mid"),
        (20.4, "heat", "low"),
        (20.8, "heat", "quiet"),
        (21.4, "heat", "quiet"),  # deadband: still running past target
        (21.5, "off", None),
        (21.0, "off", None),
        (20.6, "off", None),  # deadband: still idle below target
        (20.5, "heat", "low"),
    ]
    for temp, mode, fan in steps:
        await _room(hass, temp)
        state, setpoint, fan_mode = _ac(hass)
        assert state == mode, (temp, state)
        if fan:
            assert (setpoint, fan_mode) == (30, fan), temp


async def test_fan_offsets(hass, fake_ac):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.5)
    assert _ac(hass)[2] == "low"
    await _call(hass, "set_fan_mode", fan_mode="high")
    assert _ac(hass)[2] == "mid"
    await _call(hass, "set_fan_mode", fan_mode="low")
    assert _ac(hass)[2] == "quiet"
    await _room(hass, 18)  # large demand: "low" caps one level below max
    assert _ac(hass)[2] == "mid"


async def test_cool_and_mode_switch(hass, fake_ac):
    await _setup(hass)
    await _room(hass, 22)
    await _call(hass, "set_hvac_mode", hvac_mode="cool")
    assert _ac(hass) == ("cool", 16, "mid")
    assert _proxy(hass).attributes["hvac_action"] == "cooling"
    await _room(hass, 20.5)
    assert _ac(hass)[0] == "off"
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    assert _ac(hass) == ("heat", 30, "low")


async def test_min_cycle(hass, fake_ac, freezer):
    await _setup(hass, min_cycle_duration={"minutes": 5})
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    assert _ac(hass)[0] == "heat"
    await _room(hass, 21.6)
    assert _ac(hass)[0] == "heat", "min on-time keeps it running"
    freezer.tick(timedelta(minutes=6))
    await _tick(hass, 0)
    assert _ac(hass)[0] == "off"
    await _room(hass, 20.0)
    assert _ac(hass)[0] == "off", "min off-time blocks restart"
    freezer.tick(timedelta(minutes=6))
    await _tick(hass, 0)
    assert _ac(hass)[0] == "heat"


async def test_off_and_sensor_loss(hass, fake_ac):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    assert _ac(hass)[0] == "heat"
    await _room(hass, "unavailable")
    assert _ac(hass)[0] == "off"
    assert _proxy(hass).attributes["hvac_action"] == "idle"
    await _room(hass, 20.0)
    assert _ac(hass)[0] == "heat"

    await _call(hass, "turn_off")
    assert _ac(hass)[0] == "off"
    # When the proxy is off, the AC is left alone.
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"}, blocking=True
    )
    await _tick(hass)
    assert _ac(hass)[0] == "cool"
    await _call(hass, "turn_on")
    assert _ac(hass)[0] == "heat"


async def test_hold_setpoint_idle(hass, fake_ac):
    await _setup(hass, idle_action="hold_setpoint")
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    assert _ac(hass) == ("heat", 16, "quiet")
    await _room(hass, 20.0)
    assert _ac(hass) == ("heat", 30, "mid")


async def test_no_redundant_commands(hass, fake_ac):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    sent = len(fake_ac.calls)
    await _room(hass, 19.9)
    await _room(hass, 20.1)
    await _tick(hass)
    assert fake_ac.calls[sent:] == []


async def test_restore_and_custom_setpoint(hass, fake_ac):
    mock_restore_cache(
        hass,
        [State("climate.proxy", "heat", {"temperature": 22.5, "fan_mode": "high", "hvac_action": "heating"})],
    )
    hass.set_state(CoreState.not_running)
    await _setup(hass, heat_setpoint=26)
    state = _proxy(hass)
    assert state.state == "heat"
    assert state.attributes["temperature"] == 22.5
    assert state.attributes["fan_mode"] == "high"
    assert _ac(hass) == ("heat", 26, "high")
