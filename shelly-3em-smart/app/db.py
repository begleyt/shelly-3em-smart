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
    source_entity_id TEXT,
    is_continuous    INTEGER NOT NULL DEFAULT 0,
    energy_source    TEXT NOT NULL DEFAULT 'inferred',  -- 'inferred' or 'metered'
    is_hvac          INTEGER NOT NULL DEFAULT 0
);

-- HA-reported cumulative energy readings (e.g. smart plug kWh sensors).
-- baseline_energy_kwh tracks the cumulative value at the start of today
-- so we can compute today's delta without storing every reading.
CREATE TABLE IF NOT EXISTS ha_energy_sources (
    entity_id            TEXT PRIMARY KEY,
    friendly_name        TEXT,
    device_id            INTEGER,
    baseline_energy_kwh  REAL,
    baseline_ts          REAL,
    latest_energy_kwh    REAL,
    latest_power_w       REAL,
    latest_ts            REAL,
    first_seen_ts        REAL
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

-- Outside-temperature samples pushed by the HACS integration from the user's
-- HA weather.* or sensor.* entity. Stored in Fahrenheit (CDD/HDD convention);
-- the UI converts on display per the user's unit preference.
CREATE TABLE IF NOT EXISTS weather_samples (
    ts          REAL PRIMARY KEY,
    temp_f      REAL,
    humidity    REAL,
    condition   TEXT,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_weather_samples_ts ON weather_samples(ts);

-- One row per local-calendar day, rebuilt nightly. Holds the panel-wide kWh,
-- optional HVAC-device kWh, daily temperature stats, and HDD/CDD totals.
-- date_str is local-time YYYY-MM-DD; day_start_ts is its midnight in epoch s.
CREATE TABLE IF NOT EXISTS daily_rollups (
    date_str        TEXT PRIMARY KEY,
    day_start_ts    REAL NOT NULL,
    panel_wh        REAL NOT NULL DEFAULT 0,
    hvac_wh         REAL,
    avg_temp_f      REAL,
    min_temp_f      REAL,
    max_temp_f      REAL,
    hdd             REAL,
    cdd             REAL,
    base_temp_f     REAL,
    sample_count    INTEGER,
    rolled_up_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_rollups_day_start ON daily_rollups(day_start_ts);

-- Simple key-value bag for user prefs set from the UI (e.g. which device is
-- the HVAC target). Avoids hardcoding device IDs in add-on options.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Cumulative natural-gas (or propane / oil) usage reported by a HA sensor.*
-- entity. Normalised to therms on ingest (1 therm ≈ 29.3 kWh thermal) so the
-- daily rollup math is consistent regardless of HA's raw unit.
CREATE TABLE IF NOT EXISTS gas_samples (
    ts                REAL PRIMARY KEY,
    cumulative_therms REAL,
    raw_value         REAL,
    raw_unit          TEXT,
    source            TEXT
);
CREATE INDEX IF NOT EXISTS idx_gas_samples_ts ON gas_samples(ts);

-- Setpoint + current-temp time series from one or more HA climate.* entities.
-- target_temp_f is the single setpoint; target_low_f / target_high_f populated
-- for dual-setpoint heat_cool mode. hvac_mode is the user-selected mode
-- (heat, cool, heat_cool, off, auto); hvac_action is what the unit is actually
-- doing right now (heating, cooling, fan, idle, off).
CREATE TABLE IF NOT EXISTS setpoint_samples (
    ts             REAL,
    entity_id      TEXT,
    target_temp_f  REAL,
    target_low_f   REAL,
    target_high_f  REAL,
    current_temp_f REAL,
    hvac_mode      TEXT,
    hvac_action    TEXT,
    PRIMARY KEY (ts, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_setpoint_samples_entity_ts ON setpoint_samples(entity_id, ts);

-- Forecast high/low for the current local day, refreshed periodically by the
-- HACS integration. One row, replaced each push, so the dashboard can show
-- meaningful day H/L early in the morning before the empirical min/max has
-- built up. Empirical observations override these once they're outside the
-- forecast range (real beats predicted).
CREATE TABLE IF NOT EXISTS weather_forecast_today (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    date_str      TEXT NOT NULL,
    forecast_high_f REAL,
    forecast_low_f  REAL,
    ts            REAL NOT NULL
);

-- Multi-day forecast for the upcoming week (or however many days the user's
-- HA weather entity exposes). One row per date_str; refreshed on each poll.
-- Drives the "tomorrow / next week" energy prediction card on Insights.
CREATE TABLE IF NOT EXISTS weather_forecast_daily (
    date_str        TEXT PRIMARY KEY,
    forecast_high_f REAL,
    forecast_low_f  REAL,
    forecast_avg_f  REAL,         -- derived: (high + low) / 2
    condition       TEXT,
    refreshed_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weather_forecast_daily_date ON weather_forecast_daily(date_str);
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

    if "is_continuous" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN is_continuous INTEGER NOT NULL DEFAULT 0")

    if "energy_source" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN energy_source TEXT NOT NULL DEFAULT 'inferred'")

    if "is_hvac" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN is_hvac INTEGER NOT NULL DEFAULT 0")

    if "hvac_role" not in cols:
        # 'cooling' | 'heating' | NULL. Supersedes is_hvac. Backfill: any
        # legacy is_hvac=1 device is treated as cooling (matches the 0.6.0
        # use case where users tagged their AC).
        c.execute("ALTER TABLE devices ADD COLUMN hvac_role TEXT")
        c.execute("UPDATE devices SET hvac_role = 'cooling' WHERE is_hvac = 1 AND hvac_role IS NULL")

    # Extend daily_rollups with per-role energy + gas + setpoint averages.
    cur = c.execute("PRAGMA table_info(daily_rollups)")
    rcols = {row[1] for row in cur.fetchall()}
    for col, ddl in (
        ("cooling_wh",           "ALTER TABLE daily_rollups ADD COLUMN cooling_wh REAL"),
        ("heating_wh",           "ALTER TABLE daily_rollups ADD COLUMN heating_wh REAL"),
        ("heating_therms",       "ALTER TABLE daily_rollups ADD COLUMN heating_therms REAL"),
        ("avg_cool_setpoint_f",  "ALTER TABLE daily_rollups ADD COLUMN avg_cool_setpoint_f REAL"),
        ("avg_heat_setpoint_f",  "ALTER TABLE daily_rollups ADD COLUMN avg_heat_setpoint_f REAL"),
        ("forecast_high_f",      "ALTER TABLE daily_rollups ADD COLUMN forecast_high_f REAL"),
        ("forecast_low_f",       "ALTER TABLE daily_rollups ADD COLUMN forecast_low_f REAL"),
    ):
        if col not in rcols:
            c.execute(ddl)


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
