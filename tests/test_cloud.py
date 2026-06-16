"""Tests for src/cloud/client.py — HTTP readings uplink + command poll/ack."""

import json
import urllib.error
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _make_client(**kw):
    from cloud.client import CloudClient

    defaults = dict(
        device_id="BS-WQM1-0000A92E4E7D",
        ingest_url="https://example.test/ingestReading",
        command_url="https://example.test/deviceCommands",
        api_key="testkey0123456789",
        fw_version="9.9.9",
    )
    defaults.update(kw)
    c = CloudClient(**defaults)
    c._sleep = lambda *_: None  # no real backoff in tests
    return c


class _Resp:
    """Minimal stand-in for the urlopen context-manager response."""

    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _patch_urlopen(monkeypatch, responder):
    """Patch urllib.request.urlopen; capture each request, return responder(req-info)."""
    captured = []

    def fake_urlopen(req, timeout=None):
        info = {
            "url": req.full_url,
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": json.loads(req.data.decode()) if req.data else None,
        }
        captured.append(info)
        return responder(info)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def _row(**over):
    base = {
        "id": 1,
        "timestamp": "2026-06-16T18:30:46Z",
        "ph": None,
        "tds_ppm": None,
        "turbidity_ntu": None,
        "orp_mv": None,
        "temp_c": None,
        "lat": None,
        "lon": None,
        "alt_m": None,
        "battery_v": None,
        "relay_state": 0,
    }
    base.update(over)
    return base


class TestReadingToJson:
    def test_maps_sensors_and_skips_none(self):
        c = _make_client()
        out = c.reading_to_json(_row(ph=7.2, tds_ppm=450.0, temp_c=21.4, relay_state=2))
        assert out["deviceId"] == "BS-WQM1-0000A92E4E7D"
        expected_ms = int(datetime(2026, 6, 16, 18, 30, 46, tzinfo=UTC).timestamp() * 1000)
        assert out["timestamp"] == expected_ms
        assert out["sensors"] == {
            "ph": {"value": 7.2},
            "tds": {"value": 450.0},
            "temperature": {"value": 21.4},
        }
        assert "turbidity" not in out["sensors"]
        assert out["metadata"]["firmware"] == "9.9.9"
        assert out["metadata"]["relayState"] == 2

    def test_includes_gps_when_present(self):
        c = _make_client()
        out = c.reading_to_json(_row(lat=38.9, lon=-77.0, alt_m=10.0))
        assert out["metadata"]["gps"] == {"latitude": 38.9, "longitude": -77.0, "altitude": 10.0}

    def test_orp_and_battery_mapping(self):
        c = _make_client()
        out = c.reading_to_json(_row(orp_mv=200.0, battery_v=4.1))
        assert out["sensors"]["orp"] == {"value": 200.0}
        assert out["sensors"]["battery_voltage"] == {"value": 4.1}


class TestSyncReadings:
    def test_marks_synced_on_200(self, monkeypatch):
        c = _make_client(batch_size=10)
        db = MagicMock()
        db.get_unsynced.return_value = [_row(id=1, ph=7.0), _row(id=2, ph=7.1)]
        captured = _patch_urlopen(
            monkeypatch, lambda _: _Resp(200, {"success": True, "processed": 2, "failed": 0})
        )
        n = c.sync_readings(db)
        assert n == 2
        db.mark_synced.assert_called_once_with([1, 2])
        assert captured[0]["url"].endswith("/ingestReading")
        assert captured[0]["headers"]["x-api-key"] == "testkey0123456789"
        assert isinstance(captured[0]["body"], list) and len(captured[0]["body"]) == 2

    def test_no_mark_on_network_failure(self, monkeypatch):
        c = _make_client(max_retries=1)
        db = MagicMock()
        db.get_unsynced.return_value = [_row(id=1, ph=7.0)]

        def boom(req, timeout=None):
            raise urllib.error.URLError("no network")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert c.sync_readings(db) == 0
        db.mark_synced.assert_not_called()

    def test_empty_buffer_is_noop(self):
        c = _make_client()
        db = MagicMock()
        db.get_unsynced.return_value = []
        assert c.sync_readings(db) == 0
        db.mark_synced.assert_not_called()


class TestCommands:
    def test_poll_returns_commands(self, monkeypatch):
        c = _make_client()
        cmds = [{"id": "abc", "type": "relay", "channel": 1, "state": 1}]
        captured = _patch_urlopen(monkeypatch, lambda _: _Resp(200, {"commands": cmds}))
        assert c.poll_commands() == cmds
        assert captured[0]["body"] == {"deviceId": "BS-WQM1-0000A92E4E7D", "action": "poll"}

    def test_poll_empty_when_no_commands_key(self, monkeypatch):
        c = _make_client()
        _patch_urlopen(monkeypatch, lambda _: _Resp(200, {}))
        assert c.poll_commands() == []

    def test_ack_posts_status(self, monkeypatch):
        c = _make_client()
        captured = _patch_urlopen(monkeypatch, lambda _: _Resp(200, {"success": True}))
        c.ack_command("abc", "done")
        assert captured[0]["body"] == {
            "deviceId": "BS-WQM1-0000A92E4E7D",
            "action": "ack",
            "commandId": "abc",
            "status": "done",
        }

    def test_4xx_does_not_retry(self, monkeypatch):
        c = _make_client(max_retries=3)
        calls = []

        def unauthorized(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", unauthorized)
        assert c.poll_commands() == []
        assert len(calls) == 1  # 401 is terminal — no retry


class TestCloudConfig:
    def test_settings_load_cloud_fields(self, tmp_path):
        from utils.config import _load_settings

        path = tmp_path / "config.yaml"
        path.write_text("cloud_enabled: true\napi_key: abc123\ncommand_poll_s: 7\n")
        s = _load_settings(str(path))
        assert s.cloud_enabled is True
        assert s.api_key == "abc123"
        assert s.command_poll_s == 7
        # default URLs present
        assert "ingestReading" in s.cloud_ingest_url
        assert "deviceCommands" in s.cloud_command_url
