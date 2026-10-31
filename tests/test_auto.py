"""Auto mode: the proxy asks an external optimizer on every control tick."""

from datetime import timedelta

import aiohttp

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.proxy_climate import DOMAIN

URL = "http://optimizer.local:8099"
COMMAND = URL + "/api/v1/command"

AC = {
    "platform": DOMAIN,
    "name": "Proxy",
    "source": "climate.ac",
    "sensor": "sensor.room",
    "source_fan_modes": ["quiet", "low", "mid", "high"],
    "target_temp": 21,
    "optimizer_url": URL,
}
HEATER = {
    "platform": DOMAIN,
    "name": "Proxy",
    "source": "switch.heater",
    "sensor": "sensor.room",
    "target_temp": 21,
    "optimizer_url": URL,
}


async def _setup_ac(hass, **extra):
    hass.states.async_set("sensor.room", "21.0")
    assert await async_setup_component(
        hass, "climate", {"climate": [{"platform": "fake_ac"}, {**AC, **extra}]}
    )
    await hass.async_block_till_done()
    await hass.async_start()
    await hass.async_block_till_done()


async def _setup_heater(hass, **extra):
    hass.states.async_set("sensor.room", "21.0")
    assert await async_setup_component(hass, "switch", {"switch": [{"platform": "fake_switch"}]})
    assert await async_setup_component(hass, "climate", {"climate": [{**HEATER, **extra}]})
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


async def _tick(hass, minutes=1):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


def _answer(aioclient_mock, **body):
    aioclient_mock.clear_requests()
    aioclient_mock.post(COMMAND, json=body)


def _ac(hass):
    s = hass.states.get("climate.ac")
    return s.state, s.attributes["temperature"], s.attributes["fan_mode"]


def _proxy(hass):
    return hass.states.get("climate.proxy")


async def test_auto_only_with_optimizer(hass, fake_ac):
    hass.states.async_set("sensor.room", "21.0")
    assert await async_setup_component(
        hass, "climate",
        {"climate": [{"platform": "fake_ac"}, {k: v for k, v in AC.items() if k != "optimizer_url"}]},
    )
    await hass.async_block_till_done()
    assert "auto" not in _proxy(hass).attributes["hvac_modes"]


async def test_follows_mode_and_fan_level(hass, fake_ac, aioclient_mock):
    _answer(aioclient_mock, hvac_mode="heat", fan_level=2, reason="cheap hour")
    await _setup_ac(hass)
    assert "auto" in _proxy(hass).attributes["hvac_modes"]
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    # the room is at target; the thermostat would idle, the optimizer says heat
    assert _ac(hass) == ("heat", 30, "mid")
    state = _proxy(hass)
    assert state.state == "auto"
    assert state.attributes["hvac_action"] == "heating"
    assert state.attributes["optimizer"] == "following"
    assert state.attributes["optimizer_reason"] == "cheap hour"

    _answer(aioclient_mock, hvac_mode="heat", fan_level=0)
    await _tick(hass)
    assert _ac(hass)[2] == "quiet"
    _answer(aioclient_mock, hvac_mode="heat", fan_level=9)
    await _tick(hass)
    assert _ac(hass)[2] == "high", "levels beyond the top are clamped"
    _answer(aioclient_mock, hvac_mode="off")
    await _tick(hass)
    assert _ac(hass)[0] == "off"
    assert _proxy(hass).attributes["hvac_action"] == "idle"
    _answer(aioclient_mock, hvac_mode="cool", fan_level=1)
    await _tick(hass)
    assert _ac(hass) == ("cool", 16, "low")
    assert _proxy(hass).attributes["hvac_action"] == "cooling"


async def test_request_describes_the_proxy(hass, fake_ac, aioclient_mock):
    _answer(aioclient_mock, hvac_mode="off")
    await _setup_ac(hass, optimizer_token="secret")
    await _room(hass, 20.4)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert aioclient_mock.call_count == 1
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url)) == ("POST", COMMAND)
    assert headers["Authorization"] == "Bearer secret"
    assert body["entity_id"] == "climate.proxy"
    assert body["current_temperature"] == 20.4
    assert body["target_temperature"] == 21
    assert body["source_fan_modes"] == ["quiet", "low", "mid", "high"]
    assert body["fan_mode"] == "medium" and body["fan_mode_offset"] == 0
    assert body["modes"] == ["heat", "cool"]


async def test_asks_on_tick_not_on_every_reading(hass, fake_ac, aioclient_mock):
    _answer(aioclient_mock, hvac_mode="heat", fan_level=1)
    await _setup_ac(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    for t in (20.9, 20.8, 20.7):
        await _room(hass, t)
    assert aioclient_mock.call_count == 1
    await _tick(hass)
    assert aioclient_mock.call_count == 2


async def test_unreachable_falls_back_to_thermostat(hass, fake_ac, aioclient_mock):
    aioclient_mock.post(COMMAND, exc=aiohttp.ClientError("down"))
    await _setup_ac(hass)
    await _room(hass, 20.0)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert _proxy(hass).attributes["optimizer"] == "unreachable"
    # demand 1.0 -> the built-in curve runs the mid level, as in heat mode
    assert _ac(hass) == ("heat", 30, "mid")
    await _room(hass, 21.5)
    assert _ac(hass)[0] == "off"


async def test_bad_answers_fall_back(hass, fake_ac, aioclient_mock):
    await _setup_ac(hass)
    await _room(hass, 20.0)
    for body in ({"hvac_mode": "dry"}, {"hvac_mode": "heat", "valid_until": "soon"}, ["heat"]):
        aioclient_mock.clear_requests()
        aioclient_mock.post(COMMAND, json=body)
        await _call(hass, "set_hvac_mode", hvac_mode="off")
        await _call(hass, "set_hvac_mode", hvac_mode="auto")
        assert _proxy(hass).attributes["optimizer"] == "unreachable", body
        assert _ac(hass)[0] == "heat", body


async def test_deferred_answer_uses_thermostat(hass, fake_ac, aioclient_mock):
    """An optimizer in shadow mode answers follow: false."""
    _answer(aioclient_mock, follow=False, reason="shadow mode")
    await _setup_ac(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert _proxy(hass).attributes["optimizer"] == "deferred"
    assert _ac(hass)[0] == "off"  # at target: the thermostat idles
    await _room(hass, 20.0)
    assert _ac(hass)[0] == "heat"


async def test_expired_answer_uses_thermostat(hass, fake_ac, aioclient_mock, freezer):
    until = (dt_util.utcnow() + timedelta(minutes=3)).isoformat()
    _answer(aioclient_mock, hvac_mode="heat", fan_level=3, valid_until=until)
    await _setup_ac(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert _ac(hass)[2] == "high"
    # the optimizer goes quiet: the last answer holds until it expires
    aioclient_mock.clear_requests()
    aioclient_mock.post(COMMAND, exc=TimeoutError())
    freezer.tick(timedelta(minutes=2))
    await _tick(hass, 0)
    assert _ac(hass) == ("heat", 30, "high")
    freezer.tick(timedelta(minutes=2))
    await _tick(hass, 0)
    # at target, within the deadband: the thermostat keeps the cycle at the quietest level
    assert _ac(hass) == ("heat", 30, "quiet")
    await _room(hass, 21.5)
    assert _ac(hass)[0] == "off"


async def test_min_cycle_applies_to_commands(hass, fake_ac, aioclient_mock, freezer):
    _answer(aioclient_mock, hvac_mode="heat", fan_level=1)
    await _setup_ac(hass, min_cycle_duration={"minutes": 5})
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert _ac(hass)[0] == "heat"
    _answer(aioclient_mock, hvac_mode="off")
    freezer.tick(timedelta(minutes=1))
    await _tick(hass, 0)
    assert _ac(hass) == ("heat", 30, "low"), "min on-time keeps it running"
    freezer.tick(timedelta(minutes=5))
    await _tick(hass, 0)
    assert _ac(hass)[0] == "off"


async def test_auto_fallback_off(hass, fake_ac, aioclient_mock):
    aioclient_mock.post(COMMAND, exc=aiohttp.ClientError("down"))
    await _setup_ac(hass, auto_fallback="off")
    await _room(hass, 18)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert _ac(hass)[0] == "off"


async def test_switch_source(hass, fake_switch, aioclient_mock):
    _answer(aioclient_mock, hvac_mode="heat")
    await _setup_heater(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert fake_switch.calls == ["on"]
    assert _proxy(hass).attributes["hvac_action"] == "heating"
    _answer(aioclient_mock, hvac_mode="off")
    await _tick(hass)
    assert fake_switch.calls == ["on", "off"]
    assert aioclient_mock.mock_calls[0][2]["source_domain"] == "switch"


async def test_config_checks(hass, fake_ac):
    hass.states.async_set("sensor.room", "21.0")
    no_url = {k: v for k, v in AC.items() if k != "optimizer_url"}
    assert await async_setup_component(
        hass, "climate",
        {"climate": [{"platform": "fake_ac"}, {**no_url, "auto_fallback": "heat"},
                     {**AC, "name": "Wrong", "hvac_modes": ["heat"], "auto_fallback": "cool"}]},
    )
    await hass.async_block_till_done()
    assert hass.states.get("climate.proxy") is None, "auto_fallback without optimizer_url"
    assert hass.states.get("climate.wrong") is None, "auto_fallback outside the modes"


async def test_update_entity_asks_at_once(hass, fake_switch, aioclient_mock):
    """The optimizer has the proxy ask now (homeassistant.update_entity), not at its tick."""
    assert await async_setup_component(hass, "homeassistant", {})
    _answer(aioclient_mock, hvac_mode="off")
    await _setup_heater(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert fake_switch.calls == [] or fake_switch.calls[-1] == "off"
    _answer(aioclient_mock, hvac_mode="heat", force=True)
    await hass.services.async_call("homeassistant", "update_entity", {"entity_id": "climate.proxy"}, blocking=True)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    assert fake_switch.calls[-1] == "on"


async def test_force_starts_past_min_cycle(hass, fake_ac, aioclient_mock, freezer):
    _answer(aioclient_mock, hvac_mode="heat", fan_level=1)
    await _setup_ac(hass, min_cycle_duration={"minutes": 5})
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    freezer.tick(timedelta(minutes=6))
    _answer(aioclient_mock, hvac_mode="off")
    await _tick(hass, 0)
    assert _ac(hass)[0] == "off"
    freezer.tick(timedelta(minutes=1))
    _answer(aioclient_mock, hvac_mode="heat", fan_level=1)
    await _tick(hass, 0)
    assert _ac(hass)[0] == "off", "min off-time holds an ordinary start"
    freezer.tick(timedelta(minutes=1))
    _answer(aioclient_mock, hvac_mode="heat", fan_level=1, force=True)
    await _tick(hass, 0)
    assert _ac(hass)[0] == "heat", "a forced start does not wait"
