"""
Supervisor worker that drives :meth:`SmartBreakerController.poll` on the
configured cadence. Lives on its own thread so a slow or rate-limited vendor
call can never stall sensor sampling, relay rules, or LoRa windows.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.workers import Worker
from integrations.smart_breaker.controller import SmartBreakerController


class SmartBreakerWorker(Worker):
    name = "smart-breaker"
    error_bucket = "cloud"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        controller: SmartBreakerController,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._controller = controller

    def interval_s(self) -> float:
        return float(getattr(self._settings(), "smart_breaker_poll_s", 60))

    def step(self) -> None:
        self._controller.poll()
