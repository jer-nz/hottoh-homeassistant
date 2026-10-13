"""Embedded hottoh_api and web interface relay."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
from unittest.mock import AsyncMock, patch

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.hottoh.const import DOMAIN
from custom_components.hottoh.embedded import (
    DEFAULT_FEATURES,
    EmbeddedBridge,
    EmbeddedError,
    _install,
    read_features,
    render_config,
)

from .conftest import FakeBridge

EMBEDDED_PORT = 39123


def test_config_keeps_features(tmp_path: Path) -> None:
    """Features changed from the web interface survive the next start."""
    path = tmp_path / "config.ini"
    assert read_features(path) == DEFAULT_FEATURES
    assert DEFAULT_FEATURES["clock_write"] is True
    features = {**DEFAULT_FEATURES, "wifi_scan": True}
    path.write_text(
        render_config("192.168.1.60", 5001, 4000, None, tmp_path / "logs", features)
    )
    assert read_features(path)["wifi_scan"] is True


def test_config_web_on_network(tmp_path: Path) -> None:
    """API on 127.0.0.1; the interface gets its own server on the network."""
    text = render_config("stove", 5001, 4000, 3000, tmp_path, DEFAULT_FEATURES)
    http_api = text.split("[http_api]")[1].split("[web_ui]")[0]
    web_ui = text.split("[web_ui]")[1].split("[log]")[0]
    assert "ip = 127.0.0.1" in http_api
    assert "port = 4000" in http_api
    assert "ip = 0.0.0.0" in web_ui
    assert "port = 3000" in web_ui
    local = render_config("stove", 5001, 4000, None, tmp_path, DEFAULT_FEATURES)
    assert "0.0.0.0" not in local


def _archive(member: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_install(tmp_path: Path) -> None:
    """Checked archive, executable binary, older versions removed."""
    member = "hottoh_api-9.9.9-linux-amd64/hottoh_api"
    data = _archive(member, b"binary")
    binary = tmp_path / "bin" / "hottoh_api-9.9.9"
    binary.parent.mkdir()
    (binary.parent / "hottoh_api-9.9.8").write_bytes(b"old")
    with pytest.raises(EmbeddedError, match="SHA-256"):
        _install(data, "0" * 64, member, binary)
    assert not binary.exists()
    _install(data, hashlib.sha256(data).hexdigest(), member, binary)
    assert binary.read_bytes() == b"binary"
    assert binary.stat().st_mode & 0o111
    assert [p.name for p in binary.parent.iterdir()] == ["hottoh_api-9.9.9"]


@pytest.fixture
def embedded_bridge(aioclient_mock: AiohttpClientMocker) -> FakeBridge:
    """Fake hottoh_api on the local port of the embedded process."""
    fake = FakeBridge()
    pattern = re.compile(re.escape(f"http://127.0.0.1:{EMBEDDED_PORT}") + r"/.*")
    for method in ("get", "post"):
        aioclient_mock.request(method, pattern, side_effect=fake.handle)
    return fake


async def test_embedded_setup(hass: HomeAssistant, embedded_bridge: FakeBridge) -> None:
    """hottoh_api started with the entry and stopped with it; panel of the stove."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Poêle",
        unique_id="192.168.1.50:5001",
        data={"mode": "embedded", "stove_host": "192.168.1.50", "stove_port": 5001},
        options={"web_panel": True, "web_lan": False, "web_lan_port": 3000},
    )
    entry.add_to_hass(hass)

    async def start(self: EmbeddedBridge) -> None:
        self.api_port = EMBEDDED_PORT

    stop = AsyncMock()
    with (
        patch.object(EmbeddedBridge, "async_start", start),
        patch.object(EmbeddedBridge, "async_stop", stop),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        runtime = entry.runtime_data
        assert runtime.embedded is not None
        assert runtime.embedded.stove_host == "192.168.1.50"
        assert runtime.panel == "hottoh-poele"
        panel = hass.data[DATA_PANELS]["hottoh-poele"]
        assert panel.require_admin
        assert panel.config["relay"] == f"/api/hottoh/web/{entry.entry_id}/"
        assert await hass.config_entries.async_unload(entry.entry_id)
    stop.assert_awaited_once()
    assert "hottoh-poele" not in hass.data[DATA_PANELS]


async def test_remove_entry_files(hass: HomeAssistant, tmp_path: Path) -> None:
    """Removing a stove deletes its folder; the binary goes with the last embedded stove."""
    entries = [
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=f"192.168.1.{n}:5001",
            data={
                "mode": "embedded",
                "stove_host": f"192.168.1.{n}",
                "stove_port": 5001,
            },
        )
        for n in (50, 51)
    ]
    external = MockConfigEntry(
        domain=DOMAIN, unique_id="h:3000", data={"host": "h", "port": 3000}
    )
    base = tmp_path / "hottoh"
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "hottoh_api-2.3.1").write_bytes(b"binary")
    for entry in [*entries, external]:
        entry.add_to_hass(hass)
        (base / entry.entry_id / "logs").mkdir(parents=True)
    with (
        patch("custom_components.hottoh.async_setup_entry", return_value=True),
        patch("custom_components.hottoh.embedded.base_directory", return_value=base),
    ):
        await hass.config_entries.async_remove(entries[0].entry_id)
        assert not (base / entries[0].entry_id).exists()
        assert (base / "bin" / "hottoh_api-2.3.1").exists()
        await hass.config_entries.async_remove(external.entry_id)
        assert (base / external.entry_id).exists()
        await hass.config_entries.async_remove(entries[1].entry_id)
    assert not (base / entries[1].entry_id).exists()
    assert not (base / "bin").exists()


async def test_embedded_start_failed(hass: HomeAssistant) -> None:
    """Setup retried later when hottoh_api cannot start."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mode": "embedded", "stove_host": "192.168.1.50", "stove_port": 5001},
    )
    entry.add_to_hass(hass)
    with patch.object(
        EmbeddedBridge, "async_start", side_effect=EmbeddedError("offline")
    ):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_web_relay(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client,
    hass_read_only_access_token: str,
) -> None:
    """The relay needs a session, opened by an administrator only."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    url = f"/api/hottoh/web/{config_entry.entry_id}/api/status"

    reader = await hass_client(hass_read_only_access_token)
    assert (await reader.post("/api/hottoh/session")).status == 401
    assert (await reader.get(url)).status == 401

    admin = await hass_client()
    assert (await admin.get(url)).status == 401
    resp = await admin.post("/api/hottoh/session")
    assert resp.status == 200
    assert resp.cookies["hottoh_session"]["httponly"]
    resp = await admin.get(url)
    assert resp.status == 200
    # the aiohttp mocker sends no Content-Type
    assert json.loads(await resp.text())["stove_address"] == "192.168.1.50:5001"
    resp = await admin.get(f"/api/hottoh/web/{config_entry.entry_id}/api/nothing")
    assert resp.status == 404
    assert (await admin.get("/api/hottoh/web/unknown/api/status")).status == 404
