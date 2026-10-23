"""Setup, entities and actions."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.hottoh.const import BUS_EVENT, DOMAIN

from .conftest import FakeBridge


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_entities(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Entities of the stove, only those it has."""
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.poele_state").state == "off"
    assert hass.states.get("sensor.poele_room_temperature").state == str(
        bridge.dat0["index_ambient_t1"]
    )
    assert hass.states.get("sensor.poele_smoke_temperature") is not None
    assert hass.states.get("binary_sensor.poele_stove_link").state == "on"
    assert hass.states.get("binary_sensor.poele_alarm").state == "off"
    assert hass.states.get("switch.poele_eco_mode").state == "on"
    assert hass.states.get("number.poele_power_level").state == "10"
    climate = hass.states.get("climate.poele")
    assert climate.state == "off"
    assert climate.attributes["temperature"] == bridge.dat0["index_ambient_t1_set"]

    registry = er.async_get(hass)
    # not on this stove: water probe, room 2, settable fans
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{config_entry.entry_id}_water_temperature"
        )
        is None
    )
    assert (
        registry.async_get_entity_id(
            "number", DOMAIN, f"{config_entry.entry_id}_fan_1_setpoint"
        )
        is None
    )
    assert (
        registry.async_get_entity_id(
            "number", DOMAIN, f"{config_entry.entry_id}_ambiance_2_setpoint"
        )
        is None
    )

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retry(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """hottoh_api down at start."""
    bridge.down = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_entities_after_module_restart(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Started while the module has no Modbus link: the entities of the stove board come
    with the first valid page."""
    page = bridge.module_restarting()
    await _setup(hass, config_entry)
    assert hass.states.get("sensor.poele_room_temperature") is None
    assert hass.states.get("sensor.poele_state").state == STATE_UNAVAILABLE

    bridge.pages["dat/0"] = page
    await config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.poele_room_temperature").state == str(
        page["index_ambient_t1"]
    )
    assert hass.states.get("sensor.poele_state").state == "off"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_unload_before_valid_page(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Unloaded while still waiting for a valid page."""
    bridge.module_restarting()
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_stove_lost(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Stove entities unavailable while hottoh_api has lost the stove."""
    await _setup(hass, config_entry)
    bridge.pages["status"]["connected"] = False
    await config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.poele_state").state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.poele_stove_link").state == "off"


async def test_actions(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Actions write through hottoh_api and return the outcome."""
    await _setup(hass, config_entry)
    response = await hass.services.async_call(
        DOMAIN,
        "set_power_level",
        {"entity_id": "climate.poele", "level": 4},
        blocking=True,
        return_response=True,
    )
    assert bridge.writes[-1] == ("dat/set_power_level", {"value": 4})
    assert response["results"][0]["status"] == "ok"
    assert hass.states.get("number.poele_power_level").state == "4"

    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "entity_id": "climate.poele",
            "days": [
                {
                    "day": "monday",
                    "ranges": [{"start": "06:30", "end": "08:00", "program": 1}],
                }
            ],
        },
        blocking=True,
    )
    assert bridge.writes[-1][0] == "chrono/schedule"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.poele_eco_mode"}, blocking=True
    )
    assert bridge.writes[-1] == ("dat/set_eco_mode", {"value": False})
    assert hass.states.get("switch.poele_eco_mode").state == "off"


async def test_write_refused(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """A refusal raises an error and fires the event."""
    await _setup(hass, config_entry)
    events = async_capture_events(hass, BUS_EVENT)
    bridge.write_status = "error"
    bridge.write_error_code = 19
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "set_power_level",
            {"entity_id": "climate.poele", "level": 4},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert events[-1].data["type"] == "write_failed"
    assert "19" in events[-1].data["detail"]


async def test_value_applied_late(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """The board applies a power level seconds after its OK: read again a little later."""
    await _setup(hass, config_entry)
    before = hass.states.get("number.poele_power_level").state
    bridge.apply_writes = False
    await hass.services.async_call(
        DOMAIN,
        "set_power_level",
        {"entity_id": "climate.poele", "level": 4},
        blocking=True,
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert hass.states.get("number.poele_power_level").state == before
    bridge.apply_pending()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=7))
    await hass.async_block_till_done()
    assert hass.states.get("number.poele_power_level").state == "4"
