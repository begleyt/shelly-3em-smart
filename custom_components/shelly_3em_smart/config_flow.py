from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, DEFAULT_HOST, DEFAULT_PORT, DOMAIN
from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)


class ShellyAddonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask the user where the Shelly 3EM Smart Monitor add-on is reachable."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])

            session = async_get_clientsession(self.hass)
            client = ShellyAddonClient(session, host, port)
            try:
                info = await client.info()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                unique_id = f"{host}:{port}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Shelly 3EM Smart ({host}:{port})",
                    data={CONF_HOST: host, CONF_PORT: port},
                    description_placeholders={
                        "version": info.get("version", "?"),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )
