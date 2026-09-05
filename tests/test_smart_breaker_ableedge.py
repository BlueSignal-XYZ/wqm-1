"""
Eaton AbleEdge client — exercised against a scripted urlopen, never the wire.

Pins down the things live smoke will otherwise find the hard way: header
names, the client-credentials token exchange, token caching + refresh, the
load↔breaker vocabulary inversion (on → "close"), and the HTTP-status → typed
error mapping the controller's fail-safe logic depends on.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.parse
from typing import Any

import pytest

from integrations.smart_breaker.ableedge import (
    DEFAULT_SUBSCRIPTION_HEADER,
    AbleEdgeClient,
)
from integrations.smart_breaker.base import (
    AuthError,
    DeviceUnavailable,
    NotBound,
    RateLimited,
    SmartBreakerError,
    Unreachable,
    UnsupportedCommand,
)

DEVICE = "f4628c73-0c62-491a-9454-a4f1b08e98ef"


class _Resp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status = status
        self._raw = b"" if body is None else json.dumps(body).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class ScriptedHTTP:
    """Routes (method, url) to a canned response; records every request."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.routes: dict[tuple[str, str], list[Any]] = {}
        self.token_responses: list[Any] = [
            _Resp(200, {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600})
        ]

    def route(self, method: str, url: str, *responses: Any) -> None:
        self.routes.setdefault((method, url), []).extend(responses)

    def __call__(self, req: Any, timeout: float = 0) -> Any:
        self.requests.append(req)
        if req.full_url.endswith("/oauth2/token"):
            queue = self.token_responses
        else:
            queue = self.routes.get((req.get_method(), req.full_url))
            if queue is None:
                raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):  # bare status = HTTPError
            raise urllib.error.HTTPError(req.full_url, item, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return item


@pytest.fixture
def http() -> ScriptedHTTP:
    return ScriptedHTTP()


@pytest.fixture
def clock():
    t = {"now": 1_000_000.0}

    def _now() -> float:
        return t["now"]

    _now.advance = lambda s: t.__setitem__("now", t["now"] + s)  # type: ignore[attr-defined]
    return _now


@pytest.fixture
def client(http: ScriptedHTTP, clock) -> AbleEdgeClient:
    return AbleEdgeClient(
        device_id=DEVICE,
        client_id="cid",
        client_secret="sekrit",
        subscription_key="subkey",
        api_base="https://api.example.test/api/v1",
        token_url="https://api.example.test/oauth2/token",
        urlopen=http,
        clock=clock,
    )


BASE = "https://api.example.test/api/v1"
POSITION = f"{BASE}/devices/{DEVICE}/breaker/remoteHandle/position"
CONNECTED = f"{BASE}/devices/{DEVICE}/device/metadata/isConnected"
READING = f"{BASE}/devices/{DEVICE}/data/telemetry/meter/reading"


class TestConstruction:
    def test_refuses_incomplete_credentials(self):
        with pytest.raises(AuthError):
            AbleEdgeClient(DEVICE, "cid", "", "subkey")

    def test_refuses_non_http_urls(self):
        with pytest.raises(ValueError):
            AbleEdgeClient(DEVICE, "cid", "s", "k", api_base="file:///etc/passwd")

    def test_device_id_property(self, client):
        assert client.device_id == DEVICE


class TestAuthenticate:
    def test_client_credentials_grant_with_basic_auth(self, client, http):
        client.authenticate()
        req = http.requests[0]
        assert req.get_method() == "POST"
        assert req.full_url == "https://api.example.test/oauth2/token"
        expected = base64.b64encode(b"cid:sekrit").decode()
        assert req.get_header("Authorization") == f"Basic {expected}"
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert urllib.parse.parse_qs(req.data.decode()) == {"grant_type": ["client_credentials"]}
        # The subscription key rides along on the token call too.
        assert req.get_header(DEFAULT_SUBSCRIPTION_HEADER.capitalize()) == "subkey"

    def test_rejected_credentials_are_an_auth_error(self, client, http):
        http.token_responses = [401]
        with pytest.raises(AuthError):
            client.authenticate()

    def test_token_endpoint_outage_is_unreachable(self, client, http):
        http.token_responses = [urllib.error.URLError("dns")]
        with pytest.raises(Unreachable):
            client.authenticate()

    def test_missing_access_token_is_an_auth_error(self, client, http):
        http.token_responses = [_Resp(200, {"token_type": "Bearer"})]
        with pytest.raises(AuthError):
            client.authenticate()


class TestTokenLifecycle:
    def test_token_is_cached_across_calls(self, client, http):
        http.route("GET", POSITION, _Resp(200, {"data": {"position": "open"}}))
        client.get_status(probe_connected=False)
        client.get_status(probe_connected=False)
        token_calls = [r for r in http.requests if r.full_url.endswith("/oauth2/token")]
        assert len(token_calls) == 1

    def test_token_refreshes_before_expiry(self, client, http, clock):
        http.route("GET", POSITION, _Resp(200, {"data": {"position": "open"}}))
        client.get_status(probe_connected=False)
        clock.advance(3600 - 30)  # inside the 60 s skew window
        client.get_status(probe_connected=False)
        token_calls = [r for r in http.requests if r.full_url.endswith("/oauth2/token")]
        assert len(token_calls) == 2

    def test_401_on_api_call_refreshes_once_and_retries(self, client, http):
        http.token_responses = [
            _Resp(200, {"access_token": "stale", "expires_in": 3600}),
            _Resp(200, {"access_token": "fresh", "expires_in": 3600}),
        ]
        http.route("GET", POSITION, 401, _Resp(200, {"data": {"position": "close"}}))
        status = client.get_status(probe_connected=False)
        assert status.is_on is True
        api_calls = [r for r in http.requests if r.full_url == POSITION]
        assert [r.get_header("Authorization") for r in api_calls] == [
            "Bearer stale",
            "Bearer fresh",
        ]

    def test_persistent_401_is_an_auth_error(self, client, http):
        http.route("GET", POSITION, 401)
        with pytest.raises(AuthError):
            client.get_status(probe_connected=False)


class TestHeaders:
    def test_api_calls_carry_subscription_key_and_bearer(self, client, http):
        http.route("GET", POSITION, _Resp(200, {"data": {"position": "open"}}))
        client.get_status(probe_connected=False)
        req = http.requests[-1]
        assert req.get_header(DEFAULT_SUBSCRIPTION_HEADER.capitalize()) == "subkey"
        assert req.get_header("Authorization") == "Bearer tok-1"
        assert req.get_header("Accept") == "application/json"

    def test_subscription_header_name_is_configurable(self, http, clock):
        """Eaton's newer AbleEdge portal names the header `api-key`."""
        c = AbleEdgeClient(
            DEVICE,
            "cid",
            "s",
            "k",
            api_base=BASE,
            token_url="https://api.example.test/oauth2/token",
            subscription_header="api-key",
            urlopen=http,
            clock=clock,
        )
        http.route("GET", POSITION, _Resp(200, {"data": {"position": "open"}}))
        c.get_status(probe_connected=False)
        assert http.requests[-1].get_header("Api-key") == "k"


class TestGetStatus:
    @pytest.mark.parametrize(
        "position, expected",
        [("close", True), ("closed", True), ("open", False), ("OPEN", False), ("weird", None)],
    )
    def test_position_vocabulary(self, client, http, position, expected):
        http.route("GET", POSITION, _Resp(200, {"data": {"position": position}}))
        st = client.get_status(probe_connected=False)
        assert st.is_on is expected
        assert st.raw_position == position.lower()
        assert st.connected is None
        assert st.observed_at == 1_000_000.0

    def test_connected_probe_is_read_when_available(self, client, http):
        http.route("GET", POSITION, _Resp(200, {"data": {"position": "close"}}))
        http.route("GET", CONNECTED, _Resp(200, {"data": {"isConnected": {"val": True}}}))
        assert client.get_status().connected is True

    def test_connected_probe_failure_does_not_fail_status(self, client, http):
        http.route("GET", POSITION, _Resp(200, {"data": {"position": "close"}}))
        http.route("GET", CONNECTED, 500)
        st = client.get_status()
        assert st.is_on is True
        assert st.connected is None

    def test_unbound_device_id_refuses_to_call(self, http, clock):
        c = AbleEdgeClient("", "cid", "s", "k", api_base=BASE, urlopen=http, clock=clock)
        with pytest.raises(NotBound):
            c.get_status()
        assert http.requests == []


class TestSetCircuit:
    def test_on_means_close(self, client, http):
        http.route("POST", POSITION, _Resp(204))
        client.set_circuit(True, "installer bench test")
        req = http.requests[-1]
        assert json.loads(req.data) == {"command": "close", "reason": "installer bench test"}
        assert req.get_header("Content-type") == "application/json"

    def test_off_means_open(self, client, http):
        http.route("POST", POSITION, _Resp(204))
        client.set_circuit(False, "cloud")
        assert json.loads(http.requests[-1].data)["command"] == "open"

    def test_reason_is_bounded_and_defaulted(self, client, http):
        http.route("POST", POSITION, _Resp(204))
        client.set_circuit(False, "x" * 500)
        assert len(json.loads(http.requests[-1].data)["reason"]) == 200
        client.set_circuit(False, "")
        assert json.loads(http.requests[-1].data)["reason"] == "WQM-1"

    @pytest.mark.parametrize(
        "status, exc",
        [
            (403, AuthError),
            (404, NotBound),
            (418, UnsupportedCommand),
            (429, RateLimited),
            (503, DeviceUnavailable),
            (500, Unreachable),
            (400, SmartBreakerError),
        ],
    )
    def test_http_status_maps_to_typed_error(self, client, http, status, exc):
        http.route("POST", POSITION, status)
        with pytest.raises(exc):
            client.set_circuit(False, "test")

    def test_transport_failure_is_unreachable(self, client, http):
        http.route("POST", POSITION, urllib.error.URLError("timed out"))
        with pytest.raises(Unreachable):
            client.set_circuit(False, "test")

    def test_socket_timeout_is_unreachable(self, client, http):
        http.route("POST", POSITION, TimeoutError())
        with pytest.raises(Unreachable):
            client.set_circuit(False, "test")


class TestGetPower:
    def test_parses_metrology_nodes(self, client, http):
        http.route(
            "GET",
            READING,
            _Resp(
                200,
                {
                    "data": {
                        "currentA": {"val": 12.5},
                        "voltageAN": {"val": 121},
                        "energy": {"deliveredWH": 4321.0, "generatedWH": 0},
                        "ts": 1700000000,
                    }
                },
            ),
        )
        pw = client.get_power()
        assert pw.current_a == 12.5
        assert pw.voltage_v == 121.0
        assert pw.energy_delivered_wh == 4321.0
        assert pw.observed_at == 1700000000.0
        assert pw.raw["currentA"] == {"val": 12.5}

    def test_missing_fields_stay_none_not_zero(self, client, http):
        """No metering data must never be reported as 'drawing 0 A'."""
        http.route("GET", READING, _Resp(200, {"data": {}}))
        pw = client.get_power()
        assert pw.current_a is None
        assert pw.voltage_v is None
        assert pw.energy_delivered_wh is None
        assert pw.observed_at == 1_000_000.0

    def test_bare_numbers_accepted(self, client, http):
        http.route("GET", READING, _Resp(200, {"data": {"currentA": 3, "voltageAN": True}}))
        pw = client.get_power()
        assert pw.current_a == 3.0
        assert pw.voltage_v is None  # booleans are not measurements

    def test_non_object_payload_yields_empty_reading(self, client, http):
        http.route("GET", READING, _Resp(200, [1, 2, 3]))
        assert client.get_power().raw == {}
