"""Tests for the RS485 probe setup + calibration pages."""

import sqlite3

import pytest
import yaml

from service_window.app import create_app


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL,
            temp_c REAL, lat REAL, lon REAL, alt_m REAL,
            battery_v REAL, relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
        );
        CREATE TABLE lorawan_session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            dev_addr BLOB, nwk_skey BLOB, app_skey BLOB,
            fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,
            joined INTEGER DEFAULT 0, updated_at TEXT
        );
        INSERT INTO lorawan_session (id) VALUES (1);
        """
    )
    conn.close()
    return path


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / "config.yaml")


@pytest.fixture
def app(db_path, tmp_path, config_path):
    return create_app(
        {
            "db_path": db_path,
            "pin": "9999",
            "config_path": config_path,
            "cal_path": str(tmp_path / "calibration.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
        }
    )


@pytest.fixture
def client(app):
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["pin_verified"] = True
    return client


class FakeBus:
    def __init__(self, scan_result=None, single_address=None, probe_ok=True):
        self.scan_result = scan_result or []
        self.single_address = single_address
        self.probe_ok = probe_ok
        self.closed = False

    def scan(self, addresses):
        return self.scan_result

    def query_single_address(self):
        return self.single_address

    def probe(self, address):
        return self.probe_ok

    def close(self):
        self.closed = True


class FakeProbe:
    def __init__(self, bus=None, address=1):
        self.address = address
        self.calls: list[tuple] = []

    def set_address(self, new_address):
        self.calls.append(("set_address", new_address))
        self.address = new_address

    def read(self):
        return 0.35

    def read_ad(self):
        return 512

    def read_all(self):
        return {
            "ph": 6.86,
            "conductivity_uscm": 100.0,
            "temp_c": 25.0,
            "tds_ppm": 50.0,
            "salinity_ppt": 0.5,
        }

    def calibrate_zero(self):
        self.calls.append(("zero",))

    def calibrate_slope(self, reference):
        self.calls.append(("slope", reference))

    def calibrate_ph_point(self, buffer):
        self.calls.append(("ph_point", buffer))

    def set_ec_slope(self, slope):
        self.calls.append(("ec_slope", slope))


@pytest.fixture
def fake_bus(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr("service_window.routes.rs485._make_bus", lambda _config: bus)
    return bus


@pytest.fixture
def fake_probe(monkeypatch):
    probe = FakeProbe()
    monkeypatch.setattr("service_window.routes.rs485._make_probe", lambda _t, _b, address: probe)
    return probe


def write_config(config_path, **values):
    with open(config_path, "w") as f:
        yaml.safe_dump(values, f)


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


class TestIndexAndScan:
    def test_index_renders_unconfigured_probes(self, client, fake_bus):
        resp = client.get("/rs485/")
        assert resp.status_code == 200
        assert b"Not set up" in resp.data
        assert b"Residual chlorine" in resp.data

    def test_scan_lists_responding_addresses(self, client, fake_bus):
        fake_bus.scan_result = [1, 3]
        resp = client.post("/rs485/scan")
        assert resp.status_code == 200
        assert b"Probes answered" in resp.data
        assert fake_bus.closed

    def test_scan_empty_bus_gives_wiring_hint(self, client, fake_bus):
        resp = client.post("/rs485/scan")
        assert b"Nothing answered" in resp.data

    def test_requires_login(self, app):
        client = app.test_client()
        resp = client.get("/rs485/")
        assert resp.status_code == 302


class TestAssign:
    def test_assign_readdresses_and_enables_probe(self, client, fake_bus, fake_probe, config_path):
        fake_bus.single_address = 1
        resp = client.post(
            "/rs485/assign",
            data={"probe_type": "chlorine", "new_address": "4"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert ("set_address", 4) in fake_probe.calls
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config["rs485_chlorine_enabled"] is True
        assert config["rs485_chlorine_addr"] == 4

    def test_assign_same_address_skips_write_but_enables(
        self, client, fake_bus, fake_probe, config_path
    ):
        fake_bus.single_address = 4
        client.post(
            "/rs485/assign",
            data={"probe_type": "orp", "new_address": "4"},
            follow_redirects=True,
        )
        assert fake_probe.calls == []  # already there - no bus write
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config["rs485_orp_enabled"] is True

    def test_assign_with_silent_bus_changes_nothing(
        self, client, fake_bus, fake_probe, config_path, tmp_path
    ):
        fake_bus.single_address = None
        resp = client.post(
            "/rs485/assign",
            data={"probe_type": "chlorine", "new_address": "4"},
            follow_redirects=True,
        )
        assert b"No probe answered" in resp.data
        import os

        assert not os.path.exists(config_path) or not (load_yaml(config_path) or {}).get(
            "rs485_chlorine_enabled"
        )

    def test_assign_rejects_bad_address(self, client, fake_bus, fake_probe):
        resp = client.post(
            "/rs485/assign",
            data={"probe_type": "chlorine", "new_address": "999"},
            follow_redirects=True,
        )
        assert b"between 1 and 247" in resp.data

    def test_assign_page_suggests_free_address(self, client, config_path):
        write_config(config_path, rs485_chlorine_enabled=True, rs485_chlorine_addr=1)
        resp = client.get("/rs485/assign")
        assert b'value="2"' in resp.data  # 1 is taken


class TestChlorineCalibration:
    def test_redirects_when_probe_not_set_up(self, client, fake_bus, fake_probe):
        resp = client.get("/rs485/calibrate/chlorine", follow_redirects=True)
        assert b"Set up the chlorine probe first" in resp.data

    def test_live_reading_shown(self, client, fake_bus, fake_probe, config_path):
        write_config(config_path, rs485_chlorine_enabled=True, rs485_chlorine_addr=4)
        resp = client.get("/rs485/calibrate/chlorine")
        assert resp.status_code == 200
        assert b"0.350" in resp.data
        assert b"512" in resp.data

    def test_zero_point(self, client, fake_bus, fake_probe, config_path):
        write_config(config_path, rs485_chlorine_enabled=True, rs485_chlorine_addr=4)
        resp = client.post(
            "/rs485/calibrate/chlorine", data={"action": "zero"}, follow_redirects=True
        )
        assert ("zero",) in fake_probe.calls
        assert b"Zero point saved" in resp.data

    def test_slope_stamps_calibrated_at(self, client, fake_bus, fake_probe, config_path, tmp_path):
        write_config(config_path, rs485_chlorine_enabled=True, rs485_chlorine_addr=4)
        client.post(
            "/rs485/calibrate/chlorine",
            data={"action": "slope", "reference_mgl": "1.0"},
            follow_redirects=True,
        )
        assert ("slope", 1.0) in fake_probe.calls
        cal = load_yaml(tmp_path / "calibration.yaml")
        assert "chlorine" in cal["calibrated_at"]

    def test_slope_requires_reference(self, client, fake_bus, fake_probe, config_path):
        write_config(config_path, rs485_chlorine_enabled=True, rs485_chlorine_addr=4)
        resp = client.post(
            "/rs485/calibrate/chlorine", data={"action": "slope"}, follow_redirects=True
        )
        assert b"lab-verified" in resp.data
        assert not any(c[0] == "slope" for c in fake_probe.calls)


class TestMultiCalibration:
    def test_ph_buffer_point(self, client, fake_bus, fake_probe, config_path, tmp_path):
        write_config(config_path, rs485_multi_enabled=True, rs485_multi_addr=3)
        client.post(
            "/rs485/calibrate/multi",
            data={"action": "ph_point", "buffer": "6.86"},
            follow_redirects=True,
        )
        assert ("ph_point", "6.86") in fake_probe.calls
        cal = load_yaml(tmp_path / "calibration.yaml")
        assert "ph" in cal["calibrated_at"]

    def test_unknown_buffer_shows_error(self, client, fake_bus, fake_probe, config_path):
        fake_probe.calibrate_ph_point = lambda b: (_ for _ in ()).throw(
            ValueError(f"unknown pH buffer: {b!r}")
        )
        write_config(config_path, rs485_multi_enabled=True, rs485_multi_addr=3)
        resp = client.post(
            "/rs485/calibrate/multi",
            data={"action": "ph_point", "buffer": "7.77"},
            follow_redirects=True,
        )
        assert b"unknown pH buffer" in resp.data

    def test_ec_slope(self, client, fake_bus, fake_probe, config_path, tmp_path):
        write_config(config_path, rs485_multi_enabled=True, rs485_multi_addr=3)
        client.post(
            "/rs485/calibrate/multi",
            data={"action": "ec_slope", "slope": "1.2"},
            follow_redirects=True,
        )
        assert ("ec_slope", 1.2) in fake_probe.calls
        cal = load_yaml(tmp_path / "calibration.yaml")
        assert "conductivity" in cal["calibrated_at"]

    def test_live_values_rendered(self, client, fake_bus, fake_probe, config_path):
        write_config(config_path, rs485_multi_enabled=True, rs485_multi_addr=3)
        resp = client.get("/rs485/calibrate/multi")
        assert b"6.86" in resp.data
        assert b"100" in resp.data


class TestRs485SystemCard:
    """The bus gets ONE system card, and only when RS485 probes are enabled."""

    def _readings(self, **fields):
        from datetime import UTC, datetime

        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = {"timestamp": ts, "ph": 7.0, "synced": 1, "lat": 1.0, "lon": 2.0}
        base.update(fields)
        return [dict(base) for _ in range(10)]

    def _cards(self, readings, config):
        from service_window.health import system_cards

        return system_cards(readings, config, {"joined": True}, len(readings))

    def test_no_card_when_no_rs485_probes(self):
        cards = self._cards(self._readings(), {})
        assert "rs485" not in cards

    def test_ok_when_all_enabled_probes_answer(self):
        config = {"rs485_chlorine_enabled": True, "rs485_multi_enabled": True}
        cards = self._cards(self._readings(chlorine_mgl=0.3, conductivity_uscm=480.0), config)
        assert cards["rs485"]["status"] == "ok"

    def test_degraded_when_one_probe_silent(self):
        config = {"rs485_chlorine_enabled": True, "rs485_multi_enabled": True}
        cards = self._cards(self._readings(chlorine_mgl=0.3, conductivity_uscm=None), config)
        assert cards["rs485"]["status"] == "attention"

    def test_down_when_nothing_answers(self):
        config = {"rs485_chlorine_enabled": True}
        cards = self._cards(self._readings(chlorine_mgl=None), config)
        assert cards["rs485"]["status"] == "fault"
        assert "12V" in (cards["rs485"]["likelyCause"] or "")
