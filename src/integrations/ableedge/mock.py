"""
In-process AbleEdge backend for unit tests and bench (no Eaton credentials).

The mock speaks the same client interface as HttpAbleEdgeClient. Tests flip
``unreachable`` / ``auth_ok`` to exercise fail-safe without touching the
network. Power values are whatever the test last set — never invented from
nameplate or ampacity.
"""

from __future__ import annotations

from integrations.ableedge.client import (
    CircuitCommandResult,
    CircuitStatus,
    PowerReading,
)
from integrations.ableedge.errors import AbleEdgeAuthError, AbleEdgeUnreachableError
from integrations.ableedge.schema import LoadControlConfig


class MockAbleEdgeClient:
    """Fake Eaton backend. Safe to construct with empty secrets."""

    def __init__(
        self,
        config: LoadControlConfig | None = None,
        *,
        device_id: str = "mock-device",
        circuit_id: str = "",
    ) -> None:
        self._cfg = config
        self.device_id = config.device_id if config and config.device_id else device_id
        self.circuit_id = (
            config.bound_circuit_id
            if config and config.bound_circuit_id
            else (circuit_id or self.device_id)
        )
        self.on = False
        self.connected = True
        self.authenticated = False
        self.auth_ok = True
        self.unreachable = False
        self.power = PowerReading()
        self.auth_calls = 0
        self.set_calls: list[bool] = []
        self.status_calls = 0
        self.power_calls = 0

    def authenticate(self) -> bool:
        self.auth_calls += 1
        if self.unreachable:
            raise AbleEdgeUnreachableError("mock unreachable")
        if not self.auth_ok:
            self.authenticated = False
            raise AbleEdgeAuthError("mock auth rejected")
        self.authenticated = True
        return True

    def get_status(self) -> CircuitStatus:
        self.status_calls += 1
        self._require_reachable()
        return CircuitStatus(
            device_id=self.device_id,
            circuit_id=self.circuit_id,
            on=self.on,
            reachable=True,
            connected=self.connected,
            position="close" if self.on else "open",
        )

    def set_circuit(self, state: bool, reason: str = "") -> CircuitCommandResult:
        self.set_calls.append(state)
        self._require_reachable()
        self.on = bool(state)
        return CircuitCommandResult(ok=True, on=self.on, via="ableedge", reachable=True)

    def get_power(self) -> PowerReading:
        self.power_calls += 1
        self._require_reachable()
        return self.power

    def _require_reachable(self) -> None:
        if self.unreachable:
            raise AbleEdgeUnreachableError("mock unreachable")
        if not self.authenticated and not self.auth_ok:
            raise AbleEdgeAuthError("mock not authenticated")
        if not self.authenticated:
            self.authenticate()
