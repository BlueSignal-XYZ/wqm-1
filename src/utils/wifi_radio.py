"""Which station bands this unit's radio can join.

The setup hotspot stays 2.4 GHz (hostapd ``hw_mode=g``, channels 1/6/11).
Station joins follow the radio in the board:

* Raspberry Pi Zero W, Zero 2 W and 3B are 2.4 GHz only. A join on those
  boards locks ``802-11-wireless.band`` to ``bg`` so wpa_supplicant picks
  the 2.4 GHz BSS of a dual-band SSID (Spectrum-style band steering).
* Pi 3B+, 3A+, 4, 400 and 5 may join 2.4 or 5 GHz. Those boards get no
  band lock, and the regulatory domain must be a real country (default US)
  so 5 GHz channels are not disabled.

Detection prefers a live ``iw phy`` / ``iw list`` read, then the device-tree
model and the Pi revision code. Unknown hardware is treated as 2.4 GHz only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from platform_support.board import CPUINFO_PATH, DEVICE_TREE_MODEL
from utils.wifi_setup import band_of_mhz, parse_freq_token

logger = logging.getLogger("wqm1.wifi_radio")

# 2.4 GHz 20 MHz channels, including Japan channel 14.
FREQ_2G4_MHZ: tuple[int, ...] = tuple(range(2412, 2473, 5)) + (2484,)

# Pi revision types (bits 4..11 of a new-style revision).
_REV_2G4_ONLY = frozenset({0x0C, 0x12, 0x08})  # Zero W, Zero 2 W, 3B
_REV_DUAL = frozenset({0x0D, 0x0E, 0x11, 0x13, 0x17, 0x19})  # 3B+, 3A+, 4B, 400, 5, 500

_CACHED_BANDS: frozenset[str] | None = None


def reset_cache() -> None:
    """Tests only: forget a cached radio read."""
    global _CACHED_BANDS
    _CACHED_BANDS = None


def parse_iw_phy(text: str) -> frozenset[str]:
    """Bands advertised by ``iw phy`` or ``iw list``.

    A frequency marked ``disabled`` is ignored. 6 GHz is recorded as ``5``
    for the station-join policy (the lock is "2.4 only" vs "high band OK").
    """
    bands: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip().lower()
        if "mhz" not in line:
            continue
        if "disabled" in line:
            continue
        freq = parse_freq_token(raw)
        band = band_of_mhz(freq)
        if band == "2.4":
            bands.add("2.4")
        elif band in {"5", "6"}:
            bands.add("5")
    return frozenset(bands)


def _compact(model: str) -> str:
    return model.lower().replace(" ", "").replace("-", "").replace("_", "")


def bands_from_board(model: str = "", revision: str = "") -> frozenset[str] | None:
    """Station bands from a Raspberry Pi model string or revision, or None."""
    compact = _compact(model or "")
    # Plus / 4 / 5 first: "raspberrypi3modelbplus" also contains "raspberrypi3".
    if "plus" in compact or "3b+" in compact or "3a+" in compact:
        return frozenset({"2.4", "5"})
    if re.search(r"raspberrypi400(?!\d)", compact) or re.search(r"raspberrypi4(?!\d)", compact):
        return frozenset({"2.4", "5"})
    if re.search(r"raspberrypi5(?!\d)", compact) or re.search(r"raspberrypi500(?!\d)", compact):
        return frozenset({"2.4", "5"})
    if "zero2" in compact or "zerow" in compact:
        return frozenset({"2.4"})
    if re.search(r"raspberrypi3(?!\d)", compact):
        return frozenset({"2.4"})
    if revision:
        text = revision.strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        try:
            value = int(text, 16)
        except ValueError:
            value = 0
        if value & (1 << 23):
            kind = (value >> 4) & 0xFF
            if kind in _REV_DUAL:
                return frozenset({"2.4", "5"})
            if kind in _REV_2G4_ONLY:
                return frozenset({"2.4"})
    return None


def _read_model(model_path: str = DEVICE_TREE_MODEL) -> str:
    try:
        raw = Path(model_path).read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b"").decode("utf-8", errors="replace").strip()


def _read_revision(cpuinfo_path: str = CPUINFO_PATH) -> str:
    try:
        text = Path(cpuinfo_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip().lower() == "revision":
            return val.strip()
    return ""


def station_bands(
    *,
    iw_phy_text: str | None = None,
    model: str | None = None,
    revision: str | None = None,
) -> frozenset[str]:
    """Bands this radio can join as a station.

    A live ``iw phy`` read wins when it names at least one band. Otherwise
    the board model or revision is used. Unknown hardware is 2.4 GHz only.
    """
    if iw_phy_text:
        seen = parse_iw_phy(iw_phy_text)
        if seen:
            return seen
    board = bands_from_board(model or "", revision or "")
    if board:
        return board
    return frozenset({"2.4"})


def detect_station_bands(
    *,
    iw_phy_text: str | None = None,
    model_path: str = DEVICE_TREE_MODEL,
    cpuinfo_path: str = CPUINFO_PATH,
    run: Any = None,
) -> frozenset[str]:
    """Read the radio (and the board) and cache the result for this process."""
    global _CACHED_BANDS
    text = iw_phy_text
    if text is None and run is not None:
        for argv in (["iw", "phy"], ["iw", "list"]):
            try:
                rc, out, _err = run(argv)
            except (TypeError, ValueError):
                continue
            if rc == 0 and out:
                text = out
                break
    model = _read_model(model_path)
    revision = _read_revision(cpuinfo_path)
    bands = station_bands(iw_phy_text=text, model=model, revision=revision)
    _CACHED_BANDS = bands
    logger.info("Station Wi-Fi bands: %s (model=%r)", "+".join(sorted(bands)) or "none", model)
    return bands


def cached_station_bands() -> frozenset[str]:
    if _CACHED_BANDS is not None:
        return _CACHED_BANDS
    return detect_station_bands()


def allows_5ghz(bands: frozenset[str] | None = None) -> bool:
    return "5" in (bands if bands is not None else cached_station_bands())


def station_nm_settings(bands: frozenset[str] | None = None) -> list[str]:
    """Extra ``nmcli connection add`` key/value pairs for a station profile.

    2.4-only radios lock the band so a dual-band SSID lands on the 2.4 GHz
    BSS. Dual-band radios get no lock; country is set globally by
    ``prepare_wifi_radio`` (default US).
    """
    if allows_5ghz(bands):
        return []
    return ["802-11-wireless.band", "bg"]
