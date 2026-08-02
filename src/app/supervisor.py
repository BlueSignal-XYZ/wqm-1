"""
Supervisor — owns the worker threads and the process-level safety rails.

Main-thread loop (1s tick):
- thermal fan control
- periodic DB rotation
- liveness check over every worker's last beat; ONLY when all are fresh does
  it pet the systemd watchdog (Type=notify + WatchdogSec) and the optional
  BCM2835 hardware watchdog. A hung worker ⇒ no pet ⇒ systemd restarts us —
  that's the self-heal the v1 loop never had.
- FAULT LED when any worker is on an error streak
- graceful shutdown / restart-on-request (remote `restart` command)
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.state import StateStore
from app.workers import FAULT_STREAK, Worker

logger = logging.getLogger("wqm1.supervisor")

# A worker is considered hung after this many multiples of its interval
# without a beat (interval + one full step's worth of slack, then some).
LIVENESS_FACTOR = 3.0
# Floor so fast workers (5s command poll) aren't declared hung by one slow
# HTTP timeout; ceiling so slow workers (600s GPS) still fail fast enough
# for WatchdogSec=180 to be meaningful — the supervisor itself pets more
# often than any worker deadline.
LIVENESS_MIN_S = 60.0

DB_ROTATE_EVERY_S = 600.0


class Supervisor:
    """Runs workers in threads and guards their liveness."""

    def __init__(
        self,
        workers: list[Worker],
        state: StateStore,
        fan: Any = None,
        leds: Any = None,
        db: Any = None,
        notifier: Any = None,
        hw_watchdog: Any = None,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = 1.0,
    ) -> None:
        self._workers = workers
        self._state = state
        self._fan = fan
        self._leds = leds
        self._db = db
        self._notifier = notifier
        self._hw_watchdog = hw_watchdog
        self._clock = clock
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_rotate = clock()
        self._faulted: set[str] = set()
        self.exit_reason: str | None = None

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def start(self) -> None:
        for worker in self._workers:
            t = threading.Thread(
                target=worker.run_forever,
                args=(self._stop, self._state, self._on_fault),
                name=f"wqm1-{worker.name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
            self._state.beat(worker.name)  # seed liveness so startup isn't a false hang
        if self._notifier:
            self._notifier.ready()
        logger.info("Supervisor started %d workers", len(self._workers))

    def _on_fault(self, worker_name: str) -> None:
        if worker_name not in self._faulted:
            logger.error(
                "Worker %s entered FAULT state (%d+ consecutive errors)", worker_name, FAULT_STREAK
            )
        self._faulted.add(worker_name)
        if self._leds:
            self._leds.error_pattern(5)

    def _deadline_for(self, worker: Worker) -> float:
        try:
            interval = float(worker.interval_s())
        except Exception:  # noqa: BLE001 — a broken interval() must not kill liveness
            interval = 60.0
        return max(interval * LIVENESS_FACTOR, LIVENESS_MIN_S)

    def all_workers_alive(self) -> bool:
        now = self._clock()
        beats = self._state.beats()
        for worker in self._workers:
            last = beats.get(worker.name)
            if last is None or (now - last) > self._deadline_for(worker):
                logger.warning(
                    "Worker %s liveness overdue (last beat %.0fs ago)",
                    worker.name,
                    (now - last) if last is not None else -1,
                )
                return False
        return True

    def tick(self) -> None:
        """One supervisor cycle — extracted for tests."""
        if self._fan:
            try:
                self._fan.update()
            except Exception as e:  # noqa: BLE001
                logger.error("Fan update error: %s", e)

        now = self._clock()
        if self._db is not None and now - self._last_rotate >= DB_ROTATE_EVERY_S:
            try:
                self._db.rotate()
            except Exception as e:  # noqa: BLE001
                logger.error("DB rotate error: %s", e)
                self._state.incr_error("db")
            self._last_rotate = now

        # Watchdogs are petted ONLY when every worker is alive. A hung thread
        # (blocking call that never returns) starves the pet and systemd
        # restarts the whole service — the failure mode Restart=on-failure
        # could never catch.
        if self.all_workers_alive():
            if self._notifier:
                self._notifier.watchdog()
            if self._hw_watchdog:
                self._hw_watchdog.pet()

    def run(self) -> None:
        """Block until stop() or a restart request; runs the tick loop."""
        while not self._stop.is_set():
            self.tick()
            if self._state.restart_requested:
                logger.info("Restart requested — shutting down for systemd to relaunch")
                self.exit_reason = "restart"
                break
            self._stop.wait(self._tick_s)

    def stop(self, join_timeout_s: float = 10.0) -> None:
        self._stop.set()
        if self._notifier:
            self._notifier.stopping()
        for t in self._threads:
            t.join(timeout=join_timeout_s)
        alive = [t.name for t in self._threads if t.is_alive()]
        if alive:
            logger.warning("Workers did not stop in time: %s", ", ".join(alive))
        if self._hw_watchdog:
            self._hw_watchdog.close()
