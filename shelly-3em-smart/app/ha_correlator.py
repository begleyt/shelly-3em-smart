"""Correlate HA state changes with detected step events.

When the HACS integration POSTs a state change for an entity the user has
elected to track, we record it in `ha_events`. On every step event we look
for an HA event within ±CORRELATION_WINDOW_S whose direction (on/off)
matches the step direction. A hit:

  1. Updates the entity's running signature (mean power, per-leg means).
  2. Increments the entity's match_count.
  3. If match_count >= PROMOTION_THRESHOLD and no device has been promoted
     yet, auto-creates a device for the entity with the accumulated mean
     power as the signature snapshot.

This is a much stronger signal than the unsupervised clusterer: HA *knows*
when each appliance turned on/off, so we don't need to guess from waveform
similarity.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .db import cursor

log = logging.getLogger(__name__)


CORRELATION_WINDOW_S = 5.0
PROMOTION_THRESHOLD = 3   # promote to device after N correlated events


# State-string normalization. HA state strings vary by domain — these lists
# cover the common cases. Anything not listed is treated as "unknown direction".
ON_STATES = {
    "on", "true", "open", "active",
    "cooling", "heating", "fan_only", "dehumidifying", "drying",
    "running", "playing",
}
OFF_STATES = {"off", "false", "closed", "inactive", "idle", "standby", "paused", "unavailable"}


def classify_transition(old_state: Optional[str], new_state: Optional[str]) -> Optional[str]:
    """Return 'on' if the state change looks like a turn-on, 'off' if turn-off,
    None if we can't tell (e.g. brightness changes on a light that stays on)."""
    if new_state is None:
        return None
    n = new_state.lower()
    o = (old_state or "").lower()

    new_on = n in ON_STATES
    new_off = n in OFF_STATES
    old_on = o in ON_STATES
    old_off = o in OFF_STATES

    if new_on and (old_off or o == "" or not (old_on or old_off)):
        return "on"
    if new_off and (old_on or o == "" or not (old_on or old_off)):
        return "off"
    return None


def record_ha_event(entity_id: str, old_state: Optional[str], new_state: Optional[str],
                    friendly_name: Optional[str] = None, ts: Optional[float] = None) -> dict:
    """Store an HA state-change event and ensure the entity has a row in
    `ha_entities`. If any device is linked to this entity via
    source_entity_id, snap that device's is_on state and write a
    device_state_log entry — HA is authoritative for entity-linked devices,
    so we don't have to wait for (or even require) a coincident Shelly step
    event to update the device state. Returns a summary dict for the API
    response."""
    if ts is None:
        ts = time.time()
    direction = classify_transition(old_state, new_state)

    with cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO ha_entities "
            "(entity_id, friendly_name, first_seen_ts, last_seen_ts) "
            "VALUES (?,?,?,?)",
            (entity_id, friendly_name, ts, ts),
        )
        if friendly_name:
            cur.execute(
                "UPDATE ha_entities SET friendly_name = ?, last_seen_ts = ? WHERE entity_id = ?",
                (friendly_name, ts, entity_id),
            )
        else:
            cur.execute(
                "UPDATE ha_entities SET last_seen_ts = ? WHERE entity_id = ?",
                (ts, entity_id),
            )

        cur.execute(
            "INSERT INTO ha_events (ts, entity_id, old_state, new_state, direction) "
            "VALUES (?,?,?,?,?)",
            (ts, entity_id, old_state, new_state, direction),
        )
        ha_event_id = cur.lastrowid

        # Snap any device linked to this entity to the new state. Without this,
        # entity-linked devices (e.g. an AC tracked by a binary_sensor) stay
        # stuck in their last-correlated state if the Shelly didn't see a clear
        # step at the same moment — manifesting as "device on for hours" with
        # bogus attributed energy.
        if direction in ("on", "off"):
            cur.row_factory = _dict_row
            cur.execute(
                "SELECT id, is_on, mean_power_w FROM devices WHERE source_entity_id = ?",
                (entity_id,),
            )
            linked = cur.fetchall()
            for dev in linked:
                want_on = direction == "on"
                if bool(dev["is_on"]) == want_on:
                    continue
                if want_on:
                    cur.execute(
                        "UPDATE devices SET is_on = 1, last_on_ts = ? WHERE id = ?",
                        (ts, dev["id"]),
                    )
                else:
                    cur.execute(
                        "UPDATE devices SET is_on = 0, last_off_ts = ? WHERE id = ?",
                        (ts, dev["id"]),
                    )
                cur.execute(
                    "INSERT INTO device_state_log (device_id, ts, state, event_id) "
                    "VALUES (?,?,?,NULL)",
                    (dev["id"], ts, direction),
                )
                log.info(
                    "HA-driven state change: device %d -> %s (entity %s)",
                    dev["id"], direction, entity_id,
                )

    return {"id": ha_event_id, "entity_id": entity_id, "direction": direction, "ts": ts}


def _dict_row(cur, row):
    return {col[0]: row[i] for i, col in enumerate(cur.description)}


def correlate_step_event(event_id: int, event: dict) -> Optional[str]:
    """Called whenever a step event lands. Looks for an HA event in the
    correlation window with the matching direction, and if found, updates
    the entity's signature and possibly auto-promotes it to a device.

    Returns the entity_id of the matched HA event, or None.
    """
    direction = event["direction"]
    ts = float(event["ts"])
    window_start = ts - CORRELATION_WINDOW_S
    window_end = ts + CORRELATION_WINDOW_S

    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            """SELECT id, entity_id FROM ha_events
               WHERE ts BETWEEN ? AND ?
                 AND direction = ?
                 AND matched_event_id IS NULL
               ORDER BY ABS(ts - ?) ASC LIMIT 1""",
            (window_start, window_end, direction, ts),
        )
        ha_event = cur.fetchone()
        if ha_event is None:
            return None

        entity_id = ha_event["entity_id"]

        # Link them so the same HA event can't be claimed twice.
        cur.execute(
            "UPDATE ha_events SET matched_event_id = ? WHERE id = ?",
            (event_id, ha_event["id"]),
        )

        # Update running signature on the entity. We use sample sums so we can
        # cheaply compute mean + variance later without storing every sample.
        p = abs(float(event["delta_power"]))
        a = abs(float(event["delta_a_power"]))
        b = abs(float(event["delta_b_power"]))
        c = abs(float(event["delta_c_power"]))
        cur.execute(
            """UPDATE ha_entities SET
                 match_count    = match_count + 1,
                 sum_power_w    = sum_power_w + ?,
                 sum_power_w_sq = sum_power_w_sq + ?,
                 sum_a_power_w  = sum_a_power_w + ?,
                 sum_b_power_w  = sum_b_power_w + ?,
                 sum_c_power_w  = sum_c_power_w + ?,
                 last_seen_ts   = ?
               WHERE entity_id = ?""",
            (p, p * p, a, b, c, ts, entity_id),
        )

        # Check whether to auto-promote.
        cur.execute(
            "SELECT entity_id, friendly_name, match_count, sum_power_w, sum_a_power_w, "
            "sum_b_power_w, sum_c_power_w, promoted_device_id "
            "FROM ha_entities WHERE entity_id = ?",
            (entity_id,),
        )
        entity = cur.fetchone()

        if entity["promoted_device_id"] is None and entity["match_count"] >= PROMOTION_THRESHOLD:
            n = entity["match_count"]
            mean_w = entity["sum_power_w"] / n
            mean_a = entity["sum_a_power_w"] / n
            mean_b = entity["sum_b_power_w"] / n
            mean_c = entity["sum_c_power_w"] / n
            name = entity["friendly_name"] or entity_id

            cur.execute(
                "INSERT INTO devices (name, notes, created_ts, is_on, mean_power_w, "
                "total_energy_wh, source_entity_id) "
                "VALUES (?,?,?,0,?,0,?)",
                (name, f"Auto-promoted from HA entity {entity_id}", ts, mean_w, entity_id),
            )
            device_id = cur.lastrowid

            std_w = max(mean_w * 0.10, 20.0)
            cur.execute(
                """INSERT INTO clusters
                (created_ts, updated_ts, mean_power, std_power,
                 mean_a_power, mean_b_power, mean_c_power, mean_pf, sample_count, device_id)
                VALUES (?,?,?,?,?,?,?,1.0,?,?)""",
                (ts, ts, mean_w, std_w, mean_a, mean_b, mean_c, n, device_id),
            )
            cur.execute(
                """INSERT INTO clusters
                (created_ts, updated_ts, mean_power, std_power,
                 mean_a_power, mean_b_power, mean_c_power, mean_pf, sample_count, device_id)
                VALUES (?,?,?,?,?,?,?,1.0,?,?)""",
                (ts, ts, -mean_w, std_w, -mean_a, -mean_b, -mean_c, n, device_id),
            )
            cur.execute(
                "UPDATE ha_entities SET promoted_device_id = ? WHERE entity_id = ?",
                (device_id, entity_id),
            )
            log.info(
                "Promoted HA entity %s to device %d (%s) at %.0fW after %d matches",
                entity_id, device_id, name, mean_w, n,
            )
            # Mop up any pre-existing unlabeled clusters whose signature
            # matches the new device. Done outside the cursor() block since
            # the helper acquires its own cursor.
            _newly_promoted = True
        else:
            _newly_promoted = False

    if _newly_promoted:
        try:
            from .inference import absorb_unlabeled_clusters
            absorb_unlabeled_clusters()
        except Exception:
            log.exception("Post-promotion cluster absorb failed")

    return entity_id
