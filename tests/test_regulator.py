"""Thermostat logic."""

from __future__ import annotations

import pytest

from custom_components.hottoh.regulator import (
    OnOffSettings,
    StoveSnapshot,
    compensated_setpoint,
    lock_remaining,
    on_off_decision,
    should_write_setpoint,
)

SETTINGS = OnOffSettings(
    hysteresis_low=1.0, hysteresis_high=1.0, min_on_s=3600, min_off_s=1800
)
OFF = StoveSnapshot(on=False, state_raw=0)
ON = StoveSnapshot(on=True, state_raw=8)


@pytest.mark.parametrize(
    ("room", "stove", "expected"),
    [
        (19.0, OFF, True),  # target - 1: on
        (19.1, OFF, None),  # inside the band
        (20.9, ON, None),
        (21.0, ON, False),  # target + 1: off
        (18.0, ON, None),  # already on
        (22.0, OFF, None),  # already off
        (None, OFF, None),  # sensor unavailable: hold
        (15.0, StoveSnapshot(on=False, state_raw=10), None),  # still stopping
        (15.0, StoveSnapshot(on=False, state_raw=61), None),  # alarm
        (25.0, StoveSnapshot(on=True, state_raw=60), None),  # alarm
    ],
)
def test_on_off_band(
    room: float | None, stove: StoveSnapshot, expected: bool | None
) -> None:
    """On at target - hysteresis_low, off at target + hysteresis_high."""
    assert on_off_decision(20.0, room, stove, None, 10_000, SETTINGS) is expected


def test_minimum_durations() -> None:
    """No switch before the minimum running or stop time."""
    assert on_off_decision(20.0, 22.0, ON, 1000, 1000 + 3599, SETTINGS) is None
    assert on_off_decision(20.0, 22.0, ON, 1000, 1000 + 3600, SETTINGS) is False
    assert on_off_decision(20.0, 18.0, OFF, 1000, 1000 + 1799, SETTINGS) is None
    assert on_off_decision(20.0, 18.0, OFF, 1000, 1000 + 1800, SETTINGS) is True
    assert lock_remaining(False, 1000, 1600, SETTINGS) == 1200
    assert lock_remaining(True, None, 1600, SETTINGS) == 0


def test_compensated_setpoint() -> None:
    """The stove set point moves by the difference of both probes, within limits."""
    assert compensated_setpoint(20.0, 19.0, 23.0, 5, 30) == 24.0
    assert compensated_setpoint(20.0, 21.5, 20.0, 5, 30) == 18.5
    assert compensated_setpoint(20.0, 10.0, 30.0, 5, 30) == 30
    assert compensated_setpoint(6.0, 20.0, 15.0, 5, 30) == 5


def test_should_write_setpoint() -> None:
    """Small changes are skipped, others wait for the minimum interval unless forced."""
    assert not should_write_setpoint(20.1, 20.0, None, 0)
    assert should_write_setpoint(20.5, 20.0, None, 0)
    assert not should_write_setpoint(20.5, 20.0, 100, 200)
    assert should_write_setpoint(20.5, 20.0, 100, 200, force=True)
    assert should_write_setpoint(20.5, 20.0, 100, 400)
