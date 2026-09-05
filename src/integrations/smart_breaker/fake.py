"""
In-memory smart-breaker backend for tests and bench work without credentials.

Implements the same four calls as :class:`~.ableedge.AbleEdgeClient` against a
dict, with knobs to make the "vendor" misbehave on demand: go unreachable,
reject credentials, rate-limit, or report an unknown position. The controller
tests drive their whole fail-safe matrix through this.

It never touches the network and never pretends to be Eaton; a firmware build
with ``smart_breaker_vendor: ableedge`` will not construct it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from integrations.smart_breaker.base import (
    AuthError,
    CircuitStatus,
    NotBound,
    PowerReading,
    RateLimited,
    SmartBreakerError,
    Unreachable,
)


class FakeSmartBreaker:
    """A scriptable breaker. ``calls`` records every operation in order."""

    def __init__(
        self,
        device_id: str = "fake-device",
        is_on: bool = False,
        connected: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._device_id = device_id
        self.is_on = is_on
        self.connected = connected
        self.authenticated = False
        self.current_a: float | None = None
        self.voltage_v: float | None = None
        self.energy_delivered_wh: float | None = None
        self.calls: list[tuple[str, Any]] = []
        self._clock = clock

        # Failure injection. `unreachable` fails every call until cleared;
        # `fail_next` raises the given error on the very next call only.
        self.unreachable = False
        self.reject_auth = False
        self.rate_limited = False
        self._fail_next: SmartBreakerError | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    # -- scripting helpers ----------------------------------------------------

    def fail_next(self, error: SmartBreakerError) -> None:
        self._fail_next = error

    def _gate(self, op: str, arg: Any = None) -> None:
        self.calls.append((op, arg))
        if self._fail_next is not None:
            err, self._fail_next = self._fail_next, None
            raise err
        if not self._device_id:
            raise NotBound("fake: no device bound")
        if self.reject_auth:
            raise AuthError("fake: credentials rejected")
        if self.unreachable:
            raise Unreachable("fake: vendor unreachable")
        if self.rate_limited:
            raise RateLimited("fake: 429")

    # -- SmartBreakerClient ---------------------------------------------------

    def authenticate(self) -> None:
        self._gate("authenticate")
        self.authenticated = True

    def get_status(self) -> CircuitStatus:
        self._gate("get_status")
        return CircuitStatus(
            is_on=self.is_on,
            connected=self.connected,
            raw_position="close" if self.is_on else "open",
            observed_at=self._clock(),
        )

    def set_circuit(self, on: bool, reason: str) -> None:
        self._gate("set_circuit", (on, reason))
        self.is_on = on

    def get_power(self) -> PowerReading:
        self._gate("get_power")
        return PowerReading(
            current_a=self.current_a,
            voltage_v=self.voltage_v,
            energy_delivered_wh=self.energy_delivered_wh,
            observed_at=self._clock(),
            raw={"fake": True},
        )
