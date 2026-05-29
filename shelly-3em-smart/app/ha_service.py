"""Call Home Assistant services from inside the add-on.

HA Supervisor exposes the core REST API at http://supervisor/core/api when
the add-on has homeassistant_api: true in config.yaml. The supervisor
auto-injects a SUPERVISOR_TOKEN env var that authenticates the request.

Currently used for one-way setpoint control: dashboard → add-on → HA →
thermostat. Read paths (current setpoint, hvac_action, etc.) still come
via the HACS integration's setpoint poller so this stays minimal.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

SUPERVISOR_BASE = "http://supervisor/core/api"
_TIMEOUT_S = 10.0


def _token() -> Optional[str]:
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")


def have_ha_api() -> bool:
    return _token() is not None


async def call_service(domain: str, service: str, data: dict) -> dict:
    """Call a HA service via the supervisor proxy. Raises on non-2xx."""
    token = _token()
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN missing. Make sure homeassistant_api: true in "
            "config.yaml and the add-on was restarted by Supervisor."
        )
    url = f"{SUPERVISOR_BASE}/services/{domain}/{service}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(url, json=data, headers=headers)
        resp.raise_for_status()
        # HA returns the list of changed states; surface it but don't depend on it
        try:
            return {"ok": True, "changed_states": resp.json()}
        except Exception:
            return {"ok": True}


# --- cooling/heating setpoint bounds (safety net) ---------------------------

COOL_MIN_F, COOL_MAX_F = 60.0, 85.0
HEAT_MIN_F, HEAT_MAX_F = 55.0, 80.0


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


async def set_climate_temperature(
    entity_id: str,
    *,
    hvac_mode: Optional[str] = None,
    target_temp_f: Optional[float] = None,
    target_low_f: Optional[float] = None,
    target_high_f: Optional[float] = None,
    ha_temp_unit: str = "F",
) -> dict:
    """Send a climate.set_temperature service call. Caller must validate
    bounds; we re-check here as a safety net. ha_temp_unit is what HA's
    user-configured display unit is — we convert from our internal F to
    that unit before sending."""
    if not entity_id.startswith("climate."):
        raise ValueError(f"entity_id must be a climate.* entity, got {entity_id!r}")

    def _check(value, lo, hi, label):
        if value is None:
            return
        if not (lo <= value <= hi):
            raise ValueError(f"{label} {value:.1f}°F out of safe range [{lo}, {hi}]")

    # If it's a single-setpoint write we don't know cool vs heat — apply the
    # wider cool range as a sanity ceiling.
    if target_temp_f is not None:
        _check(target_temp_f, HEAT_MIN_F, COOL_MAX_F, "target_temp")
    if target_low_f is not None:
        _check(target_low_f, HEAT_MIN_F, HEAT_MAX_F, "target_low (heat setpoint)")
    if target_high_f is not None:
        _check(target_high_f, COOL_MIN_F, COOL_MAX_F, "target_high (cool setpoint)")

    def _to_ha(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(f_to_c(value), 1) if ha_temp_unit.upper().startswith("C") else round(value, 1)

    payload: dict = {"entity_id": entity_id}
    if hvac_mode:
        payload["hvac_mode"] = hvac_mode
    if target_temp_f is not None:
        payload["temperature"] = _to_ha(target_temp_f)
    if target_low_f is not None:
        payload["target_temp_low"] = _to_ha(target_low_f)
    if target_high_f is not None:
        payload["target_temp_high"] = _to_ha(target_high_f)

    if "temperature" not in payload and "target_temp_low" not in payload and "target_temp_high" not in payload:
        raise ValueError("must supply at least one of target_temp_f / target_low_f / target_high_f")

    log.info("HA service call climate.set_temperature: %s", payload)
    return await call_service("climate", "set_temperature", payload)
