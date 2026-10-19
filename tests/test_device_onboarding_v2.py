"""Device-side onboarding v2: identity key, register contract, installed layout.

The #138 lesson: importing from the repo checkout stays green while an
installed image (`/opt/bluesignal/releases/<ver>` + `current` symlink) is
broken. Key and registration lookups are therefore exercised against a
chroot-style root, not only this tree.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
import yaml

from tests.test_ap_unit_layout import materialize_release
from tests.test_service_window_setup import make_app

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()

# RFC 8032 test vector 1 seed. Signature is over the contract message
# ``register|<device_id>|<ts>`` with that seed — frozen so a drift in
# encoding or signed-string format fails CI instead of a live unit.
VECTOR_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
VECTOR_PK_B64 = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
VECTOR_DEVICE_ID = "BS-WQM1-0000abcdef01"
VECTOR_TS = 1760840000000
VECTOR_MSG = "register|BS-WQM1-0000abcdef01|1760840000000"
VECTOR_SIG_B64 = (
    "CkA6vpSbiEhQE_AlEq4nxzWn9wh8IGVxXT9ekvQP5935SAoehh4W6eIZ-sQhq7aZneh-hsJs8HOYRmG97QLNBg"
)

_PROBE = r"""
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
release = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(release / "src"))
os.environ["BLUESIGNAL_ROOT"] = str(root)
os.environ.pop("BLUESIGNAL_STATE_DIR", None)

from utils.device_keys import (
    ensure_keypair,
    http_key_path,
    identity_qr_payload,
    private_key_path,
    register_state_path,
    state_dir,
)
from cloud.register import attempt_register, register_message

loaded = Path(ensure_keypair.__globals__.get("__file__", "") or private_key_path.__module__)
mod_file = Path(sys.modules["utils.device_keys"].__file__).resolve()
if not mod_file.is_relative_to(release):
    raise SystemExit(f"imported {mod_file}, not the installed tree")

pk = ensure_keypair()
key_path = private_key_path()
if not key_path.is_relative_to(root / "var" / "lib" / "bluesignal"):
    raise SystemExit(f"key escaped persistent data path: {key_path}")
if key_path.is_relative_to(release):
    raise SystemExit(f"key landed in the release tree: {key_path}")
if "releases" in key_path.parts:
    raise SystemExit(f"key path contains releases/: {key_path}")

payload = identity_qr_payload()
if payload.get("v") != 2 or "pk" not in payload or "device_id" not in payload:
    raise SystemExit(f"bad qr payload: {payload}")

# Offline attempt must persist pending state under the data dir, not the release.
import urllib.request as urllib_request

def _offline(*_a, **_k):
    raise OSError("offline")

urllib_request.urlopen = _offline
state = attempt_register(api_base="https://example.invalid/app", timeout_s=1)
if state.get("status") != "pending":
    raise SystemExit(f"expected pending, got {state}")
if not register_state_path().is_file():
    raise SystemExit("register state missing")
if register_state_path().is_relative_to(release):
    raise SystemExit("register state in release tree")

st = key_path.stat()
dst = state_dir().stat()
print(json.dumps({
    "key": str(key_path),
    "state_dir": str(state_dir()),
    "payload": payload,
    "status": state["status"],
    "register_state": str(register_state_path()),
    "http_key": str(http_key_path()),
    "msg": register_message("BS-WQM1-0000abcdef01", 1760840000000),
    "key_mode": st.st_mode & 0o777,
    "dir_mode": dst.st_mode & 0o777,
    "key_uid": st.st_uid,
    "dir_uid": dst.st_uid,
    "euid": os.geteuid(),
}))
"""


def _write_seed(tmp_path: Path, seed: bytes = VECTOR_SEED) -> Path:
    from utils.device_keys import atomic_write_bytes, private_key_path

    path = private_key_path()
    atomic_write_bytes(path, seed)
    return path


class TestSignatureContract:
    def test_signed_string_is_exactly_register_device_ts(self):
        from cloud.register import register_message

        assert register_message(VECTOR_DEVICE_ID, VECTOR_TS) == VECTOR_MSG

    def test_fixed_vector_signature_matches_contract(self, tmp_path, monkeypatch):
        from Crypto.Signature import eddsa

        from cloud.register import build_register_body
        from utils.device_keys import b64url_decode, b64url_encode, public_key_b64url, sign_bytes

        _write_seed(tmp_path)
        sig = sign_bytes(VECTOR_MSG.encode("utf-8"))
        assert b64url_encode(sig) == VECTOR_SIG_B64
        assert public_key_b64url() == VECTOR_PK_B64
        body = build_register_body(device_id=VECTOR_DEVICE_ID, fw="2.4.0", ts=VECTOR_TS)
        assert body["device_id"] == VECTOR_DEVICE_ID
        assert body["pk"] == VECTOR_PK_B64
        assert body["fw"] == "2.4.0"
        assert body["ts"] == VECTOR_TS
        assert body["sig"] == VECTOR_SIG_B64
        assert "register|" + VECTOR_DEVICE_ID + "|" + str(VECTOR_TS) not in json.dumps(
            {k: body[k] for k in ("device_id", "pk", "fw", "ts")}
        )
        from Crypto.PublicKey import ECC

        key = ECC.construct(curve="Ed25519", seed=VECTOR_SEED)
        eddsa.new(key.public_key(), "rfc8032").verify(
            VECTOR_MSG.encode("utf-8"), b64url_decode(VECTOR_SIG_B64)
        )


class TestIdempotentKeyGen:
    def test_does_not_regenerate_when_present(self, tmp_path):
        from utils.device_keys import ensure_keypair, private_key_path, public_key_b64url

        first = ensure_keypair()
        path = private_key_path()
        mode = path.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        seed = path.read_bytes()
        second = ensure_keypair()
        assert first == second
        assert path.read_bytes() == seed
        assert public_key_b64url() == public_key_b64url()

    def test_promotes_a_complete_tmp_after_crash(self, tmp_path):
        from utils.device_keys import SEED_BYTES, ensure_keypair, private_key_path

        path = private_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(VECTOR_SEED)
        assert not path.exists()
        pub = ensure_keypair()
        assert path.is_file()
        assert path.read_bytes() == VECTOR_SEED
        assert len(pub) == SEED_BYTES

    def test_refuses_to_store_inside_a_release_tree(self, tmp_path, monkeypatch):
        from utils.device_keys import atomic_write_bytes

        release_key = tmp_path / "opt" / "bluesignal" / "releases" / "2.4.0" / "device-ed25519.key"
        with pytest.raises(RuntimeError, match="release"):
            atomic_write_bytes(release_key, VECTOR_SEED)

    def test_truncated_seed_is_not_replaced(self, tmp_path):
        from utils.device_keys import ensure_keypair, private_key_path

        path = private_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 8)
        with pytest.raises(RuntimeError, match="not regenerating"):
            ensure_keypair()
        assert path.read_bytes() == b"\x00" * 8


class TestRegisterClient:
    def _patch_http(self, monkeypatch, status: int, body: dict | None):
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
                payload = json.dumps(body or {}).encode()

                def _read(_payload=payload):
                    return _payload

                err = HTTPError(req.full_url, status, "err", hdrs={}, fp=None)
                err.read = _read  # type: ignore[method-assign]
                raise err
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", urlopen)

    def test_success_stores_http_key_once(self, tmp_path, monkeypatch):
        from cloud.register import attempt_register
        from utils.device_keys import http_key_path, load_http_key, load_register_state

        _write_seed(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text("cloud_enabled: false\n")
        self._patch_http(monkeypatch, 200, {"api_key": "issuedkey0123456789ab"})  # noqa: S105
        state = attempt_register(api_base="https://example.test/app", config_path=str(cfg))
        assert state["status"] == "registered"
        assert load_http_key() == "issuedkey0123456789ab"
        assert stat.S_IMODE(http_key_path().stat().st_mode) == 0o600
        saved = yaml.safe_load(cfg.read_text())
        assert saved["api_key"] == "issuedkey0123456789ab"
        assert saved["cloud_enabled"] is True
        # Idempotent: a second call with the key present must not POST again.
        calls = {"n": 0}

        def boom(*_a, **_k):
            calls["n"] += 1
            raise AssertionError("must not re-register when already registered")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        again = attempt_register(api_base="https://example.test/app", config_path=str(cfg))
        assert again["status"] == "registered"
        assert calls["n"] == 0
        assert load_register_state()["status"] == "registered"

    def test_pk_conflict_is_a_clear_status_not_a_crash_loop(self, tmp_path, monkeypatch):
        from cloud.register import attempt_register
        from utils.device_keys import load_http_key, load_register_state

        _write_seed(tmp_path)
        self._patch_http(monkeypatch, 409, {"code": "pk_conflict"})
        cfg = str(tmp_path / "c.yaml")
        state = attempt_register(api_base="https://example.test/app", config_path=cfg)
        assert state["status"] == "conflict"
        assert state["last_code"] == "pk_conflict"
        assert load_http_key() == ""
        calls = {"n": 0}

        def count(*_a, **_k):
            calls["n"] += 1
            err = HTTPError(
                "https://example.test/app/v2/devices/register",
                409,
                "err",
                hdrs={},
                fp=None,
            )

            def _read():
                return b'{"code":"pk_conflict"}'

            err.read = _read  # type: ignore[method-assign]
            raise err

        monkeypatch.setattr("urllib.request.urlopen", count)
        again = attempt_register(api_base="https://example.test/app")
        assert again["status"] == "conflict"
        assert calls["n"] == 0
        assert load_register_state()["status"] == "conflict"

    def test_offline_persists_pending_across_reboot(self, tmp_path, monkeypatch):
        from cloud.register import attempt_register
        from utils.device_keys import load_register_state, register_state_path

        _write_seed(tmp_path)

        def offline(*_a, **_k):
            raise URLError("offline")

        monkeypatch.setattr("urllib.request.urlopen", offline)
        first = attempt_register(api_base="https://example.test/app", timeout_s=1)
        assert first["status"] == "pending"
        assert register_state_path().is_file()
        saved = json.loads(register_state_path().read_text())
        assert saved["status"] == "pending"
        assert saved["attempts"] >= 1
        assert saved["backoff_s"] >= 5
        # Simulate process exit: only the file remains.
        reloaded = load_register_state()
        assert reloaded["status"] == "pending"
        assert reloaded["attempts"] == saved["attempts"]
        assert reloaded["next_attempt_unix"] == saved["next_attempt_unix"]

    def test_stale_ts_retries_with_a_fresh_clock(self, tmp_path, monkeypatch):
        from cloud.register import attempt_register

        _write_seed(tmp_path)
        self._patch_http(monkeypatch, 401, {"code": "stale_ts"})
        state = attempt_register(api_base="https://example.test/app")
        assert state["status"] == "pending"
        assert state["last_code"] == "stale_ts"
        assert state["backoff_s"] == 1

    def test_bad_sig_does_not_change_the_identity_key(self, tmp_path, monkeypatch):
        from cloud.register import attempt_register
        from utils.device_keys import private_key_path

        _write_seed(tmp_path)
        seed = private_key_path().read_bytes()
        self._patch_http(monkeypatch, 401, {"code": "bad_sig"})
        state = attempt_register(api_base="https://example.test/app")
        assert state["status"] == "error"
        assert state["last_code"] == "bad_sig"
        assert private_key_path().read_bytes() == seed

    def test_http_key_wipe_reregisters_the_same_pk(self, tmp_path, monkeypatch):
        from cloud.register import attempt_register
        from utils.device_keys import http_key_path, load_http_key

        _write_seed(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text("cloud_enabled: false\n")
        self._patch_http(monkeypatch, 200, {"api_key": "firstkey0123456789ab"})  # noqa: S105
        assert (
            attempt_register(api_base="https://example.test/app", config_path=str(cfg))["status"]
            == "registered"
        )
        http_key_path().unlink()
        assert load_http_key() == ""
        self._patch_http(monkeypatch, 200, {"api_key": "rotatedkey0123456789"})  # noqa: S105
        again = attempt_register(api_base="https://example.test/app", config_path=str(cfg))
        assert again["status"] == "registered"
        assert load_http_key() == "rotatedkey0123456789"


class TestIdentityQr:
    def test_payload_is_v2_contract(self, tmp_path, monkeypatch):
        from utils.device_keys import identity_qr_payload
        from utils.identity import get_device_id

        payload = identity_qr_payload()
        assert payload["v"] == 2
        assert payload["device_id"] == get_device_id()
        assert isinstance(payload["pk"], str)
        assert set(payload) <= {"v", "device_id", "label", "pk"}

    def test_setup_identity_page_has_no_key_paste(self, tmp_path, mock_hardware):
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            page = c.get("/setup/identity").get_data(as_text=True)
        assert 'name="api_key"' not in page
        assert "qr" in page.lower()
        assert "Registration:" in page
        assert '"v":2' in page
        assert '"pk":' in page
        assert '"device_id":' in page


class TestSetupOrder:
    def test_cloud_registration_follows_network(self):
        from service_window.routes.setup import CARDED_STEPS, STEPS

        assert STEPS.index("network") < STEPS.index("cloud")
        assert STEPS[-2:] == ["cloud", "done"]
        assert "cloud" not in CARDED_STEPS
        assert STEPS.index("identity") < STEPS.index("sensors")
        assert STEPS.index("sensors") < STEPS.index("network")

    def test_installer_cloud_page_hides_paste_behind_bench_override(self, tmp_path, mock_hardware):
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            page = c.get("/setup/cloud").get_data(as_text=True)
        assert "Bench / dev override" in page
        assert "never pastes" in page.lower() or "never paste" in page.lower()
        assert 'name="api_key"' in page
        assert "required" not in page.split('name="api_key"', 1)[1].split(">", 1)[0]

    def test_cloud_page_does_not_wait_on_register(self, tmp_path, mock_hardware, monkeypatch):
        """A slow or hanging Cloud must not stall the setup page."""

        def hang(*_a, **_k):
            time.sleep(30)
            raise URLError("hung")

        monkeypatch.setattr("urllib.request.urlopen", hang)
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            started = time.monotonic()
            resp = c.get("/setup/cloud")
        assert resp.status_code == 200
        assert time.monotonic() - started < 2.0


class TestInstalledImageLayout:
    def test_key_and_register_state_live_outside_the_release(self, tmp_path):
        root = tmp_path / "root"
        release = root / "opt" / "bluesignal" / "releases" / VERSION
        materialize_release(ROOT, release)
        current = root / "opt" / "bluesignal" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(release)
        (root / "var" / "lib" / "bluesignal").mkdir(parents=True)
        probe = tmp_path / "probe_onboarding.py"
        probe.write_text(_PROBE, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(probe), str(root), str(release)],
            capture_output=True,
            text=True,
            check=False,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in ("BLUESIGNAL_STATE_DIR", "BLUESIGNAL_ROOT")
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout.splitlines()[-1])
        key = Path(data["key"])
        assert key.is_relative_to(root / "var" / "lib" / "bluesignal")
        assert not key.is_relative_to(release)
        assert data["status"] == "pending"
        assert data["msg"] == VECTOR_MSG
        # OTA swap: flip current to a new empty release; the key must still be
        # where the installed module looks, not beside the old tree.
        other = root / "opt" / "bluesignal" / "releases" / "9.9.9"
        other.mkdir(parents=True)
        current.unlink()
        current.symlink_to(other)
        assert key.is_file()
        assert key.read_bytes()  # still the seed
        assert Path(data["register_state"]).is_file()
        assert data["key_mode"] == 0o600
        assert data["dir_mode"] == 0o700
        assert data["key_uid"] == data["euid"]
        assert data["dir_uid"] == data["euid"]


class TestBootStatusRegistration:
    def test_status_file_carries_pending_registered_conflict(self, monkeypatch, tmp_path):
        from diagnostics.boot_status import commission_verdict, parse_status, write_boot_status
        from utils.device_keys import store_http_key, store_register_state

        path = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(path))
        store_register_state({"status": "pending"})
        text = write_boot_status(hotspot="up", portal="up (80)", adc="ok", power="ok")
        parsed = parse_status(text)
        assert parsed["registration"] == "pending"

        store_http_key("issuedkey0123456789ab")
        store_register_state({"status": "registered"})
        parsed = parse_status(write_boot_status())
        assert parsed["registration"] == "registered"

        store_register_state({"status": "conflict", "last_code": "pk_conflict"})
        parsed = parse_status(write_boot_status())
        assert parsed["registration"] == "conflict"
        assert "identity conflict" in commission_verdict(parsed)


class TestRegisterDoesNotBlockStartup:
    def test_worker_is_a_daemon_thread_after_notify_ready(self):
        supervisor = (ROOT / "src" / "app" / "supervisor.py").read_text()
        assert supervisor.index("t.start()") < supervisor.index("self._notifier.ready()")
        assert "daemon=True" in supervisor.split("def start")[1].split("def _on_fault")[0]
        app = (ROOT / "src" / "service_window" / "app.py").read_text()
        assert "kick_register" not in app
        assert "attempt_register" not in app
        wqm = (ROOT / "systemd" / "bluesignal-wqm.service").read_text()
        assert "Type=notify" in wqm
        sw = (ROOT / "systemd" / "bluesignal-service-window.service").read_text()
        assert "bluesignal-wqm.service" in sw
        setup_src = (ROOT / "src" / "service_window" / "routes" / "setup.py").read_text()
        assert "daemon=True" in setup_src
        assert "wqm1-register-kick" in setup_src
