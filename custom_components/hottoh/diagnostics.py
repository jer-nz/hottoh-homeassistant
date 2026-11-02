"""Diagnostics of the HottoH integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_STOVE_HOST

if TYPE_CHECKING:
    from . import HottohConfigEntry

# addresses of the network: of hottoh_api, of the stove, and of the stove as hottoh_api sees it
TO_REDACT = {CONF_HOST, CONF_STOVE_HOST, "stove_address"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HottohConfigEntry
) -> dict[str, Any]:
    """Last poll of hottoh_api, options and enabled module features."""
    coordinator = entry.runtime_data.coordinator
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "features": coordinator.features,
        "last_update_success": coordinator.last_update_success,
        "poll": async_redact_data(asdict(coordinator.data), TO_REDACT)
        if coordinator.data
        else None,
    }
