"""Fixtures: a stateful fake hottoh_api behind the aiohttp mocker."""

from __future__ import annotations

from collections.abc import Generator
import copy
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch

from aiohttp import ClientError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.hottoh.const import DOMAIN

HOST = "192.168.1.10"
PORT = 3000
BASE = f"http://{HOST}:{PORT}"
FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    """JSON fixture captured from a real hottoh_api 2.3.0."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeBridge:
    """Answers like hottoh_api; writes change the served pages at once."""

    def __init__(self) -> None:
        self.pages: dict[str, Any] = {
            "status": load("status"),
            "inf": load("inf"),
            "dat/0": load("dat_0"),
            "dat/1": load("dat_1"),
            "dat/2": load("dat_2"),
            "alarms": load("alarms"),
            "features": load("features"),
            "firmware": {
                "installed": "10.5.0",
                "latest_available": "10.5.0",
                "update_available": False,
            },
        }
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.requests: dict[int, dict[str, Any]] = {}
        self.write_status = "ok"
        self.write_error_code: int | None = None
        self.down = False
        # False: the stove applies writes later (`apply_pending`), as the real board does
        self.apply_writes = True
        self.pending: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1

    @property
    def dat0(self) -> dict[str, Any]:
        """Main page."""
        return self.pages["dat/0"]

    def set_alarm(
        self, state_raw: int, started_at: str, ended_at: str | None = None
    ) -> None:
        """Puts an alarm at the head of the history."""
        event = {
            "state": str(state_raw),
            "state_raw": state_raw,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        events = [
            e for e in self.pages["alarms"]["events"] if e["started_at"] != started_at
        ]
        self.pages["alarms"] = {
            "current": event if ended_at is None else None,
            "events": [event, *events],
        }
        self.dat0["index_stove_state_raw"] = state_raw if ended_at is None else 0

    def module_restarting(self) -> dict[str, Any]:
        """Page 0 of a module whose Modbus link is not up yet; returns the real page."""
        page = self.dat0
        self.pages["dat/0"] = {
            key: value if isinstance(value, str) else type(value)()
            for key, value in page.items()
        }
        return page

    def apply_pending(self) -> None:
        """The stove applies the writes it acknowledged."""
        for path, body in self.pending:
            self._apply(path, body)
        self.pending = []

    def _apply(self, path: str, body: dict[str, Any]) -> None:
        d0 = self.dat0
        match path:
            case "dat/set_on_off":
                d0["index_stove_on"] = body["value"]
                d0["index_stove_state_raw"] = 1 if body["value"] else 9
            case "dat/set_eco_mode":
                d0["index_eco_mode"] = body["value"]
            case "dat/set_chrono_mode":
                d0["index_chrono_mode"] = 2 if body["value"] else 0
            case "dat/set_power_level":
                d0["index_power_set"] = body["value"]
            case "dat/set_ambiance_temp":
                d0[f"index_ambient_t{body['ambiance']}_set"] = body["value"]
            case "dat/set_fan_speed":
                d0[f"index_fan_{body['fan']}_set"] = body["value"]
            case "dat/set_chrono_temp":
                self.pages["dat/1"][f"index_program_{body['chrono']}_temp"] = body[
                    "value"
                ]

    async def handle(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        """Answers one request."""
        if self.down:
            return AiohttpClientMockResponse(method, url, exc=ClientError("down"))
        path = url.path.removeprefix("/api/")
        if method.upper() == "GET":
            if match := re.fullmatch(r"request/(\d+)", path):
                return AiohttpClientMockResponse(
                    method, url, json=self.requests[int(match[1])]
                )
            if path in self.pages:
                return AiohttpClientMockResponse(
                    method, url, json=copy.deepcopy(self.pages[path])
                )
            return AiohttpClientMockResponse(
                method, url, status=404, json={"error": path}
            )
        body = data or {}
        request_id = self._next_id
        self._next_id += 1
        self.writes.append((path, body))
        if self.write_status == "ok":
            if self.apply_writes:
                self._apply(path, body)
            else:
                self.pending.append((path, body))
        state: dict[str, Any] = {
            "request_id": request_id,
            "command": path,
            "value": str(body.get("value", "")),
            "status": self.write_status,
        }
        if self.write_error_code is not None:
            state["error_code"] = self.write_error_code
        self.requests[request_id] = state
        return AiohttpClientMockResponse(
            method,
            url,
            json={"success": True, "request_id": request_id},
        )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Loads custom_components/hottoh."""


@pytest.fixture(autouse=True)
def fast_writes() -> Generator[None]:
    """No pause between two polls of a write status."""
    with patch("custom_components.hottoh.api.WRITE_POLL_S", 0):
        yield


@pytest.fixture
def bridge(aioclient_mock: AiohttpClientMocker) -> FakeBridge:
    """Fake hottoh_api on BASE."""
    fake = FakeBridge()
    pattern = re.compile(re.escape(BASE) + r"/.*")
    for method in ("get", "post"):
        aioclient_mock.request(method, pattern, side_effect=fake.handle)
    return fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Entry of the fake hottoh_api."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Poêle",
        unique_id="192.168.1.50:5001",
        data={"host": HOST, "port": PORT},
        options={},
    )
