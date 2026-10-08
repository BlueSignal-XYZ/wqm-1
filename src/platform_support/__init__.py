"""
Board/platform support: which host the firmware runs on (board.py), where the
HAT's nets land on it (hostpins.py), and one door to drive them (gpio.py).

``set_active_board()`` is called once by main.py after reading the ``board``
config setting; everything else asks ``active_board()`` / ``active_pins()``.
A process that never calls it (tests, scripts) gets auto-detection, which on
a development host resolves to the Raspberry Pi profile.
"""

from __future__ import annotations

import threading

from platform_support.board import PROFILES, BoardProfile, detect_board
from platform_support.hostpins import HostPins, UnresolvedPinError, pins_for_board

__all__ = [
    "PROFILES",
    "BoardProfile",
    "HostPins",
    "UnresolvedPinError",
    "active_board",
    "active_pins",
    "detect_board",
    "pins_for_board",
    "set_active_board",
]

_lock = threading.Lock()
_board: BoardProfile | None = None
_pins: HostPins | None = None


def set_active_board(profile: BoardProfile | None, pins: HostPins | None = None) -> None:
    """Pin the board for this process (``None`` returns to auto-detection)."""
    global _board, _pins
    with _lock:
        _board = profile
        _pins = pins if profile is not None else None


def active_board() -> BoardProfile:
    global _board
    with _lock:
        if _board is None:
            _board = detect_board()
        return _board


def active_pins() -> HostPins:
    """Resolved host pins for the active board (resolved once, then cached)."""
    global _pins
    board = active_board()
    with _lock:
        if _pins is None or _pins.board_id != board.id:
            _pins = pins_for_board(board.id)
        return _pins
