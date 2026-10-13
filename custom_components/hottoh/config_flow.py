"""Config flow of the HottoH integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import HottohClient, HottohError
from .const import (
    CONF_CONNECTION_LOST_MINUTES,
    CONF_DISABLE_CHRONO,
    CONF_HYSTERESIS_HIGH,
    CONF_HYSTERESIS_LOW,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_ON_MINUTES,
    CONF_MODE,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_EVENTS,
    CONF_NOTIFY_SERVICES,
    CONF_ONOFF_STOVE_SETPOINT,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_REGULATION_MODE,
    CONF_SENSOR_TIMEOUT_MINUTES,
    CONF_STOVE_HOST,
    CONF_STOVE_PORT,
    CONF_TEMPERATURE_SENSOR,
    CONF_WEB_LAN,
    CONF_WEB_LAN_PORT,
    CONF_WEB_PANEL,
    DEFAULT_CONNECTION_LOST_MINUTES,
    DEFAULT_HYSTERESIS_HIGH,
    DEFAULT_HYSTERESIS_LOW,
    DEFAULT_MIN_OFF_MINUTES,
    DEFAULT_MIN_ON_MINUTES,
    DEFAULT_NOTIFY_EVENTS,
    DEFAULT_ONOFF_STOVE_SETPOINT,
    DEFAULT_PORT,
    DEFAULT_SENSOR_TIMEOUT_MINUTES,
    DEFAULT_STOVE_PORT,
    DEFAULT_WEB_LAN_PORT,
    DOMAIN,
    HOTTOH_API_URL,
    HOTTOH_API_VERSION,
    MODE_EMBEDDED,
    MODE_EXTERNAL,
    NOTIFY_EVENTS,
    REGULATION_COMPENSATED,
    REGULATION_ON_OFF,
)
from .embedded import EmbeddedError, architecture, async_ensure_binary, port_is_free

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "HottoH"


async def _validate(hass: HomeAssistant, host: str, port: int) -> dict[str, Any]:
    """Status of hottoh_api; raises HottohError when it cannot be reached."""
    client = HottohClient(async_get_clientsession(hass), host, port)
    status = await client.status()
    await client.features()
    return status


def _port_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1, max=65535, mode=selector.NumberSelectorMode.BOX
        )
    )


def _external_schema(defaults: dict[str, Any], creating: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if creating:
        fields[
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME))
        ] = str
    fields[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))] = str
    fields[vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT))] = (
        _port_selector()
    )
    if creating:
        fields[
            vol.Required(CONF_WEB_PANEL, default=defaults.get(CONF_WEB_PANEL, True))
        ] = selector.BooleanSelector()
    return vol.Schema(fields)


def _embedded_schema(defaults: dict[str, Any], creating: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if creating:
        fields[
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME))
        ] = str
    fields[vol.Required(CONF_STOVE_HOST, default=defaults.get(CONF_STOVE_HOST, ""))] = (
        str
    )
    fields[
        vol.Required(
            CONF_STOVE_PORT, default=defaults.get(CONF_STOVE_PORT, DEFAULT_STOVE_PORT)
        )
    ] = _port_selector()
    if creating:
        fields.update(_web_fields(defaults, embedded=True))
    return vol.Schema(fields)


def _web_fields(defaults: dict[str, Any], embedded: bool) -> dict[Any, Any]:
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_WEB_PANEL, default=defaults.get(CONF_WEB_PANEL, True)
        ): selector.BooleanSelector()
    }
    if embedded:
        fields[
            vol.Required(CONF_WEB_LAN, default=defaults.get(CONF_WEB_LAN, False))
        ] = selector.BooleanSelector()
        fields[
            vol.Required(
                CONF_WEB_LAN_PORT,
                default=defaults.get(CONF_WEB_LAN_PORT, DEFAULT_WEB_LAN_PORT),
            )
        ] = _port_selector()
    return fields


async def _check_lan_port(
    hass: HomeAssistant, user_input: dict[str, Any], current: int | None
) -> dict[str, str]:
    """Error when the web interface should open on a port already taken."""
    if not user_input.get(CONF_WEB_LAN):
        return {}
    port = int(user_input[CONF_WEB_LAN_PORT])
    if port == current:  # served by the running hottoh_api
        return {}
    if not await hass.async_add_executor_job(port_is_free, port):
        return {CONF_WEB_LAN_PORT: "port_in_use"}
    return {}


def _web_options(user_input: dict[str, Any], embedded: bool) -> dict[str, Any]:
    options: dict[str, Any] = {CONF_WEB_PANEL: user_input[CONF_WEB_PANEL]}
    if embedded:
        options[CONF_WEB_LAN] = user_input[CONF_WEB_LAN]
        options[CONF_WEB_LAN_PORT] = int(user_input[CONF_WEB_LAN_PORT])
    return options


class HottohConfigFlow(ConfigFlow, domain=DOMAIN):
    """hottoh_api started by the integration, or already running elsewhere."""

    VERSION = 1

    def __init__(self) -> None:
        self._entry: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HottohOptionsFlow:
        """Thermostat, notifications and web interface."""
        return HottohOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Where hottoh_api runs."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[MODE_EMBEDDED, MODE_EXTERNAL],
            description_placeholders={"hottoh_api_url": HOTTOH_API_URL},
        )

    async def async_step_embedded(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Address of the stove; hottoh_api is downloaded and run by the integration."""
        if architecture() is None:
            return self.async_abort(reason="unsupported_architecture")
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_STOVE_HOST].strip()
            port = int(user_input[CONF_STOVE_PORT])
            # same unique id as hottoh_api reports (`stove_address`): one client per module
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            errors = await _check_lan_port(self.hass, user_input, None)
            if not errors:
                try:
                    await async_ensure_binary(self.hass)
                except EmbeddedError as err:
                    _LOGGER.warning("Cannot install hottoh_api: %s", err)
                    errors["base"] = "download_failed"
            if not errors:
                self._entry = {
                    "title": user_input[CONF_NAME],
                    "data": {
                        CONF_MODE: MODE_EMBEDDED,
                        CONF_STOVE_HOST: host,
                        CONF_STOVE_PORT: port,
                    },
                    "options": _web_options(user_input, embedded=True),
                }
                return await self.async_step_thermostat()
        return self.async_show_form(
            step_id="embedded",
            data_schema=_embedded_schema(user_input or {}, creating=True),
            description_placeholders={"version": HOTTOH_API_VERSION},
            errors=errors,
        )

    async def async_step_external(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Address of a hottoh_api already running."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            try:
                status = await _validate(self.hass, host, port)
            except HottohError as err:
                _LOGGER.debug("Cannot reach hottoh_api at %s:%s: %s", host, port, err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(
                    status.get("stove_address") or f"{host}:{port}"
                )
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port}
                )
                self._entry = {
                    "title": user_input[CONF_NAME],
                    "data": {
                        CONF_MODE: MODE_EXTERNAL,
                        CONF_HOST: host,
                        CONF_PORT: port,
                    },
                    "options": _web_options(user_input, embedded=False),
                }
                return await self.async_step_thermostat()
        return self.async_show_form(
            step_id="external",
            data_schema=_external_schema(user_input or {}, creating=True),
            description_placeholders={"hottoh_api_url": HOTTOH_API_URL},
            errors=errors,
        )

    async def async_step_thermostat(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Temperature source of the thermostat (the rest is in the options)."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._entry["title"],
                data=self._entry["data"],
                options={
                    **self._entry["options"],
                    CONF_TEMPERATURE_SENSOR: user_input.get(CONF_TEMPERATURE_SENSOR),
                    CONF_REGULATION_MODE: user_input[CONF_REGULATION_MODE],
                },
            )
        return self.async_show_form(
            step_id="thermostat",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TEMPERATURE_SENSOR
                    ): _temperature_sensor_selector(),
                    vol.Required(
                        CONF_REGULATION_MODE, default=REGULATION_ON_OFF
                    ): _regulation_selector(),
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """New address of the stove (embedded) or of hottoh_api."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_MODE, MODE_EXTERNAL) == MODE_EMBEDDED:
            return await self.async_step_reconfigure_embedded(user_input)
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            try:
                await _validate(self.hass, host, port)
            except HottohError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host, CONF_PORT: port}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_external_schema(
                user_input or dict(entry.data), creating=False
            ),
            errors=errors,
        )

    async def async_step_reconfigure_embedded(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """New address of the stove for the embedded hottoh_api."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            host = user_input[CONF_STOVE_HOST].strip()
            port = int(user_input[CONF_STOVE_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            if entry.unique_id != self.unique_id:
                self._abort_if_unique_id_configured()
            return self.async_update_reload_and_abort(
                entry,
                unique_id=self.unique_id,
                data_updates={CONF_STOVE_HOST: host, CONF_STOVE_PORT: port},
            )
        return self.async_show_form(
            step_id="reconfigure_embedded",
            data_schema=_embedded_schema(dict(entry.data), creating=False),
        )


def _temperature_sensor_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="sensor", device_class=SensorDeviceClass.TEMPERATURE
        )
    )


def _regulation_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[REGULATION_ON_OFF, REGULATION_COMPENSATED],
            translation_key=CONF_REGULATION_MODE,
        )
    )


def _number(
    minimum: float, maximum: float, step: float, unit: str
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class HottohOptionsFlow(OptionsFlowWithReload):
    """Thermostat, then notifications."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Thermostat."""
        if user_input is not None:
            self._options.update(user_input)
            if not user_input.get(CONF_TEMPERATURE_SENSOR):
                self._options[CONF_TEMPERATURE_SENSOR] = None
            return await self.async_step_notifications()
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TEMPERATURE_SENSOR,
                    description={
                        "suggested_value": options.get(CONF_TEMPERATURE_SENSOR)
                    },
                ): _temperature_sensor_selector(),
                vol.Required(
                    CONF_REGULATION_MODE,
                    default=options.get(CONF_REGULATION_MODE, REGULATION_ON_OFF),
                ): _regulation_selector(),
                vol.Required(
                    CONF_HYSTERESIS_LOW,
                    default=options.get(CONF_HYSTERESIS_LOW, DEFAULT_HYSTERESIS_LOW),
                ): _number(0, 5, 0.1, "°C"),
                vol.Required(
                    CONF_HYSTERESIS_HIGH,
                    default=options.get(CONF_HYSTERESIS_HIGH, DEFAULT_HYSTERESIS_HIGH),
                ): _number(0, 5, 0.1, "°C"),
                vol.Required(
                    CONF_MIN_ON_MINUTES,
                    default=options.get(CONF_MIN_ON_MINUTES, DEFAULT_MIN_ON_MINUTES),
                ): _number(0, 480, 1, "min"),
                vol.Required(
                    CONF_MIN_OFF_MINUTES,
                    default=options.get(CONF_MIN_OFF_MINUTES, DEFAULT_MIN_OFF_MINUTES),
                ): _number(0, 480, 1, "min"),
                vol.Required(
                    CONF_ONOFF_STOVE_SETPOINT,
                    default=options.get(
                        CONF_ONOFF_STOVE_SETPOINT, DEFAULT_ONOFF_STOVE_SETPOINT
                    ),
                ): _number(5, 55, 0.5, "°C"),
                vol.Required(
                    CONF_DISABLE_CHRONO,
                    default=options.get(CONF_DISABLE_CHRONO, True),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SENSOR_TIMEOUT_MINUTES,
                    default=options.get(
                        CONF_SENSOR_TIMEOUT_MINUTES, DEFAULT_SENSOR_TIMEOUT_MINUTES
                    ),
                ): _number(1, 240, 1, "min"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Where and what to notify."""
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_web()
        options = self.config_entry.options
        services = sorted(
            f"notify.{name}"
            for name in self.hass.services.async_services_for_domain("notify")
            if name not in ("send_message", "persistent_notification")
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_NOTIFY_SERVICES,
                    default=options.get(CONF_NOTIFY_SERVICES, []),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=services,
                        multiple=True,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_NOTIFY_ENTITIES,
                    default=options.get(CONF_NOTIFY_ENTITIES, []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="notify", multiple=True)
                ),
                vol.Required(
                    CONF_NOTIFY_EVENTS,
                    default=options.get(CONF_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=NOTIFY_EVENTS,
                        multiple=True,
                        translation_key=CONF_NOTIFY_EVENTS,
                    )
                ),
                vol.Required(
                    CONF_PERSISTENT_NOTIFICATION,
                    default=options.get(CONF_PERSISTENT_NOTIFICATION, False),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_CONNECTION_LOST_MINUTES,
                    default=options.get(
                        CONF_CONNECTION_LOST_MINUTES, DEFAULT_CONNECTION_LOST_MINUTES
                    ),
                ): _number(1, 240, 1, "min"),
            }
        )
        return self.async_show_form(step_id="notifications", data_schema=schema)

    async def async_step_web(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Web interface of hottoh_api."""
        options = self.config_entry.options
        embedded = self.config_entry.data.get(CONF_MODE, MODE_EXTERNAL) == MODE_EMBEDDED
        errors: dict[str, str] = {}
        if user_input is not None:
            current = (
                int(options.get(CONF_WEB_LAN_PORT, DEFAULT_WEB_LAN_PORT))
                if options.get(CONF_WEB_LAN)
                else None
            )
            errors = await _check_lan_port(self.hass, user_input, current)
            if not errors:
                self._options.update(_web_options(user_input, embedded))
                return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="web" if embedded else "web_external",
            data_schema=vol.Schema(_web_fields(user_input or dict(options), embedded)),
            errors=errors,
        )

    # same step without the network options (hottoh_api runs elsewhere)
    async_step_web_external = async_step_web
