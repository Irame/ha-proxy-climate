"""Proxy climate entities.

Both kinds read the room temperature from a different entity (sensor or
climate) and use a deadband (start/stop offsets) plus an optional minimum cycle
time so the source doesn't switch on and off too often. The domain of the
`source` entity decides how it is driven:

* climate (e.g. an AC / heat pump): runs the source at a fixed extreme setpoint
  while active and modulates its output through the source's fan speed,
  proportional to how far the room is from the target, shifted by the proxy's
  own low/medium/high fan setting,
* switch / input_boolean (e.g. a space heater or cooler on a smart plug):
  switches the source on while active and off while idle.

With `optimizer_url` set, the entity also offers an `auto` mode: on every
control tick it posts its state to an external optimizer and does what the
answer says (run in heat or cool at a given fan level, or stay idle). Without
a fresh answer it falls back to its own thermostat.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA as CLIMATE_PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    split_entity_id,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

CONF_SOURCE = "source"
CONF_SENSOR = "sensor"
CONF_SOURCE_FAN_MODES = "source_fan_modes"
CONF_HVAC_MODES = "hvac_modes"
CONF_HVAC_MODE = "hvac_mode"
CONF_HEAT_SETPOINT = "heat_setpoint"
CONF_COOL_SETPOINT = "cool_setpoint"
CONF_START_OFFSET = "start_offset"
CONF_STOP_OFFSET = "stop_offset"
CONF_FAN_STEP = "fan_step"
CONF_FAN_HYSTERESIS = "fan_hysteresis"
CONF_FAN_MODE_OFFSETS = "fan_mode_offsets"
CONF_IDLE_ACTION = "idle_action"
CONF_MIN_CYCLE = "min_cycle_duration"
CONF_INTERVAL = "control_interval"
CONF_TARGET_TEMP = "target_temp"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TEMP_STEP = "target_temp_step"
CONF_OPTIMIZER_URL = "optimizer_url"
CONF_OPTIMIZER_TOKEN = "optimizer_token"
CONF_OPTIMIZER_TIMEOUT = "optimizer_timeout"
CONF_AUTO_FALLBACK = "auto_fallback"

COMMAND_PATH = "/api/v1/command"

# What the auto mode is doing right now (the `optimizer` attribute).
OPT_FOLLOWING = "following"
OPT_DEFERRED = "deferred"  # the optimizer answered but asked us to decide ourselves
OPT_UNREACHABLE = "unreachable"
OPT_STALE = "stale"

# Source domains that are plain on/off devices.
SWITCH_DOMAINS = ["switch", "input_boolean"]

IDLE_TURN_OFF = "turn_off"
IDLE_FAN_ONLY = "fan_only"
IDLE_HOLD_SETPOINT = "hold_setpoint"

ATTR_HVAC_MODE = "hvac_mode"
ATTR_FAN_MODE = "fan_mode"
ATTR_FAN_MODES = "fan_modes"
ATTR_HVAC_ACTION = "hvac_action"
ATTR_CURRENT_TEMPERATURE = "current_temperature"
ATTR_LAST_ON_MODE = "last_on_mode"
ATTR_RUN_MODE = "run_mode"

DEFAULT_FAN_OFFSETS = {"low": -1, "medium": 0, "high": 1}

_FLOAT = vol.Coerce(float)
_NON_NEG_FLOAT = vol.All(vol.Coerce(float), vol.Range(min=0))

# Options shared by every source type.
_COMMON_SCHEMA = CLIMATE_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default="Proxy Climate"): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_SENSOR): cv.entity_id,
        # Deadband: start when the room is `start_offset` on the wrong side of
        # the target, stop once it has overshot the target by `stop_offset`.
        vol.Optional(CONF_START_OFFSET, default=0.5): _NON_NEG_FLOAT,
        vol.Optional(CONF_STOP_OFFSET, default=0.5): _NON_NEG_FLOAT,
        vol.Optional(CONF_MIN_CYCLE, default=timedelta(0)): cv.positive_time_period,
        vol.Optional(CONF_INTERVAL, default=timedelta(seconds=60)): vol.All(
            cv.time_period, vol.Range(min=timedelta(seconds=10))
        ),
        vol.Optional(CONF_TARGET_TEMP, default=21.0): _FLOAT,
        vol.Optional(CONF_MIN_TEMP, default=16.0): _FLOAT,
        vol.Optional(CONF_MAX_TEMP, default=30.0): _FLOAT,
        vol.Optional(CONF_TEMP_STEP, default=0.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.1)
        ),
        # External optimizer that the `auto` mode asks on every control tick.
        vol.Optional(CONF_OPTIMIZER_URL): cv.url,
        vol.Optional(CONF_OPTIMIZER_TOKEN): cv.string,
        vol.Optional(CONF_OPTIMIZER_TIMEOUT, default=timedelta(seconds=5)): vol.All(
            cv.time_period, vol.Range(min=timedelta(seconds=1))
        ),
        # What the built-in thermostat does in auto mode while the optimizer
        # has no fresh answer. Default: the first of the entity's modes.
        vol.Optional(CONF_AUTO_FALLBACK): vol.In(
            [HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF]
        ),
    }
)

CLIMATE_SOURCE_SCHEMA = _COMMON_SCHEMA.extend(
    {
        vol.Required(CONF_SOURCE): cv.entity_domain("climate"),
        # Source fan speeds ordered weakest -> strongest, spelled exactly as
        # the source entity reports them in its `fan_modes` attribute.
        vol.Required(CONF_SOURCE_FAN_MODES): vol.All(
            cv.ensure_list, [cv.string], vol.Length(min=1)
        ),
        vol.Optional(
            CONF_HVAC_MODES, default=[HVACMode.HEAT, HVACMode.COOL]
        ): vol.All(
            cv.ensure_list,
            [vol.In([HVACMode.HEAT, HVACMode.COOL])],
            vol.Length(min=1),
        ),
        # Setpoints sent to the source while actively heating / cooling.
        # Default: the source's own max_temp / min_temp.
        vol.Optional(CONF_HEAT_SETPOINT): _FLOAT,
        vol.Optional(CONF_COOL_SETPOINT): _FLOAT,
        # One source fan level per `fan_step` degrees of demand.
        vol.Optional(CONF_FAN_STEP, default=0.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.1)
        ),
        # Extra margin needed before the fan steps *down* (prevents flapping).
        vol.Optional(CONF_FAN_HYSTERESIS, default=0.2): _NON_NEG_FLOAT,
        # Proxy fan modes and how many source levels each one shifts by.
        vol.Optional(CONF_FAN_MODE_OFFSETS, default=DEFAULT_FAN_OFFSETS): vol.All(
            {cv.string: vol.Coerce(int)}, vol.Length(min=1)
        ),
        vol.Optional(CONF_IDLE_ACTION, default=IDLE_TURN_OFF): vol.In(
            [IDLE_TURN_OFF, IDLE_FAN_ONLY, IDLE_HOLD_SETPOINT]
        ),
    }
)

SWITCH_SOURCE_SCHEMA = _COMMON_SCHEMA.extend(
    {
        vol.Required(CONF_SOURCE): cv.entity_domain(SWITCH_DOMAINS),
        # What the device does while it is switched on.
        vol.Optional(CONF_HVAC_MODE, default=HVACMode.HEAT): vol.In(
            [HVACMode.HEAT, HVACMode.COOL]
        ),
    }
)

_SOURCE_DOMAIN_SCHEMA = vol.Schema(
    {vol.Required(CONF_SOURCE): cv.entity_domain(["climate", *SWITCH_DOMAINS])},
    extra=vol.ALLOW_EXTRA,
)


def _validate_platform(config: ConfigType) -> ConfigType:
    """Validate with the schema that matches the source entity's domain."""
    config = _SOURCE_DOMAIN_SCHEMA(config)
    if split_entity_id(config[CONF_SOURCE])[0] == "climate":
        config = CLIMATE_SOURCE_SCHEMA(config)
        modes = config[CONF_HVAC_MODES]
    else:
        config = SWITCH_SOURCE_SCHEMA(config)
        modes = [config[CONF_HVAC_MODE]]
    fallback = config.get(CONF_AUTO_FALLBACK)
    if fallback not in (None, HVACMode.OFF) and fallback not in modes:
        raise vol.Invalid(
            f"{CONF_AUTO_FALLBACK} {fallback} is not one of the entity's modes {modes}"
        )
    if fallback is not None and CONF_OPTIMIZER_URL not in config:
        raise vol.Invalid(f"{CONF_AUTO_FALLBACK} needs {CONF_OPTIMIZER_URL}")
    return config


PLATFORM_SCHEMA = vol.All(_validate_platform)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up a proxy climate entity from YAML."""
    if split_entity_id(config[CONF_SOURCE])[0] == "climate":
        async_add_entities([ProxyClimate(hass, config)])
    else:
        async_add_entities([SwitchProxyClimate(hass, config)])


class BaseProxyClimate(ClimateEntity, RestoreEntity):
    """Thermostat logic shared by every source type.

    Tracks the room temperature and decides when a heating/cooling cycle starts
    and stops; subclasses decide what that means for the source.
    """

    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, config: ConfigType, modes: list[HVACMode]
    ) -> None:
        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = config.get(CONF_UNIQUE_ID)
        self._attr_temperature_unit = hass.config.units.temperature_unit

        self._source: str = config[CONF_SOURCE]
        self._source_domain = split_entity_id(self._source)[0]
        self._sensor: str = config[CONF_SENSOR]
        self._modes = modes
        self._start_offset: float = config[CONF_START_OFFSET]
        self._stop_offset: float = config[CONF_STOP_OFFSET]
        self._min_cycle: timedelta = config[CONF_MIN_CYCLE]
        self._interval: timedelta = config[CONF_INTERVAL]

        self._optimizer_url: str | None = config.get(CONF_OPTIMIZER_URL)
        self._optimizer_token: str | None = config.get(CONF_OPTIMIZER_TOKEN)
        self._optimizer_timeout: timedelta = config[CONF_OPTIMIZER_TIMEOUT]
        self._auto_fallback = HVACMode(config.get(CONF_AUTO_FALLBACK) or self._modes[0])

        self._attr_hvac_modes = [HVACMode.OFF, *self._modes]
        if self._optimizer_url:
            self._attr_hvac_modes.append(HVACMode.AUTO)
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = config[CONF_TARGET_TEMP]
        self._attr_min_temp = config[CONF_MIN_TEMP]
        self._attr_max_temp = config[CONF_MAX_TEMP]
        self._attr_target_temperature_step = config[CONF_TEMP_STEP]
        self._attr_current_temperature = None

        self._last_on_mode: HVACMode = self._modes[0]
        self._active = False  # True while heating/cooling (inside the deadband cycle)
        self._last_switch: datetime | None = None
        self._demand: float | None = None
        self._pending_off = False  # turn-off that hasn't reached the source yet
        self._started = False
        self._lock = asyncio.Lock()

        # Auto mode: the mode the source is actually run in, and the last
        # answer from the optimizer.
        self._run_mode: HVACMode | None = None
        self._command: dict[str, Any] | None = None
        self._command_until: datetime | None = None
        self._optimizer_state: str | None = None
        self._optimizer_reason: str | None = None

    # ------------------------------------------------------------------ setup

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to updates."""
        await super().async_added_to_hass()

        if (old := await self.async_get_last_state()) is not None:
            self._restore(old)
        if self._attr_hvac_mode != HVACMode.OFF:
            self._last_on_mode = self._attr_hvac_mode

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._sensor], self._async_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source], self._async_source_changed
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_tick, self._interval, cancel_on_shutdown=True
            )
        )
        self.async_on_remove(async_at_started(self.hass, self._async_startup))

    def _restore(self, old: State) -> None:
        if old.state in self._attr_hvac_modes:
            self._attr_hvac_mode = HVACMode(old.state)
        if (temp := old.attributes.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = float(temp)
        if (last := old.attributes.get(ATTR_LAST_ON_MODE)) in self._modes:
            self._last_on_mode = HVACMode(last)
        # Resume an in-progress cycle instead of waiting for the start threshold.
        action = old.attributes.get(ATTR_HVAC_ACTION)
        self._active = action in (HVACAction.HEATING, HVACAction.COOLING)
        if self._active:
            run = HVACMode.HEAT if action == HVACAction.HEATING else HVACMode.COOL
            self._run_mode = run if run in self._modes else None

    async def _async_startup(self, _hass: HomeAssistant) -> None:
        self._attr_current_temperature = self._read_temperature(
            self.hass.states.get(self._sensor)
        )
        self._started = True
        if self._attr_hvac_mode == HVACMode.AUTO:
            await self._async_fetch_command()
        await self._async_control()

    # -------------------------------------------------------------- listeners

    async def _async_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        self._attr_current_temperature = self._read_temperature(
            event.data["new_state"]
        )
        await self._async_control()

    async def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        came_back = (
            new is not None
            and new.state != STATE_UNAVAILABLE
            and (old is None or old.state == STATE_UNAVAILABLE)
        )
        if came_back:
            await self._async_control()

    async def async_update(self) -> None:
        """homeassistant.update_entity: in auto, ask the optimizer now rather than
        at the next tick, and act on it. The optimizer uses it to start what someone
        asked for at once."""
        if self._started and self._attr_hvac_mode == HVACMode.AUTO:
            await self._async_fetch_command()
            await self._async_control()

    async def _async_tick(self, _now: datetime) -> None:
        # The optimizer is asked on the tick only; sensor updates in between
        # reuse its last answer.
        if self._attr_hvac_mode == HVACMode.AUTO:
            await self._async_fetch_command()
        await self._async_control()

    @staticmethod
    def _read_temperature(state: State | None) -> float | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        raw = (
            state.attributes.get(ATTR_CURRENT_TEMPERATURE)
            if state.domain == "climate"
            else state.state
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------- control logic

    async def _async_control(self, force: bool = False) -> None:
        """Re-evaluate and push the desired state to the source."""
        if not self._started:
            return
        async with self._lock:
            await self._async_control_locked(force)
        self.async_write_ha_state()

    async def _async_control_locked(self, force: bool) -> None:
        mode = self._attr_hvac_mode

        if mode == HVACMode.OFF:
            self._set_active(False)
            self._demand = None
            # Only switch the source off when the user turns the proxy off
            # (retried until the source is reachable); afterwards the source
            # is left alone (remote etc. still works).
            if force or self._pending_off:
                self._pending_off = not await self._async_source_off()
            return

        if mode == HVACMode.AUTO:
            if (command := self._valid_command()) is not None:
                # force: someone asked for this run, past min_cycle_duration
                await self._async_follow(command, force or command.get("force") is True)
                return
            # No fresh answer: the built-in thermostat takes over.
            mode = self._auto_fallback
            if mode == HVACMode.OFF:
                await self._async_stop_or_hold(force)
                return
            if self._run_mode not in (None, mode) and self._active:
                self._set_active(False)
            self._run_mode = mode
        else:
            self._run_mode = mode

        cur = self._attr_current_temperature
        target = self._attr_target_temperature
        if cur is None or target is None:
            if self._active:
                _LOGGER.warning(
                    "%s: no temperature from %s, going idle", self.entity_id, self._sensor
                )
            self._set_active(False)
            self._demand = None
            await self._async_source_idle(mode)
            return

        # Demand > 0 means the room needs conditioning in the current mode.
        demand = target - cur if mode == HVACMode.HEAT else cur - target
        self._demand = round(demand, 2)

        if not self._active and demand >= self._start_offset:
            if force or self._cycle_allowed():
                self._set_active(True)
        elif self._active and demand <= -self._stop_offset:
            if force or self._cycle_allowed():
                self._set_active(False)

        if self._active:
            await self._async_source_active(mode, demand)
        else:
            await self._async_source_idle(mode)

    # -------------------------------------------------------------- auto mode

    def _valid_command(self) -> dict[str, Any] | None:
        if self._command is None or self._command_until is None:
            return None
        if dt_util.utcnow() > self._command_until:
            if self._optimizer_state == OPT_FOLLOWING:
                _LOGGER.warning(
                    "%s: optimizer answer expired, using the built-in thermostat",
                    self.entity_id,
                )
            self._optimizer_state = OPT_STALE
            self._command = None
            return None
        return self._command

    async def _async_follow(self, command: dict[str, Any], force: bool) -> None:
        """Do what the optimizer asked for, within min_cycle_duration."""
        want = command["hvac_mode"]
        if want == HVACMode.OFF:
            await self._async_stop_or_hold(force)
            return
        run = HVACMode(want)
        if self._active and self._run_mode != run:
            # heat <-> cool: the running cycle belongs to the other mode
            self._set_active(False)
        self._run_mode = run
        if not self._active and (force or self._cycle_allowed()):
            self._set_active(True)
        if self._active:
            await self._async_source_command(run, command)
        else:
            await self._async_source_idle(run)

    async def _async_stop_or_hold(self, force: bool) -> None:
        """Stop the source, unless min_cycle_duration says it has to keep running."""
        run = self._run_mode or self._modes[0]
        if self._active and not (force or self._cycle_allowed()):
            await self._async_source_command(run, self._command or {})
            return
        self._set_active(False)
        self._demand = None
        await self._async_source_idle(run)

    def _command_request(self) -> dict[str, Any]:
        """What the optimizer gets to know about this entity on every tick."""
        return {
            "entity_id": self.entity_id,
            "name": self._attr_name,
            "source": self._source,
            "source_domain": self._source_domain,
            "sensor": self._sensor,
            "modes": [str(m) for m in self._modes],
            "current_temperature": self._attr_current_temperature,
            "target_temperature": self._attr_target_temperature,
            "running": self._is_running(),
            "run_mode": str(self._run_mode) if self._run_mode else None,
            "last_switch": self._last_switch.isoformat() if self._last_switch else None,
            "min_cycle_s": self._min_cycle.total_seconds(),
            "control_interval_s": self._interval.total_seconds(),
        }

    async def _async_fetch_command(self) -> None:
        """Ask the optimizer what to do until its next answer."""
        assert self._optimizer_url
        url = self._optimizer_url.rstrip("/") + COMMAND_PATH
        headers = {}
        if self._optimizer_token:
            headers["Authorization"] = f"Bearer {self._optimizer_token}"
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(self._optimizer_timeout.total_seconds()):
                resp = await session.post(url, json=self._command_request(), headers=headers)
                resp.raise_for_status()
                data = await resp.json()
            command = self._parse_command(data)
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as err:
            # The last answer still holds until it expires, so a single lost
            # request doesn't hand control back to the thermostat.
            if self._optimizer_state != OPT_UNREACHABLE:
                _LOGGER.warning(
                    "%s: no usable answer from the optimizer at %s (%r), "
                    "keeping its last answer until it expires",
                    self.entity_id,
                    url,
                    err,
                )
            self._optimizer_state = OPT_UNREACHABLE
            return
        self._optimizer_reason = data.get("reason")
        if command is None:
            self._optimizer_state = OPT_DEFERRED
            self._command = None
            return
        self._optimizer_state = OPT_FOLLOWING
        self._command, self._command_until = command

    def _parse_command(self, data: Any) -> tuple[dict[str, Any], datetime] | None:
        """Check the answer; None means the optimizer leaves the decision to us."""
        if not isinstance(data, dict):
            raise ValueError("answer is not a JSON object")
        if data.get("follow", True) is False:
            return None
        mode = data.get("hvac_mode")
        if mode != HVACMode.OFF and mode not in self._modes:
            raise ValueError(f"hvac_mode {mode!r} is not one of {self._modes}")
        until = None
        if (raw := data.get("valid_until")) is not None:
            until = dt_util.parse_datetime(str(raw))
            if until is None:
                raise ValueError(f"valid_until {raw!r} is not a timestamp")
        if until is None:
            until = dt_util.utcnow() + 2 * self._interval
        return dict(data, hvac_mode=HVACMode(mode)), until

    def _set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self._last_switch = dt_util.utcnow()
        _LOGGER.debug(
            "%s: %s (room %s, target %s)",
            self.entity_id,
            "start" if active else "stop",
            self._attr_current_temperature,
            self._attr_target_temperature,
        )

    def _cycle_allowed(self) -> bool:
        if not self._min_cycle or self._last_switch is None:
            return True
        return dt_util.utcnow() - self._last_switch >= self._min_cycle

    # --------------------------------------------------------- source control

    async def _async_source_off(self) -> bool:
        """Switch the source off because the user turned the proxy off.

        Returns False if the source couldn't be reached, so it gets retried.
        """
        raise NotImplementedError

    async def _async_source_idle(self, mode: HVACMode) -> None:
        """Keep the source idle while the proxy waits in `mode`."""
        raise NotImplementedError

    async def _async_source_active(self, mode: HVACMode, demand: float) -> None:
        """Run the source in `mode` for the given demand."""
        raise NotImplementedError

    async def _async_source_command(self, mode: HVACMode, command: dict[str, Any]) -> None:
        """Run the source in `mode` as the optimizer asked."""
        raise NotImplementedError

    def _is_running(self) -> bool:
        """Whether the source is heating/cooling right now."""
        return self._active

    async def _async_call(self, service: str, data: dict[str, Any]) -> bool:
        domain = self._source_domain
        _LOGGER.debug("%s -> %s.%s %s", self.entity_id, domain, service, data)
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001 - keep the control loop alive
            _LOGGER.warning(
                "%s: %s.%s on %s failed: %s",
                self.entity_id,
                domain,
                service,
                self._source,
                err,
            )
            return False
        return True

    # ------------------------------------------------------------ entity API

    @property
    def hvac_action(self) -> HVACAction:
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if not self._is_running():
            return HVACAction.IDLE
        if self._run_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        return HVACAction.COOLING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "demand": self._demand,
            ATTR_LAST_ON_MODE: self._last_on_mode,
        }
        if self._attr_hvac_mode == HVACMode.AUTO:
            attrs["optimizer"] = self._optimizer_state
            attrs["optimizer_reason"] = self._optimizer_reason
            attrs[ATTR_RUN_MODE] = self._run_mode
        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._check_hvac_mode(hvac_mode)
        changed = hvac_mode != self._attr_hvac_mode
        if changed:
            # A running cycle belongs to the previous mode.
            self._set_active(False)
            self._run_mode = None
        self._attr_hvac_mode = hvac_mode
        if hvac_mode in self._modes:
            self._last_on_mode = hvac_mode
        if hvac_mode == HVACMode.AUTO and changed and self._started:
            await self._async_fetch_command()
        # Re-sending the current mode must not skip min_cycle_duration, but
        # "off" always goes through so it also stops a source switched on by hand.
        await self._async_control(force=changed or hvac_mode == HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            # HA doesn't validate this hvac_mode; check it before the target
            # changes so a rejected call has no effect at all.
            self._check_hvac_mode(HVACMode(mode))
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = float(temp)
        if mode is not None:
            await self.async_set_hvac_mode(HVACMode(mode))
        else:
            await self._async_control()

    def _check_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self._attr_hvac_modes:
            raise ServiceValidationError(
                f"{self.entity_id} does not support hvac mode {hvac_mode} "
                f"(supported: {', '.join(self._attr_hvac_modes)})"
            )

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(self._last_on_mode)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)


class ProxyClimate(BaseProxyClimate):
    """Climate entity that drives another climate entity through its fan speed."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        super().__init__(hass, config, [HVACMode(m) for m in config[CONF_HVAC_MODES]])

        self._source_fan_modes: list[str] = config[CONF_SOURCE_FAN_MODES]
        self._heat_sp: float | None = config.get(CONF_HEAT_SETPOINT)
        self._cool_sp: float | None = config.get(CONF_COOL_SETPOINT)
        self._fan_step: float = config[CONF_FAN_STEP]
        self._fan_hyst: float = config[CONF_FAN_HYSTERESIS]
        self._fan_offsets: dict[str, int] = dict(config[CONF_FAN_MODE_OFFSETS])
        self._idle_action: str = config[CONF_IDLE_ACTION]

        self._attr_fan_modes = list(self._fan_offsets)
        self._attr_fan_mode = next(
            (m for m, off in self._fan_offsets.items() if off == 0),
            self._attr_fan_modes[0],
        )

        self._base_level: int | None = None  # source fan level before offset
        self._source_fan: str | None = None

    # ------------------------------------------------------------------ setup

    def _restore(self, old: State) -> None:
        super()._restore(old)
        if (fan := old.attributes.get(ATTR_FAN_MODE)) in self._fan_offsets:
            self._attr_fan_mode = fan

    async def _async_startup(self, _hass: HomeAssistant) -> None:
        self._check_source_fan_modes()
        await super()._async_startup(_hass)

    def _check_source_fan_modes(self) -> None:
        state = self.hass.states.get(self._source)
        available = state.attributes.get(ATTR_FAN_MODES) if state else None
        if not available:
            return
        missing = [m for m in self._source_fan_modes if m not in available]
        if missing:
            _LOGGER.warning(
                "%s: source_fan_modes %s not offered by %s (it offers %s)",
                self.entity_id,
                missing,
                self._source,
                available,
            )

    # ---------------------------------------------------------- control logic

    def _set_active(self, active: bool) -> None:
        super()._set_active(active)
        if not active:
            self._base_level = None

    def _fan_level(self, demand: float) -> int:
        """Map demand to an index into source_fan_modes.

        One level per `fan_step` degrees of demand. Stepping up happens as soon
        as the boundary is crossed; stepping down needs an extra
        `fan_hysteresis` so a sensor jittering on a boundary doesn't flap the fan.
        The proxy fan mode then shifts the result by its configured offset.
        """
        top = len(self._source_fan_modes) - 1
        eps = 1e-6
        up = math.floor(max(demand, 0.0) / self._fan_step + eps)
        down = math.floor(max(demand + self._fan_hyst, 0.0) / self._fan_step + eps)

        if self._base_level is None or up > self._base_level:
            self._base_level = up
        elif down < self._base_level:
            self._base_level = down
        self._base_level = max(0, min(self._base_level, top))

        offset = self._fan_offsets.get(self._attr_fan_mode or "", 0)
        return max(0, min(self._base_level + offset, top))

    # --------------------------------------------------------- source control

    async def _async_source_off(self) -> bool:
        self._source_fan = None
        return await self._async_apply_source(HVACMode.OFF)

    async def _async_source_active(self, mode: HVACMode, demand: float) -> None:
        level = self._fan_level(demand)
        self._source_fan = self._source_fan_modes[level]
        await self._async_apply_source(
            mode, self._running_setpoint(mode), self._source_fan
        )

    async def _async_source_command(self, mode: HVACMode, command: dict[str, Any]) -> None:
        top = len(self._source_fan_modes) - 1
        level = command.get("fan_level")
        if isinstance(level, bool) or not isinstance(level, int):
            # no usable level: keep the one running, else start at the quietest
            level = self._base_level if self._base_level is not None else 0
        level = max(0, min(level, top))
        self._base_level = level
        self._source_fan = self._source_fan_modes[level]
        await self._async_apply_source(mode, self._running_setpoint(mode), self._source_fan)

    def _command_request(self) -> dict[str, Any]:
        return {
            **super()._command_request(),
            "source_fan_modes": self._source_fan_modes,
            "source_fan_mode": self._source_fan,
            "fan_mode": self._attr_fan_mode,
            "fan_mode_offset": self._fan_offsets.get(self._attr_fan_mode or "", 0),
            "fan_modes": self._attr_fan_modes,
            "idle_action": self._idle_action,
        }

    async def _async_source_idle(self, mode: HVACMode) -> None:
        quietest = self._source_fan_modes[0]
        if self._idle_action == IDLE_TURN_OFF:
            self._source_fan = None
            await self._async_apply_source(HVACMode.OFF)
        elif self._idle_action == IDLE_FAN_ONLY:
            self._source_fan = quietest
            await self._async_apply_source(HVACMode.FAN_ONLY, fan_mode=quietest)
        else:
            # Stay in heat/cool but aim the setpoint the "wrong" way so the
            # unit idles on its own sensor without a full power-off.
            self._source_fan = quietest
            setpoint = (
                self._source_float("min_temp", 16.0)
                if mode == HVACMode.HEAT
                else self._source_float("max_temp", 30.0)
            )
            await self._async_apply_source(mode, setpoint, quietest)

    def _source_float(self, attr: str, fallback: float) -> float:
        state = self.hass.states.get(self._source)
        try:
            return float(state.attributes.get(attr))  # type: ignore[union-attr, arg-type]
        except (AttributeError, TypeError, ValueError):
            return fallback

    def _running_setpoint(self, mode: HVACMode) -> float:
        if mode == HVACMode.HEAT:
            return self._heat_sp if self._heat_sp is not None else self._source_float("max_temp", 30.0)
        return self._cool_sp if self._cool_sp is not None else self._source_float("min_temp", 16.0)

    async def _async_apply_source(
        self,
        hvac_mode: HVACMode,
        temperature: float | None = None,
        fan_mode: str | None = None,
    ) -> bool:
        """Send only the commands whose value differs from the source's state.

        Returns whether the source was reachable and every command succeeded.
        """
        state = self.hass.states.get(self._source)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        base = {ATTR_ENTITY_ID: self._source}
        ok = True

        if state.state != hvac_mode:
            ok &= await self._async_call(
                "set_hvac_mode", {**base, ATTR_HVAC_MODE: hvac_mode}
            )

        if temperature is not None:
            current_sp = state.attributes.get(ATTR_TEMPERATURE)
            try:
                same = abs(float(current_sp) - temperature) < 0.01
            except (TypeError, ValueError):
                same = False
            if not same:
                ok &= await self._async_call(
                    "set_temperature", {**base, ATTR_TEMPERATURE: temperature}
                )

        if fan_mode is not None and state.attributes.get(ATTR_FAN_MODE) != fan_mode:
            ok &= await self._async_call(
                "set_fan_mode", {**base, ATTR_FAN_MODE: fan_mode}
            )
        return ok

    # ------------------------------------------------------------ entity API

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**super().extra_state_attributes, "source_fan_mode": self._source_fan}

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        await self._async_control()


class SwitchProxyClimate(BaseProxyClimate):
    """Climate entity that switches a plain on/off heater or cooler."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        super().__init__(hass, config, [HVACMode(config[CONF_HVAC_MODE])])

    async def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        await super()._async_source_changed(event)
        self.async_write_ha_state()  # hvac_action follows the switch

    async def _async_source_off(self) -> bool:
        return await self._async_switch(False)

    async def _async_source_active(self, mode: HVACMode, demand: float) -> None:
        await self._async_switch(True)

    async def _async_source_command(self, mode: HVACMode, command: dict[str, Any]) -> None:
        await self._async_switch(True)

    async def _async_source_idle(self, mode: HVACMode) -> None:
        await self._async_switch(False)

    async def _async_switch(self, on: bool) -> bool:
        """Switch the source unless it is already in that state.

        Returns whether the source was reachable and the command succeeded.
        """
        state = self.hass.states.get(self._source)
        if state is None or state.state == STATE_UNAVAILABLE:
            return False
        # An unknown state still gets the command, so a heater whose plug
        # hasn't reported yet isn't left running.
        if state.state == (STATE_ON if on else STATE_OFF):
            return True
        return await self._async_call(
            SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: self._source},
        )

    def _is_running(self) -> bool:
        # Report what the switch is actually doing, not what the proxy asked
        # for, so an unreachable plug or a failed command shows up as idle.
        state = self.hass.states.get(self._source)
        return state is not None and state.state == STATE_ON
