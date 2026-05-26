import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .clusterer import run_clustering
from .config import settings
from .db import cursor
from .mqtt_publisher import publisher
from .state import state

router = APIRouter()


# ---------- info (used by the HACS integration to self-configure) ----------

@router.get("/api/info")
def info():
    return {
        "version": "0.1.6",
        "shelly_host": settings.shelly_host,
        "channel_a_label": settings.channel_a_label,
        "channel_b_label": settings.channel_b_label,
        "channel_c_label": settings.channel_c_label,
        "mqtt_enabled": settings.mqtt_enabled,
    }


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


class DeviceCreate(BaseModel):
    name: str
    notes: Optional[str] = None
    cluster_id: int


@router.post("/api/devices")
def create_device(body: DeviceCreate):
    with cursor() as cur:
        cur.row_factory = _dict_row
        cur.execute("SELECT ABS(mean_power) AS mean_power_w FROM clusters WHERE id = ?", (body.cluster_id,))
        c = cur.fetchone()
        mean_power_w = float(c["mean_power_w"]) if c else 0.0
        cur.execute(
            "INSERT INTO devices (name, notes, created_ts, is_on, mean_power_w, total_energy_wh) "
            "VALUES (?,?,?,0,?,0)",
            (body.name, body.notes, time.time(), mean_power_w),
        )
        device_id = cur.lastrowid
        cur.execute(
            "UPDATE clusters SET device_id = ? WHERE id = ?",
            (device_id, body.cluster_id),
        )
        cur.execute(
            "UPDATE events SET device_id = ? WHERE cluster_id = ?",
            (device_id, body.cluster_id),
        )
    publisher.publish_device_discovery(device_id, body.name)
    return {"id": device_id, "name": body.name, "cluster_id": body.cluster_id, "mean_power_w": mean_power_w}


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
