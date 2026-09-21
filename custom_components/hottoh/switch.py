"""Switches of a HottoH stove: eco and chrono modes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HottohClient, WriteResult
from .coordinator import HottohCoordinator, HottohData
from .entity import HottohEntity, async_write

if TYPE_CHECKING:
    from . import HottohConfigEntry

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class HottohSwitchDescription(SwitchEntityDescription):
    """Mode read from DAT page 0."""

    value_fn: Callable[[HottohData], bool]
    set_fn: Callable[[HottohClient, bool], Awaitable[WriteResult]]


SWITCHES: tuple[HottohSwitchDescription, ...] = (
    HottohSwitchDescription(
        key="eco_mode",
        translation_key="eco_mode",
        value_fn=lambda d: bool(d.dat0.get("index_eco_mode")),
        set_fn=lambda c, on: c.set_eco_mode(on),
    ),
    HottohSwitchDescription(
        key="chrono_mode",
        translation_key="chrono_mode",
        # "Function Mode": 0 manual, 2 chrono
        value_fn=lambda d: int(d.dat0.get("index_chrono_mode", 0)) != 0,
        set_fn=lambda c, on: c.set_chrono_mode(on),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the switches."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(HottohSwitch(coordinator, d) for d in SWITCHES)


class HottohSwitch(HottohEntity, SwitchEntity):
    """Stove mode."""

    entity_description: HottohSwitchDescription

    def __init__(
        self, coordinator: HottohCoordinator, description: HottohSwitchDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Mode enabled."""
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enables the mode."""
        await async_write(
            self.coordinator, self.entity_description.set_fn(self.client, True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disables the mode."""
        await async_write(
            self.coordinator, self.entity_description.set_fn(self.client, False)
        )
