"""HottoH pellet stoves through hottoh_api."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HottohClient, HottohError
from .const import DOMAIN
from .coordinator import HottohCoordinator
from .entity import device_info
from .notifier import HottohNotifier
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class HottohRuntime:
    """Objects of one config entry."""

    client: HottohClient
    coordinator: HottohCoordinator
    notifier: HottohNotifier


type HottohConfigEntry = ConfigEntry[HottohRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Registers the actions of the integration."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HottohConfigEntry) -> bool:
    """Connects to one hottoh_api instance."""
    client = HottohClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    try:
        features = await client.features()
    except HottohError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = HottohCoordinator(hass, entry, client, features)
    notifier = HottohNotifier(hass, entry)
    coordinator.notifier = notifier
    await coordinator.async_config_entry_first_refresh()

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **device_info(entry, coordinator)
    )
    notifier.device_id = device.id

    entry.runtime_data = HottohRuntime(client, coordinator, notifier)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HottohConfigEntry) -> bool:
    """Unloads a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
