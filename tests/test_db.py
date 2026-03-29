"""Tests for firmware/storage/db.py — SQLite WAL database."""


class TestDatabaseInit:
    def test_creates_db_file(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db_path = str(tmp_path / "test.db")
        db = WQM1Database(path=db_path)
        assert (tmp_path / "test.db").exists()
        db.close()

    def test_wal_mode_enabled(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        cur = db._conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0].lower() == "wal"
        db.close()

    def test_readings_table_exists(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='readings'"
        )
        assert cur.fetchone() is not None
        db.close()


class TestInsertReading:
    def test_insert_and_retrieve(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        row_id = db.insert_reading(
            {
                "ph": 7.2,
                "tds_ppm": 450.0,
                "turbidity_ntu": 120.0,
                "orp_mv": 250.0,
                "temp_c": 22.5,
                "lat": 30.267,
                "lon": -97.743,
            }
        )
        assert row_id == 1

        latest = db.get_latest()
        assert latest["ph"] == 7.2
        assert latest["tds_ppm"] == 450.0
        assert latest["orp_mv"] == 250.0
        db.close()

    def test_insert_partial_data(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        row_id = db.insert_reading({"ph": 7.0})
        assert row_id == 1
        latest = db.get_latest()
        assert latest["ph"] == 7.0
        assert latest["tds_ppm"] is None
        db.close()

    def test_multiple_inserts(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        for i in range(50):
            db.insert_reading({"ph": 7.0 + i * 0.01})
        assert db.get_count() == 50
        db.close()


class TestSyncTracking:
    def test_unsynced_readings(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 7.0})
        db.insert_reading({"ph": 7.1})

        unsynced = db.get_unsynced(limit=10)
        assert len(unsynced) == 2
        db.close()

    def test_mark_synced(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 7.0})
        db.insert_reading({"ph": 7.1})
        db.mark_synced([1])
        unsynced = db.get_unsynced()
        assert len(unsynced) == 1
        assert unsynced[0]["id"] == 2
        db.close()

    def test_mark_empty_list(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.mark_synced([])  # should not raise
        db.close()


class TestRotation:
    def test_rotate_deletes_synced(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        for _i in range(20):
            db.insert_reading({"ph": 7.0})
        db.mark_synced(list(range(1, 16)))  # sync first 15

        deleted = db.rotate(max_rows=10)
        assert deleted == 10
        assert db.get_count() <= 15
        db.close()

    def test_rotate_no_op_under_threshold(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 7.0})
        deleted = db.rotate(max_rows=100)
        assert deleted == 0
        db.close()

    def test_get_count_filtered(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 7.0})
        db.insert_reading({"ph": 7.1})
        db.mark_synced([1])

        assert db.get_count(synced=True) == 1
        assert db.get_count(synced=False) == 1
        assert db.get_count() == 2
        db.close()
