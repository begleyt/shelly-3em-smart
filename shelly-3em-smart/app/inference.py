import logging
import math
import time
from typing import Optional

from .db import cursor

log = logging.getLogger(__name__)


def _dict_row(cursor_, row):
    return {col[0]: row[i] for i, col in enumerate(cursor_.description)}


def match_event_to_device(event_id: int, event: dict) -> Optional[int]:
    """Find the labelled cluster whose signature best matches this event.

    Returns the device_id if a match is found within tolerance, else None.
    """
    direction = event["direction"]
    sign = 1 if direction == "on" else -1

    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            """SELECT c.id AS cluster_id, c.device_id, c.mean_power, c.std_power,
                      c.mean_a_power, c.mean_b_power, c.mean_c_power
               FROM clusters c
               WHERE c.device_id IS NOT NULL AND c.mean_power * ? > 0""",
            (sign,),
        )
        candidates = cur.fetchall()

    if not candidates:
        return None

    best = None
    best_score = math.inf
    for c in candidates:
        score = (
            (abs(event["delta_power"]) - abs(c["mean_power"])) ** 2 * 1.0
            + (abs(event["delta_a_power"]) - abs(c["mean_a_power"])) ** 2 * 0.5
            + (abs(event["delta_b_power"]) - abs(c["mean_b_power"])) ** 2 * 0.5
            + (abs(event["delta_c_power"]) - abs(c["mean_c_power"])) ** 2 * 0.5
        )
        # Tolerance scales with the cluster's own spread, with a floor.
        tol = max(c["std_power"], 25.0) ** 2 * 4.0
        if score < best_score and score < tol:
            best_score = score
            best = c

    if best is None:
        return None

    device_id = int(best["device_id"])
    cluster_id = int(best["cluster_id"])

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
            cur.execute(
                "UPDATE devices SET is_on = 1, last_on_ts = ? WHERE id = ?",
                (event["ts"], device_id),
            )
        else:
            cur.execute(
                "UPDATE devices SET is_on = 0, last_off_ts = ? WHERE id = ?",
                (event["ts"], device_id),
            )

    log.info("Event %d matched device %d (%s)", event_id, device_id, direction)
    return device_id
