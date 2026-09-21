"""Tests for the installer setup wizard, plain-language health, and simple
settings pages (service window v2.2)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml


def make_app(tmp_path, mock_hardware, pin="1234", setup_completed=False, config_extra=None):
    from service_window.app import create_app
    from storage.database import WQM1Database

    db_path = str(tmp_path / "wqm1.db")
    db = WQM1Database(path=db_path)
    db.close()

    config = {"app_key": "A" * 32}
    if config_extra:
        config.update(config_extra)
    if setup_completed:
        config["service_window"] = {"setup_completed": True}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": db_path,
            "CONFIG_PATH": str(config_path),
            "CAL_PATH": str(tmp_path / "cal.yaml"),
            "CMD_SOCK": str(tmp_path / "cmd.sock"),
            "PIN": pin,
            "SECRET_KEY": "test-secret",
        }
    )
    return app


@pytest.fixture
def factory_client(tmp_path, mock_hardware):
    """Client for a factory-fresh unit (PIN 1234, setup not completed)."""
    app = make_app(tmp_path, mock_hardware)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["pin_verified"] = True
        yield c, app


@pytest.fixture
def commissioned_client(tmp_path, mock_hardware):
    """Client for a unit that finished setup (non-factory PIN)."""
    app = make_app(tmp_path, mock_hardware, pin="8642", setup_completed=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["pin_verified"] = True
        yield c, app


class TestForcedSetup:
    def test_factory_unit_redirects_everything_to_setup(self, factory_client):
        client, _ = factory_client
        for path in ["/", "/sensors/", "/calibrate/", "/settings/"]:
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 302, path
            assert "/setup" in resp.headers["Location"], path

    def test_setup_and_login_are_reachable_on_factory_unit(self, factory_client):
        client, _ = factory_client
        assert client.get("/setup/").status_code == 200
        assert client.get("/login").status_code == 200

    def test_commissioned_unit_is_not_redirected(self, commissioned_client):
        client, _ = commissioned_client
        assert client.get("/", follow_redirects=False).status_code == 200

    def test_changed_pin_alone_clears_the_redirect(self, tmp_path, mock_hardware):
        app = make_app(tmp_path, mock_hardware, pin="9753")
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            assert c.get("/", follow_redirects=False).status_code == 200


class TestPinStep:
    def test_rejects_factory_pin(self, factory_client):
        client, _ = factory_client
        resp = client.post(
            "/setup/pin", data={"pin": "1234", "pin_confirm": "1234"}, follow_redirects=True
        )
        assert b"factory PIN" in resp.data

    def test_rejects_mismatched_confirm(self, factory_client):
        client, _ = factory_client
        resp = client.post(
            "/setup/pin", data={"pin": "8642", "pin_confirm": "8643"}, follow_redirects=True
        )
        assert b"don&#39;t match" in resp.data or b"match" in resp.data

    def test_sets_pin_and_persists(self, factory_client):
        client, app = factory_client
        resp = client.post(
            "/setup/pin", data={"pin": "8642", "pin_confirm": "8642"}, follow_redirects=False
        )
        assert resp.status_code == 302
        assert app.config["PIN"] == "8642"
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["service_window"]["pin"] == "8642"
        # app_key from the base config must survive the nested merge
        assert saved["app_key"] == "A" * 32


class TestCloudStep:
    def test_valid_key_saved(self, factory_client):
        client, app = factory_client
        resp = client.post("/setup/cloud", data={"api_key": "k" * 20}, follow_redirects=False)
        assert resp.status_code == 302
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["api_key"] == "k" * 20
        assert saved["cloud_enabled"] is True

    def test_bad_key_rejected(self, factory_client):
        client, app = factory_client
        client.post("/setup/cloud", data={"api_key": "short"}, follow_redirects=True)
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert "api_key" not in saved

    def test_skip_moves_on_without_key(self, factory_client):
        client, app = factory_client
        resp = client.post("/setup/cloud", data={"skip": "1"}, follow_redirects=False)
        assert resp.status_code == 302
        assert "/setup/sensors" in resp.headers["Location"]


class TestNetworkStep:
    """The link check the wizard never had — see PR discussion on WiFi vs LoRa."""

    def test_network_step_is_in_the_wizard_before_cloud(self, factory_client):
        from service_window.routes.setup import STEPS

        assert STEPS.index("network") < STEPS.index("cloud")

    def test_page_reports_the_link(self, factory_client, monkeypatch):
        import utils.netinfo as netinfo

        monkeypatch.setattr(netinfo, "current_ssid", lambda: "PondHouse")
        monkeypatch.setattr(netinfo, "local_ip", lambda: "192.168.1.224")
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: -52)

        client, _ = factory_client
        resp = client.get("/setup/network")
        assert resp.status_code == 200
        assert b"PondHouse" in resp.data
        assert b"192.168.1.224" in resp.data
        assert b"-52 dBm" in resp.data

    def test_marginal_signal_tells_the_installer_to_move_it(self, factory_client, monkeypatch):
        import utils.netinfo as netinfo

        monkeypatch.setattr(netinfo, "current_ssid", lambda: "FarShed")
        monkeypatch.setattr(netinfo, "local_ip", lambda: "10.0.0.9")
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: -80)

        client, _ = factory_client
        resp = client.get("/setup/network")
        # The point of the step: an actionable instruction, before they leave.
        assert b"before you leave the site" in resp.data

    def test_offline_unit_still_renders_and_says_monitoring_continues(
        self, factory_client, monkeypatch
    ):
        import utils.netinfo as netinfo

        monkeypatch.setattr(netinfo, "current_ssid", lambda: None)
        monkeypatch.setattr(netinfo, "local_ip", lambda: None)
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: None)

        client, _ = factory_client
        resp = client.get("/setup/network")
        assert resp.status_code == 200
        assert b"Not connected" in resp.data
        assert b"monitoring" in resp.data.lower() or b"locally" in resp.data


class TestCloudKeyVerification:
    """Saving a key must prove it works, not just report 'saved'."""

    def _patch_probe(self, monkeypatch, state, detail="x"):
        import service_window.routes.setup as setup_mod  # noqa: F401
        import utils.netinfo as netinfo

        monkeypatch.setattr(
            netinfo,
            "verify_device_key",
            lambda *a, **k: {"state": state, "detail": detail, "status": None},
        )

    def test_accepted_key_reports_verified(self, factory_client, monkeypatch):
        self._patch_probe(monkeypatch, "ok")
        client, _ = factory_client
        resp = client.post("/setup/cloud", data={"api_key": "k" * 20}, follow_redirects=True)
        assert b"verified" in resp.data

    def test_rejected_key_warns_before_leaving(self, factory_client, monkeypatch):
        self._patch_probe(monkeypatch, "degraded", detail="The cloud rejected this key.")
        client, app = factory_client
        resp = client.post("/setup/cloud", data={"api_key": "k" * 20}, follow_redirects=True)
        assert b"rejected this key" in resp.data
        # Still saved: the installer may be fixing it on the cloud side, and
        # losing what they pasted would be worse than keeping a suspect key.
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["api_key"] == "k" * 20

    def test_unreachable_cloud_does_not_claim_the_key_is_bad(self, factory_client, monkeypatch):
        self._patch_probe(monkeypatch, "down")
        client, _ = factory_client
        resp = client.post("/setup/cloud", data={"api_key": "k" * 20}, follow_redirects=True)
        assert b"could not be reached" in resp.data
        assert b"rejected" not in resp.data


class TestLoRaCredentials:
    """A wizard-commissioned unit must be able to join, not just report.

    Before this, the wizard collected only the HTTP api_key, so every unit that
    finished setup kept app_key = 32 zeros — the same sentinel provision.py
    treats as 'never provisioned'. It could never complete an OTAA join.
    """

    KEY = "k" * 20
    APP_KEY = "b00d95c6cb0d65bc9e57fff1c27afa4b"

    def test_lora_credentials_are_saved_with_the_cloud_key(self, factory_client):
        client, app = factory_client
        client.post(
            "/setup/cloud",
            data={
                "api_key": self.KEY,
                "app_key": self.APP_KEY.upper(),
                "app_eui": "0000000000000000",
            },
            follow_redirects=False,
        )
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        # Stored lowercase so it round-trips through bytes.fromhex consistently.
        assert saved["app_key"] == self.APP_KEY
        assert saved["app_eui"] == "0000000000000000"

    def test_wifi_only_site_may_omit_them(self, factory_client):
        client, app = factory_client
        resp = client.post("/setup/cloud", data={"api_key": self.KEY}, follow_redirects=False)
        assert resp.status_code == 302
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["api_key"] == self.KEY

    def test_malformed_app_key_is_rejected_not_silently_stored(self, factory_client):
        client, app = factory_client
        resp = client.post(
            "/setup/cloud",
            data={"api_key": self.KEY, "app_key": "nothex"},
            follow_redirects=True,
        )
        assert b"32 hex characters" in resp.data
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved.get("app_key") != "nothex"

    def test_malformed_join_eui_is_rejected(self, factory_client):
        client, _ = factory_client
        resp = client.post(
            "/setup/cloud",
            data={"api_key": self.KEY, "app_eui": "123"},
            follow_redirects=True,
        )
        assert b"16 hex characters" in resp.data


class TestDoneStep:
    def test_finish_marks_setup_completed(self, factory_client):
        client, app = factory_client
        # A real walk sets the PIN first; finishing on the factory PIN is
        # refused (see the next test), so this one sets it the way the
        # wizard does before finishing.
        client.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
        resp = client.post("/setup/done", data={}, follow_redirects=False)
        assert resp.status_code == 302
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["service_window"]["setup_completed"] is True

    def test_cannot_finish_on_the_factory_pin(self, factory_client):
        """Commissioning plan test #2: the PIN step refused 1234 on its own
        page, but nothing stopped Finish with the shipped PIN still in
        place. A finished unit on PIN 1234 is one anyone on the site network
        can drive."""
        client, app = factory_client
        resp = client.post("/setup/done", data={}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/setup/pin")
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert not (saved.get("service_window") or {}).get("setup_completed")

    def test_go_no_go_page_renders_checklist(self, factory_client):
        client, _ = factory_client
        resp = client.get("/setup/done")
        assert resp.status_code == 200
        for subject in [b"Ph", b"Cloud", b"Lora", b"Gps", b"Storage"]:
            assert subject in resp.data
        assert b"Readings waiting to upload" in resp.data


class TestNetworkStepDeclaresTheVariant:
    """PR 4/5: the network step records what the unit IS, and can join."""

    def test_declare_writes_backhaul_and_fitted_radios(self, factory_client):
        client, app = factory_client
        resp = client.post(
            "/setup/network",
            data={"action": "declare", "backhaul": "none", "gps_enabled": "on"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/setup/cloud")
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["backhaul"] == "none"
        assert saved["gps_enabled"] is True
        # Unticked is a real declaration of "not fitted".
        assert saved["lora_enabled"] is False

    def test_declare_rejects_an_unknown_backhaul(self, factory_client):
        client, app = factory_client
        client.post("/setup/network", data={"action": "declare", "backhaul": "carrier-pigeon"})
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert "backhaul" not in saved

    def test_wrong_password_reraises_the_ap_and_says_so(self, factory_client, monkeypatch):
        """Commissioning plan test #4: an installer locked out by a typo is
        worse than no feature."""
        from utils import netctl

        calls = {}

        def fake_join(ssid, password, ap_ssid=None, ap_passphrase=None, **_):
            calls["args"] = (ssid, password, ap_ssid, ap_passphrase)
            return {
                "ok": False,
                "connected": False,
                "ap_restored": True,
                "error": "Secrets were required",
            }

        monkeypatch.setattr(netctl, "join_network", fake_join)
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))
        client, _ = factory_client
        resp = client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "wrong"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert calls["args"] == ("PondHouse", "wrong", "WQM1-0001", "river-cedar-42")
        assert b"WQM1-0001 is back" in resp.data

    def test_page_warns_before_a_join_when_served_over_the_ap(self, factory_client, monkeypatch):
        from utils import netctl, netinfo

        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [{"ssid": "PondHouse", "signal": 72, "secured": True}],
        )
        monkeypatch.setattr(
            netinfo,
            "wifi_status",
            lambda: {"state": "down", "ssid": None, "rssi_dbm": None, "ip": "192.168.4.1"},
        )
        client, _ = factory_client
        resp = client.get("/setup/network")
        assert resp.status_code == 200
        assert b"turns that setup network off" in resp.data
        assert b"PondHouse" in resp.data
        assert b"No network here" in resp.data


class TestWizardWalkedInOrder:
    """Commissioning plan test #1: a factory-fresh unit walks
    welcome→pin→identity→network→cloud→sensors→done IN ORDER and ends with
    every config key written and setup_completed true. Every step used to
    be tested alone against a fresh client; nothing had ever driven the
    whole wizard on one unit."""

    def test_factory_unit_walks_to_done_with_every_key_written(self, factory_client, monkeypatch):
        import utils.netinfo as netinfo

        monkeypatch.setattr(netinfo, "current_ssid", lambda: "PondHouse")
        monkeypatch.setattr(netinfo, "local_ip", lambda: "192.168.1.224")
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: -52)
        monkeypatch.setattr(
            netinfo, "verify_device_key", lambda *a, **k: {"state": "verified", "detail": ""}
        )
        client, app = factory_client
        cfg = lambda: yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())  # noqa: E731

        assert client.get("/setup/").status_code == 200
        # The wizard cannot be finished from here: still the factory PIN.
        r = client.post("/setup/done", data={}, follow_redirects=False)
        assert r.headers["Location"].endswith("/setup/pin")

        r = client.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
        assert r.headers["Location"].endswith("/setup/identity")
        assert client.get("/setup/identity").status_code == 200

        assert client.get("/setup/network").status_code == 200
        r = client.post(
            "/setup/network",
            data={"action": "declare", "backhaul": "wifi", "gps_enabled": "on"},
        )
        assert r.headers["Location"].endswith("/setup/cloud")

        r = client.post(
            "/setup/cloud",
            data={"api_key": "k" * 40, "app_key": "B" * 32, "app_eui": "70B3D57ED0000001"},
        )
        assert r.status_code == 302

        r = client.post("/setup/sensors", data={"ph_enabled": "on", "temperature_enabled": "on"})
        assert r.status_code == 302

        assert client.get("/setup/done").status_code == 200
        r = client.post("/setup/done", data={}, follow_redirects=False)
        assert r.status_code == 302

        saved = cfg()
        assert saved["service_window"]["pin"] == "8642"
        assert saved["service_window"]["setup_completed"] is True
        assert saved["backhaul"] == "wifi"
        assert saved["gps_enabled"] is True
        assert saved["lora_enabled"] is False
        assert saved["api_key"] == "k" * 40
        assert saved["cloud_enabled"] is True
        assert saved["app_key"].lower() == "b" * 32
        assert saved["ph_enabled"] is True and saved["temperature_enabled"] is True
        # Unticked probes are written false, not left absent.
        assert saved["tds_enabled"] is False and saved["turbidity_enabled"] is False


class TestStatusHealthPage:
    def test_home_shows_plain_language_cards(self, commissioned_client):
        client, _ = commissioned_client
        resp = client.get("/")
        assert resp.status_code == 200
        # No readings yet -> the pH card explains itself in plain English.
        assert b"health-dot" in resp.data
        assert b"no data" in resp.data or b"not being recorded" in resp.data


class TestSettingsPage:
    def test_saves_valid_simple_settings(self, commissioned_client):
        client, app = commissioned_client
        resp = client.post(
            "/settings/",
            data={"sensor_read_s": "120", "sync_interval_s": "600", "heartbeat_s": "900"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["sensor_read_s"] == 120
        assert saved["sync_interval_s"] == 600

    def test_rejects_out_of_range(self, commissioned_client):
        client, app = commissioned_client
        client.post("/settings/", data={"sensor_read_s": "1"}, follow_redirects=True)
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert "sensor_read_s" not in saved


class TestCalibrationStamping:
    def test_ph_calibration_records_calibrated_at(self, commissioned_client):
        client, app = commissioned_client
        resp = client.post(
            "/calibrate/ph", data={"v_ph4": "1.04", "v_ph7": "1.50"}, follow_redirects=False
        )
        assert resp.status_code == 302
        cal = yaml.safe_load(Path(app.config["CAL_PATH"]).read_text())
        assert "ph" in cal["calibrated_at"]

    def test_calibration_index_shows_age(self, commissioned_client):
        client, app = commissioned_client
        stamp = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        Path(app.config["CAL_PATH"]).write_text(yaml.safe_dump({"calibrated_at": {"ph": stamp}}))
        resp = client.get("/calibrate/")
        assert b"10 days ago" in resp.data
        assert b"Never calibrated" in resp.data  # the others


class TestHealthModule:
    def _readings(self, n=20, ph=None, minutes_ago_start=0):
        now = datetime.now(UTC)
        rows = []
        for i in range(n):
            ts = now - timedelta(minutes=minutes_ago_start + i)
            rows.append(
                {
                    "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ph": ph(i) if callable(ph) else ph,
                    "tds_ppm": 300.0 + i,
                    "turbidity_ntu": 4.0 + (i % 3),
                    "temp_c": 21.0 + i / 10,
                    "orp_mv": None,
                    "lat": 37.0,
                    "lon": -78.0,
                    "synced": 1,
                }
            )
        return rows

    def test_varied_readings_are_ok(self, mock_hardware):
        from service_window.health import sensor_cards

        cards = sensor_cards(self._readings(ph=lambda i: 7.0 + i / 20))
        assert cards["ph"]["status"] == "ok"
        assert cards["tds"]["status"] == "ok"
        assert cards["orp"]["status"] == "disabled"

    def test_flat_ph_is_fault_with_plain_language(self, mock_hardware):
        from service_window.health import sensor_cards

        cards = sensor_cards(self._readings(ph=7.0))
        assert cards["ph"]["status"] == "fault"
        assert "flat" in cards["ph"]["message"]
        assert cards["ph"]["action"]

    def test_stale_readings_flag_everything(self, mock_hardware):
        from service_window.health import sensor_cards, system_cards

        old = self._readings(ph=7.2, minutes_ago_start=120)
        cards = sensor_cards(old)
        assert cards["ph"]["status"] == "fault"
        sys_cards = system_cards(old, {"cloud_enabled": True, "api_key": "k"}, None, 20)
        assert sys_cards["storage"]["status"] == "fault"

    def test_system_cards_reflect_config_and_session(self, mock_hardware):
        from service_window.health import system_cards

        readings = self._readings(ph=7.0)
        cards = system_cards(
            readings,
            {"cloud_enabled": True, "api_key": "k"},
            {"joined": 1},
            reading_count=20,
        )
        assert cards["cloud"]["status"] == "ok"
        assert cards["lora"]["status"] == "ok"
        assert cards["gps"]["status"] == "ok"
        assert cards["storage"]["status"] == "ok"

    def test_cloud_unconfigured_is_fault(self, mock_hardware):
        from service_window.health import system_cards, worst_status

        cards = system_cards(self._readings(ph=7.0), {}, None, 20)
        assert cards["cloud"]["status"] == "fault"
        assert worst_status(cards) == "fault"

    # -- variant-aware verdicts (commissioning plan test #3) ----------------

    def test_wifi_only_unit_is_not_amber_on_lora(self, mock_hardware):
        from service_window.health import system_cards, worst_status

        cards = system_cards(
            self._readings(ph=7.0),
            {"cloud_enabled": True, "api_key": "k", "lora_enabled": False},
            None,  # never joined — there is no radio to join with
            20,
        )
        assert cards["lora"]["status"] == "disabled"
        assert "not fitted" in cards["lora"]["message"]
        assert worst_status(cards) == "ok"

    def test_no_gps_unit_is_not_amber_on_gps(self, mock_hardware):
        from service_window.health import system_cards, worst_status

        rows = self._readings(ph=7.0)
        for r in rows:
            r["lat"] = r["lon"] = None
        cards = system_cards(
            rows, {"cloud_enabled": True, "api_key": "k", "gps_enabled": False}, {"joined": 1}, 20
        )
        assert cards["gps"]["status"] == "disabled"
        assert worst_status(cards) == "ok"

    def test_declared_no_link_site_buffers_at_ok_and_states_the_queue(self, mock_hardware):
        from service_window.health import system_cards, worst_status

        rows = self._readings(ph=7.0)
        for r in rows:
            r["synced"] = 0
        cards = system_cards(
            rows,
            {"cloud_enabled": True, "api_key": "k", "backhaul": "none", "lora_enabled": False},
            None,
            reading_count=20,
            pending=17,
        )
        assert cards["cloud"]["status"] == "ok"
        assert "17 readings queued" in cards["cloud"]["message"]
        assert worst_status(cards) == "ok"

    def test_wifi_site_that_is_not_syncing_is_still_degraded(self, mock_hardware):
        """The buffering verdict is for a DECLARED dark site only. A Wi-Fi
        site that is not uploading is a problem and must say so."""
        from service_window.health import system_cards

        rows = self._readings(ph=7.0)
        for r in rows:
            r["synced"] = 0
        cards = system_cards(rows, {"cloud_enabled": True, "api_key": "k"}, None, 20, pending=17)
        assert cards["cloud"]["status"] == "attention"

    def test_lte_variant_gets_its_own_card(self, mock_hardware):
        from service_window.health import system_cards

        cards = system_cards(
            self._readings(ph=7.0),
            {"cloud_enabled": True, "api_key": "k", "backhaul": "lte"},
            None,
            20,
        )
        assert cards["lte"]["status"] == "ok"
        assert "lte" not in system_cards(
            self._readings(ph=7.0), {"cloud_enabled": True, "api_key": "k"}, None, 20
        )
