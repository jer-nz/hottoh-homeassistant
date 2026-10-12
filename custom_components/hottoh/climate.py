"""Thermostat of a HottoH stove, on the stove probe or on an external sensor."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    COMPENSATION_MIN_DELTA,
    CONF_DISABLE_CHRONO,
    CONF_HYSTERESIS_HIGH,
    CONF_HYSTERESIS_LOW,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_ON_MINUTES,
    CONF_ONOFF_STOVE_SETPOINT,
    CONF_REGULATION_MODE,
    CONF_SENSOR_TIMEOUT_MINUTES,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_HYSTERESIS_HIGH,
    DEFAULT_HYSTERESIS_LOW,
    DEFAULT_MIN_OFF_MINUTES,
    DEFAULT_MIN_ON_MINUTES,
    DEFAULT_ONOFF_STOVE_SETPOINT,
    DEFAULT_SENSOR_TIMEOUT_MINUTES,
    EVENT_SENSOR_UNAVAILABLE,
    EVENT_SETPOINT_CHANGED,
    REGULATION_COMPENSATED,
    REGULATION_ON_OFF,
    STARTING_STATES,
    STOPPING_STATES,
    is_alarm,
)
from .coordinator import HottohCoordinator
from .entity import HottohEntity, async_write
from .regulator import (
    OnOffSettings,
    StoveSnapshot,
    compensated_setpoint,
    lock_remaining,
    on_off_decision,
    should_write_setpoint,
)

if TYPE_CHECKING:
    from . import HottohConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

EXTERNAL_MIN_TEMP = 5.0
EXTERNAL_MAX_TEMP = 35.0
DEFAULT_TARGET = 20.0
# retry delay of a takeover write (chrono off, stove set point) that failed
TAKEOVER_RETRY_S = 300
# the stove applies a set point a few seconds after its `OK`: a different value seen sooner is
# the old one, not a change made on the stove
SETPOINT_SETTLE_S = 15
SETPOINT_TOLERANCE = 0.05
ATTR_LAST_SWITCH = "last_switch"
ATTR_ALARM_HOLD = "alarm_hold"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the thermostat."""
    async_add_entities([HottohClimate(entry.runtime_data.coordinator)])


class HottohClimate(HottohEntity, ClimateEntity, RestoreEntity):
    """Thermostat of the stove."""

    _attr_name = None
    _attr_translation_key = "thermostat"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.1

    def __init__(self, coordinator: HottohCoordinator) -> None:
        super().__init__(coordinator, "thermostat")
        options = coordinator.config_entry.options
        self._sensor: str | None = options.get(CONF_TEMPERATURE_SENSOR) or None
        self._mode: str = options.get(CONF_REGULATION_MODE, REGULATION_ON_OFF)
        self._settings = OnOffSettings(
            hysteresis_low=float(
                options.get(CONF_HYSTERESIS_LOW, DEFAULT_HYSTERESIS_LOW)
            ),
            hysteresis_high=float(
                options.get(CONF_HYSTERESIS_HIGH, DEFAULT_HYSTERESIS_HIGH)
            ),
            min_on_s=float(options.get(CONF_MIN_ON_MINUTES, DEFAULT_MIN_ON_MINUTES))
            * 60,
            min_off_s=float(options.get(CONF_MIN_OFF_MINUTES, DEFAULT_MIN_OFF_MINUTES))
            * 60,
        )
        self._onoff_setpoint = float(
            options.get(CONF_ONOFF_STOVE_SETPOINT, DEFAULT_ONOFF_STOVE_SETPOINT)
        )
        self._disable_chrono: bool = options.get(CONF_DISABLE_CHRONO, True)
        self._sensor_timeout_s = (
            float(
                options.get(CONF_SENSOR_TIMEOUT_MINUTES, DEFAULT_SENSOR_TIMEOUT_MINUTES)
            )
            * 60
        )
        # external sensor: target and mode kept by Home Assistant
        self._target: float = DEFAULT_TARGET
        self._hvac_mode = HVACMode.OFF
        self._room_temp: float | None = None
        self._sensor_lost_since: float | None = None
        self._sensor_lost_notified = False
        # last observed change of `index_stove_on` (UTC timestamp)
        self._last_switch: float | None = None
        self._stove_on: bool | None = None
        self._last_setpoint_write: float | None = None
        # stove set point last written by Home Assistant, or first seen: any other value was
        # set on the control panel of the stove (or by another app) and becomes the target
        self._expected_setpoint: float | None = None
        self._last_chrono_write: float | None = None
        self._force_setpoint = True
        # after an alarm (failed ignition, no pellets...), the stove is not turned on again
        # until someone asks for heat or turns it on
        self._alarm_hold = False
        self._lock = asyncio.Lock()

    @property
    def _external(self) -> bool:
        return self._sensor is not None

    async def async_added_to_hass(self) -> None:
        """Restores the target and follows the external sensor."""
        await super().async_added_to_hass()
        self._observe_stove()
        if not self._external:
            return
        if (last := await self.async_get_last_state()) is not None:
            if last.state in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(last.state)
            if (target := last.attributes.get(ATTR_TEMPERATURE)) is not None:
                self._target = float(target)
            if (switch := last.attributes.get(ATTR_LAST_SWITCH)) and (
                parsed := dt_util.parse_datetime(switch)
            ):
                self._last_switch = parsed.timestamp()
            self._alarm_hold = bool(last.attributes.get(ATTR_ALARM_HOLD))
        elif self.coordinator.data.dat0.get("index_stove_on"):
            self._hvac_mode = HVACMode.HEAT
        self._update_room_temp(self.hass.states.get(self._sensor))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._sensor], self._async_sensor_changed
            )
        )
        self._schedule_regulation()

    # --- state -------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Room temperature."""
        if self._external:
            return self._room_temp
        return self.dat0.get("index_ambient_t1")

    @property
    def target_temperature(self) -> float | None:
        """Target of the room."""
        if self._external:
            return self._target
        return self.dat0.get("index_ambient_t1_set")

    @property
    def min_temp(self) -> float:
        """Lowest target."""
        if self._external:
            return EXTERNAL_MIN_TEMP
        return float(self.dat0.get("index_ambient_t1_set_min", EXTERNAL_MIN_TEMP))

    @property
    def max_temp(self) -> float:
        """Highest target."""
        if self._external:
            return EXTERNAL_MAX_TEMP
        return float(self.dat0.get("index_ambient_t1_set_max", EXTERNAL_MAX_TEMP))

    @property
    def hvac_mode(self) -> HVACMode:
        """Heat when the stove is on (or the regulation active)."""
        if self._external:
            return self._hvac_mode
        return HVACMode.HEAT if self.dat0.get("index_stove_on") else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """What the stove is doing."""
        state = self.coordinator.data.state_raw
        if state is None:
            return None
        if state in STARTING_STATES:
            return HVACAction.PREHEATING
        if state == 8:
            return HVACAction.HEATING
        if state == 0 and self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        # stopping, eco stop, alarm, anti-freeze...
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Regulation details."""
        attrs: dict[str, Any] = {
            "stove_temperature": self.dat0.get("index_ambient_t1"),
            "stove_setpoint": self.dat0.get("index_ambient_t1_set"),
        }
        if not self._external:
            return attrs
        attrs["temperature_sensor"] = self._sensor
        attrs["regulation_mode"] = self._mode
        attrs[ATTR_ALARM_HOLD] = self._alarm_hold
        if self._last_switch is not None:
            attrs[ATTR_LAST_SWITCH] = dt_util.utc_from_timestamp(
                self._last_switch
            ).isoformat()
        if self._mode == REGULATION_ON_OFF:
            remaining = lock_remaining(
                bool(self.dat0.get("index_stove_on")),
                self._last_switch,
                dt_util.utcnow().timestamp(),
                self._settings,
            )
            attrs["lock_remaining_min"] = round(remaining / 60, 1)
        return attrs

    # --- commands ----------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """New target (and optionally mode)."""
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(mode)
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if not self._external:
            await async_write(
                self.coordinator,
                self.client.set_ambiance_temperature(1, float(temperature)),
            )
            return
        self._target = float(temperature)
        self._force_setpoint = True
        self.async_write_ha_state()
        await self._async_regulate()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Heat or off. Off stops the stove at once, whatever the minimum durations."""
        if not self._external:
            await async_write(
                self.coordinator, self.client.set_on_off(hvac_mode == HVACMode.HEAT)
            )
            return
        self._hvac_mode = hvac_mode
        self._force_setpoint = True
        self._last_chrono_write = None
        if hvac_mode == HVACMode.HEAT:
            self._alarm_hold = False
        self.async_write_ha_state()
        if hvac_mode == HVACMode.OFF:
            if self.dat0.get("index_stove_on"):
                await self._async_switch(False)
            return
        await self._async_regulate()

    async def async_turn_on(self) -> None:
        """Heat."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    # --- regulation --------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        self._observe_stove()
        super()._handle_coordinator_update()
        self._schedule_regulation()

    @callback
    def _observe_stove(self) -> None:
        data = self.coordinator.data
        if data is None or not data.connected:
            return
        on = bool(data.dat0.get("index_stove_on"))
        if self._stove_on is not None and on != self._stove_on:
            self._last_switch = dt_util.utcnow().timestamp()
            if on:  # turned on by someone else: the alarm was dealt with
                self._alarm_hold = False
        self._stove_on = on
        if self._external and data.state_raw is not None and is_alarm(data.state_raw):
            self._alarm_hold = True
        if self._external:
            self._observe_setpoint(data.dat0)

    @callback
    def _observe_setpoint(self, dat0: dict[str, Any]) -> None:
        """Adopts a set point changed on the stove instead of writing over it.

        Compensated: the target moves by the same amount. On/off: the stove set point is
        meaningless to the person at the stove, the value becomes the target and the stove set
        point is written again (the stove would otherwise stop before the room is warm).
        """
        if (raw := dat0.get("index_ambient_t1_set")) is None or self._lock.locked():
            return
        value = float(raw)
        expected = self._expected_setpoint
        if expected is None:
            self._expected_setpoint = value
            return
        if abs(value - expected) < SETPOINT_TOLERANCE:
            return
        now = dt_util.utcnow().timestamp()
        if (
            self._last_setpoint_write is not None
            and now - self._last_setpoint_write < SETPOINT_SETTLE_S
        ):
            return
        if self._mode == REGULATION_COMPENSATED:
            target = self._target + (value - expected)
        else:
            target = value
            self._force_setpoint = True
        target = round(min(max(target, EXTERNAL_MIN_TEMP), EXTERNAL_MAX_TEMP), 1)
        _LOGGER.info(
            "%s: stove set point changed on the stove (%s -> %s), target %s -> %s",
            self.entity_id,
            expected,
            value,
            self._target,
            target,
        )
        self._expected_setpoint = value
        self._target = target
        if (notifier := self.coordinator.notifier) is not None:
            notifier.emit(
                EVENT_SETPOINT_CHANGED, stove_setpoint=str(value), target=str(target)
            )

    @callback
    def _async_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        self._update_room_temp(event.data["new_state"])
        self.async_write_ha_state()
        self._schedule_regulation()

    @callback
    def _update_room_temp(self, state: Any) -> None:
        value: float | None = None
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            try:
                value = float(state.state)
            except ValueError:
                value = None
        self._room_temp = value
        now = dt_util.utcnow().timestamp()
        if value is not None:
            self._sensor_lost_since = None
            self._sensor_lost_notified = False
        elif self._sensor_lost_since is None:
            self._sensor_lost_since = now

    @callback
    def _schedule_regulation(self) -> None:
        if not self._external or self._lock.locked() or self.hass is None:
            return
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self._async_regulate(), f"{self.entity_id} regulation"
        )

    def _check_sensor_timeout(self, now: float) -> None:
        if (
            self._sensor_lost_since is None
            or self._sensor_lost_notified
            or now - self._sensor_lost_since < self._sensor_timeout_s
        ):
            return
        self._sensor_lost_notified = True
        if (notifier := self.coordinator.notifier) is not None:
            notifier.emit(
                EVENT_SENSOR_UNAVAILABLE,
                entity_id=self._sensor,
                minutes=str(int((now - self._sensor_lost_since) // 60)),
            )

    async def _async_regulate(self) -> None:
        """One regulation pass; writes are serialised."""
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._async_regulate_locked()
            except HomeAssistantError as err:
                _LOGGER.warning("%s: regulation write failed: %s", self.entity_id, err)

    async def _async_regulate_locked(self) -> None:
        data = self.coordinator.data
        if not self._external or data is None or not data.connected:
            return
        now = dt_util.utcnow().timestamp()
        self._check_sensor_timeout(now)
        if self._hvac_mode != HVACMode.HEAT:
            return
        dat0 = data.dat0
        state_raw = data.state_raw or 0
        stove_on = bool(dat0.get("index_stove_on"))
        await self._async_takeover_chrono(dat0, now)

        if self._mode == REGULATION_COMPENSATED:
            if self._room_temp is not None:
                await self._async_write_setpoint(
                    compensated_setpoint(
                        self._target,
                        self._room_temp,
                        float(dat0.get("index_ambient_t1", self._room_temp)),
                        float(dat0.get("index_ambient_t1_set_min", EXTERNAL_MIN_TEMP)),
                        float(dat0.get("index_ambient_t1_set_max", EXTERNAL_MAX_TEMP)),
                    ),
                    dat0,
                    now,
                )
            if (
                not stove_on
                and not self._alarm_hold
                and state_raw not in STOPPING_STATES
                and not is_alarm(state_raw)
            ):
                await self._async_switch(True)
            return

        # on/off: the stove runs flat out on its own set point, Home Assistant switches it
        setpoint = min(
            max(self._onoff_setpoint, float(dat0.get("index_ambient_t1_set_min", 5))),
            float(dat0.get("index_ambient_t1_set_max", EXTERNAL_MAX_TEMP)),
        )
        await self._async_write_setpoint(setpoint, dat0, now)
        decision = on_off_decision(
            self._target,
            self._room_temp,
            StoveSnapshot(on=stove_on, state_raw=state_raw),
            self._last_switch,
            now,
            self._settings,
        )
        if decision is False or (decision is True and not self._alarm_hold):
            await self._async_switch(decision)

    async def _async_takeover_chrono(self, dat0: dict[str, Any], now: float) -> None:
        if not self._disable_chrono or int(dat0.get("index_chrono_mode", 0)) == 0:
            return
        if (
            self._last_chrono_write is not None
            and now - self._last_chrono_write < TAKEOVER_RETRY_S
        ):
            return
        self._last_chrono_write = now
        _LOGGER.info(
            "%s: chrono mode disabled, Home Assistant regulates", self.entity_id
        )
        await async_write(self.coordinator, self.client.set_chrono_mode(False))

    async def _async_write_setpoint(
        self, value: float, dat0: dict[str, Any], now: float
    ) -> None:
        current = float(dat0.get("index_ambient_t1_set", value))
        if not should_write_setpoint(
            value, current, self._last_setpoint_write, now, self._force_setpoint
        ):
            if abs(value - current) < COMPENSATION_MIN_DELTA:
                # already there: a forced write is not owed any more
                self._force_setpoint = False
            return
        self._force_setpoint = False
        self._last_setpoint_write = now
        self._expected_setpoint = value
        _LOGGER.debug("%s: stove set point %s -> %s", self.entity_id, current, value)
        try:
            await async_write(
                self.coordinator, self.client.set_ambiance_temperature(1, value)
            )
        except HomeAssistantError:
            self._expected_setpoint = current
            raise

    async def _async_switch(self, on: bool) -> None:
        _LOGGER.info(
            "%s: turning the stove %s (room %s, target %s)",
            self.entity_id,
            "on" if on else "off",
            self._room_temp,
            self._target,
        )
        self._last_switch = dt_util.utcnow().timestamp()
        await async_write(self.coordinator, self.client.set_on_off(on))
        self.async_write_ha_state()
