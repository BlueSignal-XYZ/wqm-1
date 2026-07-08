"""
OTA agent tests — fully hermetic.

Real Ed25519 keys (generated in-test with pycryptodome), real tarballs and
sha256s, a fake HTTP opener with canned responses, a fake subprocess runner,
and a fake clock/sleep. Filesystem roots live under tmp_path.
"""

import base64
import io
import json
import os
import sqlite3
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from ota.agent import (
    OTAAgent,
    sha256_file,
    validate_tar_members,
    verify_manifest_signature,
    version_tuple,
)
from utils.config import Settings

DEVICE_ID = "BS-WQM1-000000000001"
DOWNLOAD_URL = "https://storage.example.com/signed/bundle.tar.gz"

# One real keypair for the whole module — signing is fast, generation is not.
KEY = ECC.generate(curve="Ed25519")
PUBLIC_PEM = KEY.public_key().export_key(format="PEM")


def sign_b64(manifest_str: str) -> str:
    signature = eddsa.new(KEY, "rfc8032").sign(manifest_str.encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


def make_bundle(tmp_path: Path, version: str = "2.1.0") -> Path:
    """A minimal but real firmware bundle: src/main.py + VERSION at top level."""
    tree = tmp_path / f"tree-{version}"
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "main.py").write_text(f"VERSION = {version!r}\n")
    (tree / "VERSION").write_text(version + "\n")
    bundle = tmp_path / f"wqm1-firmware-{version}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(tree / "src", arcname="src")
        tar.add(tree / "VERSION", arcname="VERSION")
    return bundle


def make_target(
    bundle: Path,
    version: str = "2.1.0",
    manifest_over: dict | None = None,
    data_over: dict | None = None,
) -> dict:
    """Build the GET .../firmware `data` payload with a validly-signed manifest."""
    manifest = {
        "product": "wqm1",
        "version": version,
        "channel": "stable",
        "sha256": sha256_file(bundle),
        "sizeBytes": bundle.stat().st_size,
        "minFromVersion": "2.0.0",
        "keyId": "ota-2026",
        "createdAt": "2026-07-08T00:00:00Z",
        "requiresDeps": False,
        "notes": "test release",
    }
    if manifest_over:
        manifest.update(manifest_over)
    manifest_str = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    data = {
        "version": version,
        "channel": manifest["channel"],
        "sha256": manifest["sha256"],
        "sizeBytes": manifest["sizeBytes"],
        "minFromVersion": manifest["minFromVersion"],
        "keyId": manifest["keyId"],
        "manifest": manifest_str,
        "signature": sign_b64(manifest_str),
        "url": DOWNLOAD_URL,
        "urlExpiresAt": 9999999999999,
    }
    if data_over:
        data.update(data_over)
    return data


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict | None = None):
        self.status = status
        self._buf = io.BytesIO(body)
        self.headers = dict(headers or {})

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        pass


class FakeOpener:
    """Routes poll/status/download requests; records every status report."""

    def __init__(
        self,
        poll_data: object = None,
        bundle_path: Path | None = None,
        content_length: int | None = None,
    ):
        self.poll_data = poll_data  # None -> 204; dict -> 200; Exception -> raise
        self.bundle_path = bundle_path
        self.content_length = content_length
        self.reports: list[dict] = []
        self.poll_count = 0
        self.on_poll = None

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/firmware/status"):
            assert req.get_method() == "POST"
            self.reports.append(json.loads(req.data.decode("utf-8")))
            return FakeResponse(200, b'{"success": true}')
        if url.endswith(f"/v2/devices/{DEVICE_ID}/firmware"):
            self.poll_count += 1
            if self.on_poll:
                self.on_poll(self)
            if isinstance(self.poll_data, Exception):
                raise self.poll_data
            if self.poll_data is None:
                return FakeResponse(204)
            body = json.dumps({"success": True, "data": self.poll_data}).encode("utf-8")
            return FakeResponse(200, body)
        if url == DOWNLOAD_URL:
            body = self.bundle_path.read_bytes()
            length = self.content_length if self.content_length is not None else len(body)
            return FakeResponse(200, body, {"Content-Length": str(length)})
        raise AssertionError(f"unexpected request: {req.get_method()} {url}")

    @property
    def phases(self) -> list[str]:
        return [r["phase"] for r in self.reports]


class FakeRunCmd:
    def __init__(self, active: bool = True):
        self.active = active
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "is-active"]:
            if self.active:
                return SimpleNamespace(returncode=0, stdout="active\n")
            return SimpleNamespace(returncode=3, stdout="failed\n")
        return SimpleNamespace(returncode=0, stdout="")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Env:
    """tmp_path-rooted install: releases/2.0.0 active, empty readings DB."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.root = tmp_path / "opt"
        self.state_dir = tmp_path / "state"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        self.state_dir.mkdir()

        installed = self.releases / "2.0.0"
        (installed / "src").mkdir(parents=True)
        (installed / "VERSION").write_text("2.0.0\n")
        os.symlink(str(installed), str(self.root / "current"))

        self.db_path = tmp_path / "wqm1.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL)")
        conn.commit()
        conn.close()

        self.pubkey = tmp_path / "ota_public_key.pem"
        self.pubkey.write_text(PUBLIC_PEM + "\n")

        self.settings = Settings(db_path=str(self.db_path), api_key="test-api-key")
        self.run_cmd = FakeRunCmd()
        self.clock = FakeClock()

    def agent(self, opener: FakeOpener, pubkey_path: Path | None = "default") -> OTAAgent:
        return OTAAgent(
            self.settings,
            DEVICE_ID,
            opener=opener,
            run_cmd=self.run_cmd,
            clock=self.clock,
            sleep=self.clock.sleep,
            root=self.root,
            state_dir=self.state_dir,
            pubkey_path=self.pubkey if pubkey_path == "default" else pubkey_path,
        )

    def current_release(self) -> str:
        return Path(os.path.realpath(self.root / "current")).name

    def insert_reading(self, timestamp: str = "2999-01-01T00:00:00Z") -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO readings (timestamp) VALUES (?)", (timestamp,))
        conn.commit()
        conn.close()

    def read_state(self) -> dict:
        return json.loads((self.state_dir / "ota-state.json").read_text())


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------------------
# Idle paths
# ---------------------------------------------------------------------------


class TestIdle:
    def test_204_returns_idle(self, env):
        opener = FakeOpener(poll_data=None)
        assert env.agent(opener).check_and_apply() == "idle"
        assert opener.reports == []
        assert env.current_release() == "2.0.0"

    def test_already_on_target_version_returns_idle(self, env, tmp_path):
        bundle = make_bundle(tmp_path, version="2.0.0")
        opener = FakeOpener(poll_data=make_target(bundle, version="2.0.0"), bundle_path=bundle)
        assert env.agent(opener).check_and_apply() == "idle"
        assert opener.reports == []

    def test_poll_error_returns_error_without_reports(self, env):
        opener = FakeOpener(poll_data=OSError("network unreachable"))
        assert env.agent(opener).check_and_apply() == "error"
        assert opener.reports == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_full_cycle_success(self, env, tmp_path):
        env.insert_reading()  # a fresh reading is already there when self-test runs
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)

        result = env.agent(opener).check_and_apply()

        assert result == "success"
        assert opener.phases == ["downloading", "verifying", "applying", "self_test", "success"]
        assert all(r["version"] == "2.1.0" for r in opener.reports)
        # Release extracted and activated
        assert (env.releases / "2.1.0" / "src" / "main.py").exists()
        assert (env.releases / "2.1.0" / "VERSION").read_text().strip() == "2.1.0"
        assert env.current_release() == "2.1.0"
        # Services restarted
        assert [
            "systemctl",
            "restart",
            "bluesignal-wqm.service",
            "bluesignal-service-window.service",
        ] in env.run_cmd.calls
        # State file records success; download and staging cleaned up
        state = env.read_state()
        assert state["phase"] == "success"
        assert state["version"] == "2.1.0"
        assert not (env.state_dir / "ota-download" / "2.1.0.tar.gz").exists()
        assert not (env.releases / "2.1.0.staging").exists()
        # Previous release retained for rollback
        assert (env.releases / "2.0.0").is_dir()

    def test_pubkey_loaded_from_current_release_with_comment_header(self, env, tmp_path):
        env.insert_reading()
        key_dir = env.releases / "2.0.0" / "config"
        key_dir.mkdir()
        (key_dir / "ota_public_key.pem").write_text(
            "# OTA verification key (comment header must be tolerated)\n" + PUBLIC_PEM + "\n"
        )
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)

        agent = env.agent(opener, pubkey_path=None)  # force fallback resolution
        assert agent.check_and_apply() == "success"


# ---------------------------------------------------------------------------
# Tamper matrix — every entry must report correctly and leave the symlink alone
# ---------------------------------------------------------------------------


class TestTamperMatrix:
    def _assert_refused(self, env, opener, expected_phase, error_fragment):
        assert env.current_release() == "2.0.0", "symlink must not move"
        assert "applying" not in opener.phases, "must never reach apply"
        last = opener.reports[-1]
        assert last["phase"] == expected_phase
        assert error_fragment in last["error"]

    def test_bad_signature(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        data = make_target(bundle)
        tampered = bytearray(base64.b64decode(data["signature"]))
        tampered[0] ^= 0xFF
        data["signature"] = base64.b64encode(bytes(tampered)).decode("ascii")
        opener = FakeOpener(poll_data=data, bundle_path=bundle)

        assert env.agent(opener).check_and_apply() == "verify_failed"
        self._assert_refused(env, opener, "verify_failed", "signature")

    def test_wrong_sha256(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(
            poll_data=make_target(bundle, manifest_over={"sha256": "0" * 64}),
            bundle_path=bundle,
        )
        assert env.agent(opener).check_and_apply() == "verify_failed"
        self._assert_refused(env, opener, "verify_failed", "sha256")

    def test_manifest_version_mismatch(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        # Manifest claims 2.2.0 (validly signed) while the target offers 2.1.0.
        opener = FakeOpener(
            poll_data=make_target(bundle, manifest_over={"version": "2.2.0"}),
            bundle_path=bundle,
        )
        assert env.agent(opener).check_and_apply() == "verify_failed"
        self._assert_refused(env, opener, "verify_failed", "version")

    def test_tar_with_traversal_member(self, env, tmp_path):
        bundle = tmp_path / "evil.tar.gz"
        with tarfile.open(bundle, "w:gz") as tar:
            payload = b"evil"
            info = tarfile.TarInfo("../evil.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        # Manifest is honest about the evil bundle — only the tar check trips.
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)

        assert env.agent(opener).check_and_apply() == "verify_failed"
        self._assert_refused(env, opener, "verify_failed", "traversal")
        assert not (tmp_path / "evil.txt").exists()

    def test_oversize_content_length(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(
            poll_data=make_target(bundle),
            bundle_path=bundle,
            content_length=env.settings.ota_max_bundle_bytes + 1,
        )
        assert env.agent(opener).check_and_apply() == "failed"
        self._assert_refused(env, opener, "failed", "Content-Length")

    def test_streamed_size_exceeds_declared_limit(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        # sizeBytes and Content-Length both lie small; the stream is bigger.
        opener = FakeOpener(
            poll_data=make_target(bundle, data_over={"sizeBytes": 10}),
            bundle_path=bundle,
            content_length=10,
        )
        assert env.agent(opener).check_and_apply() == "failed"
        self._assert_refused(env, opener, "failed", "size limit")

    def test_min_from_version_above_installed(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(
            poll_data=make_target(bundle, manifest_over={"minFromVersion": "2.5.0"}),
            bundle_path=bundle,
        )
        assert env.agent(opener).check_and_apply() == "verify_failed"
        self._assert_refused(env, opener, "verify_failed", "minFromVersion")

    def test_requires_deps_refused(self, env, tmp_path):
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(
            poll_data=make_target(bundle, manifest_over={"requiresDeps": True}),
            bundle_path=bundle,
        )
        assert env.agent(opener).check_and_apply() == "failed"
        self._assert_refused(env, opener, "failed", "requires manual dependency update")

    def test_insufficient_disk_refused(self, env, tmp_path, monkeypatch):
        bundle = make_bundle(tmp_path)
        monkeypatch.setattr(
            "ota.agent.shutil.disk_usage",
            lambda p: SimpleNamespace(total=1000, used=900, free=100),
        )
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)
        assert env.agent(opener).check_and_apply() == "failed"
        self._assert_refused(env, opener, "failed", "insufficient free disk")


# ---------------------------------------------------------------------------
# Self-test failure -> rollback
# ---------------------------------------------------------------------------


class TestRollback:
    def test_no_fresh_reading_rolls_back(self, env, tmp_path):
        # readings table stays empty -> self-test times out on the fake clock.
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)

        result = env.agent(opener).check_and_apply()

        assert result == "rolled_back"
        assert env.current_release() == "2.0.0"
        last = opener.reports[-1]
        assert last["phase"] == "rolled_back"
        assert "no fresh sensor reading" in last["error"]
        # Bad release kept for post-mortem
        assert (env.releases / "2.1.0").is_dir()
        assert env.read_state()["phase"] == "rolled_back"
        # Services restarted twice: once to apply, once to roll back.
        restarts = [c for c in env.run_cmd.calls if c[:2] == ["systemctl", "restart"]]
        assert len(restarts) == 2

    def test_service_not_active_rolls_back(self, env, tmp_path):
        env.insert_reading()
        env.run_cmd.active = False
        bundle = make_bundle(tmp_path)
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)

        assert env.agent(opener).check_and_apply() == "rolled_back"
        assert env.current_release() == "2.0.0"
        assert "not active" in opener.reports[-1]["error"]


# ---------------------------------------------------------------------------
# Boot-time safety net
# ---------------------------------------------------------------------------


class TestBootSafetyNet:
    def _simulate_crash_mid_apply(self, env):
        """Agent died after flipping the symlink to 2.1.0, before self-test."""
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

    def test_rolls_back_after_mid_apply_crash(self, env):
        self._simulate_crash_mid_apply(env)
        opener = FakeOpener(poll_data=None)

        assert env.agent(opener).recover_incomplete_apply() == "rolled_back"
        assert env.current_release() == "2.0.0"
        assert opener.reports[-1]["phase"] == "rolled_back"
        assert "interrupted" in opener.reports[-1]["error"]
        assert env.read_state()["phase"] == "rolled_back"

    def test_noop_when_last_state_is_terminal(self, env):
        (env.state_dir / "ota-state.json").write_text(
            json.dumps({"phase": "success", "version": "2.0.0"})
        )
        opener = FakeOpener(poll_data=None)
        assert env.agent(opener).recover_incomplete_apply() is None
        assert opener.reports == []
        assert env.current_release() == "2.0.0"

    def test_noop_when_no_state_file(self, env):
        opener = FakeOpener(poll_data=None)
        assert env.agent(opener).recover_incomplete_apply() is None
        assert opener.reports == []

    def test_marks_failed_when_symlink_never_flipped(self, env):
        # Crash happened before the flip: current still points at 2.0.0.
        (env.state_dir / "ota-state.json").write_text(
            json.dumps(
                {
                    "phase": "applying",
                    "version": "2.1.0",
                    "previous": os.path.realpath(env.releases / "2.0.0"),
                }
            )
        )
        opener = FakeOpener(poll_data=None)

        assert env.agent(opener).recover_incomplete_apply() == "failed"
        assert env.current_release() == "2.0.0"
        assert opener.reports[-1]["phase"] == "failed"
        assert env.read_state()["phase"] == "failed"


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_keeps_limit_and_protected(self, env, tmp_path):
        env.insert_reading()
        for old_version, age in [("1.8.0", 5000), ("1.9.0", 4000)]:
            d = env.releases / old_version
            d.mkdir()
            stamp = 1_000_000 - age
            os.utime(d, (stamp, stamp))

        bundle = make_bundle(tmp_path)
        opener = FakeOpener(poll_data=make_target(bundle), bundle_path=bundle)
        assert env.agent(opener).check_and_apply() == "success"  # keep_releases = 2

        remaining = sorted(p.name for p in env.releases.iterdir())
        assert remaining == ["2.0.0", "2.1.0"]  # new + previous; 1.8/1.9 pruned


# ---------------------------------------------------------------------------
# run_forever: jittered wait + check-now flag
# ---------------------------------------------------------------------------


class TestRunForever:
    def test_flag_file_triggers_immediate_poll(self, env):
        opener = FakeOpener(poll_data=None)
        stop = threading.Event()
        (env.state_dir / "ota-check-now").touch()

        def on_poll(o):
            if o.poll_count >= 2:
                stop.set()

        opener.on_poll = on_poll
        start = env.clock.now
        env.agent(opener).run_forever(stop)

        assert opener.poll_count == 2
        assert not (env.state_dir / "ota-check-now").exists(), "flag must be consumed"
        # Second poll came from the flag, not from waiting out the jittered
        # interval (which would be >= 0.8 * 900s on the fake clock).
        assert env.clock.now - start < 60

    def test_waits_jittered_interval_without_flag(self, env):
        opener = FakeOpener(poll_data=None)
        stop = threading.Event()

        def on_poll(o):
            if o.poll_count >= 2:
                stop.set()

        opener.on_poll = on_poll
        start = env.clock.now
        env.agent(opener).run_forever(stop)

        assert opener.poll_count == 2
        elapsed = env.clock.now - start
        assert 0.8 * env.settings.ota_poll_s <= elapsed <= 1.2 * env.settings.ota_poll_s + 1


# ---------------------------------------------------------------------------
# Helper units: signature, tar sanity, version compare
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_signature_roundtrip_and_rejection(self):
        message = b'{"product":"wqm1","version":"9.9.9"}'
        signature = eddsa.new(KEY, "rfc8032").sign(message)
        verify_manifest_signature(message, signature, PUBLIC_PEM)  # must not raise

        with pytest.raises(ValueError):
            verify_manifest_signature(message + b" ", signature, PUBLIC_PEM)

        other_pub = ECC.generate(curve="Ed25519").public_key().export_key(format="PEM")
        with pytest.raises(ValueError):
            verify_manifest_signature(message, signature, other_pub)

    @pytest.mark.parametrize(
        ("member_name", "fragment"),
        [("/etc/passwd", "absolute"), ("../evil.txt", "traversal"), ("a/../../b", "traversal")],
    )
    def test_tar_member_paths_rejected(self, tmp_path, member_name, fragment):
        bundle = tmp_path / "t.tar.gz"
        with tarfile.open(bundle, "w:gz") as tar:
            info = tarfile.TarInfo(member_name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        error = validate_tar_members(bundle)
        assert error is not None and fragment in error

    def test_tar_symlink_escape_rejected_internal_allowed(self, tmp_path):
        escaping = tmp_path / "escape.tar.gz"
        with tarfile.open(escaping, "w:gz") as tar:
            info = tarfile.TarInfo("src/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../etc/passwd"
            tar.addfile(info)
        error = validate_tar_members(escaping)
        assert error is not None and "escapes" in error

        internal = tmp_path / "internal.tar.gz"
        with tarfile.open(internal, "w:gz") as tar:
            data = b"real"
            fi = tarfile.TarInfo("src/real.py")
            fi.size = len(data)
            tar.addfile(fi, io.BytesIO(data))
            link = tarfile.TarInfo("src/alias.py")
            link.type = tarfile.SYMTYPE
            link.linkname = "real.py"
            tar.addfile(link)
        assert validate_tar_members(internal) is None

    def test_tar_absolute_symlink_rejected(self, tmp_path):
        bundle = tmp_path / "abs.tar.gz"
        with tarfile.open(bundle, "w:gz") as tar:
            info = tarfile.TarInfo("src/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        error = validate_tar_members(bundle)
        assert error is not None and "absolute link" in error

    def test_version_tuple(self):
        assert version_tuple("2.1.0") == (2, 1, 0)
        assert version_tuple("2.1.0") > version_tuple("2.0.9")
        assert version_tuple("10.0.0") > version_tuple("9.9.9")
        assert version_tuple("not-a-version") is None
        assert version_tuple(None) is None

    def test_installed_version_reads_current_symlink(self, env):
        agent = env.agent(FakeOpener(poll_data=None))
        assert agent.installed_version() == "2.0.0"
