"""Ingest HA energy sensor readings into the add-on.

The HACS integration polls user-selected sensors and POSTs each reading
here. For every entity we track a cumulative `latest_energy_kwh` plus a
`baseline_energy_kwh` snapshotted at midnight, so today's delta is just
`latest - baseline`. The auto-create-device step makes any tracked
energy entity show up immediately as a "metered" device — accurate
energy reporting without needing the clusterer to discover it first.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .db import cursor
from .insights import _today_start

log = logging.getLogger(__name__)

ON_THRESHOLD_W = 5.0   # below this we treat the metered device as off


def _dict_row(cur, row):
    return {col[0]: row[i] for i, col in enumerate(cur.description)}


def record_energy_reading(
    entity_id: str,
    energy_kwh: float,
    power_w: Optional[float] = None,
    friendly_name: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict:
    if ts is None:
        ts = time.time()
    today_start = _today_start()

    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT entity_id, friendly_name, device_id, baseline_ts, baseline_energy_kwh "
            "FROM ha_energy_sources WHERE entity_id = ?",
            (entity_id,),
        )
        row = cur.fetchone()

        if row is None:
            # First time seeing this sensor — record it and auto-create a
            # device unless one already exists with this source_entity_id.
            cur.execute(
                """INSERT INTO ha_energy_sources
                (entity_id, friendly_name, baseline_energy_kwh, baseline_ts,
                 latest_energy_kwh, latest_power_w, latest_ts, first_seen_ts)
                VALUES (?,?,?,?,?,?,?,?)""",
                (entity_id, friendly_name, energy_kwh, today_start,
                 energy_kwh, power_w, ts, ts),
            )
            cur.execute(
                "SELECT id FROM devices WHERE source_entity_id = ? LIMIT 1",
                (entity_id,),
            )
            existing = cur.fetchone()
            if existing is None:
                name = friendly_name or entity_id
                is_on = 1 if (power_w or 0) >= ON_THRESHOLD_W else 0
                cur.execute(
                    """INSERT INTO devices
                    (name, notes, created_ts, is_on, last_on_ts, mean_power_w,
                     total_energy_wh, source_entity_id, energy_source)
                    VALUES (?,?,?,?,?,?,0,?,?)""",
                    (
                        name,
                        f"Metered via HA entity {entity_id}",
                        ts,
                        is_on,
                        ts if is_on else None,
                        float(power_w or 0),
                        entity_id,
                        "metered",
                    ),
                )
                device_id = cur.lastrowid
                cur.execute(
                    "UPDATE ha_energy_sources SET device_id = ? WHERE entity_id = ?",
                    (device_id, entity_id),
                )
                log.info("Auto-created metered device #%d (%s) from %s", device_id, name, entity_id)
            else:
                # Link existing device to this source
                cur.execute(
                    "UPDATE ha_energy_sources SET device_id = ? WHERE entity_id = ?",
                    (existing["id"], entity_id),
                )
                cur.execute(
                    "UPDATE devices SET energy_source = 'metered' WHERE id = ?",
                    (existing["id"],),
                )
            return {"entity_id": entity_id, "auto_created": existing is None}

        # Existing source — update latest, roll baseline at midnight
        device_id = row.get("device_id")
        baseline_ts = row.get("baseline_ts") or 0
        baseline_kwh = row.get("baseline_energy_kwh")
        if baseline_ts < today_start:
            # New day — reset baseline to the current reading
            cur.execute(
                """UPDATE ha_energy_sources SET
                    friendly_name = COALESCE(?, friendly_name),
                    baseline_energy_kwh = ?,
                    baseline_ts = ?,
                    latest_energy_kwh = ?,
                    latest_power_w = ?,
                    latest_ts = ?
                   WHERE entity_id = ?""",
                (friendly_name, energy_kwh, today_start, energy_kwh, power_w, ts, entity_id),
            )
        else:
            cur.execute(
                """UPDATE ha_energy_sources SET
                    friendly_name = COALESCE(?, friendly_name),
                    latest_energy_kwh = ?,
                    latest_power_w = ?,
                    latest_ts = ?
                   WHERE entity_id = ?""",
                (friendly_name, energy_kwh, power_w, ts, entity_id),
            )

        # Push state to linked device: is_on driven by current power_w,
        # last_on_ts / last_off_ts updated on transitions.
        if device_id is not None and power_w is not None:
            cur.execute(
                "SELECT is_on FROM devices WHERE id = ?",
                (device_id,),
            )
            d = cur.fetchone()
            if d is not None:
                was_on = bool(d.get("is_on"))
                now_on = power_w >= ON_THRESHOLD_W
                if now_on and not was_on:
                    cur.execute(
                        "UPDATE devices SET is_on = 1, last_on_ts = ?, mean_power_w = ? WHERE id = ?",
                        (ts, power_w, device_id),
                    )
                elif now_on and was_on:
                    # Keep mean_power_w roughly fresh from the meter
                    cur.execute(
                        "UPDATE devices SET mean_power_w = ? WHERE id = ?",
                        (power_w, device_id),
                    )
                elif (not now_on) and was_on:
                    cur.execute(
                        "UPDATE devices SET is_on = 0, last_off_ts = ? WHERE id = ?",
                        (ts, device_id),
                    )

        return {"entity_id": entity_id, "auto_created": False}


def list_energy_sources() -> list[dict]:
    today_start = _today_start()
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT entity_id, friendly_name, device_id, baseline_energy_kwh, baseline_ts, "
            "latest_energy_kwh, latest_power_w, latest_ts, first_seen_ts "
            "FROM ha_energy_sources ORDER BY friendly_name, entity_id"
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        baseline_ts = r.get("baseline_ts") or 0
        baseline_kwh = r.get("baseline_energy_kwh") or 0.0
        latest_kwh = r.get("latest_energy_kwh") or 0.0
        if baseline_ts < today_start:
            # Will be reset on next reading; for now best-effort
            today_kwh = 0.0
        else:
            today_kwh = max(0.0, latest_kwh - baseline_kwh)
        r["today_kwh"] = today_kwh
        r["today_wh"] = today_kwh * 1000
        out.append(r)
    return out


def metered_today_wh(entity_id: str) -> Optional[float]:
    today_start = _today_start()
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT baseline_ts, baseline_energy_kwh, latest_energy_kwh "
            "FROM ha_energy_sources WHERE entity_id = ?",
            (entity_id,),
        )
        row = cur.fetchone()
    if row is None or row.get("latest_energy_kwh") is None:
        return None
    baseline_ts = row.get("baseline_ts") or 0
    if baseline_ts < today_start:
        return 0.0
    return max(0.0, (row["latest_energy_kwh"] - (row.get("baseline_energy_kwh") or 0)) * 1000.0)
