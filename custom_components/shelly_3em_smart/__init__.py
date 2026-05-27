from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ENERGY_ENTITIES, CONF_HOST, CONF_PORT, CONF_TRACKED_ENTITIES, DOMAIN
from .coordinator import ShellyAddonClient, ShellyAddonCoordinator
from .ha_energy_poller import setup_energy_poller
from .ha_listener import setup_ha_event_listener

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = ShellyAddonClient(session, entry.data[CONF_HOST], entry.data[CONF_PORT])

    try:
        info = await client.info()
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot reach Shelly 3EM add-on: {err}") from err

    coordinator = ShellyAddonCoordinator(hass, client, info)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Wire up the HA state-change listener for any entities the user has
    # configured in the options flow.
    tracked = list(entry.options.get(CONF_TRACKED_ENTITIES, []))
    unsub_listener = setup_ha_event_listener(hass, client, tracked)
    entry.async_on_unload(unsub_listener)

    # Wire up the energy poller for cumulative-kWh sensors.
    energy_entities = list(entry.options.get(CONF_ENERGY_ENTITIES, []))
    unsub_energy = setup_energy_poller(hass, client, energy_entities)
    entry.async_on_unload(unsub_energy)

    # Reload when the user changes the options.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
