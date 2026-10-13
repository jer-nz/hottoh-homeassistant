"""Client of the hottoh_api HTTP API (https://github.com/jer-nz/hottoh_api)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
# hottoh_api ends a write as `timeout` after 60 s: wait a little longer
WRITE_WAIT_S = 70
# a clock write without time is first held until the start of the next minute (up to 60 s)
CLOCK_WAIT_S = 60 + WRITE_WAIT_S
WRITE_POLL_S = 0.5

WRITE_OK = "ok"
WRITE_ERROR = "error"
WRITE_TIMEOUT = "timeout"

STOVE_ERRORS = {
    17: "value out of range or missing",
    18: "module refused the value",
    19: "stove board refused the value",
}


class HottohError(Exception):
    """Base error of the client."""


class HottohConnectionError(HottohError):
    """hottoh_api cannot be reached or answered garbage."""


class HottohRequestError(HottohError):
    """hottoh_api refused the request (HTTP 4xx/5xx)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class HottohWriteError(HottohError):
    """The stove refused a write or did not answer it."""

    def __init__(self, result: WriteResult) -> None:
        detail = result.status
        if result.error_code is not None:
            detail += f" {result.error_code} ({STOVE_ERRORS.get(result.error_code, 'stove error')})"
        super().__init__(f"{result.command}: {detail}")
        self.result = result


@dataclass(slots=True)
class WriteResult:
    """Outcome of a write, from `GET /api/request/<id>`."""

    request_id: int
    command: str
    value: str
    status: str
    error_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Service response."""
        return {
            "request_id": self.request_id,
            "command": self.command,
            "value": self.value,
            "status": self.status,
            "error_code": self.error_code,
        }


class HottohClient:
    """Asynchronous client of one hottoh_api instance."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(
                method, url, json=json, timeout=REQUEST_TIMEOUT
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError as err:
                    raise HottohConnectionError(f"{url}: invalid JSON") from err
                if resp.status >= 400:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise HottohRequestError(resp.status, message or str(body))
                return body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HottohConnectionError(f"{url}: {err}") from err

    async def get(self, path: str) -> Any:
        """GET a JSON document."""
        return await self._request("GET", path)

    async def status(self) -> dict[str, Any]:
        """Link with the stove, version of hottoh_api."""
        return await self.get("/api/status")

    async def features(self) -> dict[str, bool]:
        """Enabled module features."""
        return await self.get("/api/features")

    async def firmware(self) -> dict[str, Any]:
        """Installed and newest module firmware."""
        return await self.get("/api/firmware")

    async def fetch_all(self) -> dict[str, Any]:
        """Everything polled by the coordinator, in parallel."""
        paths = {
            "status": "/api/status",
            "inf": "/api/inf",
            "dat0": "/api/dat/0",
            "dat1": "/api/dat/1",
            "dat2": "/api/dat/2",
            "alarms": "/api/alarms",
        }
        results = await asyncio.gather(*(self.get(p) for p in paths.values()))
        return dict(zip(paths, results, strict=True))

    async def write(
        self, path: str, body: dict[str, Any], wait_s: float = WRITE_WAIT_S
    ) -> WriteResult:
        """Queues a write and waits for the answer of the stove.

        Raises HottohWriteError when the stove refuses it or does not answer.
        """
        answer = await self._request("POST", path, json=body)
        request_id = int(answer["request_id"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            await asyncio.sleep(WRITE_POLL_S)
            state = await self.get(f"/api/request/{request_id}")
            if state["status"] in (WRITE_OK, WRITE_ERROR, WRITE_TIMEOUT):
                break
            if loop.time() > deadline:
                state["status"] = WRITE_TIMEOUT
                break
        result = WriteResult(
            request_id=request_id,
            command=str(state.get("command", path)),
            value=str(state.get("value", "")),
            status=state["status"],
            error_code=state.get("error_code"),
        )
        _LOGGER.debug("Write %s %s: %s", path, body, result)
        if result.status != WRITE_OK:
            raise HottohWriteError(result)
        return result

    async def set_on_off(self, on: bool) -> WriteResult:
        """Turns the stove on or off."""
        return await self.write("/api/dat/set_on_off", {"value": on})

    async def set_eco_mode(self, enabled: bool) -> WriteResult:
        """Eco mode."""
        return await self.write("/api/dat/set_eco_mode", {"value": enabled})

    async def set_chrono_mode(self, enabled: bool) -> WriteResult:
        """Chrono (weekly schedule) mode."""
        return await self.write("/api/dat/set_chrono_mode", {"value": enabled})

    async def set_power_level(self, level: int) -> WriteResult:
        """Power level set point."""
        return await self.write("/api/dat/set_power_level", {"value": level})

    async def set_ambiance_temperature(
        self, ambiance: int, value: float
    ) -> WriteResult:
        """Room set point (ambiance 1 or 2)."""
        return await self.write(
            "/api/dat/set_ambiance_temp", {"ambiance": ambiance, "value": value}
        )

    async def set_chrono_temperature(self, program: int, value: float) -> WriteResult:
        """Temperature of chrono program 1 to 3."""
        return await self.write(
            "/api/dat/set_chrono_temp", {"chrono": program, "value": value}
        )

    async def set_fan_speed(self, fan: int, value: int) -> WriteResult:
        """Set point of fan 1 to 3."""
        return await self.write("/api/dat/set_fan_speed", {"fan": fan, "value": value})

    async def set_schedule(self, days: list[dict[str, Any]]) -> WriteResult:
        """Replaces the weekly schedule of some days."""
        return await self.write("/api/chrono/schedule", {"days": days})

    async def get_schedule(self) -> dict[str, Any]:
        """Weekly schedule (read from the stove, about 2 s)."""
        return await self.get("/api/chrono/schedule")

    async def sync_clock(self) -> WriteResult:
        """Sets the module and stove clocks from the clock of hottoh_api."""
        return await self.write("/api/clock", {}, CLOCK_WAIT_S)

    async def restart_module(self) -> None:
        """Restarts the Wi-Fi module (no answer expected)."""
        await self._request("POST", "/api/module/restart", json={})
