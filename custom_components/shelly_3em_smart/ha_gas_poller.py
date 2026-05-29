"""Poll a HA sensor representing a cumulative natural-gas / propane / oil meter
reading and forward to the add-on. The add-on normalises units internally so
this just passes the raw value + HA's unit through."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import GAS_POLL_INTERVAL_S
from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)


def _read_cumulative(state) -> Optional[float]:
    raw = state.state
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def setup_gas_poller(
    hass: HomeAssistant,
    client: ShellyAddonClient,
    entity_id: Optional[str],
) -> Callable[[], None]:
    if not entity_id:
        _LOGGER.info("Gas: no entity configured")
        return lambda: None

    _LOGGER.info(
        "Gas: polling %s every %ds",
        entity_id, GAS_POLL_INTERVAL_S,
    )

    @callback
    def _on_tick(_now) -> None:
        state = hass.states.get(entity_id)
        if state is None:
            return
        cumulative = _read_cumulative(state)
        if cumulative is None:
            return
        unit = state.attributes.get("unit_of_measurement")
        hass.async_create_task(
            client.post_gas_reading(
                cumulative=cumulative,
                unit=unit,
                source=f"ha_entity:{entity_id}",
                ts=None,
            )
        )

    _on_tick(None)
    return async_track_time_interval(
        hass, _on_tick, timedelta(seconds=GAS_POLL_INTERVAL_S)
    )
