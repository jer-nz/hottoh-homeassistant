#!/usr/bin/env python3
"""Fake HottoH Wifier 2.0 module, to run hottoh_api and the integration without a stove.

Speaks the local TCP protocol of firmware 10.5.0 (INF, DAT R/W, SCH, CLK, TMZ) and simulates
ignition, heating and shutdown, faster than a real stove. A small HTTP control port reads and
changes the simulation:

    python3 tools/fake_stove.py --port 5001 --control-port 5080 --speed 10
    curl localhost:5080/                                      # state
    curl -X POST localhost:5080/ -d '{"alarm": 61}'           # no pellets
    curl -X POST localhost:5080/ -d '{"alarm": null}'         # alarm cleared
    curl -X POST localhost:5080/ -d '{"room": 18.5}'          # stove probe

Point hottoh_api at it with `[stove] ip = 127.0.0.1`, `port = 5001`. Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
import time
from typing import Any

_LOGGER = logging.getLogger("fake_stove")

# ignition goes through these states (no 6), as measured on a real stove
IGNITION = [1, 2, 3, 4, 5, 7]


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def frame(req_id: str, command: str, kind: str, params: str) -> bytes:
    """Answer frame `#<id>C---<len><cmd><R|W|E><params><crc>\\n`."""
    body = f"{req_id}C---{len(params):04X}{command}{kind}{params}"
    return f"#{body}{crc16(body.encode()):04X}\n".encode()


class Stove:
    """State of the simulated stove."""

    def __init__(self, speed: float) -> None:
        self.lock = threading.Lock()
        self.speed = speed
        self.on = False
        self.state = 0
        self.state_since = time.monotonic()
        self.alarm: int | None = None
        self.eco = False
        self.chrono = 0
        self.room = 19.0
        self.room_set = 21.0
        self.room2_set = 20.0
        self.smoke = 20.0
        self.power_level = 0
        self.power_set = 5
        self.fans = [3, 0, 0]
        self.programs = [21.0, 20.5, 23.0]
        self.schedule = [0] * (7 * 48)
        self.pellet_setpoint = 0

    # --- simulation ----------------------------------------------------------

    def _elapsed(self) -> float:
        return (time.monotonic() - self.state_since) * self.speed

    def _goto(self, state: int) -> None:
        self.state = state
        self.state_since = time.monotonic()

    def tick(self, dt: float) -> None:
        """Advances the simulation by `dt` real seconds."""
        dt *= self.speed
        with self.lock:
            if self.alarm is not None:
                self.state = self.alarm
            elif self.on and self.state in IGNITION:
                if self._elapsed() >= 120:
                    index = IGNITION.index(self.state)
                    self._goto(IGNITION[index + 1] if index + 1 < len(IGNITION) else 8)
            elif self.on and self.state == 8:
                if self.power_level < 100:
                    self.power_level = min(
                        100, self.power_level + int(dt / 45 * 10) or 1
                    )
                self.smoke = min(180.0, self.smoke + dt * 0.5)
                self.room += dt * 0.002 * self.power_level / 100
            elif self.on and self.state in (0, 9, 10):
                self._goto(1)
            elif not self.on and self.state in (*IGNITION, 8):
                self._goto(9)
            elif self.state == 9 and self._elapsed() >= 180:
                self._goto(10)
            elif self.state == 10 and self._elapsed() >= 600:
                self._goto(0)
            if self.state != 8:
                self.power_level = max(0, self.power_level - int(dt))
                self.smoke = max(20.0, self.smoke - dt * 0.2)
                self.room = max(12.0, self.room - dt * 0.0005)

    # --- protocol --------------------------------------------------------------

    def dat(self, page: str) -> str:
        """DAT page 0, 1 or 2, in tenths of °C."""
        t = lambda value: str(round(value * 10))  # noqa: E731
        with self.lock:
            if page == "0":
                # fmt: off
                fields = [
                    0, 9, 0, 1, 9, self.state, int(self.on), int(self.eco), self.chrono,
                    t(self.room), t(self.room_set), 50, 550,
                    t(self.room), t(self.room2_set), 50, 550,
                    -435, 0, 0, 0,
                    t(self.smoke), self.power_level, self.power_set, 1, 10,
                    0,
                    self.fans[0], self.fans[0], 5,
                    self.fans[1], self.fans[1], 5,
                    self.fans[2], self.fans[2], 0,
                ]
                # fmt: on
            elif page == "1":
                fields = [1, self.chrono]
                for program in self.programs:
                    fields += [t(program), 50, 350]
            else:
                fields = [2, 1, 0, self.fans[0], self.fans[1], 0] + [0] * 16
        return "".join(f"{f};" for f in fields)

    def write(self, index: int, value: int) -> str:
        """DAT W: `OK;` or `ERR;17;`."""
        with self.lock:
            match index:
                case 0 if value in (0, 1):
                    self.on = bool(value)
                    if not self.on and self.alarm is None and self.state in IGNITION:
                        self._goto(9)
                case 1 if value in (0, 1):
                    self.eco = bool(value)
                case 2 if 0 <= value <= 100:
                    self.power_set = value
                case 3 if 50 <= value <= 550:
                    self.room_set = value / 10
                case 4 if 50 <= value <= 550:
                    self.room2_set = value / 10
                case 5 | 6 | 7 if 0 <= value <= 101:
                    self.fans[index - 5] = value
                case 8 if value in (0, 2):
                    self.chrono = value
                case 9 | 10 | 11 if 50 <= value <= 350:
                    self.programs[index - 9] = value / 10
                case 12 | 13 | 14 | 15:
                    pass
                case 16:
                    self.pellet_setpoint = value
                case _:
                    return "ERR;17;"
        return "OK;"

    def answer(self, command: str, kind: str, params: list[str]) -> str | None:
        """Parameters of the answer, None for no answer."""
        if command == "INF":
            return "HOTTOH32;10.5.0;196;"
        if command == "DAT" and kind == "R" and params and params[0] in ("0", "1", "2"):
            return self.dat(params[0])
        if command == "DAT" and kind == "W" and len(params) >= 2:
            try:
                return self.write(int(params[0]), int(params[1]))
            except ValueError:
                return "OK;"
        if command == "SCH" and kind == "R":
            with self.lock:
                return "0;" + "".join(f"{s};" for s in self.schedule)
        if command == "SCH" and kind == "W" and len(params) == 1 + 7 * 48:
            with self.lock:
                self.schedule = [int(p) for p in params[1:]]
            return "OK;"
        if command == "CLK" and kind == "R":
            now = int(time.time())
            local = time.localtime(now)
            return f"{now};{local.tm_hour * 3600 + local.tm_min * 60};"
        if command in ("CLK", "TMZ") and kind == "W":
            return "OK;"
        if command == "TMZ" and kind == "R":
            return '\\"Europe/Paris\\";'
        return None

    def control(self, changes: dict[str, Any]) -> None:
        """Changes from the control port."""
        with self.lock:
            if "alarm" in changes:
                self.alarm = changes["alarm"]
                if self.alarm is None:
                    self.on = False
                    self._goto(0)
                else:
                    self.on = False
                    self._goto(self.alarm)
            if "room" in changes:
                self.room = float(changes["room"])
            if "speed" in changes:
                self.speed = float(changes["speed"])

    def snapshot(self) -> dict[str, Any]:
        """Readable state."""
        with self.lock:
            return {
                "on": self.on,
                "state": self.state,
                "alarm": self.alarm,
                "room": round(self.room, 2),
                "room_set": self.room_set,
                "power_level": self.power_level,
                "power_set": self.power_set,
                "eco": self.eco,
                "chrono": self.chrono,
                "speed": self.speed,
            }


async def serve_client(
    stove: Stove, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """One client, like the real module (which refuses a second one)."""
    peer = writer.get_extra_info("peername")
    _LOGGER.info("client %s connected", peer)
    try:
        while line := await reader.readline():
            text = line.decode("ascii", "replace").rstrip("\n")
            start = text.rfind("#")
            if start < 0 or len(text) - start < 23:
                continue
            text = text[start:]
            body, crc = text[1:-4], text[-4:]
            if f"{crc16(body.encode()):04X}" != crc.upper():
                _LOGGER.warning("bad CRC: %s", text)
                continue
            req_id, command, kind = body[0:5], body[13:16], body[16]
            length = int(body[9:13], 16)
            params = body[17 : 17 + length].removesuffix(";").split(";")
            reply = stove.answer(command, kind, params)
            _LOGGER.debug("%s %s %s -> %s", command, kind, params, reply)
            if reply is not None:
                writer.write(frame(req_id, command, kind, reply))
                await writer.drain()
    except ConnectionError:
        pass
    finally:
        _LOGGER.info("client %s gone", peer)
        writer.close()


def control_server(stove: Stove, port: int) -> ThreadingHTTPServer:
    """GET: state; POST {"alarm": 61|null, "room": 18.5, "speed": 10}."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            body = json.dumps(stove.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._reply()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            try:
                stove.control(json.loads(self.rfile.read(length) or b"{}"))
            except (ValueError, TypeError) as err:
                self.send_error(400, str(err))
                return
            self._reply()

        def log_message(self, fmt: str, *args: Any) -> None:
            _LOGGER.debug("control: " + fmt, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--control-port", type=int, default=5080)
    parser.add_argument("--speed", type=float, default=10, help="time factor")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    stove = Stove(args.speed)
    control_server(stove, args.control_port)
    busy = asyncio.Lock()

    async def client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if busy.locked():  # one client at a time, like the module
            writer.close()
            return
        async with busy:
            await serve_client(stove, reader, writer)

    server = await asyncio.start_server(client, args.host, args.port)
    _LOGGER.info(
        "fake stove on %s:%s, control on :%s, speed x%s",
        args.host,
        args.port,
        args.control_port,
        args.speed,
    )
    async with server:
        while True:
            await asyncio.sleep(1)
            stove.tick(1)


if __name__ == "__main__":
    asyncio.run(main())
