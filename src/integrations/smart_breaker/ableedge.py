"""
Eaton AbleEdge / Smart Breaker API client (thin, stdlib-only).

Talks to ONE Eaton smart breaker — the one the installer bound to this site's
AWG circuit — over the public Smart Breaker REST API (formerly the "Energy
Management" / EM API, now surfaced through Eaton's Brightlayer developer
portal). Endpoints and payloads below follow the published v1.20 reference:

* ``POST {token_url}``                — OAuth2 client-credentials; Basic auth
                                        with client id + secret; bearer token
                                        valid ~1 h.
* ``GET  /devices/{id}/breaker/remoteHandle/position``
                                      — ``{"data": {"position": "open"|"close"}}``
* ``POST /devices/{id}/breaker/remoteHandle/position``
                                      — ``{"command": "open"|"close", "reason": …}``
                                        → 204. SB-only (418 on EV chargers).
* ``GET  /devices/{id}/device/metadata/isConnected``
                                      — breaker ↔ Eaton cloud link.
* ``GET  /devices/{id}/data/telemetry/meter/reading``
                                      — live meter sample (current, voltage,
                                        delivered energy).

Every request carries the API subscription key (``Em-Api-Subscription-Key``)
and an organisation service-account bearer token. Eaton's newer AbleEdge
portal names the key header ``api-key``; the header name is therefore a
constructor argument so live smoke can flip it without a code change.

Vocabulary inversion lives HERE and nowhere else: the load is *on* when the
breaker is *closed*. Callers say ``set_circuit(on=True)``; this module says
``"command": "close"``.

Uses only ``urllib`` (like :mod:`cloud.client`) so the Pi gains no dependency.
No retry loop of its own: the controller owns cadence, and Eaton rate-limits
(429) — hammering a failing call is the wrong reflex here.

Credentials never live in this file. They come from ``/etc/bluesignal/
config.yaml`` (``smart_breaker_client_id`` etc., provisioning-time only) or,
once the proxy ships, from the BlueSignal Cloud Functions.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from integrations.smart_breaker.base import (
    AuthError,
    CircuitStatus,
    DeviceUnavailable,
    NotBound,
    PowerReading,
    RateLimited,
    SmartBreakerError,
    Unreachable,
    UnsupportedCommand,
)

logger = logging.getLogger("wqm1.smart_breaker.ableedge")

DEFAULT_API_BASE = "https://api.em.eaton.com/api/v1"
# B105: a public endpoint URL, not a credential — bandit keys on the word TOKEN.
DEFAULT_TOKEN_URL = "https://api.em.eaton.com/oauth2/token"  # nosec B105
DEFAULT_SUBSCRIPTION_HEADER = "Em-Api-Subscription-Key"

# Refresh this many seconds before the token's stated expiry so a request
# issued at the boundary never goes out with a token that dies in flight.
_TOKEN_SKEW_S = 60
# When the token response omits expires_in, assume the documented lifetime.
_DEFAULT_TOKEN_TTL_S = 3600

_POSITION_ON = {"close", "closed"}
_POSITION_OFF = {"open", "opened"}

Opener = Callable[..., Any]


class AbleEdgeClient:
    """One bound circuit on the Eaton Smart Breaker API."""

    def __init__(
        self,
        device_id: str,
        client_id: str,
        client_secret: str,
        subscription_key: str,
        api_base: str = DEFAULT_API_BASE,
        token_url: str = DEFAULT_TOKEN_URL,
        subscription_header: str = DEFAULT_SUBSCRIPTION_HEADER,
        timeout_s: float = 10.0,
        urlopen: Opener = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not client_id or not client_secret or not subscription_key:
            raise AuthError(
                "AbleEdge credentials incomplete — need client id, client secret and "
                "subscription key (smart_breaker_* in /etc/bluesignal/config.yaml)"
            )
        for url in (api_base, token_url):
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError(f"Refusing non-HTTP(S) AbleEdge URL: {url}")
        self._device_id = device_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._subscription_key = subscription_key
        self._api_base = api_base.rstrip("/")
        self._token_url = token_url
        self._subscription_header = subscription_header
        self._timeout_s = timeout_s
        self._urlopen = urlopen
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def device_id(self) -> str:
        return self._device_id

    # -- auth -----------------------------------------------------------------

    def authenticate(self) -> None:
        """Fetch a fresh bearer token (client-credentials grant)."""
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            self._token_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                self._subscription_header: self._subscription_key,
            },
        )
        status, payload = self._send(req)
        if status == 401 or status == 403:
            raise AuthError(f"AbleEdge token endpoint rejected credentials (HTTP {status})")
        if status != 200 or not isinstance(payload, dict):
            raise Unreachable(f"AbleEdge token endpoint returned HTTP {status}")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError("AbleEdge token response carried no access_token")
        ttl = payload.get("expires_in", _DEFAULT_TOKEN_TTL_S)
        ttl_s = float(ttl) if isinstance(ttl, int | float) else float(_DEFAULT_TOKEN_TTL_S)
        self._token = token
        self._token_expires_at = self._clock() + max(0.0, ttl_s - _TOKEN_SKEW_S)
        logger.info("AbleEdge token obtained (valid %ds)", int(ttl_s))

    def _ensure_token(self) -> str:
        if self._token is None or self._clock() >= self._token_expires_at:
            self.authenticate()
        assert self._token is not None
        return self._token

    def _forget_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    # -- HTTP -----------------------------------------------------------------

    def _send(self, req: urllib.request.Request) -> tuple[int, Any]:
        """One HTTP round-trip. Maps transport faults to Unreachable; returns
        (status, parsed JSON | None) for everything that got an HTTP answer."""
        try:
            # B310: scheme validated in __init__ (HTTP/S only) — vendor endpoint.
            with self._urlopen(req, timeout=self._timeout_s) as resp:  # nosec B310
                status = int(getattr(resp, "status", 200))
                raw = resp.read()
                if status == 204 or not raw:
                    return status, None
                return status, json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            raise Unreachable(f"AbleEdge {req.get_method()} {req.full_url} failed: {e}") from e

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        """Authenticated JSON call against ``api_base + path``. Retries once
        on 401 with a fresh token; every other non-2xx maps to a typed error."""
        if not self._device_id:
            raise NotBound("no smart_breaker_device_id configured")
        url = f"{self._api_base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (1, 2):
            token = self._ensure_token()
            headers = {
                self._subscription_header: self._subscription_key,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            status, payload = self._send(req)
            if status == 401 and attempt == 1:
                logger.info("AbleEdge 401 — refreshing token and retrying once")
                self._forget_token()
                continue
            return self._check(status, payload, method, path)
        raise AssertionError("unreachable")  # pragma: no cover

    def _check(self, status: int, payload: Any, method: str, path: str) -> Any:
        if 200 <= status < 300:
            return payload
        where = f"{method} {path}"
        if status in (401, 403):
            raise AuthError(f"AbleEdge {where} -> HTTP {status}")
        if status == 404:
            raise NotBound(f"AbleEdge device {self._device_id!r} not found ({where})")
        if status == 418:
            raise UnsupportedCommand(f"AbleEdge device does not support {where} (HTTP 418)")
        if status == 429:
            raise RateLimited(f"AbleEdge rate limit hit on {where}")
        if status == 503:
            raise DeviceUnavailable(f"AbleEdge reports breaker unavailable ({where})")
        if status >= 500:
            raise Unreachable(f"AbleEdge {where} -> HTTP {status}")
        raise SmartBreakerError(f"AbleEdge {where} -> HTTP {status}")

    @staticmethod
    def _data(payload: Any) -> dict[str, Any]:
        """Eaton wraps every body in ``{"data": …}``; tolerate a bare object."""
        if isinstance(payload, dict):
            inner = payload.get("data", payload)
            if isinstance(inner, dict):
                return inner
        return {}

    # -- SmartBreakerClient ---------------------------------------------------

    def get_status(self, probe_connected: bool = True) -> CircuitStatus:
        pos = self._data(
            self._request("GET", f"/devices/{self._device_id}/breaker/remoteHandle/position")
        )
        position = pos.get("position")
        position_str = str(position).lower() if position is not None else None
        is_on: bool | None
        if position_str in _POSITION_ON:
            is_on = True
        elif position_str in _POSITION_OFF:
            is_on = False
        else:
            is_on = None
            logger.warning("AbleEdge reported unknown handle position %r", position)

        connected: bool | None = None
        if probe_connected:
            # Best-effort: a failed link probe must not turn a good position
            # answer into an outage.
            try:
                meta = self._data(
                    self._request("GET", f"/devices/{self._device_id}/device/metadata/isConnected")
                )
                node = meta.get("isConnected", meta)
                val = node.get("val") if isinstance(node, dict) else node
                connected = bool(val) if isinstance(val, bool | int) else None
            except SmartBreakerError as e:
                logger.debug("AbleEdge isConnected probe failed: %s", e)
        return CircuitStatus(
            is_on=is_on,
            connected=connected,
            raw_position=position_str,
            observed_at=self._clock(),
        )

    def set_circuit(self, on: bool, reason: str) -> None:
        body = {"command": "close" if on else "open", "reason": (reason or "WQM-1")[:200]}
        self._request("POST", f"/devices/{self._device_id}/breaker/remoteHandle/position", body)
        logger.info(
            "AbleEdge circuit %s -> %s (%s)", self._device_id, "ON" if on else "OFF", body["reason"]
        )

    def get_power(self) -> PowerReading:
        data = self._data(
            self._request("GET", f"/devices/{self._device_id}/data/telemetry/meter/reading")
        )
        energy_node = data.get("energy")
        energy: dict[str, Any] = energy_node if isinstance(energy_node, dict) else {}
        ts = data.get("ts")
        return PowerReading(
            current_a=_val(data.get("currentA")),
            voltage_v=_val(data.get("voltageAN")),
            energy_delivered_wh=_num(energy.get("deliveredWH")),
            observed_at=float(ts) if isinstance(ts, int | float) else self._clock(),
            raw=data,
        )


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _val(node: Any) -> float | None:
    """Eaton metrology nodes are ``{"val": n}``; accept a bare number too."""
    if isinstance(node, dict):
        return _num(node.get("val"))
    return _num(node)
