"""Polling of one hottoh_api instance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HottohClient, HottohError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from . import HottohConfigEntry
    from .notifier import HottohNotifier

_LOGGER = logging.getLogger(__name__)

# hottoh_api acknowledges a write before it reads the pages again (one cycle, 1 to 3 s), and
# the stove board applies some values (power level) only 3 to 5 s after its `OK`: read twice
REFRESH_AFTER_WRITE_S = (3, 7)


@dataclass(slots=True)
class HottohData:
    """One poll of hottoh_api."""

    status: dict[str, Any]
    inf: dict[str, Any]
    dat0: dict[str, Any]
    dat1: dict[str, Any]
    dat2: dict[str, Any]
    alarms: dict[str, Any] = field(default_factory=dict)

    @property
    def connected(self) -> bool:
        """hottoh_api currently talks with the stove."""
        return bool(self.status.get("connected"))

    @property
    def state_raw(self) -> int | None:
        """Raw stove state."""
        value = self.dat0.get("index_stove_state_raw")
        return int(value) if value is not None else None


class HottohCoordinator(DataUpdateCoordinator[HottohData]):
    """Reads every page of hottoh_api every few seconds."""

    config_entry: HottohConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HottohConfigEntry,
        client: HottohClient,
        features: dict[str, bool],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}:{client.port}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.features = features
        self.notifier: HottohNotifier | None = None
        self._unsub_write_refresh: list[Callable[[], None]] = []

    async def _async_update_data(self) -> HottohData:
        try:
            raw = await self.client.fetch_all()
        except HottohError as err:
            if self.notifier is not None:
                self.notifier.bridge_unreachable()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        data = HottohData(**raw)
        if self.notifier is not None:
            self.notifier.process(data)
        return data

    @callback
    def async_refresh_after_write(self) -> None:
        """Reads the pages again once hottoh_api has polled the stove after a write."""
        self._cancel_write_refresh()
        self._unsub_write_refresh = [
            async_call_later(self.hass, delay, self._async_write_refresh)
            for delay in REFRESH_AFTER_WRITE_S
        ]

    async def _async_write_refresh(self, _now: datetime) -> None:
        await self.async_refresh()

    @callback
    def _cancel_write_refresh(self) -> None:
        for unsub in self._unsub_write_refresh:
            unsub()
        self._unsub_write_refresh = []

    async def async_shutdown(self) -> None:
        """Cancels a pending refresh."""
        self._cancel_write_refresh()
        await super().async_shutdown()

    def feature(self, name: str) -> bool:
        """Module feature enabled in hottoh_api."""
        return bool(self.features.get(name))
