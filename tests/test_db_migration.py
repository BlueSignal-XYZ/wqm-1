"""Tests for the v1→v2 database migration and three-state sync tracking."""

import sqlite3


def make_v1_db(path):
    """Create a database with the exact v1.1.0 schema + some data."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL, temp_c REAL,
            lat REAL, lon REAL, alt_m REAL, battery_v REAL,
            relay_state INTEGER DEFAULT 0,
            synced INTEGER DEFAULT 0
        );
        CREATE INDEX idx_readings_synced ON readings(synced);
        CREATE INDEX idx_readings_ts ON readings(timestamp);
        CREATE TABLE lorawan_session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            dev_addr BLOB, nwk_skey BLOB, app_skey BLOB,
            fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,
            joined INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO lorawan_session (id) VALUES (1);
        """
    )
    with conn:
        conn.execute(
            "INSERT INTO readings (timestamp, ph, synced) VALUES"
            " ('2026-07-01T00:00:00Z', 7.0, 1),"
            " ('2026-07-01T00:01:00Z', 7.1, 0),"
            " ('2026-07-01T00:02:00Z', 7.2, 0)"
        )
        conn.execute("UPDATE lorawan_session SET fcnt_up=42, joined=1 WHERE id=1")
    conn.close()


class TestMigration:
    def test_migrates_v1_db_in_place(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        path = tmp_path / "v1.db"
        make_v1_db(path)

        db = WQM1Database(path=str(path))
        # Legacy synced flag backfilled into sync_state.
        assert db.get_state_count("synced") == 1
        assert db.get_state_count("pending") == 2
        # Existing session survives.
        session = db.load_session()
        assert session["fcnt_up"] == 42
        assert session["joined"] == 1
        db.close()

    def test_migration_is_idempotent(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        path = tmp_path / "v1.db"
        make_v1_db(path)
        for _ in range(3):
            db = WQM1Database(path=str(path))
            db.close()
        db = WQM1Database(path=str(path))
        assert db.get_state_count("pending") == 2
        db.close()

    def test_fresh_db_gets_v2_schema(self, tmp_path, mock_hardware):
        from storage.database import SCHEMA_VERSION, WQM1Database

        db = WQM1Database(path=str(tmp_path / "fresh.db"))
        db.insert_reading({"ph": 7.0})
        assert db.get_state_count("pending") == 1
        db.close()
        conn = sqlite3.connect(str(tmp_path / "fresh.db"))
        # Fresh DBs are stamped too, so future migrations know where they are.
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(row[0]) == SCHEMA_VERSION
        conn.close()


class TestThreeStateSync:
    def test_failed_permanent_rows_leave_the_retry_queue(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "t.db"))
        a = db.insert_reading({"ph": 7.0})
        b = db.insert_reading({"ph": 7.1})
        db.mark_failed_permanent([a])
        pending = db.get_unsynced()
        assert [r["id"] for r in pending] == [b]
        assert db.get_state_count("failed_permanent") == 1
        db.close()

    def test_legacy_synced_flag_kept_in_lockstep(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "t.db"))
        a = db.insert_reading({"ph": 7.0})
        b = db.insert_reading({"ph": 7.1})
        db.mark_synced([a])
        db.mark_failed_permanent([b])
        # A v1.1.0 rollback reads `synced` — both resolved rows must read 1.
        assert db.get_count(synced=True) == 2
        db.close()

    def test_bump_sync_attempts(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "t.db"))
        a = db.insert_reading({"ph": 7.0})
        db.bump_sync_attempts([a])
        db.bump_sync_attempts([a])
        cur = db._conn.execute("SELECT sync_attempts FROM readings WHERE id=?", (a,))
        assert cur.fetchone()[0] == 2
        db.close()

    def test_rotate_never_drops_pending(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "t.db"))
        ids = [db.insert_reading({"ph": 7.0}) for _ in range(10)]
        db.mark_synced(ids[:3])
        db.mark_failed_permanent(ids[3:5])
        deleted = db.rotate(max_rows=6)
        assert deleted == 4  # only resolved rows go
        assert db.get_state_count("pending") == 5
        db.close()


class TestCrossThreadAccess:
    def test_each_thread_gets_its_own_connection(self, tmp_path, mock_hardware):
        import threading

        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "t.db"))
        db.insert_reading({"ph": 7.0})
        results = {}

        def reader():
            results["conn"] = db._conn
            results["latest"] = db.get_latest()

        t = threading.Thread(target=reader)
        t.start()
        t.join()

        assert results["latest"]["ph"] == 7.0
        assert results["conn"] is not db._conn  # distinct per-thread handles
        db.close()

    def test_concurrent_writes_from_threads(self, tmp_path, mock_hardware):
        import threading

        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "t.db"))
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    db.insert_reading({"ph": 7.0 + n + i / 100})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert db.get_count() == 80
        db.close()


class TestV3Migration:
    """v3 adds the RS485 sensor columns (chlorine, conductivity, salinity)."""

    def test_v1_db_gains_rs485_columns(self, tmp_path, mock_hardware):
        from storage.database import SCHEMA_VERSION, WQM1Database

        path = tmp_path / "v1.db"
        make_v1_db(path)
        db = WQM1Database(path=str(path))
        rid = db.insert_reading(
            {"ph": 7.0, "chlorine_mgl": 0.35, "conductivity_uscm": 480.0, "salinity_ppt": 0.24}
        )
        row = next(r for r in db.get_unsynced(limit=10) if r["id"] == rid)
        assert row["chlorine_mgl"] == 0.35
        assert row["conductivity_uscm"] == 480.0
        assert row["salinity_ppt"] == 0.24
        db.close()

        conn = sqlite3.connect(str(path))
        stamp = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(stamp[0]) == SCHEMA_VERSION
        conn.close()

    def test_pre_rs485_rows_read_null_for_new_columns(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        path = tmp_path / "v1.db"
        make_v1_db(path)
        db = WQM1Database(path=str(path))
        rows = db.get_unsynced(limit=10)
        assert all(r["chlorine_mgl"] is None for r in rows)
        db.close()

    def test_v3_migration_is_idempotent(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        path = tmp_path / "v1.db"
        make_v1_db(path)
        for _ in range(3):
            WQM1Database(path=str(path)).close()
        db = WQM1Database(path=str(path))
        assert db.insert_reading({"chlorine_mgl": 0.1}) > 0
        db.close()
