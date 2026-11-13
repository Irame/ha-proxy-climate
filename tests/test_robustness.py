"""What goes wrong around the proxy: a lagging or unreachable AC, a restart, odd readings."""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_fire_time_changed, mock_restore_cache

from homeassistant.core import CoreState, State
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.proxy_climate import DOMAIN

URL = "http://optimizer.local:8099"
COMMAND = URL + "/api/v1/command"
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
    assert await async_setup_component(hass, "climate", {"climate": [{"platform": "fake_ac"}, {**BASE, **extra}]})
    await hass.async_block_till_done()
    await hass.async_start()
    await hass.async_block_till_done()


async def _room(hass, value):
    hass.states.async_set("sensor.room", str(value))
    await hass.async_block_till_done()


async def _call(hass, service, **data):
    await hass.services.async_call("climate", service, {"entity_id": "climate.proxy", **data}, blocking=True)
    await hass.async_block_till_done()


def _proxy(hass):
    return hass.states.get("climate.proxy")


async def test_a_lagging_ac_is_not_sent_the_same_again_and_again(hass, fake_ac, freezer):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    fake_ac.lazy = True  # a cloud AC: takes commands, shows them only at its next poll
    await _room(hass, 20.0)
    sent = [c for c in fake_ac.calls if c[0] == "mode"]
    for v in (19.9, 19.8, 19.9):
        await _room(hass, v)
    assert [c for c in fake_ac.calls if c[0] == "mode"] == sent  # not again while it may still be on its way
    freezer.tick(timedelta(seconds=95))
    await _room(hass, 19.7)
    assert len([c for c in fake_ac.calls if c[0] == "mode"]) == len(sent) + 1  # it did not take it: once more


async def test_an_unreachable_ac_does_not_count_as_running(hass, fake_ac):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    hass.states.async_set("climate.ac", "unavailable")
    await _room(hass, 19.5)
    assert _proxy(hass).attributes["hvac_action"] == "idle"
    fake_ac.async_write_ha_state()  # back
    await hass.async_block_till_done()
    assert _proxy(hass).attributes["hvac_action"] == "heating"
    assert hass.states.get("climate.ac").state == "heat"


async def test_the_minimum_cycle_holds_across_a_restart(hass, fake_ac):
    started = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
    mock_restore_cache(hass, [State("climate.proxy", "heat", {"temperature": 21, "hvac_action": "heating", "last_switch": started})])
    hass.set_state(CoreState.not_running)
    await _setup(hass, min_cycle_duration={"minutes": 5})
    await _room(hass, 21.6)  # past the target: would stop, but it started a minute ago
    assert hass.states.get("climate.ac").state == "heat"


async def test_heat_to_cool_is_not_held_by_the_minimum_cycle(hass, fake_ac, aioclient_mock):
    aioclient_mock.post(COMMAND, json={"hvac_mode": "heat", "fan_level": 1})
    await _setup(hass, optimizer_url=URL, min_cycle_duration={"minutes": 5})
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert hass.states.get("climate.ac").state == "heat"
    aioclient_mock.clear_requests()
    aioclient_mock.post(COMMAND, json={"hvac_mode": "cool", "fan_level": 1})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert hass.states.get("climate.ac").state == "cool"


async def test_a_nan_reading_is_no_reading(hass, fake_ac):
    await _setup(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.0)
    assert hass.states.get("climate.ac").state == "heat"
    await _room(hass, "nan")
    assert hass.states.get("climate.ac").state == "off" and _proxy(hass).attributes["hvac_action"] == "idle"
