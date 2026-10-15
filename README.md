# Proxy Climate for Home Assistant

A climate entity that sits in front of a device you already have in Home Assistant and turns it into a proper thermostat. It reads the room temperature from another entity and uses a deadband so the device doesn't flip on and off all the time. Two kinds of device are supported:

* **AC / heat pump** (a `climate` entity): runs the unit at a fixed extreme setpoint while it is working and controls how hard it works through the unit's fan speed.
* **Space heater or cooler on a switch** (a `switch` or `input_boolean` entity, e.g. a smart plug): switches it on while working and off while idle.

## Install with HACS

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Irame&repository=ha-proxy-climate&category=integration)

Or manually: HACS → ⋮ (top right) → Custom repositories → paste `https://github.com/Irame/ha-proxy-climate`, type **Integration** → Add. Then search for "Proxy Climate" in HACS, download it, add the YAML below to `configuration.yaml`, and restart Home Assistant.

You'll get a new `climate.<name>` entity that works in the normal thermostat card. Updates show up in HACS (and under Settings → Updates) whenever a new GitHub release is published.

## Configuration

Both kinds use `platform: proxy_climate`. The domain of `source` decides which one you get.

### AC / heat pump

```yaml
climate:
  - platform: proxy_climate
    name: Living Room
    unique_id: proxy_climate_living_room
    source: climate.living_room_ac          # the real AC entity
    sensor: sensor.living_room_temperature  # sensor or climate entity (uses current_temperature)
    source_fan_modes: [quiet, low, mid, high]  # weakest -> strongest, exactly as the AC names them

    # everything below is optional (defaults shown)
    hvac_modes: [heat, cool]
    # heat_setpoint: 30        # default: the AC's max_temp
    # cool_setpoint: 16        # default: the AC's min_temp
    start_offset: 0.5          # start when 0.5° on the wrong side of target
    stop_offset: 0.5           # stop once 0.5° past the target
    fan_step: 0.5              # one fan level per 0.5° of demand
    fan_hysteresis: 0.2        # extra margin before stepping the fan down
    fan_mode_offsets:          # proxy fan modes -> shift in AC fan levels
      low: -1
      medium: 0
      high: 1
    idle_action: turn_off      # turn_off | fan_only | hold_setpoint
    min_cycle_duration: "00:00:00"
    control_interval: "00:01:00"
    target_temp: 21
    min_temp: 16
    max_temp: 30
    target_temp_step: 0.5
```

Check the exact fan mode names under Developer Tools → States → your AC entity → `fan_modes`. A warning is logged at startup if any configured name doesn't exist on the unit.

### Space heater / cooler on a switch

```yaml
climate:
  - platform: proxy_climate
    name: Office Heater
    unique_id: proxy_climate_office_heater
    source: switch.office_heater_plug       # switch or input_boolean
    sensor: sensor.office_temperature       # sensor or climate entity (uses current_temperature)

    # everything below is optional (defaults shown)
    hvac_mode: heat            # heat | cool, i.e. what the device does while switched on
    start_offset: 0.5
    stop_offset: 0.5
    min_cycle_duration: "00:00:00"
    control_interval: "00:01:00"
    target_temp: 21
    min_temp: 16
    max_temp: 30
    target_temp_step: 0.5
```

The fan-speed options of the AC variant (`source_fan_modes`, `fan_step`, `fan_mode_offsets`, setpoints, `idle_action`, …) don't apply here and are rejected. The entity offers `off` plus the configured `hvac_mode`, and has no fan mode.

## How it behaves

"Demand" is how far the room is from the target in the direction that needs work: `target − room` when heating, `room − target` when cooling. It's shown as the `demand` attribute on the proxy entity.

The device starts when demand reaches `start_offset` and stops when demand falls to `−stop_offset`, i.e. once the room has overshot the target. Between those points it keeps doing whatever it was doing, which is the deadband. `min_cycle_duration` additionally enforces a minimum on-time and off-time; user actions like changing the HVAC mode bypass it.

A switch source is simply on while running and off while idle. With the defaults and a 21° heating target, it switches on at 20.5° and off again at 21.5°.

While running, an AC gets `heat_setpoint` / `cool_setpoint` and a fan level of `floor(demand / fan_step)`, clamped to the available levels, then shifted by the proxy fan mode's offset. With the defaults, a 21° heating target and the medium proxy fan mode:

| Room temp | Demand | AC state |
|---|---|---|
| 20.6 (idle) | 0.4 | off, below start threshold |
| 20.5 | 0.5 | heat, low |
| 20.0 | 1.0 | heat, mid |
| 19.5 | 1.5 | heat, high |
| 19.8 (warming) | 1.2 | heat, mid (drops below 1.5 − 0.2) |
| 20.8 | 0.2 | heat, quiet |
| 21.4 | −0.4 | heat, quiet (still in deadband) |
| 21.5 | −0.5 | off |

The proxy's low mode shifts everything one level down (so it never reaches the top speed), high shifts one level up (so it never drops to the quietest).

When idle, `idle_action` decides what the AC does: `turn_off` switches it off, `fan_only` leaves the fan circulating on the quietest level, and `hold_setpoint` keeps it in heat/cool mode but sets the setpoint to the opposite extreme so it idles on its own sensor, which avoids the startup delay some units have after a full power-off.

## Notes

While the proxy is in heat or cool, it owns the device: anything changed with the IR remote, or by flipping the switch by hand, gets corrected on the next `control_interval` tick. When you turn the proxy off it switches the device off once and then leaves it alone.

If the room sensor becomes unavailable, the proxy goes idle until readings come back, so the device is never left running without feedback.

Target temperature, mode, fan mode, and whether a cycle was in progress are restored after a restart.

On many inverter units the compressor follows the gap between its setpoint and its own internal sensor, so fan speed only partly limits output. If you see icing in cooling, high-pressure trips in heating, or noisy operation, use less extreme `heat_setpoint` / `cool_setpoint` values (e.g. 26 / 20).

For troubleshooting, enable debug logging; every start/stop decision and every command sent to the device is logged:

```yaml
logger:
  logs:
    custom_components.proxy_climate: debug
```

## Publishing an update

1. Bump `version` in `custom_components/proxy_climate/manifest.json` (e.g. `0.1.1`) and push.
2. On GitHub, create a release with a matching tag (`v0.1.1`).
3. HACS picks it up on its next check (or hit ⋮ → Update information in HACS), and you install it and restart Home Assistant.

The Validate workflow runs on every push and weekly: hassfest and HACS check the repository structure, and the tests run against the latest Home Assistant release so you hear about breaking changes before your own instance does.

To run the tests locally (needs the same Python version as current Home Assistant, 3.14 at the time of writing):

```bash
pip install -r requirements_test.txt
pytest
```
