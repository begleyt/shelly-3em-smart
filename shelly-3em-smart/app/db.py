import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import settings


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts             REAL PRIMARY KEY,
    a_current      REAL,
    b_current      REAL,
    c_current      REAL,
    a_voltage      REAL,
    b_voltage      REAL,
    c_voltage      REAL,
    a_power        REAL,
    b_power        REAL,
    c_power        REAL,
    a_pf           REAL,
    b_pf           REAL,
    c_pf           REAL,
    total_power    REAL,
    total_current  REAL
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    direction      TEXT NOT NULL,   -- 'on' | 'off'
    delta_power    REAL NOT NULL,
    delta_a_power  REAL NOT NULL,
    delta_b_power  REAL NOT NULL,
    delta_c_power  REAL NOT NULL,
    delta_a_current REAL NOT NULL,
    delta_b_current REAL NOT NULL,
    delta_c_current REAL NOT NULL,
    pf_after       REAL,
    cluster_id     INTEGER,
    device_id      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_cluster ON events(cluster_id);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id);

CREATE TABLE IF NOT EXISTS clusters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts     REAL NOT NULL,
    updated_ts     REAL NOT NULL,
    mean_power     REAL NOT NULL,
    std_power      REAL NOT NULL,
    mean_a_power   REAL NOT NULL,
    mean_b_power   REAL NOT NULL,
    mean_c_power   REAL NOT NULL,
    mean_pf        REAL,
    sample_count   INTEGER NOT NULL,
    device_id      INTEGER
);

CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    notes            TEXT,
    created_ts       REAL NOT NULL,
    is_on            INTEGER NOT NULL DEFAULT 0,
    last_on_ts       REAL,
    last_off_ts      REAL,
    mean_power_w     REAL NOT NULL DEFAULT 0,
    total_energy_wh  REAL NOT NULL DEFAULT 0,
    source_entity_id TEXT
);

-- Raw HA state-change events posted by the HACS integration. Kept for
-- correlation against step events and as a debug log.
CREATE TABLE IF NOT EXISTS ha_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    entity_id   TEXT NOT NULL,
    old_state   TEXT,
    new_state   TEXT,
    direction   TEXT,           -- 'on' | 'off' | NULL (unknown)
    matched_event_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ha_events_ts ON ha_events(ts);
CREATE INDEX IF NOT EXISTS idx_ha_events_entity_ts ON ha_events(entity_id, ts);

-- Accumulated per-entity power signature, built up from correlated step events.
-- When `match_count` >= the promotion threshold, the entity is auto-created as
-- a labelled device.
CREATE TABLE IF NOT EXISTS ha_entities (
    entity_id        TEXT PRIMARY KEY,
    friendly_name    TEXT,
    first_seen_ts    REAL NOT NULL,
    last_seen_ts     REAL NOT NULL,
    match_count      INTEGER NOT NULL DEFAULT 0,
    sum_power_w      REAL NOT NULL DEFAULT 0,
    sum_power_w_sq   REAL NOT NULL DEFAULT 0,
    sum_a_power_w    REAL NOT NULL DEFAULT 0,
    sum_b_power_w    REAL NOT NULL DEFAULT 0,
    sum_c_power_w    REAL NOT NULL DEFAULT 0,
    promoted_device_id INTEGER
);

CREATE TABLE IF NOT EXISTS device_state_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      INTEGER NOT NULL,
    ts             REAL NOT NULL,
    state          TEXT NOT NULL,   -- 'on' | 'off'
    event_id       INTEGER,
    FOREIGN KEY (device_id) REFERENCES devices(id)
);

CREATE INDEX IF NOT EXISTS idx_device_state_log_device ON device_state_log(device_id, ts);
"""


def init_db() -> None:
    global _conn
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.executescript(SCHEMA)
    _migrate(_conn)


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns / backfill for upgrades from older databases."""
    cur = c.execute("PRAGMA table_info(devices)")
    cols = {row[1] for row in cur.fetchall()}

    if "mean_power_w" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN mean_power_w REAL NOT NULL DEFAULT 0")
        c.execute(
            """UPDATE devices
               SET mean_power_w = COALESCE(
                   (SELECT ABS(c.mean_power) FROM clusters c
                    WHERE c.device_id = devices.id
                    ORDER BY ABS(c.mean_power) DESC LIMIT 1),
                   0
               )
               WHERE mean_power_w = 0"""
        )

    if "total_energy_wh" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN total_energy_wh REAL NOT NULL DEFAULT 0")

    if "source_entity_id" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN source_entity_id TEXT")


def conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    with _lock:
        cur = conn().cursor()
        try:
            yield cur
        finally:
            cur.close()


def insert_sample(sample: dict) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO samples
            (ts, a_current, b_current, c_current, a_voltage, b_voltage, c_voltage,
             a_power, b_power, c_power, a_pf, b_pf, c_pf, total_power, total_current)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sample["ts"],
                sample.get("a_current"), sample.get("b_current"), sample.get("c_current"),
                sample.get("a_voltage"), sample.get("b_voltage"), sample.get("c_voltage"),
                sample.get("a_power"), sample.get("b_power"), sample.get("c_power"),
                sample.get("a_pf"), sample.get("b_pf"), sample.get("c_pf"),
                sample.get("total_power"), sample.get("total_current"),
            ),
        )


def insert_event(event: dict) -> int:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO events
            (ts, direction, delta_power,
             delta_a_power, delta_b_power, delta_c_power,
             delta_a_current, delta_b_current, delta_c_current, pf_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event["ts"], event["direction"], event["delta_power"],
                event["delta_a_power"], event["delta_b_power"], event["delta_c_power"],
                event["delta_a_current"], event["delta_b_current"], event["delta_c_current"],
                event.get("pf_after"),
            ),
        )
        return int(cur.lastrowid)


def prune_old_samples() -> int:
    cutoff = time.time() - (settings.sample_retention_days * 86400)
    with cursor() as cur:
        cur.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        return cur.rowcount
