"""Config and options flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hottoh.const import DOMAIN
from custom_components.hottoh.embedded import EmbeddedError

from .conftest import HOST, PORT, FakeBridge

FLOW = "custom_components.hottoh.config_flow"


async def _external(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "external"}
    )


async def test_external_flow(hass: HomeAssistant, bridge: FakeBridge) -> None:
    """hottoh_api entered by hand."""
    result = await _external(hass)
    assert result["step_id"] == "external"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Poêle", "host": HOST, "port": PORT, "web_panel": False},
    )
    assert result["step_id"] == "thermostat"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"temperature_sensor": "sensor.salon", "regulation_mode": "on_off"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Poêle"
    assert result["data"] == {"mode": "external", "host": HOST, "port": PORT}
    assert result["result"].options == {
        "web_panel": False,
        "temperature_sensor": "sensor.salon",
        "regulation_mode": "on_off",
    }
    assert result["result"].unique_id == "192.168.1.50:5001"


async def test_external_flow_cannot_connect(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """Error shown, then accepted once hottoh_api answers."""
    bridge.down = True
    result = await _external(hass)
    user = {"name": "Poêle", "host": HOST, "port": PORT, "web_panel": True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    bridge.down = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"regulation_mode": "on_off"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["temperature_sensor"] is None


async def test_external_flow_already_configured(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Same stove twice."""
    config_entry.add_to_hass(hass)
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Poêle", "host": HOST, "port": PORT, "web_panel": True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


EMBEDDED_INPUT = {
    "name": "Poêle",
    "stove_host": "192.168.1.60",
    "stove_port": 5001,
    "web_panel": True,
    "web_lan": True,
    "web_lan_port": 3000,
}


async def _embedded(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "embedded"}
    )


async def test_embedded_flow(hass: HomeAssistant) -> None:
    """Address of the stove; the binary is installed before the entry is created."""
    with (
        patch(f"{FLOW}.architecture", return_value="amd64"),
        patch(f"{FLOW}.async_ensure_binary") as ensure,
        patch(f"{FLOW}.port_is_free", return_value=True),
        patch("custom_components.hottoh.async_setup_entry", return_value=True),
    ):
        result = await _embedded(hass)
        assert result["step_id"] == "embedded"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], EMBEDDED_INPUT
        )
        assert result["step_id"] == "thermostat"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"regulation_mode": "compensated"}
        )
    ensure.assert_awaited_once()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "mode": "embedded",
        "stove_host": "192.168.1.60",
        "stove_port": 5001,
    }
    assert result["result"].options == {
        "web_panel": True,
        "web_lan": True,
        "web_lan_port": 3000,
        "temperature_sensor": None,
        "regulation_mode": "compensated",
    }
    assert result["result"].unique_id == "192.168.1.60:5001"


async def test_embedded_flow_errors(hass: HomeAssistant) -> None:
    """Port of the interface taken, then download failed."""
    with (
        patch(f"{FLOW}.architecture", return_value="arm64"),
        patch(f"{FLOW}.async_ensure_binary", side_effect=EmbeddedError("offline")),
        patch(f"{FLOW}.port_is_free", return_value=False),
    ):
        result = await _embedded(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], EMBEDDED_INPUT
        )
        assert result["errors"] == {"web_lan_port": "port_in_use"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**EMBEDDED_INPUT, "web_lan": False}
        )
        assert result["errors"] == {"base": "download_failed"}


async def test_embedded_flow_unsupported(hass: HomeAssistant) -> None:
    """No release for this machine."""
    with patch(f"{FLOW}.architecture", return_value=None):
        result = await _embedded(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_architecture"


async def test_embedded_flow_stove_already_external(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The stove is already reached through another hottoh_api: one client only."""
    config_entry.add_to_hass(hass)
    with patch(f"{FLOW}.architecture", return_value="amd64"):
        result = await _embedded(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**EMBEDDED_INPUT, "stove_host": "192.168.1.50"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Thermostat, notifications, then web interface."""
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
    assert result["step_id"] == "web_external"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"web_panel": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options["temperature_sensor"] == "sensor.salon"
    assert config_entry.options["notify_services"] == ["notify.mobile_app_phone"]
    assert config_entry.options["web_panel"] is False
    assert hass.states.get("climate.poele").attributes["temperature_sensor"] == (
        "sensor.salon"
    )
