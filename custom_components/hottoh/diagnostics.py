"""Diagnostics of the HottoH integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import HottohConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HottohConfigEntry
) -> dict[str, Any]:
    """Last poll of hottoh_api, options and enabled module features."""
    coordinator = entry.runtime_data.coordinator
    return {
        "data": dict(entry.data),
        "options": dict(entry.options),
        "features": coordinator.features,
        "last_update_success": coordinator.last_update_success,
        "poll": asdict(coordinator.data) if coordinator.data else None,
    }
