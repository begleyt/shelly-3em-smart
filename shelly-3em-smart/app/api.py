import logging
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)

from . import APP_VERSION
from .clusterer import run_clustering
from .config import settings
from .db import cursor
from .ha_correlator import record_ha_event
from .ha_energy import list_energy_sources, record_energy_reading
from .inference import absorb_unlabeled_clusters
from .insights import all_device_stats, anomaly_check, device_stats, history_summary, insights, panel_energy_today, phantom_load
from .mqtt_publisher import publisher
from .state import state
from .weather import (
    backfill_recent_rollups,
    compute_rollup,
    get_today_forecast,
    latest_weather,
    predict_upcoming_energy,
    record_forecast_daily,
    record_gas_reading,
    record_setpoint_sample,
    record_weather_reading,
    setpoint_savings,
    weather_anomaly,
)
from .ha_service import (
    COOL_MAX_F,
    COOL_MIN_F,
    HEAT_MAX_F,
    HEAT_MIN_F,
    have_ha_api,
    set_climate_temperature,
)

router = APIRouter()


# ---------- info (used by the HACS integration to self-configure) ----------

@router.get("/api/info")
def info():
    return {
        "version": APP_VERSION,
        "shelly_host": settings.shelly_host,
        "channel_a_label": settings.channel_a_label,
        "channel_b_label": settings.channel_b_label,
        "channel_c_label": settings.channel_c_label,
        "mqtt_enabled": settings.mqtt_enabled,
        "supports_ha_events": True,
        "supports_insights": True,
        "supports_ha_energy": True,
        "rate_cents_per_kwh": settings.electricity_rate_cents_per_kwh,
        "currency_symbol": settings.currency_symbol,
        "supports_weather": True,
        "weather_entity_id": settings.weather_entity_id,
        "hdd_cdd_base_temp_f": settings.hdd_cdd_base_temp_f,
        "temp_unit": settings.temp_unit,
        "gas_rate_dollars_per_therm": settings.gas_rate_dollars_per_therm,
        "heating_fuel_kind": settings.heating_fuel_kind,
    }


# ---------- insights (v0.3.0) ----------

@router.get("/api/insights")
def get_insights():
    """One-shot summary for the Insights tab: today's energy + cost,
    phantom load, top consumers, anomalies."""
    try:
        return insights()
    except sqlite3.OperationalError as e:
        # DB locked / migration in progress — return stubs so the UI doesn't 500.
        # Other errors propagate so they show up in logs instead of silently zeroing.
        log.warning("get_insights: db unavailable: %s", e)
        return {
            "now": time.time(),
            "panel_today": {"wh": 0.0, "cost": 0.0},
            "phantom_load": {"watts": 0.0, "daily_wh": 0.0, "daily_cost": 0.0, "sample_count": 0},
            "top_devices_today": [],
            "all_devices_today": [],
            "anomalies": [],
            "rate_cents_per_kwh": settings.electricity_rate_cents_per_kwh,
            "currency_symbol": settings.currency_symbol,
        }


@router.get("/api/history_summary")
def get_history_summary():
    """Panel kWh / cost rolled up for today, yesterday, last 7 days, last
    30 days. Bounded by samples-table retention (30 days)."""
    try:
        return history_summary()
    except sqlite3.OperationalError as e:
        log.warning("get_history_summary: db unavailable: %s", e)
        empty = {"wh": 0.0, "cost": 0.0, "since": 0, "until": 0}
        return {
            "today": empty, "yesterday": empty, "last_7d": empty, "last_30d": empty,
            "rate_cents_per_kwh": settings.electricity_rate_cents_per_kwh,
            "currency_symbol": settings.currency_symbol,
        }


# ---------- weather / climate (v0.6.0) ----------

class ForecastDayIn(BaseModel):
    date_str: str            # YYYY-MM-DD local
    forecast_high_f: Optional[float] = None
    forecast_low_f: Optional[float] = None
    condition: Optional[str] = None


class WeatherReadingIn(BaseModel):
    temp_f: float
    humidity: Optional[float] = None
    condition: Optional[str] = None
    source: Optional[str] = None
    ts: Optional[float] = None
    forecast_high_f: Optional[float] = None
    forecast_low_f: Optional[float] = None
    forecast_days: Optional[list[ForecastDayIn]] = None     # next-week forecast


@router.post("/api/weather_reading")
def post_weather_reading(body: WeatherReadingIn):
    """Pushed by the HACS integration every ~60s from the configured
    weather entity. Temperature must be in Fahrenheit. forecast_high_f /
    forecast_low_f are best-effort — sent when the weather entity exposes
    a daily forecast, used by the dashboard's today H/L card so it has a
    meaningful range before empirical min/max has built up. forecast_days
    is the upcoming N-day daily forecast used by the energy prediction
    card on the Insights tab."""
    result = record_weather_reading(
        temp_f=body.temp_f,
        humidity=body.humidity,
        condition=body.condition,
        source=body.source,
        ts=body.ts,
        forecast_high_f=body.forecast_high_f,
        forecast_low_f=body.forecast_low_f,
    )
    if body.forecast_days:
        try:
            written = record_forecast_daily([d.model_dump() for d in body.forecast_days])
            result["forecast_days_written"] = written
        except Exception:
            log.exception("record_forecast_daily failed")
    return result


@router.get("/api/forecast/energy")
def get_forecast_energy(days_ahead: int = 7):
    """Predicted panel kWh + cost for tomorrow and the next N-1 days,
    derived from the regression fit on history and the HA weather entity's
    daily forecast. Used by the Energy forecast card on Insights."""
    days_ahead = max(1, min(int(days_ahead), 14))
    try:
        return predict_upcoming_energy(days_ahead=days_ahead)
    except sqlite3.OperationalError as e:
        log.warning("predict_upcoming_energy: db unavailable: %s", e)
        return {"days": [], "total_kwh": 0.0, "total_cost": 0.0, "has_forecast": False}


class GasReadingIn(BaseModel):
    cumulative: float            # cumulative reading from the meter
    unit: Optional[str] = None   # 'therm', 'ccf', 'ft3', 'm3', 'kWh' etc.
    source: Optional[str] = None
    ts: Optional[float] = None


@router.post("/api/gas_reading")
def post_gas_reading(body: GasReadingIn):
    """Pushed by the HACS integration when a gas meter sensor is configured.
    The add-on normalises to therms on ingest."""
    return record_gas_reading(
        cumulative=body.cumulative,
        unit=body.unit,
        source=body.source,
        ts=body.ts,
    )


class SetpointReadingIn(BaseModel):
    entity_id: str
    target_temp_f: Optional[float] = None
    target_low_f: Optional[float] = None
    target_high_f: Optional[float] = None
    current_temp_f: Optional[float] = None
    hvac_mode: Optional[str] = None
    hvac_action: Optional[str] = None
    ts: Optional[float] = None


@router.post("/api/setpoint_reading")
def post_setpoint_reading(body: SetpointReadingIn):
    """Climate entity snapshot from the HACS poller. Temperatures pre-converted
    to Fahrenheit on the integration side."""
    return record_setpoint_sample(
        entity_id=body.entity_id,
        target_temp_f=body.target_temp_f,
        target_low_f=body.target_low_f,
        target_high_f=body.target_high_f,
        current_temp_f=body.current_temp_f,
        hvac_mode=body.hvac_mode,
        hvac_action=body.hvac_action,
        ts=body.ts,
    )


@router.get("/api/weather/now")
def get_weather_now():
    """Latest outside-temperature reading + today's HDD/CDD progress.
    Surfaces forecast H/L when available so the dashboard can show
    meaningful values before empirical min/max has built up across the day."""
    latest = latest_weather() or {}
    today_row = compute_rollup(__import__("datetime").date.today(), force=True) or {}
    fc = get_today_forecast() or {}
    return {
        "temp_f": latest.get("temp_f"),
        "humidity": latest.get("humidity"),
        "condition": latest.get("condition"),
        "ts": latest.get("ts"),
        "today_avg_f": today_row.get("avg_temp_f"),
        "today_min_f": today_row.get("min_temp_f"),
        "today_max_f": today_row.get("max_temp_f"),
        "today_forecast_high_f": fc.get("forecast_high_f"),
        "today_forecast_low_f":  fc.get("forecast_low_f"),
        "today_hdd": today_row.get("hdd"),
        "today_cdd": today_row.get("cdd"),
        "today_cooling_kwh": (today_row.get("cooling_wh") or 0) / 1000.0 if today_row.get("cooling_wh") is not None else None,
        "today_heating_kwh": (today_row.get("heating_wh") or 0) / 1000.0 if today_row.get("heating_wh") is not None else None,
        "today_heating_therms": today_row.get("heating_therms"),
        "today_avg_cool_setpoint_f": today_row.get("avg_cool_setpoint_f"),
        "today_avg_heat_setpoint_f": today_row.get("avg_heat_setpoint_f"),
        "base_temp_f": settings.hdd_cdd_base_temp_f,
        "temp_unit": settings.temp_unit,
    }


@router.get("/api/daily_rollups")
def get_daily_rollups(days: int = 30):
    """Daily kWh + temperature + HDD/CDD for the last N days. Used by the
    Climate tab's scatter and bar charts. Bounded by sqlite contents — days
    with no data are simply absent."""
    days = max(1, min(days, 400))
    cutoff = time.time() - days * 86400
    with cursor() as cur:
        cur.execute(
            """SELECT date_str, day_start_ts, panel_wh, hvac_wh,
                      cooling_wh, heating_wh, heating_therms,
                      avg_temp_f, min_temp_f, max_temp_f,
                      forecast_high_f, forecast_low_f,
                      hdd, cdd, base_temp_f,
                      avg_cool_setpoint_f, avg_heat_setpoint_f,
                      sample_count
               FROM daily_rollups WHERE day_start_ts >= ? ORDER BY date_str""",
            (cutoff,),
        )
        cols = ["date_str", "day_start_ts", "panel_wh", "hvac_wh",
                "cooling_wh", "heating_wh", "heating_therms",
                "avg_temp_f", "min_temp_f", "max_temp_f",
                "forecast_high_f", "forecast_low_f",
                "hdd", "cdd", "base_temp_f",
                "avg_cool_setpoint_f", "avg_heat_setpoint_f",
                "sample_count"]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {
        "days": rows,
        "base_temp_f": settings.hdd_cdd_base_temp_f,
        "temp_unit": settings.temp_unit,
        "rate_cents_per_kwh": settings.electricity_rate_cents_per_kwh,
        "currency_symbol": settings.currency_symbol,
        "gas_rate_dollars_per_therm": settings.gas_rate_dollars_per_therm,
    }


@router.get("/api/weather/anomaly")
def get_weather_anomaly():
    """Regression-based anomaly: how today's actual usage compares to what
    a 30-day HDD/CDD model would predict for today's outside temperature."""
    try:
        return weather_anomaly()
    except sqlite3.OperationalError as e:
        log.warning("get_weather_anomaly: db unavailable: %s", e)
        return {"verdict": "unavailable", "history_days": 0}


@router.post("/api/weather/rebuild_rollups")
def rebuild_rollups(days: int = 31):
    """Force-rebuild the last N daily rollups. Call this after tagging a
    device as HVAC or changing the HDD/CDD base temperature so the existing
    rows reflect the new config."""
    rebuilt = backfill_recent_rollups(days=max(1, min(days, 400)))
    return {"rebuilt": rebuilt}


@router.get("/api/setpoint/savings")
def get_setpoint_savings(deltas: Optional[str] = None):
    """Projected monthly kWh + cost change for setpoint deltas.
    `deltas` is a comma-separated list of floats (in °F); default `-2,-1,1,2`.
    Returns separate blocks for cooling and heating. If we have ≥14 days of
    role-specific energy data with ≥1.5°F of setpoint variance, uses a
    setpoint-adjusted CDD/HDD regression; otherwise falls back to the
    industry rule of thumb (~5%/°F) with a `needs` field explaining the
    gap to a fitted model."""
    parsed: Optional[list[float]] = None
    if deltas:
        try:
            parsed = [float(x) for x in deltas.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="deltas must be comma-separated floats")
    try:
        return setpoint_savings(parsed)
    except sqlite3.OperationalError as e:
        log.warning("setpoint_savings: db unavailable: %s", e)
        return {"cooling": {"has_model": False}, "heating": {"has_model": False}}


class SetpointSetIn(BaseModel):
    entity_id: str
    hvac_mode: Optional[str] = None
    target_temp_f: Optional[float] = None
    target_low_f: Optional[float] = None    # heat setpoint (heat_cool mode)
    target_high_f: Optional[float] = None   # cool setpoint (heat_cool mode)
    ha_temp_unit: str = "F"                 # what unit HA wants


@router.post("/api/setpoint/set")
async def post_setpoint_set(body: SetpointSetIn):
    """Forward a setpoint change to HA via the supervisor proxy. Requires
    homeassistant_api: true in config.yaml (set in 0.8.0) — the supervisor
    will prompt the user to grant the permission on first upgrade. Returns
    `{"ok": false, "needs_api": true}` if the env var isn't there yet."""
    if not have_ha_api():
        return {
            "ok": False,
            "needs_api": True,
            "message": "Supervisor token missing. Grant the 'Home Assistant API' permission on the add-on and restart.",
        }
    try:
        return await set_climate_temperature(
            entity_id=body.entity_id,
            hvac_mode=body.hvac_mode,
            target_temp_f=body.target_temp_f,
            target_low_f=body.target_low_f,
            target_high_f=body.target_high_f,
            ha_temp_unit=body.ha_temp_unit,
        )
    except ValueError as e:
        # Bounds / payload validation failures should land at the UI as 400
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("set_climate_temperature failed")
        raise HTTPException(status_code=502, detail=f"HA service call failed: {e}")


@router.get("/api/setpoint/bounds")
def get_setpoint_bounds():
    """Safe bounds the dashboard should enforce on its sliders."""
    return {
        "cool_min_f": COOL_MIN_F, "cool_max_f": COOL_MAX_F,
        "heat_min_f": HEAT_MIN_F, "heat_max_f": HEAT_MAX_F,
        "ha_api_available": have_ha_api(),
    }


@router.get("/api/setpoint/entities")
def get_setpoint_entities():
    """All climate entities we've seen samples from, with their most recent
    snapshot. Used by the dashboard to populate the control dropdown."""
    with cursor() as cur:
        cur.execute(
            """SELECT entity_id, MAX(ts) FROM setpoint_samples GROUP BY entity_id"""
        )
        latest_per_entity = {row[0]: row[1] for row in cur.fetchall()}
        results = []
        for entity_id, max_ts in latest_per_entity.items():
            cur.execute(
                """SELECT target_temp_f, target_low_f, target_high_f,
                          current_temp_f, hvac_mode, hvac_action
                   FROM setpoint_samples WHERE entity_id = ? AND ts = ?""",
                (entity_id, max_ts),
            )
            row = cur.fetchone()
            if row is None:
                continue
            results.append({
                "entity_id": entity_id,
                "ts": max_ts,
                "target_temp_f": row[0],
                "target_low_f": row[1],
                "target_high_f": row[2],
                "current_temp_f": row[3],
                "hvac_mode": row[4],
                "hvac_action": row[5],
            })
    return {"entities": sorted(results, key=lambda r: r["entity_id"])}


@router.get("/api/devices/{device_id}/stats")
def get_device_stats(device_id: int):
    s = device_stats(device_id)
    if s:
        msg = anomaly_check(s)
        if msg:
            s["anomaly"] = msg
    return s


# ---------- HA state-change correlation ----------

class HaEventIn(BaseModel):
    entity_id: str
    old_state: Optional[str] = None
    new_state: Optional[str] = None
    friendly_name: Optional[str] = None
    ts: Optional[float] = None


@router.post("/api/ha_event")
def post_ha_event(body: HaEventIn):
    """Record a state change from the HACS integration. Used to correlate
    HA's ground-truth on/off knowledge with our detected step events."""
    return record_ha_event(
        entity_id=body.entity_id,
        old_state=body.old_state,
        new_state=body.new_state,
        friendly_name=body.friendly_name,
        ts=body.ts,
    )


@router.get("/api/device_state_log")
def device_state_log(minutes: int = 60, limit: int = 500):
    """Recent on/off transitions for labelled devices — used to overlay
    shaded on-periods and transition markers on the dashboard charts."""
    cutoff = time.time() - minutes * 60
    try:
        with cursor() as cur:
            cur.row_factory = _dict_row
            cur.execute(
                """SELECT dsl.device_id, d.name AS device_name, dsl.ts, dsl.state
                   FROM device_state_log dsl
                   JOIN devices d ON d.id = dsl.device_id
                   WHERE dsl.ts >= ?
                   ORDER BY dsl.ts ASC
                   LIMIT ?""",
                (cutoff, limit),
            )
            return cur.fetchall()
    except Exception:
        return []


@router.get("/api/ha_event_log")
def ha_event_log(minutes: int = 60, limit: int = 200):
    """Recent classified HA state changes — used by the dashboard charts
    to overlay vertical "X turned on/off" markers on the power lines."""
    cutoff = time.time() - minutes * 60
    try:
        with cursor() as cur:
            cur.row_factory = _dict_row
            cur.execute(
                """SELECT he.ts, he.entity_id, he.direction, he.new_state, he.old_state,
                          hen.friendly_name
                   FROM ha_events he
                   LEFT JOIN ha_entities hen ON hen.entity_id = he.entity_id
                   WHERE he.ts >= ?
                     AND he.direction IS NOT NULL
                   ORDER BY he.ts ASC
                   LIMIT ?""",
                (cutoff, limit),
            )
            return cur.fetchall()
    except Exception:
        # Defensive: if for any reason the tables aren't populated yet, don't
        # 500 — the dashboard treats an empty list as "no annotations".
        return []


# ---------- HA energy entity import (v0.5.0) ----------

class HaEnergyReading(BaseModel):
    entity_id: str
    energy_kwh: float
    power_w: Optional[float] = None
    friendly_name: Optional[str] = None
    ts: Optional[float] = None


@router.post("/api/ha_energy_reading")
def post_ha_energy_reading(body: HaEnergyReading):
    """The HACS integration calls this every ~30s with the latest reading from
    each user-selected energy sensor. We track cumulative kWh + a midnight
    baseline so today's energy can be computed as a delta."""
    try:
        return record_energy_reading(
            entity_id=body.entity_id,
            energy_kwh=body.energy_kwh,
            power_w=body.power_w,
            friendly_name=body.friendly_name,
            ts=body.ts,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/ha_energy_sources")
def get_ha_energy_sources():
    """Inspector endpoint: every tracked energy sensor with its latest reading,
    today's delta, and linked device id."""
    try:
        return list_energy_sources()
    except Exception:
        return []


@router.get("/api/ha_entities")
def list_ha_entities():
    """List the entities we've seen state changes from and how close they
    are to being auto-promoted to devices."""
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT entity_id, friendly_name, first_seen_ts, last_seen_ts, "
            "match_count, sum_power_w, sum_a_power_w, sum_b_power_w, sum_c_power_w, "
            "promoted_device_id "
            "FROM ha_entities ORDER BY match_count DESC, last_seen_ts DESC"
        )
        rows = cur.fetchall()

    out: list[dict] = []
    for r in rows:
        n = r["match_count"] or 0
        if n > 0:
            r["mean_power_w"] = (r["sum_power_w"] or 0.0) / n
            r["mean_a_power_w"] = (r["sum_a_power_w"] or 0.0) / n
            r["mean_b_power_w"] = (r["sum_b_power_w"] or 0.0) / n
            r["mean_c_power_w"] = (r["sum_c_power_w"] or 0.0) / n
        else:
            r["mean_power_w"] = 0.0
            r["mean_a_power_w"] = 0.0
            r["mean_b_power_w"] = 0.0
            r["mean_c_power_w"] = 0.0
        out.append(r)
    return out


def _dict_row(cur, row):
    return {col[0]: row[i] for i, col in enumerate(cur.description)}


# ---------- live ----------

@router.get("/api/live")
def live():
    # Prefer in-memory state (sub-second freshness) over the downsampled DB.
    if state.last_sample:
        return state.last_sample
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    return row or {}


@router.get("/api/history")
def history(minutes: int = 60):
    cutoff = time.time() - minutes * 60
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT ts, total_power, a_power, b_power, c_power "
            "FROM samples WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        )
        return cur.fetchall()


# ---------- clusters & devices ----------

@router.get("/api/clusters")
def list_clusters(unlabeled_only: bool = False):
    with cursor() as cur:
        cur.row_factory = _dict_row
        q = "SELECT * FROM clusters"
        if unlabeled_only:
            q += " WHERE device_id IS NULL"
        q += " ORDER BY sample_count DESC, ABS(mean_power) DESC"
        cur.execute(q)
        return cur.fetchall()


@router.get("/api/clusters/{cluster_id}/events")
def cluster_events(cluster_id: int, limit: int = 20):
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT id, ts, direction, delta_power, delta_a_power, delta_b_power, delta_c_power "
            "FROM events WHERE cluster_id = ? ORDER BY ts DESC LIMIT ?",
            (cluster_id, limit),
        )
        return cur.fetchall()


def _find_paired_cluster(cur, labeled_cluster: dict) -> Optional[dict]:
    """Find the opposite-sign cluster whose signature best matches the one
    the user labelled. Used to auto-link the matching off-cluster when an
    on-cluster is labelled (and vice versa) so both event directions feed
    the same device.

    Returns the candidate cluster row, or None if no good match is found.
    """
    opp_sign = -1 if labeled_cluster["mean_power"] > 0 else 1
    cur.execute(
        """SELECT id, mean_power, mean_a_power, mean_b_power, mean_c_power
           FROM clusters
           WHERE device_id IS NULL AND mean_power * ? > 0""",
        (opp_sign,),
    )
    candidates = cur.fetchall()
    if not candidates:
        return None

    target_mp = abs(labeled_cluster["mean_power"])
    target_a = abs(labeled_cluster["mean_a_power"])
    target_b = abs(labeled_cluster["mean_b_power"])
    target_c = abs(labeled_cluster["mean_c_power"])

    best = None
    best_score = float("inf")
    for c in candidates:
        score = (
            (target_mp - abs(c["mean_power"])) ** 2
            + (target_a - abs(c["mean_a_power"])) ** 2 * 0.5
            + (target_b - abs(c["mean_b_power"])) ** 2 * 0.5
            + (target_c - abs(c["mean_c_power"])) ** 2 * 0.5
        )
        if score < best_score:
            best_score = score
            best = c

    if best is None:
        return None
    # Only auto-link if magnitudes match within ~25% (with a floor for small loads).
    tol = max(target_mp * 0.25, 50.0) ** 2 * 2.0
    if best_score > tol:
        return None
    return best


def _recompute_device_state(cur, device_id: int, mean_power_w: float) -> None:
    """Replay every event currently linked to this device to compute is_on,
    timestamps, and accumulated energy. Used after labelling so the device
    reflects the appliance's real state immediately, without waiting for a
    fresh on/off cycle."""
    cur.execute(
        "SELECT ts, direction FROM events WHERE device_id = ? ORDER BY ts",
        (device_id,),
    )
    rows = cur.fetchall()

    is_on = 0
    last_on_ts: Optional[float] = None
    last_off_ts: Optional[float] = None
    total_energy_wh = 0.0
    current_on_ts: Optional[float] = None

    for row in rows:
        ts = float(row["ts"])
        direction = row["direction"]
        if direction == "on":
            if not is_on:
                is_on = 1
                current_on_ts = ts
            last_on_ts = ts
        else:  # off
            if is_on and current_on_ts is not None:
                elapsed_s = max(0.0, ts - current_on_ts)
                total_energy_wh += mean_power_w * elapsed_s / 3600.0
                is_on = 0
                current_on_ts = None
            last_off_ts = ts

    cur.execute(
        """UPDATE devices
           SET is_on = ?, last_on_ts = ?, last_off_ts = ?, total_energy_wh = ?
           WHERE id = ?""",
        (is_on, last_on_ts, last_off_ts, total_energy_wh, device_id),
    )


def _augment_device_row(row: dict, now: float) -> dict:
    """Add computed current_power_w and current_energy_wh fields to a device row.

    `current_energy_wh` includes any energy accumulated during the in-progress
    on-period, so the value rises smoothly while a device is running rather
    than only stepping at each off event.
    """
    mean_w = float(row.get("mean_power_w") or 0.0)
    is_on = bool(row.get("is_on"))
    base_wh = float(row.get("total_energy_wh") or 0.0)
    last_on = row.get("last_on_ts")
    if is_on and last_on:
        elapsed_s = max(0.0, now - float(last_on))
        live_wh = base_wh + (mean_w * elapsed_s / 3600.0)
    else:
        live_wh = base_wh
    row["current_power_w"] = mean_w if is_on else 0.0
    row["current_energy_wh"] = live_wh
    return row


@router.get("/api/devices")
def list_devices():
    now = time.time()
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute("SELECT * FROM devices ORDER BY name")
        return [_augment_device_row(r, now) for r in cur.fetchall()]


# ---------- cluster pairs (UI helper) ----------

def _pair_score(a: dict, b: dict) -> float:
    return (
        (abs(a["mean_power"]) - abs(b["mean_power"])) ** 2
        + (abs(a["mean_a_power"]) - abs(b["mean_a_power"])) ** 2 * 0.5
        + (abs(a["mean_b_power"]) - abs(b["mean_b_power"])) ** 2 * 0.5
        + (abs(a["mean_c_power"]) - abs(b["mean_c_power"])) ** 2 * 0.5
    )


@router.get("/api/cluster_pairs")
def list_cluster_pairs(recent_n: int = 5):
    """Group unlabelled clusters into probable appliances (a start-cluster
    paired with its matching stop-cluster) plus orphans that didn't find a
    confident pair.

    `recent_n` is the number of most-recent event timestamps to attach to
    each cluster — used by the UI to show "last seen at HH:MM" hints that
    help identify which appliance the cluster belongs to.
    """
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            """SELECT id, mean_power, std_power, mean_a_power, mean_b_power,
                      mean_c_power, mean_pf, sample_count
               FROM clusters
               WHERE device_id IS NULL
               ORDER BY ABS(mean_power) DESC"""
        )
        clusters = cur.fetchall()

        # Pull the most recent event timestamps for each unlabelled cluster.
        if clusters:
            ids = [c["id"] for c in clusters]
            placeholders = ",".join(["?"] * len(ids))
            cur.execute(
                f"SELECT cluster_id, ts FROM events "
                f"WHERE cluster_id IN ({placeholders}) "
                f"ORDER BY ts DESC",
                ids,
            )
            recent: dict[int, list[float]] = {}
            for row in cur.fetchall():
                cid = row["cluster_id"]
                if cid is None:
                    continue
                lst = recent.setdefault(cid, [])
                if len(lst) < max(recent_n, 1):
                    lst.append(row["ts"])
            for c in clusters:
                c["recent_event_ts"] = recent.get(c["id"], [])

    on_clusters = [c for c in clusters if c["mean_power"] > 0]
    off_clusters = [c for c in clusters if c["mean_power"] < 0]

    pairs: list[dict] = []
    used_off: set[int] = set()
    used_on: set[int] = set()

    for on_c in on_clusters:
        target_mp = abs(on_c["mean_power"])
        tol = max(target_mp * 0.25, 50.0) ** 2 * 2.0
        best = None
        best_score = float("inf")
        for off_c in off_clusters:
            if off_c["id"] in used_off:
                continue
            score = _pair_score(on_c, off_c)
            if score < best_score:
                best_score = score
                best = off_c
        if best is not None and best_score <= tol:
            used_off.add(best["id"])
            used_on.add(on_c["id"])
            pairs.append({
                "on_cluster": on_c,
                "off_cluster": best,
                "mean_power_w": (abs(on_c["mean_power"]) + abs(best["mean_power"])) / 2.0,
                "total_events": on_c["sample_count"] + best["sample_count"],
            })

    pairs.sort(key=lambda p: p["total_events"], reverse=True)

    orphans = (
        [{"cluster": c, "direction": "on"} for c in on_clusters if c["id"] not in used_on]
        + [{"cluster": c, "direction": "off"} for c in off_clusters if c["id"] not in used_off]
    )
    orphans.sort(key=lambda o: abs(o["cluster"]["mean_power"]), reverse=True)

    return {"pairs": pairs, "orphans": orphans}


# ---------- devices ----------

class DeviceCreate(BaseModel):
    name: str
    notes: Optional[str] = None
    cluster_id: int


@router.post("/api/devices")
def create_device(body: DeviceCreate):
    paired_cluster_id: Optional[int] = None
    with cursor() as cur:
        cur.row_factory = _dict_row

        # 1. Read the labelled cluster's signature so we can both snapshot the
        # device's mean power and look for a matching opposite-sign cluster.
        cur.execute(
            "SELECT id, mean_power, mean_a_power, mean_b_power, mean_c_power "
            "FROM clusters WHERE id = ?",
            (body.cluster_id,),
        )
        labeled = cur.fetchone()
        mean_power_w = abs(float(labeled["mean_power"])) if labeled else 0.0

        # 2. Create the device with the snapshot mean power. State fields will
        # be replaced by _recompute_device_state below; we initialise them to
        # safe defaults here.
        cur.execute(
            "INSERT INTO devices (name, notes, created_ts, is_on, mean_power_w, total_energy_wh) "
            "VALUES (?,?,?,0,?,0)",
            (body.name, body.notes, time.time(), mean_power_w),
        )
        device_id = cur.lastrowid

        # 3. Link the labelled cluster and its events to this device.
        cur.execute(
            "UPDATE clusters SET device_id = ? WHERE id = ?",
            (device_id, body.cluster_id),
        )
        cur.execute(
            "UPDATE events SET device_id = ? WHERE cluster_id = ?",
            (device_id, body.cluster_id),
        )

        # 4. Auto-link the paired opposite-sign cluster (the off-events that
        # match this appliance) so future state transitions are detected.
        if labeled:
            paired = _find_paired_cluster(cur, labeled)
            if paired:
                paired_cluster_id = int(paired["id"])
                cur.execute(
                    "UPDATE clusters SET device_id = ? WHERE id = ?",
                    (device_id, paired_cluster_id),
                )
                cur.execute(
                    "UPDATE events SET device_id = ? WHERE cluster_id = ?",
                    (device_id, paired_cluster_id),
                )

        # 5. Replay every linked event to compute current state and backfilled energy.
        _recompute_device_state(cur, device_id, mean_power_w)

    publisher.publish_device_discovery(device_id, body.name)
    return {
        "id": device_id,
        "name": body.name,
        "cluster_id": body.cluster_id,
        "paired_cluster_id": paired_cluster_id,
        "mean_power_w": mean_power_w,
    }


class DeviceManualCreate(BaseModel):
    name: str
    notes: Optional[str] = None
    power_w: float
    channel_a_power_w: Optional[float] = None
    channel_b_power_w: Optional[float] = None
    channel_c_power_w: Optional[float] = None
    power_factor: Optional[float] = 1.0
    currently_on: bool = False
    is_continuous: bool = False


def _retroactive_match_events(
    cur,
    device_id: int,
    mean_power_w: float,
    a: float,
    b: float,
    c: float,
    std_w: float,
    lookback_s: int = 86400,
) -> int:
    """Tag previously-unmatched events from the last `lookback_s` seconds whose
    signature fits this device. Lets manually-added appliances pick up history
    that the clusterer may have already detected but couldn't attribute."""
    cutoff = time.time() - lookback_s
    cur.execute(
        """SELECT id, delta_power, delta_a_power, delta_b_power, delta_c_power
           FROM events
           WHERE device_id IS NULL
             AND ts >= ?
             AND ABS(delta_power) BETWEEN ? AND ?""",
        (cutoff, mean_power_w * 0.3, mean_power_w * 3.0),
    )
    candidates = cur.fetchall()
    if not candidates:
        return 0

    tol = max(std_w, 25.0) ** 2 * 4.0
    linked: list[int] = []
    for ev in candidates:
        score = (
            (abs(ev["delta_power"]) - mean_power_w) ** 2
            + (abs(ev["delta_a_power"]) - abs(a)) ** 2 * 0.5
            + (abs(ev["delta_b_power"]) - abs(b)) ** 2 * 0.5
            + (abs(ev["delta_c_power"]) - abs(c)) ** 2 * 0.5
        )
        if score <= tol:
            linked.append(int(ev["id"]))

    if linked:
        placeholders = ",".join(["?"] * len(linked))
        cur.execute(
            f"UPDATE events SET device_id = ? WHERE id IN ({placeholders})",
            (device_id, *linked),
        )
    return len(linked)


@router.post("/api/devices/manual")
def create_device_manual(body: DeviceManualCreate):
    """Create a device directly from a user-specified signature, without
    needing the clusterer to discover it first. Synthesises a start/stop
    cluster pair so the existing event matcher will attribute future events,
    then scans the last 24h of unmatched events for retroactive history.
    """
    if body.power_w <= 0:
        raise HTTPException(status_code=400, detail="power_w must be > 0")

    a = body.channel_a_power_w
    b = body.channel_b_power_w
    c = body.channel_c_power_w
    specified = sum(x or 0.0 for x in (a, b, c))
    unspecified_count = sum(1 for x in (a, b, c) if x is None)
    if unspecified_count == 3:
        a, b, c = 0.0, body.power_w, 0.0
    else:
        remainder = body.power_w - specified
        per_unspec = remainder / unspecified_count if unspecified_count else 0.0
        a = a if a is not None else per_unspec
        b = b if b is not None else per_unspec
        c = c if c is not None else per_unspec

    pf = float(body.power_factor or 1.0)
    std_w = max(body.power_w * 0.10, 20.0)
    now = time.time()

    with cursor() as cur:
        cur.row_factory = _dict_row

        cur.execute(
            "INSERT INTO devices (name, notes, created_ts, is_on, mean_power_w, "
            "total_energy_wh, is_continuous) "
            "VALUES (?,?,?,0,?,0,?)",
            (body.name, body.notes, now, body.power_w, 1 if body.is_continuous else 0),
        )
        device_id = cur.lastrowid

        cur.execute(
            """INSERT INTO clusters
            (created_ts, updated_ts, mean_power, std_power,
             mean_a_power, mean_b_power, mean_c_power, mean_pf, sample_count, device_id)
            VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (now, now, body.power_w, std_w, a, b, c, pf, device_id),
        )
        cur.execute(
            """INSERT INTO clusters
            (created_ts, updated_ts, mean_power, std_power,
             mean_a_power, mean_b_power, mean_c_power, mean_pf, sample_count, device_id)
            VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (now, now, -body.power_w, std_w, -a, -b, -c, pf, device_id),
        )

        matched_count = _retroactive_match_events(cur, device_id, body.power_w, a, b, c, std_w)
        _recompute_device_state(cur, device_id, body.power_w)

        # If user says it's on right now and the event replay didn't already
        # leave it on, force it on. They can see the appliance — trust them.
        if body.currently_on:
            cur.execute("SELECT is_on FROM devices WHERE id = ?", (device_id,))
            row = cur.fetchone()
            if row and not row.get("is_on"):
                cur.execute(
                    "UPDATE devices SET is_on = 1, last_on_ts = ? WHERE id = ?",
                    (now, device_id),
                )

    publisher.publish_device_discovery(device_id, body.name)
    # Mop up any pre-existing unlabeled clusters that now match this device.
    absorb_result = absorb_unlabeled_clusters()
    return {
        "id": device_id,
        "name": body.name,
        "mean_power_w": body.power_w,
        "matched_history_events": matched_count,
        "absorbed_clusters": absorb_result["absorbed"],
    }


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None
    is_continuous: Optional[bool] = None
    is_hvac: Optional[bool] = None        # legacy: maps to hvac_role='cooling'
    hvac_role: Optional[str] = None       # 'cooling' | 'heating' | '' (clear)
    force_state: Optional[str] = None   # 'on' | 'off' to manually toggle


@router.patch("/api/devices/{device_id}")
def update_device(device_id: int, body: DeviceUpdate):
    fields: list[str] = []
    values: list = []
    if body.name is not None:
        fields.append("name = ?")
        values.append(body.name)
    if body.notes is not None:
        fields.append("notes = ?")
        values.append(body.notes)
    if body.is_continuous is not None:
        fields.append("is_continuous = ?")
        values.append(1 if body.is_continuous else 0)
    # hvac_role supersedes is_hvac. Accept both for backwards-compat with the
    # 0.6.0 UI: a true is_hvac without an explicit role maps to cooling.
    new_role = body.hvac_role
    if new_role is None and body.is_hvac is not None:
        new_role = "cooling" if body.is_hvac else ""
    if new_role is not None:
        role = (new_role or "").strip().lower()
        if role not in ("cooling", "heating", ""):
            raise HTTPException(status_code=400, detail="hvac_role must be 'cooling', 'heating', or empty")
        with cursor() as cur:
            if role:
                # Only one device per role at a time so the per-role rollup
                # totals don't double-count.
                cur.execute(
                    "UPDATE devices SET hvac_role = NULL WHERE hvac_role = ? AND id != ?",
                    (role, device_id),
                )
        fields.append("hvac_role = ?")
        values.append(role or None)
        # Keep is_hvac in sync for any callers still reading the legacy column.
        fields.append("is_hvac = ?")
        values.append(1 if role == "cooling" else 0)

    now = time.time()
    if body.force_state == "on":
        fields.append("is_on = 1")
        fields.append("last_on_ts = ?")
        values.append(now)
    elif body.force_state == "off":
        # Tally any in-progress energy first
        with cursor() as cur:
            cur.execute(
                "SELECT is_on, last_on_ts, mean_power_w, total_energy_wh "
                "FROM devices WHERE id = ?",
                (device_id,),
            )
            row = cur.fetchone()
            if row and row[0] and row[1]:
                elapsed_s = max(0.0, now - float(row[1]))
                energy_wh = float(row[2] or 0) * elapsed_s / 3600.0
                cur.execute(
                    "UPDATE devices SET total_energy_wh = "
                    "COALESCE(total_energy_wh, 0) + ? WHERE id = ?",
                    (energy_wh, device_id),
                )
        fields.append("is_on = 0")
        fields.append("last_off_ts = ?")
        values.append(now)

    if not fields:
        return {"ok": True}
    values.append(device_id)
    with cursor() as cur:
        cur.execute(f"UPDATE devices SET {', '.join(fields)} WHERE id = ?", values)
    if body.force_state in ("on", "off"):
        publisher.publish_device_state(device_id, body.force_state)
    return {"ok": True}


@router.post("/api/absorb_clusters")
def trigger_absorb():
    """Force a sweep of unlabeled clusters into matching existing devices."""
    return absorb_unlabeled_clusters()


@router.delete("/api/devices/{device_id}")
def delete_device(device_id: int):
    with cursor() as cur:
        cur.execute("UPDATE clusters SET device_id = NULL WHERE device_id = ?", (device_id,))
        cur.execute("UPDATE events SET device_id = NULL WHERE device_id = ?", (device_id,))
        cur.execute("DELETE FROM device_state_log WHERE device_id = ?", (device_id,))
        cur.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    return {"ok": True}


class ClusterAssign(BaseModel):
    device_id: int


@router.post("/api/clusters/{cluster_id}/assign")
def assign_cluster(cluster_id: int, body: ClusterAssign):
    with cursor() as cur:
        cur.execute("UPDATE clusters SET device_id = ? WHERE id = ?", (body.device_id, cluster_id))
        cur.execute("UPDATE events SET device_id = ? WHERE cluster_id = ?", (body.device_id, cluster_id))
    return {"ok": True}


# ---------- ops ----------

@router.post("/api/recluster")
def recluster():
    return run_clustering()


@router.get("/api/stats")
def stats():
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute("SELECT COUNT(*) AS n FROM samples")
        samples = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM events")
        events = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM clusters WHERE device_id IS NULL")
        unlabeled = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM devices")
        devices = cur.fetchone()["n"]
        cur.execute("SELECT MIN(ts) AS first_ts FROM samples")
        first_ts = cur.fetchone()["first_ts"]
    return {
        "samples": samples,
        "events": events,
        "unlabeled_clusters": unlabeled,
        "devices": devices,
        "first_sample_ts": first_ts,
    }
