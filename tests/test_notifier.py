"""Alarms, lost link and notifications."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.hottoh.const import BUS_EVENT, DOMAIN

from .conftest import HOST, PORT, FakeBridge


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


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_alarm_notified(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """New alarm and its end: bus event and notify action."""
    calls = async_mock_service(hass, "notify", "phone")
    events = async_capture_events(hass, BUS_EVENT)
    entry = _entry(notify_services=["notify.phone"])
    await _setup(hass, entry)

    bridge.set_alarm(61, "2026-09-21T10:00:00+02:00")
    await _poll(hass, entry)
    assert [e.data["type"] for e in events] == ["alarm"]
    assert events[0].data["state"] == "no_pellet"
    assert len(calls) == 1
    assert calls[0].data == {
        "title": "Poêle",
        "message": "Alarm: no pellets (code 61).",
    }
    assert hass.states.get("binary_sensor.poele_alarm").state == "on"
    assert hass.states.get("sensor.poele_last_alarm").state == "no_pellet"

    bridge.set_alarm(61, "2026-09-21T10:00:00+02:00", "2026-09-21T10:30:00+02:00")
    await _poll(hass, entry)
    assert [e.data["type"] for e in events] == ["alarm", "alarm_cleared"]
    assert len(calls) == 2


async def test_existing_alarm_not_notified(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """An alarm already there at start is shown, not notified again."""
    calls = async_mock_service(hass, "notify", "phone")
    bridge.set_alarm(60, "2026-09-21T09:00:00+02:00")
    entry = _entry(notify_services=["notify.phone"])
    await _setup(hass, entry)
    await _poll(hass, entry)
    assert calls == []
    assert hass.states.get("binary_sensor.poele_alarm").state == "on"


async def test_recent_alarm_at_start_notified(
    hass: HomeAssistant, bridge: FakeBridge, freezer: FrozenDateTimeFactory
) -> None:
    """Power cut: Home Assistant starts with the stove, whose alarm is already there; it is
    notified once Home Assistant has started (notify services ready), in order."""
    freezer.move_to("2026-10-23T06:55:00+00:00")
    calls = async_mock_service(hass, "notify", "phone")
    events = async_capture_events(hass, BUS_EVENT)
    bridge.set_alarm(16, "2026-10-23T08:52:34+02:00")
    hass.set_state(CoreState.starting)
    entry = _entry(notify_services=["notify.phone"])
    await _setup(hass, entry)
    bridge.set_alarm(16, "2026-10-23T08:52:34+02:00", "2026-10-23T08:56:00+02:00")
    await _poll(hass, entry)
    assert calls == []

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert [e.data["type"] for e in events] == ["alarm", "alarm_cleared"]
    assert events[0].data["device_id"] is not None
    assert [c.data["message"] for c in calls] == [
        "Alarm: power cut (code 16).",
        "Alarm cleared: power cut (code 16).",
    ]


async def test_events_filter_and_persistent(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """Only the chosen events are notified; persistent notification on request."""
    calls = async_mock_service(hass, "notify", "phone")
    entry = _entry(
        notify_services=["notify.phone"],
        notify_events=["alarm_cleared"],
        persistent_notification=True,
    )
    await _setup(hass, entry)
    bridge.set_alarm(69, "2026-09-21T10:00:00+02:00")
    await _poll(hass, entry)
    assert calls == []


async def test_connection_lost(
    hass: HomeAssistant, bridge: FakeBridge, freezer: FrozenDateTimeFactory
) -> None:
    """Lost after the delay, restored afterwards."""
    calls = async_mock_service(hass, "notify", "phone")
    events = async_capture_events(hass, BUS_EVENT)
    entry = _entry(notify_services=["notify.phone"], connection_lost_minutes=10)
    await _setup(hass, entry)

    bridge.pages["status"]["connected"] = False
    bridge.pages["status"]["last_error"] = "Host is unreachable"
    await _poll(hass, entry)
    freezer.tick(timedelta(minutes=9))
    await _poll(hass, entry)
    assert events == []
    freezer.tick(timedelta(minutes=2))
    await _poll(hass, entry)
    assert [e.data["type"] for e in events] == ["connection_lost"]
    assert "Host is unreachable" in calls[0].data["message"]

    bridge.down = True  # hottoh_api itself gone: still lost, no new event
    await _poll(hass, entry)
    bridge.down = False
    bridge.pages["status"]["connected"] = True
    await _poll(hass, entry)
    assert [e.data["type"] for e in events] == [
        "connection_lost",
        "connection_restored",
    ]
    # connection_restored is not in the default notified events
    assert len(calls) == 1
