"""Tests for LoRaWAN session persistence and frame counter in database."""


class TestLoRaWANSession:
    def test_save_and_load_session(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        dev_addr = b"\x01\x02\x03\x04"
        nwk_skey = b"\xAA" * 16
        app_skey = b"\xBB" * 16

        db.save_session(dev_addr, nwk_skey, app_skey, fcnt_up=42, fcnt_down=7, joined=True)

        session = db.load_session()
        assert session is not None
        assert bytes(session["dev_addr"]) == dev_addr
        assert bytes(session["nwk_skey"]) == nwk_skey
        assert bytes(session["app_skey"]) == app_skey
        assert session["fcnt_up"] == 42
        assert session["fcnt_down"] == 7
        assert session["joined"] == 1
        db.close()

    def test_session_update_overwrites(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.save_session(b"\x01" * 4, b"\xAA" * 16, b"\xBB" * 16, 10, 5, True)
        db.save_session(b"\x02" * 4, b"\xCC" * 16, b"\xDD" * 16, 20, 10, True)

        session = db.load_session()
        assert session["fcnt_up"] == 20
        assert bytes(session["dev_addr"]) == b"\x02" * 4
        db.close()

    def test_session_not_joined_by_default(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        session = db.load_session()
        assert session is not None
        assert session["joined"] == 0
        db.close()

    def test_increment_fcnt(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.save_session(b"\x01" * 4, b"\xAA" * 16, b"\xBB" * 16, 0, 0, True)

        fcnt1 = db.increment_fcnt()
        assert fcnt1 == 1
        fcnt2 = db.increment_fcnt()
        assert fcnt2 == 2

        session = db.load_session()
        assert session["fcnt_up"] == 2
        db.close()

    def test_increment_fcnt_from_nonzero(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.save_session(b"\x01" * 4, b"\xAA" * 16, b"\xBB" * 16, 100, 0, True)

        fcnt = db.increment_fcnt()
        assert fcnt == 101
        db.close()


class TestDatabaseWALBuffering:
    def test_concurrent_read_during_write(self, tmp_path, mock_hardware):
        """WAL mode allows concurrent reads while writing."""
        import sqlite3

        from storage.database import WQM1Database

        db_path = str(tmp_path / "test.db")
        db = WQM1Database(path=db_path)
        db.insert_reading({"ph": 7.0})

        # Open a second read-only connection (simulates service window)
        reader = sqlite3.connect(db_path, timeout=5)
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only = ON")

        cur = reader.execute("SELECT COUNT(*) FROM readings")
        assert cur.fetchone()[0] == 1

        # Insert more data through the main connection
        db.insert_reading({"ph": 7.1})

        # Reader should see the new data after the write commits
        cur = reader.execute("SELECT COUNT(*) FROM readings")
        assert cur.fetchone()[0] == 2

        reader.close()
        db.close()

    def test_close_sets_conn_none(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.close()
        assert db._conn is None

    def test_get_latest_empty_db(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        assert db.get_latest() is None
        db.close()

    def test_get_unsynced_respects_limit(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        for i in range(10):
            db.insert_reading({"ph": 7.0 + i * 0.01})

        unsynced = db.get_unsynced(limit=3)
        assert len(unsynced) == 3
        db.close()

    def test_reading_has_timestamp(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 7.0, "timestamp": "2025-01-15T12:00:00Z"})
        latest = db.get_latest()
        assert latest["timestamp"] == "2025-01-15T12:00:00Z"
        db.close()
