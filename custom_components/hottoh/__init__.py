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
from .const import (
    CONF_MODE,
    CONF_STOVE_HOST,
    CONF_STOVE_PORT,
    CONF_WEB_LAN,
    CONF_WEB_LAN_PORT,
    CONF_WEB_PANEL,
    DEFAULT_WEB_LAN_PORT,
    DOMAIN,
    MODE_EMBEDDED,
    MODE_EXTERNAL,
)
from .coordinator import HottohCoordinator
from .embedded import LOCALHOST, EmbeddedBridge, EmbeddedError
from .entity import device_info
from .notifier import HottohNotifier
from .services import async_setup_services
from .web import async_add_web, async_setup_web, remove_web

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
    embedded: EmbeddedBridge | None = None
    panel: str | None = None


type HottohConfigEntry = ConfigEntry[HottohRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Registers the actions and the web relay of the integration."""
    async_setup_services(hass)
    await async_setup_web(hass)
    return True


def _embedded_bridge(hass: HomeAssistant, entry: HottohConfigEntry) -> EmbeddedBridge:
    web_lan_port = None
    if entry.options.get(CONF_WEB_LAN):
        web_lan_port = int(entry.options.get(CONF_WEB_LAN_PORT, DEFAULT_WEB_LAN_PORT))
    return EmbeddedBridge(
        hass,
        entry.entry_id,
        entry.data[CONF_STOVE_HOST],
        int(entry.data[CONF_STOVE_PORT]),
        web_lan_port,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HottohConfigEntry) -> bool:
    """Starts hottoh_api (embedded mode) and connects to it."""
    embedded: EmbeddedBridge | None = None
    if entry.data.get(CONF_MODE, MODE_EXTERNAL) == MODE_EMBEDDED:
        embedded = _embedded_bridge(hass, entry)
        try:
            await embedded.async_start()
        except EmbeddedError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="embedded_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        host, port = LOCALHOST, embedded.api_port
    else:
        host, port = entry.data[CONF_HOST], entry.data[CONF_PORT]

    try:
        runtime = await _async_connect(hass, entry, host, port, embedded)
    except BaseException:
        if embedded is not None:
            await embedded.async_stop()
        raise
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_connect(
    hass: HomeAssistant,
    entry: HottohConfigEntry,
    host: str,
    port: int,
    embedded: EmbeddedBridge | None,
) -> HottohRuntime:
    client = HottohClient(async_get_clientsession(hass), host, port)
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

    web_base = (
        f"http://{LOCALHOST}:{embedded.web_port}" if embedded else client.base_url
    )
    panel = await async_add_web(
        hass,
        entry.entry_id,
        entry.title,
        web_base,
        entry.options.get(CONF_WEB_PANEL, True),
    )
    if panel is not None:
        configuration_url: str | None = f"homeassistant://{panel}"
    elif embedded is not None:
        # interface on the local network only, at an address Home Assistant cannot name
        configuration_url = None
    else:
        configuration_url = client.base_url

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **device_info(entry, coordinator, configuration_url),
    )
    notifier.device_id = device.id
    return HottohRuntime(client, coordinator, notifier, embedded, panel)


async def async_unload_entry(hass: HomeAssistant, entry: HottohConfigEntry) -> bool:
    """Unloads a config entry and stops its hottoh_api."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data
    remove_web(hass, entry.entry_id, runtime.panel)
    if runtime.embedded is not None:
        await runtime.embedded.async_stop()
    return True
