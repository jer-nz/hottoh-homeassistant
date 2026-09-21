"""Thermostat logic on an external temperature sensor, without Home Assistant objects."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .const import (
    COMPENSATION_MIN_DELTA,
    COMPENSATION_MIN_INTERVAL_S,
    STOPPING_STATES,
    is_alarm,
)


@dataclass(slots=True, frozen=True)
class OnOffSettings:
    """Hysteresis around the target and minimum durations, in °C and seconds."""

    hysteresis_low: float
    hysteresis_high: float
    min_on_s: float
    min_off_s: float


@dataclass(slots=True, frozen=True)
class StoveSnapshot:
    """What the regulation needs from the stove."""

    on: bool
    state_raw: int


def lock_remaining(
    stove_on: bool, last_change: float | None, now: float, settings: OnOffSettings
) -> float:
    """Seconds before the stove may be switched again (0 when it may)."""
    if last_change is None:
        return 0.0
    minimum = settings.min_on_s if stove_on else settings.min_off_s
    return max(0.0, minimum - (now - last_change))


def on_off_decision(
    target: float,
    room_temp: float | None,
    stove: StoveSnapshot,
    last_change: float | None,
    now: float,
    settings: OnOffSettings,
) -> bool | None:
    """True to turn the stove on, False to turn it off, None to leave it.

    - on at or below `target - hysteresis_low`, off at or above `target + hysteresis_high`;
    - the stove stays on at least `min_on_s` and off at least `min_off_s` after the last
      change (`last_change`, None when unknown);
    - never turned on while it is still stopping (Stopping1/2), nor touched during an alarm.
    """
    if room_temp is None or math.isnan(room_temp) or is_alarm(stove.state_raw):
        return None
    if lock_remaining(stove.on, last_change, now, settings) > 0:
        return None
    if stove.on:
        if room_temp >= target + settings.hysteresis_high:
            return False
        return None
    if stove.state_raw in STOPPING_STATES:
        return None
    if room_temp <= target - settings.hysteresis_low:
        return True
    return None


def compensated_setpoint(
    target: float,
    room_temp: float,
    stove_temp: float,
    setpoint_min: float,
    setpoint_max: float,
) -> float:
    """Stove set point so that the stove probe regulates the room at `target`.

    The stove probe reads `stove_temp - room_temp` more than the room: shift the set point
    by the same amount, within the limits of the stove.
    """
    value = round(target + (stove_temp - room_temp), 1)
    return min(max(value, setpoint_min), setpoint_max)


def should_write_setpoint(
    new: float,
    current: float,
    last_write: float | None,
    now: float,
    force: bool = False,
) -> bool:
    """Writes only meaningful changes, and at most every few minutes unless forced."""
    if abs(new - current) < COMPENSATION_MIN_DELTA:
        return False
    if force or last_write is None:
        return True
    return now - last_write >= COMPENSATION_MIN_INTERVAL_S
