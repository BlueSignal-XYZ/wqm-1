"""
Minimal systemd notification client (sd_notify protocol).

Stdlib-only: writes datagrams to the socket systemd passes via NOTIFY_SOCKET.
Used with Type=notify + WatchdogSec so systemd restarts the service if the
supervisor stops confirming that every worker thread is alive — a real
liveness watchdog, not just crash-restart.
"""

import contextlib
import logging
import os
import socket

logger = logging.getLogger("wqm1.sdnotify")


class SdNotifier:
    """Best-effort sd_notify sender. No-ops when not under systemd."""

    def __init__(self, address: str | None = None) -> None:
        self._addr = address if address is not None else os.environ.get("NOTIFY_SOCKET")
        self._sock: socket.socket | None = None
        if self._addr:
            # Abstract-namespace sockets start with '@' in the env var.
            if self._addr.startswith("@"):
                self._addr = "\0" + self._addr[1:]
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            except OSError as e:
                logger.warning("sd_notify socket unavailable: %s", e)
                self._sock = None

    @property
    def available(self) -> bool:
        return self._sock is not None

    def _send(self, message: str) -> None:
        if not self._sock or not self._addr:
            return
        try:
            self._sock.sendto(message.encode(), self._addr)
        except OSError as e:
            logger.debug("sd_notify send failed: %s", e)

    def ready(self) -> None:
        """Signal startup complete (required for Type=notify)."""
        self._send("READY=1")

    def watchdog(self) -> None:
        """Pet the systemd watchdog (call only while all workers are healthy)."""
        self._send("WATCHDOG=1")

    def stopping(self) -> None:
        self._send("STOPPING=1")

    def status(self, text: str) -> None:
        self._send(f"STATUS={text[:200]}")

    def close(self) -> None:
        if self._sock:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
