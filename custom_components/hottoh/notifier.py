"""Stove events: bus event, notify services and entities, persistent notification."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    BUS_EVENT,
    CONF_CONNECTION_LOST_MINUTES,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_EVENTS,
    CONF_NOTIFY_SERVICES,
    CONF_PERSISTENT_NOTIFICATION,
    DEFAULT_CONNECTION_LOST_MINUTES,
    DEFAULT_NOTIFY_EVENTS,
    EVENT_ALARM,
    EVENT_ALARM_CLEARED,
    EVENT_CONNECTION_LOST,
    EVENT_CONNECTION_RESTORED,
    EVENT_SENSOR_UNAVAILABLE,
    EVENT_WRITE_FAILED,
    stove_state_name,
)

if TYPE_CHECKING:
    from datetime import datetime

    from . import HottohConfigEntry
    from .coordinator import HottohData

_LOGGER = logging.getLogger(__name__)

ALARM_LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "low_pellet": "réserve de granulés basse",
        "end_pellet": "granulés épuisés",
        "black_out": "coupure de courant",
        "ignition_failed": "échec d'allumage",
        "no_pellet": "plus de granulés",
        "cover_open": "porte ou trappe ouverte",
        "unknown": "alarme inconnue",
    },
    "en": {
        "low_pellet": "low pellet level",
        "end_pellet": "out of pellets",
        "black_out": "power cut",
        "ignition_failed": "ignition failed",
        "no_pellet": "no pellets",
        "cover_open": "door or cover open",
        "unknown": "unknown alarm",
    },
}

MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        EVENT_ALARM: "Alarme : {alarm} (code {code}).",
        EVENT_ALARM_CLEARED: "Alarme terminée : {alarm} (code {code}).",
        EVENT_CONNECTION_LOST: "Poêle injoignable depuis {minutes} min ({detail}).",
        EVENT_CONNECTION_RESTORED: "Liaison avec le poêle rétablie.",
        EVENT_WRITE_FAILED: "Commande refusée : {detail}.",
        EVENT_SENSOR_UNAVAILABLE: (
            "Sonde {entity_id} indisponible depuis {minutes} min : "
            "régulation suspendue, le poêle garde son état."
        ),
    },
    "en": {
        EVENT_ALARM: "Alarm: {alarm} (code {code}).",
        EVENT_ALARM_CLEARED: "Alarm cleared: {alarm} (code {code}).",
        EVENT_CONNECTION_LOST: "Stove unreachable for {minutes} min ({detail}).",
        EVENT_CONNECTION_RESTORED: "Stove link restored.",
        EVENT_WRITE_FAILED: "Command refused: {detail}.",
        EVENT_SENSOR_UNAVAILABLE: (
            "Sensor {entity_id} unavailable for {minutes} min: "
            "regulation paused, the stove keeps its state."
        ),
    },
}


def _alarm_key(event: dict[str, Any]) -> tuple[str, int]:
    return (str(event.get("started_at")), int(event.get("state_raw", 0)))


class HottohNotifier:
    """Turns successive polls into events and notifications."""

    def __init__(self, hass: HomeAssistant, entry: HottohConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.device_id: str | None = None
        self._initialized = False
        # alarm key -> ended
        self._alarms: dict[tuple[str, int], bool] = {}
        self._lost_since: datetime | None = None
        self._lost_detail = ""
        self._lost_notified = False

    @property
    def _language(self) -> str:
        return "fr" if self.hass.config.language.startswith("fr") else "en"

    def _alarm_label(self, state_raw: int) -> str:
        name = stove_state_name(state_raw) or "unknown"
        return ALARM_LABELS[self._language].get(name, name)

    @callback
    def process(self, data: HottohData) -> None:
        """Takes one successful poll into account."""
        if data.connected:
            self._link_up()
        else:
            self._link_down(str(data.status.get("last_error") or "no answer"))
        self._process_alarms(data.alarms.get("events") or [])
        self._initialized = True

    @callback
    def bridge_unreachable(self) -> None:
        """hottoh_api itself did not answer."""
        self._link_down("hottoh_api unreachable")

    def _process_alarms(self, events: list[dict[str, Any]]) -> None:
        for event in reversed(events):  # oldest first
            key = _alarm_key(event)
            ended = event.get("ended_at") is not None
            known = self._alarms.get(key)
            self._alarms[key] = ended
            if not self._initialized:
                continue
            placeholders = {
                "alarm": self._alarm_label(key[1]),
                "code": str(key[1]),
                "state": stove_state_name(key[1]),
                "started_at": key[0],
            }
            if known is None:
                self.emit(EVENT_ALARM, **placeholders)
                if ended:
                    self.emit(EVENT_ALARM_CLEARED, **placeholders)
            elif not known and ended:
                self.emit(EVENT_ALARM_CLEARED, **placeholders)
        # forget alarms that left the history of hottoh_api
        current = {_alarm_key(e) for e in events}
        for key in list(self._alarms):
            if key not in current:
                del self._alarms[key]

    def _link_up(self) -> None:
        if self._lost_notified:
            self.emit(EVENT_CONNECTION_RESTORED)
        self._lost_since = None
        self._lost_notified = False

    def _link_down(self, detail: str) -> None:
        now = dt_util.utcnow()
        self._lost_detail = detail
        if self._lost_since is None:
            self._lost_since = now
        minutes = self.entry.options.get(
            CONF_CONNECTION_LOST_MINUTES, DEFAULT_CONNECTION_LOST_MINUTES
        )
        elapsed = (now - self._lost_since).total_seconds()
        if not self._lost_notified and elapsed >= minutes * 60:
            self._lost_notified = True
            self.emit(
                EVENT_CONNECTION_LOST, minutes=str(int(elapsed // 60)), detail=detail
            )

    @callback
    def write_failed(self, detail: str) -> None:
        """A write was refused by hottoh_api or the stove."""
        self.emit(EVENT_WRITE_FAILED, detail=detail)

    @callback
    def emit(self, event_type: str, **data: str | None) -> None:
        """Fires the bus event and sends the notifications chosen in the options."""
        self.hass.bus.async_fire(
            BUS_EVENT,
            {
                "type": event_type,
                "entry_id": self.entry.entry_id,
                "device_id": self.device_id,
                **data,
            },
        )
        options = self.entry.options
        if event_type not in options.get(CONF_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS):
            return
        template = MESSAGES[self._language][event_type]
        message = template.format_map({k: v or "" for k, v in data.items()})
        title = self.entry.title
        self.entry.async_create_task(
            self.hass,
            self._send(event_type, title, message),
            f"hottoh notify {event_type}",
        )

    async def _send(self, event_type: str, title: str, message: str) -> None:
        options = self.entry.options
        payload = {"title": title, "message": message}
        for service in options.get(CONF_NOTIFY_SERVICES, []):
            name = service.removeprefix("notify.")
            try:
                await self.hass.services.async_call(
                    "notify", name, payload, blocking=True
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.warning("Cannot notify through notify.%s: %s", name, err)
        if entities := options.get(CONF_NOTIFY_ENTITIES):
            try:
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    payload,
                    target={"entity_id": entities},
                    blocking=True,
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.warning("Cannot notify %s: %s", entities, err)
        if options.get(CONF_PERSISTENT_NOTIFICATION):
            persistent_notification.async_create(
                self.hass,
                message,
                title=title,
                notification_id=f"{self.entry.entry_id}_{event_type}",
            )
