"""
Self-registration with BlueSignal Cloud (device-onboarding v2).

After the unit has a link it POSTs ``/v2/devices/register`` signed with its
Ed25519 identity key. The installer never pastes an HTTP key.

Offline sites: pending state is persisted and retried with backoff (5s → 1h
cap) across reboots. A 409 ``pk_conflict`` stops — the card and the cloud
disagree and a human has to look. ``stale_ts`` applies the server's
``serverTime`` as a clock offset and retries; without that field it waits
for NTP instead of signing again in a 1s loop. ``bad_sig`` does not
generate a different key. HTTP 202 ``pending_identity`` stays pending.
A 401 on heartbeat/ingest clears the HTTP key and re-registers.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from service_window.config_editor import update_config
from utils.clock import ClockDiscipline
from utils.config import FIRMWARE_VERSION
from utils.device_keys import (
    b64url_encode,
    clear_http_key,
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
# After a stale_ts with no server clock, wait at least this long for NTP
# instead of signing the same wrong ts every second. The Pi Zero W has no RTC.
STALE_WITHOUT_NTP_S = 60
PENDING_IDENTITY_MESSAGE = "Waiting for installer to assign this unit."

_CLOCK: Callable[[], float] = time.time
_active_config_path = DEFAULT_CONFIG_PATH
_REVOKE_LOCK = threading.Lock()


def set_config_path(path: str) -> None:
    """Where a revoked HTTP key should be stripped from config.yaml."""
    global _active_config_path
    _active_config_path = path or DEFAULT_CONFIG_PATH


def _ntp_synced() -> bool:
    try:
        return ClockDiscipline().ntp_synced()
    except Exception:  # noqa: BLE001 — absence of timedatectl is unsynced
        return False


def _now_ms() -> int:
    offset = int(load_register_state().get("ts_offset_ms") or 0)
    return int(_CLOCK() * 1000) + offset


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
    status: str,
    *,
    attempts: int,
    http: int,
    code: str,
    error: str,
    delay: int,
    ts_offset_ms: int | None = None,
) -> dict[str, Any]:
    now = int(_CLOCK())
    fields: dict[str, Any] = {
        "status": status,
        "attempts": attempts,
        "backoff_s": delay,
        "next_attempt_unix": now + delay,
        "last_http": http,
        "last_code": code,
        "last_error": error[:200],
    }
    if ts_offset_ms is not None:
        fields["ts_offset_ms"] = int(ts_offset_ms)
    return _write_state(**fields)


def _coerce_epoch_ms(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        if value > 10**12:
            return value
        if value > 10**9:
            return value * 1000
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.isdigit():
            return _coerce_epoch_ms(int(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    return None


def _extract_server_time_ms(payload: dict[str, Any] | None) -> int | None:
    """Marketplace 401 stale_ts body: ``serverTime`` (unix ms)."""
    if not payload:
        return None
    candidates: list[Any] = [
        payload.get("serverTime"),
        payload.get("server_time"),
        payload.get("serverTimeMs"),
        payload.get("server_time_ms"),
    ]
    nested = payload.get("error")
    if isinstance(nested, dict):
        candidates.extend(
            (
                nested.get("serverTime"),
                nested.get("server_time"),
                nested.get("serverTimeMs"),
            )
        )
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend((data.get("serverTime"), data.get("server_time")))
    for raw in candidates:
        ms = _coerce_epoch_ms(raw)
        if ms is not None:
            return ms
    return None


def _date_header_ms(headers: Any) -> int | None:
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Date") or headers.get("date")
    except Exception:  # noqa: BLE001
        raw = None
    return _coerce_epoch_ms(raw)


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
        payload = _parse_json(raw) or {}
        if e.code == 401 and _extract_server_time_ms(payload) is None:
            header_ms = _date_header_ms(getattr(e, "headers", None) or getattr(e, "hdrs", None))
            if header_ms is not None:
                payload["serverTime"] = header_ms
        return e.code, payload or None
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
        server_ms = _extract_server_time_ms(payload)
        local_ms = int(_CLOCK() * 1000)
        if server_ms is not None:
            offset = server_ms - local_ms
            logger.warning(
                "Register ts rejected as stale; applying serverTime offset %+d ms",
                offset,
            )
            return _due_state(
                "pending",
                attempts=attempts,
                http=http,
                code=code,
                error="stale_ts",
                delay=BACKOFF_S[0],
                ts_offset_ms=offset,
            )
        if _ntp_synced():
            logger.warning("Register ts rejected as stale; NTP is synced, retrying shortly")
            return _due_state(
                "pending",
                attempts=attempts,
                http=http,
                code=code,
                error="stale_ts",
                delay=BACKOFF_S[0],
            )
        delay = max(_backoff_for(attempts), STALE_WITHOUT_NTP_S)
        logger.warning(
            "Register ts rejected as stale and no serverTime; waiting %ss for NTP",
            delay,
        )
        return _due_state(
            "pending",
            attempts=attempts,
            http=http,
            code=code,
            error="stale_ts",
            delay=delay,
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

    if http == 202 or code == "pending_identity":
        delay = _backoff_for(attempts)
        logger.info(
            "Register pending_identity (HTTP %s); retry in %ss until the installer assigns",
            http or 202,
            delay,
        )
        return _due_state(
            "pending",
            attempts=attempts,
            http=http or 202,
            code=code or "pending_identity",
            error="pending_identity",
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


def note_http_key_revoked(*, reason: str = "unauthorized") -> dict[str, Any]:
    """Clear a rejected HTTP key and schedule re-registration. Idempotent.

    Heartbeat / ingest 401 means the cloud revoked or disabled the key
    (unclaim, rotation). Transient 5xx must not call this.
    """
    with _REVOKE_LOCK:
        had_key = bool(load_http_key())
        clear_http_key()
        cfg = Path(_active_config_path)
        if cfg.is_file():
            try:
                update_config(str(cfg), {"api_key": ""})
            except Exception as e:  # noqa: BLE001 — file unlink is the source of truth
                logger.warning("Could not clear api_key in config.yaml: %s", e)
        state = load_register_state()
        status_now = str(state.get("status") or "pending")
        if not had_key and status_now == "pending":
            return state
        attempts = int(state.get("attempts") or 0) + 1
        logger.warning("HTTP device key rejected (%s); will re-register with backoff", reason)
        return _due_state(
            "pending",
            attempts=attempts,
            http=401,
            code="key_revoked",
            error=reason,
            delay=_backoff_for(attempts),
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
    message = copies.get(status, copies["pending"])
    if str(state.get("last_code") or "") == "pending_identity":
        message = PENDING_IDENTITY_MESSAGE
    return {
        "status": status,
        "message": message,
        "code": str(state.get("last_code") or ""),
        "pk": identity_qr_payload().get("pk", ""),
    }
