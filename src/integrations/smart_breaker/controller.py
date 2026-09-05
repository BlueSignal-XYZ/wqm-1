"""
AWG circuit controller — the one place that decides what happens to the load.

Sits between the firmware's command paths (Service Window socket, cloud
command queue) and a :class:`~.base.SmartBreakerClient`, and owns three
things the client deliberately does not:

1. **The interlock relay.** When the installer wires a G5Q-14 channel in
   series with the AWG's enable input / contactor coil, this controller
   drops it BEFORE asking the breaker to open and energises it only AFTER
   the breaker confirms it closed. The relay is the local, instant,
   no-network half of every OFF; the breaker is the remote half.

2. **Fail-safe.** Every vendor call is a link-health sample. Once the vendor
   has been unreachable for ``smart_breaker_unreachable_grace_s`` the
   configured :class:`~.base.FailSafeMode` is applied exactly once — and for
   ``off`` (the shipped default for compressor loads) that means the
   interlock relay drops now and an "open" is queued for the moment the link
   returns. De-energising is always allowed; energising is never retried
   behind the operator's back (an ON that failed stays failed until someone
   asks again).

3. **``relay_only`` mode.** A site with no smart breaker still gets the same
   ``awg_set`` command surface: the AWG enable is simply the relay channel.

It never derives litres/day, amps, or nameplate data. ``circuit_amps`` is
carried through from the installer's entry for display only.

Thread-safety: ``request()`` arrives on the command-socket / cloud-command
threads, ``poll()`` on the worker thread; one lock serialises them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from integrations.smart_breaker.base import (
    AuthError,
    CircuitStatus,
    FailSafeMode,
    PowerReading,
    SmartBreakerClient,
    SmartBreakerError,
)

logger = logging.getLogger("wqm1.smart_breaker")

EventSink = Callable[[dict[str, Any]], Any]


class SmartBreakerController:
    """Binds settings + vendor client + optional interlock relay for ONE circuit."""

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        client: SmartBreakerClient | None,
        relays: Any = None,
        clock: Callable[[], float] = time.monotonic,
        event_sink: EventSink | None = None,
    ) -> None:
        self._settings = settings_provider
        self._client = client
        self._relays = relays
        self._clock = clock
        self._emit = event_sink
        self._lock = threading.Lock()

        self._desired: bool | None = None
        self._last_status: CircuitStatus | None = None
        self._last_power: PowerReading | None = None
        self._last_ok_mono: float | None = None
        # Link considered "down since boot" until the first successful call:
        # a unit that never reaches the vendor must still fail safe.
        self._unreachable_since: float | None = self._clock()
        self._last_error: str | None = None
        self._fail_safe_applied: FailSafeMode | None = None
        # (on, reason) to send when the link returns. Only OFF is ever queued.
        self._pending: tuple[bool, str] | None = None

        s = self._settings()
        if self.vendor == "ableedge" and not int(getattr(s, "smart_breaker_circuit_amps", 0)):
            logger.warning(
                "Smart breaker bound (%s) but smart_breaker_circuit_amps is 0 — the installer "
                "has not entered the circuit ampacity from the panel label",
                getattr(s, "smart_breaker_device_id", ""),
            )

    # -- settings views -------------------------------------------------------

    @property
    def vendor(self) -> str:
        return str(getattr(self._settings(), "smart_breaker_vendor", "none"))

    @property
    def enabled(self) -> bool:
        return self.vendor != "none"

    @property
    def fail_safe_mode(self) -> FailSafeMode:
        return FailSafeMode.parse(str(getattr(self._settings(), "smart_breaker_fail_safe", "off")))

    @property
    def _grace_s(self) -> float:
        return float(getattr(self._settings(), "smart_breaker_unreachable_grace_s", 300))

    @property
    def interlock_relay(self) -> int | None:
        ch = int(getattr(self._settings(), "smart_breaker_interlock_relay", 0) or 0)
        return ch if 1 <= ch <= 4 else None

    @property
    def link_ok(self) -> bool:
        return self._unreachable_since is None

    # -- interlock relay ------------------------------------------------------

    def _set_interlock(self, on: bool, why: str) -> bool:
        """Drive the series relay. True when the coil is known to be in the
        requested state (including "there is no interlock relay to drive")."""
        ch = self.interlock_relay
        if ch is None or self._relays is None:
            return True
        try:
            self._relays.set(ch, on)
        except Exception as e:  # noqa: BLE001 — a relay fault must be reported, not raised
            logger.error("Interlock relay %d -> %s failed (%s): %s", ch, on, why, e)
            return False
        logger.info("Interlock relay %d -> %s (%s)", ch, "ON" if on else "OFF", why)
        return True

    # -- link health ----------------------------------------------------------

    def _mark_ok(self) -> None:
        now = self._clock()
        self._last_ok_mono = now
        self._last_error = None
        if self._unreachable_since is not None:
            was_down_s = now - self._unreachable_since
            self._unreachable_since = None
            if self._fail_safe_applied is not None:
                logger.warning(
                    "Smart breaker link restored after %.0fs; fail-safe '%s' had been applied",
                    was_down_s,
                    self._fail_safe_applied.value,
                )
                self._event(
                    "smart_breaker_restored",
                    f"Breaker API link restored after {int(was_down_s)}s",
                    {"failSafe": self._fail_safe_applied.value, "downSeconds": int(was_down_s)},
                )
                self._fail_safe_applied = None
            else:
                logger.info("Smart breaker link up (was down %.0fs)", was_down_s)

    def _mark_fail(self, error: Exception) -> None:
        self._last_error = f"{type(error).__name__}: {error}"
        if self._unreachable_since is None:
            self._unreachable_since = self._clock()
        log = logger.error if isinstance(error, AuthError) else logger.warning
        log("Smart breaker call failed: %s", self._last_error)
        self._evaluate_fail_safe()

    def _evaluate_fail_safe(self) -> None:
        if self._unreachable_since is None or self._fail_safe_applied is not None:
            return
        down_s = self._clock() - self._unreachable_since
        if down_s < self._grace_s:
            return
        mode = self.fail_safe_mode
        self._fail_safe_applied = mode
        details: dict[str, Any] = {
            "mode": mode.value,
            "downSeconds": int(down_s),
            "lastError": self._last_error,
        }
        if mode is FailSafeMode.OFF:
            details["interlockDropped"] = self._set_interlock(False, "fail-safe OFF")
            self._desired = False
            self._pending = (False, "WQM-1 fail-safe: breaker API unreachable")
            logger.error(
                "Smart breaker unreachable for %.0fs — FAIL-SAFE OFF applied "
                "(interlock dropped, breaker open queued)",
                down_s,
            )
        elif mode is FailSafeMode.ON:
            details["interlockEnergised"] = self._set_interlock(True, "fail-safe ON")
            logger.error(
                "Smart breaker unreachable for %.0fs — FAIL-SAFE ON applied (interlock energised; "
                "breaker position cannot be changed while unreachable)",
                down_s,
            )
        else:
            logger.error(
                "Smart breaker unreachable for %.0fs — fail-safe LAST: load left as-is", down_s
            )
        self._event(
            "smart_breaker_failsafe", f"Breaker API unreachable; fail-safe {mode.value}", details
        )

    def _event(self, type_: str, message: str, details: dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            self._emit({"type": type_, "message": message, "details": details})
        except Exception as e:  # noqa: BLE001 — telemetry must never break control
            logger.debug("event sink failed: %s", e)

    # -- command path ---------------------------------------------------------

    def request(self, on: bool, source: str, reason: str | None = None) -> dict[str, Any]:
        """Energise / de-energise the AWG circuit on behalf of ``source``.

        Returns a JSON-able result. ``ok`` is True only when the load is in the
        requested state as far as we can tell; ``breaker`` says whether the
        vendor confirmed ("confirmed"), was not asked ("n/a" in relay_only),
        or failed ("unconfirmed" — for OFF the interlock is still dropped and
        the open is queued). ``interlockOk`` reports whether the series relay
        reached the requested state (always True when no relay is bound).
        """
        why = reason or f"WQM-1 {source}"
        with self._lock:
            vendor = self.vendor
            if vendor == "none":
                return {"ok": False, "error": "smart breaker integration disabled"}
            result: dict[str, Any] = {
                "state": on,
                "source": source,
                "vendor": vendor,
                "interlockRelay": self.interlock_relay,
            }
            if vendor == "relay_only":
                if self.interlock_relay is None:
                    return {**result, "ok": False, "error": "relay_only needs interlock relay 1-4"}
                ok = self._set_interlock(on, why)
                self._desired = on
                return {**result, "ok": ok, "breaker": "n/a"}

            if self._client is None:
                return {**result, "ok": False, "error": "smart breaker client not initialised"}

            if not on:
                # Local half first — it needs no network and cannot be rate-limited.
                interlock_ok = self._set_interlock(False, why)
                self._desired = False
                try:
                    self._client.set_circuit(False, why)
                    self._mark_ok()
                    self._pending = None
                    return {
                        **result,
                        "ok": True,
                        "breaker": "confirmed",
                        "interlockOk": interlock_ok,
                    }
                except SmartBreakerError as e:
                    self._mark_fail(e)
                    self._pending = (False, why)
                    return {
                        **result,
                        "ok": False,
                        "breaker": "unconfirmed",
                        "interlockOk": interlock_ok,
                        "error": self._last_error,
                    }

            # ON: remote half first; the interlock only follows a confirmed close.
            try:
                self._client.set_circuit(True, why)
                self._mark_ok()
            except SmartBreakerError as e:
                self._mark_fail(e)
                return {**result, "ok": False, "breaker": "unconfirmed", "error": self._last_error}
            self._desired = True
            self._pending = None
            interlock_ok = self._set_interlock(True, why)
            return {
                **result,
                "ok": interlock_ok,
                "breaker": "confirmed",
                "interlockOk": interlock_ok,
            }

    # -- periodic path --------------------------------------------------------

    def poll(self) -> dict[str, Any]:
        """One supervision cycle: flush a queued OFF, sample status + power,
        update link health, apply fail-safe if due. Vendor errors are handled
        here; anything else propagates to the worker runner."""
        with self._lock:
            if self.vendor != "ableedge" or self._client is None:
                return self.status()
            if self._pending is not None:
                on, why = self._pending
                try:
                    self._client.set_circuit(on, why)
                    self._pending = None
                    self._mark_ok()
                    logger.info("Queued breaker %s delivered (%s)", "close" if on else "open", why)
                except SmartBreakerError as e:
                    self._mark_fail(e)
                    return self.status()
            try:
                self._last_status = self._client.get_status()
                self._mark_ok()
            except SmartBreakerError as e:
                self._mark_fail(e)
                return self.status()
            try:
                self._last_power = self._client.get_power()
            except SmartBreakerError as e:
                # Metering is telemetry, not control: log, keep the last good sample.
                logger.debug("Smart breaker power read failed: %s", e)
            if (
                self._desired is not None
                and self._last_status.is_on is not None
                and self._last_status.is_on != self._desired
            ):
                logger.warning(
                    "Breaker reports %s but WQM-1 last asked for %s — someone else "
                    "(app, panel handle, override event) moved it",
                    "ON" if self._last_status.is_on else "OFF",
                    "ON" if self._desired else "OFF",
                )
            return self.status()

    # -- snapshot -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self._settings()
        st, pw = self._last_status, self._last_power
        now = self._clock()
        return {
            "vendor": self.vendor,
            "deviceId": getattr(s, "smart_breaker_device_id", ""),
            "siteId": getattr(s, "smart_breaker_site_id", ""),
            "circuitLabel": getattr(s, "smart_breaker_circuit_label", ""),
            "circuitAmps": int(getattr(s, "smart_breaker_circuit_amps", 0) or 0) or None,
            "interlockRelay": self.interlock_relay,
            "failSafe": self.fail_safe_mode.value,
            "failSafeApplied": self._fail_safe_applied.value if self._fail_safe_applied else None,
            "linkOk": self.link_ok,
            "unreachableForS": (
                int(now - self._unreachable_since) if self._unreachable_since is not None else 0
            ),
            "lastError": self._last_error,
            "desired": self._desired,
            "pendingCommand": ("close" if self._pending[0] else "open") if self._pending else None,
            "breaker": (
                {
                    "isOn": st.is_on,
                    "connected": st.connected,
                    "position": st.raw_position,
                    "observedAt": st.observed_at,
                }
                if st
                else None
            ),
            "power": (
                {
                    "currentA": pw.current_a,
                    "voltageV": pw.voltage_v,
                    "energyDeliveredWh": pw.energy_delivered_wh,
                    "observedAt": pw.observed_at,
                }
                if pw
                else None
            ),
        }

    def shutdown(self) -> None:
        """Nothing to send: main() drops every relay coil on the way down, and
        a service restart must not open the customer's breaker."""
        logger.info("Smart breaker controller stopped (desired=%s)", self._desired)
