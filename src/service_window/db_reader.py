"""
Read-only database access for the service window.

Uses a separate SQLite connection (WAL mode allows concurrent readers)
so there is no contention with the firmware's write connection.
"""

import sqlite3
from typing import Any


class DBReader:
    """Read-only interface to the WQM-1 SQLite database."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    # Columns added by later schema versions. The Service Window reads the
    # buffer while the main service — the one that runs migrations — may be
    # restarting after an OTA, so a SELECT must not name a column an older
    # buffer lacks: it would throw and take the home page down on exactly
    # the unit that just updated. Ask the table what it has.
    _OPTIONAL_COLS = ("flow_total_gal", "flow_rate_gpm")

    def _optional_cols(self, conn: sqlite3.Connection) -> str:
        have = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
        present = [c for c in self._OPTIONAL_COLS if c in have]
        return "".join(f", {c}" for c in present)

    def get_latest_reading(self) -> dict[str, Any] | None:
        """Get the most recent sensor reading."""
        conn = self._connect()
        try:
            extra = self._optional_cols(conn)
            cur = conn.execute(
                f"""SELECT id, timestamp, ph, tds_ppm, turbidity_ntu, orp_mv,
                          temp_c, lat, lon, alt_m, battery_v, relay_state, synced{extra}
                   FROM readings ORDER BY id DESC LIMIT 1"""  # nosec B608 — constant column names
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_readings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent readings, newest first."""
        conn = self._connect()
        try:
            extra = self._optional_cols(conn)
            cur = conn.execute(
                f"""SELECT id, timestamp, ph, tds_ppm, turbidity_ntu, orp_mv,
                          temp_c, lat, lon, alt_m, battery_v, relay_state{extra}
                   FROM readings ORDER BY id DESC LIMIT ?""",  # nosec B608 — constant column names
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_reading_count(self) -> int:
        """Get total number of readings."""
        conn = self._connect()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM readings")
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def get_lorawan_session(self) -> dict[str, Any] | None:
        """Get LoRaWAN session state."""
        conn = self._connect()
        try:
            cur = conn.execute(
                """SELECT dev_addr, nwk_skey, app_skey, fcnt_up, fcnt_down,
                          joined, updated_at
                   FROM lorawan_session WHERE id=1"""
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
