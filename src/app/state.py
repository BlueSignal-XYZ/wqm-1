"""
Shared state between workers — a small lock-guarded snapshot store.

Workers never call into each other; they publish here (GPS fix, LoRa RSSI,
error counters, liveness beats) and read snapshots. Cross-worker *events*
(device events destined for the cloud) go through the bounded event queue.
"""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GpsFix:
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    sats: int | None = None


class StateStore:
    """Thread-safe shared state. All getters return copies/immutables."""

    # Error-counter buckets, matching the heartbeat errorCounts contract.
    ERROR_BUCKETS = ("sensor", "lora", "cloud", "gps", "db")

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._gps = GpsFix()
        self._lora_rssi: int | None = None
        self._error_counts: dict[str, int] = dict.fromkeys(self.ERROR_BUCKETS, 0)
        self._beats: dict[str, float] = {}
        self._restart_requested = threading.Event()
        # Device events awaiting upload (monitor findings, config acks, ...).
        # Bounded: if the cloud is unreachable for long, old events drop first —
        # readings (the primary data) have their own durable SQLite buffer.
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)

    # -- GPS ------------------------------------------------------------------

    def set_gps(self, lat: float, lon: float, alt_m: float | None, sats: int | None) -> None:
        with self._lock:
            self._gps = GpsFix(lat, lon, alt_m, sats)

    def gps(self) -> GpsFix:
        with self._lock:
            return self._gps

    # -- Radio ----------------------------------------------------------------

    def set_lora_rssi(self, rssi: int) -> None:
        with self._lock:
            self._lora_rssi = rssi

    def lora_rssi(self) -> int | None:
        with self._lock:
            return self._lora_rssi

    # -- Errors ---------------------------------------------------------------

    def incr_error(self, bucket: str) -> None:
        with self._lock:
            if bucket in self._error_counts:
                self._error_counts[bucket] += 1

    def error_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._error_counts)

    # -- Liveness -------------------------------------------------------------

    def beat(self, worker: str) -> None:
        with self._lock:
            self._beats[worker] = self._clock()

    def beats(self) -> dict[str, float]:
        with self._lock:
            return dict(self._beats)

    # -- Device events --------------------------------------------------------

    def emit_event(self, event: dict[str, Any]) -> bool:
        """Queue a device event for upload. Returns False if the queue is full
        (event dropped — never blocks a worker)."""
        try:
            self.events.put_nowait(event)
            return True
        except queue.Full:
            return False

    def drain_events(self, limit: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # -- Restart --------------------------------------------------------------

    def request_restart(self) -> None:
        """Ask the supervisor for a graceful restart (systemd brings us back)."""
        self._restart_requested.set()

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested.is_set()
