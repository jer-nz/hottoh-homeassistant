"""hottoh_api run by the integration (embedded mode).

The static Linux binary of a hottoh_api release is downloaded once, checked against the SHA-256
known to this version of the integration, and kept in `<config>/hottoh/bin`. Each embedded stove
gets its own process, `config.ini` and logs in `<config>/hottoh/<entry id>`. The API listens on
127.0.0.1 only; the web interface can also be opened on the local network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import configparser
import hashlib
import io
import logging
from pathlib import Path
import platform
import socket
import tarfile
import time

import aiohttp
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, HOTTOH_API_SHA256, HOTTOH_API_VERSION

_LOGGER = logging.getLogger(__name__)

DOWNLOAD_URL = (
    "https://github.com/jer-nz/hottoh_api/releases/download/v{version}/{archive}"
)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
LOCALHOST = "127.0.0.1"
# hottoh_api answers /api/status within a second; the first start may be slower
START_TIMEOUT_S = 30
STOP_TIMEOUT_S = 5
# pause before restarting a process that stopped by itself; back to the first after a long run
RESTART_DELAYS_S = (5, 15, 60, 300)
STABLE_RUN_S = 600

# Options of the module, as in the hottoh_api documentation, except `clock_write`: the clock of
# Home Assistant is kept by NTP, and hottoh_api writes it at the start of a minute
DEFAULT_FEATURES: dict[str, bool] = {
    "chrono_schedule_read": True,
    "chrono_schedule_write": True,
    "clock_read": True,
    "clock_write": True,
    "timezone_read": True,
    "timezone_write": False,
    "datalog_read": True,
    "datalog_clear": False,
    "pin_read": True,
    "pin_write": False,
    "module_restart": False,
    "wifi_scan": False,
    "cloud_read": True,
    "firmware_update_check": True,
}

ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


class EmbeddedError(Exception):
    """hottoh_api cannot be installed or started."""


def architecture() -> str | None:
    """Architecture of the release archive for this machine, None when there is none."""
    return ARCHITECTURES.get(platform.machine().lower())


def port_is_free(port: int, host: str = "0.0.0.0") -> bool:
    """True when nothing listens on this TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def free_local_port() -> int:
    """A TCP port nobody listens on, on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOCALHOST, 0))
        return int(sock.getsockname()[1])


def base_directory(hass: HomeAssistant) -> Path:
    """Folder of the integration in the configuration of Home Assistant."""
    return Path(hass.config.path(DOMAIN))


async def async_ensure_binary(hass: HomeAssistant) -> Path:
    """Path of the hottoh_api binary, downloaded first when missing."""
    arch = architecture()
    if arch is None:
        raise EmbeddedError(f"no hottoh_api release for {platform.machine()}")
    binary = base_directory(hass) / "bin" / f"hottoh_api-{HOTTOH_API_VERSION}"
    lock: asyncio.Lock = hass.data.setdefault(f"{DOMAIN}_binary_lock", asyncio.Lock())
    async with lock:
        if await hass.async_add_executor_job(binary.is_file):
            return binary
        archive = f"hottoh_api-{HOTTOH_API_VERSION}-linux-{arch}.tar.gz"
        url = DOWNLOAD_URL.format(version=HOTTOH_API_VERSION, archive=archive)
        _LOGGER.info("Downloading %s", url)
        try:
            async with async_get_clientsession(hass).get(
                url, timeout=DOWNLOAD_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EmbeddedError(f"cannot download {url}: {err}") from err
        await hass.async_add_executor_job(
            _install,
            data,
            HOTTOH_API_SHA256[arch],
            f"{archive[:-7]}/hottoh_api",
            binary,
        )
    return binary


def _install(data: bytes, sha256: str, member: str, binary: Path) -> None:
    """Checks the archive, extracts the binary and removes other versions."""
    if hashlib.sha256(data).hexdigest() != sha256:
        raise EmbeddedError("the downloaded archive does not match its SHA-256")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            extracted = tar.extractfile(member)
            if extracted is None:
                raise EmbeddedError(f"{member} missing from the archive")
            content = extracted.read()
    except (tarfile.TarError, KeyError) as err:
        raise EmbeddedError(f"invalid archive: {err}") from err
    binary.parent.mkdir(parents=True, exist_ok=True)
    partial = binary.with_name(binary.name + ".part")
    partial.write_bytes(content)
    partial.chmod(0o755)
    partial.replace(binary)
    for old in binary.parent.glob("hottoh_api-*"):
        if old != binary:
            old.unlink(missing_ok=True)


def read_features(path: Path) -> dict[str, bool]:
    """Features of an existing config.ini (changed from the web interface), else defaults."""
    features = dict(DEFAULT_FEATURES)
    if not path.is_file():
        return features
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as err:
        _LOGGER.warning("Ignoring the features of %s: %s", path, err)
        return features
    if parser.has_section("features"):
        for name in features:
            if parser.has_option("features", name):
                try:
                    features[name] = parser.getboolean("features", name)
                except ValueError:
                    continue
    return features


def render_config(
    stove_host: str,
    stove_port: int,
    api_port: int,
    web_lan_port: int | None,
    log_directory: Path,
    features: dict[str, bool],
) -> str:
    """config.ini of an embedded hottoh_api."""
    lines = [
        "# Written by the HottoH integration of Home Assistant at each start: change the",
        "# integration options instead. [features] is kept (changed from the web interface).",
        "[stove]",
        f"ip = {stove_host}",
        f"port = {stove_port}",
        "poll_interval_ms = 1000",
        "",
        "[http_api]",
        f"ip = {LOCALHOST}",
        f"port = {api_port}",
        "edit_features = true",
        "",
        "[web_ui]",
        "enabled = true",
        "open_browser = false",
    ]
    if web_lan_port is not None:
        # own server: interface and API on the local network
        lines += ["ip = 0.0.0.0", f"port = {web_lan_port}"]
    lines += [
        "",
        "[log]",
        "level = info",
        f"directory = {log_directory}",
        "max_log_files = 7",
        "compress = true",
        "max_file_size_mb = 20",
        "stats_interval_s = 3600",
        "",
        "[features]",
    ]
    lines += [f"{name} = {str(value).lower()}" for name, value in features.items()]
    return "\n".join(lines) + "\n"


class EmbeddedBridge:
    """One hottoh_api process, restarted when it stops by itself."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        stove_host: str,
        stove_port: int,
        web_lan_port: int | None,
    ) -> None:
        self.hass = hass
        self.stove_host = stove_host
        self.stove_port = stove_port
        self.web_lan_port = web_lan_port
        self.directory = base_directory(hass) / entry_id
        self.api_port = 0
        self._binary: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._stopping = False
        self._unsub_stop: Callable[[], None] | None = None

    @property
    def log_directory(self) -> Path:
        """Log files of hottoh_api."""
        return self.directory / "logs"

    @property
    def web_port(self) -> int:
        """Port serving the web interface (the API port unless opened on the network)."""
        return self.web_lan_port or self.api_port

    async def async_start(self) -> None:
        """Installs, configures and starts hottoh_api, then waits until it answers."""
        self._binary = await async_ensure_binary(self.hass)
        self.api_port = await self.hass.async_add_executor_job(free_local_port)
        await self.hass.async_add_executor_job(self._write_config)
        self._stopping = False
        await self._async_spawn()
        self._unsub_stop = self.hass.bus.async_listen(
            EVENT_HOMEASSISTANT_STOP, self._async_on_hass_stop
        )
        try:
            await self._async_wait_ready()
        except EmbeddedError:
            await self.async_stop()
            raise
        self._watcher = self.hass.async_create_background_task(
            self._async_watch(), f"{DOMAIN} hottoh_api {self.stove_host}"
        )

    def _write_config(self) -> None:
        self.log_directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "config.ini"
        path.write_text(
            render_config(
                self.stove_host,
                self.stove_port,
                self.api_port,
                self.web_lan_port,
                self.log_directory,
                read_features(path),
            ),
            encoding="utf-8",
        )

    async def _async_spawn(self) -> None:
        assert self._binary is not None
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(self._binary),
                str(self.directory / "config.ini"),
                cwd=self.directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            raise EmbeddedError(f"cannot start {self._binary}: {err}") from err
        _LOGGER.info(
            "hottoh_api %s started (pid %s) for the stove at %s:%s, API on %s:%s",
            HOTTOH_API_VERSION,
            self._process.pid,
            self.stove_host,
            self.stove_port,
            LOCALHOST,
            self.api_port,
        )
        self.hass.async_create_background_task(
            self._async_forward_output(self._process),
            f"{DOMAIN} hottoh_api output",
        )

    async def _async_forward_output(self, process: asyncio.subprocess.Process) -> None:
        """hottoh_api prints its errors (and panics) on stderr: into the log of HA."""
        assert process.stdout is not None
        while line := await process.stdout.readline():
            _LOGGER.warning(
                "hottoh_api %s: %s",
                self.stove_host,
                line.decode(errors="replace").rstrip(),
            )

    async def _async_wait_ready(self) -> None:
        session = async_get_clientsession(self.hass)
        url = f"http://{LOCALHOST}:{self.api_port}/api/status"
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._process is None or self._process.returncode is not None:
                raise EmbeddedError(
                    f"hottoh_api stopped at startup, see {self.log_directory}"
                )
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        return
            except aiohttp.ClientError, TimeoutError:
                pass
            await asyncio.sleep(0.5)
        raise EmbeddedError(f"hottoh_api did not answer within {START_TIMEOUT_S} s")

    async def _async_watch(self) -> None:
        """Restarts hottoh_api when it stops by itself."""
        failures = 0
        while not self._stopping and self._process is not None:
            started = time.monotonic()
            code = await self._process.wait()
            if self._stopping:
                return
            if time.monotonic() - started > STABLE_RUN_S:
                failures = 0
            delay = RESTART_DELAYS_S[min(failures, len(RESTART_DELAYS_S) - 1)]
            failures += 1
            _LOGGER.error(
                "hottoh_api for %s stopped (exit code %s), restarted in %s s; logs in %s",
                self.stove_host,
                code,
                delay,
                self.log_directory,
            )
            await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                await self._async_spawn()
            except EmbeddedError as err:
                _LOGGER.error("%s", err)

    async def async_stop(self) -> None:
        """Stops hottoh_api (SIGTERM, then SIGKILL)."""
        self._stopping = True
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
        _LOGGER.info("hottoh_api for %s stopped", self.stove_host)

    @callback
    def _async_on_hass_stop(self, _event: Event) -> None:
        self._unsub_stop = None
        self.hass.async_create_task(self.async_stop())
