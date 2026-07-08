"""
WQM-1 OTA Update Agent

Device-side over-the-air firmware updates:

1. Poll ``GET {cloud_api_base}/v2/devices/{id}/firmware`` (jittered, plus an
   on-demand flag file) — 204 means nothing assigned.
2. Download the signed bundle with size + free-disk guards.
3. Verify, in order, aborting on the first failure: Ed25519 signature over the
   verbatim manifest bytes -> manifest/product/version pin -> bundle sha256 ->
   tar packaging sanity (no absolute paths, no ``..``, no escaping links) ->
   ``minFromVersion`` downgrade guard -> ``requiresDeps`` refusal.
4. Apply atomically: extract to ``releases/<version>.staging``, rename to
   ``releases/<version>``, flip the ``current`` symlink in one ``os.replace``,
   restart the firmware services.
5. Self-test: main service active AND a fresh sensor reading lands in SQLite
   within ``ota_self_test_timeout_s`` — otherwise flip the symlink back to the
   previous release and report ``rolled_back``.

Every phase transition is reported to
``POST {cloud_api_base}/v2/devices/{id}/firmware/status``.

All side effects are dependency-injected (HTTP opener, subprocess runner,
clock, sleep, filesystem roots) so the full cycle is testable without
hardware, network, or systemd. Design:
``docs/architecture/ota-firmware-updates.md`` in the marketplace repo.
"""

import base64
import contextlib
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from utils.config import FIRMWARE_VERSION, Settings

logger = logging.getLogger("wqm1.ota")

DEFAULT_ROOT = Path("/opt/bluesignal")
DEFAULT_STATE_DIR = Path("/var/lib/bluesignal")

CHECK_NOW_FLAG = "ota-check-now"
STATE_FILE = "ota-state.json"
DOWNLOAD_DIR = "ota-download"

REQUEST_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 120
SELF_TEST_POLL_S = 10
DOWNLOAD_CHUNK = 64 * 1024
# Free disk must be >= this multiple of the bundle size before downloading
# (bundle + extracted staging tree + the retained previous release).
FREE_DISK_FACTOR = 3

MAIN_SERVICE = "bluesignal-wqm.service"
RESTART_SERVICES = [MAIN_SERVICE, "bluesignal-service-window.service"]

# Phases mid-apply: if the agent dies while the state file records one of
# these, the boot-time safety net treats the update as failed and rolls back.
_INCOMPLETE_PHASES = ("applying", "self_test")


class OTAError(Exception):
    """Non-verification OTA failure (download, apply, ...)."""


class VerifyError(OTAError):
    """Bundle failed verification — reported as ``verify_failed``."""


# ---------------------------------------------------------------------------
# Pure helpers (also used by CI's bundle round-trip job)
# ---------------------------------------------------------------------------


def verify_manifest_signature(manifest_bytes: bytes, signature: bytes, pubkey_pem: str) -> None:
    """Verify an Ed25519 signature over the verbatim manifest bytes.

    Raises ValueError (from pycryptodome) when the signature does not match.
    """
    key = ECC.import_key(pubkey_pem)
    verifier = eddsa.new(key, "rfc8032")
    verifier.verify(manifest_bytes, signature)


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(DOWNLOAD_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def validate_tar_members(bundle: Path) -> str | None:
    """Packaging sanity for an OTA bundle (tar-slip guard).

    Returns an error string on the first offending member, or None when every
    member is a relative path with no ``..`` components and no link that
    points outside the archive.
    """
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar:
            name = member.name
            if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
                return f"absolute path in archive: {name}"
            if ".." in Path(name).parts:
                return f"path traversal in archive: {name}"
            if member.issym() or member.islnk():
                link = member.linkname
                if link.startswith(("/", "\\")):
                    return f"absolute link target: {name} -> {link}"
                # Symlink targets are relative to the member's directory;
                # hardlink targets are relative to the archive root.
                base = os.path.dirname(name) if member.issym() else ""
                resolved = os.path.normpath(os.path.join(base, link))
                if resolved == ".." or resolved.startswith(("../", "..\\")):
                    return f"link escapes archive: {name} -> {link}"
    return None


def version_tuple(version: str) -> tuple[int, ...] | None:
    """``"2.1.0"`` -> ``(2, 1, 0)``; None when unparseable."""
    try:
        return tuple(int(p) for p in str(version).strip().split("."))
    except (ValueError, AttributeError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class OTAAgent:
    """One device's OTA state machine. See module docstring for the cycle."""

    def __init__(
        self,
        settings: Settings,
        device_id: str,
        opener: Any = urllib.request.urlopen,
        run_cmd: Any = subprocess.run,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
        root: Path = DEFAULT_ROOT,
        state_dir: Path = DEFAULT_STATE_DIR,
        pubkey_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._device_id = device_id
        self._opener = opener
        self._run_cmd = run_cmd
        self._clock = clock
        self._sleep = sleep
        self._root = Path(root)
        self._state_dir = Path(state_dir)
        self._pubkey_path = Path(pubkey_path) if pubkey_path else None
        self._apply_time_iso: str | None = None

    # -- paths --------------------------------------------------------------

    @property
    def _releases_dir(self) -> Path:
        return self._root / "releases"

    @property
    def _current_link(self) -> Path:
        return self._root / "current"

    @property
    def _state_file(self) -> Path:
        return self._state_dir / STATE_FILE

    @property
    def _check_now_flag(self) -> Path:
        return self._state_dir / CHECK_NOW_FLAG

    # -- identity / versions -------------------------------------------------

    def installed_version(self) -> str:
        """Version of the currently-running release (``current/VERSION``),
        falling back to the version this module was imported from."""
        try:
            return (self._current_link / "VERSION").read_text().strip()
        except OSError:
            return FIRMWARE_VERSION

    def _load_public_key(self) -> str:
        """Resolve the OTA verification public key: explicit constructor path,
        then the running release's embedded key, then the repo checkout."""
        candidates = [
            self._pubkey_path,
            self._current_link / "config" / "ota_public_key.pem",
            Path(__file__).resolve().parent.parent.parent / "config" / "ota_public_key.pem",
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                text = candidate.read_text()
            except OSError:
                continue
            # Tolerate comment headers above the PEM block.
            marker = text.find("-----BEGIN")
            if marker >= 0:
                return text[marker:]
        raise VerifyError("no OTA public key installed")

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, url: str, body: dict | None = None) -> dict | None:
        """One JSON request via the injected opener. 204 -> None."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "X-API-Key": self._settings.api_key,
                "Content-Type": "application/json",
            },
        )
        resp = self._opener(req, timeout=REQUEST_TIMEOUT_S)
        try:
            if getattr(resp, "status", 200) == 204:
                return None
            raw = resp.read()
        finally:
            with contextlib.suppress(Exception):
                resp.close()
        if not raw:
            return None
        return json.loads(raw)

    def _poll(self) -> dict | None:
        """Fetch the assigned OTA target; None when nothing is assigned."""
        url = f"{self._settings.cloud_api_base}/v2/devices/{self._device_id}/firmware"
        payload = self._request("GET", url)
        if not payload:
            return None
        return payload.get("data")

    def _report(self, phase: str, version: str, error: str | None = None) -> None:
        """POST a status transition. Never raises — reporting must not be able
        to break the update itself."""
        url = f"{self._settings.cloud_api_base}/v2/devices/{self._device_id}/firmware/status"
        body: dict[str, Any] = {"phase": phase, "version": version}
        if error:
            body["error"] = error[:300]
        try:
            self._request("POST", url, body)
        except Exception as e:
            logger.warning("OTA status report (%s %s) failed: %s", phase, version, e)

    # -- download ------------------------------------------------------------

    def _download(self, data: dict) -> Path:
        """Stream the bundle to the state dir with size + free-disk guards."""
        version = data["version"]
        url = data.get("url")
        if not url:
            raise OTAError("no download URL in target")

        size_hint = int(data.get("sizeBytes") or 0)
        limit = self._settings.ota_max_bundle_bytes
        if size_hint > 0:
            limit = min(limit, int(size_hint * 1.05))

        download_dir = self._state_dir / DOWNLOAD_DIR
        download_dir.mkdir(parents=True, exist_ok=True)
        dest = download_dir / f"{version}.tar.gz"

        resp = self._opener(urllib.request.Request(url), timeout=DOWNLOAD_TIMEOUT_S)
        try:
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > limit:
                raise OTAError(f"Content-Length {declared} exceeds limit {limit}")

            expected = max(declared, size_hint, 1)
            free = shutil.disk_usage(download_dir).free
            if free < FREE_DISK_FACTOR * expected:
                raise OTAError(
                    f"insufficient free disk: {free} bytes free, need {FREE_DISK_FACTOR * expected}"
                )

            written = 0
            with dest.open("wb") as f:
                while chunk := resp.read(DOWNLOAD_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise OTAError(f"bundle exceeds size limit {limit} while streaming")
                    f.write(chunk)
        except Exception:
            with contextlib.suppress(OSError):
                dest.unlink()
            raise
        finally:
            with contextlib.suppress(Exception):
                resp.close()

        logger.info("Downloaded %s (%d bytes)", dest, dest.stat().st_size)
        return dest

    def _cleanup_download(self, bundle: Path) -> None:
        with contextlib.suppress(OSError):
            bundle.unlink()

    # -- verify --------------------------------------------------------------

    def _verify(self, data: dict, bundle: Path) -> dict:
        """Full verification chain; raises VerifyError with a reason on the
        first failure. Returns the parsed manifest on success."""
        # (1) Ed25519 signature over the verbatim manifest bytes.
        manifest_str = data.get("manifest")
        if not isinstance(manifest_str, str) or not manifest_str:
            raise VerifyError("target has no manifest")
        try:
            signature = base64.b64decode(data.get("signature") or "", validate=True)
        except (ValueError, TypeError) as e:
            raise VerifyError(f"malformed signature: {e}") from e
        pubkey_pem = self._load_public_key()
        try:
            verify_manifest_signature(manifest_str.encode("utf-8"), signature, pubkey_pem)
        except (ValueError, TypeError, IndexError) as e:
            raise VerifyError(f"manifest signature invalid: {e}") from e

        # (2) Manifest pins product + the exact version we were offered.
        try:
            manifest = json.loads(manifest_str)
        except json.JSONDecodeError as e:
            raise VerifyError(f"manifest is not valid JSON: {e}") from e
        if manifest.get("product") != "wqm1":
            raise VerifyError(f"manifest product {manifest.get('product')!r} != 'wqm1'")
        if manifest.get("version") != data.get("version"):
            raise VerifyError(
                f"manifest version {manifest.get('version')!r} "
                f"!= target version {data.get('version')!r}"
            )

        # (3) Bundle hash pinned by the (signed) manifest.
        digest = sha256_file(bundle)
        if digest != str(manifest.get("sha256", "")).lower():
            raise VerifyError("bundle sha256 does not match manifest")

        # (4) Packaging sanity (tar-slip guard).
        tar_error = validate_tar_members(bundle)
        if tar_error:
            raise VerifyError(tar_error)

        # (5) Downgrade-to-vulnerable guard.
        min_from = manifest.get("minFromVersion")
        if min_from:
            installed = version_tuple(self.installed_version())
            required = version_tuple(min_from)
            if installed is None or required is None or installed < required:
                raise VerifyError(
                    f"installed version {self.installed_version()} < minFromVersion {min_from}"
                )

        return manifest

    # -- apply / rollback ----------------------------------------------------

    def _flip_current(self, target: Path) -> None:
        """Atomically point ``current`` at ``target`` (symlink + os.replace)."""
        tmp = self._root / "current.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(str(target), str(tmp))
        os.replace(str(tmp), str(self._current_link))

    def _restart_services(self) -> None:
        self._run_cmd(["systemctl", "restart", *RESTART_SERVICES])

    def _write_state(self, state: dict) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(self._state_file)
        except OSError as e:
            logger.warning("Could not write OTA state file: %s", e)

    def _read_state(self) -> dict | None:
        try:
            return json.loads(self._state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _apply(self, version: str, bundle: Path) -> str | None:
        """Extract, stage, flip the symlink, restart services.

        Returns the previous ``current`` target path (for rollback), or None
        when there was no previous release.
        """
        staging = self._releases_dir / f"{version}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with tarfile.open(bundle, "r:gz") as tar:
            try:
                # Python >= 3.11.4: the 'data' filter re-rejects unsafe members.
                tar.extractall(staging, filter="data")
            except TypeError:
                # Older 3.11 — members were already vetted by
                # validate_tar_members() during verification.
                tar.extractall(staging)  # noqa: S202

        final = self._releases_dir / version
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)

        previous: str | None = None
        if self._current_link.is_symlink() or self._current_link.exists():
            previous = os.path.realpath(self._current_link)

        self._apply_time_iso = _utc_now_iso()
        self._write_state(
            {
                "phase": "applying",
                "version": version,
                "previous": previous,
                "startedAt": self._apply_time_iso,
            }
        )
        self._flip_current(final)
        self._restart_services()
        return previous

    def _rollback(self, previous: str, version: str, reason: str) -> None:
        """Flip ``current`` back to the previous release and restart. The bad
        release directory is kept for post-mortem."""
        logger.error("OTA %s failed self-test, rolling back: %s", version, reason)
        self._flip_current(Path(previous))
        self._restart_services()
        self._write_state(
            {
                "phase": "rolled_back",
                "version": version,
                "previous": previous,
                "reason": reason,
                "at": _utc_now_iso(),
            }
        )
        self._report("rolled_back", version, reason)

    # -- self-test -----------------------------------------------------------

    def _service_active(self) -> bool:
        try:
            result = self._run_cmd(
                ["systemctl", "is-active", MAIN_SERVICE], capture_output=True, text=True
            )
        except Exception as e:
            logger.warning("systemctl is-active failed: %s", e)
            return False
        stdout = (getattr(result, "stdout", "") or "").strip()
        return getattr(result, "returncode", 1) == 0 and stdout == "active"

    def _has_fresh_reading(self, since_iso: str) -> bool:
        """True when SQLite has a readings row newer than the apply time.
        Opens read-only so the agent can never corrupt the firmware's DB."""
        try:
            conn = sqlite3.connect(f"file:{self._settings.db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            row = conn.execute(
                "SELECT 1 FROM readings WHERE timestamp > ? LIMIT 1", (since_iso,)
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _self_test(self) -> tuple[bool, str | None]:
        """Within the timeout: main service active AND one fresh reading."""
        since = self._apply_time_iso or _utc_now_iso()
        deadline = self._clock() + self._settings.ota_self_test_timeout_s
        reason = "self-test timed out"
        while True:
            if not self._service_active():
                reason = f"{MAIN_SERVICE} not active"
            elif not self._has_fresh_reading(since):
                reason = "no fresh sensor reading since apply"
            else:
                return True, None
            if self._clock() >= deadline:
                return False, reason
            self._sleep(SELF_TEST_POLL_S)

    # -- pruning -------------------------------------------------------------

    def _prune_releases(self, protect: set[str]) -> None:
        """Delete release dirs beyond ``ota_keep_releases`` (newest kept),
        never touching the protected (current/previous) ones."""
        if not self._releases_dir.is_dir():
            return
        entries = [p for p in self._releases_dir.iterdir() if p.is_dir()]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        keep = entries[: max(self._settings.ota_keep_releases, 1)]
        for entry in entries:
            if entry in keep or entry.name in protect:
                continue
            logger.info("Pruning old release %s", entry)
            shutil.rmtree(entry, ignore_errors=True)

    # -- boot-time safety net --------------------------------------------------

    def recover_incomplete_apply(self) -> str | None:
        """If a previous agent run died mid-apply (state file stuck in
        applying/self_test), treat the update as failed: roll back when
        ``current`` still points at the recorded new version."""
        state = self._read_state()
        if not state or state.get("phase") not in _INCOMPLETE_PHASES:
            return None

        version = state.get("version") or "0.0.0"
        previous = state.get("previous")
        reason = "apply interrupted (agent restarted mid-update)"
        logger.warning("Incomplete OTA apply detected for %s — recovering", version)

        current_target = None
        if self._current_link.is_symlink() or self._current_link.exists():
            current_target = os.path.realpath(self._current_link)

        if previous and current_target and Path(current_target).name == version:
            self._rollback(previous, version, reason)
            return "rolled_back"

        self._write_state(
            {"phase": "failed", "version": version, "reason": reason, "at": _utc_now_iso()}
        )
        self._report("failed", version, reason)
        return "failed"

    # -- main cycle ------------------------------------------------------------

    def check_and_apply(self) -> str:
        """One full poll->verify->apply->self-test cycle. Returns the terminal
        phase string for logging/tests. Never raises."""
        try:
            data = self._poll()
        except Exception as e:
            logger.warning("OTA poll failed: %s", e)
            return "error"
        if not data:
            return "idle"

        version = str(data.get("version") or "")
        installed = self.installed_version()
        if not version or version == installed:
            return "idle"
        logger.info("OTA target %s assigned (installed %s)", version, installed)

        try:
            self._report("downloading", version)
            bundle = self._download(data)
        except Exception as e:
            logger.error("OTA download failed: %s", e)
            self._report("failed", version, f"download failed: {e}")
            return "failed"

        try:
            self._report("verifying", version)
            manifest = self._verify(data, bundle)
        except VerifyError as e:
            logger.error("OTA verification failed: %s", e)
            self._report("verify_failed", version, str(e))
            self._cleanup_download(bundle)
            return "verify_failed"
        except Exception as e:
            logger.error("OTA verification errored: %s", e)
            self._report("verify_failed", version, f"verification error: {e}")
            self._cleanup_download(bundle)
            return "verify_failed"

        if manifest.get("requiresDeps"):
            logger.error("OTA %s requires manual dependency update — refusing", version)
            self._report("failed", version, "requires manual dependency update")
            self._cleanup_download(bundle)
            return "failed"

        try:
            self._report("applying", version)
            previous = self._apply(version, bundle)
        except Exception as e:
            logger.error("OTA apply failed: %s", e)
            self._report("failed", version, f"apply failed: {e}")
            self._cleanup_download(bundle)
            return "failed"
        finally:
            self._cleanup_download(bundle)

        self._write_state(
            {
                "phase": "self_test",
                "version": version,
                "previous": previous,
                "startedAt": self._apply_time_iso,
            }
        )
        self._report("self_test", version)
        ok, reason = self._self_test()

        if ok:
            logger.info("OTA %s applied and self-tested OK", version)
            self._report("success", version)
            self._write_state({"phase": "success", "version": version, "appliedAt": _utc_now_iso()})
            protect = {version}
            if previous:
                protect.add(Path(previous).name)
            self._prune_releases(protect)
            return "success"

        if not previous:
            message = f"self-test failed and no previous release to roll back to: {reason}"
            logger.critical("OTA %s: %s", version, message)
            self._report("failed", version, message)
            self._write_state(
                {"phase": "failed", "version": version, "reason": message, "at": _utc_now_iso()}
            )
            return "failed"

        self._rollback(previous, version, reason or "self-test failed")
        return "rolled_back"

    # -- long-running loop -------------------------------------------------------

    def run_forever(self, stop_event: Any) -> None:
        """Poll on ``ota_poll_s`` with +/-20% jitter until ``stop_event`` is
        set. A ``{state_dir}/ota-check-now`` flag file (touched by the main
        firmware on an ota_check command / otaPending) triggers an immediate
        poll — checked every wait tick."""
        with contextlib.suppress(Exception):
            self.recover_incomplete_apply()

        while not stop_event.is_set():
            try:
                result = self.check_and_apply()
                logger.debug("OTA cycle: %s", result)
            except Exception:  # pragma: no cover — check_and_apply never raises
                logger.exception("OTA cycle crashed")

            wait_s = self._settings.ota_poll_s * random.uniform(0.8, 1.2)
            deadline = self._clock() + wait_s
            while not stop_event.is_set() and self._clock() < deadline:
                if self._check_now_flag.exists():
                    with contextlib.suppress(OSError):
                        self._check_now_flag.unlink()
                    logger.info("ota-check-now flag seen — polling immediately")
                    break
                self._sleep(1)
