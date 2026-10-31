"""SQLite storage. One file, versioned migrations via PRAGMA user_version."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Each entry is one migration; append, never edit.
MIGRATIONS = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    -- Latest raw value of every field the API returns, with the car's own timestamp.
    CREATE TABLE state (
        key TEXT PRIMARY KEY,
        value TEXT,
        unit TEXT,
        ts REAL,
        fetched_at REAL NOT NULL
    );

    -- The fields trip/charge detection depends on. A row is written only when
    -- something changed, so this is a log of what the car reported and when.
    -- Trips and charges are derived from it and can be rebuilt at any time.
    CREATE TABLE observations (
        id INTEGER PRIMARY KEY,
        t REAL NOT NULL,
        soc REAL, soc_ts REAL, range_km REAL,
        charging_status TEXT, charge_ts REAL, connection TEXT, charge_type TEXT,
        power_w REAL, target_soc REAL, mins_to_target REAL,
        engine TEXT, engine_ts REAL,
        availability TEXT, availability_ts REAL,
        odometer_km REAL, odometer_ts REAL,
        trip_auto_km REAL, avg_speed_auto REAL, stats_ts REAL,
        lat REAL, lon REAL, location_ts REAL
    );
    CREATE INDEX observations_t ON observations (t);

    CREATE TABLE trips (
        id INTEGER PRIMARY KEY,
        started_at REAL NOT NULL,
        ended_at REAL,
        start_estimated INTEGER NOT NULL DEFAULT 0,
        odo_start REAL, odo_end REAL, ta_start REAL, ta_end REAL,
        distance_km REAL,
        soc_start REAL, soc_end REAL,
        energy_kwh REAL, efficiency REAL, avg_speed_kmh REAL,
        start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
        start_place TEXT, end_place TEXT,
        temp_c REAL,
        note TEXT
    );
    CREATE INDEX trips_started ON trips (started_at);

    CREATE TABLE trip_points (
        trip_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL
    );
    CREATE INDEX trip_points_trip ON trip_points (trip_id, ts);

    CREATE TABLE charges (
        id INTEGER PRIMARY KEY,
        started_at REAL NOT NULL,
        ended_at REAL,
        soc_start REAL, soc_end REAL, target_soc REAL,
        charge_type TEXT,
        energy_soc_kwh REAL, energy_metered_kwh REAL, metered_coverage REAL,
        energy_kwh REAL, energy_source TEXT,
        max_power_kw REAL, avg_power_kw REAL,
        lat REAL, lon REAL, odometer_km REAL,
        place_id INTEGER, place_name TEXT,
        price_per_kwh REAL,
        cost_override REAL, energy_override REAL,
        note TEXT
    );
    CREATE INDEX charges_started ON charges (started_at);

    CREATE TABLE charge_samples (
        charge_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        power_w REAL,
        soc REAL
    );
    CREATE INDEX charge_samples_charge ON charge_samples (charge_id, ts);

    CREATE TABLE places (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        radius_m REAL NOT NULL DEFAULT 150,
        price_per_kwh REAL
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        type TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX events_ts ON events (ts);

    CREATE TABLE api_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);

    CREATE TABLE geocache (
        key TEXT PRIMARY KEY,
        name TEXT
    );

    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        expires_at REAL NOT NULL
    );
    """,
    """
    -- Every answer from Volvo's API, zlib-compressed, stored when it differs
    -- from the previous answer for the same endpoint. The source of truth:
    -- observations, trips and charges can all be rebuilt from it.
    CREATE TABLE raw_responses (
        id INTEGER PRIMARY KEY,
        fetched_at REAL NOT NULL,
        method TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        status INTEGER NOT NULL,
        hash TEXT NOT NULL,
        body BLOB NOT NULL
    );
    CREATE INDEX raw_responses_endpoint ON raw_responses (endpoint, fetched_at);
    CREATE INDEX raw_responses_fetched ON raw_responses (fetched_at);
    """,
]


class DB:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        if self.path != ":memory:":
            # Holds API credentials and location history.
            Path(self.path).chmod(0o600)

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={i};\nCOMMIT;")

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, *args: Any) -> int:
        """Execute a write; returns lastrowid."""
        return self.conn.execute(sql, args).lastrowid or 0

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.x(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *values.values())

    def update(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        self.x(f"UPDATE {table} SET {sets} WHERE id = ?", *values.values(), row_id)

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            key,
            json.dumps(value),
        )

    def delete(self, key: str) -> None:
        self.x("DELETE FROM meta WHERE key = ?", key)


DEFAULT_SETTINGS: dict[str, Any] = {
    "vehicle_name": "",
    "units": "km",
    "currency": "€",
    "timezone": "UTC",
    # None means "use what the Volvo API reports for this car".
    "usable_capacity_kwh": None,
    "default_price_per_kwh": None,
    "idle_interval_s": 300,
    "active_interval_s": 60,
    # Stops shorter than this are stitched into one trip.
    "merge_gap_s": 300,
    # Sends coordinates to OpenStreetMap (addresses) and Open-Meteo (temperature).
    "lookups_enabled": True,
    "webhook_url": "",
    "webhook_events": ["trip_ended", "charge_started", "charge_ended", "needs_reauth"],
    "mqtt_enabled": False,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_tls": False,
    "mqtt_topic_prefix": "volvodash",
    "mqtt_discovery_prefix": "homeassistant",
}

# Settings that are stored but never sent back to the browser.
SECRET_SETTINGS = {"mqtt_password"}


def get_settings(db: DB) -> dict[str, Any]:
    return DEFAULT_SETTINGS | db.get("settings", {})


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
