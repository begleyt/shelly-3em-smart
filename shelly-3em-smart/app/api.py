import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .clusterer import run_clustering
from .config import settings
from .db import cursor
from .ha_correlator import record_ha_event
from .mqtt_publisher import publisher
from .state import state

router = APIRouter()


# ---------- info (used by the HACS integration to self-configure) ----------

@router.get("/api/info")
def info():
    return {
        "version": "0.2.0",
        "shelly_host": settings.shelly_host,
        "channel_a_label": settings.channel_a_label,
        "channel_b_label": settings.channel_b_label,
        "channel_c_label": settings.channel_c_label,
        "mqtt_enabled": settings.mqtt_enabled,
        "supports_ha_events": True,
    }


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
def list_cluster_pairs():
    """Group unlabelled clusters into probable appliances (a start-cluster
    paired with its matching stop-cluster) plus orphans that didn't find a
    confident pair."""
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
            "INSERT INTO devices (name, notes, created_ts, is_on, mean_power_w, total_energy_wh) "
            "VALUES (?,?,?,0,?,0)",
            (body.name, body.notes, now, body.power_w),
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
    return {
        "id": device_id,
        "name": body.name,
        "mean_power_w": body.power_w,
        "matched_history_events": matched_count,
    }


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/api/devices/{device_id}")
def update_device(device_id: int, body: DeviceUpdate):
    fields = []
    values = []
    if body.name is not None:
        fields.append("name = ?")
        values.append(body.name)
    if body.notes is not None:
        fields.append("notes = ?")
        values.append(body.notes)
    if not fields:
        return {"ok": True}
    values.append(device_id)
    with cursor() as cur:
        cur.execute(f"UPDATE devices SET {', '.join(fields)} WHERE id = ?", values)
    return {"ok": True}


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
