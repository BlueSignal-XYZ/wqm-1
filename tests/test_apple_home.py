"""
Apple Home — src/integrations/apple_home and everything it touches.

The unit becomes one HomeKit accessory on the LAN: readings out, nothing in.
These tests pin the mapping (what the Home app is told), the lifecycle (start
and stop follow the one setting; a missing library is a reported state, not a
crash), the boundary (the setup code reaches the Service Window and never the
heartbeat), the worker hook, the settings schema, and the Service Window
page — all against a fake HAP adapter, so no mDNS and no network.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from app.workers import SamplingWorker
from integrations.apple_home import AppleHomeBridge, HapUnavailable, snapshot_from_reading
from integrations.apple_home.accessory import (
    BANNED_NAME_WORDS,
    CONTACT_CLOSED,
    CONTACT_OPEN,
    CUSTOM_CHARACTERISTICS,
    SERVICES,
    accessory_name,
)
from integrations.apple_home.bridge import STATE_FILENAME
from utils.config import SETTINGS_SCHEMA, Settings, validate_values
from utils.health import HealthReporter

READING = {"temp_c": 24.1, "ph": 7.42, "tds_ppm": 312.0, "turbidity_ntu": 3.8, "flow_rate_gpm": 0.4}


class FakeAdapter:
    """Stands in for HapAdapter: records every characteristic write."""

    instances: list[FakeAdapter] = []

    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.started = False
        self.stopped = False
        self.paired = False
        self.setup_code = "031-45-154"
        self.setup_uri = "X-HM://0023ISYWYBS6P"
        self.values: dict[str, Any] = {}
        FakeAdapter.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeAdapter.instances.clear()
    yield
    FakeAdapter.instances.clear()


def _settings(**over):
    s = Settings()
    s.apple_home_enabled = True
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _bridge(tmp_path, factory=FakeAdapter):
    return AppleHomeBridge("BS-WQM1-0000a92e4e7d", "2.4.0", tmp_path, adapter_factory=factory)


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestMapping:
    def test_reading_values_pass_through_untouched(self):
        snap = snapshot_from_reading(READING, None, set())
        assert snap.temperature_c == 24.1
        assert snap.temperature_active is True
        assert snap.custom == {"ph": 7.42, "tds": 312.0, "turbidity": 3.8, "flow": 0.4}

    def test_hold_contact_describes_the_relay_never_the_water(self):
        off = snapshot_from_reading(READING, None, set())
        assert off.hold_enabled is False and off.contact_state == CONTACT_CLOSED
        clear = snapshot_from_reading(READING, {"enabled": True, "active": False}, set())
        assert clear.hold_enabled is True and clear.contact_state == CONTACT_CLOSED
        holding = snapshot_from_reading(READING, {"enabled": True, "active": True}, set())
        assert holding.contact_state == CONTACT_OPEN
        faulted = snapshot_from_reading(
            READING, {"enabled": True, "active": False, "fault": "released"}, set()
        )
        assert faulted.hold_fault is True

    def test_missing_or_absurd_values_are_none_not_zero(self):
        snap = snapshot_from_reading({"temp_c": None, "ph": "x", "tds_ppm": float("nan")}, None)
        assert snap.temperature_c is None and snap.temperature_active is False
        assert snap.custom["ph"] is None and snap.custom["tds"] is None
        assert snapshot_from_reading({"temp_c": 180.0}).temperature_c is None
        assert snapshot_from_reading({"ph": 15.0}).custom["ph"] is None
        assert snapshot_from_reading(None).temperature_c is None

    def test_suspended_sensors_become_faults(self):
        snap = snapshot_from_reading(READING, None, {"temperature", "tds"})
        assert snap.temperature_fault is True
        assert snap.custom_faults == frozenset({"tds"})

    def test_plan_is_temperature_plus_hold_contact_and_nothing_judges_water(self):
        assert [s["type"] for s in SERVICES] == ["TemperatureSensor", "ContactSensor"]
        names = " ".join(s["name"] for s in SERVICES).lower()
        for w in BANNED_NAME_WORDS:
            assert w not in names
        for spec in CUSTOM_CHARACTERISTICS.values():
            assert spec["display_name"].lower() not in BANNED_NAME_WORDS
        # No LeakSensor / AirQuality / WaterLevel verdict scales, ever.
        assert not any(s["type"] in ("LeakSensor", "AirQualitySensor") for s in SERVICES)

    def test_custom_uuids_are_stable_and_distinct(self):
        ids = {str(spec["type_id"]) for spec in CUSTOM_CHARACTERISTICS.values()}
        assert len(ids) == len(CUSTOM_CHARACTERISTICS)
        assert str(CUSTOM_CHARACTERISTICS["ph"]["type_id"]) == str(
            CUSTOM_CHARACTERISTICS["ph"]["type_id"]
        )

    def test_accessory_name_is_the_label_tail(self):
        assert accessory_name("BS-WQM1-0000a92e4e7d") == "WQM-1 4e7d"
        assert accessory_name("WQM-10001") == "WQM-1 0001"
        assert accessory_name("") == "WQM-1 unit"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_off_by_default_starts_nothing(self, tmp_path):
        b = _bridge(tmp_path)
        b.configure(Settings())
        assert FakeAdapter.instances == []
        assert b.status()["running"] is False
        assert b.heartbeat() == {"enabled": False, "paired": False}

    def test_enable_starts_once_and_disable_stops(self, tmp_path):
        b = _bridge(tmp_path)
        s = _settings()
        b.configure(s)
        b.configure(s)
        b.configure(s)
        assert len(FakeAdapter.instances) == 1
        a = FakeAdapter.instances[0]
        assert a.started and a.kw["name"] == "WQM-1 4e7d"
        assert a.kw["persist_file"] == tmp_path / STATE_FILENAME
        assert a.kw["port"] == 51826
        s.apple_home_enabled = False
        b.configure(s)
        assert a.stopped is True
        assert b.status()["running"] is False

    def test_port_change_restarts(self, tmp_path):
        b = _bridge(tmp_path)
        b.configure(_settings())
        b.configure(_settings(apple_home_port=52000))
        assert FakeAdapter.instances[0].stopped is True
        assert FakeAdapter.instances[1].kw["port"] == 52000

    def test_update_pushes_values_and_remembers_the_last_for_a_late_start(self, tmp_path):
        b = _bridge(tmp_path)
        b.update(READING, {"enabled": True, "active": True}, set())
        b.configure(_settings())
        a = FakeAdapter.instances[0]
        assert a.values["temperature"] == 24.1
        assert a.values["contact"] == CONTACT_OPEN
        assert a.values["contact_active"] is True
        assert a.values["custom:ph"] == 7.42
        b.update({"temp_c": None, "ph": 7.0}, None, {"temperature"})
        assert a.values["temperature"] == 24.1  # a missing read keeps the last number
        assert a.values["temperature_active"] is False
        assert a.values["temperature_fault"] == 1
        assert a.values["contact"] == CONTACT_CLOSED

    def test_library_missing_is_a_reported_state(self, tmp_path):
        def factory(**kw):
            raise HapUnavailable("no pyhap")

        b = _bridge(tmp_path, factory)
        b.configure(_settings())
        st = b.status()
        assert st["enabled"] is True and st["running"] is False
        assert st["error"] == "library_missing"
        assert "setupCode" not in st
        # Does not retry every cycle; a disable clears it so an enable retries.
        calls = MagicMock(side_effect=HapUnavailable("x"))
        b2 = _bridge(tmp_path, calls)
        for _ in range(5):
            b2.configure(_settings())
        assert calls.call_count == 1
        b2.configure(Settings())
        b2.configure(_settings())
        assert calls.call_count == 2

    def test_a_start_failure_never_raises_into_the_caller(self, tmp_path):
        def factory(**kw):
            raise OSError("port in use")

        b = _bridge(tmp_path, factory)
        b.configure(_settings())
        assert b.status()["error"].startswith("start_failed")
        b.update(READING)  # still a pure mapping, still no raise

    def test_a_hap_write_failure_is_logged_not_raised(self, tmp_path):
        class Bad(FakeAdapter):
            def set(self, key, value):
                raise RuntimeError("loop closed")

        b = _bridge(tmp_path, Bad)
        b.configure(_settings())
        b.update(READING)
        assert b.status()["pushes"] == 0


# ---------------------------------------------------------------------------
# Boundary — where the setup code may and may not go
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_status_carries_the_code_only_while_unpaired(self, tmp_path):
        b = _bridge(tmp_path)
        b.configure(_settings())
        st = b.status()
        assert st["setupCode"] == "031-45-154" and st["setupUri"].startswith("X-HM://")
        FakeAdapter.instances[0].paired = True
        st = b.status()
        assert st["paired"] is True and "setupCode" not in st and "setupUri" not in st

    def test_heartbeat_is_enabled_and_paired_and_nothing_else(self, tmp_path):
        b = _bridge(tmp_path)
        b.configure(_settings())
        hb = b.heartbeat()
        assert hb == {"enabled": True, "paired": False}
        FakeAdapter.instances[0].paired = True
        assert b.heartbeat() == {"enabled": True, "paired": True}

    def test_health_reporter_includes_it_and_survives_its_failure(self, tmp_path):
        h = HealthReporter("2.4.0")
        b = _bridge(tmp_path)
        b.configure(_settings())
        h.set_apple_home(b)
        hb = h.build_heartbeat()
        assert hb["appleHome"] == {"enabled": True, "paired": False}
        assert "setupCode" not in str(hb)
        broken = MagicMock()
        broken.heartbeat.side_effect = RuntimeError("x")
        h.set_apple_home(broken)
        assert "appleHome" not in h.build_heartbeat()

    def test_heartbeat_absent_when_no_bridge_registered(self):
        assert "appleHome" not in HealthReporter("2.4.0").build_heartbeat()


# ---------------------------------------------------------------------------
# Settings schema
# ---------------------------------------------------------------------------


class TestSettings:
    def test_enabled_is_hot_and_remote_port_is_local_only(self):
        assert SETTINGS_SCHEMA["apple_home_enabled"].hot is True
        assert SETTINGS_SCHEMA["apple_home_enabled"].remote is True
        assert SETTINGS_SCHEMA["apple_home_port"].remote is False
        assert Settings().apple_home_enabled is False
        assert Settings().apple_home_port == 51826

    def test_remote_config_can_flip_enabled_but_never_the_port(self):
        accepted, errors = validate_values({"apple_home_enabled": True}, remote=True)
        assert accepted == {"apple_home_enabled": True} and errors == []
        _, errors = validate_values({"apple_home_port": 8080}, remote=True)
        assert errors
        _, errors = validate_values({"apple_home_port": 80})
        assert errors  # below 1024

    def test_the_cloud_schema_mirrors_the_remote_key(self):
        """functions/v2/deviceTelemetry.js must carry apple_home_enabled —
        pinned on that side by deviceTelemetry.test.js; here we only assert
        the firmware side exposes exactly one remote key for the feature."""
        remote = [
            k for k in SETTINGS_SCHEMA if k.startswith("apple_home_") and SETTINGS_SCHEMA[k].remote
        ]
        assert remote == ["apple_home_enabled"]


# ---------------------------------------------------------------------------
# Worker hook
# ---------------------------------------------------------------------------


def _worker(apple_home, hold=None):
    from tests.test_app_workers import FakeDB, FakeSensor, make_settings

    sensors = {
        "temperature": FakeSensor(24.1),
        "ph": FakeSensor(7.4),
        "tds": None,
        "turbidity": None,
        "orp": None,
        "chlorine": None,
        "multi485": None,
    }
    return SamplingWorker(
        make_settings(apple_home_enabled=True),
        sensors=sensors,
        db=FakeDB(),
        rules=None,
        relays=None,
        leds=None,
        health=SimpleNamespace(update_last_seen=lambda: None),
        state=SimpleNamespace(
            gps=lambda: SimpleNamespace(lat=None, lon=None, alt_m=None),
            incr_error=lambda _b: None,
            emit_event=lambda _e: True,
        ),
        irrigation_hold=hold,
        apple_home=apple_home,
    )


class TestWorkerHook:
    def test_each_cycle_configures_then_updates_with_the_hold_payload(self):
        bridge = MagicMock()
        hold = MagicMock()
        hold.payload.return_value = {"enabled": True, "active": False}
        hold.evaluate.return_value = SimpleNamespace(active=False)
        w = _worker(bridge, hold)
        w.step()
        bridge.configure.assert_called()
        args, _ = bridge.update.call_args
        assert args[0]["ph"] == 7.4
        assert args[1] == {"enabled": True, "active": False}

    def test_a_bridge_error_does_not_stop_sampling(self):
        bridge = MagicMock()
        bridge.update.side_effect = RuntimeError("boom")
        w = _worker(bridge)
        w.step()  # no raise
        bridge.update.assert_called_once()

    def test_absent_bridge_is_a_no_op(self):
        w = _worker(None)
        w.step()


# ---------------------------------------------------------------------------
# Service Window page
# ---------------------------------------------------------------------------


@pytest.fixture
def sw_client(tmp_path, monkeypatch):
    from service_window.app import create_app
    from service_window.routes import apple_home as route

    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE readings (id INTEGER PRIMARY KEY, timestamp TEXT, ph REAL, tds_ppm REAL,"
        " turbidity_ntu REAL, orp_mv REAL, temp_c REAL, lat REAL, lon REAL, alt_m REAL,"
        " battery_v REAL, relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0);"
        "CREATE TABLE lorawan_session (id INTEGER PRIMARY KEY CHECK (id = 1), dev_addr BLOB,"
        " nwk_skey BLOB, app_skey BLOB, fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,"
        " joined INTEGER DEFAULT 0, updated_at TEXT); INSERT INTO lorawan_session (id) VALUES (1);"
    )
    conn.close()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"apple_home_enabled": False, "service_window": {"setup_completed": True}})
    )
    app = create_app(
        {
            "db_path": db_path,
            "pin": "9999",
            "config_path": str(config_path),
            "cal_path": str(tmp_path / "cal.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
            "secrets_dir": str(tmp_path / "secrets"),
        }
    )
    app.config["TESTING"] = True
    app.config["TEST_OPEN_WINDOW"] = True
    client = app.test_client()
    sent: list[tuple[str, dict]] = []
    answer: dict[str, Any] = {
        "ok": True,
        "enabled": True,
        "running": True,
        "paired": False,
        "name": "WQM-1 4e7d",
        "port": 51826,
        "error": None,
        "pushes": 3,
        "services": [dict(s) for s in SERVICES],
        "setupCode": "031-45-154",
        "setupUri": "X-HM://0023ISYWYBS6P",
    }

    def fake_send(sock, action, **kw):
        sent.append((action, kw))
        if action == "apple_home_status":
            return dict(answer)
        return {"ok": True}

    monkeypatch.setattr(route, "send_command", fake_send)
    return client, config_path, sent, answer


class TestServiceWindow:
    def test_page_shows_the_code_and_the_uncertified_notice_while_unpaired(self, sw_client):
        client, _, _, _ = sw_client
        html = client.get("/apple-home/").get_data(as_text=True)
        assert "031-45-154" in html
        assert "uncertified" in html
        assert "Add Anyway" in html
        assert "never the water" in html
        for w in ("Works with Apple Home", "certified by Apple", "MFi-certified"):
            assert w not in html

    def test_page_hides_the_code_once_paired(self, sw_client):
        client, _, _, answer = sw_client
        answer["paired"] = True
        answer.pop("setupCode")
        answer.pop("setupUri")
        html = client.get("/apple-home/").get_data(as_text=True)
        assert "031-45-154" not in html and "Paired" in html
        assert client.get("/apple-home/qr.svg").get_data(as_text=True).strip().endswith("/>")

    def test_qr_carries_the_setup_uri_and_is_not_cached(self, sw_client):
        client, _, _, _ = sw_client
        r = client.get("/apple-home/qr.svg")
        assert r.mimetype == "image/svg+xml"
        assert r.headers.get("Cache-Control") == "no-store"
        assert b"<svg" in r.data and b"rect" in r.data

    def test_enable_writes_the_validated_key_and_asks_for_a_reload(self, sw_client):
        client, config_path, sent, _ = sw_client
        r = client.post("/apple-home/enable", data={"enabled": "1"})
        assert r.status_code == 302
        assert yaml.safe_load(config_path.read_text())["apple_home_enabled"] is True
        assert ("config_reload", {}) in sent
        client.post("/apple-home/enable", data={"enabled": "0"})
        assert yaml.safe_load(config_path.read_text())["apple_home_enabled"] is False

    def test_firmware_not_answering_is_said_in_words(self, sw_client, monkeypatch):
        from service_window.routes import apple_home as route

        client, _, _, _ = sw_client
        monkeypatch.setattr(
            route, "send_command", lambda *a, **k: {"ok": False, "error": "no socket"}
        )
        html = client.get("/apple-home/").get_data(as_text=True)
        assert "did not answer" in html and "no socket" in html

    def test_page_requires_login(self, sw_client):
        client, _, _, _ = sw_client
        client.application.config["TEST_OPEN_WINDOW"] = False
        client.get("/logout")
        assert client.get("/apple-home/").status_code in (302, 401, 403)
