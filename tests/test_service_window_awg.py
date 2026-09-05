"""
Service Window — the AWG circuit page (smart breaker integration front end).

Everything here runs against a stubbed command socket: the page must render,
switch, and bind correctly without a firmware process, an Eaton account, or
any secret on disk. The firmware side of these commands is covered in
tests/test_smart_breaker_wiring.py.
"""

import sqlite3

import pytest
import yaml

from service_window.app import create_app
from service_window.health import smart_breaker_card
from service_window.routes import awg as awg_routes
from service_window.routes import status as status_routes

DEVICE_UUID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
SITE_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


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


@pytest.fixture
def commands(monkeypatch):
    """Stub the firmware command socket for both the AWG page and the dashboard.

    ``replies`` maps action -> reply; unknown actions get {"ok": False}.
    """
    sent: list[tuple[str, dict]] = []
    replies: dict[str, dict] = {}

    def fake_send(sock_path, action, **kwargs):
        sent.append((action, kwargs))
        return dict(replies.get(action, {"ok": False, "error": "no firmware"}))

    monkeypatch.setattr(awg_routes, "send_command", fake_send)
    monkeypatch.setattr(status_routes, "send_command", fake_send)
    return {"sent": sent, "replies": replies}


def write_config(path: str, **values) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(values, f)


def relay_only_config(channel: int) -> dict:
    return {"smart_breaker_vendor": "relay_only", "smart_breaker_interlock_relay": channel}


def read_config(path: str) -> dict:
    """Empty when nothing was written — a refused save must not create the file."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def live_status(**overrides) -> dict:
    base = {
        "ok": True,
        "configured": True,
        "vendor": "ableedge",
        "deviceId": DEVICE_UUID,
        "siteId": SITE_UUID,
        "circuitLabel": "Garage AWG",
        "circuitAmps": 20,
        "interlockRelay": 1,
        "failSafe": "off",
        "failSafeApplied": None,
        "linkOk": True,
        "unreachableForS": 0,
        "lastError": None,
        "desired": True,
        "pendingCommand": None,
        "breaker": {"isOn": True, "connected": True, "position": "close", "observedAt": 1.0},
        "power": {
            "currentA": 4.2,
            "voltageV": 240.0,
            "energyDeliveredWh": 1234.0,
            "observedAt": 1.0,
        },
    }
    base.update(overrides)
    return base


ABLEEDGE_CONFIG = dict(
    smart_breaker_vendor="ableedge",
    smart_breaker_device_id=DEVICE_UUID,
    smart_breaker_site_id=SITE_UUID,
    smart_breaker_circuit_label="Garage AWG",
    smart_breaker_circuit_amps=20,
    smart_breaker_interlock_relay=1,
    smart_breaker_fail_safe="off",
    smart_breaker_client_id="cid",
    smart_breaker_client_secret="shh",  # noqa: S106 - test fixture, not a credential
    smart_breaker_subscription_key="sub",
)


# --------------------------------------------------------------------------
# Navigation: only appears once something is bound
# --------------------------------------------------------------------------


class TestNavigation:
    def test_unbound_unit_has_no_awg_nav_entry(self, client, commands):
        body = client.get("/relays/").data
        assert b"AWG circuit</a>" not in body

    def test_bound_unit_gets_the_nav_entry(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        body = client.get("/relays/").data
        assert b"AWG circuit</a>" in body

    def test_relay_only_also_counts_as_bound(self, client, commands, config_path):
        write_config(config_path, **relay_only_config(2))
        assert b"AWG circuit</a>" in client.get("/relays/").data

    def test_settings_page_links_to_the_awg_page_when_unbound(self, client, commands):
        body = client.get("/settings/").data
        assert b"/awg/" in body

    def test_page_requires_login(self, app, commands):
        app.config["TESTING"] = True
        anon = app.test_client()
        resp = anon.get("/awg/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# --------------------------------------------------------------------------
# Page rendering
# --------------------------------------------------------------------------


class TestPage:
    def test_unbound_page_renders_binding_form_only(self, client, commands):
        commands["replies"]["awg_status"] = {"ok": True, "configured": False, "vendor": "none"}
        resp = client.get("/awg/")
        assert resp.status_code == 200
        assert b"No smart breaker is bound" in resp.data
        assert b"Save binding" in resp.data
        assert b"AWG ON" not in resp.data  # no switch controls without a controller

    def test_bound_page_shows_live_snapshot_and_switch(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        commands["replies"]["awg_status"] = live_status()
        resp = client.get("/awg/")
        assert resp.status_code == 200
        body = resp.data
        assert b"AWG ON" in body and b"AWG OFF" in body
        assert b"Garage AWG" in body
        assert b"4.2 A" in body
        assert b"240 V" in body
        assert b"1234 Wh" in body
        assert b"is ON and the breaker link is up" in body

    def test_secrets_are_never_rendered(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        commands["replies"]["awg_status"] = live_status()
        body = client.get("/awg/").data
        for secret in (b"cid", b"shh", b'"sub"'):
            assert secret not in body
        assert b"leave blank to keep" in body

    def test_firmware_down_reads_as_stale_not_fault(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        # no awg_status reply registered -> {"ok": False}
        body = client.get("/awg/").data
        assert b"monitoring service is not reporting" in body

    def test_relay_only_page_has_no_link_card(self, client, commands, config_path):
        write_config(config_path, **relay_only_config(3))
        commands["replies"]["awg_status"] = live_status(
            vendor="relay_only", linkOk=False, breaker=None, power=None, interlockRelay=3
        )
        body = client.get("/awg/").data
        assert b"Relay-only mode" in body
        assert b"interlock relay\n3" in body or b"relay 3" in body.lower()
        assert b"breaker link is up" not in body

    def test_status_json_mirrors_awg_status_plus_card(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        commands["replies"]["awg_status"] = live_status(linkOk=False, unreachableForS=120)
        data = client.get("/awg/status.json").get_json()
        assert data["configured"] is True
        assert data["linkOk"] is False
        assert data["card"]["status"] == "attention"
        assert "2 minutes" in data["card"]["message"]


# --------------------------------------------------------------------------
# Switching
# --------------------------------------------------------------------------


class TestSwitch:
    def test_api_on_goes_through_awg_set_with_service_window_source(self, client, commands):
        commands["replies"]["awg_set"] = {"ok": True, "breaker": "confirmed", "interlockOk": True}
        resp = client.post("/awg/api/set", json={"state": True, "reason": "bench check"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "confirmed" in data["message"]
        action, kwargs = commands["sent"][-1]
        assert action == "awg_set"
        assert kwargs == {"state": True, "source": "service_window", "reason": "bench check"}

    def test_api_rejects_non_boolean_state(self, client, commands):
        resp = client.post("/awg/api/set", json={"state": "maybe"})
        assert resp.status_code == 400
        assert commands["sent"] == []

    def test_api_accepts_on_off_strings(self, client, commands):
        commands["replies"]["awg_set"] = {"ok": True, "breaker": "n/a"}
        client.post("/awg/api/set", json={"state": "off"})
        assert commands["sent"][-1][1]["state"] is False

    def test_off_with_unconfirmed_breaker_explains_the_queue(self, client, commands):
        commands["replies"]["awg_set"] = {
            "ok": False,
            "breaker": "unconfirmed",
            "interlockOk": True,
            "error": "Unreachable: timeout",
        }
        data = client.post("/awg/api/set", json={"state": False}).get_json()
        assert data["ok"] is False
        assert "Interlock relay is off" in data["message"]
        assert "queued" in data["message"]

    def test_failed_on_is_reported_plainly(self, client, commands):
        commands["replies"]["awg_set"] = {
            "ok": False,
            "breaker": "unconfirmed",
            "interlockOk": True,
            "error": "Unreachable: timeout",
        }
        data = client.post("/awg/api/set", json={"state": True}).get_json()
        assert data["message"].startswith("Could not switch the AWG circuit on")

    def test_form_fallback_flashes_result(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        commands["replies"]["awg_status"] = live_status()
        commands["replies"]["awg_set"] = {"ok": True, "breaker": "confirmed", "interlockOk": True}
        resp = client.post("/awg/set", data={"state": "on", "reason": "x"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"switched on" in resp.data
        assert ("awg_set", {"state": True, "source": "service_window", "reason": "x"}) in commands[
            "sent"
        ]

    def test_form_without_state_does_nothing(self, client, commands):
        resp = client.post("/awg/set", data={}, follow_redirects=True)
        assert b"Choose ON or OFF" in resp.data
        assert all(a != "awg_set" for a, _ in commands["sent"])

    def test_unconfigured_firmware_error_surfaces(self, client, commands):
        commands["replies"]["awg_set"] = {
            "ok": False,
            "error": "smart breaker integration not configured",
        }
        data = client.post("/awg/api/set", json={"state": True}).get_json()
        assert data["ok"] is False
        assert "not configured" in data["message"]


# --------------------------------------------------------------------------
# Binding form
# --------------------------------------------------------------------------


def bind_form(**overrides) -> dict[str, str]:
    form = {
        "smart_breaker_vendor": "ableedge",
        "smart_breaker_site_id": SITE_UUID,
        "smart_breaker_device_id": DEVICE_UUID,
        "smart_breaker_circuit_label": "Garage AWG",
        "smart_breaker_circuit_amps": "20",
        "smart_breaker_interlock_relay": "1",
        "smart_breaker_fail_safe": "off",
        "smart_breaker_unreachable_grace_s": "300",
        "smart_breaker_poll_s": "60",
        "smart_breaker_client_id": "client-id",
        "smart_breaker_client_secret": "client-secret",
        "smart_breaker_subscription_key": "sub-key",
    }
    form.update(overrides)
    return form


class TestBinding:
    def test_full_ableedge_binding_is_written(self, client, commands, config_path):
        resp = client.post("/awg/bind", data=bind_form(), follow_redirects=True)
        assert resp.status_code == 200
        assert b"Binding saved" in resp.data
        assert b"Restart" in resp.data
        cfg = read_config(config_path)
        assert cfg["smart_breaker_vendor"] == "ableedge"
        assert cfg["smart_breaker_device_id"] == DEVICE_UUID
        assert cfg["smart_breaker_site_id"] == SITE_UUID
        assert cfg["smart_breaker_circuit_label"] == "Garage AWG"
        assert cfg["smart_breaker_circuit_amps"] == 20
        assert cfg["smart_breaker_interlock_relay"] == 1
        assert cfg["smart_breaker_fail_safe"] == "off"
        assert cfg["smart_breaker_client_id"] == "client-id"
        assert cfg["smart_breaker_client_secret"] == "client-secret"
        assert cfg["smart_breaker_subscription_key"] == "sub-key"

    def test_written_config_is_what_the_firmware_would_load(self, client, commands, config_path):
        """The values land as real strings/ints — the YAML `off` boolean trap
        the example config had to quote around must not reappear here."""
        client.post("/awg/bind", data=bind_form(), follow_redirects=True)
        from utils.config import validate_values

        cfg = read_config(config_path)
        accepted, errors = validate_values(
            {k: v for k, v in cfg.items() if k.startswith("smart_breaker_")}
        )
        assert errors == []
        assert accepted["smart_breaker_fail_safe"] == "off"
        assert accepted["smart_breaker_vendor"] == "ableedge"

    def test_blank_secret_keeps_existing_value(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        client.post(
            "/awg/bind",
            data=bind_form(
                smart_breaker_client_id="",
                smart_breaker_client_secret="",
                smart_breaker_subscription_key="",
                smart_breaker_circuit_label="Renamed",
            ),
            follow_redirects=True,
        )
        cfg = read_config(config_path)
        assert cfg["smart_breaker_client_secret"] == "shh"
        assert cfg["smart_breaker_client_id"] == "cid"
        assert cfg["smart_breaker_circuit_label"] == "Renamed"

    def test_ableedge_without_credentials_is_refused(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind",
            data=bind_form(
                smart_breaker_client_id="",
                smart_breaker_client_secret="",
                smart_breaker_subscription_key="",
            ),
            follow_redirects=True,
        )
        assert b"Eaton client ID is required" in resp.data
        assert b"Eaton client secret is required" in resp.data
        assert b"Eaton subscription key is required" in resp.data
        assert read_config(config_path) == {}

    def test_device_uuid_must_look_like_a_uuid(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind",
            data=bind_form(smart_breaker_device_id="SN-1234567"),
            follow_redirects=True,
        )
        assert b"does not look like a UUID" in resp.data
        assert read_config(config_path) == {}

    def test_device_uuid_required_for_ableedge(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind", data=bind_form(smart_breaker_device_id=""), follow_redirects=True
        )
        assert b"Device UUID is required" in resp.data
        assert read_config(config_path) == {}

    def test_site_uuid_is_optional_but_validated(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind", data=bind_form(smart_breaker_site_id="garage"), follow_redirects=True
        )
        assert b"Site UUID does not look like a UUID" in resp.data
        client.post("/awg/bind", data=bind_form(smart_breaker_site_id=""), follow_redirects=True)
        assert read_config(config_path)["smart_breaker_site_id"] == ""

    def test_ampacity_is_required_and_never_guessed(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind", data=bind_form(smart_breaker_circuit_amps=""), follow_redirects=True
        )
        assert b"Circuit ampacity is required" in resp.data
        assert read_config(config_path) == {}

    def test_schema_bounds_are_enforced(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind", data=bind_form(smart_breaker_circuit_amps="999"), follow_redirects=True
        )
        assert resp.status_code == 200
        assert read_config(config_path) == {}

    def test_unknown_fail_safe_is_refused(self, client, commands, config_path):
        client.post(
            "/awg/bind", data=bind_form(smart_breaker_fail_safe="maybe"), follow_redirects=True
        )
        assert read_config(config_path) == {}

    def test_non_numeric_number_is_a_friendly_error(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind", data=bind_form(smart_breaker_poll_s="soon"), follow_redirects=True
        )
        assert b"Poll interval: not a whole number" in resp.data
        assert read_config(config_path) == {}

    def test_relay_only_needs_a_channel(self, client, commands, config_path):
        resp = client.post(
            "/awg/bind",
            data=bind_form(
                smart_breaker_vendor="relay_only",
                smart_breaker_interlock_relay="0",
                smart_breaker_client_id="",
                smart_breaker_client_secret="",
                smart_breaker_subscription_key="",
            ),
            follow_redirects=True,
        )
        assert b"Relay-only mode needs an interlock relay" in resp.data
        assert read_config(config_path) == {}

    def test_relay_only_binding_needs_no_eaton_fields(self, client, commands, config_path):
        client.post(
            "/awg/bind",
            data=bind_form(
                smart_breaker_vendor="relay_only",
                smart_breaker_device_id="",
                smart_breaker_site_id="",
                smart_breaker_circuit_amps="",
                smart_breaker_interlock_relay="2",
                smart_breaker_client_id="",
                smart_breaker_client_secret="",
                smart_breaker_subscription_key="",
            ),
            follow_redirects=True,
        )
        cfg = read_config(config_path)
        assert cfg["smart_breaker_vendor"] == "relay_only"
        assert cfg["smart_breaker_interlock_relay"] == 2
        assert cfg["smart_breaker_circuit_amps"] == 0
        assert "smart_breaker_client_secret" not in cfg

    def test_unbinding_sets_vendor_none_and_keeps_secrets(self, client, commands, config_path):
        """Setting the vendor back to none disables the integration but does
        not wipe credentials — re-enabling later must not need Eaton again."""
        write_config(config_path, **ABLEEDGE_CONFIG)
        client.post(
            "/awg/bind",
            data=bind_form(
                smart_breaker_vendor="none",
                smart_breaker_client_id="",
                smart_breaker_client_secret="",
                smart_breaker_subscription_key="",
            ),
            follow_redirects=True,
        )
        cfg = read_config(config_path)
        assert cfg["smart_breaker_vendor"] == "none"
        assert cfg["smart_breaker_client_secret"] == "shh"

    def test_saving_does_not_hot_reload_restart_required_keys(self, client, commands, config_path):
        client.post("/awg/bind", data=bind_form(), follow_redirects=True)
        assert all(a != "config_reload" for a, _ in commands["sent"])

    def test_restart_button_sends_restart(self, client, commands):
        commands["replies"]["restart"] = {"ok": True}
        resp = client.post("/awg/restart", follow_redirects=True)
        assert b"Restarting" in resp.data
        assert ("restart", {}) in commands["sent"]


# --------------------------------------------------------------------------
# Dashboard card
# --------------------------------------------------------------------------


class TestDashboardCard:
    def test_unbound_dashboard_does_not_ask_the_firmware(self, client, commands):
        resp = client.get("/")
        assert resp.status_code == 200
        assert all(a != "awg_status" for a, _ in commands["sent"])
        assert b"AWG breaker" not in resp.data

    def test_bound_dashboard_shows_the_card(self, client, commands, config_path):
        write_config(config_path, **ABLEEDGE_CONFIG)
        commands["replies"]["awg_status"] = live_status()
        body = client.get("/").data
        assert b"AWG breaker" in body
        assert b"Garage AWG is ON and the breaker link is up" in body
        assert b"Open the AWG circuit page" in body

    def test_relay_only_dashboard_has_no_card(self, client, commands, config_path):
        write_config(config_path, **relay_only_config(1))
        body = client.get("/").data
        assert b"AWG breaker" not in body
        assert all(a != "awg_status" for a, _ in commands["sent"])


class TestSmartBreakerCard:
    """The pure mapping from snapshot -> traffic light."""

    def test_no_vendor_no_card(self):
        assert smart_breaker_card({}, live_status()) is None
        assert smart_breaker_card({"smart_breaker_vendor": "none"}, live_status()) is None

    def test_relay_only_no_card(self):
        assert smart_breaker_card({"smart_breaker_vendor": "relay_only"}, live_status()) is None

    def test_link_up_is_ok_with_position(self):
        card = smart_breaker_card({"smart_breaker_vendor": "ableedge"}, live_status())
        assert card["status"] == "ok"
        assert "Garage AWG is ON" in card["message"] or "The AWG circuit is ON" in card["message"]

    def test_label_from_config_when_snapshot_lacks_one(self):
        cfg = {"smart_breaker_vendor": "ableedge", "smart_breaker_circuit_label": "Shed AWG"}
        card = smart_breaker_card(cfg, live_status(breaker={"isOn": False}))
        assert card["message"].startswith("Shed AWG is OFF")

    def test_link_down_inside_grace_is_attention(self):
        card = smart_breaker_card(
            {"smart_breaker_vendor": "ableedge"},
            live_status(linkOk=False, unreachableForS=200, failSafeApplied=None),
        )
        assert card["status"] == "attention"
        assert "3 minutes" in card["message"]
        assert "OFF" in card["action"]

    def test_fail_safe_applied_is_fault(self):
        card = smart_breaker_card(
            {"smart_breaker_vendor": "ableedge"},
            live_status(linkOk=False, unreachableForS=900, failSafeApplied="off"),
        )
        assert card["status"] == "fault"
        assert "fail-safe (OFF) has been applied" in card["message"]

    def test_firmware_silent_is_stale(self):
        cfg = {"smart_breaker_vendor": "ableedge"}
        silent = (
            None,
            {"ok": False, "error": "connection refused"},
            {"ok": True, "configured": False},
        )
        for awg in silent:
            card = smart_breaker_card(cfg, awg)
            assert card["status"] == "fault"
            assert "monitoring service" in card["message"]
            assert "Restart" in card["action"]
