"""Tests for uncovered lines in src/cloud/client.py."""

import json
import urllib.error
from unittest.mock import MagicMock


def _make_client(**kw):
    from cloud.client import CloudClient

    defaults = dict(
        device_id="BS-WQM1-000000000000",
        ingest_url="https://example.test/ingest",
        command_url="https://example.test/commands",
        api_key="testkey0123456789",
    )
    defaults.update(kw)
    c = CloudClient(**defaults)
    c._sleep = lambda *_: None
    return c


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestIsoToMs:
    def test_none_timestamp_uses_current_time(self):
        from cloud.client import _iso_to_ms

        result = _iso_to_ms(None)
        assert isinstance(result, int)
        assert result > 0

    def test_empty_string_uses_current_time(self):
        from cloud.client import _iso_to_ms

        result = _iso_to_ms("")
        assert isinstance(result, int)
        assert result > 0

    def test_invalid_format_uses_current_time(self):
        from cloud.client import _iso_to_ms

        result = _iso_to_ms("not-a-date")
        assert isinstance(result, int)
        assert result > 0

    def test_valid_timestamp_converts_correctly(self):
        from cloud.client import _iso_to_ms

        result = _iso_to_ms("2026-01-01T00:00:00Z")
        assert isinstance(result, int)
        assert result > 0


class TestPostSchemeValidation:
    def test_rejects_non_http_scheme(self, monkeypatch):
        c = _make_client(ingest_url="file:///etc/passwd")
        db = MagicMock()
        db.get_unsynced.return_value = [
            {
                "id": 1,
                "timestamp": "2026-01-01T00:00:00Z",
                "ph": 7.0,
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
        ]
        assert c.sync_readings(db) == 0
        db.mark_synced.assert_not_called()

    def test_rejects_ftp_scheme(self):
        c = _make_client()
        status, resp = c._post("ftp://evil.test/data", {"test": True})
        assert status == 0
        assert resp is None


class TestHttpRetryBehavior:
    def test_4xx_stops_retry_immediately(self, monkeypatch):
        c = _make_client(max_retries=3)
        calls = []

        def unauthorized(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", unauthorized)
        status, resp = c._post("https://example.test/data", {"test": True})
        assert status == 403
        assert resp is None
        assert len(calls) == 1

    def test_5xx_retries_then_fails(self, monkeypatch):
        c = _make_client(max_retries=2)
        calls = []

        def server_error(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", server_error)
        status, resp = c._post("https://example.test/data", {"test": True})
        assert status == 500
        assert resp is None
        assert len(calls) == 2

    def test_network_error_retries(self, monkeypatch):
        c = _make_client(max_retries=2)
        calls = []

        def network_error(req, timeout=None):
            calls.append(1)
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", network_error)
        status, resp = c._post("https://example.test/data", {"test": True})
        assert status == 0
        assert resp is None
        assert len(calls) == 2


class TestRadiosProviderError:
    def test_radios_provider_exception_is_swallowed(self, monkeypatch):
        def bad_radios():
            raise RuntimeError("radio hardware fault")

        c = _make_client(radios_provider=bad_radios)
        db = MagicMock()
        db.get_unsynced.return_value = [
            {
                "id": 1,
                "timestamp": "2026-01-01T00:00:00Z",
                "ph": 7.0,
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
        ]

        def ok_resp(req, timeout=None):
            return _Resp(200, {"success": True})

        monkeypatch.setattr("urllib.request.urlopen", ok_resp)
        n = c.sync_readings(db)
        assert n == 1
        db.mark_synced.assert_called_once()


class TestAckCommand:
    def test_ack_with_error_message(self, monkeypatch):
        c = _make_client()
        captured = []

        def capture(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            return _Resp(200, {"success": True})

        monkeypatch.setattr("urllib.request.urlopen", capture)
        c.ack_command("cmd-123", "error", "relay not responding")
        assert len(captured) == 1
        assert captured[0]["commandId"] == "cmd-123"
        assert captured[0]["status"] == "error"
        assert captured[0]["error"] == "relay not responding"

    def test_ack_skips_empty_command_id(self, monkeypatch):
        c = _make_client()
        calls = []
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda req, timeout=None: calls.append(1)
        )
        c.ack_command("", "done")
        assert len(calls) == 0

    def test_ack_truncates_long_error(self, monkeypatch):
        c = _make_client()
        captured = []

        def capture(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            return _Resp(200, {"success": True})

        monkeypatch.setattr("urllib.request.urlopen", capture)
        c.ack_command("cmd-456", "error", "x" * 500)
        assert len(captured[0]["error"]) == 200


class TestStopMethod:
    def test_stop_is_noop(self):
        c = _make_client()
        c.stop()
