from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ENERGY_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_TRACKED_ENTITIES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
    TRACKABLE_DOMAINS,
)
from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)


def _entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(multiple=True, domain=TRACKABLE_DOMAINS)
    )


def _energy_entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            multiple=True,
            domain="sensor",
            device_class="energy",
        )
    )


class ShellyAddonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup: host + port, validated against /api/info."""

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
                    description_placeholders={"version": info.get("version", "?")},
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

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "ShellyAddonOptionsFlow":
        # OptionsFlow base class exposes self.config_entry as a read-only
        # property in HA 2024.11+; do not pass it through or assign to it.
        return ShellyAddonOptionsFlow()


class ShellyAddonOptionsFlow(config_entries.OptionsFlow):
    """Lets the user pick which HA entities to forward state changes from."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_tracked = self.config_entry.options.get(CONF_TRACKED_ENTITIES, [])
        current_energy = self.config_entry.options.get(CONF_ENERGY_ENTITIES, [])
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TRACKED_ENTITIES, default=current_tracked): _entity_selector(),
                    vol.Optional(CONF_ENERGY_ENTITIES,  default=current_energy):  _energy_entity_selector(),
                }
            ),
        )
