"""
Host pins — where each net on the WQM-1 HAT lands on THIS host board.

The HAT was designed for the Raspberry Pi 40-pin header, so every net in the
firmware is named by its Pi BCM number (``RELAY_1 = 17``, ``LORA_DIO1 = 16``,
``FAN_EN = 21`` …). That naming is the HAT's, not the Pi's: the physical pin
a BCM number lands on is fixed by the header, and the header is what a
compatible host board copies. So compatibility with a second host is one
table — physical pin → that SoC's pin — plus a way to turn the SoC's pin
name into something the kernel will drive (a gpiochip index and a line
offset).

Two sources feed the table, in increasing authority:

1. The vendor's published pinout (``ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC``).
   Orange Pi publishes the function pins (I2C, SPI, UART, PWM) and leaves
   the plain-GPIO pins out of the text; those are ``None`` here and the
   firmware REFUSES to drive a net whose pin is unknown rather than guess.
2. ``/etc/bluesignal/host-pins.yaml`` — written at the bench by
   ``scripts/host-pins.py --from-readall``, which parses the board's own
   ``gpio readall`` (wiringOP ships on every Orange Pi image). The board
   says what its pins are; we do not infer them from a different model.

A resolved table is a ``HostPins``: bus numbers, the GPS UART, and
``lines[bcm] = (chip, line)``. On the Raspberry Pi the map is the identity
(chip 0, line = BCM) and nothing here changes a field unit's behaviour.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("wqm1.hostpins")

#: Bench-written overrides (physical pin → SoC pin name, bus numbers).
OVERRIDES_PATH = "/etc/bluesignal/host-pins.yaml"

#: Raspberry Pi 40-pin header: BCM number → physical pin. The HAT's nets are
#: named in BCM numbers, so this is the one table every host maps through.
PI_HEADER_BCM_TO_PHYSICAL: dict[int, int] = {
    2: 3,
    3: 5,
    4: 7,
    14: 8,
    15: 10,
    17: 11,
    18: 12,
    27: 13,
    22: 15,
    23: 16,
    24: 18,
    10: 19,
    9: 21,
    25: 22,
    11: 23,
    8: 24,
    7: 26,
    0: 27,
    1: 28,
    5: 29,
    6: 31,
    12: 32,
    13: 33,
    19: 35,
    16: 36,
    26: 37,
    20: 38,
    21: 40,
}

PHYSICAL_TO_BCM: dict[int, int] = {p: b for b, p in PI_HEADER_BCM_TO_PHYSICAL.items()}

#: Orange Pi Zero 3W (Allwinner A733) — physical pin → SoC pin, from the
#: vendor's published pinout (orangepi.org product page, 2026). Power and
#: ground sit where the Pi puts them. ``None`` = a plain GPIO the published
#: text does not name; read it off the board with ``gpio readall``.
ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC: dict[int, str | None] = {
    3: "PB3",  # TWI0_SDA   → ADS1115 SDA
    5: "PB2",  # TWI0_SCK   → ADS1115 SCL
    7: "PB4",  # PWM0_0     → DS18B20 1-Wire (w1-gpio overlay on PB4)
    8: "PB9",  # UART0_TX   → GPS RX
    10: "PB10",  # UART0_RX → GPS TX
    11: "PB0",  # UART2_TX  → relay 1
    12: None,  # (unpublished) → LORA_RST
    13: "PB1",  # UART2_RX  → relay 2
    15: None,  # (unpublished) → relay 3
    16: "PL2",  # UART7_TX  → relay 4   (R_PIO — second gpiochip)
    18: "PL3",  # UART7_RX  → LED1 heartbeat (R_PIO)
    19: "PE2",  # SPI3_MOSI → LoRa MOSI
    21: "PE3",  # SPI3_MISO → LoRa MISO
    22: None,  # (unpublished) → LED2 LoRa TX
    23: "PE1",  # SPI3_CLK  → LoRa SCLK
    24: "PE0",  # SPI3_CS0  → LoRa CS
    26: "PE4",  # SPI3_CS1  → expansion IO7
    27: None,  # (unpublished) → expansion IO0
    28: None,  # (unpublished) → expansion IO1
    29: None,  # (unpublished) → ADS1115 ALERT/RDY (unused by firmware)
    31: None,  # (unpublished) → expansion IO6
    32: "PD1",  # PWM0_1    → LED3 GPS fix
    33: "PD3",  # PWM0_3    → LED4 error
    35: "PB6",  # PWM0_8    → GPS EXTINT
    36: "PD2",  # PWM0_2    → LORA_DIO1
    37: "PD4",  # PWM0_4    → flow pulse (default flow_pulse_gpio 26)
    38: "PB8",  # TWI1_SDA  → LORA_BUSY
    40: "PB7",  # TWI1_SCK / PWM0_9 → FAN_EN
}

#: Allwinner R_PIO ports live on a second gpiochip. In the BSP kernels' global
#: numbering PL0 = 11 * 32 = 352, which is also where that chip's base sits
#: when sysfs cannot be read to confirm it.
_SUNXI_RPIO_FIRST_PORT = "L"
_SUNXI_RPIO_BASE = (ord(_SUNXI_RPIO_FIRST_PORT) - ord("A")) * 32

_SOC_PIN_RE = re.compile(r"^P([A-M])(\d{1,2})$")


class UnresolvedPinError(RuntimeError):
    """A net the firmware needs has no known line on this host."""


@dataclass(frozen=True)
class HostPins:
    """Resolved host facts for the active board."""

    board_id: str
    backend: str  # "rpi" | "gpiochip" | "none"
    i2c_bus: int
    spi_bus: int
    spi_device: int
    gps_port: str
    w1_pin: str  # documentation + diagnostics only; the kernel overlay owns it
    soc_names: Mapping[int, str | None] = field(default_factory=dict)  # bcm → "PB4"
    lines: Mapping[int, tuple[int, int] | None] = field(default_factory=dict)  # bcm → (chip, line)
    source: str = "profile"

    def line(self, bcm: int) -> tuple[int, int]:
        """(chip, line) for a BCM-numbered net, or a precise refusal."""
        resolved = self.lines.get(bcm)
        if resolved is not None:
            return resolved
        physical = PI_HEADER_BCM_TO_PHYSICAL.get(bcm)
        name = self.soc_names.get(bcm)
        where = f"header pin {physical}" if physical else "no header pin"
        if name:
            raise UnresolvedPinError(
                f"BCM {bcm} ({where}, {name}) has no gpiochip line on {self.board_id} — "
                "could not map the SoC pin to a chip/line; check /sys/class/gpio/gpiochip*/base"
            )
        raise UnresolvedPinError(
            f"BCM {bcm} ({where}) is not published for {self.board_id} — run "
            "scripts/host-pins.py --from-readall on the board to record it in "
            f"{OVERRIDES_PATH}"
        )

    def unresolved(self, nets: Mapping[str, int]) -> list[tuple[str, int, int | None]]:
        """[(net name, bcm, physical)] for every net in ``nets`` with no line."""
        out: list[tuple[str, int, int | None]] = []
        for net, bcm in nets.items():
            if self.lines.get(bcm) is None:
                out.append((net, bcm, PI_HEADER_BCM_TO_PHYSICAL.get(bcm)))
        return out


# ---------------------------------------------------------------------------
# Raspberry Pi: identity map
# ---------------------------------------------------------------------------


def rpi_pins(board_id: str = "rpi-zero-2w") -> HostPins:
    """The reference host: Linux gpiochip0 line N is BCM N."""
    return HostPins(
        board_id=board_id,
        backend="rpi",
        i2c_bus=1,
        spi_bus=0,
        spi_device=0,
        gps_port="/dev/serial0",
        w1_pin="GPIO4",
        soc_names={bcm: f"GPIO{bcm}" for bcm in PI_HEADER_BCM_TO_PHYSICAL},
        lines={bcm: (0, bcm) for bcm in PI_HEADER_BCM_TO_PHYSICAL},
        source="profile",
    )


def headerless_pins(board_id: str) -> HostPins:
    """A host whose headers Linux cannot reach (Arduino Q family, generic)."""
    return HostPins(
        board_id=board_id,
        backend="none",
        i2c_bus=1,
        spi_bus=0,
        spi_device=0,
        gps_port="/dev/serial0",
        w1_pin="",
        soc_names={},
        lines={},
        source="profile",
    )


# ---------------------------------------------------------------------------
# Allwinner / Orange Pi: name → (chip, line)
# ---------------------------------------------------------------------------


def soc_global_number(name: str) -> int:
    """``PB4`` → 36. Allwinner BSP global GPIO number: port index × 32 + pin."""
    m = _SOC_PIN_RE.match(name.strip().upper())
    if not m:
        raise ValueError(f"not an Allwinner pin name: {name!r}")
    port, pin = m.group(1), int(m.group(2))
    if pin > 31:
        raise ValueError(f"pin index out of range: {name!r}")
    return (ord(port) - ord("A")) * 32 + pin


def soc_name_from_global(number: int) -> str:
    """36 → ``PB4`` (inverse of :func:`soc_global_number`)."""
    if number < 0:
        raise ValueError(f"negative gpio number: {number}")
    port = chr(ord("A") + number // 32)
    return f"P{port}{number % 32}"


def _sysfs_chip_bases(sysfs_root: str) -> list[tuple[int, int]]:
    """[(base, ngpio)] sorted by base, from /sys/class/gpio/gpiochip*/."""
    root = Path(sysfs_root)
    out: list[tuple[int, int]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.startswith("gpiochip"):
            continue
        try:
            base = int((entry / "base").read_text().strip())
            ngpio = int((entry / "ngpio").read_text().strip())
        except (OSError, ValueError):
            continue
        out.append((base, ngpio))
    out.sort()
    return out


def _lgpio_line_by_name(name: str, lgpio_module: Any, max_chips: int = 4) -> tuple[int, int] | None:
    """Ask the kernel: which chip/line is named ``name``? None when unnamed."""
    if lgpio_module is None:
        return None
    for chip in range(max_chips):
        handle = None
        try:
            handle = lgpio_module.gpiochip_open(chip)
            info = lgpio_module.gpio_get_chip_info(handle)
            n_lines = int(info[0])
            for line in range(n_lines):
                li = lgpio_module.gpio_get_line_info(handle, line)
                if str(li[2]).strip().upper() == name.upper():
                    return (chip, line)
        except Exception:  # noqa: BLE001  # nosec B112 — a chip that cannot be opened is skipped
            continue
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    lgpio_module.gpiochip_close(handle)
    return None


def resolve_soc_line(
    name: str,
    lgpio_module: Any = None,
    sysfs_root: str = "/sys/class/gpio",
) -> tuple[int, int]:
    """
    Turn an Allwinner pin name into (gpiochip index, line offset).

    Three sources, most authoritative first:
      1. the kernel's own line names (``gpio-line-names`` in the device tree,
         read through lgpio) — when the BSP populates them;
      2. the sysfs chip table (``/sys/class/gpio/gpiochip*/base``) plus the
         BSP global-number convention;
      3. the sunxi convention alone: PA–PK on chip 0 from base 0, PL/PM on
         chip 1 from base 352. Logged, because it is an assumption.
    """
    by_name = _lgpio_line_by_name(name, lgpio_module)
    if by_name is not None:
        return by_name
    number = soc_global_number(name)
    chips = _sysfs_chip_bases(sysfs_root)
    for index, (base, ngpio) in enumerate(chips):
        if base <= number < base + ngpio:
            return (index, number - base)
    if number >= _SUNXI_RPIO_BASE:
        logger.warning(
            "%s: assuming R_PIO on gpiochip1 base %d (unverified)", name, _SUNXI_RPIO_BASE
        )
        return (1, number - _SUNXI_RPIO_BASE)
    logger.warning("%s: assuming gpiochip0 base 0 (unverified)", name)
    return (0, number)


# ---------------------------------------------------------------------------
# Overrides file + gpio readall parser
# ---------------------------------------------------------------------------

_READALL_ROW_RE = re.compile(
    r"^\s*\|\s*(\d*)\s*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|\s*(\d+)\s*\|\|\s*(\d+)\s*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|\s*(\d*)\s*\|"
)


def parse_gpio_readall(text: str) -> dict[int, str]:
    """
    wiringOP ``gpio readall`` → {physical pin: SoC pin name}.

    Only the GPIO-number columns are trusted (first and last cells of a row);
    the Name column prints function aliases ("SDA.0") that are not pin names.
    The global number converts to a name by the BSP convention, which is
    exactly what wiringOP used to print it.
    """
    out: dict[int, str] = {}
    for raw in text.splitlines():
        m = _READALL_ROW_RE.match(raw)
        if not m:
            continue
        left_gpio, left_phys, right_phys, right_gpio = m.groups()
        if left_gpio:
            out[int(left_phys)] = soc_name_from_global(int(left_gpio))
        if right_gpio:
            out[int(right_phys)] = soc_name_from_global(int(right_gpio))
    return out


def load_overrides(path: str = OVERRIDES_PATH) -> dict[str, Any]:
    """Read the bench-written overrides; {} when absent or unreadable."""
    try:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 — a bad file must never stop boot
        if Path(path).exists():
            logger.warning("Ignoring unreadable host-pins file %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def merge_physical_overrides(
    base: Mapping[int, str | None], overrides: Mapping[Any, Any]
) -> tuple[dict[int, str | None], list[str]]:
    """
    Apply ``{physical pin: "PB4"}`` overrides. Returns (table, conflicts):
    a conflict is a published pin the override contradicts — recorded, not
    silently taken, because one of the two sources is wrong.
    """
    table = dict(base)
    conflicts: list[str] = []
    for key, value in overrides.items():
        try:
            physical = int(key)
        except (TypeError, ValueError):
            continue
        if physical not in PHYSICAL_TO_BCM:
            continue
        if value is None:
            continue
        name = str(value).strip().upper()
        if not _SOC_PIN_RE.match(name):
            conflicts.append(f"pin {physical}: {value!r} is not an Allwinner pin name")
            continue
        published = base.get(physical)
        if published and published != name:
            conflicts.append(f"pin {physical}: published {published}, board reports {name}")
        table[physical] = name
    return table, conflicts


def orangepi_zero_3w_pins(
    overrides: Mapping[str, Any] | None = None,
    lgpio_module: Any = None,
    sysfs_root: str = "/sys/class/gpio",
) -> HostPins:
    """Build the Orange Pi Zero 3W table (published pinout + bench overrides)."""
    ov = dict(overrides or {})
    physical_ov: dict[Any, Any] = ov["pins"] if isinstance(ov.get("pins"), dict) else {}
    table, conflicts = merge_physical_overrides(ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC, physical_ov)
    for c in conflicts:
        logger.warning("host-pins override conflict: %s", c)

    soc_names: dict[int, str | None] = {}
    lines: dict[int, tuple[int, int] | None] = {}
    for bcm, physical in PI_HEADER_BCM_TO_PHYSICAL.items():
        name = table.get(physical)
        soc_names[bcm] = name
        if name is None:
            lines[bcm] = None
            continue
        try:
            lines[bcm] = resolve_soc_line(name, lgpio_module=lgpio_module, sysfs_root=sysfs_root)
        except ValueError as e:
            logger.warning("BCM %d (%s): %s", bcm, name, e)
            lines[bcm] = None

    def _int(key: str, default: int) -> int:
        try:
            return int(ov.get(key, default))
        except (TypeError, ValueError):
            return default

    gps_port = str(ov.get("gps_port") or "/dev/ttyS0")
    return HostPins(
        board_id="orangepi-zero-3w",
        backend="gpiochip",
        # TWI0 on pins 3/5. The BSP usually numbers /dev/i2c-N after the TWI
        # controller, but not always — i2cdetect -l is the check, and
        # host-pins.yaml ``i2c_bus:`` is the override.
        i2c_bus=_int("i2c_bus", 0),
        # SPI3 on pins 19/21/23/24 → /dev/spidev3.0 once the spidev overlay
        # is enabled.
        spi_bus=_int("spi_bus", 3),
        spi_device=_int("spi_device", 0),
        # UART0 on pins 8/10 is the board's debug console; setup.sh frees it.
        gps_port=gps_port,
        w1_pin=str(table.get(7) or "PB4"),
        soc_names=soc_names,
        lines=lines,
        source="profile+overrides"
        if physical_ov or any(k in ov for k in ("i2c_bus", "spi_bus", "spi_device", "gps_port"))
        else "profile",
    )


_MISSING = object()


def pins_for_board(
    board_id: str,
    overrides_path: str = OVERRIDES_PATH,
    lgpio_module: Any = _MISSING,
    sysfs_root: str = "/sys/class/gpio",
) -> HostPins:
    """The HostPins for a profile id (see board.PROFILES)."""
    if board_id == "orangepi-zero-3w":
        lg = lgpio_module
        if lg is _MISSING:
            # The kernel's own line names are the best source; consult them
            # when lgpio is installed, and fall through to sysfs when not.
            try:
                import lgpio as lg  # type: ignore[no-redef]
            except ImportError:
                lg = None
        return orangepi_zero_3w_pins(
            overrides=load_overrides(overrides_path),
            lgpio_module=lg,
            sysfs_root=sysfs_root,
        )
    if board_id == "rpi-zero-2w":
        return rpi_pins(board_id)
    return headerless_pins(board_id)
