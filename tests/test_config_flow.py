"""Config and options flows."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hottoh.const import DOMAIN

from .conftest import HOST, PORT, FakeBridge


async def test_user_flow(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """hottoh_api entered by hand."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Poêle", "host": HOST, "port": PORT}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Poêle"
    assert result["data"] == {"host": HOST, "port": PORT}
    assert result["result"].unique_id == "192.168.1.50:5001"


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """Error shown, then accepted once hottoh_api answers."""
    bridge.down = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Poêle", "host": HOST, "port": PORT}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    bridge.down = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Poêle", "host": HOST, "port": PORT}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Same stove twice."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Poêle", "host": HOST, "port": PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_hassio_discovery(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """Add-on announced by the Supervisor."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"host": HOST, "port": PORT},
            name="hottoh_api",
            slug="local_hottoh_api",
            uuid="1234",
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Poêle"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "1234"
    assert result["data"] == {"host": HOST, "port": PORT}


async def test_options_flow(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Thermostat then notifications."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "temperature_sensor": "sensor.salon",
            "regulation_mode": "on_off",
            "hysteresis_low": 1.0,
            "hysteresis_high": 1.0,
            "min_on_minutes": 60,
            "min_off_minutes": 30,
            "onoff_stove_setpoint": 30,
            "disable_chrono": True,
            "sensor_timeout_minutes": 15,
        },
    )
    assert result["step_id"] == "notifications"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_services": ["notify.mobile_app_phone"],
            "notify_entities": [],
            "notify_events": ["alarm"],
            "persistent_notification": True,
            "connection_lost_minutes": 10,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options["temperature_sensor"] == "sensor.salon"
    assert config_entry.options["notify_services"] == ["notify.mobile_app_phone"]
    assert hass.states.get("climate.poele").attributes["temperature_sensor"] == (
        "sensor.salon"
    )
