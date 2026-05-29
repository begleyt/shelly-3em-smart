"""Poll a single HA weather.* / sensor.* outside-temperature entity and
forward the reading to the add-on. The add-on stores temperatures in
Fahrenheit (US HDD/CDD convention), so this poller converts from whatever
unit the HA entity reports."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import WEATHER_POLL_INTERVAL_S
from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)


def _to_fahrenheit(value: float, unit: Optional[str]) -> Optional[float]:
    """HA's weather and outdoor-temp entities can report in °C, °F, or just K.
    Normalise to °F. Returns None for unknown units rather than guessing."""
    if value is None:
        return None
    u = (unit or "").strip().lower().replace("°", "")
    if u in ("f", "fahrenheit"):
        return float(value)
    if u in ("c", "celsius", ""):    # blank unit on weather.* usually means HA's user unit
        return float(value) * 9.0 / 5.0 + 32.0
    if u in ("k", "kelvin"):
        return (float(value) - 273.15) * 9.0 / 5.0 + 32.0
    return None


def _read_temp_f(state, hass: HomeAssistant) -> Optional[float]:
    """Try the entity's state directly first (works for sensor.outdoor_temp),
    then fall back to a `temperature` attribute (the convention on weather.*
    domain entities)."""
    raw = state.state
    unit = state.attributes.get("unit_of_measurement")
    try:
        if raw not in (None, "", "unknown", "unavailable"):
            return _to_fahrenheit(float(raw), unit)
    except (TypeError, ValueError):
        pass

    attr = state.attributes.get("temperature")
    if attr is None:
        return None
    attr_unit = state.attributes.get("temperature_unit") or unit
    # Fall back to HA's user-configured unit if nothing else
    if attr_unit is None and hasattr(hass, "config"):
        try:
            attr_unit = hass.config.units.temperature_unit
        except Exception:
            attr_unit = None
    try:
        return _to_fahrenheit(float(attr), attr_unit)
    except (TypeError, ValueError):
        return None


def _read_today_forecast(state, hass: HomeAssistant) -> tuple[Optional[float], Optional[float]]:
    """Best-effort: pull today's high/low from the weather.* entity's
    `forecast` attribute. Modern HA weather entities deprecate this in favor
    of weather.get_forecasts, but most providers still expose it; we just
    skip silently if it's not there. Returns (high_f, low_f)."""
    forecast = state.attributes.get("forecast") or []
    if not forecast:
        return (None, None)
    first = forecast[0] if isinstance(forecast, list) else None
    if not isinstance(first, dict):
        return (None, None)

    unit = state.attributes.get("temperature_unit") or "C"
    try:
        unit = hass.config.units.temperature_unit
    except Exception:
        pass

    high = first.get("temperature")
    low = first.get("templow")
    high_f = _to_fahrenheit(float(high), unit) if isinstance(high, (int, float)) else None
    low_f = _to_fahrenheit(float(low), unit) if isinstance(low, (int, float)) else None
    return (high_f, low_f)


def _read_multi_day_forecast(state, hass: HomeAssistant) -> list[dict]:
    """Pull the full N-day forecast from the weather entity's `forecast`
    attribute. Each entry: {date_str, forecast_high_f, forecast_low_f,
    condition}. Skips entries without a valid datetime or temperature."""
    forecast = state.attributes.get("forecast") or []
    if not isinstance(forecast, list):
        return []

    unit = state.attributes.get("temperature_unit") or "C"
    try:
        unit = hass.config.units.temperature_unit
    except Exception:
        pass

    out = []
    for entry in forecast[:14]:    # cap at 2 weeks to be safe
        if not isinstance(entry, dict):
            continue
        dt = entry.get("datetime") or entry.get("date")
        if not dt:
            continue
        # Normalise to YYYY-MM-DD; HA gives ISO 8601 typically
        if isinstance(dt, str):
            date_str = dt[:10]
        else:
            try:
                date_str = dt.date().isoformat()
            except Exception:
                continue
        high = entry.get("temperature")
        low = entry.get("templow")
        high_f = _to_fahrenheit(float(high), unit) if isinstance(high, (int, float)) else None
        low_f = _to_fahrenheit(float(low), unit) if isinstance(low, (int, float)) else None
        if high_f is None and low_f is None:
            continue
        out.append({
            "date_str": date_str,
            "forecast_high_f": high_f,
            "forecast_low_f": low_f,
            "condition": entry.get("condition"),
        })
    return out


def setup_weather_poller(
    hass: HomeAssistant,
    client: ShellyAddonClient,
    entity_id: Optional[str],
) -> Callable[[], None]:
    """Attach a recurring poll for the configured weather entity. Returns an
    unsubscribe callable that's a no-op if no entity was configured."""
    if not entity_id:
        _LOGGER.info("Weather: no entity configured")
        return lambda: None

    _LOGGER.info(
        "Weather: polling %s every %ds",
        entity_id, WEATHER_POLL_INTERVAL_S,
    )

    @callback
    def _on_tick(_now) -> None:
        state = hass.states.get(entity_id)
        if state is None:
            return
        temp_f = _read_temp_f(state, hass)
        if temp_f is None:
            return
        humidity = state.attributes.get("humidity")
        condition = state.state if state.entity_id.startswith("weather.") else None
        high_f, low_f = (None, None)
        multi_day = []
        if state.entity_id.startswith("weather."):
            try:
                high_f, low_f = _read_today_forecast(state, hass)
            except Exception:
                pass
            try:
                multi_day = _read_multi_day_forecast(state, hass)
            except Exception:
                pass
        hass.async_create_task(
            client.post_weather_reading(
                temp_f=temp_f,
                humidity=humidity if isinstance(humidity, (int, float)) else None,
                condition=condition,
                source=f"ha_entity:{entity_id}",
                ts=None,
                forecast_high_f=high_f,
                forecast_low_f=low_f,
                forecast_days=multi_day,
            )
        )

    _on_tick(None)
    return async_track_time_interval(
        hass, _on_tick, timedelta(seconds=WEATHER_POLL_INTERVAL_S)
    )
