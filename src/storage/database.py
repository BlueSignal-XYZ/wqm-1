"""
SQLite WAL Database Buffer

Stores sensor readings locally with sync tracking. WAL mode ensures
crash resilience. Supports row rotation at configurable threshold.

v2 changes:
- Per-thread connections (``threading.local``) — the supervisor, sampling,
  cloud, and radio workers each get their own connection instead of sharing
  one ``check_same_thread=False`` handle across threads.
- Three-state sync tracking: ``sync_state`` pending/synced/failed_permanent
  (+ ``sync_attempts``), replacing the binary ``synced`` flag as the source of
  truth. The legacy ``synced`` column is kept in lockstep so a rollback to
  v1.1.0 finds a coherent buffer.
- A ``meta`` table records the schema version; ``_migrate()`` upgrades a
  v1.1.0-shaped database in place on first boot after the update.
"""

import contextlib
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.config import get_settings

logger = logging.getLogger("wqm1.db")

SCHEMA_VERSION = 3

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
    chlorine_mgl REAL,
    conductivity_uscm REAL,
    salinity_ppt REAL,
    lat REAL,
    lon REAL,
    alt_m REAL,
    battery_v REAL,
    relay_state INTEGER DEFAULT 0,
    synced INTEGER DEFAULT 0,
    sync_state TEXT NOT NULL DEFAULT 'pending',
    sync_attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_readings_synced ON readings(synced);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp);
-- idx_readings_sync_state is created in _migrate(), after the column is
-- guaranteed to exist on pre-v2 databases.

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

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_READING_COLS = (
    "id, timestamp, ph, tds_ppm, turbidity_ntu, orp_mv, temp_c,"
    " chlorine_mgl, conductivity_uscm, salinity_ppt, lat, lon, alt_m,"
    " battery_v, relay_state"
)


class WQM1Database:
    """SQLite database for WQM-1 readings with WAL mode (thread-safe: one
    connection per calling thread)."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or get_settings().db_path

        # Ensure parent directory exists
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._closed = False

        # Create schema + run migrations on the constructing thread.
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._migrate()

        logger.info("Database opened: %s", self._path)

    @property
    def _conn(self) -> sqlite3.Connection:
        """Connection for the current thread (created on first use)."""
        if self._closed:
            raise sqlite3.ProgrammingError("database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    def _migrate(self) -> None:
        """Upgrade an existing (v1.1.0) database in place. Idempotent."""
        conn = self._conn
        cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        current = int(row[0]) if row else 1

        if current < 2:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
            with conn:
                if "sync_state" not in cols:
                    conn.execute(
                        "ALTER TABLE readings ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'pending'"
                    )
                if "sync_attempts" not in cols:
                    conn.execute(
                        "ALTER TABLE readings ADD COLUMN sync_attempts INTEGER NOT NULL DEFAULT 0"
                    )
                # Backfill sync_state from the legacy flag.
                conn.execute(
                    "UPDATE readings SET sync_state = CASE synced WHEN 1 THEN 'synced'"
                    " ELSE 'pending' END"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_readings_sync_state ON readings(sync_state)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '2')"
                )
            logger.info("Database migrated to schema v2")
            current = 2

        if current < 3:
            # v3 (RS485 sensor expansion): residual chlorine, conductivity
            # and salinity columns. NULL for readings that predate the
            # sensors — additive and rollback-safe.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
            with conn:
                for column in ("chlorine_mgl", "conductivity_uscm", "salinity_ppt"):
                    if column not in cols:
                        conn.execute(f"ALTER TABLE readings ADD COLUMN {column} REAL")
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            logger.info("Database migrated to schema v%d", SCHEMA_VERSION)

    def insert_reading(self, data: dict[str, Any]) -> int:
        """
        Insert a sensor reading.

        Args:
            data: Dict with keys matching the readings table columns.

        Returns:
            Row ID of the inserted reading.
        """
        ts = data.get("timestamp") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._conn
        with conn:
            cur = conn.execute(
                """INSERT INTO readings
                   (timestamp, ph, tds_ppm, turbidity_ntu, orp_mv, temp_c,
                    chlorine_mgl, conductivity_uscm, salinity_ppt,
                    lat, lon, alt_m, battery_v, relay_state, synced, sync_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending')""",
                (
                    ts,
                    data.get("ph"),
                    data.get("tds_ppm"),
                    data.get("turbidity_ntu"),
                    data.get("orp_mv"),
                    data.get("temp_c"),
                    data.get("chlorine_mgl"),
                    data.get("conductivity_uscm"),
                    data.get("salinity_ppt"),
                    data.get("lat"),
                    data.get("lon"),
                    data.get("alt_m"),
                    data.get("battery_v"),
                    data.get("relay_state", 0),
                ),
            )
            return cur.lastrowid or 0

    def get_unsynced(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get pending readings ordered by timestamp (excludes rows the cloud
        permanently rejected — those are counted, not retried forever)."""
        # Single-quoted fragments, not a triple-quoted block: bandit's nosec must
        # share a line with the flagged expression, and inside a multi-line SQL
        # string the marker would be shipped to SQLite as query text.
        query = (
            f"SELECT {_READING_COLS} "  # nosec B608 — constant column list; values bound
            "FROM readings WHERE sync_state = 'pending' "
            "ORDER BY timestamp ASC LIMIT ?"
        )
        cur = self._conn.execute(query, (limit,))
        return [dict(row) for row in cur.fetchall()]

    def mark_synced(self, ids: list[int]) -> None:
        """Mark readings as successfully synced."""
        if not ids:
            return
        conn = self._conn
        with conn:
            conn.executemany(
                "UPDATE readings SET synced = 1, sync_state = 'synced' WHERE id = ?",
                [(i,) for i in ids],
            )

    def mark_failed_permanent(self, ids: list[int]) -> None:
        """Mark readings the cloud permanently rejected (4xx-class row errors).

        They stop being retried but are retained until rotation so an operator
        can inspect what was refused; the legacy `synced` flag is set so a
        v1.1.0 rollback doesn't re-send them forever either."""
        if not ids:
            return
        conn = self._conn
        with conn:
            conn.executemany(
                "UPDATE readings SET synced = 1, sync_state = 'failed_permanent' WHERE id = ?",
                [(i,) for i in ids],
            )

    def bump_sync_attempts(self, ids: list[int]) -> None:
        """Increment the attempt counter for rows that were sent but not resolved."""
        if not ids:
            return
        conn = self._conn
        with conn:
            conn.executemany(
                "UPDATE readings SET sync_attempts = sync_attempts + 1 WHERE id = ?",
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
        return int(cur.fetchone()[0])

    def get_state_count(self, state: str) -> int:
        """Count readings in a given sync_state (buffer-depth telemetry)."""
        cur = self._conn.execute("SELECT COUNT(*) FROM readings WHERE sync_state = ?", (state,))
        return int(cur.fetchone()[0])

    def rotate(self, max_rows: int | None = None) -> int:
        """
        Delete oldest resolved (synced or permanently failed) rows if total
        exceeds max_rows. Pending rows are never dropped.

        Returns:
            Number of rows deleted.
        """
        if max_rows is None:
            max_rows = get_settings().db_max_rows

        total = self.get_count()
        if total <= max_rows:
            return 0

        excess = total - max_rows
        conn = self._conn
        with conn:
            cur = conn.execute(
                """DELETE FROM readings WHERE id IN (
                       SELECT id FROM readings
                       WHERE sync_state IN ('synced', 'failed_permanent')
                       ORDER BY timestamp ASC
                       LIMIT ?
                   )""",
                (excess,),
            )
            deleted = cur.rowcount

        if deleted > 0:
            logger.info("Rotated %d resolved rows (total was %d)", deleted, total)
        return deleted

    def get_latest(self) -> dict[str, Any] | None:
        """Get the most recent reading."""
        query = (
            f"SELECT {_READING_COLS}, synced, sync_state "  # nosec B608 — constant cols
            "FROM readings ORDER BY id DESC LIMIT 1"
        )
        cur = self._conn.execute(query)
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
        conn = self._conn
        with conn:
            conn.execute(
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
            "SELECT dev_addr, nwk_skey, app_skey, fcnt_up, fcnt_down, joined"
            " FROM lorawan_session WHERE id=1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def increment_fcnt(self) -> int:
        """Increment and return uplink frame counter."""
        conn = self._conn
        with conn:
            conn.execute("UPDATE lorawan_session SET fcnt_up = fcnt_up + 1 WHERE id=1")
        cur = conn.execute("SELECT fcnt_up FROM lorawan_session WHERE id=1")
        return int(cur.fetchone()[0])

    def close(self) -> None:
        """Close all per-thread database connections."""
        self._closed = True
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.close()
        self._local = threading.local()
        logger.info("Database closed")
