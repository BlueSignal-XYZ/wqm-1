"""
Home Assistant — src/integrations/home_assistant and everything it touches.

The unit publishes MQTT discovery configs and one state document per cycle to
the owner's broker: readings out, nothing in. These tests pin the mapping
(what Home Assistant is told), the lifecycle (connect and stop follow the
settings; a missing library or a missing host is a reported state, not a
crash), the boundary (the password reaches neither the status nor the
heartbeat; the unit subscribes to nothing), the worker hook, the settings
schema, and the Service Window page — all against a fake MQTT adapter, so no
socket is ever opened.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.workers import SamplingWorker
from integrations.home_assistant import (
    ENTITIES,
    HOLD_ENTITY,
    HomeAssistantPublisher,
    MqttUnavailable,
    discovery_messages,
    state_payload,
    topics_for,
)
from integrations.home_assistant.discovery import BANNED_NAME_WORDS, device_name
from utils.config import SETTINGS_SCHEMA, Settings, validate_values
from utils.health import HealthReporter

READING = {
    "temp_c": 24.1,
    "ph": 7.42,
    "tds_ppm": 312.0,
    "turbidity_ntu": 3.8,
    "flow_rate_gpm": 0.4,
    "flow_total_gal": 1234.5,
}
DEVICE = "BS-WQM1-0000a92e4e7d"
SRC = Path(__file__).resolve().parents[1] / "src" / "integrations" / "home_assistant"


class FakeAdapter:
    """Stands in for MqttAdapter: records every publish, never a socket."""

    instances: list[FakeAdapter] = []

    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.started = False
        self.stopped = False
        self.connected = False
        self.last_error: str | None = None
        self.published: list[tuple[str, str, bool]] = []
        FakeAdapter.instances.append(self)

    def start(self) -> None:
        self.started = True
        self.connected = True

    def stop(self) -> None:
        self.stopped = True
        self.connected = False

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


@pytest.fixture(autouse=True)
def _reset():
    FakeAdapter.instances.clear()
    yield
    FakeAdapter.instances.clear()


def settings(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "mqtt_enabled": True,
        "mqtt_host": "homeassistant.local",
        "mqtt_port": 0,
        "mqtt_username": "wqm1",
        "mqtt_password": "hunter2-secret",
        "mqtt_tls": False,
        "mqtt_discovery_prefix": "homeassistant",
    }
    base.update(over)
    return SimpleNamespace(**base)


def publisher(**over: Any) -> HomeAssistantPublisher:
    p = HomeAssistantPublisher(DEVICE, "2.4.0", adapter_factory=FakeAdapter)
    p.configure(settings(**over))
    return p


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestMapping:
    def test_state_is_the_readings_own_numbers_and_nothing_else(self):
        s = state_payload(READING, {"enabled": True, "active": False}, set())
        assert s == {
            "temperature": 24.1,
            "ph": 7.42,
            "tds": 312.0,
            "turbidity": 3.8,
            "flow_rate": 0.4,
            "flow_total": 1234.5,
            "irrigation_hold": "clear",
        }

    def test_hold_describes_the_contact_never_the_water(self):
        assert state_payload(READING, {"enabled": True, "active": True})["irrigation_hold"] == (
            "holding"
        )
        assert "irrigation_hold" not in state_payload(READING, None)
        assert "irrigation_hold" not in state_payload(READING, {"enabled": False, "active": True})

    def test_missing_absurd_or_nan_values_are_absent_not_zero(self):
        s = state_payload({"temp_c": float("nan"), "ph": 99, "tds_ppm": "x"}, None)
        assert "temperature" not in s and "ph" not in s and "tds" not in s
        assert 0 not in s.values()

    def test_suspended_sensor_is_left_out_and_named_as_a_fault(self):
        s = state_payload(READING, None, {"turbidity", "flow"})
        assert "turbidity" not in s and "flow_rate" not in s and "flow_total" not in s
        assert s["faults"] == ["turbidity", "flow_rate", "flow_total"]

    def test_every_reading_column_has_an_entity_with_a_unit(self):
        from control.rules import RulesEngine

        columns = set(RulesEngine._SENSOR_TO_COLUMN.values()) | {"flow_total_gal"}
        assert {e["column"] for e in ENTITIES} == columns
        for e in ENTITIES:
            assert e["unit"], e["key"]
            assert e["state_class"] in ("measurement", "total_increasing")
        assert [e for e in ENTITIES if e["key"] == "flow_total"][0]["state_class"] == (
            "total_increasing"
        )

    def test_nothing_names_a_verdict(self):
        names = [e["name"] for e in ENTITIES] + [HOLD_ENTITY["name"], device_name(DEVICE)]
        for n in names:
            assert not any(w in n.lower() for w in BANNED_NAME_WORDS), n
        for _, payload in discovery_messages(DEVICE, "2.4.0"):
            assert not any(w in payload.lower() for w in BANNED_NAME_WORDS), payload

    def test_discovery_is_one_retained_config_per_entity_on_one_state_topic(self):
        msgs = discovery_messages(DEVICE, "2.4.0")
        assert len(msgs) == len(ENTITIES) + 1
        t = topics_for(DEVICE)
        topics = [m[0] for m in msgs]
        assert topics[-1] == (
            "homeassistant/binary_sensor/wqm1_bs_wqm1_0000a92e4e7d/irrigation_hold/config"
        )
        assert all(x.startswith("homeassistant/sensor/") for x in topics[:-1])
        for _, payload in msgs:
            cfg = json.loads(payload)
            assert cfg["state_topic"] == t["state"]
            assert cfg["availability_topic"] == t["availability"]
            assert cfg["device"]["manufacturer"] == "BlueSignal XYZ, Inc."
            assert cfg["device"]["model"] == "WQM-1"
            assert cfg["device"]["sw_version"] == "2.4.0"
            assert cfg["unique_id"].startswith("wqm1_")
            assert "command_topic" not in cfg
        temp = json.loads(msgs[0][1])
        assert temp["device_class"] == "temperature" and temp["unit_of_measurement"] == "°C"
        assert temp["value_template"] == "{{ value_json.temperature | default('') }}"
        hold = json.loads(msgs[-1][1])
        assert hold["payload_on"] == "holding" and hold["payload_off"] == "clear"

    def test_prefix_is_honoured_and_node_id_is_topic_safe(self):
        t = topics_for("BS/WQM1 #1", "/custom/")
        assert t["discovery_prefix"] == "custom"
        assert re.fullmatch(r"wqm1_[a-z0-9_]+", t["node"])
        assert "/" not in t["node"] and "#" not in t["node"]

    def test_device_name_is_the_label_tail(self):
        assert device_name(DEVICE) == "WQM-1 4e7d"
        assert device_name("") == "WQM-1 unit"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_off_by_default_starts_nothing(self):
        p = HomeAssistantPublisher(DEVICE, "2.4.0", adapter_factory=FakeAdapter)
        p.configure(settings(mqtt_enabled=False))
        assert FakeAdapter.instances == []
        assert p.heartbeat() == {"enabled": False, "connected": False}

    def test_enable_connects_once_and_disable_stops(self):
        p = publisher()
        p.configure(settings())
        p.configure(settings())
        assert len(FakeAdapter.instances) == 1
        a = FakeAdapter.instances[0]
        assert a.started and a.kw["host"] == "homeassistant.local" and a.kw["port"] == 1883
        assert a.kw["will_topic"] == topics_for(DEVICE)["availability"]
        p.configure(settings(mqtt_enabled=False))
        assert a.stopped
        assert p.heartbeat() == {"enabled": False, "connected": False}

    def test_tls_defaults_the_port_to_8883(self):
        publisher(mqtt_tls=True)
        assert FakeAdapter.instances[0].kw["port"] == 8883
        assert FakeAdapter.instances[0].kw["tls"] is True

    def test_a_broker_change_reconnects(self):
        p = publisher()
        p.configure(settings(mqtt_host="10.0.0.5"))
        assert len(FakeAdapter.instances) == 2
        assert FakeAdapter.instances[0].stopped
        assert FakeAdapter.instances[1].kw["host"] == "10.0.0.5"

    def test_no_host_is_a_reported_state_and_connects_to_nothing(self):
        p = publisher(mqtt_host="")
        assert FakeAdapter.instances == []
        assert p.status()["error"] == "no_host"
        p.configure(settings())  # the host arrives
        assert len(FakeAdapter.instances) == 1
        assert p.status()["error"] is None

    def test_first_connected_cycle_announces_then_every_cycle_publishes_state(self):
        p = publisher()
        a = FakeAdapter.instances[0]
        p.update(READING, {"enabled": True, "active": False}, set())
        retained = [m for m in a.published if m[2]]
        assert len(retained) == len(ENTITIES) + 2  # configs + availability
        assert (topics_for(DEVICE)["availability"], "online", True) in a.published
        states = [m for m in a.published if m[0] == topics_for(DEVICE)["state"]]
        assert len(states) == 1 and json.loads(states[0][1])["temperature"] == 24.1
        p.update(READING, None, set())
        assert len([m for m in a.published if m[2]]) == len(ENTITIES) + 2  # announced once
        assert len([m for m in a.published if m[0] == topics_for(DEVICE)["state"]]) == 2
        assert p.status()["pushes"] == 2 and p.status()["announced"] is True

    def test_not_yet_connected_publishes_nothing_and_does_not_count(self):
        p = publisher()
        a = FakeAdapter.instances[0]
        a.connected = False
        p.update(READING)
        assert a.published == [] and p.status()["pushes"] == 0

    def test_reconnect_announces_again(self):
        p = publisher()
        p.update(READING)
        p.configure(settings(mqtt_host="10.0.0.9"))
        b = FakeAdapter.instances[1]
        p.update(READING)
        assert len([m for m in b.published if m[2]]) == len(ENTITIES) + 2

    def test_library_missing_is_a_reported_state(self):
        def missing(**kw):
            raise MqttUnavailable("paho-mqtt is not installed")

        p = HomeAssistantPublisher(DEVICE, "2.4.0", adapter_factory=missing)
        p.configure(settings())
        assert p.status()["error"] == "library_missing"
        p.update(READING)  # does not raise
        p.configure(settings(mqtt_enabled=False))
        assert p.status()["error"] is None

    def test_a_start_failure_never_raises_into_the_caller(self):
        def boom(**kw):
            raise OSError("port in use")

        p = HomeAssistantPublisher(DEVICE, "2.4.0", adapter_factory=boom)
        p.configure(settings())
        assert p.status()["error"].startswith("start_failed")
        assert p.heartbeat() == {"enabled": True, "connected": False}

    def test_a_publish_failure_is_logged_not_raised(self):
        p = publisher()
        a = FakeAdapter.instances[0]

        def bad(*_a, **_k):
            raise RuntimeError("broker gone")

        a.publish = bad  # type: ignore[method-assign]
        p.update(READING)
        assert p.status()["pushes"] == 0


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_status_names_the_broker_and_never_the_password(self):
        s = publisher().status()
        assert s["host"] == "homeassistant.local" and s["port"] == 1883
        assert s["usernameSet"] is True and s["passwordSet"] is True
        assert "hunter2-secret" not in json.dumps(s)
        assert "password" not in json.dumps(s).replace("passwordSet", "")

    def test_heartbeat_is_enabled_and_connected_and_nothing_else(self):
        p = publisher()
        assert p.heartbeat() == {"enabled": True, "connected": True}

    def test_health_reporter_includes_it_and_survives_its_failure(self, tmp_path):
        hr = HealthReporter("2.4.0")
        p = publisher()
        hr.set_home_assistant(p)
        hb = hr.build_heartbeat()
        assert hb["homeAssistant"] == {"enabled": True, "connected": True}
        assert "hunter2-secret" not in json.dumps(hb)
        hr.set_home_assistant(SimpleNamespace(heartbeat=lambda: 1 / 0))
        assert "homeAssistant" not in hr.build_heartbeat()

    def test_the_unit_subscribes_to_nothing(self):
        for f in SRC.glob("*.py"):
            src = f.read_text()
            assert ".subscribe(" not in src, f
            assert "on_message" not in src, f
            assert "command_topic" not in src.replace('"command_topic" not in', ""), f

    def test_the_real_adapter_exists_and_takes_no_subscription(self):
        """paho is installed in CI; the adapter must construct without a
        socket and expose nothing that receives a message."""
        pytest.importorskip("paho.mqtt.client")
        from integrations.home_assistant.publisher import MqttAdapter

        a = MqttAdapter(
            host="127.0.0.1",
            port=1883,
            username="u",
            password="p",
            tls=False,
            client_id="wqm1_test",
            will_topic="bluesignal/wqm1/wqm1_test/availability",
        )
        assert a.connected is False
        assert not hasattr(a, "subscribe") and not hasattr(a, "on_message")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_enabled_is_the_only_remote_key(self):
        remote = [k for k in SETTINGS_SCHEMA if k.startswith("mqtt_") and SETTINGS_SCHEMA[k].remote]
        assert remote == ["mqtt_enabled"]
        assert SETTINGS_SCHEMA["mqtt_enabled"].hot is True

    def test_remote_config_can_flip_enabled_but_never_name_a_broker(self):
        ok, errs = validate_values({"mqtt_enabled": True}, remote=True)
        assert ok == {"mqtt_enabled": True} and errs == []
        for key in ("mqtt_host", "mqtt_password", "mqtt_port", "mqtt_tls", "mqtt_username"):
            _, errs = validate_values({key: "x" if key != "mqtt_port" else 1}, remote=True)
            assert errs and "not remotely configurable" in errs[0], key

    def test_defaults_are_off_and_empty(self):
        s = Settings()
        assert s.mqtt_enabled is False and s.mqtt_host == "" and s.mqtt_password == ""
        assert s.mqtt_discovery_prefix == "homeassistant"


# ---------------------------------------------------------------------------
# Worker hook
# ---------------------------------------------------------------------------


def _worker(home_assistant, hold=None):
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
        make_settings(mqtt_enabled=True),
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
        home_assistant=home_assistant,
    )


class TestWorkerHook:
    def test_each_cycle_configures_then_updates_with_the_hold_payload(self):
        calls: list[tuple[str, Any]] = []
        pub = SimpleNamespace(
            configure=lambda s: calls.append(("configure", s)),
            update=lambda r, h, susp: calls.append(("update", (r, h, susp))),
        )
        hold = SimpleNamespace(
            configure=lambda s: None,
            evaluate=lambda r, s: None,
            payload=lambda: {"enabled": True, "active": True},
        )
        w = _worker(pub, hold)
        w.step()
        assert [c[0] for c in calls] == ["configure", "update"]
        reading, hold_payload, _ = calls[1][1]
        assert reading["temp_c"] == 24.1 and hold_payload == {"enabled": True, "active": True}

    def test_a_publisher_error_does_not_stop_sampling(self):
        pub = SimpleNamespace(configure=lambda s: 1 / 0, update=lambda *a: None)
        w = _worker(pub)
        w.step()
        assert len(w._db.readings) == 1

    def test_absent_publisher_is_a_no_op(self):
        w = _worker(None)
        w.step()
        assert len(w._db.readings) == 1


# ---------------------------------------------------------------------------
# Service Window page
# ---------------------------------------------------------------------------


@pytest.fixture
def sw_client(tmp_path, monkeypatch):
    from service_window import auth
    from service_window.app import create_app
    from service_window.routes import home_assistant as route

    # The login throttle is per-process; a run of Service Window suites
    # would otherwise lock the test client out after five logins.
    monkeypatch.setattr(auth, "_attempts", {})
    monkeypatch.setattr(auth, "_failures", {})
    monkeypatch.setattr(auth, "_lockouts", {})

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
    config_path.write_text(yaml.safe_dump({"mqtt_enabled": False, "mqtt_password": "keep-me"}))
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
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["pin_verified"] = True
    sent: list[tuple[str, dict]] = []
    answer: dict[str, Any] = {"ok": True, **publisher().status()}

    def fake_send(sock, action, **kw):
        sent.append((action, kw))
        if action == "home_assistant_status":
            return dict(answer)
        return {"ok": True}

    monkeypatch.setattr(route, "send_command", fake_send)
    return client, config_path, sent, answer


class TestServiceWindow:
    def test_page_shows_the_broker_the_entities_and_never_a_password(self, sw_client):
        client, _, _, _ = sw_client
        html = client.get("/home-assistant/").get_data(as_text=True)
        assert "homeassistant.local:1883" in html
        assert "Water temperature" in html and "Irrigation hold" in html
        assert "never the water" in html
        assert "keep-me" not in html and "hunter2-secret" not in html
        assert "set — leave blank to keep" in html

    def test_save_writes_validated_keys_keeps_the_password_and_asks_for_a_reload(self, sw_client):
        client, config_path, sent, _ = sw_client
        r = client.post(
            "/home-assistant/save",
            data={
                "mqtt_enabled": "on",
                "mqtt_host": "10.0.0.2",
                "mqtt_port": "",
                "mqtt_username": "wqm1",
                "mqtt_password": "",
                "mqtt_discovery_prefix": "",
            },
        )
        assert r.status_code == 302
        cfg = yaml.safe_load(config_path.read_text())
        assert cfg["mqtt_enabled"] is True and cfg["mqtt_host"] == "10.0.0.2"
        assert cfg["mqtt_port"] == 0 and cfg["mqtt_discovery_prefix"] == "homeassistant"
        assert cfg["mqtt_password"] == "keep-me"
        assert ("config_reload", {}) in sent

    def test_save_sets_and_clears_the_password(self, sw_client):
        client, config_path, _, _ = sw_client
        client.post(
            "/home-assistant/save",
            data={"mqtt_host": "10.0.0.2", "mqtt_password": "new-one", "mqtt_port": "8883"},
        )
        assert yaml.safe_load(config_path.read_text())["mqtt_password"] == "new-one"
        client.post("/home-assistant/save", data={"mqtt_host": "10.0.0.2", "clear_password": "on"})
        assert yaml.safe_load(config_path.read_text())["mqtt_password"] == ""

    def test_enabling_without_a_host_is_refused(self, sw_client):
        client, config_path, sent, _ = sw_client
        client.post("/home-assistant/save", data={"mqtt_enabled": "on", "mqtt_host": ""})
        assert yaml.safe_load(config_path.read_text())["mqtt_enabled"] is False
        assert ("config_reload", {}) not in sent

    def test_a_bad_port_is_refused_by_the_schema(self, sw_client):
        client, config_path, _, _ = sw_client
        client.post("/home-assistant/save", data={"mqtt_host": "h", "mqtt_port": "70000"})
        assert "mqtt_host" not in yaml.safe_load(config_path.read_text())

    def test_firmware_not_answering_is_said_in_words(self, sw_client, monkeypatch):
        from service_window.routes import home_assistant as route

        client, _, _, _ = sw_client
        monkeypatch.setattr(route, "send_command", lambda *a, **k: None)
        html = client.get("/home-assistant/").get_data(as_text=True)
        assert "did not answer" in html

    def test_page_requires_login(self, sw_client):
        client, _, _, _ = sw_client
        client.get("/logout")
        assert client.get("/home-assistant/").status_code in (302, 401, 403)
