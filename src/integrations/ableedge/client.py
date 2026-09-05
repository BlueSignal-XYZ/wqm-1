"""
Eaton AbleEdge / Smart Breaker HTTP client (stdlib urllib only).

Auth: POST /api/v1/serviceAccount/authToken with Client ID + secret, plus
``Em-Api-Subscription-Key`` on every request. Device commands use the
remote-handle position API (close = circuit ON / load energized; open =
circuit OFF). Energy comes from meter telemetry — measured values only.

This module never invents L/day, nameplate amps, or circuit ampacity.
Live smoke is blocked until jacques@bluesignal.xyz supplies the developer
application credentials; do not commit them.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from integrations.ableedge.errors import AbleEdgeAuthError, AbleEdgeUnreachableError
from integrations.ableedge.schema import DEFAULT_API_BASE, LoadControlConfig
from integrations.ableedge.secrets import AbleEdgeSecrets

logger = logging.getLogger("wqm1.ableedge")

AUTH_PATH = "/api/v1/serviceAccount/authToken"
DEVICE_PATH = "/api/v1/devices/{device_id}"
POSITION_PATH = "/api/v1/devices/{device_id}/breaker/remoteHandle/position"
CONNECTED_PATH = "/api/v1/devices/{device_id}/device/metadata/isConnected"
POWER_PATH = "/api/v1/devices/{device_id}/data/telemetry/meter/reading"

# Refresh a minute before Eaton's expiresAt when we have one; else 50 minutes
# (tokens are documented as ~1 hour).
_TOKEN_SKEW_S = 60
_DEFAULT_TOKEN_TTL_S = 50 * 60


@dataclass(frozen=True)
class CircuitStatus:
    device_id: str
    circuit_id: str
    on: bool
    reachable: bool
    connected: bool | None = None
    position: str | None = None


@dataclass(frozen=True)
class PowerReading:
    """Measured meter values. Missing fields stay None — never guessed."""

    watts: float | None = None
    volts: float | None = None
    amps: float | None = None
    energy_wh: float | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class CircuitCommandResult:
    ok: bool
    on: bool
    via: str
    reachable: bool
    error: str | None = None
    fail_safe_applied: str | None = None


class AbleEdgeClient(Protocol):
    """Device client: authenticate, status, on/off, power."""

    def authenticate(self) -> bool: ...

    def get_status(self) -> CircuitStatus: ...

    def set_circuit(self, state: bool, reason: str = "") -> CircuitCommandResult: ...

    def get_power(self) -> PowerReading: ...


class HttpAbleEdgeClient:
    """Thin urllib client for api.em.eaton.com. Secrets stay in memory only."""

    def __init__(
        self,
        config: LoadControlConfig,
        secrets: AbleEdgeSecrets,
        *,
        timeout_s: float = 15.0,
        sleeper: Any = time.sleep,
    ) -> None:
        if not config.device_id:
            raise AbleEdgeUnreachableError("AbleEdge device_id is not bound")
        if not secrets.complete:
            raise AbleEdgeAuthError("AbleEdge credentials are incomplete")
        self._cfg = config
        self._secrets = secrets
        self._timeout_s = timeout_s
        self._sleep = sleeper
        self._token: str = ""
        self._token_expires_mono: float = 0.0

    def authenticate(self) -> bool:
        """Obtain (or refresh) a service-account bearer token."""
        status, body = self._request(
            "POST",
            AUTH_PATH,
            {"clientId": self._secrets.client_id, "clientSecret": self._secrets.client_secret},
            authed=False,
        )
        data = (body or {}).get("data") if isinstance(body, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            logger.warning("AbleEdge auth failed (HTTP %s)", status)
            self._token = ""  # nosec B105 — clear cached token, not a password
            self._token_expires_mono = 0.0
            raise AbleEdgeAuthError(f"auth HTTP {status}")
        self._token = token
        self._token_expires_mono = _expiry_mono(
            data.get("expiresAt") if isinstance(data, dict) else None
        )
        logger.info("AbleEdge service-account token acquired")
        return True

    def get_status(self) -> CircuitStatus:
        self._ensure_token()
        pos_status, pos_body = self._request(
            "GET", POSITION_PATH.format(device_id=self._cfg.device_id)
        )
        if pos_status == 0 or pos_status >= 400:
            raise AbleEdgeUnreachableError(f"status HTTP {pos_status}")
        position = _nested_str(pos_body, "position")
        on = _position_is_on(position)
        connected: bool | None = None
        conn_status, conn_body = self._request(
            "GET", CONNECTED_PATH.format(device_id=self._cfg.device_id)
        )
        if conn_status == 200:
            raw = _nested(conn_body, "isConnected")
            if isinstance(raw, bool):
                connected = raw
        return CircuitStatus(
            device_id=self._cfg.device_id,
            circuit_id=self._cfg.bound_circuit_id,
            on=on,
            reachable=True,
            connected=connected,
            position=position,
        )

    def set_circuit(self, state: bool, reason: str = "") -> CircuitCommandResult:
        """close = ON (energized), open = OFF. Eaton remote-handle mapping."""
        self._ensure_token()
        command = "close" if state else "open"
        why = reason or ("WQM-1 AWG load on" if state else "WQM-1 AWG load off")
        http_status, _ = self._request(
            "POST",
            POSITION_PATH.format(device_id=self._cfg.device_id),
            {"command": command, "reason": why},
        )
        # Official docs: 204 No Content. Some preview paths return 202.
        if http_status not in (200, 202, 204):
            raise AbleEdgeUnreachableError(f"set_circuit HTTP {http_status}")
        return CircuitCommandResult(ok=True, on=state, via="ableedge", reachable=True)

    def get_power(self) -> PowerReading:
        self._ensure_token()
        http_status, body = self._request("GET", POWER_PATH.format(device_id=self._cfg.device_id))
        if http_status != 200 or not isinstance(body, dict):
            raise AbleEdgeUnreachableError(f"get_power HTTP {http_status}")
        return parse_meter_reading(body)

    def _ensure_token(self) -> None:
        if self._token and time.monotonic() < self._token_expires_mono:
            return
        self.authenticate()

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        authed: bool = True,
    ) -> tuple[int, dict[str, Any] | None]:
        url = f"{self._cfg.api_base or DEFAULT_API_BASE}{path}"
        if not url.lower().startswith(("http://", "https://")):
            logger.error("Refusing non-HTTP(S) AbleEdge URL")
            return 0, None
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Em-Api-Subscription-Key": self._secrets.subscription_key,
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if authed:
            if not self._token:
                return 0, None
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            # B310: scheme checked above (HTTP/S only) — Eaton API base from config.
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:  # nosec B310
                if resp.status == 204:
                    return 204, None
                raw = resp.read().decode("utf-8") or "{}"
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    return resp.status, None
                return resp.status, parsed if isinstance(parsed, dict) else None
        except urllib.error.HTTPError as e:
            logger.warning("AbleEdge %s %s -> HTTP %d", method, path, e.code)
            if e.code in (401, 403) and authed:
                self._token = ""  # nosec B105 — drop stale bearer on 401/403
            return e.code, None
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning("AbleEdge %s %s failed: %s", method, path, e)
            return 0, None


def parse_meter_reading(body: dict[str, Any]) -> PowerReading:
    """Map Eaton meter telemetry to measured fields only (no nameplate fill-in)."""
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return PowerReading()
    volts = _meter_val(data, "voltageAN")
    if volts is None:
        volts = _meter_val(data, "voltageBN")
    amps = _meter_val(data, "currentA")
    if amps is None:
        amps = _meter_val(data, "currentB")
    watts = None
    if volts is not None and amps is not None:
        watts = volts * amps
    energy = data.get("energy") if isinstance(data.get("energy"), dict) else {}
    energy_wh = None
    if isinstance(energy, dict):
        for key in ("deliveredWH", "delivered_wh"):
            raw = energy.get(key)
            if isinstance(raw, int | float) and not isinstance(raw, bool):
                energy_wh = float(raw)
                break
    ts = data.get("ts")
    timestamp = str(ts) if ts is not None and ts != "" else None
    return PowerReading(
        watts=watts, volts=volts, amps=amps, energy_wh=energy_wh, timestamp=timestamp
    )


def _meter_val(data: dict[str, Any], key: str) -> float | None:
    raw = data.get(key)
    if isinstance(raw, dict):
        raw = raw.get("val")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        return float(raw)
    return None


def _nested(body: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return None
    return data.get(key)


def _nested_str(body: dict[str, Any] | None, key: str) -> str | None:
    raw = _nested(body, key)
    return str(raw) if isinstance(raw, str) and raw else None


def _position_is_on(position: str | None) -> bool:
    """Eaton: close = contacts made = load ON; open = load OFF."""
    if not position:
        return False
    return position.strip().lower() in {"close", "closed", "on"}


def _expiry_mono(expires_at: Any) -> float:
    now = time.monotonic()
    if isinstance(expires_at, str) and expires_at:
        try:
            text = expires_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            remaining = dt.timestamp() - time.time() - _TOKEN_SKEW_S
            return now + max(1.0, remaining)
        except ValueError:
            pass
    return now + _DEFAULT_TOKEN_TTL_S
