"""Field installer scenarios for Saturday flash → join → Cloud claim.

Pins current behaviour of the flash/boot/setup/Wi-Fi/claim/heartbeat/poller
and OTA paths so a bench run is not the first time a gap shows up. Nothing
here talks to the live unit ``BS-WQM1-0000a92e4e7d`` or to production Cloud.
OTA hold is unchanged: tests never target a release.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

from app.state import StateStore
from app.workers import (
    CloudSyncWorker,
    DeviceRegisterWorker,
    HeartbeatWorker,
    SamplingWorker,
)
from cloud.client import CloudClient
from cloud.device_commands import (
    ANALOG_BUFFERS,
    COMMAND_TYPES,
    PH_PROBES,
    PH_STEPS,
    RS485_BUFFER_LABELS,
    RS485_OPS,
    WIRE_SENSORS,
)
from cloud.register import (
    REGISTER_PATH,
    attempt_register,
    build_register_body,
    kick_register,
)
from tests.test_app_workers import FakeDB, FakeSensor, make_settings
from tests.test_device_onboarding_v2 import VECTOR_DEVICE_ID, _write_seed
from tests.test_netctl import FakeNm
from tests.test_ota_agent import DOWNLOAD_URL, Env, FakeOpener, make_bundle, make_target
from utils import join, netctl
from utils.atomic import atomic_write_text
from utils.device_keys import (
    ensure_keypair,
    identity_qr_payload,
    load_http_key,
    load_register_state,
    private_key_path,
    public_key_b64url,
    store_http_key,
    store_register_state,
)
from utils.identity import SETUP_SSID
from utils.wifi_setup import WEAK_RSSI_DBM, block_code, is_weak_rssi

# Live POST /v2/devices/register {} on 2026-11-02 returned:
# {"code":"invalid_body","error":"device_id, pk, fw, ts and sig are required."}
CLOUD_REGISTER_REQUIRED = frozenset({"device_id", "pk", "fw", "ts", "sig"})
# Cloud dashboard DeviceDetailPage chunk (Last-Modified Tue 27 Oct 2026).
CLOUD_PH_PROBES = frozenset({"analog", "rs485_multi"})
CLOUD_ANALOG_BUFFERS = frozenset({4.0, 7.0})
CLOUD_RS485_BUFFERS = frozenset({4.01, 6.86, 9.18})
CLOUD_RS485_SENSORS = frozenset({"residual_chlorine", "RD-ORP-WE-01", "RD-PETSTS-01"})
CLOUD_RS485_OPS = frozenset({"add", "set_address", "enable", "disable"})
CLOUD_COMMAND_TYPES = frozenset({"ph_calibrate", "rs485_config"})
CLOUD_PH_STEPS = frozenset({"start", "point", "finish"})


def _clock():
    t = [0.0]

    def clock():
        return t[0]

    def sleep(seconds):
        t[0] += float(seconds)

    return t, clock, sleep


def _patch_register_http(monkeypatch, status: int, body: dict | None):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return json.dumps(body or {}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def urlopen(req, timeout=None):
        if status >= 400:
            url = getattr(req, "full_url", "https://example.test")
            err = HTTPError(url, status, "err", {}, None)

            def _read():
                return json.dumps(body or {}).encode()

            err.read = _read  # type: ignore[method-assign]
            raise err
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)


class TestCloudCommissioningContract:
    def test_register_body_fields_match_live_cloud_required_list(self, tmp_path):
        _write_seed(tmp_path)
        body = build_register_body(device_id=VECTOR_DEVICE_ID, fw="2.4.0", ts=1)
        assert set(body) == CLOUD_REGISTER_REQUIRED
        assert REGISTER_PATH == "/v2/devices/register"

    def test_firmware_self_registers_and_does_not_claim(self):
        blob = "".join(path.read_text() for path in Path("src/cloud").glob("*.py"))
        assert REGISTER_PATH in blob
        assert "/v2/devices/claim" not in blob

    def test_identity_qr_has_no_claim_token(self, tmp_path):
        _write_seed(tmp_path)
        payload = identity_qr_payload()
        assert payload["v"] == 2
        assert "device_id" in payload
        assert "pk" in payload
        assert "claim_token" not in payload
        assert "claimToken" not in payload

    def test_cloud_dashboard_command_names_match_firmware(self):
        assert COMMAND_TYPES == CLOUD_COMMAND_TYPES
        assert PH_PROBES == CLOUD_PH_PROBES
        assert PH_STEPS == CLOUD_PH_STEPS
        assert ANALOG_BUFFERS == CLOUD_ANALOG_BUFFERS
        assert set(RS485_BUFFER_LABELS) == CLOUD_RS485_BUFFERS
        assert set(WIRE_SENSORS) == CLOUD_RS485_SENSORS
        assert RS485_OPS == CLOUD_RS485_OPS

    def test_heartbeat_posts_and_pending_gets_with_x_api_key(self, monkeypatch):
        captured: list[dict] = []

        def fake_urlopen(req, timeout=None):
            captured.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "api_key": req.headers.get("X-api-key") or req.headers.get("X-API-Key"),
                }
            )
            raise urllib.error.URLError("blocked")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_base="https://example.test/app",
            api_key="bench-device-key",
            fw_version="2.4.0",
            max_retries=1,
        )
        client._sleep = lambda *_: None
        client.send_heartbeat({"uptimeS": 1})
        client.fetch_pending_commands()
        by = {c["url"].rstrip("/").rsplit("/", 1)[-1]: c for c in captured}
        assert "heartbeat" in by
        assert "pending" in by
        assert by["heartbeat"]["method"] == "POST"
        assert by["pending"]["method"] == "GET"
        assert all(c["api_key"] == "bench-device-key" for c in captured)


class TestPowerPulledMidJoin:
    def test_joining_state_without_the_lock_is_interrupted(self):
        join.ensure_state_dir()
        join.state_path().write_text(
            json.dumps({"state": "joining", "ssid": "PondHouse", "boot_id": "this-boot"})
        )
        state = join.read_state()
        assert state["state"] == "failed"
        assert state["reason"] == "interrupted"

    def test_phone_leaving_does_not_cancel_a_running_join(self, monkeypatch):
        release = threading.Event()
        started: list[str] = []

        def slow_join(ssid, *a, **k):
            started.append(ssid)
            release.wait(5)
            return {
                "ok": True,
                "connected": True,
                "ip": "192.168.1.8",
                "ap_restored": False,
            }

        monkeypatch.setattr(netctl, "join_network", slow_join)
        monkeypatch.setattr(netctl, "usable_ipv4", lambda ip: True)
        monkeypatch.setattr(
            netctl, "forget_saved_wifi", lambda keep, iface="wlan0": {"forgotten": []}
        )
        first = join.start_join("PondHouse", "hunter2222", ap_ssid=SETUP_SSID)
        assert first == {"state": "joining", "ssid": "PondHouse"}
        deadline = time.monotonic() + 2
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started == ["PondHouse"]
        # The phone dropped BlueSignal Setup. The join worker is still running.
        assert join.read_state()["state"] == "joining"
        release.set()
        assert join.wait_for_follow_up(timeout_s=2)

    def test_firstboot_resumes_the_interrupted_step(self):
        text = Path("tools/golden/overlay/usr/local/sbin/wqm1-firstboot").read_text()
        assert "resuming ${step} after power loss" in text
        assert "mark_step" in text


class TestWrongPasswordAndNoInternet:
    def test_wrong_password_restores_the_open_setup_network(self, monkeypatch, tmp_path):
        fake = FakeNm()
        fake.ap = True
        fake.join_rc = 4
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: fake.reach)
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        result = netctl.join_network(
            "PondHouse",
            "wrong-pass",
            ap_ssid=SETUP_SSID,
            sleep=lambda _s: None,
        )
        assert result["ok"] is False
        assert result["reason"] == "wrong_password"
        assert result["ap_restored"] is True
        assert fake.ap is True

    def test_address_without_bluesignal_is_no_bluesignal(self, monkeypatch, tmp_path):
        fake = FakeNm()
        fake.ap = True
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: True)
        monkeypatch.setattr(netctl, "reach_bluesignal", lambda *a, **k: {"ok": False})
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        result = netctl.join_network(
            "PondHouse",
            "hunter22",
            ap_ssid=SETUP_SSID,
            api_base="https://example.test/app",
            sleep=lambda _s: None,
        )
        assert result["ok"] is False
        assert result["reason"] == "no_bluesignal"
        assert result["ap_restored"] is True

    def test_address_without_a_gateway_is_no_internet(self, monkeypatch, tmp_path):
        fake = FakeNm()
        fake.ap = True
        _t, clock, sleep = _clock()
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: False)
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        result = netctl.join_network(
            "PondHouse",
            "hunter22",
            ap_ssid=SETUP_SSID,
            sleep=sleep,
            clock=clock,
        )
        assert result["ok"] is False
        assert result["reason"] == "no_internet"
        assert result["ap_restored"] is True
        assert fake.ap is True


class TestCaptivePortalAndRadioLimits:
    def test_sign_in_page_is_captive_and_restores_the_setup_network(self, monkeypatch, tmp_path):
        fake = FakeNm()
        fake.ap = True
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: True)
        monkeypatch.setattr(netctl, "probe_sign_in_page", lambda *a, **k: "captive")
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        result = netctl.join_network(
            "PondHouse",
            "hunter22",
            ap_ssid=SETUP_SSID,
            sleep=lambda _s: None,
        )
        assert result["ok"] is False
        assert result["reason"] == "captive_sign_in"
        assert result["ap_restored"] is True

    def test_5ghz_only_is_refused_before_the_hotspot_drops(self):
        assert block_code({"band": "5", "security": "wpa2"}) == "refused_band"
        assert block_code({"band": "2.4", "security": "wpa2"}) is None

    def test_weak_signal_is_below_minus_70(self):
        assert WEAK_RSSI_DBM == -70
        assert is_weak_rssi(-70) is False
        assert is_weak_rssi(-71) is True


class TestRouterAndPasswordChangeAfterInstall:
    def test_retry_does_nothing_when_the_hotspot_is_down(self, monkeypatch, tmp_path):
        """A commissioned unit on a dead station does not raise the AP until reboot."""
        fake = FakeNm()
        fake.ap = False
        fake.saved = ["PondHouse"]
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        result = netctl.retry_saved_station(SETUP_SSID, None, sleep=lambda _s: None)
        assert result == {"skipped": "hotspot not up"}
        assert not any(c[1:3] == ["connection", "up"] for c in fake.calls)

    def test_power_cycle_raises_the_setup_network_when_the_saved_station_is_dead(
        self, monkeypatch, tmp_path
    ):
        fake = FakeNm()
        fake.ap = False
        fake.saved = ["PondHouse"]
        fake.station_up_rc = 4
        _t, clock, sleep = _clock()
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        result = netctl.ensure_reachable(SETUP_SSID, None, grace_s=5, sleep=sleep, clock=clock)
        assert result["mode"] == "ap"
        assert result["ap"] is True
        assert fake.ap is True

    def test_wifi_retry_timer_is_five_minutes(self):
        text = Path("systemd/bluesignal-wifi-retry.timer").read_text()
        assert "OnUnitInactiveSec=5min" in text
        assert "OnBootSec=6min" in text


class TestReadOnlyFilesystem:
    def test_atomic_write_survives_directory_fsync_failure(self, tmp_path, monkeypatch):
        dest = tmp_path / "config.yaml"
        real_open = os.open

        def open_dir(path, flags, *args):
            if Path(path) == dest.parent:
                raise OSError("Invalid argument")
            return real_open(path, flags, *args)

        monkeypatch.setattr(os, "open", open_dir)
        atomic_write_text(dest, "cloud_enabled: true\n")
        assert dest.read_text() == "cloud_enabled: true\n"

    def test_join_state_write_failure_does_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            join,
            "atomic_write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("Read-only file system")),
        )
        join.ensure_state_dir()
        join._write_state({"state": "joining", "ssid": "PondHouse"})


class TestClockNotSet:
    def test_unsynced_clock_is_stamped_on_the_reading_not_dropped(self, mock_hardware):
        worker = SamplingWorker(
            make_settings(),
            sensors={
                "temperature": FakeSensor(21.5),
                "ph": FakeSensor(7.2),
                "tds": FakeSensor(340.0),
                "turbidity": FakeSensor(3.0),
            },
            db=FakeDB(),
            rules=None,
            relays=SimpleNamespace(get_state_bitmask=lambda: 0),
            leds=None,
            health=SimpleNamespace(update_last_seen=lambda: None),
            state=StateStore(),
        )
        worker.step()
        assert worker._db.readings[0]["clock_source"] == "unsynced"

    def test_register_200_without_api_key_stays_retryable(self, tmp_path, monkeypatch):
        _write_seed(tmp_path)
        _patch_register_http(monkeypatch, 200, {"ok": True})
        state = attempt_register(
            api_base="https://example.test/app", config_path=str(tmp_path / "c.yaml")
        )
        assert state["status"] == "error"
        assert state["last_code"] == "missing_api_key"
        assert load_http_key() == ""


class TestCloudUnreachable:
    def test_register_5xx_stays_pending(self, tmp_path, monkeypatch):
        _write_seed(tmp_path)
        _patch_register_http(monkeypatch, 503, {"error": "unavailable"})
        state = attempt_register(api_base="https://example.test/app")
        assert state["status"] == "pending"
        assert state["last_http"] == 503

    def test_kick_register_swallows_exceptions(self, monkeypatch):
        monkeypatch.setattr(
            "cloud.register.attempt_register",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("dns")),
        )
        state = kick_register(api_base="https://example.test/app")
        assert state["status"] == "pending"

    def test_heartbeat_and_sync_idle_without_a_device_key(self, mock_hardware):
        synced: list[int] = []
        cloud = SimpleNamespace(
            _api_key="",
            sync_readings=lambda db: synced.append(1) or 1,
            send_heartbeat=lambda p: True,
        )
        settings = make_settings(cloud_enabled=True)
        CloudSyncWorker(settings, cloud, FakeDB()).step()
        assert synced == []
        hb = HeartbeatWorker(
            settings,
            cloud,
            FakeDB(),
            SimpleNamespace(build_heartbeat=lambda **k: {"uptimeS": 1}),
            StateStore(),
            config_version_provider=lambda: None,
        )
        assert hb.send_now() is False

    def test_heartbeat_401_clears_the_device_key(self, tmp_path, monkeypatch):
        _write_seed(tmp_path)
        store_http_key("issuedkey0123456789ab")

        def urlopen(req, timeout=None):
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        client = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_base="https://example.test/app",
            api_key="issuedkey0123456789ab",
            fw_version="2.4.0",
            max_retries=1,
        )
        client._sleep = lambda *_: None
        assert client.send_heartbeat({"uptimeS": 1}) is False
        assert client._api_key == ""
        assert load_http_key() == ""
        assert load_register_state().get("last_code") == "key_revoked"


class TestDuplicateClaimAndReflash:
    def test_reflash_new_identity_key_stops_on_pk_conflict(self, tmp_path, monkeypatch):
        _write_seed(tmp_path)
        first_pk = public_key_b64url()
        private_key_path().unlink()
        ensure_keypair()
        second_pk = public_key_b64url()
        assert first_pk != second_pk
        seed_after = private_key_path().read_bytes()
        _patch_register_http(monkeypatch, 409, {"code": "pk_conflict"})
        state = attempt_register(api_base="https://example.test/app")
        assert state["status"] == "conflict"
        assert state["last_code"] == "pk_conflict"
        assert load_http_key() == ""
        assert private_key_path().read_bytes() == seed_after
        again = attempt_register(api_base="https://example.test/app")
        assert again["status"] == "conflict"

    def test_register_worker_does_not_retry_a_conflict(self, tmp_path, monkeypatch):
        store_register_state({"status": "conflict", "last_code": "pk_conflict"})
        calls: list[int] = []
        monkeypatch.setattr(
            "app.workers.attempt_register",
            lambda **_k: calls.append(1) or {"status": "conflict"},
        )
        worker = DeviceRegisterWorker(lambda: object(), config_path=str(tmp_path / "c.yaml"))
        worker.step()
        assert calls == []
        assert worker.interval_s() == 3600.0

    def test_register_worker_hands_the_http_key_to_cloud(self, tmp_path, monkeypatch):
        _write_seed(tmp_path)
        store_register_state({"status": "pending", "next_attempt_unix": 0})
        keys: list[str] = []
        monkeypatch.setattr(
            "app.workers.attempt_register",
            lambda **_k: store_http_key("issuedkey0123456789ab") or {"status": "registered"},
        )
        worker = DeviceRegisterWorker(
            lambda: SimpleNamespace(cloud_api_base="https://example.test/app"),
            config_path=str(tmp_path / "c.yaml"),
            on_api_key=keys.append,
        )
        worker.step()
        assert keys == ["issuedkey0123456789ab"]


class TestFactoryResetPath:
    def test_production_firmware_has_no_factory_reset_route(self):
        setup = Path("src/service_window/routes/setup.py").read_text()
        app = Path("src/service_window/app.py").read_text()
        assert "factory_reset" not in setup
        assert "factory_reset" not in app
        demo = Path("scripts/demo-unit.py").read_text()
        assert "--reset" in demo


class TestMissingProbes:
    def test_undeclared_probes_do_not_store_a_row(self, mock_hardware):
        db = FakeDB()
        worker = SamplingWorker(
            make_settings(),
            sensors={},
            db=db,
            rules=None,
            relays=SimpleNamespace(get_state_bitmask=lambda: 0),
            leds=None,
            health=SimpleNamespace(update_last_seen=lambda: None),
            state=StateStore(),
        )
        worker.step()
        worker.step()
        assert db.readings == []

    def test_hardware_note_is_the_reason_not_no_probes_declared(self, mock_hardware, caplog):
        db = FakeDB()
        worker = SamplingWorker(
            make_settings(),
            sensors={},
            db=db,
            rules=None,
            relays=SimpleNamespace(get_state_bitmask=lambda: 0),
            leds=None,
            health=SimpleNamespace(update_last_seen=lambda: None),
            state=StateStore(),
        )
        worker.hardware_note = "ADC missing at 0x48"
        caplog.set_level(logging.ERROR)
        worker.step()
        assert db.readings == []
        assert "ADC missing at 0x48" in caplog.text
        assert "No probes declared fitted" not in caplog.text


class TestOtaHoldAndMidApply:
    def test_download_failure_reports_failed_and_keeps_current(self, tmp_path):
        env = Env(tmp_path)
        bundle = make_bundle(tmp_path)

        class BoomDownload(FakeOpener):
            def __call__(self, req, timeout=None):
                if req.full_url == DOWNLOAD_URL:
                    raise OSError("connection reset")
                return super().__call__(req, timeout)

        opener = BoomDownload(poll_data=make_target(bundle), bundle_path=bundle)
        assert env.agent(opener).check_and_apply() == "failed"
        assert env.current_release() == "2.0.0"
        assert opener.reports[-1]["phase"] == "failed"
        assert "download failed" in opener.reports[-1]["error"]

    def test_power_loss_mid_apply_rolls_back_to_the_previous_release(self, tmp_path):
        env = Env(tmp_path)
        new_release = env.releases / "2.1.0"
        (new_release / "src").mkdir(parents=True)
        (new_release / "VERSION").write_text("2.1.0\n")
        tmp = env.root / "current.tmp"
        os.symlink(str(new_release), str(tmp))
        os.replace(str(tmp), str(env.root / "current"))
        (env.state_dir / "ota-state.json").write_text(
            json.dumps(
                {
                    "phase": "applying",
                    "version": "2.1.0",
                    "previous": os.path.realpath(env.releases / "2.0.0"),
                    "startedAt": "2026-07-08T00:00:00Z",
                }
            )
        )
        opener = FakeOpener(poll_data=None)
        assert env.agent(opener).recover_incomplete_apply() == "rolled_back"
        assert env.current_release() == "2.0.0"
        assert "interrupted" in opener.reports[-1]["error"]

    def test_ota_enabled_false_is_the_hold(self):
        text = Path("docs/ota-hold.md").read_text()
        assert "ota_enabled: false" in text
        assert "no over-the-air release reaches a field unit" in text.lower()
