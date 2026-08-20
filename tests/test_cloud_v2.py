"""Tests for the v2 CloudClient contract: per-row sync outcomes, backfill
flagging, heartbeat/events/config endpoints."""

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock


def _make_client(**kw):
    from cloud.client import CloudClient

    defaults = dict(
        device_id="BS-WQM1-000000000000",
        ingest_url="https://example.test/ingest",
        command_url="https://example.test/commands",
        api_key="testkey0123456789",
        fw_version="2.0.0",
        api_base="https://example.test/app",
    )
    defaults.update(kw)
    c = CloudClient(**defaults)
    c._sleep = lambda *_: None
    return c


class _Resp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = json.dumps(body).encode() if body is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.synced = []
        self.failed = []
        self.attempts = []

    def get_unsynced(self, limit):
        return self.rows[:limit]

    def mark_synced(self, ids):
        self.synced.extend(ids)

    def mark_failed_permanent(self, ids):
        self.failed.extend(ids)

    def bump_sync_attempts(self, ids):
        self.attempts.extend(ids)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_fixture():
    now = datetime.now(UTC)
    return [
        {"id": 1, "timestamp": iso(now - timedelta(hours=1)), "ph": 7.0},
        {"id": 2, "timestamp": iso(now - timedelta(hours=2)), "ph": 7.1},
        {"id": 3, "timestamp": iso(now - timedelta(days=5)), "ph": 7.2},
    ]


class TestPerRowSync:
    def test_mixed_results_split_synced_and_failed(self, monkeypatch, mock_hardware):
        c = _make_client()
        db = FakeDB(rows_fixture())
        resp = {
            "success": True,
            "results": [
                {"index": 0, "status": "stored"},
                {"index": 1, "error": "Timestamp out of acceptable range"},
                {"index": 2, "status": "stored"},
            ],
        }
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(200, resp)))
        n = c.sync_readings(db)
        assert n == 2
        assert db.synced == [1, 3]
        assert db.failed == [2]

    def test_rejection_warning_names_the_running_total(self, monkeypatch, mock_hardware, caplog):
        # A per-batch "1 rejected" is a blip; the same line repeated for
        # sixteen days while the total passed 15,000 is a systemic fault, and
        # only the cumulative figure makes it look like one.
        class CountingDB(FakeDB):
            def get_state_count(self, state):
                return 15409 if state == "failed_permanent" else 0

        c = _make_client()
        db = CountingDB(rows_fixture())
        resp = {
            "results": [
                {"index": 0, "status": "stored"},
                {"index": 1, "error": "Missing sensors data"},
                {"index": 2, "status": "stored"},
            ],
        }
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(200, resp)))
        with caplog.at_level(logging.WARNING, logger="wqm1.cloud"):
            c.sync_readings(db)
        assert "15409 total rejected" in caplog.text
        assert "Missing sensors data" in caplog.text

    def test_rejection_warning_survives_a_db_without_state_counts(
        self, monkeypatch, mock_hardware, caplog
    ):
        # Telemetry must never be the thing that breaks a sync. FakeDB has no
        # get_state_count at all.
        c = _make_client()
        db = FakeDB(rows_fixture())
        resp = {"results": [{"index": 0, "error": "Missing sensors data"}]}
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(200, resp)))
        with caplog.at_level(logging.WARNING, logger="wqm1.cloud"):
            n = c.sync_readings(db)
        assert n == 0
        assert db.failed == [1]
        assert "total rejected" not in caplog.text

    def test_legacy_server_without_indices_marks_whole_batch(self, monkeypatch, mock_hardware):
        c = _make_client()
        db = FakeDB(rows_fixture())
        resp = {"success": True, "processed": 3, "failed": 0}
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(200, resp)))
        n = c.sync_readings(db)
        assert n == 3
        assert db.synced == [1, 2, 3]
        assert db.failed == []

    def test_transport_failure_marks_nothing_but_bumps_attempts(self, monkeypatch, mock_hardware):
        import urllib.error

        c = _make_client(max_retries=1)
        db = FakeDB(rows_fixture())
        monkeypatch.setattr(
            "urllib.request.urlopen",
            MagicMock(side_effect=urllib.error.URLError("down")),
        )
        n = c.sync_readings(db)
        assert n == 0
        assert db.synced == []
        assert db.failed == []
        assert db.attempts == [1, 2, 3]

    def test_old_rows_flagged_backfill(self, monkeypatch, mock_hardware):
        c = _make_client()
        db = FakeDB(rows_fixture())
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode())
            return _Resp(200, {"results": [{"index": i, "status": "stored"} for i in range(3)]})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        c.sync_readings(db)
        payload = captured["payload"]
        assert "backfill" not in payload[0]["metadata"]  # 1h old — live
        assert "backfill" not in payload[1]["metadata"]  # 2h old — live
        assert payload[2]["metadata"]["backfill"] is True  # 5 days old


class TestReadingMetadata:
    def test_health_provider_populates_battery_and_signal(self, mock_hardware):
        c = _make_client(health_provider=lambda: {"batteryLevel": 82, "signalStrength": -70})
        j = c.reading_to_json({"id": 1, "timestamp": None, "ph": 7.0})
        assert j["metadata"]["batteryLevel"] == 82
        assert j["metadata"]["signalStrength"] == -70

    def test_health_provider_absent_keeps_nulls(self, mock_hardware):
        c = _make_client()
        j = c.reading_to_json({"id": 1, "timestamp": None, "ph": 7.0})
        assert j["metadata"]["batteryLevel"] is None
        assert j["metadata"]["signalStrength"] is None

    def test_health_provider_errors_swallowed(self, mock_hardware):
        def broken():
            raise RuntimeError("no health")

        c = _make_client(health_provider=broken)
        j = c.reading_to_json({"id": 1, "timestamp": None, "ph": 7.0})
        assert j["metadata"]["batteryLevel"] is None


class TestPoll:
    def test_poll_returns_hints(self, monkeypatch, mock_hardware):
        c = _make_client()
        resp = {
            "commands": [{"id": "c1", "type": "relay"}],
            "relaySchedule": None,
            "configVersion": 9,
            "otaPending": True,
        }
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(200, resp)))
        poll = c.poll()
        assert poll["configVersion"] == 9
        assert poll["otaPending"] is True
        assert poll["commands"][0]["id"] == "c1"

    def test_poll_failure_returns_safe_defaults(self, monkeypatch, mock_hardware):
        import urllib.error

        c = _make_client(max_retries=1)
        monkeypatch.setattr(
            "urllib.request.urlopen",
            MagicMock(side_effect=urllib.error.URLError("down")),
        )
        poll = c.poll()
        assert poll == {
            "commands": [],
            "relaySchedule": None,
            "configVersion": None,
            "otaPending": False,
        }

    def test_poll_commands_compat_shim(self, monkeypatch, mock_hardware):
        c = _make_client()
        resp = {"commands": [{"id": "c1"}]}
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(200, resp)))
        assert c.poll_commands() == [{"id": "c1"}]


class TestTelemetryEndpoints:
    def test_send_heartbeat_posts_to_device_url(self, monkeypatch, mock_hardware):
        c = _make_client()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _Resp(200, {"success": True})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        ok = c.send_heartbeat({"uptimeS": 10})
        assert ok
        assert captured["url"].endswith("/v2/devices/BS-WQM1-000000000000/heartbeat")
        assert captured["body"] == {"uptimeS": 10}

    def test_send_event_truncates_message(self, monkeypatch, mock_hardware):
        c = _make_client()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _Resp(200, {"success": True})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        c.send_event("sensor_stuck", message="x" * 999, sensor="ph")
        assert len(captured["body"]["message"]) == 300
        assert captured["body"]["type"] == "sensor_stuck"

    def test_fetch_config_parses_version_and_values(self, monkeypatch, mock_hardware):
        c = _make_client()
        resp = {"success": True, "data": {"version": 3, "values": {"sensor_read_s": 30}}}

        def fake_urlopen(req, timeout=None):
            assert req.get_method() == "GET"
            return _Resp(200, resp)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert c.fetch_config() == (3, {"sensor_read_s": 30})

    def test_fetch_config_204_returns_none(self, monkeypatch, mock_hardware):
        c = _make_client()
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=_Resp(204)))
        assert c.fetch_config() is None

    def test_no_api_base_disables_v2_endpoints(self, mock_hardware):
        c = _make_client(api_base="")
        assert c.send_heartbeat({}) is False
        assert c.send_event("degraded") is False
        assert c.fetch_config() is None
