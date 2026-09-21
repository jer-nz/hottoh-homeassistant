"""Binary sensors of a HottoH stove."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import is_alarm, stove_state_name
from .coordinator import HottohCoordinator, HottohData
from .entity import HottohEntity

if TYPE_CHECKING:
    from . import HottohConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HottohBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor read from the polled pages."""

    value_fn: Callable[[HottohData], bool | None]
    attrs_fn: Callable[[HottohData], dict[str, Any]] | None = None
    needs_stove: bool = True


def _alarm_attrs(data: HottohData) -> dict[str, Any]:
    current = data.alarms.get("current")
    if not current:
        return {}
    return {
        "alarm": stove_state_name(int(current["state_raw"])),
        "code": current["state_raw"],
        "started_at": current.get("started_at"),
    }


BINARY_SENSORS: tuple[HottohBinarySensorDescription, ...] = (
    HottohBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.connected,
        attrs_fn=lambda d: {
            "stove_address": d.status.get("stove_address"),
            "connected_since": d.status.get("connected_since"),
            "last_response_at": d.status.get("last_response_at"),
            "last_error": d.status.get("last_error"),
            "last_error_at": d.status.get("last_error_at"),
        },
        needs_stove=False,
    ),
    HottohBinarySensorDescription(
        key="running",
        translation_key="running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d: bool(d.dat0.get("index_stove_on")),
    ),
    HottohBinarySensorDescription(
        key="alarm",
        translation_key="alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.state_raw is not None and is_alarm(d.state_raw),
        attrs_fn=_alarm_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the binary sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(HottohBinarySensor(coordinator, d) for d in BINARY_SENSORS)


class HottohBinarySensor(HottohEntity, BinarySensorEntity):
    """Flag of a polled page."""

    entity_description: HottohBinarySensorDescription

    def __init__(
        self, coordinator: HottohCoordinator, description: HottohBinarySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._needs_stove = description.needs_stove

    @property
    def is_on(self) -> bool | None:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Details."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
