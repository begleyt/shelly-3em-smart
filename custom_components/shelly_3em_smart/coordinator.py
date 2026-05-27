from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class ShellyAddonClient:
    """Tiny async HTTP client for the Shelly 3EM Smart Monitor add-on REST API."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base = f"http://{host}:{port}/api"

    async def _get(self, path: str) -> Any:
        async with self._session.get(f"{self._base}{path}", timeout=aiohttp.ClientTimeout(total=5)) as r:
            r.raise_for_status()
            return await r.json()

    async def info(self) -> dict:
        return await self._get("/info")

    async def live(self) -> dict:
        return await self._get("/live")

    async def devices(self) -> list[dict]:
        return await self._get("/devices")

    async def stats(self) -> dict:
        return await self._get("/stats")

    async def post_ha_event(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
        friendly_name: str | None,
        ts: float | None,
    ) -> None:
        payload = {
            "entity_id": entity_id,
            "old_state": old_state,
            "new_state": new_state,
            "friendly_name": friendly_name,
            "ts": ts,
        }
        try:
            async with self._session.post(
                f"{self._base}/ha_event",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                r.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Don't crash the listener for a transient post failure; the next
            # state change will retry on its own.
            pass

    async def post_ha_energy_reading(
        self,
        entity_id: str,
        energy_kwh: float,
        power_w: float | None = None,
        friendly_name: str | None = None,
        ts: float | None = None,
    ) -> None:
        payload = {
            "entity_id": entity_id,
            "energy_kwh": energy_kwh,
            "power_w": power_w,
            "friendly_name": friendly_name,
            "ts": ts,
        }
        try:
            async with self._session.post(
                f"{self._base}/ha_energy_reading",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                r.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass


class ShellyAddonCoordinator(DataUpdateCoordinator[dict]):
    """Polls /api/live and /api/devices every couple of seconds."""

    def __init__(self, hass: HomeAssistant, client: ShellyAddonClient, info: dict) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.info = info

    async def _async_update_data(self) -> dict:
        try:
            live, devices = await asyncio.gather(
                self.client.live(),
                self.client.devices(),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Add-on API unreachable: {err}") from err
        return {"live": live or {}, "devices": devices or []}
