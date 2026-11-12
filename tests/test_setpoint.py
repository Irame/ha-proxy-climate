"""power_control: setpoint: the levels are setpoints some degrees past the AC's own reading."""

from homeassistant.setup import async_setup_component

from custom_components.proxy_climate import DOMAIN

URL = "http://optimizer.local:8099"
COMMAND = URL + "/api/v1/command"

SP = {
    "platform": DOMAIN,
    "name": "Proxy",
    "source": "climate.ac",
    "sensor": "sensor.room",
    "power_control": "setpoint",
    "setpoint_offsets": [1, 2, 3, 4],
    "setpoint_fan_mode": "auto",
    "target_temp": 21,
}


async def _setup(hass, fake_ac, own=24.0, **extra):
    fake_ac._attr_current_temperature = own
    hass.states.async_set("sensor.room", "21.0")
    assert await async_setup_component(hass, "climate", {"climate": [{"platform": "fake_ac"}, {**SP, **extra}]})
    await hass.async_block_till_done()
    await hass.async_start()
    await hass.async_block_till_done()


async def _room(hass, value):
    hass.states.async_set("sensor.room", str(value))
    await hass.async_block_till_done()


async def _own(hass, fake_ac, value):
    fake_ac._attr_current_temperature = value
    fake_ac.async_write_ha_state()
    await hass.async_block_till_done()


async def _call(hass, service, **data):
    await hass.services.async_call("climate", service, {"entity_id": "climate.proxy", **data}, blocking=True)
    await hass.async_block_till_done()


def _ac(hass):
    s = hass.states.get("climate.ac")
    return s.state, s.attributes["temperature"], s.attributes["fan_mode"]


async def test_levels_are_setpoints_past_the_acs_own_reading(hass, fake_ac):
    await _setup(hass, fake_ac)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.5)  # demand 0.5: the second level
    assert _ac(hass) == ("heat", 26, "auto")  # its own 24 °C + 2 K, the fan left to the AC
    assert hass.states.get("climate.proxy").attributes["source_fan_mode"] == "+2 K"
    await _room(hass, 20.0)  # demand 1.0: the third level
    assert _ac(hass)[1] == 27
    await _own(hass, fake_ac, 25.0)  # it warms itself: the setpoint follows its reading
    assert _ac(hass)[1] == 28
    sent = len(fake_ac.calls)
    await _own(hass, fake_ac, 24.0)  # a flicker back down a step: held, no command
    await _own(hass, fake_ac, 25.0)
    assert fake_ac.calls[sent:] == []
    await _own(hass, fake_ac, 23.0)  # two steps down: followed
    assert _ac(hass)[1] == 26


async def test_rounding_cooling_and_range(hass, fake_ac):
    await _setup(hass, fake_ac, own=24.3)  # the fake AC takes whole degrees
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.5)  # demand 0.5: +2 K
    assert _ac(hass)[1] == 27  # 26.3 up to the next whole degree: never less than asked
    await _room(hass, 20.8)  # demand 0.2: the weakest level, +1 K
    assert _ac(hass)[1] == 26  # a level down is followed at once
    await _own(hass, fake_ac, 27.5)
    assert _ac(hass)[1] == 29
    await _room(hass, 19.0)  # the strongest: +4 K, held to the AC's 30 °C
    assert _ac(hass)[1] == 30
    await _call(hass, "set_hvac_mode", hvac_mode="cool")
    await _own(hass, fake_ac, 24.3)
    await _room(hass, 21.9)  # cooling demand 0.9: the second level, -2 K
    assert _ac(hass)[:2] == ("cool", 22)  # 22.3 down to a whole degree


async def test_without_the_acs_reading_the_proxy_sensor_is_used(hass, fake_ac):
    await _setup(hass, fake_ac, own=None)
    await _call(hass, "set_hvac_mode", hvac_mode="heat")
    await _room(hass, 20.4)  # demand 0.6: +2 K on the room sensor
    assert _ac(hass)[1] == 23  # 20.4 + 2 up to a whole degree


async def test_the_optimizer_sees_setpoint_levels(hass, fake_ac, aioclient_mock):
    aioclient_mock.post(COMMAND, json={"hvac_mode": "heat", "fan_level": 0})
    await _setup(hass, fake_ac, optimizer_url=URL)
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    body = aioclient_mock.mock_calls[-1][2]
    assert body["source_fan_modes"] == ["+1 K", "+2 K", "+3 K", "+4 K"]
    assert body["power_control"] == "setpoint" and body["setpoint_offsets"] == [1, 2, 3, 4]
    assert _ac(hass) == ("heat", 25, "auto")


async def test_setpoint_config_checks(hass, fake_ac):
    hass.states.async_set("sensor.room", "21.0")
    bad = [
        {**SP, "power_control": "fan"},  # fan control needs source_fan_modes
        {**SP, "setpoint_offsets": [2, 1]},  # weakest first
    ]
    for i, cfg in enumerate(bad):
        assert await async_setup_component(hass, "climate", {"climate": [{"platform": "fake_ac"}, {**cfg, "name": f"bad{i}"}]})
    await hass.async_block_till_done()
    assert hass.states.get("climate.bad0") is None and hass.states.get("climate.bad1") is None


async def test_a_fan_mode_per_level(hass, fake_ac, aioclient_mock):
    """Both levers: each level its setpoint offset and its own fan speed."""
    aioclient_mock.post(COMMAND, json={"hvac_mode": "heat", "fan_level": 2})
    await _setup(hass, fake_ac, optimizer_url=URL, setpoint_fan_mode=["quiet", "low", "mid", "high"])
    await _call(hass, "set_hvac_mode", hvac_mode="auto")
    assert _ac(hass) == ("heat", 27, "mid")  # its own 24 °C + 3 K, at the third fan speed
    body = aioclient_mock.mock_calls[-1][2]
    assert body["source_fan_modes"] == ["+1 K quiet", "+2 K low", "+3 K mid", "+4 K high"]
    assert body["setpoint_fan_modes"] == ["quiet", "low", "mid", "high"]
    assert hass.states.get("climate.proxy").attributes["source_fan_mode"] == "+3 K mid"


async def test_one_fan_mode_per_offset(hass, fake_ac):
    hass.states.async_set("sensor.room", "21.0")
    cfg = {**SP, "name": "bad", "setpoint_fan_mode": ["quiet", "low"]}  # four offsets, two fan modes
    assert await async_setup_component(hass, "climate", {"climate": [{"platform": "fake_ac"}, cfg]})
    await hass.async_block_till_done()
    assert hass.states.get("climate.bad") is None
