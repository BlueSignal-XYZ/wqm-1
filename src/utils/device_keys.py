"""
Device Ed25519 identity and the HTTP device key.

The private key is generated once, on first boot, and kept on the persistent
data path (``/var/lib/bluesignal``). That directory survives OTA release
swaps of ``/opt/bluesignal/releases/<ver>`` and the ``current`` symlink. It
is never stored inside a release tree.

Writes are atomic (temp file + fsync + rename + directory fsync) and the
secret files are mode 0600. ``ensure_keypair`` is idempotent: a present,
valid seed is never regenerated. After an unclean power loss the completed
file is used; a leftover ``.tmp`` of the right size is promoted, a truncated
one is discarded.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from utils.identity import get_device_id, is_label_serial, read_provisioned_identity

logger = logging.getLogger("wqm1.device_keys")

# Default on a real unit. Tests and a chroot-style root override this; see
# ``state_dir``. Never derived from ``__file__`` — that would put secrets
# inside a release that OTA replaces.
DEFAULT_STATE_DIR = Path("/var/lib/bluesignal")
STATE_DIR_ENV = "BLUESIGNAL_STATE_DIR"
ROOT_ENV = "BLUESIGNAL_ROOT"

PRIVATE_KEY_NAME = "device-ed25519.key"
HTTP_KEY_NAME = "device-http.key"
REGISTER_STATE_NAME = "device-register.json"

SEED_BYTES = 32
FILE_MODE = 0o600

REGISTER_STATUSES = ("pending", "registered", "conflict", "error")


def state_dir() -> Path:
    """Persistent data directory. Survives OTA; never a release path."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    root = os.environ.get(ROOT_ENV)
    if root:
        return Path(root) / "var" / "lib" / "bluesignal"
    return DEFAULT_STATE_DIR


def private_key_path() -> Path:
    return state_dir() / PRIVATE_KEY_NAME


def http_key_path() -> Path:
    return state_dir() / HTTP_KEY_NAME


def register_state_path() -> Path:
    return state_dir() / REGISTER_STATE_NAME


def _assert_not_in_release(path: Path) -> None:
    """Refuse to place secrets under the OTA-swappable tree."""
    parts = {p.lower() for p in path.resolve().parts}
    # A path of the form .../opt/bluesignal/releases/<ver>/... or
    # .../opt/bluesignal/current/... is a release. /opt/bluesignal itself
    # is the install root; we only reject the release subtrees.
    resolved = path.resolve()
    text = str(resolved)
    if (
        "/opt/bluesignal/releases/" in text
        or "/opt/bluesignal/current/" in text
        or text.endswith("/opt/bluesignal/current")
    ):
        raise RuntimeError(f"refusing to store a device secret inside a release: {resolved}")
    if "releases" in parts and "bluesignal" in parts and "opt" in parts:
        # chroot-style root: <root>/opt/bluesignal/releases/...
        raise RuntimeError(f"refusing to store a device secret inside a release: {resolved}")


def atomic_write_bytes(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Write ``data`` so a power cut cannot leave a half file as the live path."""
    _assert_not_in_release(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(parent, os.O_RDONLY)
    except OSError as e:
        logger.debug("identity dir open %s failed: %s", parent, e)
        return
    try:
        os.fsync(dir_fd)
    except OSError as e:
        logger.debug("identity dir fsync %s failed: %s", parent, e)
    finally:
        os.close(dir_fd)


def b64url_encode(raw: bytes) -> str:
    """URL-safe base64 without padding (contract §1 / §2)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _read_seed(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != SEED_BYTES:
        logger.error(
            "Identity key at %s is %d bytes, expected %d — not regenerating",
            path,
            len(data),
            SEED_BYTES,
        )
        return None
    return data


def _promote_tmp(path: Path) -> bytes | None:
    """A crash after fsync but before rename left a valid seed in ``.tmp``."""
    tmp = path.with_name(path.name + ".tmp")
    seed = _read_seed(tmp)
    if seed is None:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return None
    try:
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Could not promote identity key tmp: %s", e)
        return seed if path.exists() and _read_seed(path) else seed
    return _read_seed(path) or seed


def _key_from_seed(seed: bytes) -> ECC.EccKey:
    return ECC.construct(curve="Ed25519", seed=seed)


def ensure_keypair() -> bytes:
    """Return the 32-byte public key, generating the seed once if absent.

    Disk is the source of truth. Two first-boot processes racing both end up
    using whichever seed won the rename, never a discarded in-memory pair.
    """
    path = private_key_path()
    seed = _read_seed(path)
    if seed is None:
        seed = _promote_tmp(path)
        if seed is None and path.exists():
            raise RuntimeError(
                f"identity key at {path} is present but unreadable; not regenerating"
            )
    if seed is not None:
        with contextlib.suppress(OSError):
            os.chmod(path, FILE_MODE)
        return _key_from_seed(seed).public_key().export_key(format="raw")

    generated = ECC.generate(curve="Ed25519")
    new_seed = generated.seed
    if not isinstance(new_seed, (bytes, bytearray)) or len(new_seed) != SEED_BYTES:
        raise RuntimeError("Ed25519 seed was not 32 bytes")
    atomic_write_bytes(path, bytes(new_seed), FILE_MODE)
    disk = _read_seed(path)
    if disk is None:
        raise RuntimeError("identity key did not persist")
    logger.info("Generated device identity key at %s (mode 0600)", path)
    return _key_from_seed(disk).public_key().export_key(format="raw")


def public_key_raw() -> bytes:
    return ensure_keypair()


def public_key_b64url() -> str:
    return b64url_encode(public_key_raw())


def sign_bytes(message: bytes) -> bytes:
    """Ed25519 signature (64 bytes) over ``message`` with the device seed."""
    path = private_key_path()
    seed = _read_seed(path)
    if seed is None:
        ensure_keypair()
        seed = _read_seed(path)
    if seed is None:
        raise RuntimeError("identity key missing")
    key = _key_from_seed(seed)
    return eddsa.new(key, "rfc8032").sign(message)


def identity_qr_payload() -> dict[str, Any]:
    """v2 identity JSON for the QR and the setup page (contract §1)."""
    device_id = get_device_id()
    payload: dict[str, Any] = {"v": 2, "device_id": device_id}
    provisioned = read_provisioned_identity()
    label = None
    if is_label_serial(device_id):
        label = device_id
    elif provisioned is not None:
        serial = str(provisioned.get("serial") or "")
        if is_label_serial(serial):
            label = serial
    if label:
        payload["label"] = label
    payload["pk"] = public_key_b64url()
    return payload


def load_http_key() -> str:
    path = http_key_path()
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError:
        return ""
    return text


def store_http_key(api_key: str) -> None:
    """Persist the cloud HTTP device key once (0600, atomic)."""
    key = api_key.strip()
    if not key:
        raise ValueError("refusing to store an empty HTTP device key")
    atomic_write_bytes(http_key_path(), key.encode("ascii"), FILE_MODE)


def default_register_state() -> dict[str, Any]:
    return {
        "status": "pending",
        "attempts": 0,
        "backoff_s": 5,
        "next_attempt_unix": 0,
        "last_http": 0,
        "last_code": "",
        "last_error": "",
    }


def load_register_state() -> dict[str, Any]:
    path = register_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_register_state()
    if not isinstance(data, dict):
        return default_register_state()
    out = default_register_state()
    out.update({k: data[k] for k in out if k in data})
    status = str(out.get("status") or "pending")
    if status not in REGISTER_STATUSES:
        out["status"] = "pending"
    return out


def store_register_state(state: dict[str, Any]) -> None:
    payload = default_register_state()
    payload.update(state)
    if payload["status"] not in REGISTER_STATUSES:
        payload["status"] = "pending"
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write_bytes(register_state_path(), text.encode("utf-8"), FILE_MODE)


def registration_status() -> str:
    """One of pending / registered / conflict / error for status files."""
    if load_http_key() and load_register_state().get("status") == "registered":
        return "registered"
    return str(load_register_state().get("status") or "pending")
