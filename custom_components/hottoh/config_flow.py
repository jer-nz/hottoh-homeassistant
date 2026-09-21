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
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
import voluptuous as vol

from .api import HottohClient, HottohError
from .const import (
    CONF_CONNECTION_LOST_MINUTES,
    CONF_DISABLE_CHRONO,
    CONF_HYSTERESIS_HIGH,
    CONF_HYSTERESIS_LOW,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_ON_MINUTES,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_EVENTS,
    CONF_NOTIFY_SERVICES,
    CONF_ONOFF_STOVE_SETPOINT,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_REGULATION_MODE,
    CONF_SENSOR_TIMEOUT_MINUTES,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_CONNECTION_LOST_MINUTES,
    DEFAULT_HYSTERESIS_HIGH,
    DEFAULT_HYSTERESIS_LOW,
    DEFAULT_MIN_OFF_MINUTES,
    DEFAULT_MIN_ON_MINUTES,
    DEFAULT_NOTIFY_EVENTS,
    DEFAULT_ONOFF_STOVE_SETPOINT,
    DEFAULT_PORT,
    DEFAULT_SENSOR_TIMEOUT_MINUTES,
    DOMAIN,
    NOTIFY_EVENTS,
    REGULATION_COMPENSATED,
    REGULATION_ON_OFF,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "HottoH"


async def _validate(hass: HomeAssistant, host: str, port: int) -> dict[str, Any]:
    """Status of hottoh_api; raises HottohError when it cannot be reached."""
    client = HottohClient(async_get_clientsession(hass), host, port)
    status = await client.status()
    await client.features()
    return status


def _connection_schema(defaults: dict[str, Any], with_name: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_name:
        fields[
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME))
        ] = str
    fields[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))] = str
    fields[vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT))] = (
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        )
    )
    return vol.Schema(fields)


class HottohConfigFlow(ConfigFlow, domain=DOMAIN):
    """hottoh_api entered by hand or discovered from its add-on."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}
        self._entry: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HottohOptionsFlow:
        """Thermostat and notifications."""
        return HottohOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Address of hottoh_api."""
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
                    "data": {CONF_HOST: host, CONF_PORT: port},
                }
                return await self.async_step_thermostat()
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}, with_name=True),
            errors=errors,
        )

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """hottoh_api add-on announced by the Supervisor."""
        host = discovery_info.config[CONF_HOST]
        port = int(discovery_info.config[CONF_PORT])
        await self.async_set_unique_id(discovery_info.uuid)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})
        self._discovered = {
            CONF_HOST: host,
            CONF_PORT: port,
            "addon": discovery_info.name,
        }
        self.context["title_placeholders"] = {"addon": discovery_info.name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirms the add-on and names the stove."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate(
                    self.hass, self._discovered[CONF_HOST], self._discovered[CONF_PORT]
                )
            except HottohError:
                errors["base"] = "cannot_connect"
            else:
                self._entry = {
                    "title": user_input[CONF_NAME],
                    "data": {
                        CONF_HOST: self._discovered[CONF_HOST],
                        CONF_PORT: self._discovered[CONF_PORT],
                    },
                }
                return await self.async_step_thermostat()
        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=DEFAULT_NAME): str}
            ),
            description_placeholders={"addon": self._discovered["addon"]},
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
        """New address of the same hottoh_api."""
        entry = self._get_reconfigure_entry()
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
            data_schema=_connection_schema(
                user_input or dict(entry.data), with_name=False
            ),
            errors=errors,
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
            return self.async_create_entry(data=self._options)
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
