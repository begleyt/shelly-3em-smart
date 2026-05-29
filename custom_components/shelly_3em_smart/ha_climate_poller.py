"""Poll one or more HA climate.* entities and forward setpoints + mode +
current temp to the add-on. Used by the Climate tab to overlay setpoint
trends against energy use."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import CLIMATE_POLL_INTERVAL_S
from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)


def _to_fahrenheit(value: Optional[float], unit: Optional[str], hass: HomeAssistant) -> Optional[float]:
    if value is None:
        return None
    u = (unit or "").strip().lower().replace("°", "")
    if not u:
        try:
            u = (hass.config.units.temperature_unit or "").strip().lower().replace("°", "")
        except Exception:
            u = "c"
    if u in ("f", "fahrenheit"):
        return float(value)
    if u in ("c", "celsius"):
        return float(value) * 9.0 / 5.0 + 32.0
    return None


def setup_climate_poller(
    hass: HomeAssistant,
    client: ShellyAddonClient,
    entity_ids: list[str],
) -> Callable[[], None]:
    if not entity_ids:
        _LOGGER.info("Climate setpoints: no entities configured")
        return lambda: None

    _LOGGER.info(
        "Climate setpoints: polling %d entities every %ds",
        len(entity_ids), CLIMATE_POLL_INTERVAL_S,
    )

    @callback
    def _on_tick(_now) -> None:
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state is None:
                continue
            attrs = state.attributes
            unit = attrs.get("temperature_unit")
            target = _to_fahrenheit(attrs.get("temperature"), unit, hass)
            low = _to_fahrenheit(attrs.get("target_temp_low"), unit, hass)
            high = _to_fahrenheit(attrs.get("target_temp_high"), unit, hass)
            current = _to_fahrenheit(attrs.get("current_temperature"), unit, hass)
            mode = state.state            # heat / cool / heat_cool / off / auto
            action = attrs.get("hvac_action")  # heating / cooling / idle / fan / off
            # Skip if nothing useful to report
            if target is None and low is None and high is None and current is None:
                continue
            hass.async_create_task(
                client.post_setpoint_reading(
                    entity_id=entity_id,
                    target_temp_f=target,
                    target_low_f=low,
                    target_high_f=high,
                    current_temp_f=current,
                    hvac_mode=mode,
                    hvac_action=action,
                    ts=None,
                )
            )

    _on_tick(None)
    return async_track_time_interval(
        hass, _on_tick, timedelta(seconds=CLIMATE_POLL_INTERVAL_S)
    )
