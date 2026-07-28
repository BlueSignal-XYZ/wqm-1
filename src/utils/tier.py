"""
Commercial-tier entitlement gate for relay control.

WHY THIS LIVES OUTSIDE ChannelController
----------------------------------------
Requirement: gating must never be able to disable fail-safe behaviour or stop
an already-running control loop when the device is offline or the flag cannot
be refreshed.

The way that is guaranteed here is structural, not by discipline:
``ChannelController`` holds no reference to this class and never imports it.
There is no code path from an entitlement check to ``revert_to_fail_safe``,
to a dwell timer, or to the evaluation loop. A billing outage physically
cannot reach the safety layer.

The gate is consulted in exactly three places, all of them *admission*
decisions made before control is running:

  1. commissioning a channel
  2. loading/accepting new setpoints or rules
  3. accepting a manual actuation request

BEHAVIOUR ON LAPSE (founder decision, 2026-07-27)
-------------------------------------------------
An entitled device that later loses entitlement keeps running its existing
control loop and keeps its fail-safe behaviour, and simply stops accepting new
setpoints. A lapsed subscription must never be able to kill a customer's fish.

OFFLINE
-------
The last known good answer is cached on disk and reused indefinitely. Failing
to reach the cloud is *not* a revocation — it is silence, and silence changes
nothing.
"""

import json
import logging
from pathlib import Path
from typing import Any

from utils.config import atomic_json_write

logger = logging.getLogger("wqm1.tier")

_DEFAULT_CACHE_PATH = "/var/lib/bluesignal/entitlement.json"


class TierGate:
    """Cached Commercial-tier entitlement for control features."""

    def __init__(self, cache_path: str | None = None) -> None:
        self._path = Path(cache_path or _DEFAULT_CACHE_PATH)
        self._granted = False
        self._ever_granted = False
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self._granted = bool(data.get("granted", False))
                self._ever_granted = bool(data.get("ever_granted", self._granted))
                logger.info(
                    "Entitlement cache loaded: granted=%s ever_granted=%s",
                    self._granted,
                    self._ever_granted,
                )
        except Exception as e:
            # A corrupt cache must not take the device down, and must not
            # silently grant control either.
            logger.warning("Could not read entitlement cache: %s", e)

    def _persist(self) -> None:
        try:
            atomic_json_write(
                str(self._path),
                {"granted": self._granted, "ever_granted": self._ever_granted},
            )
        except Exception as e:
            logger.error("Could not persist entitlement cache: %s", e)

    # -- refresh -----------------------------------------------------------

    def refresh(self, entitled: bool | None) -> None:
        """
        Apply a cloud answer.

        ``None`` means "could not reach the cloud" and is deliberately a no-op:
        an outage is silence, not a revocation.
        """
        if entitled is None:
            logger.debug("Entitlement refresh unavailable — keeping last known good")
            return

        changed = entitled != self._granted
        self._granted = bool(entitled)
        if entitled:
            self._ever_granted = True
        if changed:
            logger.warning(
                "Commercial control entitlement %s", "granted" if entitled else "revoked"
            )
            if not entitled:
                logger.warning(
                    "Existing control loop and fail-safe behaviour continue; "
                    "new setpoints and commissioning are refused until restored."
                )
            self._persist()
        elif not self._path.exists():
            self._persist()

    # -- admission decisions ----------------------------------------------

    @property
    def granted(self) -> bool:
        """Current entitlement (last known good)."""
        return self._granted

    @property
    def loop_may_run(self) -> bool:
        """
        May the on-device control loop keep evaluating?

        True once the device has ever been entitled. Deliberately sticky: a
        lapse stops new setpoints, never a running loop.
        """
        return self._ever_granted

    def allows_commissioning(self) -> bool:
        return self._granted

    def allows_new_setpoints(self) -> bool:
        return self._granted

    def allows_manual_control(self) -> bool:
        return self._granted

    def snapshot(self) -> dict[str, Any]:
        return {"granted": self._granted, "ever_granted": self._ever_granted}
