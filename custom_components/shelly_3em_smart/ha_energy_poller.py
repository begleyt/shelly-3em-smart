"""Periodically poll user-selected energy sensors and forward each
cumulative kWh reading to the add-on."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import ENERGY_POLL_INTERVAL_S
from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)

# Acceptable units; values get normalised to kWh.
_KWH_UNITS = {"kwh", "kilowatt hours", "kilowatt-hours"}
_WH_UNITS  = {"wh", "watt hours", "watt-hours"}


def _read_kwh(state) -> float | None:
    """Pull a cumulative kWh value out of a HA state object, normalising units."""
    raw = state.state
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    unit = (state.attributes.get("unit_of_measurement") or "kWh").strip().lower()
    if unit in _KWH_UNITS:
        return value
    if unit in _WH_UNITS:
        return value / 1000.0
    # Unknown unit — bail rather than misreport. Common in mis-configured
    # template sensors.
    return None


def _read_power_w(state, hass: HomeAssistant) -> float | None:
    """Best-effort current power reading associated with an energy sensor.

    Tries (in order):
      1. A `power` attribute on the energy sensor (some integrations expose it).
      2. `current_power_w` (common attribute name on older smart-plug integrations).
      3. A sibling sensor with the same object-id minus '_energy' plus '_power'.
    """
    val = state.attributes.get("power")
    if val is None:
        val = state.attributes.get("current_power_w")
    if val is None:
        eid = state.entity_id
        if eid.endswith("_energy"):
            guess = eid[: -len("_energy")] + "_power"
            sibling = hass.states.get(guess)
            if sibling is not None and sibling.state not in (None, "", "unknown", "unavailable"):
                try:
                    val = float(sibling.state)
                except (TypeError, ValueError):
                    val = None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def setup_energy_poller(
    hass: HomeAssistant,
    client: ShellyAddonClient,
    entity_ids: list[str],
) -> Callable[[], None]:
    """Attach a recurring poll for each configured energy entity. Returns an
    unsubscribe callable."""
    if not entity_ids:
        _LOGGER.info("HA energy import: no entities configured")
        return lambda: None

    _LOGGER.info(
        "HA energy import: polling %d entities every %ds",
        len(entity_ids), ENERGY_POLL_INTERVAL_S,
    )

    @callback
    def _on_tick(_now) -> None:
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state is None:
                continue
            kwh = _read_kwh(state)
            if kwh is None:
                continue
            power_w = _read_power_w(state, hass)
            friendly = state.attributes.get("friendly_name")
            hass.async_create_task(
                client.post_ha_energy_reading(
                    entity_id=entity_id,
                    energy_kwh=kwh,
                    power_w=power_w,
                    friendly_name=friendly,
                    ts=None,
                )
            )

    # Fire once immediately so the add-on sees baseline readings quickly,
    # then on the interval.
    _on_tick(None)
    return async_track_time_interval(
        hass, _on_tick, timedelta(seconds=ENERGY_POLL_INTERVAL_S)
    )
