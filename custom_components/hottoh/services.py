"""Actions of the HottoH integration, targeting stoves (devices or their entities)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids
import voluptuous as vol

from .api import HottohClient, WriteResult
from .const import DOMAIN
from .entity import async_write

if TYPE_CHECKING:
    from . import HottohConfigEntry

DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

ATTR_LEVEL = "level"
ATTR_ENABLED = "enabled"
ATTR_FAN = "fan"
ATTR_SPEED = "speed"
ATTR_PROGRAM = "program"
ATTR_AMBIANCE = "ambiance"
ATTR_TEMPERATURE = "temperature"
ATTR_DAYS = "days"

TARGET = cv.ENTITY_SERVICE_FIELDS
TIME = vol.Match(r"^([01]\d|2[0-3]):[03]0$|^24:00$")

RANGE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): TIME,
        vol.Required("end"): TIME,
        vol.Required("program"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
    }
)
DAY_SCHEMA = vol.Schema(
    {
        vol.Required("day"): vol.In(DAYS),
        vol.Exclusive("ranges", "day"): [RANGE_SCHEMA],
        vol.Exclusive("slots", "day"): vol.All(
            [vol.All(vol.Coerce(int), vol.Range(min=0, max=3))],
            vol.Length(min=48, max=48),
        ),
    }
)


def _schema(fields: dict[Any, Any]) -> vol.Schema:
    return vol.Schema({**TARGET, **fields})


type WriteFn = Callable[[HottohClient, dict[str, Any]], Awaitable[WriteResult]]

SERVICES: dict[str, tuple[vol.Schema, WriteFn]] = {
    "set_power_level": (
        _schema({vol.Required(ATTR_LEVEL): vol.All(vol.Coerce(int), vol.Range(min=0))}),
        lambda c, d: c.set_power_level(d[ATTR_LEVEL]),
    ),
    "set_eco_mode": (
        _schema({vol.Required(ATTR_ENABLED): cv.boolean}),
        lambda c, d: c.set_eco_mode(d[ATTR_ENABLED]),
    ),
    "set_chrono_mode": (
        _schema({vol.Required(ATTR_ENABLED): cv.boolean}),
        lambda c, d: c.set_chrono_mode(d[ATTR_ENABLED]),
    ),
    "set_fan_speed": (
        _schema(
            {
                vol.Required(ATTR_FAN): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=3)
                ),
                vol.Required(ATTR_SPEED): vol.All(vol.Coerce(int), vol.Range(min=0)),
            }
        ),
        lambda c, d: c.set_fan_speed(d[ATTR_FAN], d[ATTR_SPEED]),
    ),
    "set_ambiance_temperature": (
        _schema(
            {
                vol.Optional(ATTR_AMBIANCE, default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=2)
                ),
                vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
            }
        ),
        lambda c, d: c.set_ambiance_temperature(d[ATTR_AMBIANCE], d[ATTR_TEMPERATURE]),
    ),
    "set_chrono_temperature": (
        _schema(
            {
                vol.Required(ATTR_PROGRAM): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=3)
                ),
                vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
            }
        ),
        lambda c, d: c.set_chrono_temperature(d[ATTR_PROGRAM], d[ATTR_TEMPERATURE]),
    ),
    "set_schedule": (
        _schema({vol.Required(ATTR_DAYS): vol.All(cv.ensure_list, [DAY_SCHEMA])}),
        lambda c, d: c.set_schedule(d[ATTR_DAYS]),
    ),
    "sync_clock": (_schema({}), lambda c, _: c.sync_clock()),
}


def _target_entries(call: ServiceCall, entry_ids: set[str]) -> list[HottohConfigEntry]:
    entries: list[HottohConfigEntry] = []
    for entry_id in entry_ids:
        entry = call.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            entries.append(entry)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_target"
        )
    return entries


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Registers the actions."""

    def handler(write: WriteFn) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
        async def handle(call: ServiceCall) -> ServiceResponse:
            entries = _target_entries(call, await async_extract_config_entry_ids(call))
            results: list[dict[str, Any]] = []
            for entry in entries:
                runtime = entry.runtime_data
                result = await async_write(
                    runtime.coordinator, write(runtime.client, dict(call.data))
                )
                results.append({"entry_id": entry.entry_id, **result.as_dict()})
            return {"results": results}

        return handle

    for name, (schema, write) in SERVICES.items():
        hass.services.async_register(
            DOMAIN,
            name,
            handler(write),
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )

    async def restart_module(call: ServiceCall) -> None:
        for entry in _target_entries(call, await async_extract_config_entry_ids(call)):
            await entry.runtime_data.client.restart_module()

    hass.services.async_register(
        DOMAIN, "restart_module", restart_module, schema=_schema({})
    )
