"""Numeric settings of a HottoH stove."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HottohClient, WriteResult
from .coordinator import HottohCoordinator, HottohData
from .entity import HottohEntity, async_add_stove_entities, async_write

if TYPE_CHECKING:
    from . import HottohConfigEntry

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class HottohNumberDescription(NumberEntityDescription):
    """Setting read from a page and written through hottoh_api."""

    value_fn: Callable[[HottohData], float | None]
    min_fn: Callable[[HottohData], float]
    max_fn: Callable[[HottohData], float]
    set_fn: Callable[[HottohClient, float], Awaitable[WriteResult]]
    exists_fn: Callable[[HottohData], bool] = lambda _: True


def _fan(fan: int) -> HottohNumberDescription:
    return HottohNumberDescription(
        key=f"fan_{fan}_setpoint",
        translation_key="fan_setpoint",
        translation_placeholders={"fan": str(fan)},
        native_step=1,
        mode=NumberMode.SLIDER,
        value_fn=lambda d: d.dat0.get(f"index_fan_{fan}_set"),
        min_fn=lambda _: 0,
        max_fn=lambda d: d.dat0.get(f"index_fan_{fan}_set_max", 0),
        set_fn=lambda c, v: c.set_fan_speed(fan, int(v)),
        # a fan without maximum cannot be set on this stove
        exists_fn=lambda d: int(d.dat0.get(f"index_fan_{fan}_set_max", 0)) > 0,
    )


def _program(program: int) -> HottohNumberDescription:
    field = f"index_program_{program}_temp"
    return HottohNumberDescription(
        key=f"program_{program}_temperature",
        translation_key="program_temperature",
        translation_placeholders={"program": str(program)},
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.dat1.get(field),
        min_fn=lambda d: d.dat1.get(f"{field}_min", 5),
        max_fn=lambda d: d.dat1.get(f"{field}_max", 35),
        set_fn=lambda c, v: c.set_chrono_temperature(program, v),
    )


NUMBERS: tuple[HottohNumberDescription, ...] = (
    HottohNumberDescription(
        key="power_setpoint",
        translation_key="power_setpoint",
        native_step=1,
        mode=NumberMode.SLIDER,
        value_fn=lambda d: d.dat0.get("index_power_set"),
        min_fn=lambda d: d.dat0.get("index_power_min", 1),
        max_fn=lambda d: d.dat0.get("index_power_max", 10),
        set_fn=lambda c, v: c.set_power_level(int(v)),
    ),
    HottohNumberDescription(
        key="ambiance_2_setpoint",
        translation_key="ambiance_2_setpoint",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_step=0.1,
        mode=NumberMode.BOX,
        value_fn=lambda d: d.dat0.get("index_ambient_t2_set"),
        min_fn=lambda d: d.dat0.get("index_ambient_t2_set_min", 5),
        max_fn=lambda d: d.dat0.get("index_ambient_t2_set_max", 35),
        set_fn=lambda c, v: c.set_ambiance_temperature(2, v),
        exists_fn=lambda d: bool(d.dat0.get("temp_room2_enabled")),
    ),
    _fan(1),
    _fan(2),
    _fan(3),
    _program(1),
    _program(2),
    _program(3),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the settings the stove has."""
    coordinator = entry.runtime_data.coordinator
    async_add_stove_entities(
        coordinator, NUMBERS, lambda d: HottohNumber(coordinator, d), async_add_entities
    )


class HottohNumber(HottohEntity, NumberEntity):
    """Numeric setting."""

    entity_description: HottohNumberDescription

    def __init__(
        self, coordinator: HottohCoordinator, description: HottohNumberDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_min_value(self) -> float:
        """Minimum reported by the stove."""
        return float(self.entity_description.min_fn(self.coordinator.data))

    @property
    def native_max_value(self) -> float:
        """Maximum reported by the stove."""
        return float(self.entity_description.max_fn(self.coordinator.data))

    async def async_set_native_value(self, value: float) -> None:
        """Writes the setting."""
        await async_write(
            self.coordinator, self.entity_description.set_fn(self.client, value)
        )
