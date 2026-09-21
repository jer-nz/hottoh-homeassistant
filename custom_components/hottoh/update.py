"""Firmware of the Wi-Fi module (information only: updates go through AppFire)."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.update import UpdateEntity, UpdateEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HottohError
from .coordinator import HottohCoordinator
from .entity import HottohEntity

if TYPE_CHECKING:
    from . import HottohConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
# hottoh_api keeps its answer 6 hours
SCAN_INTERVAL = timedelta(hours=6)

FIRMWARE = UpdateEntityDescription(
    key="firmware",
    translation_key="firmware",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Creates the firmware entity when hottoh_api may check for updates."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.feature("firmware_update_check"):
        async_add_entities([HottohFirmware(coordinator)], update_before_add=True)


class HottohFirmware(HottohEntity, UpdateEntity):
    """Installed and newest module firmware."""

    def __init__(self, coordinator: HottohCoordinator) -> None:
        super().__init__(coordinator, FIRMWARE.key)
        self.entity_description = FIRMWARE
        self._firmware: dict[str, Any] = {}

    @property
    def should_poll(self) -> bool:
        """Polled every SCAN_INTERVAL, apart from the coordinator."""
        return True

    @property
    def installed_version(self) -> str | None:
        """Firmware of the module."""
        return self._firmware.get("installed") or self.coordinator.data.inf.get(
            "version"
        )

    @property
    def latest_version(self) -> str | None:
        """Newest firmware on the HottoH update server."""
        return self._firmware.get("latest_available") or self.installed_version

    async def async_update(self) -> None:
        """Asks hottoh_api (cached there 6 hours)."""
        try:
            self._firmware = await self.client.firmware()
        except HottohError as err:
            _LOGGER.debug("Firmware check failed: %s", err)
