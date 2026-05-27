"""Daily summary, phantom load, per-device statistics, anomaly hints.

All computed on-the-fly from the existing tables so the data is always
consistent with whatever the live event matcher just wrote. Cheap on small
households' worth of data; if profiles look problematic we can cache.
"""
from __future__ import annotations

import time
from typing import Optional

from .config import settings
from .db import cursor


def _dict_row(cur, row):
    return {col[0]: row[i] for i, col in enumerate(cur.description)}


def _today_start() -> float:
    now = time.time()
    t = time.localtime(now)
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, t.tm_isdst))
    return midnight


def _cost(wh: float) -> float:
    rate = float(settings.electricity_rate_cents_per_kwh or 0.0)
    if rate <= 0:
        return 0.0
    return (wh / 1000.0) * rate / 100.0


def device_stats(device_id: int, since_ts: Optional[float] = None) -> dict:
    """Cycle count, on-time, energy used in the given window for one device.

    Energy is approximated as mean_power_w × total_on_seconds — the same
    model the device-row uses, just summed over each completed cycle inside
    the window. If the device is currently on, the open interval contributes
    from its last_on_ts to now.
    """
    if since_ts is None:
        since_ts = _today_start()
    now = time.time()

    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT id, name, is_on, last_on_ts, last_off_ts, mean_power_w, "
            "is_continuous "
            "FROM devices WHERE id = ?",
            (device_id,),
        )
        dev = cur.fetchone()
        if dev is None:
            return {}

        # State transitions in window, oldest first.
        cur.execute(
            "SELECT ts, state FROM device_state_log "
            "WHERE device_id = ? AND ts >= ? ORDER BY ts",
            (device_id, since_ts),
        )
        rows = cur.fetchall()

    cycles = 0
    on_seconds = 0.0

    # Reconstruct cycles. We may start the window mid-cycle (device was
    # already on before since_ts), in which case the first transition in
    # the window is an off-event; pair it with since_ts as the implicit
    # start.
    last_on: Optional[float] = None
    # Was the device on at since_ts? Approximate: if there's a known last_on_ts
    # before since_ts and no off_ts between them, assume yes.
    cur_on_at_start = False
    if dev.get("last_on_ts") and dev["last_on_ts"] < since_ts:
        # Check if there was an off after last_on_ts but before since_ts
        with cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM device_state_log "
                "WHERE device_id = ? AND state = 'off' "
                "AND ts > ? AND ts < ?",
                (device_id, dev["last_on_ts"], since_ts),
            )
            had_off = cur.fetchone()[0] > 0
        if not had_off:
            cur_on_at_start = True

    if cur_on_at_start:
        last_on = since_ts

    for row in rows:
        ts = float(row["ts"])
        if row["state"] == "on":
            if last_on is None:
                last_on = ts
        else:  # off
            if last_on is not None:
                on_seconds += max(0.0, ts - last_on)
                cycles += 1
                last_on = None
    # Tail: still on at now
    if last_on is not None:
        on_seconds += max(0.0, now - last_on)

    mean_w = float(dev["mean_power_w"] or 0.0)
    # Continuous devices: energy is just mean_w × window-seconds (they don't
    # cycle, so on_seconds estimated above may be 0).
    if dev.get("is_continuous"):
        on_seconds = max(on_seconds, now - since_ts) if dev["is_on"] else on_seconds
    wh = mean_w * on_seconds / 3600.0

    return {
        "device_id": device_id,
        "name": dev["name"],
        "mean_power_w": mean_w,
        "is_on": bool(dev["is_on"]),
        "is_continuous": bool(dev.get("is_continuous")),
        "cycles_today": cycles,
        "runtime_seconds": on_seconds,
        "energy_wh": wh,
        "cost": _cost(wh),
    }


def phantom_load() -> dict:
    """Estimate the always-on baseline by taking the 10th percentile of
    total_power over the last 24h. Below that is rare-quiet noise; above is
    intermittent device activity. Returns watts plus extrapolated daily
    energy + cost.
    """
    cutoff = time.time() - 24 * 3600
    with cursor() as cur:
        cur.execute(
            "SELECT total_power FROM samples WHERE ts >= ? AND total_power IS NOT NULL",
            (cutoff,),
        )
        vals = [row[0] for row in cur.fetchall() if row[0] is not None]
    if not vals:
        return {"watts": 0.0, "daily_wh": 0.0, "daily_cost": 0.0, "sample_count": 0}
    vals.sort()
    idx = max(0, int(len(vals) * 0.10) - 1)
    baseline_w = float(vals[idx])
    daily_wh = baseline_w * 24
    return {
        "watts": baseline_w,
        "daily_wh": daily_wh,
        "daily_cost": _cost(daily_wh),
        "sample_count": len(vals),
    }


def panel_energy_today() -> dict:
    """Sum total_power × dt across today's samples for the panel-wide kWh."""
    since = _today_start()
    now = time.time()
    with cursor() as cur:
        cur.execute(
            "SELECT ts, total_power FROM samples "
            "WHERE ts >= ? AND total_power IS NOT NULL ORDER BY ts",
            (since,),
        )
        rows = cur.fetchall()
    if not rows:
        return {"wh": 0.0, "cost": 0.0, "since": since, "now": now}
    wh = 0.0
    prev_ts, prev_p = rows[0]
    for ts, p in rows[1:]:
        dt = ts - prev_ts
        if 0 < dt < 300:   # ignore gaps > 5 min
            wh += ((prev_p + p) / 2.0) * dt / 3600.0
        prev_ts, prev_p = ts, p
    return {"wh": wh, "cost": _cost(wh), "since": since, "now": now}


def all_device_stats() -> list[dict]:
    since = _today_start()
    with cursor() as cur:
        cur.execute("SELECT id FROM devices ORDER BY name")
        ids = [row[0] for row in cur.fetchall()]
    return [device_stats(i, since) for i in ids]


def anomaly_check(stats: dict) -> Optional[str]:
    """Return a short anomaly message if the device's current runtime is
    well above its rolling average; else None. Only meaningful for devices
    with established usage patterns (>5 historical cycles).
    """
    device_id = stats.get("device_id")
    if not device_id or stats.get("is_continuous") or not stats.get("is_on"):
        return None

    # Average on-period duration from the last 14 days, excluding the current
    # in-progress one.
    cutoff = time.time() - 14 * 86400
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT ts, state FROM device_state_log "
            "WHERE device_id = ? AND ts >= ? ORDER BY ts",
            (device_id, cutoff),
        )
        rows = cur.fetchall()

    durations = []
    last_on = None
    for row in rows:
        if row["state"] == "on":
            last_on = float(row["ts"])
        elif last_on is not None:
            durations.append(float(row["ts"]) - last_on)
            last_on = None
    if len(durations) < 5:
        return None
    avg = sum(durations) / len(durations)
    runtime = float(stats.get("runtime_seconds") or 0.0)
    if avg > 0 and runtime > avg * 2.5:
        return f"running ~{runtime/60:.0f} min (avg {avg/60:.0f} min)"
    return None


def insights() -> dict:
    devices = all_device_stats()
    for d in devices:
        msg = anomaly_check(d)
        if msg:
            d["anomaly"] = msg

    devices_sorted = sorted(devices, key=lambda d: d.get("energy_wh") or 0.0, reverse=True)
    top = devices_sorted[:5]

    panel = panel_energy_today()
    phantom = phantom_load()
    attributed_wh = sum(d.get("energy_wh") or 0.0 for d in devices)
    unattributed_wh = max(0.0, panel["wh"] - attributed_wh)

    return {
        "now": time.time(),
        "today_start": _today_start(),
        "panel_today": panel,
        "phantom_load": phantom,
        "top_devices_today": top,
        "all_devices_today": devices,
        "attributed_wh": attributed_wh,
        "unattributed_wh": unattributed_wh,
        "anomalies": [d for d in devices if d.get("anomaly")],
        "rate_cents_per_kwh": settings.electricity_rate_cents_per_kwh,
        "currency_symbol": settings.currency_symbol,
    }
