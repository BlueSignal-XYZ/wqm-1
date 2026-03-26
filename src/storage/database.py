"""
SQLite WAL Database Buffer

Stores sensor readings locally with sync tracking. WAL mode ensures
crash resilience. Supports row rotation at configurable threshold.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.config import get_settings

logger = logging.getLogger("wqm1.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ph REAL,
    tds_ppm REAL,
    turbidity_ntu REAL,
    orp_mv REAL,
    temp_c REAL,
    lat REAL,
    lon REAL,
    alt_m REAL,
    battery_v REAL,
    relay_state INTEGER DEFAULT 0,
    synced INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_readings_synced ON readings(synced);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp);

CREATE TABLE IF NOT EXISTS lorawan_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dev_addr BLOB,
    nwk_skey BLOB,
    app_skey BLOB,
    fcnt_up INTEGER DEFAULT 0,
    fcnt_down INTEGER DEFAULT 0,
    joined INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO lorawan_session (id) VALUES (1);
"""


class WQM1Database:
    """SQLite database for WQM-1 readings with WAL mode."""

    def __init__(self, path: str = None):
        self._path = path or get_settings().db_path

        # Ensure parent directory exists
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

        logger.info("Database opened: %s", self._path)

    def insert_reading(self, data: dict[str, Any]) -> int:
        """
        Insert a sensor reading.

        Args:
            data: Dict with keys matching the readings table columns.

        Returns:
            Row ID of the inserted reading.
        """
        ts = data.get("timestamp") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO readings
                   (timestamp, ph, tds_ppm, turbidity_ntu, orp_mv, temp_c,
                    lat, lon, alt_m, battery_v, relay_state, synced)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    ts,
                    data.get("ph"),
                    data.get("tds_ppm"),
                    data.get("turbidity_ntu"),
                    data.get("orp_mv"),
                    data.get("temp_c"),
                    data.get("lat"),
                    data.get("lon"),
                    data.get("alt_m"),
                    data.get("battery_v"),
                    data.get("relay_state", 0),
                ),
            )
            return cur.lastrowid

    def get_unsynced(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get unsynced readings ordered by timestamp."""
        cur = self._conn.execute(
            """SELECT id, timestamp, ph, tds_ppm, turbidity_ntu, orp_mv,
                      temp_c, lat, lon, alt_m, battery_v, relay_state
               FROM readings WHERE synced = 0
               ORDER BY timestamp ASC LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    def mark_synced(self, ids: list[int]) -> None:
        """Mark readings as synced."""
        if not ids:
            return
        with self._conn:
            self._conn.executemany(
                "UPDATE readings SET synced = 1 WHERE id = ?",
                [(i,) for i in ids],
            )

    def get_count(self, synced: bool | None = None) -> int:
        """Get reading count, optionally filtered by sync status."""
        if synced is None:
            cur = self._conn.execute("SELECT COUNT(*) FROM readings")
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM readings WHERE synced = ?",
                (1 if synced else 0,),
            )
        return cur.fetchone()[0]

    def rotate(self, max_rows: int = None) -> int:
        """
        Delete oldest synced rows if total exceeds max_rows.

        Returns:
            Number of rows deleted.
        """
        if max_rows is None:
            max_rows = get_settings().db_max_rows

        total = self.get_count()
        if total <= max_rows:
            return 0

        excess = total - max_rows
        with self._conn:
            cur = self._conn.execute(
                """DELETE FROM readings WHERE id IN (
                       SELECT id FROM readings
                       WHERE synced = 1
                       ORDER BY timestamp ASC
                       LIMIT ?
                   )""",
                (excess,),
            )
            deleted = cur.rowcount

        if deleted > 0:
            logger.info("Rotated %d synced rows (total was %d)", deleted, total)
        return deleted

    def get_latest(self) -> dict[str, Any] | None:
        """Get the most recent reading."""
        cur = self._conn.execute(
            """SELECT id, timestamp, ph, tds_ppm, turbidity_ntu, orp_mv,
                      temp_c, lat, lon, alt_m, battery_v, relay_state, synced
               FROM readings ORDER BY id DESC LIMIT 1"""
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # --- LoRaWAN Session ---

    def save_session(
        self,
        dev_addr: bytes,
        nwk_skey: bytes,
        app_skey: bytes,
        fcnt_up: int,
        fcnt_down: int,
        joined: bool,
    ) -> None:
        """Persist LoRaWAN session state."""
        with self._conn:
            self._conn.execute(
                """UPDATE lorawan_session SET
                   dev_addr=?, nwk_skey=?, app_skey=?,
                   fcnt_up=?, fcnt_down=?, joined=?,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=1""",
                (dev_addr, nwk_skey, app_skey, fcnt_up, fcnt_down, 1 if joined else 0),
            )

    def load_session(self) -> dict[str, Any] | None:
        """Load LoRaWAN session state."""
        cur = self._conn.execute(
            "SELECT dev_addr, nwk_skey, app_skey, fcnt_up, fcnt_down, joined FROM lorawan_session WHERE id=1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def increment_fcnt(self) -> int:
        """Increment and return uplink frame counter."""
        with self._conn:
            self._conn.execute("UPDATE lorawan_session SET fcnt_up = fcnt_up + 1 WHERE id=1")
        cur = self._conn.execute("SELECT fcnt_up FROM lorawan_session WHERE id=1")
        return cur.fetchone()[0]

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("Database closed")
