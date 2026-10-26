"""Which Pi a golden card was first booted on.

The card stores that serial in ``/var/lib/wqm1/card-binding`` (override with
``WQM1_CARD_STATE``). The file is four ``key=value`` lines so the first-boot
shell and this module write the same bytes:

    commissioned_serial=<16 hex>
    moved_from=<16 hex or empty>
    moved_to=<16 hex or empty>
    wifi_reconfirm=0|1

A later boot on a different Pi sets ``moved_from`` / ``moved_to``. Hostname,
SSID and passphrase still follow the Pi the card is in now. If the card
already has a site Wi-Fi profile (or NetworkManager could not be asked),
``wifi_reconfirm`` latches and boot will not join that profile until setup
joins a network again. The setup page reads ``GET /setup/card-status``; this
module does not render that page.

``moved_from`` is the serial the card was first commissioned with, not the
printed WQM label. The label stays on the card across a Pi swap.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.atomic import atomic_write_text
from utils.identity import FALLBACK_PI_SERIAL, get_pi_serial

_STATE_ENV = "WQM1_CARD_STATE"
_DEFAULT_PATH = Path("/var/lib/wqm1/card-binding")
_SERIAL_RE = re.compile(r"^[0-9a-f]{16}$")
_KEYS = ("commissioned_serial", "moved_from", "moved_to", "wifi_reconfirm")


@dataclass
class CardBinding:
    commissioned_serial: str = ""
    moved_from: str = ""
    moved_to: str = ""
    wifi_reconfirm: bool = False

    @property
    def moved(self) -> bool:
        return bool(self.moved_from) and self.moved_from != self.moved_to


def state_path() -> Path:
    override = os.environ.get(_STATE_ENV)
    if override:
        return Path(override)
    return _DEFAULT_PATH


def normalise_serial(serial: str) -> str:
    """Last 16 hex digits, lower-case, or "" when it is not a Pi serial."""
    hexdigits = "".join(c for c in (serial or "").strip().lower() if c in "0123456789abcdef")
    if not hexdigits:
        return ""
    serial_16 = hexdigits[-16:].zfill(16)
    if not _SERIAL_RE.match(serial_16) or serial_16 == FALLBACK_PI_SERIAL:
        return ""
    return serial_16


def load(path: Path | None = None) -> CardBinding:
    """Read the binding. A missing or unreadable file is an empty binding."""
    target = path or state_path()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return CardBinding()
    found: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _KEYS:
            found[key] = value.strip()
    commissioned = normalise_serial(found.get("commissioned_serial", ""))
    moved_from = normalise_serial(found.get("moved_from", ""))
    moved_to = normalise_serial(found.get("moved_to", ""))
    return CardBinding(
        commissioned_serial=commissioned,
        moved_from=moved_from,
        moved_to=moved_to,
        wifi_reconfirm=found.get("wifi_reconfirm", "0") == "1",
    )


def save(binding: CardBinding, path: Path | None = None) -> None:
    target = path or state_path()
    recon = "1" if binding.wifi_reconfirm else "0"
    text = (
        f"commissioned_serial={binding.commissioned_serial}\n"
        f"moved_from={binding.moved_from}\n"
        f"moved_to={binding.moved_to}\n"
        f"wifi_reconfirm={recon}\n"
    )
    atomic_write_text(target, text, mode=0o644)


def observe(current: str, *, site_wifi: bool | None, path: Path | None = None) -> CardBinding:
    """Record ``current`` or flag a move.

    ``site_wifi`` True means a site profile is saved. None means the list
    could not be read; a move then latches reconfirm so the old network is
    not joined on a guess. False means the list was empty. The first serial
    seen is stored and is not, by itself, a move.
    """
    target = path or state_path()
    serial = normalise_serial(current)
    binding = load(target)
    if not serial:
        return binding
    if not binding.commissioned_serial:
        binding = CardBinding(commissioned_serial=serial)
        save(binding, target)
        return binding
    if binding.commissioned_serial == serial:
        if not binding.wifi_reconfirm and (binding.moved_from or binding.moved_to):
            binding = CardBinding(commissioned_serial=serial)
            save(binding, target)
        return binding
    # Unknown is fail-closed: do not adopt the new Pi or join a saved network.
    needs_hold = site_wifi is None or site_wifi
    if needs_hold:
        binding = CardBinding(
            commissioned_serial=binding.commissioned_serial,
            moved_from=binding.commissioned_serial,
            moved_to=serial,
            wifi_reconfirm=True,
        )
    else:
        binding = CardBinding(
            commissioned_serial=serial,
            moved_from=binding.commissioned_serial,
            moved_to=serial,
            wifi_reconfirm=False,
        )
    save(binding, target)
    return binding


def confirm_binding(serial: str | None = None, path: Path | None = None) -> bool:
    """Setup joined a network on this Pi. The card is commissioned to it.

    No binding file means this boot never recorded a Pi, and a normal join
    must not create one out of the fallback serial.
    """
    target = path or state_path()
    if not target.is_file():
        return False
    chosen = normalise_serial(serial if serial is not None else get_pi_serial())
    if not chosen:
        return False
    save(CardBinding(commissioned_serial=chosen), target)
    return True


def wifi_reconfirm_required(path: Path | None = None) -> bool:
    return load(path).wifi_reconfirm


def status_values(path: Path | None = None) -> tuple[str, str]:
    """``(card line, reconfirm line)`` for ``wqm1-status.txt``.

    The card line is ``moved from <old> to <new>`` or ``ok <serial>``.
    Reconfirm is ``required`` or ``ok``. Empty strings when nothing is recorded.
    """
    binding = load(path)
    if binding.moved:
        card = f"moved from {binding.moved_from} to {binding.moved_to}"
    elif binding.commissioned_serial:
        card = f"ok {binding.commissioned_serial}"
    else:
        card = ""
    if not binding.commissioned_serial and not binding.moved:
        return "", ""
    return card, ("required" if binding.wifi_reconfirm else "ok")


def heartbeat_fields(path: Path | None = None) -> dict[str, Any]:
    """Cloud heartbeat fields. Absent unless the card is flagged as moved,
    so a unit that has not moved sends the same heartbeat as before."""
    binding = load(path)
    if not binding.moved:
        return {}
    fields: dict[str, Any] = {
        "cardMoved": True,
        "cardMovedFrom": binding.moved_from,
        "cardMovedTo": binding.moved_to,
    }
    if binding.wifi_reconfirm:
        fields["wifiReconfirm"] = True
    return fields


def card_status_payload(path: Path | None = None) -> dict[str, Any]:
    """JSON the setup page can read later. No Wi-Fi material."""
    binding = load(path)
    return {
        "card_moved": binding.moved,
        "wifi_reconfirm": binding.wifi_reconfirm,
        "from": binding.moved_from,
        "to": binding.moved_to,
        "commissioned_serial": binding.commissioned_serial,
    }
