"""Weather correlation: ingest outside-temp samples, roll up daily kWh and
temperature stats, compute HDD/CDD, and fit a simple linear regression so we
can compare today's actual usage to a temperature-predicted baseline.

All temperatures stored internally in Fahrenheit (US HDD/CDD convention).
The dashboard converts to the user's preferred unit on display.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

from .config import settings
from .db import cursor
from .insights import panel_energy_window

log = logging.getLogger(__name__)


# --- unit helpers -----------------------------------------------------------

def c_to_f(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    return c * 9.0 / 5.0 + 32.0


def f_to_c(f: Optional[float]) -> Optional[float]:
    if f is None:
        return None
    return (f - 32.0) * 5.0 / 9.0


# --- ingestion --------------------------------------------------------------

# Gas unit normalisation. 1 therm = 100,000 BTU = 29.3001 kWh thermal.
# 1 ccf natural gas ≈ 1.037 therms (varies by gas heating value)
# 1 m³ natural gas ≈ 0.3531 therms (10.55 kWh thermal)
_GAS_TO_THERMS = {
    "therm":    1.0,
    "therms":   1.0,
    "th":       1.0,
    "ccf":      1.037,
    "ft3":      1.037e-2,   # 100 ft³ = 1 ccf
    "ft^3":     1.037e-2,
    "cubic feet": 1.037e-2,
    "m3":       0.3531,
    "m^3":      0.3531,
    "kwh":      0.0341296,
    "wh":       3.41296e-5,
}


def gas_to_therms(value: float, unit: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    u = (unit or "").strip().lower().replace("³", "3").replace("²", "2")
    return value * _GAS_TO_THERMS.get(u, _GAS_TO_THERMS.get(u.rstrip("s"), None)) \
        if u in _GAS_TO_THERMS or u.rstrip("s") in _GAS_TO_THERMS else None


def record_weather_reading(
    temp_f: float,
    humidity: Optional[float] = None,
    condition: Optional[str] = None,
    source: Optional[str] = None,
    ts: Optional[float] = None,
    forecast_high_f: Optional[float] = None,
    forecast_low_f: Optional[float] = None,
) -> dict:
    """Store a single weather reading from the HACS integration. Optionally
    refreshes today's forecast high/low (used by the dashboard's H/L card
    so we don't have to wait until evening to know the day's expected range)."""
    if ts is None:
        ts = time.time()
    today_str = date.today().isoformat()
    with cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO weather_samples (ts, temp_f, humidity, condition, source) "
            "VALUES (?,?,?,?,?)",
            (ts, temp_f, humidity, condition, source),
        )
        if forecast_high_f is not None or forecast_low_f is not None:
            cur.execute(
                """INSERT INTO weather_forecast_today (id, date_str, forecast_high_f, forecast_low_f, ts)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     date_str = excluded.date_str,
                     forecast_high_f = excluded.forecast_high_f,
                     forecast_low_f  = excluded.forecast_low_f,
                     ts = excluded.ts""",
                (today_str, forecast_high_f, forecast_low_f, ts),
            )
    return {"ts": ts, "temp_f": temp_f}


def record_gas_reading(
    cumulative: float,
    unit: Optional[str] = None,
    source: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict:
    """Store a cumulative gas-meter reading. Normalises to therms; preserves
    the raw value + unit so we can re-normalise if conversion factors change."""
    if ts is None:
        ts = time.time()
    therms = gas_to_therms(cumulative, unit)
    with cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO gas_samples (ts, cumulative_therms, raw_value, raw_unit, source) "
            "VALUES (?,?,?,?,?)",
            (ts, therms, cumulative, unit, source),
        )
    return {"ts": ts, "therms": therms, "raw_unit": unit}


def record_setpoint_sample(
    entity_id: str,
    target_temp_f: Optional[float] = None,
    target_low_f: Optional[float] = None,
    target_high_f: Optional[float] = None,
    current_temp_f: Optional[float] = None,
    hvac_mode: Optional[str] = None,
    hvac_action: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict:
    """Store a snapshot of a HA climate.* entity. The HACS poller pushes one
    per entity per minute; we collapse to whatever changed."""
    if ts is None:
        ts = time.time()
    with cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO setpoint_samples
               (ts, entity_id, target_temp_f, target_low_f, target_high_f,
                current_temp_f, hvac_mode, hvac_action)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ts, entity_id, target_temp_f, target_low_f, target_high_f,
             current_temp_f, hvac_mode, hvac_action),
        )
    return {"ts": ts, "entity_id": entity_id}


def record_forecast_daily(forecast_days: list[dict]) -> int:
    """Bulk-replace upcoming-week forecast rows. Each entry must include
    `date_str` (YYYY-MM-DD), `forecast_high_f`, `forecast_low_f`; optionally
    `condition`. Returns the number of rows written."""
    if not forecast_days:
        return 0
    now = time.time()
    written = 0
    with cursor() as cur:
        for d in forecast_days:
            ds = d.get("date_str")
            high = d.get("forecast_high_f")
            low = d.get("forecast_low_f")
            if not ds or (high is None and low is None):
                continue
            avg = ((high or low) + (low or high)) / 2.0 if (high is not None or low is not None) else None
            cur.execute(
                """INSERT INTO weather_forecast_daily
                   (date_str, forecast_high_f, forecast_low_f, forecast_avg_f, condition, refreshed_ts)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(date_str) DO UPDATE SET
                     forecast_high_f = excluded.forecast_high_f,
                     forecast_low_f  = excluded.forecast_low_f,
                     forecast_avg_f  = excluded.forecast_avg_f,
                     condition       = excluded.condition,
                     refreshed_ts    = excluded.refreshed_ts""",
                (ds, high, low, avg, d.get("condition"), now),
            )
            written += 1
    return written


def get_today_forecast() -> Optional[dict]:
    """Latest forecast H/L for today's local date (only useful while the
    stored date_str matches today's local date — otherwise it's stale)."""
    with cursor() as cur:
        cur.execute("SELECT date_str, forecast_high_f, forecast_low_f, ts "
                    "FROM weather_forecast_today WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return None
    if row[0] != date.today().isoformat():
        return None
    return {"forecast_high_f": row[1], "forecast_low_f": row[2], "ts": row[3]}


def latest_weather() -> Optional[dict]:
    """Most recent weather reading, or None if we have none yet."""
    with cursor() as cur:
        cur.execute(
            "SELECT ts, temp_f, humidity, condition FROM weather_samples "
            "ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"ts": row[0], "temp_f": row[1], "humidity": row[2], "condition": row[3]}


def prune_old_weather() -> int:
    """Match samples retention but be a bit more generous so we can still
    show year-over-year temperature curves after the power data is gone."""
    days = max(int(settings.weather_retention_days), int(settings.sample_retention_days))
    cutoff = time.time() - days * 86400
    with cursor() as cur:
        cur.execute("DELETE FROM weather_samples WHERE ts < ?", (cutoff,))
        return cur.rowcount


# --- daily rollups ----------------------------------------------------------

def _local_day_bounds(d: date) -> tuple[float, float]:
    """Return [midnight, next midnight) for a local-time calendar day."""
    midnight = datetime(d.year, d.month, d.day, 0, 0, 0).timestamp()
    return midnight, midnight + 86400.0


def _hvac_device_ids() -> dict:
    """Return {cooling: id|None, heating: id|None} based on hvac_role."""
    out = {"cooling": None, "heating": None}
    with cursor() as cur:
        cur.execute(
            "SELECT id, hvac_role FROM devices "
            "WHERE hvac_role IN ('cooling','heating')"
        )
        for row in cur.fetchall():
            if out.get(row[1]) is None:
                out[row[1]] = int(row[0])
    return out


def _gas_therms_for_window(start_ts: float, end_ts: float) -> Optional[float]:
    """Cumulative therms used during the window (latest cumulative reading at
    end_ts minus latest at start_ts). Returns None if we don't have anchor
    readings on both sides — partial windows aren't safe to attribute."""
    with cursor() as cur:
        cur.execute(
            "SELECT cumulative_therms FROM gas_samples "
            "WHERE ts <= ? AND cumulative_therms IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (end_ts,),
        )
        end_row = cur.fetchone()
        cur.execute(
            "SELECT cumulative_therms FROM gas_samples "
            "WHERE ts <= ? AND cumulative_therms IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (start_ts,),
        )
        start_row = cur.fetchone()
    if end_row is None or start_row is None:
        return None
    delta = float(end_row[0]) - float(start_row[0])
    return max(0.0, delta)


def _setpoint_avg_for_window(start_ts: float, end_ts: float) -> dict:
    """Average heat and cool setpoints during the window across all known
    climate entities. heat_cool / auto modes use target_low/high for the
    respective setpoint."""
    with cursor() as cur:
        cur.execute(
            """SELECT target_temp_f, target_low_f, target_high_f, hvac_mode
               FROM setpoint_samples WHERE ts >= ? AND ts < ?""",
            (start_ts, end_ts),
        )
        rows = cur.fetchall()
    cool_vals, heat_vals = [], []
    for target, low, high, mode in rows:
        m = (mode or "").lower()
        if low is not None and high is not None:
            heat_vals.append(low)
            cool_vals.append(high)
        elif m == "heat" and target is not None:
            heat_vals.append(target)
        elif m == "cool" and target is not None:
            cool_vals.append(target)
        elif target is not None:
            # Unknown mode — count as both (best-effort)
            heat_vals.append(target)
            cool_vals.append(target)
    return {
        "avg_cool_setpoint_f": sum(cool_vals) / len(cool_vals) if cool_vals else None,
        "avg_heat_setpoint_f": sum(heat_vals) / len(heat_vals) if heat_vals else None,
        "n": len(rows),
    }


def _temp_stats_for_window(start_ts: float, end_ts: float) -> dict:
    """avg/min/max temperature across a window, plus sample count.
    avg is just the arithmetic mean of recorded samples — fine for our cadence
    (HACS poller pushes every minute or so); not weighted by gap duration."""
    with cursor() as cur:
        cur.execute(
            "SELECT temp_f FROM weather_samples WHERE ts >= ? AND ts < ? AND temp_f IS NOT NULL",
            (start_ts, end_ts),
        )
        temps = [row[0] for row in cur.fetchall()]
    if not temps:
        return {"avg_f": None, "min_f": None, "max_f": None, "count": 0}
    return {
        "avg_f": sum(temps) / len(temps),
        "min_f": min(temps),
        "max_f": max(temps),
        "count": len(temps),
    }


def _device_energy_window(device_id: int, start_ts: float, end_ts: float) -> float:
    """Total Wh attributed to a single device over a window. Mirrors the
    inferred-device replay model in insights.device_stats (mean_power_w x
    on_seconds), but for an arbitrary window. Returns 0 for unknown devices."""
    with cursor() as cur:
        cur.execute(
            "SELECT mean_power_w, is_continuous, is_on, last_on_ts FROM devices WHERE id = ?",
            (device_id,),
        )
        row = cur.fetchone()
    if row is None:
        return 0.0
    mean_w = float(row[0] or 0.0)
    is_continuous = bool(row[1])
    if mean_w <= 0:
        return 0.0
    with cursor() as cur:
        cur.execute(
            "SELECT ts, state FROM device_state_log "
            "WHERE device_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (device_id, start_ts, end_ts),
        )
        logs = cur.fetchall()
    on_seconds = 0.0
    # Was the device on at the start of the window?
    with cursor() as cur:
        cur.execute(
            "SELECT state FROM device_state_log "
            "WHERE device_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (device_id, start_ts),
        )
        prev = cur.fetchone()
    last_on: Optional[float] = start_ts if (prev and prev[0] == "on") else None
    for ts, state in logs:
        if state == "on" and last_on is None:
            last_on = ts
        elif state == "off" and last_on is not None:
            on_seconds += max(0.0, ts - last_on)
            last_on = None
    if last_on is not None:
        on_seconds += max(0.0, end_ts - last_on)
    if is_continuous and on_seconds < (end_ts - start_ts):
        # Continuous device with no transitions = on the whole window
        on_seconds = end_ts - start_ts
    return mean_w * on_seconds / 3600.0


def compute_rollup(d: date, force: bool = False) -> Optional[dict]:
    """Build (or rebuild) the rollup row for one local-calendar day. Now
    splits HVAC energy into cooling/heating buckets based on devices.hvac_role,
    includes gas therms, average setpoints, and (for today) the forecast
    high/low so the dashboard can show meaningful H/L early in the day."""
    start_ts, end_ts = _local_day_bounds(d)
    now = time.time()
    if end_ts > now:
        end_ts = now
    panel = panel_energy_window(start_ts, end_ts)
    panel_wh = float(panel.get("wh") or 0.0)

    roles = _hvac_device_ids()
    cooling_wh = _device_energy_window(roles["cooling"], start_ts, end_ts) if roles["cooling"] else None
    heating_wh = _device_energy_window(roles["heating"], start_ts, end_ts) if roles["heating"] else None
    heating_therms = _gas_therms_for_window(start_ts, end_ts)

    temps = _temp_stats_for_window(start_ts, end_ts)
    empirical_min = temps["min_f"]
    empirical_max = temps["max_f"]

    # For today, pull forecast H/L if available and use it when empirical hasn't
    # built up yet (single-sample days where min == max == current).
    forecast_high = forecast_low = None
    if d == date.today():
        fc = get_today_forecast()
        if fc:
            forecast_high = fc.get("forecast_high_f")
            forecast_low = fc.get("forecast_low_f")
    # Effective min/max used by the dashboard:
    if forecast_high is not None:
        min_f = min(forecast_low, empirical_min) if (forecast_low is not None and empirical_min is not None) \
                else (forecast_low if empirical_min is None else empirical_min)
        max_f = max(forecast_high, empirical_max) if (forecast_high is not None and empirical_max is not None) \
                else (forecast_high if empirical_max is None else empirical_max)
    else:
        min_f, max_f = empirical_min, empirical_max

    avg_f = temps["avg_f"]
    base = float(settings.hdd_cdd_base_temp_f)
    hdd = max(0.0, base - avg_f) if avg_f is not None else None
    cdd = max(0.0, avg_f - base) if avg_f is not None else None

    setpoint = _setpoint_avg_for_window(start_ts, end_ts)

    if panel_wh <= 0 and temps["count"] == 0 and not force:
        return None

    row = {
        "date_str": d.isoformat(),
        "day_start_ts": start_ts,
        "panel_wh": panel_wh,
        "cooling_wh": cooling_wh,
        "heating_wh": heating_wh,
        "heating_therms": heating_therms,
        "avg_temp_f": avg_f,
        "min_temp_f": min_f,
        "max_temp_f": max_f,
        "forecast_high_f": forecast_high,
        "forecast_low_f": forecast_low,
        "hdd": hdd,
        "cdd": cdd,
        "base_temp_f": base,
        "avg_cool_setpoint_f": setpoint["avg_cool_setpoint_f"],
        "avg_heat_setpoint_f": setpoint["avg_heat_setpoint_f"],
        "sample_count": temps["count"],
        "rolled_up_ts": now,
    }
    # Also keep hvac_wh populated for backwards-compat with the 0.6.0 chart
    legacy_hvac_wh = cooling_wh if cooling_wh is not None else heating_wh
    with cursor() as cur:
        cur.execute(
            """INSERT INTO daily_rollups
               (date_str, day_start_ts, panel_wh, hvac_wh, cooling_wh, heating_wh,
                heating_therms,
                avg_temp_f, min_temp_f, max_temp_f, forecast_high_f, forecast_low_f,
                hdd, cdd, base_temp_f,
                avg_cool_setpoint_f, avg_heat_setpoint_f,
                sample_count, rolled_up_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date_str) DO UPDATE SET
                 day_start_ts        = excluded.day_start_ts,
                 panel_wh            = excluded.panel_wh,
                 hvac_wh             = excluded.hvac_wh,
                 cooling_wh          = excluded.cooling_wh,
                 heating_wh          = excluded.heating_wh,
                 heating_therms      = excluded.heating_therms,
                 avg_temp_f          = excluded.avg_temp_f,
                 min_temp_f          = excluded.min_temp_f,
                 max_temp_f          = excluded.max_temp_f,
                 forecast_high_f     = excluded.forecast_high_f,
                 forecast_low_f      = excluded.forecast_low_f,
                 hdd                 = excluded.hdd,
                 cdd                 = excluded.cdd,
                 base_temp_f         = excluded.base_temp_f,
                 avg_cool_setpoint_f = excluded.avg_cool_setpoint_f,
                 avg_heat_setpoint_f = excluded.avg_heat_setpoint_f,
                 sample_count        = excluded.sample_count,
                 rolled_up_ts        = excluded.rolled_up_ts""",
            (
                row["date_str"], row["day_start_ts"], row["panel_wh"],
                legacy_hvac_wh, row["cooling_wh"], row["heating_wh"],
                row["heating_therms"],
                row["avg_temp_f"], row["min_temp_f"], row["max_temp_f"],
                row["forecast_high_f"], row["forecast_low_f"],
                row["hdd"], row["cdd"], row["base_temp_f"],
                row["avg_cool_setpoint_f"], row["avg_heat_setpoint_f"],
                row["sample_count"], row["rolled_up_ts"],
            ),
        )
    return row


def backfill_recent_rollups(days: int = 31) -> int:
    """On startup and whenever the user changes the HVAC tagging or base temp,
    rebuild the last N days of rollups. Bounded by samples retention so we
    don't generate days with zero panel data."""
    today = date.today()
    n = 0
    for i in range(days):
        d = today - timedelta(days=i)
        if compute_rollup(d):
            n += 1
    return n


# --- regression-based anomaly ----------------------------------------------

def _completed_days(limit: int = 30) -> list[dict]:
    """Fetch the most recent N COMPLETED rollups (excluding today, which is
    still in progress). Skips days without temperature data."""
    today_str = date.today().isoformat()
    with cursor() as cur:
        cur.execute(
            """SELECT date_str, day_start_ts, panel_wh, hvac_wh,
                      cooling_wh, heating_wh, heating_therms,
                      avg_temp_f, hdd, cdd,
                      avg_cool_setpoint_f, avg_heat_setpoint_f
               FROM daily_rollups
               WHERE date_str != ? AND avg_temp_f IS NOT NULL AND panel_wh > 0
               ORDER BY date_str DESC LIMIT ?""",
            (today_str, limit),
        )
        cols = ["date_str", "day_start_ts", "panel_wh", "hvac_wh",
                "cooling_wh", "heating_wh", "heating_therms",
                "avg_temp_f", "hdd", "cdd",
                "avg_cool_setpoint_f", "avg_heat_setpoint_f"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fit_hdd_cdd(rollups: list[dict], target: str = "panel_wh") -> Optional[dict]:
    """Two-feature linear regression: kWh = a*HDD + b*CDD + intercept.
    Closed-form OLS on a 3-coefficient normal equation. Returns coeffs +
    R^2; returns None if we don't have enough variance to fit."""
    if len(rollups) < 5:
        return None
    # Build feature matrix
    X = []
    y = []
    for r in rollups:
        v = r.get(target)
        if v is None:
            continue
        X.append((r["hdd"] or 0.0, r["cdd"] or 0.0, 1.0))
        y.append(float(v) / 1000.0)  # kWh
    if len(y) < 5:
        return None
    n = len(y)
    # X^T X (3x3) and X^T y (3,)
    XtX = [[0.0] * 3 for _ in range(3)]
    Xty = [0.0] * 3
    for xi, yi in zip(X, y):
        for r in range(3):
            for c in range(3):
                XtX[r][c] += xi[r] * xi[c]
            Xty[r] += xi[r] * yi
    # Solve 3x3 via Cramer's rule (cheap and dependency-free)
    def det3(m):
        return (
            m[0][0] * (m[1][1]*m[2][2] - m[1][2]*m[2][1]) -
            m[0][1] * (m[1][0]*m[2][2] - m[1][2]*m[2][0]) +
            m[0][2] * (m[1][0]*m[2][1] - m[1][1]*m[2][0])
        )
    base = det3(XtX)
    if abs(base) < 1e-9:
        return None
    coefs = []
    for col in range(3):
        m = [row[:] for row in XtX]
        for r in range(3):
            m[r][col] = Xty[r]
        coefs.append(det3(m) / base)
    a, b, intercept = coefs
    # R^2
    y_mean = sum(y) / n
    ss_res = sum((yi - (a*xi[0] + b*xi[1] + intercept)) ** 2 for xi, yi in zip(X, y))
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
    return {
        "hdd_coef_kwh_per_dd": a,
        "cdd_coef_kwh_per_dd": b,
        "baseline_kwh": intercept,
        "r_squared": r2,
        "n": n,
        "target": target,
    }


def _predict_and_verdict(model, today_hdd, today_cdd, actual, elapsed_frac):
    """Common math for predicted-vs-actual + verdict label."""
    if model is None or today_hdd is None or today_cdd is None:
        return None
    full = (
        model["hdd_coef_kwh_per_dd"] * today_hdd +
        model["cdd_coef_kwh_per_dd"] * today_cdd +
        model["baseline_kwh"]
    )
    proportional = full * elapsed_frac
    delta = (actual or 0.0) - proportional
    pct = (delta / proportional * 100.0) if proportional > 0.01 else None
    verdict = "insufficient_baseline"
    if pct is None:
        verdict = "insufficient_baseline"
    elif pct > 25:
        verdict = "above_baseline"
    elif pct < -25:
        verdict = "below_baseline"
    else:
        verdict = "normal"
    return {
        "predicted_kwh_full_day": full,
        "predicted_kwh_so_far":   proportional,
        "delta_kwh": delta,
        "delta_pct": pct,
        "verdict":   verdict,
    }


def weather_anomaly() -> dict:
    """For today, fit three independent regressions on the rolling 30-day
    history: panel-wide, cooling-only, heating-only. Returns predicted vs
    actual + verdict for whichever ones have enough data. The frontend
    picks which sections to show based on which roles have been tagged."""
    today = date.today()
    start_ts, _ = _local_day_bounds(today)
    now = time.time()

    today_row = compute_rollup(today, force=True) or {}
    today_hdd = today_row.get("hdd")
    today_cdd = today_row.get("cdd")
    today_avg_f = today_row.get("avg_temp_f")
    elapsed_frac = max(0.0, min(1.0, (now - start_ts) / 86400.0))

    panel_actual = float(today_row.get("panel_wh") or 0.0) / 1000.0
    cool_actual = float(today_row.get("cooling_wh") or 0.0) / 1000.0 if today_row.get("cooling_wh") else 0.0
    heat_actual = float(today_row.get("heating_wh") or 0.0) / 1000.0 if today_row.get("heating_wh") else 0.0
    heat_therms_actual = float(today_row.get("heating_therms") or 0.0) if today_row.get("heating_therms") else 0.0

    history = _completed_days(limit=30)

    panel_model = _fit_hdd_cdd(history, target="panel_wh")
    cool_model = _fit_hdd_cdd(
        [r for r in history if r.get("cooling_wh") and r["cooling_wh"] > 0],
        target="cooling_wh",
    )
    heat_model = _fit_hdd_cdd(
        [r for r in history if r.get("heating_wh") and r["heating_wh"] > 0],
        target="heating_wh",
    )

    out = {
        "today_avg_temp_f": today_avg_f,
        "today_hdd": today_hdd,
        "today_cdd": today_cdd,
        "base_temp_f": float(settings.hdd_cdd_base_temp_f),
        "history_days": len(history),
        "panel":   {"model": panel_model, "today_actual_kwh": panel_actual,
                    **(_predict_and_verdict(panel_model, today_hdd, today_cdd, panel_actual, elapsed_frac) or {})},
        "cooling": {"model": cool_model,  "today_actual_kwh": cool_actual,
                    **(_predict_and_verdict(cool_model, today_hdd, today_cdd, cool_actual, elapsed_frac) or {})},
        "heating": {"model": heat_model,  "today_actual_kwh": heat_actual,
                    "today_actual_therms": heat_therms_actual,
                    **(_predict_and_verdict(heat_model, today_hdd, today_cdd, heat_actual, elapsed_frac) or {})},
    }
    return out


# Keep _fit_hdd_cdd's `target` parameter robust against the renamed columns:
# add 'panel_wh', 'cooling_wh', 'heating_wh' (already supported via dict access).


# ---------------------------------------------------------------------------
# Setpoint-adjusted savings model
# ---------------------------------------------------------------------------
#
# Standard CDD/HDD use a fixed balance-point temperature (65 °F by default).
# For "what would it cost if I changed my setpoint" we need to know energy as
# a function of the indoor-outdoor differential — so we recompute degree-days
# against the actual setpoint each day and fit:
#
#     cooling_kwh = a_c * setpoint_CDD + b_c   where setpoint_CDD = max(0, T_outside - cool_setpoint)
#     heating_kwh = a_h * setpoint_HDD + b_h   where setpoint_HDD = max(0, heat_setpoint - T_outside)
#
# The coefficient a_c (or a_h) is "kWh per °F of differential per day". For a
# ΔS change in setpoint at outside temperature T, the new differential shifts
# by ΔS on the days when cooling/heating is actually needed, so the predicted
# kWh change is a_c * (new_DD - old_DD) summed over a representative period.
# If we don't have enough setpoint variance to fit, we fall back to the
# industry rule of thumb (~5% per °F).

_RULE_OF_THUMB_PCT_PER_F = 5.0
_MIN_DAYS_FOR_FIT = 14
_MIN_SETPOINT_VARIANCE_F = 1.5    # at least this much stdev in setpoints


def _setpoint_variance(rollups: list[dict], key: str) -> float:
    vals = [r[key] for r in rollups if r.get(key) is not None]
    if len(vals) < 3:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _fit_setpoint_dd(
    rollups: list[dict],
    target_col: str,
    setpoint_col: str,
    direction: str,        # 'cool' | 'heat'
) -> Optional[dict]:
    """OLS fit of `target_kwh = a * setpoint_DD + b` where setpoint_DD is
    computed per-row from outside_avg_temp and the daily avg setpoint."""
    pts = []
    for r in rollups:
        target = r.get(target_col)
        sp = r.get(setpoint_col)
        T = r.get("avg_temp_f")
        if target is None or target <= 0 or sp is None or T is None:
            continue
        if direction == "cool":
            dd = max(0.0, T - sp)
        else:
            dd = max(0.0, sp - T)
        # Skip days where DD == 0 — they contribute zero gradient and inflate
        # the intercept; the model should describe days the unit ran
        if dd <= 0:
            continue
        pts.append((dd, float(target) / 1000.0))   # kWh
    if len(pts) < _MIN_DAYS_FOR_FIT:
        return None
    sp_variance = _setpoint_variance(rollups, setpoint_col)
    if sp_variance < _MIN_SETPOINT_VARIANCE_F:
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    y_mean = sy / n
    ss_res = sum((p[1] - (a * p[0] + b)) ** 2 for p in pts)
    ss_tot = sum((p[1] - y_mean) ** 2 for p in pts)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
    return {
        "kwh_per_dd": a,
        "intercept_kwh": b,
        "r_squared": r2,
        "n": n,
        "sp_variance_f": sp_variance,
    }


def _project_delta(
    rollups: list[dict],
    current_setpoint_f: float,
    delta_f: float,
    coef_kwh_per_dd: float,
    direction: str,
) -> dict:
    """For the last ~30 days of weather, compute the kWh delta if the setpoint
    had been (current + delta) instead. Sums across days, scales to 30-day
    'monthly' estimate."""
    deltas_kwh = []
    for r in rollups:
        T = r.get("avg_temp_f")
        if T is None:
            continue
        if direction == "cool":
            old_dd = max(0.0, T - current_setpoint_f)
            new_dd = max(0.0, T - (current_setpoint_f + delta_f))
        else:
            old_dd = max(0.0, current_setpoint_f - T)
            new_dd = max(0.0, (current_setpoint_f + delta_f) - T)
        deltas_kwh.append(coef_kwh_per_dd * (new_dd - old_dd))
    if not deltas_kwh:
        return {"delta_kwh_30d": 0.0, "days_used": 0}
    total = sum(deltas_kwh)
    return {"delta_kwh_30d": total, "days_used": len(deltas_kwh)}


# Rough fraction of panel kWh attributable to HVAC when no per-device data
# exists yet. These are residential averages; the fitted regression replaces
# them as soon as we have enough data to fit. Cooling-season ≈ when there's
# any CDD; heating-season ≈ when there's any HDD.
_HVAC_FRACTION_COOLING_SEASON = 0.40   # AC + blower as fraction of summer panel kWh
_HVAC_FRACTION_HEATING_SEASON = 0.50   # electric resistance / heat-pump backup fraction


def _project_rule_of_thumb(
    rollups: list[dict],
    target_col: str,
    delta_f: float,
    direction: str,
) -> dict:
    """Fallback when we can't fit a real model. ~5%/°F applied to the last
    30 days of role-specific energy. For cooling, raising setpoint (positive
    delta) SAVES energy; for heating, raising setpoint (positive delta)
    USES MORE energy.

    When the role column has no data yet (user hasn't tagged a cooling /
    heating device), fall back to panel_wh × HVAC fraction so the user
    still sees a meaningful estimate rather than $0.
    """
    role_kwh = 0.0
    n_role = 0
    panel_seasonal_kwh = 0.0
    n_panel = 0
    for r in rollups:
        v = r.get(target_col)
        if v is not None and v > 0:
            role_kwh += v / 1000.0
            n_role += 1
        T = r.get("avg_temp_f")
        if T is not None:
            in_cooling_season = (r.get("cdd") or 0) > 0
            in_heating_season = (r.get("hdd") or 0) > 0
            if direction == "cool" and in_cooling_season:
                panel_seasonal_kwh += (r.get("panel_wh") or 0) / 1000.0
                n_panel += 1
            elif direction == "heat" and in_heating_season:
                panel_seasonal_kwh += (r.get("panel_wh") or 0) / 1000.0
                n_panel += 1

    pct_change = -(_RULE_OF_THUMB_PCT_PER_F / 100.0) * delta_f if direction == "cool" \
        else (_RULE_OF_THUMB_PCT_PER_F / 100.0) * delta_f

    # Prefer role-specific data when we have it
    if n_role >= 5:
        return {
            "delta_kwh_30d": role_kwh * pct_change,
            "days_used": n_role,
            "basis": "role_energy",
        }
    # Fall back to panel_wh × HVAC fraction
    if n_panel >= 5:
        frac = _HVAC_FRACTION_COOLING_SEASON if direction == "cool" else _HVAC_FRACTION_HEATING_SEASON
        return {
            "delta_kwh_30d": panel_seasonal_kwh * frac * pct_change,
            "days_used": n_panel,
            "basis": f"panel_estimate_{int(frac*100)}pct",
        }
    return {"delta_kwh_30d": 0.0, "days_used": 0, "basis": "no_data"}


def _current_setpoint(direction: str) -> Optional[float]:
    """Most recent target_temp / target_low / target_high from setpoint_samples
    across all known climate entities, averaged."""
    col = "target_high_f" if direction == "cool" else "target_low_f"
    with cursor() as cur:
        cur.execute(
            f"""SELECT entity_id, target_temp_f, {col} FROM setpoint_samples
                WHERE ts >= ?
                AND (target_temp_f IS NOT NULL OR {col} IS NOT NULL)
                ORDER BY ts DESC""",
            (time.time() - 86400 * 2,),
        )
        rows = cur.fetchall()
    seen = {}
    for ent, target, dual in rows:
        if ent in seen:
            continue
        seen[ent] = dual if dual is not None else target
    vals = [v for v in seen.values() if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def predict_upcoming_energy(days_ahead: int = 7) -> dict:
    """Predict panel kWh for tomorrow + next N-1 days using the HDD/CDD
    regression fit on the rolling 30-day history, against the forecast
    high/low from the HA weather entity. Falls back to recent-average kWh
    when the regression hasn't converged."""
    today = date.today()
    base = float(settings.hdd_cdd_base_temp_f)
    rate_dollars_per_kwh = float(settings.electricity_rate_cents_per_kwh or 0.0) / 100.0

    # Fit panel-wide regression on recent completed days
    history = _completed_days(limit=30)
    model = _fit_hdd_cdd(history, target="panel_wh")

    # Pull forecast for the next N days
    with cursor() as cur:
        cur.execute(
            "SELECT date_str, forecast_high_f, forecast_low_f, forecast_avg_f, condition "
            "FROM weather_forecast_daily WHERE date_str >= ? ORDER BY date_str LIMIT ?",
            ((today + timedelta(days=1)).isoformat(), days_ahead),
        )
        cols = ["date_str", "forecast_high_f", "forecast_low_f", "forecast_avg_f", "condition"]
        fc_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Recent kWh baseline as a fallback when no model
    recent_kwh_per_day = None
    if history:
        recent_kwh = sum((r.get("panel_wh") or 0) / 1000.0 for r in history) / max(1, len(history))
        recent_kwh_per_day = recent_kwh

    predictions = []
    for fc in fc_rows:
        ds = fc["date_str"]
        avg_f = fc.get("forecast_avg_f")
        if avg_f is None:
            high = fc.get("forecast_high_f")
            low = fc.get("forecast_low_f")
            if high is not None and low is not None:
                avg_f = (high + low) / 2.0
            elif high is not None:
                avg_f = high
            elif low is not None:
                avg_f = low

        hdd = max(0.0, base - avg_f) if avg_f is not None else None
        cdd = max(0.0, avg_f - base) if avg_f is not None else None

        if model is not None and hdd is not None and cdd is not None:
            kwh = max(0.0,
                model["hdd_coef_kwh_per_dd"] * hdd +
                model["cdd_coef_kwh_per_dd"] * cdd +
                model["baseline_kwh"]
            )
            source = "regression"
        elif recent_kwh_per_day is not None:
            kwh = recent_kwh_per_day
            source = "recent_average"
        else:
            kwh = 0.0
            source = "unknown"

        predictions.append({
            "date_str": ds,
            "forecast_high_f": fc.get("forecast_high_f"),
            "forecast_low_f": fc.get("forecast_low_f"),
            "forecast_avg_f": avg_f,
            "hdd": hdd,
            "cdd": cdd,
            "condition": fc.get("condition"),
            "predicted_kwh": kwh,
            "predicted_cost": kwh * rate_dollars_per_kwh,
            "source": source,
        })

    total_kwh = sum(p["predicted_kwh"] for p in predictions)
    return {
        "days": predictions,
        "total_kwh": total_kwh,
        "total_cost": total_kwh * rate_dollars_per_kwh,
        "model_r_squared": (model or {}).get("r_squared") if model else None,
        "model_n": (model or {}).get("n") if model else None,
        "has_forecast": len(predictions) > 0,
        "base_temp_f": base,
        "rate_cents_per_kwh": settings.electricity_rate_cents_per_kwh,
        "currency_symbol": settings.currency_symbol,
        "temp_unit": settings.temp_unit,
    }


def setpoint_savings(deltas_f: Optional[list[float]] = None) -> dict:
    """For each direction (cooling, heating), compute the projected monthly
    energy and cost change for setpoint adjustments in `deltas_f` (degrees F,
    positive = raise setpoint, negative = lower). If we have a fitted model
    with enough setpoint variance, use it; otherwise fall back to the ~5%/°F
    rule of thumb. Cost uses the configured electricity rate (and gas rate
    for heating-by-gas, when no electric heating device is tagged)."""
    if deltas_f is None:
        deltas_f = [-2, -1, +1, +2]

    history = _completed_days(limit=30)
    rate_cents = float(settings.electricity_rate_cents_per_kwh or 0.0)
    rate_dollars_per_kwh = rate_cents / 100.0
    gas_rate = float(settings.gas_rate_dollars_per_therm or 0.0)

    def _build(direction: str, target_col: str, setpoint_col: str, current_f: Optional[float]) -> dict:
        model = _fit_setpoint_dd(history, target_col, setpoint_col, direction)
        # Even without a current setpoint reading we still compute scenarios
        # against a default baseline so the user gets a ballpark number.
        used_default_setpoint = False
        if current_f is None:
            current_f = 72.0 if direction == "cool" else 68.0   # common US defaults
            used_default_setpoint = True
        scenarios = []
        used_method = "fitted_regression" if model else "rule_of_thumb_5pct"
        for d in deltas_f:
            if model:
                proj = _project_delta(history, current_f, d, model["kwh_per_dd"], direction)
                basis = "fitted"
            else:
                proj = _project_rule_of_thumb(history, target_col, d, direction)
                basis = proj.get("basis", "rule_of_thumb")
            dkwh = proj["delta_kwh_30d"]
            dcost = dkwh * rate_dollars_per_kwh
            scenarios.append({
                "delta_f": d,
                "monthly_kwh_delta": dkwh,
                "monthly_cost_delta": dcost,
                "days_used": proj["days_used"],
                "basis": basis,
            })
        return {
            "has_model": model is not None,
            "current_setpoint_f": current_f,
            "used_default_setpoint": used_default_setpoint,
            "method": used_method,
            "basis": (scenarios[0].get("basis") if scenarios else None) if not model else "fitted",
            "kwh_per_dd": model["kwh_per_dd"] if model else None,
            "r_squared": model["r_squared"] if model else None,
            "n": model["n"] if model else None,
            "sp_variance_f": model["sp_variance_f"] if model else None,
            "needs": None if model else
                f"need ≥{_MIN_DAYS_FOR_FIT} days with cooling/heating running and ≥{_MIN_SETPOINT_VARIANCE_F:.1f}°F of setpoint variation; "
                f"using {_RULE_OF_THUMB_PCT_PER_F}%/°F rule of thumb in the meantime",
            "scenarios": scenarios,
        }

    cooling_now = _current_setpoint("cool")
    heating_now = _current_setpoint("heat")
    return {
        "cooling": _build("cool", "cooling_wh", "avg_cool_setpoint_f", cooling_now),
        "heating": _build("heat", "heating_wh", "avg_heat_setpoint_f", heating_now),
        "rate_cents_per_kwh": rate_cents,
        "currency_symbol": settings.currency_symbol,
        "gas_rate_dollars_per_therm": gas_rate,
    }
