import logging
import math
import time
from typing import Optional

from .db import cursor

log = logging.getLogger(__name__)

# Reject an off-event that comes too quickly after the on-event — almost
# certainly an unrelated load being mis-attributed.
MIN_ON_DURATION_S = 30.0

# When two devices match an event with similar scores, prefer the one whose
# historical timing matches the current hour-of-day. This boost (smaller is
# stronger) divides the raw score before comparison.
TIME_OF_DAY_BOOST = 0.6   # up to 40% score reduction
TIME_OF_DAY_MIN_SAMPLES = 5

# Cache: device_id -> {hour: fraction-of-historical-events-at-this-hour}
_time_pattern_cache: dict[int, dict[int, float]] = {}
_time_pattern_cache_ts: float = 0.0
_TIME_PATTERN_CACHE_TTL_S = 600.0


def _refresh_time_patterns() -> None:
    """Compute hour-of-day distributions for each device from device_state_log.

    Refreshed lazily every TIME_PATTERN_CACHE_TTL_S so the matcher gets faster
    awareness of new patterns as devices accumulate history, without doing the
    SQL on every event.
    """
    global _time_pattern_cache, _time_pattern_cache_ts
    now = time.time()
    if now - _time_pattern_cache_ts < _TIME_PATTERN_CACHE_TTL_S and _time_pattern_cache:
        return
    cutoff = now - 30 * 86400  # last 30 days
    counts: dict[int, dict[int, int]] = {}
    totals: dict[int, int] = {}
    with cursor() as cur:
        cur.execute(
            "SELECT device_id, ts FROM device_state_log "
            "WHERE state = 'on' AND ts >= ?",
            (cutoff,),
        )
        for device_id, ts in cur.fetchall():
            hour = int(time.localtime(ts).tm_hour)
            d = counts.setdefault(device_id, {})
            d[hour] = d.get(hour, 0) + 1
            totals[device_id] = totals.get(device_id, 0) + 1
    new_cache: dict[int, dict[int, float]] = {}
    for device_id, hourly in counts.items():
        total = totals.get(device_id, 0)
        if total < TIME_OF_DAY_MIN_SAMPLES:
            continue
        new_cache[device_id] = {h: c / total for h, c in hourly.items()}
    _time_pattern_cache = new_cache
    _time_pattern_cache_ts = now


def _time_of_day_score(device_id: int, ts: float) -> float:
    """Return a multiplier <= 1.0 to apply to the match score (smaller wins).
    Returns 1.0 if we have no pattern for this device yet."""
    _refresh_time_patterns()
    pat = _time_pattern_cache.get(device_id)
    if not pat:
        return 1.0
    hour = int(time.localtime(ts).tm_hour)
    # Use this hour plus the two adjacent for some smoothing.
    weight = pat.get(hour, 0.0) + 0.5 * pat.get((hour - 1) % 24, 0.0) + 0.5 * pat.get((hour + 1) % 24, 0.0)
    weight = min(1.0, weight)
    # weight 0 → multiplier 1.0; weight 1.0 → multiplier TIME_OF_DAY_BOOST.
    return 1.0 - (1.0 - TIME_OF_DAY_BOOST) * weight


def _dict_row(cursor_, row):
    return {col[0]: row[i] for i, col in enumerate(cursor_.description)}


def match_event_to_device(event_id: int, event: dict) -> Optional[int]:
    """Find the labelled cluster whose signature best matches this event.

    Returns the device_id if a match is found within tolerance, else None.
    Devices flagged is_continuous are skipped entirely — their state is
    set by the user, not by step-event matching.
    """
    direction = event["direction"]
    sign = 1 if direction == "on" else -1

    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            """SELECT c.id AS cluster_id, c.device_id, c.mean_power, c.std_power,
                      c.mean_a_power, c.mean_b_power, c.mean_c_power,
                      d.is_continuous, d.is_on, d.last_on_ts
               FROM clusters c
               JOIN devices d ON d.id = c.device_id
               WHERE c.device_id IS NOT NULL
                 AND c.mean_power * ? > 0
                 AND d.is_continuous = 0""",
            (sign,),
        )
        candidates = cur.fetchall()

    if not candidates:
        return None

    best = None
    best_score = math.inf
    for c in candidates:
        raw = (
            (abs(event["delta_power"]) - abs(c["mean_power"])) ** 2 * 1.0
            + (abs(event["delta_a_power"]) - abs(c["mean_a_power"])) ** 2 * 0.5
            + (abs(event["delta_b_power"]) - abs(c["mean_b_power"])) ** 2 * 0.5
            + (abs(event["delta_c_power"]) - abs(c["mean_c_power"])) ** 2 * 0.5
        )
        # Time-of-day awareness: a device whose historical cycles match the
        # current hour gets a lower (better) score, breaking ties between
        # devices with similar wattage.
        score = raw * _time_of_day_score(int(c["device_id"]), float(event["ts"]))
        # Tolerance still scales with the cluster's own spread, with a floor.
        tol = max(c["std_power"], 25.0) ** 2 * 4.0
        if score < best_score and score < tol:
            best_score = score
            best = c

    if best is None:
        return None

    device_id = int(best["device_id"])
    cluster_id = int(best["cluster_id"])

    # Reject too-soon off-events: a real device being on for <30s and then
    # off is rare; almost always this is a different load whose signature
    # happens to match.
    if direction == "off" and best.get("is_on") and best.get("last_on_ts"):
        on_duration = float(event["ts"]) - float(best["last_on_ts"])
        if on_duration < MIN_ON_DURATION_S:
            log.info(
                "Rejecting off-event match for device %d: only %.1fs since on (min %.0fs)",
                device_id, on_duration, MIN_ON_DURATION_S,
            )
            return None

    with cursor() as cur:
        cur.execute(
            "UPDATE events SET cluster_id = ?, device_id = ? WHERE id = ?",
            (cluster_id, device_id, event_id),
        )
        cur.execute(
            "INSERT INTO device_state_log (device_id, ts, state, event_id) VALUES (?,?,?,?)",
            (device_id, event["ts"], direction, event_id),
        )

        if direction == "on":
            # Only transition off→on. If already on, leave state alone so we
            # don't lose the original last_on_ts and the energy accumulated
            # against it.
            cur.execute(
                "UPDATE devices SET is_on = 1, last_on_ts = ? WHERE id = ? AND is_on = 0",
                (event["ts"], device_id),
            )
        else:
            # Transition on→off: tally the energy used during this on-period.
            cur.execute(
                "SELECT is_on, last_on_ts, mean_power_w, total_energy_wh "
                "FROM devices WHERE id = ?",
                (device_id,),
            )
            row = cur.fetchone()
            if row and row[0] and row[1]:
                elapsed_s = max(0.0, event["ts"] - row[1])
                mean_w = row[2] or 0.0
                energy_wh = mean_w * elapsed_s / 3600.0
                cur.execute(
                    "UPDATE devices SET is_on = 0, last_off_ts = ?, "
                    "total_energy_wh = COALESCE(total_energy_wh, 0) + ? "
                    "WHERE id = ?",
                    (event["ts"], energy_wh, device_id),
                )
                log.info(
                    "Device %d off after %.0fs at %.0fW: +%.1f Wh",
                    device_id, elapsed_s, mean_w, energy_wh,
                )
            else:
                cur.execute(
                    "UPDATE devices SET is_on = 0, last_off_ts = ? WHERE id = ?",
                    (event["ts"], device_id),
                )

    log.info("Event %d matched device %d (%s)", event_id, device_id, direction)
    return device_id


def absorb_unlabeled_clusters() -> dict:
    """Sweep every unlabeled cluster and attach it to an existing device whose
    signature matches within tolerance. Called after HA promotions, manual
    device creation, and at the end of each periodic clusterer pass — so the
    Unlabeled Clusters tab stops showing things that are clearly the same as
    an already-labelled appliance.
    """
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            """SELECT d.id AS device_id, d.mean_power_w,
                      MAX(CASE WHEN c.mean_power > 0 THEN c.mean_a_power ELSE -c.mean_a_power END) AS mean_a_power,
                      MAX(CASE WHEN c.mean_power > 0 THEN c.mean_b_power ELSE -c.mean_b_power END) AS mean_b_power,
                      MAX(CASE WHEN c.mean_power > 0 THEN c.mean_c_power ELSE -c.mean_c_power END) AS mean_c_power,
                      AVG(c.std_power) AS std_power
               FROM devices d
               JOIN clusters c ON c.device_id = d.id
               GROUP BY d.id, d.mean_power_w"""
        )
        devices = cur.fetchall()
        if not devices:
            return {"absorbed": 0, "linked_events": 0}

        cur.execute(
            """SELECT id, mean_power, mean_a_power, mean_b_power, mean_c_power
               FROM clusters WHERE device_id IS NULL"""
        )
        candidates = cur.fetchall()
        if not candidates:
            return {"absorbed": 0, "linked_events": 0}

        absorbed_ids: list[tuple[int, int]] = []  # (cluster_id, device_id)
        for cand in candidates:
            best_dev = None
            best_score = math.inf
            for dev in devices:
                target_mp = float(dev["mean_power_w"] or 0.0)
                if target_mp <= 0:
                    continue
                # Match purely on magnitude (sign just determines direction).
                score = (
                    (abs(cand["mean_power"]) - target_mp) ** 2
                    + (abs(cand["mean_a_power"]) - abs(dev["mean_a_power"] or 0.0)) ** 2 * 0.5
                    + (abs(cand["mean_b_power"]) - abs(dev["mean_b_power"] or 0.0)) ** 2 * 0.5
                    + (abs(cand["mean_c_power"]) - abs(dev["mean_c_power"] or 0.0)) ** 2 * 0.5
                )
                tol = max(target_mp * 0.25, 50.0) ** 2 * 2.0
                if score < best_score and score < tol:
                    best_score = score
                    best_dev = dev
            if best_dev:
                absorbed_ids.append((int(cand["id"]), int(best_dev["device_id"])))

        for cluster_id, device_id in absorbed_ids:
            cur.execute(
                "UPDATE clusters SET device_id = ? WHERE id = ?",
                (device_id, cluster_id),
            )
            cur.execute(
                "UPDATE events SET device_id = ? WHERE cluster_id = ? AND device_id IS NULL",
                (device_id, cluster_id),
            )

        # Count linked events for reporting.
        linked = 0
        if absorbed_ids:
            cluster_ids = [str(c) for c, _ in absorbed_ids]
            cur.execute(
                f"SELECT COUNT(*) AS n FROM events WHERE cluster_id IN ({','.join(cluster_ids)})"
            )
            linked = int(cur.fetchone()["n"])

        if absorbed_ids:
            log.info(
                "Absorbed %d unlabeled clusters into existing devices (%d events linked)",
                len(absorbed_ids), linked,
            )
        return {"absorbed": len(absorbed_ids), "linked_events": linked}
