#!/usr/bin/env python3
"""
host-pins — record where the WQM-1 HAT's nets land on THIS host board.

The HAT names its lines in Raspberry Pi BCM numbers. On a Raspberry Pi that
is the whole story. On the Orange Pi Zero 3W the firmware ships the vendor's
published pinout, which names the I2C/SPI/UART/PWM pins and leaves five
plain-GPIO header pins (12, 15, 22, 29, 31) out — and the firmware refuses
to drive a net whose pin is unknown rather than guess. This script fills
those in from the board's own ``gpio readall`` (wiringOP, preinstalled on
every Orange Pi image), and shows the resolved table.

Usage (on the unit):

    gpio readall | sudo python3 scripts/host-pins.py --from-readall -
    sudo python3 scripts/host-pins.py --check

``--from-readall`` merges the parsed pins into /etc/bluesignal/host-pins.yaml
(keeping any bus overrides already there) and then prints the check.
``--check`` exits 1 while any net the firmware drives is unresolved, so
setup.sh and diagnostics.sh can gate on it.

Conflicts between the published pinout and what the board reports are
printed and the board's value is taken — the board is the authority — but
a conflict means one of the two sources is wrong, so look at it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "src", _HERE.parent):
    if (candidate / "platform_support").is_dir():
        sys.path.insert(0, str(candidate))
        break

from platform_support import PROFILES, detect_board  # noqa: E402
from platform_support.hostpins import (  # noqa: E402
    ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC,
    OVERRIDES_PATH,
    PI_HEADER_BCM_TO_PHYSICAL,
    load_overrides,
    merge_physical_overrides,
    parse_gpio_readall,
    pins_for_board,
)

# Every net the direct-header stack drives (mirrors main._hat_nets()).
HAT_NETS: dict[str, int] = {
    "relay_1": 17,
    "relay_2": 27,
    "relay_3": 22,
    "relay_4": 23,
    "led_1": 24,
    "led_2": 25,
    "led_3": 12,
    "led_4": 13,
    "lora_rst": 18,
    "lora_busy": 20,
    "lora_dio1": 16,
    "gps_extint": 19,
    "fan_en": 21,
    "flow_pulse": 26,
}


def _write_overrides(path: Path, pins: dict[int, str], existing: dict) -> None:
    import yaml

    data = dict(existing)
    current = data.get("pins") if isinstance(data.get("pins"), dict) else {}
    merged = {int(k): str(v) for k, v in current.items() if str(k).isdigit()}
    merged.update(pins)
    data["pins"] = dict(sorted(merged.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "# Written by scripts/host-pins.py from this board's `gpio readall`.\n"
        "# physical header pin -> SoC pin name. Bus overrides: i2c_bus, spi_bus,\n"
        "# spi_device, gps_port. See docs/platforms.md.\n" + yaml.safe_dump(data, sort_keys=False)
    )
    tmp.replace(path)


def _check(board_id: str, overrides_path: str) -> int:
    pins = pins_for_board(board_id, overrides_path=overrides_path)
    print(f"board: {board_id}  backend: {pins.backend}  source: {pins.source}")
    print(
        f"i2c: /dev/i2c-{pins.i2c_bus}   spi: /dev/spidev{pins.spi_bus}.{pins.spi_device}   "
        f"gps: {pins.gps_port}   1-wire: {pins.w1_pin}"
    )
    print(f"{'net':<11}{'BCM':>4}{'pin':>5}  {'SoC':<6}{'chip/line'}")
    for net, bcm in HAT_NETS.items():
        physical = PI_HEADER_BCM_TO_PHYSICAL.get(bcm)
        name = pins.soc_names.get(bcm) or "?"
        line = pins.lines.get(bcm)
        where = f"gpiochip{line[0]} line {line[1]}" if line else "UNRESOLVED"
        print(f"{net:<11}{bcm:>4}{physical:>5}  {name:<6}{where}")
    missing = pins.unresolved(HAT_NETS)
    if missing:
        print(
            f"\n{len(missing)} net(s) unresolved. On the board run:\n"
            "  gpio readall | sudo python3 scripts/host-pins.py --from-readall -"
        )
        return 1
    print("\nall nets resolved")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--board", default="auto", help="profile id (default: detect)")
    ap.add_argument(
        "--from-readall", metavar="FILE", help="parse `gpio readall` output ('-' = stdin)"
    )
    ap.add_argument(
        "--write", default=OVERRIDES_PATH, help=f"overrides file (default {OVERRIDES_PATH})"
    )
    ap.add_argument("--check", action="store_true", help="print the resolved table (default)")
    args = ap.parse_args(argv)

    board = detect_board(override=args.board)
    if board.id not in PROFILES:
        print(f"unknown board {board.id}", file=sys.stderr)
        return 2

    if args.from_readall:
        text = sys.stdin.read() if args.from_readall == "-" else Path(args.from_readall).read_text()
        parsed = parse_gpio_readall(text)
        if not parsed:
            print("no pins parsed — is this `gpio readall` output?", file=sys.stderr)
            return 2
        if board.id != "orangepi-zero-3w":
            print(f"{board.id} needs no overrides (its map is built in); nothing written")
            return 0
        _table, conflicts = merge_physical_overrides(ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC, parsed)
        for c in conflicts:
            print(f"CONFLICT {c}")
        wanted = {p: n for p, n in parsed.items() if p in PI_HEADER_BCM_TO_PHYSICAL.values()}
        _write_overrides(Path(args.write), wanted, load_overrides(args.write))
        print(f"wrote {len(wanted)} header pins to {args.write}")

    return _check(board.id, args.write)


if __name__ == "__main__":
    sys.exit(main())
