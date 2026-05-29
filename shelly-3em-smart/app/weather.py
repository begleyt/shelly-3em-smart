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

def record_weather_reading(
    temp_f: float,
    humidity: Optional[float] = None,
    condition: Optional[str] = None,
    source: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict:
    """Store a single weather reading from the HACS integration."""
    if ts is None:
        ts = time.time()
    with cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO weather_samples (ts, temp_f, humidity, condition, source) "
            "VALUES (?,?,?,?,?)",
            (ts, temp_f, humidity, condition, source),
        )
    return {"ts": ts, "temp_f": temp_f}


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


def _hvac_device_id() -> Optional[int]:
    with cursor() as cur:
        cur.execute("SELECT id FROM devices WHERE is_hvac = 1 LIMIT 1")
        row = cur.fetchone()
    return int(row[0]) if row else None


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
    """Build (or rebuild) the rollup row for one local-calendar day.
    Returns the row's dict if produced, or None if we have no data at all
    for the day (avoids polluting the table with empty rows)."""
    start_ts, end_ts = _local_day_bounds(d)
    now = time.time()
    if end_ts > now:
        # Cap to "now" for today — we'll re-roll later when the day completes.
        end_ts = now
    panel = panel_energy_window(start_ts, end_ts)
    panel_wh = float(panel.get("wh") or 0.0)

    hvac_id = _hvac_device_id()
    hvac_wh = _device_energy_window(hvac_id, start_ts, end_ts) if hvac_id else None

    temps = _temp_stats_for_window(start_ts, end_ts)
    avg_f = temps["avg_f"]
    base = float(settings.hdd_cdd_base_temp_f)
    hdd = max(0.0, base - avg_f) if avg_f is not None else None
    cdd = max(0.0, avg_f - base) if avg_f is not None else None

    if panel_wh <= 0 and temps["count"] == 0 and not force:
        return None

    row = {
        "date_str": d.isoformat(),
        "day_start_ts": start_ts,
        "panel_wh": panel_wh,
        "hvac_wh": hvac_wh,
        "avg_temp_f": avg_f,
        "min_temp_f": temps["min_f"],
        "max_temp_f": temps["max_f"],
        "hdd": hdd,
        "cdd": cdd,
        "base_temp_f": base,
        "sample_count": temps["count"],
        "rolled_up_ts": now,
    }
    with cursor() as cur:
        cur.execute(
            """INSERT INTO daily_rollups
               (date_str, day_start_ts, panel_wh, hvac_wh,
                avg_temp_f, min_temp_f, max_temp_f, hdd, cdd, base_temp_f,
                sample_count, rolled_up_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date_str) DO UPDATE SET
                 day_start_ts = excluded.day_start_ts,
                 panel_wh     = excluded.panel_wh,
                 hvac_wh      = excluded.hvac_wh,
                 avg_temp_f   = excluded.avg_temp_f,
                 min_temp_f   = excluded.min_temp_f,
                 max_temp_f   = excluded.max_temp_f,
                 hdd          = excluded.hdd,
                 cdd          = excluded.cdd,
                 base_temp_f  = excluded.base_temp_f,
                 sample_count = excluded.sample_count,
                 rolled_up_ts = excluded.rolled_up_ts""",
            (
                row["date_str"], row["day_start_ts"], row["panel_wh"], row["hvac_wh"],
                row["avg_temp_f"], row["min_temp_f"], row["max_temp_f"],
                row["hdd"], row["cdd"], row["base_temp_f"],
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
                      avg_temp_f, hdd, cdd
               FROM daily_rollups
               WHERE date_str != ? AND avg_temp_f IS NOT NULL AND panel_wh > 0
               ORDER BY date_str DESC LIMIT ?""",
            (today_str, limit),
        )
        cols = ["date_str", "day_start_ts", "panel_wh", "hvac_wh",
                "avg_temp_f", "hdd", "cdd"]
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


def weather_anomaly() -> dict:
    """For today: predicted kWh from the temperature regression vs actual so
    far. Returns a structured response the dashboard can render even when
    we don't yet have enough history to fit a model."""
    today = date.today()
    start_ts, _ = _local_day_bounds(today)
    now = time.time()

    today_row = compute_rollup(today, force=True) or {}
    actual_kwh = float(today_row.get("panel_wh") or 0.0) / 1000.0
    today_hdd = today_row.get("hdd")
    today_cdd = today_row.get("cdd")
    today_avg_f = today_row.get("avg_temp_f")

    history = _completed_days(limit=30)
    model = _fit_hdd_cdd(history, target="panel_wh")

    out = {
        "today_actual_kwh": actual_kwh,
        "today_avg_temp_f": today_avg_f,
        "today_hdd": today_hdd,
        "today_cdd": today_cdd,
        "base_temp_f": float(settings.hdd_cdd_base_temp_f),
        "model": model,
        "history_days": len(history),
    }

    if model is not None and today_hdd is not None and today_cdd is not None:
        # Predicted for a FULL day at today's temperature
        full_day_predicted = (
            model["hdd_coef_kwh_per_dd"] * today_hdd +
            model["cdd_coef_kwh_per_dd"] * today_cdd +
            model["baseline_kwh"]
        )
        # Pro-rate for the fraction of the day elapsed so the comparison is
        # apples-to-apples at, say, 3pm
        elapsed_frac = max(0.0, min(1.0, (now - start_ts) / 86400.0))
        proportional_predicted = full_day_predicted * elapsed_frac
        delta = actual_kwh - proportional_predicted
        pct = (delta / proportional_predicted * 100.0) if proportional_predicted > 0.01 else None
        out["predicted_kwh_full_day"] = full_day_predicted
        out["predicted_kwh_so_far"] = proportional_predicted
        out["delta_kwh"] = delta
        out["delta_pct"] = pct
        if pct is None:
            out["verdict"] = "insufficient_baseline"
        elif pct > 25:
            out["verdict"] = "above_baseline"
        elif pct < -25:
            out["verdict"] = "below_baseline"
        else:
            out["verdict"] = "normal"

    return out
