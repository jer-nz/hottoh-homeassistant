"""Base entity of the HottoH integration."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import HottohClient, HottohError, HottohWriteError, WriteResult
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import HottohCoordinator

if TYPE_CHECKING:
    from . import HottohConfigEntry


def device_info(entry: HottohConfigEntry, coordinator: HottohCoordinator) -> DeviceInfo:
    """Device of a config entry: one stove behind one hottoh_api instance."""
    data = coordinator.data
    manufacturer = data.dat0.get("index_manufacturer") if data else None
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER
        if manufacturer is None
        else f"{MANUFACTURER} ({manufacturer})",
        model=MODEL,
        sw_version=data.inf.get("version") if data else None,
        hw_version=f"hottoh_api {data.status.get('version')}" if data else None,
        configuration_url=coordinator.client.base_url,
    )


async def async_write(
    coordinator: HottohCoordinator, write: Awaitable[WriteResult]
) -> WriteResult:
    """Awaits a write, reports a refusal and refreshes the data."""
    try:
        result = await write
    except HottohWriteError as err:
        if coordinator.notifier is not None:
            coordinator.notifier.write_failed(str(err))
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="write_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except HottohError as err:
        if coordinator.notifier is not None:
            coordinator.notifier.write_failed(str(err))
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    await coordinator.async_refresh()
    return result


class HottohEntity(CoordinatorEntity[HottohCoordinator]):
    """Entity of one stove. Unavailable while hottoh_api has lost the stove."""

    _attr_has_entity_name = True
    # entities that describe hottoh_api itself stay available without the stove
    _needs_stove = True

    def __init__(self, coordinator: HottohCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Data of the stove are fresh."""
        if not super().available:
            return False
        return self.coordinator.data.connected or not self._needs_stove

    @property
    def dat0(self) -> dict[str, Any]:
        """Main stove data."""
        return self.coordinator.data.dat0

    @property
    def dat1(self) -> dict[str, Any]:
        """Chrono programs."""
        return self.coordinator.data.dat1

    @property
    def dat2(self) -> dict[str, Any]:
        """Hydraulic data and fan speeds."""
        return self.coordinator.data.dat2

    @property
    def client(self) -> HottohClient:
        """Client of hottoh_api."""
        return self.coordinator.client
