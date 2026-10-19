"""
Self-registration with BlueSignal Cloud (device-onboarding v2).

After the unit has a link it POSTs ``/v2/devices/register`` signed with its
Ed25519 identity key. The installer never pastes an HTTP key.

Offline sites: pending state is persisted and retried with backoff (5s → 1h
cap) across reboots. A 409 ``pk_conflict`` stops — the card and the cloud
disagree and a human has to look. ``stale_ts`` retries immediately with a
fresh clock. ``bad_sig`` does not generate a different key.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from service_window.config_editor import update_config
from utils.config import FIRMWARE_VERSION
from utils.device_keys import (
    b64url_encode,
    identity_qr_payload,
    load_http_key,
    load_register_state,
    public_key_b64url,
    registration_status,
    sign_bytes,
    store_http_key,
    store_register_state,
)
from utils.identity import get_device_id

logger = logging.getLogger("wqm1.register")

REGISTER_PATH = "/v2/devices/register"
DEFAULT_API_BASE = "https://us-central1-waterquality-trading.cloudfunctions.net/app"
DEFAULT_CONFIG_PATH = "/etc/bluesignal/config.yaml"
REQUEST_TIMEOUT_S = 15.0

# Poorly covered sites may get a link hours or days later. Cap at one hour
# so a dark site is not a tight loop, and not a wait of days either.
BACKOFF_S = (5, 15, 30, 60, 300, 900, 3600)
STOP_BACKOFF_S = 3600

_CLOCK: Callable[[], float] = time.time


def _now_ms() -> int:
    return int(time.time() * 1000)


def register_message(device_id: str, ts: int) -> str:
    """UTF-8 signed material: ``register|<device_id>|<ts>`` exactly."""
    return f"register|{device_id}|{ts}"


def build_register_body(
    *,
    device_id: str | None = None,
    fw: str | None = None,
    ts: int | None = None,
) -> dict[str, Any]:
    """Signed POST body. ``pk`` is not in the signed message (contract §2)."""
    serial = device_id or get_device_id()
    timestamp = int(_now_ms() if ts is None else ts)
    message = register_message(serial, timestamp).encode("utf-8")
    sig = b64url_encode(sign_bytes(message))
    return {
        "device_id": serial,
        "pk": public_key_b64url(),
        "fw": fw or FIRMWARE_VERSION,
        "ts": timestamp,
        "sig": sig,
    }


def _error_code(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    code = payload.get("code")
    err = payload.get("error")
    if not code and isinstance(err, dict):
        code = err.get("code") or err.get("status")
    if not code and isinstance(err, str):
        code = err
    return str(code or "")


def _parse_json(raw: bytes | str) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_api_key(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    key = payload.get("api_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("api_key")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _persist_http_key(api_key: str, config_path: str) -> None:
    store_http_key(api_key)
    try:
        update_config(config_path, {"api_key": api_key, "cloud_enabled": True})
    except Exception as e:  # noqa: BLE001 — the 0600 file is the source of truth
        logger.warning("HTTP device key stored, but config.yaml was not updated: %s", e)


def _write_state(**fields: Any) -> dict[str, Any]:
    state = load_register_state()
    state.update(fields)
    store_register_state(state)
    return state


def _backoff_for(attempts: int) -> int:
    idx = max(0, attempts - 1)
    return BACKOFF_S[min(idx, len(BACKOFF_S) - 1)]


def _due_state(
    status: str, *, attempts: int, http: int, code: str, error: str, delay: int
) -> dict[str, Any]:
    now = int(_CLOCK())
    return _write_state(
        status=status,
        attempts=attempts,
        backoff_s=delay,
        next_attempt_unix=now + delay,
        last_http=http,
        last_code=code,
        last_error=error[:200],
    )


def _post_register(
    url: str, body: dict[str, Any], timeout_s: float
) -> tuple[int, dict[str, Any] | None]:
    if not url.lower().startswith(("http://", "https://")):
        logger.error("Refusing non-HTTP(S) register URL: %s", url)
        return 0, None
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec B310
            raw = resp.read()
            return resp.status, _parse_json(raw)
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:  # noqa: BLE001
            raw = b""
        return e.code, _parse_json(raw)
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("Register POST failed: %s", e)
        return 0, {"error": str(e)}


def attempt_register(
    *,
    api_base: str | None = None,
    device_id: str | None = None,
    fw: str | None = None,
    config_path: str = DEFAULT_CONFIG_PATH,
    timeout_s: float = REQUEST_TIMEOUT_S,
    force: bool = False,
) -> dict[str, Any]:
    """One registration attempt. Persists pending / registered / conflict / error.

    ``force`` re-registers the same pk (HTTP key rotation after a key-file wipe).
    """
    state = load_register_state()
    status_now = str(state.get("status") or "pending")
    if status_now == "conflict" and not force:
        return state
    if status_now == "registered" and load_http_key() and not force:
        return state

    serial = device_id or get_device_id()
    base = (api_base or DEFAULT_API_BASE).rstrip("/")
    url = f"{base}{REGISTER_PATH}"
    body = build_register_body(device_id=serial, fw=fw)
    http, payload = _post_register(url, body, timeout_s)
    code = _error_code(payload)
    attempts = int(state.get("attempts") or 0) + 1

    if http == 200:
        api_key = _extract_api_key(payload)
        if not api_key:
            logger.error("Register HTTP 200 did not return an api_key")
            return _due_state(
                "error",
                attempts=attempts,
                http=http,
                code="missing_api_key",
                error="register succeeded but no api_key in the body",
                delay=_backoff_for(attempts),
            )
        _persist_http_key(api_key, config_path)
        logger.info("Device registered with BlueSignal Cloud")
        return _write_state(
            status="registered",
            attempts=attempts,
            backoff_s=STOP_BACKOFF_S,
            next_attempt_unix=int(_CLOCK()) + STOP_BACKOFF_S,
            last_http=http,
            last_code="",
            last_error="",
        )

    if http == 409 or code == "pk_conflict":
        logger.error(
            "Registration conflict (HTTP %s code=%s): this unit's identity "
            "key does not match the cloud record. Not retrying.",
            http,
            code or "pk_conflict",
        )
        return _write_state(
            status="conflict",
            attempts=attempts,
            backoff_s=STOP_BACKOFF_S,
            next_attempt_unix=int(_CLOCK()) + STOP_BACKOFF_S,
            last_http=http or 409,
            last_code=code or "pk_conflict",
            last_error="pk_conflict",
        )

    if http == 401 and code == "stale_ts":
        logger.warning("Register ts rejected as stale — will retry with a current clock")
        return _due_state(
            "pending",
            attempts=max(0, attempts - 1),
            http=http,
            code=code,
            error="stale_ts",
            delay=1,
        )

    if http == 401 and code == "bad_sig":
        logger.error(
            "Register signature rejected (bad_sig). The identity key is not "
            "being replaced. Will not tight-loop."
        )
        return _due_state(
            "error",
            attempts=attempts,
            http=http,
            code=code,
            error="bad_sig",
            delay=STOP_BACKOFF_S,
        )

    if http == 429 or http >= 500 or http == 0:
        delay = _backoff_for(attempts)
        logger.warning(
            "Register not complete (HTTP %s code=%s); retry in %ss",
            http,
            code or "offline",
            delay,
        )
        return _due_state(
            "pending",
            attempts=attempts,
            http=http,
            code=code or ("offline" if http == 0 else ""),
            error=code or "offline",
            delay=delay,
        )

    delay = _backoff_for(attempts)
    logger.warning("Register refused HTTP %s code=%s", http, code)
    return _due_state(
        "error",
        attempts=attempts,
        http=http,
        code=code,
        error=code or f"http_{http}",
        delay=delay,
    )


def kick_register(**kwargs: Any) -> dict[str, Any]:
    """One attempt used by the setup join path. Never raises."""
    try:
        return attempt_register(**kwargs)
    except Exception as e:  # noqa: BLE001 — join must still finish
        logger.warning("kick_register failed: %s", e)
        return _due_state(
            "pending",
            attempts=int(load_register_state().get("attempts") or 0) + 1,
            http=0,
            code="",
            error=str(e),
            delay=_backoff_for(1),
        )


def registration_view() -> dict[str, str]:
    """Installer-facing copy for the setup page. Never includes secrets."""
    status = registration_status()
    copies = {
        "pending": (
            "Waiting for a link to register this unit with BlueSignal Cloud. "
            "No key paste is needed — the unit proves itself."
        ),
        "registered": "This unit registered itself with BlueSignal Cloud.",
        "conflict": (
            "This unit's identity key does not match the cloud record. "
            "Stop and call support — do not keep retrying."
        ),
        "error": ("Registration did not complete. The unit will retry when it has a link."),
    }
    state = load_register_state()
    return {
        "status": status,
        "message": copies.get(status, copies["pending"]),
        "code": str(state.get("last_code") or ""),
        "pk": identity_qr_payload().get("pk", ""),
    }
