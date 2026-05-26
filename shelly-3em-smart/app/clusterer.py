import asyncio
import logging
import math
import time
from typing import List, Optional

import numpy as np
from sklearn.cluster import DBSCAN

from .config import settings
from .db import cursor

log = logging.getLogger(__name__)


def _feature_row(event_row) -> List[float]:
    """Convert an events-table row into a feature vector for clustering.

    Sign of delta_power encodes on/off; clustering is done separately for
    on-events and off-events so an 'on' isn't grouped with its matching 'off'.
    """
    return [
        abs(event_row["delta_power"]),
        abs(event_row["delta_a_power"]),
        abs(event_row["delta_b_power"]),
        abs(event_row["delta_c_power"]),
        event_row["pf_after"] or 0.0,
    ]


def _fetch_events(direction: str):
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT id, ts, direction, delta_power, delta_a_power, delta_b_power, "
            "delta_c_power, delta_a_current, delta_b_current, delta_c_current, "
            "pf_after, cluster_id, device_id "
            "FROM events WHERE direction = ? ORDER BY ts",
            (direction,),
        )
        return cur.fetchall()


def _dict_row(cursor_, row):
    return {col[0]: row[i] for i, col in enumerate(cursor_.description)}


def _update_event_clusters(event_ids: List[int], cluster_ids: List[Optional[int]]) -> None:
    with cursor() as cur:
        for eid, cid in zip(event_ids, cluster_ids):
            cur.execute("UPDATE events SET cluster_id = ? WHERE id = ?", (cid, eid))


def _replace_clusters(direction: str, cluster_rows: List[dict]) -> None:
    """Delete unassigned (no device_id) clusters of this direction and reinsert fresh ones.

    Clusters that already have a device assigned are kept so labelling persists.
    """
    now = time.time()
    with cursor() as cur:
        # We tag direction onto the cluster via mean_power sign convention:
        # on-clusters have positive mean_power, off-clusters negative.
        sign = 1 if direction == "on" else -1
        cur.execute(
            "DELETE FROM clusters WHERE device_id IS NULL AND mean_power * ? > 0",
            (sign,),
        )
        for row in cluster_rows:
            cur.execute(
                """INSERT INTO clusters
                (created_ts, updated_ts, mean_power, std_power,
                 mean_a_power, mean_b_power, mean_c_power, mean_pf, sample_count, device_id)
                VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    now, now,
                    row["mean_power"] * sign, row["std_power"],
                    row["mean_a_power"] * sign, row["mean_b_power"] * sign, row["mean_c_power"] * sign,
                    row["mean_pf"], row["sample_count"],
                ),
            )


def _run_one_direction(direction: str) -> int:
    events = _fetch_events(direction)
    if len(events) < settings.cluster_min_samples:
        return 0

    feats = np.array([_feature_row(e) for e in events], dtype=float)
    # Weight delta_power more than the per-phase or PF dims so clusters track magnitude.
    scale = np.array([1.0, 0.5, 0.5, 0.5, 20.0])
    X = feats * scale
    db = DBSCAN(eps=settings.cluster_eps_w, min_samples=settings.cluster_min_samples).fit(X)
    labels = db.labels_

    # Map sklearn cluster label -> persistent cluster row id
    unique = sorted({l for l in labels if l != -1})
    cluster_rows = []
    label_to_idx: dict = {}
    for label in unique:
        mask = labels == label
        members = feats[mask]
        cluster_rows.append({
            "mean_power": float(members[:, 0].mean()),
            "std_power": float(members[:, 0].std()) if members.shape[0] > 1 else 0.0,
            "mean_a_power": float(members[:, 1].mean()),
            "mean_b_power": float(members[:, 2].mean()),
            "mean_c_power": float(members[:, 3].mean()),
            "mean_pf": float(members[:, 4].mean()),
            "sample_count": int(mask.sum()),
        })
        label_to_idx[label] = len(cluster_rows) - 1

    # Persist clusters and figure out their assigned IDs
    _replace_clusters(direction, cluster_rows)
    sign = 1 if direction == "on" else -1
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute(
            "SELECT id, mean_power FROM clusters WHERE device_id IS NULL AND mean_power * ? > 0 ORDER BY id",
            (sign,),
        )
        new_clusters = cur.fetchall()

    # new_clusters is ordered by id; we inserted in the same order as cluster_rows
    new_cluster_ids = [c["id"] for c in new_clusters[-len(cluster_rows):]]

    # Update event rows with their cluster_id (or NULL for noise points)
    event_ids = [e["id"] for e in events]
    new_event_cluster_ids: List[Optional[int]] = []
    for label in labels:
        if label == -1:
            new_event_cluster_ids.append(None)
        else:
            new_event_cluster_ids.append(new_cluster_ids[label_to_idx[label]])
    _update_event_clusters(event_ids, new_event_cluster_ids)

    return len(cluster_rows)


def run_clustering() -> dict:
    on_count = _run_one_direction("on")
    off_count = _run_one_direction("off")
    log.info("Clustering: %d on-clusters, %d off-clusters", on_count, off_count)
    return {"on": on_count, "off": off_count, "ts": time.time()}


async def cluster_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_clustering)
        except Exception:
            log.exception("Clustering failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.cluster_interval_s)
        except asyncio.TimeoutError:
            pass
