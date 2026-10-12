"""Proxy climate entity.

Wraps an existing climate entity (the "source", e.g. an AC / heat pump) and:

* reads the room temperature from a different entity (sensor or climate),
* runs the source at a fixed extreme setpoint while active and modulates its
  output through the source's fan speed, proportional to how far the room is
  from the target, shifted by the proxy's own low/medium/high fan setting,
* uses a deadband (start/stop offsets) plus an optional minimum cycle time so
  the source doesn't switch on and off too often.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
from typing import Any

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
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State
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

IDLE_TURN_OFF = "turn_off"
IDLE_FAN_ONLY = "fan_only"
IDLE_HOLD_SETPOINT = "hold_setpoint"

ATTR_HVAC_MODE = "hvac_mode"
ATTR_FAN_MODE = "fan_mode"
ATTR_FAN_MODES = "fan_modes"
ATTR_HVAC_ACTION = "hvac_action"
ATTR_CURRENT_TEMPERATURE = "current_temperature"
ATTR_LAST_ON_MODE = "last_on_mode"

DEFAULT_FAN_OFFSETS = {"low": -1, "medium": 0, "high": 1}

_FLOAT = vol.Coerce(float)
_NON_NEG_FLOAT = vol.All(vol.Coerce(float), vol.Range(min=0))

PLATFORM_SCHEMA = CLIMATE_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default="Proxy Climate"): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_SOURCE): cv.entity_domain("climate"),
        vol.Required(CONF_SENSOR): cv.entity_id,
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
        # Deadband: start when the room is `start_offset` on the wrong side of
        # the target, stop once it has overshot the target by `stop_offset`.
        vol.Optional(CONF_START_OFFSET, default=0.5): _NON_NEG_FLOAT,
        vol.Optional(CONF_STOP_OFFSET, default=0.5): _NON_NEG_FLOAT,
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
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up a proxy climate entity from YAML."""
    async_add_entities([ProxyClimate(hass, config)])


class ProxyClimate(ClimateEntity, RestoreEntity):
    """Climate entity that drives another climate entity."""

    _attr_should_poll = False
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = config.get(CONF_UNIQUE_ID)
        self._attr_temperature_unit = hass.config.units.temperature_unit

        self._source: str = config[CONF_SOURCE]
        self._sensor: str = config[CONF_SENSOR]
        self._source_fan_modes: list[str] = config[CONF_SOURCE_FAN_MODES]
        self._modes = [HVACMode(m) for m in config[CONF_HVAC_MODES]]
        self._heat_sp: float | None = config.get(CONF_HEAT_SETPOINT)
        self._cool_sp: float | None = config.get(CONF_COOL_SETPOINT)
        self._start_offset: float = config[CONF_START_OFFSET]
        self._stop_offset: float = config[CONF_STOP_OFFSET]
        self._fan_step: float = config[CONF_FAN_STEP]
        self._fan_hyst: float = config[CONF_FAN_HYSTERESIS]
        self._fan_offsets: dict[str, int] = dict(config[CONF_FAN_MODE_OFFSETS])
        self._idle_action: str = config[CONF_IDLE_ACTION]
        self._min_cycle: timedelta = config[CONF_MIN_CYCLE]
        self._interval: timedelta = config[CONF_INTERVAL]

        self._attr_hvac_modes = [HVACMode.OFF, *self._modes]
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_modes = list(self._fan_offsets)
        self._attr_fan_mode = next(
            (m for m, off in self._fan_offsets.items() if off == 0),
            self._attr_fan_modes[0],
        )
        self._attr_target_temperature = config[CONF_TARGET_TEMP]
        self._attr_min_temp = config[CONF_MIN_TEMP]
        self._attr_max_temp = config[CONF_MAX_TEMP]
        self._attr_target_temperature_step = config[CONF_TEMP_STEP]
        self._attr_current_temperature = None

        self._last_on_mode: HVACMode = self._modes[0]
        self._active = False  # True while heating/cooling (inside the deadband cycle)
        self._last_switch: datetime | None = None
        self._base_level: int | None = None  # source fan level before offset
        self._demand: float | None = None
        self._source_fan: str | None = None
        self._started = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to updates."""
        await super().async_added_to_hass()

        if (old := await self.async_get_last_state()) is not None:
            if old.state in self._attr_hvac_modes:
                self._attr_hvac_mode = HVACMode(old.state)
            if (temp := old.attributes.get(ATTR_TEMPERATURE)) is not None:
                self._attr_target_temperature = float(temp)
            if (fan := old.attributes.get(ATTR_FAN_MODE)) in self._fan_offsets:
                self._attr_fan_mode = fan
            if (last := old.attributes.get(ATTR_LAST_ON_MODE)) in self._modes:
                self._last_on_mode = HVACMode(last)
            # Resume an in-progress cycle instead of waiting for the start threshold.
            self._active = old.attributes.get(ATTR_HVAC_ACTION) in (
                HVACAction.HEATING,
                HVACAction.COOLING,
            )
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

    async def _async_startup(self, _hass: HomeAssistant) -> None:
        self._attr_current_temperature = self._read_temperature(
            self.hass.states.get(self._sensor)
        )
        self._check_source_fan_modes()
        self._started = True
        await self._async_control()

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

    async def _async_tick(self, _now: datetime) -> None:
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
            self._source_fan = None
            # Only switch the source off when the user turns the proxy off;
            # afterwards the source is left alone (remote etc. still works).
            if force:
                await self._async_apply_source(HVACMode.OFF)
            return

        cur = self._attr_current_temperature
        target = self._attr_target_temperature
        if cur is None or target is None:
            if self._active:
                _LOGGER.warning(
                    "%s: no temperature from %s, going idle", self.entity_id, self._sensor
                )
            self._set_active(False)
            self._demand = None
            await self._async_apply_idle(mode)
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
            level = self._fan_level(demand)
            self._source_fan = self._source_fan_modes[level]
            await self._async_apply_source(
                mode, self._running_setpoint(mode), self._source_fan
            )
        else:
            await self._async_apply_idle(mode)

    def _set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self._last_switch = dt_util.utcnow()
        if not active:
            self._base_level = None
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

    async def _async_apply_idle(self, mode: HVACMode) -> None:
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

    async def _async_apply_source(
        self,
        hvac_mode: HVACMode,
        temperature: float | None = None,
        fan_mode: str | None = None,
    ) -> None:
        """Send only the commands whose value differs from the source's state."""
        state = self.hass.states.get(self._source)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        base = {ATTR_ENTITY_ID: self._source}

        if state.state != hvac_mode:
            await self._async_call("set_hvac_mode", {**base, ATTR_HVAC_MODE: hvac_mode})

        if temperature is not None:
            current_sp = state.attributes.get(ATTR_TEMPERATURE)
            try:
                same = abs(float(current_sp) - temperature) < 0.01
            except (TypeError, ValueError):
                same = False
            if not same:
                await self._async_call(
                    "set_temperature", {**base, ATTR_TEMPERATURE: temperature}
                )

        if fan_mode is not None and state.attributes.get(ATTR_FAN_MODE) != fan_mode:
            await self._async_call("set_fan_mode", {**base, ATTR_FAN_MODE: fan_mode})

    async def _async_call(self, service: str, data: dict[str, Any]) -> None:
        _LOGGER.debug("%s -> climate.%s %s", self.entity_id, service, data)
        try:
            await self.hass.services.async_call(
                "climate", service, data, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - keep the control loop alive
            _LOGGER.warning(
                "%s: climate.%s on %s failed: %s",
                self.entity_id,
                service,
                self._source,
                err,
            )

    # ------------------------------------------------------------ entity API

    @property
    def hvac_action(self) -> HVACAction:
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if not self._active:
            return HVACAction.IDLE
        if self._attr_hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        return HVACAction.COOLING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "demand": self._demand,
            "source_fan_mode": self._source_fan,
            ATTR_LAST_ON_MODE: self._last_on_mode,
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode != self._attr_hvac_mode:
            # A running cycle belongs to the previous mode.
            self._set_active(False)
        self._attr_hvac_mode = hvac_mode
        if hvac_mode != HVACMode.OFF:
            self._last_on_mode = hvac_mode
        await self._async_control(force=True)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = float(temp)
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(HVACMode(mode))
        else:
            await self._async_control()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        await self._async_control()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(self._last_on_mode)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
