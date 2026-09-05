"""
Load-control front door: AbleEdge API, optional G5Q-14 interlock, fail-safe.

Relays are never presented as the AbleEdge integration. ``vendor=ableedge``
talks to Eaton; ``vendor=relay_only`` uses a configured fallback channel
explicitly; ``vendor=none`` refuses AWG/circuit commands. Sensor sampling is
not involved — cloud / service-window commands call ``set_circuit`` here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from integrations.ableedge.client import (
    AbleEdgeClient,
    CircuitCommandResult,
    CircuitStatus,
    HttpAbleEdgeClient,
    PowerReading,
)
from integrations.ableedge.errors import AbleEdgeError
from integrations.ableedge.mock import MockAbleEdgeClient
from integrations.ableedge.schema import LoadControlConfig, parse_load_control
from integrations.ableedge.secrets import AbleEdgeSecrets, resolve_secrets

logger = logging.getLogger("wqm1.ableedge")


def build_client(
    cfg: LoadControlConfig,
    secrets: AbleEdgeSecrets | None = None,
    *,
    environ: dict[str, str] | None = None,
    backend: str | None = None,
) -> AbleEdgeClient | None:
    """
    Construct the AbleEdge client, or None when this unit cannot call Eaton.

    ``backend=mock`` (config or ``ABLEEDGE_BACKEND=mock``) is for tests/bench.
    Live HTTP requires complete credential refs. Missing secrets are expected
    until jacques@bluesignal.xyz installs the developer-app values — that is
    the live-smoke blocker, not a firmware bug.
    """
    chosen = (backend or cfg.backend or "http").strip().lower()
    env = environ if environ is not None else os.environ
    if env.get("ABLEEDGE_BACKEND", "").strip().lower() == "mock":
        chosen = "mock"
    if chosen == "mock":
        return MockAbleEdgeClient(cfg)
    if secrets is None:
        secrets = resolve_secrets(cfg.credentials, environ=env)
    if not secrets.complete or not cfg.device_id:
        logger.info(
            "AbleEdge HTTP client not started (secrets_complete=%s device_id=%s) "
            "— live smoke blocked on Eaton app credentials",
            secrets.complete,
            bool(cfg.device_id),
        )
        return None
    return HttpAbleEdgeClient(cfg, secrets)


class LoadController:
    """Vendor router + fail-safe. Hooked from the existing command path."""

    def __init__(
        self,
        cfg: LoadControlConfig,
        client: AbleEdgeClient | None = None,
        relays: Any = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self._relays = relays
        self._last_applied: bool | None = None
        self._last_desired: bool | None = None
        self._reachable = client is not None
        self._fail_safe_latched = False

    @classmethod
    def from_settings(
        cls,
        raw: Any,
        relays: Any = None,
        *,
        environ: dict[str, str] | None = None,
        client_factory: Callable[[LoadControlConfig, AbleEdgeSecrets | None], AbleEdgeClient | None]
        | None = None,
    ) -> LoadController:
        cfg = parse_load_control(raw)
        factory = client_factory or (lambda c, s: build_client(c, s, environ=environ))
        client = None
        if cfg.vendor == "ableedge":
            secrets = resolve_secrets(cfg.credentials, environ=environ)
            client = factory(cfg, secrets)
        return cls(cfg, client=client, relays=relays)

    @property
    def vendor(self) -> str:
        return self.cfg.vendor

    @property
    def poll_s(self) -> int:
        return self.cfg.poll_s

    @property
    def last_applied(self) -> bool | None:
        return self._last_applied

    @property
    def reachable(self) -> bool:
        return self._reachable

    def set_circuit(self, state: bool, reason: str = "") -> CircuitCommandResult:
        """Cloud / service-window hook: turn the bound AWG circuit on or off."""
        wanted = bool(state)
        self._last_desired = wanted
        if self.cfg.vendor == "none":
            return CircuitCommandResult(
                ok=False,
                on=False,
                via="none",
                reachable=False,
                error="load_control.vendor=none — AWG circuit not bound",
            )
        if self.cfg.vendor == "relay_only":
            return self._via_fallback_relay(wanted, explicit=True)
        return self._via_ableedge(wanted, reason)

    def get_status(self) -> CircuitStatus:
        if self.cfg.vendor != "ableedge" or self.client is None:
            on = self._last_applied if self._last_applied is not None else False
            return CircuitStatus(
                device_id=self.cfg.device_id,
                circuit_id=self.cfg.bound_circuit_id,
                on=on,
                reachable=self.cfg.vendor == "relay_only",
                connected=None,
                position=None,
            )
        try:
            status = self.client.get_status()
            self._mark_reachable()
            self._last_applied = status.on
            return status
        except AbleEdgeError as e:
            logger.warning("AbleEdge get_status failed: %s", e)
            return self._apply_fail_safe(f"status: {e}")[1]

    def get_power(self) -> PowerReading:
        if self.cfg.vendor != "ableedge" or self.client is None:
            return PowerReading()
        try:
            reading = self.client.get_power()
            self._mark_reachable()
            return reading
        except AbleEdgeError as e:
            logger.warning("AbleEdge get_power failed: %s", e)
            self._apply_fail_safe(f"power: {e}")
            return PowerReading()

    def poll(self) -> CircuitStatus:
        """Periodic reachability / status refresh. Does not sample water."""
        return self.get_status()

    def shutdown(self) -> None:
        """Prefer fail-OFF for compressor/AWG loads when the process exits."""
        if self.cfg.vendor == "none":
            return
        if self.cfg.fail_safe == "on":
            # Honour the configured matrix even on shutdown.
            self._apply_fail_safe("shutdown", force=True)
            return
        # last and off both drop to OFF on process exit — a running compressor
        # after firmware death is the unsafe default.
        self._apply_local(False, via="fail_safe")
        logger.info("AbleEdge load control shutdown — local fail-OFF")

    def _via_ableedge(self, wanted: bool, reason: str) -> CircuitCommandResult:
        if self.client is None:
            result, _ = self._apply_fail_safe(
                "no AbleEdge client (credentials or device_id missing)"
            )
            return result
        try:
            result = self.client.set_circuit(wanted, reason=reason)
            self._mark_reachable()
            self._last_applied = result.on
            logger.info(
                "AbleEdge circuit %s -> %s (site=%s device=%s)",
                self.cfg.bound_circuit_id or "unbound",
                "on" if result.on else "off",
                self.cfg.site_id or "-",
                self.cfg.device_id or "-",
            )
            return result
        except AbleEdgeError as e:
            logger.warning("AbleEdge set_circuit failed: %s", e)
            result, _ = self._apply_fail_safe(str(e))
            return result

    def _via_fallback_relay(self, wanted: bool, *, explicit: bool) -> CircuitCommandResult:
        channel = self.cfg.fallback_relay
        if channel is None:
            return CircuitCommandResult(
                ok=False,
                on=False,
                via="relay_only",
                reachable=False,
                error="fallback_relay is not configured (1-4)",
            )
        if self._relays is None:
            return CircuitCommandResult(
                ok=False,
                on=False,
                via="relay_only",
                reachable=False,
                error="relays not initialised",
            )
        try:
            self._relays.set(channel, wanted)
        except Exception as e:  # noqa: BLE001 — command path must return an error
            return CircuitCommandResult(
                ok=False, on=False, via="relay_only", reachable=False, error=str(e)
            )
        self._last_applied = wanted
        via = "relay_only" if explicit else "fail_safe"
        logger.info(
            "Load-control %s via G5Q-14 CH%d -> %s (AbleEdge this is not)",
            via,
            channel,
            "on" if wanted else "off",
        )
        return CircuitCommandResult(ok=True, on=wanted, via=via, reachable=True)

    def _apply_fail_safe(
        self, why: str, *, force: bool = False
    ) -> tuple[CircuitCommandResult, CircuitStatus]:
        self._reachable = False
        policy = self.cfg.fail_safe
        if policy == "on":
            applied = True
        elif policy == "last":
            applied = self._last_applied if self._last_applied is not None else False
        else:
            applied = False
        if force or not self._fail_safe_latched:
            self._apply_local(applied, via="fail_safe")
            self._fail_safe_latched = True
            logger.warning(
                "AbleEdge unreachable (%s) — fail_safe=%s applied (circuit %s)",
                why,
                policy,
                "on" if applied else "off",
            )
        status = CircuitStatus(
            device_id=self.cfg.device_id,
            circuit_id=self.cfg.bound_circuit_id,
            on=applied,
            reachable=False,
            connected=False,
            position=None,
        )
        result = CircuitCommandResult(
            ok=False,
            on=applied,
            via="fail_safe",
            reachable=False,
            error=why,
            fail_safe_applied=policy,
        )
        return result, status

    def _apply_local(self, on: bool, *, via: str) -> None:
        self._last_applied = on
        # Interlock only: drop/raise the designated G5Q-14 when fail-safe
        # fires. This is not the AbleEdge control path.
        if self.cfg.fallback_relay and self._relays is not None and via == "fail_safe":
            try:
                self._relays.set(self.cfg.fallback_relay, on)
            except Exception as e:  # noqa: BLE001
                logger.warning("Fallback relay interlock failed: %s", e)

    def _mark_reachable(self) -> None:
        if not self._reachable:
            logger.info("AbleEdge API reachable again — fail-safe latch cleared")
        self._reachable = True
        self._fail_safe_latched = False
