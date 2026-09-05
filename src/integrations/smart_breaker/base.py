"""
Smart-breaker client contract.

A *smart breaker client* answers four questions about ONE circuit — the
customer's AWG (atmospheric water generator) or similar compressor load — on
a breaker the customer already owns:

* ``authenticate()``   — can we talk to the vendor at all?
* ``get_status()``     — is the circuit energised right now?
* ``set_circuit(on)``  — please energise / de-energise it.
* ``get_power()``      — what is it drawing, as the breaker meters it?

Every vendor backend (:mod:`.ableedge`) and the test double (:mod:`.fake`)
implement this same surface, so the controller and the firmware wiring never
know which one they hold. Vocabulary is deliberately the load's, not the
breaker's: a breaker "closes" to energise and "opens" to de-energise, and
that inversion is confined to the vendor module.

Nothing in here derives litres/day, nameplate amps, or any other figure the
API does not report. ``PowerReading`` carries what the breaker measured and
nothing more.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class SmartBreakerError(Exception):
    """Base class for everything a backend can raise."""


class AuthError(SmartBreakerError):
    """Credentials rejected or missing — retrying without change is pointless."""


class Unreachable(SmartBreakerError):
    """Transport-level failure: DNS, TCP, TLS, timeout, 5xx. Retry later."""


class RateLimited(Unreachable):
    """HTTP 429 from the vendor. Treated as unreachable, but named so the log
    says *why* the poll cadence needs slowing down."""


class DeviceUnavailable(Unreachable):
    """The vendor cloud is up but the breaker itself is not (HTTP 503)."""


class UnsupportedCommand(SmartBreakerError):
    """The bound device cannot perform the request (HTTP 418 — e.g. an EV
    charger bound where a standard breaker was expected)."""


class NotBound(SmartBreakerError):
    """No device id configured — the installer has not bound a circuit yet."""


class FailSafeMode(enum.Enum):
    """What to do with the load once the breaker API has been unreachable for
    longer than the configured grace period."""

    OFF = "off"  # de-energise (shipped default for compressors)
    LAST = "last"  # leave it where it is
    ON = "on"  # energise (only for loads where running is the safe state)

    @classmethod
    def parse(cls, value: str) -> FailSafeMode:
        try:
            return cls(value)
        except ValueError as e:
            raise ValueError(
                f"fail_safe must be one of {[m.value for m in cls]}, got {value!r}"
            ) from e


@dataclass(frozen=True)
class CircuitStatus:
    """Point-in-time answer to "is the AWG circuit energised?".

    ``is_on`` is None when the vendor reported a position we do not understand
    — that is surfaced, never coerced to a guess.
    """

    is_on: bool | None
    connected: bool | None = None  # breaker ↔ vendor cloud link, if reported
    raw_position: str | None = None  # vendor's own word, for the log
    observed_at: float | None = None  # epoch seconds (time.time())


@dataclass(frozen=True)
class PowerReading:
    """What the breaker's meter reported. All fields optional: a backend
    fills what its API returns and leaves the rest None.

    Units are AS REPORTED by the vendor. The AbleEdge ``/meter/reading``
    sample shows amps and volts while ``/deviceData`` shows milli-units, and
    which one the live endpoint actually returns is on the live-smoke list
    (docs/smart-breaker-integration.md). ``raw`` keeps the untouched payload
    so nothing is lost while that is settled.
    """

    current_a: float | None = None
    voltage_v: float | None = None
    energy_delivered_wh: float | None = None
    observed_at: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SmartBreakerClient(Protocol):
    """The four calls every backend provides. Circuit identity is fixed at
    construction — one client, one bound circuit."""

    @property
    def device_id(self) -> str: ...

    def authenticate(self) -> None:
        """Obtain / refresh whatever the vendor needs. Raises AuthError."""

    def get_status(self) -> CircuitStatus: ...

    def set_circuit(self, on: bool, reason: str) -> None:
        """Energise (True) or de-energise (False). ``reason`` is recorded by
        the vendor — say who asked and why."""

    def get_power(self) -> PowerReading: ...
