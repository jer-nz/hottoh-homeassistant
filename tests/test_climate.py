"""Thermostat on the stove probe and on an external sensor."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hottoh.const import DOMAIN

from .conftest import HOST, PORT, FakeBridge

SENSOR = "sensor.salon"


def _entry(**options: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Poêle",
        unique_id="192.168.1.50:5001",
        data={"host": HOST, "port": PORT},
        options=options,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _room(hass: HomeAssistant, value: float | str) -> None:
    hass.states.async_set(SENSOR, str(value), {"unit_of_measurement": "°C"})
    await hass.async_block_till_done(wait_background_tasks=True)


async def _climate(hass: HomeAssistant, service: str, **data: object) -> None:
    await hass.services.async_call(
        "climate", service, {"entity_id": "climate.poele", **data}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


def _on_off_writes(bridge: FakeBridge) -> list[bool]:
    return [body["value"] for path, body in bridge.writes if path == "dat/set_on_off"]


async def test_stove_probe(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """Without external sensor, the stove regulates on its own probe."""
    await _setup(hass, _entry())
    await _climate(hass, "set_temperature", temperature=22)
    assert bridge.writes[-1] == (
        "dat/set_ambiance_temp",
        {"ambiance": 1, "value": 22.0},
    )
    await _climate(hass, "set_hvac_mode", hvac_mode=HVACMode.HEAT)
    assert _on_off_writes(bridge) == [True]
    state = hass.states.get("climate.poele")
    assert state.state == HVACMode.HEAT
    assert state.attributes["temperature"] == 22.0
    assert state.attributes["current_temperature"] == bridge.dat0["index_ambient_t1"]
    assert state.attributes["hvac_action"] == "preheating"


async def test_on_off_regulation(
    hass: HomeAssistant, bridge: FakeBridge, freezer: FrozenDateTimeFactory
) -> None:
    """Hysteresis of 1 °C, 60 min running and 30 min stopped at least."""
    bridge.dat0["index_chrono_mode"] = 2
    await _room(hass, 18.0)
    await _setup(hass, _entry(temperature_sensor=SENSOR))
    assert hass.states.get("climate.poele").state == HVACMode.OFF
    assert bridge.writes == []

    await _climate(hass, "set_temperature", temperature=20, hvac_mode=HVACMode.HEAT)
    paths = [path for path, _ in bridge.writes]
    assert ("dat/set_chrono_mode", {"value": False}) in bridge.writes
    assert ("dat/set_ambiance_temp", {"ambiance": 1, "value": 30.0}) in bridge.writes
    assert paths[-1] == "dat/set_on_off"
    assert _on_off_writes(bridge) == [True]
    state = hass.states.get("climate.poele")
    assert state.attributes["current_temperature"] == 18.0
    assert state.attributes["lock_remaining_min"] == 60.0

    # above target + 1, but not running for 60 min yet
    freezer.tick(timedelta(minutes=30))
    await _room(hass, 21.5)
    assert _on_off_writes(bridge) == [True]
    freezer.tick(timedelta(minutes=31))
    await _room(hass, 21.6)
    assert _on_off_writes(bridge) == [True, False]

    # stopping (fake bridge: state 9) and under 30 min: stays off
    await _room(hass, 17.0)
    assert _on_off_writes(bridge) == [True, False]
    bridge.dat0["index_stove_state_raw"] = 0
    freezer.tick(timedelta(minutes=29))
    await _room(hass, 17.1)
    assert _on_off_writes(bridge) == [True, False]
    freezer.tick(timedelta(minutes=2))
    await _room(hass, 17.2)
    assert _on_off_writes(bridge) == [True, False, True]

    # off from Home Assistant: at once, whatever the minimum running time
    await _climate(hass, "turn_off")
    assert _on_off_writes(bridge) == [True, False, True, False]
    await _room(hass, 15.0)
    assert _on_off_writes(bridge) == [True, False, True, False]


async def test_sensor_unavailable(
    hass: HomeAssistant, bridge: FakeBridge, freezer: FrozenDateTimeFactory
) -> None:
    """Regulation paused, then an event after the timeout."""
    events = []
    hass.bus.async_listen("hottoh_event", events.append)
    await _room(hass, 18.0)
    await _setup(hass, _entry(temperature_sensor=SENSOR, sensor_timeout_minutes=15))
    await _climate(hass, "set_temperature", temperature=20, hvac_mode=HVACMode.HEAT)
    assert _on_off_writes(bridge) == [True]
    freezer.tick(timedelta(minutes=61))
    await _room(hass, "unavailable")
    assert _on_off_writes(bridge) == [True]
    freezer.tick(timedelta(minutes=16))
    await config_entry_refresh(hass)
    assert [e.data["type"] for e in events] == ["sensor_unavailable"]
    assert events[0].data["entity_id"] == SENSOR


async def config_entry_refresh(hass: HomeAssistant) -> None:
    """One poll of every entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_compensated_regulation(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """Stove set point shifted by the difference of both probes."""
    bridge.dat0["index_ambient_t1"] = 23.0
    await _room(hass, 19.0)
    await _setup(hass, _entry(temperature_sensor=SENSOR, regulation_mode="compensated"))
    await _climate(hass, "set_temperature", temperature=20, hvac_mode=HVACMode.HEAT)
    assert ("dat/set_ambiance_temp", {"ambiance": 1, "value": 24.0}) in bridge.writes
    assert _on_off_writes(bridge) == [True]


async def test_restore(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """Target and mode survive a reload."""
    await _room(hass, 20.5)
    entry = _entry(temperature_sensor=SENSOR)
    await _setup(hass, entry)
    await _climate(hass, "set_temperature", temperature=21, hvac_mode=HVACMode.HEAT)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("climate.poele")
    assert state.state == HVACMode.HEAT
    assert state.attributes["temperature"] == 21


async def test_no_restart_after_alarm(
    hass: HomeAssistant, bridge: FakeBridge, freezer: FrozenDateTimeFactory
) -> None:
    """After an alarm, the stove waits for someone to ask for heat again."""
    await _room(hass, 18.0)
    entry = _entry(temperature_sensor=SENSOR, min_on_minutes=0, min_off_minutes=0)
    await _setup(hass, entry)
    await _climate(hass, "set_temperature", temperature=20, hvac_mode=HVACMode.HEAT)
    assert _on_off_writes(bridge) == [True]

    bridge.set_alarm(60, "2026-09-21T10:00:00+02:00")
    bridge.dat0["index_stove_on"] = False
    await config_entry_refresh(hass)
    assert hass.states.get("climate.poele").attributes["alarm_hold"] is True
    bridge.set_alarm(60, "2026-09-21T10:00:00+02:00", "2026-09-21T10:20:00+02:00")
    freezer.tick(timedelta(minutes=5))
    await config_entry_refresh(hass)
    await _room(hass, 17.0)
    assert _on_off_writes(bridge) == [True]

    await _climate(hass, "set_hvac_mode", hvac_mode=HVACMode.HEAT)
    assert _on_off_writes(bridge) == [True, True]
    assert hass.states.get("climate.poele").attributes["alarm_hold"] is False
