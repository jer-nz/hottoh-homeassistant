"""Diagnostics."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hottoh.diagnostics import async_get_config_entry_diagnostics

from .conftest import HOST, FakeBridge


async def test_diagnostics_redact_addresses(
    hass: HomeAssistant, bridge: FakeBridge, config_entry: MockConfigEntry
) -> None:
    """Addresses of the network are not in a report that gets shared."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    report = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(report, default=str)
    assert HOST not in text
    assert "192.168.1.50" not in text  # stove address seen by hottoh_api
    assert report["data"]["host"] == "**REDACTED**"
    assert report["poll"]["status"]["stove_address"] == "**REDACTED**"
    assert report["poll"]["status"]["connected"] is True
