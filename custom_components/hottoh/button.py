"""Buttons of a HottoH stove: clock synchronisation, module restart."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HottohError
from .const import DOMAIN
from .coordinator import HottohCoordinator
from .entity import HottohEntity, async_write

if TYPE_CHECKING:
    from . import HottohConfigEntry

PARALLEL_UPDATES = 1

SYNC_CLOCK = ButtonEntityDescription(
    key="sync_clock",
    translation_key="sync_clock",
    entity_category=EntityCategory.CONFIG,
)
RESTART_MODULE = ButtonEntityDescription(
    key="restart_module",
    device_class=ButtonDeviceClass.RESTART,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the buttons of the features enabled in hottoh_api."""
    coordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = []
    if coordinator.feature("clock_write"):
        entities.append(HottohButton(coordinator, SYNC_CLOCK))
    if coordinator.feature("module_restart"):
        entities.append(HottohButton(coordinator, RESTART_MODULE))
    async_add_entities(entities)


class HottohButton(HottohEntity, ButtonEntity):
    """Module command."""

    def __init__(
        self, coordinator: HottohCoordinator, description: ButtonEntityDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Sends the command."""
        if self.entity_description.key == SYNC_CLOCK.key:
            await async_write(self.coordinator, self.client.sync_clock())
            return
        try:
            await self.client.restart_module()
        except HottohError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
