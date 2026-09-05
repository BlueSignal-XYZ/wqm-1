"""
Smart-breaker / AWG load control.

WQM-1 talks TO a residential smart breaker the customer already owns so the
cloud (or the installer at the Service Window) can ask for "AWG circuit on /
off". It is not a breaker, does no panel work, and the G5Q-14 relays stay in
place as the local interlock and fallback path.

Vendor support: Eaton AbleEdge (Smart Breaker API). Everything else is
documentation only — see docs/smart-breaker-integration.md.

``build_smart_breaker()`` is the single entry point main.py uses: it reads the
``smart_breaker_*`` settings and returns a controller, or None when the
feature is off or cannot be configured safely (never a half-built one).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from integrations.smart_breaker.base import (
    AuthError,
    CircuitStatus,
    DeviceUnavailable,
    FailSafeMode,
    NotBound,
    PowerReading,
    RateLimited,
    SmartBreakerClient,
    SmartBreakerError,
    Unreachable,
    UnsupportedCommand,
)
from integrations.smart_breaker.controller import SmartBreakerController
from integrations.smart_breaker.worker import SmartBreakerWorker

__all__ = [
    "AuthError",
    "CircuitStatus",
    "DeviceUnavailable",
    "FailSafeMode",
    "NotBound",
    "PowerReading",
    "RateLimited",
    "SmartBreakerClient",
    "SmartBreakerController",
    "SmartBreakerError",
    "SmartBreakerWorker",
    "Unreachable",
    "UnsupportedCommand",
    "build_smart_breaker",
]

logger = logging.getLogger("wqm1.smart_breaker")


def build_smart_breaker(
    settings_provider: Callable[[], Any],
    relays: Any = None,
    event_sink: Callable[[dict[str, Any]], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SmartBreakerController | None:
    """Construct the controller for the configured vendor, or None.

    Refuses (logs, returns None) rather than half-configuring: missing
    credentials, an unbound device id, a relay_only site without a relay
    channel, or an auth mode that is not implemented yet all mean "feature
    off" for this boot — the monitor keeps running either way.
    """
    s = settings_provider()
    vendor = str(getattr(s, "smart_breaker_vendor", "none"))
    if vendor == "none":
        return None

    if vendor == "relay_only":
        ch = int(getattr(s, "smart_breaker_interlock_relay", 0) or 0)
        if not 1 <= ch <= 4:
            logger.error(
                "smart_breaker_vendor=relay_only needs smart_breaker_interlock_relay 1-4 "
                "(got %s) — AWG control disabled",
                ch,
            )
            return None
        if relays is None:
            logger.error("relay_only AWG control needs relay hardware — disabled on this board")
            return None
        logger.info("AWG control: relay_only on channel %d", ch)
        return SmartBreakerController(settings_provider, None, relays, clock, event_sink)

    if vendor == "ableedge":
        device_id = str(getattr(s, "smart_breaker_device_id", "") or "")
        if not device_id:
            logger.error("smart_breaker_vendor=ableedge but no smart_breaker_device_id — disabled")
            return None
        auth_mode = str(getattr(s, "smart_breaker_auth_mode", "direct"))
        if auth_mode != "direct":
            # The cloud-proxy transport is specified in docs/smart-breaker-
            # integration.md and waits on the Cloud Functions side.
            logger.error(
                "smart_breaker_auth_mode=%s is not available in this firmware — disabled",
                auth_mode,
            )
            return None
        from integrations.smart_breaker.ableedge import AbleEdgeClient

        try:
            client: SmartBreakerClient = AbleEdgeClient(
                device_id=device_id,
                client_id=str(getattr(s, "smart_breaker_client_id", "")),
                client_secret=str(getattr(s, "smart_breaker_client_secret", "")),
                subscription_key=str(getattr(s, "smart_breaker_subscription_key", "")),
                api_base=str(getattr(s, "smart_breaker_api_base", "")),
                token_url=str(getattr(s, "smart_breaker_token_url", "")),
            )
        except (AuthError, ValueError) as e:
            logger.error("AbleEdge client not started: %s", e)
            return None
        logger.info(
            "AWG control: Eaton AbleEdge device %s (site %s, interlock relay %s, fail-safe %s)",
            device_id,
            getattr(s, "smart_breaker_site_id", "") or "-",
            getattr(s, "smart_breaker_interlock_relay", 0) or "none",
            getattr(s, "smart_breaker_fail_safe", "off"),
        )
        return SmartBreakerController(settings_provider, client, relays, clock, event_sink)

    logger.error("Unknown smart_breaker_vendor %r — disabled", vendor)
    return None
