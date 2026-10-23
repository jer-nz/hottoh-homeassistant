"""Sensors of a HottoH stove."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import STATE_UNKNOWN_STOVE, STOVE_STATES, stove_state_name
from .coordinator import HottohCoordinator, HottohData
from .entity import HottohEntity, async_add_stove_entities

if TYPE_CHECKING:
    from . import HottohConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HottohSensorDescription(SensorEntityDescription):
    """Sensor read from the polled pages."""

    value_fn: Callable[[HottohData], StateType]
    exists_fn: Callable[[HottohData], bool] = lambda _: True
    attrs_fn: Callable[[HottohData], dict[str, Any]] | None = None
    needs_stove: bool = True


def _temperature(
    key: str, page: str, field: str, exists: Callable[[HottohData], bool], **kwargs: Any
) -> HottohSensorDescription:
    return HottohSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: getattr(d, page).get(field),
        exists_fn=exists,
        **kwargs,
    )


def _fan_speed(fan: int) -> HottohSensorDescription:
    return HottohSensorDescription(
        key=f"fan_{fan}_speed",
        translation_key="fan_speed",
        translation_placeholders={"fan": str(fan)},
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.dat2.get(f"index_fan_{fan}_speed"),
        # only the fans declared by the stove board exist
        exists_fn=lambda d: int(d.dat0.get("fan_number", 0)) >= fan,
    )


def _last_alarm(data: HottohData) -> str | None:
    events = data.alarms.get("events") or []
    return stove_state_name(int(events[0]["state_raw"])) if events else None


def _last_alarm_attrs(data: HottohData) -> dict[str, Any]:
    events = data.alarms.get("events") or []
    if not events:
        return {}
    last = events[0]
    return {
        "code": last.get("state_raw"),
        "started_at": last.get("started_at"),
        "ended_at": last.get("ended_at"),
        "active": last.get("ended_at") is None,
    }


STATE_OPTIONS = [*dict.fromkeys(STOVE_STATES.values()), STATE_UNKNOWN_STOVE]

SENSORS: tuple[HottohSensorDescription, ...] = (
    HottohSensorDescription(
        key="state",
        translation_key="state",
        device_class=SensorDeviceClass.ENUM,
        options=STATE_OPTIONS,
        value_fn=lambda d: stove_state_name(d.state_raw),
        attrs_fn=lambda d: {"code": d.state_raw},
    ),
    _temperature(
        "room_temperature",
        "dat0",
        "index_ambient_t1",
        lambda d: d.dat0.get("temp_room1_enabled", True),
    ),
    _temperature(
        "room_temperature_2",
        "dat0",
        "index_ambient_t2",
        lambda d: bool(d.dat0.get("temp_room2_enabled")),
    ),
    _temperature(
        "room_temperature_3",
        "dat2",
        "index_room_temp_3",
        lambda d: bool(d.dat0.get("temp_room3_enabled")),
    ),
    _temperature(
        "water_temperature",
        "dat0",
        "index_water",
        lambda d: bool(d.dat0.get("temp_water_enabled")),
    ),
    _temperature(
        "boiler_temperature",
        "dat2",
        "index_boiler",
        lambda d: bool(d.dat0.get("boiler_enabled")),
    ),
    _temperature(
        "dhw_temperature",
        "dat2",
        "index_dhw",
        lambda d: bool(d.dat0.get("domestic_hot_water_enabled")),
    ),
    _temperature("smoke_temperature", "dat0", "index_smoke_t", lambda _: True),
    HottohSensorDescription(
        key="power_level",
        translation_key="power_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.dat0.get("index_power_level"),
    ),
    HottohSensorDescription(
        key="smoke_fan",
        translation_key="smoke_fan",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.dat0.get("index_fan_smoke"),
    ),
    _fan_speed(1),
    _fan_speed(2),
    _fan_speed(3),
    HottohSensorDescription(
        key="last_alarm",
        translation_key="last_alarm",
        device_class=SensorDeviceClass.ENUM,
        options=STATE_OPTIONS,
        value_fn=_last_alarm,
        attrs_fn=_last_alarm_attrs,
        needs_stove=False,
    ),
    HottohSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.inf.get("signal"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the sensors the stove has."""
    coordinator = entry.runtime_data.coordinator
    async_add_stove_entities(
        coordinator,
        SENSORS,
        lambda description: HottohSensor(coordinator, description),
        async_add_entities,
    )


class HottohSensor(HottohEntity, SensorEntity):
    """Value of a polled page."""

    entity_description: HottohSensorDescription

    def __init__(
        self, coordinator: HottohCoordinator, description: HottohSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._needs_stove = description.needs_stove

    @property
    def native_value(self) -> StateType:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Raw code, alarm times."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
