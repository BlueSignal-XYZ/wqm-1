#!/usr/bin/env python3
"""The text of ``wqm1-card.txt``: what a card is, written on the card.

    python3 tools/golden/card-label.py SRC ARCH COMMIT [--run URL]

SRC is the firmware tree the image was built from. The version, the boards
and the setup network are read from that tree by importing it, so the label
cannot disagree with the firmware on the card. Nothing secret goes in:
the setup network on a §5 card has no password, and on an older card the
password is per unit and never written down.

The card's boot partition is the one a Mac mounts, so a person can read
the label with ``cat /Volumes/bootfs/wqm1-card.txt``.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

# What each image boots (docs/raspberry-pi.md). A Zero W runs only the
# 32-bit card; a Pi 5 runs only the 64-bit one.
BOARDS = {
    "armhf": ("32-bit (armhf)", "Raspberry Pi Zero W, Zero 2 W, 3, 4", "Raspberry Pi 5"),
    "arm64": ("64-bit (arm64)", "Raspberry Pi Zero 2 W, 3, 4, 5", "Raspberry Pi Zero W"),
}


def setup_network(src: Path) -> str:
    """The network a fresh unit on this card raises, from the firmware itself."""
    sys.path.insert(0, str(src / "src"))
    try:
        identity = importlib.import_module("utils.identity")
    finally:
        sys.path.pop(0)
    shared = getattr(identity, "SETUP_SSID", None)
    if shared:
        return f"{shared}, no password"
    # Before install plan §5: one name per unit, with a per-unit password.
    return f"{identity.ap_name('WQM-10000')[:-4]} plus the last four of the unit, with a password"


def label(src: Path, arch: str, commit: str, run_url: str = "", now: datetime | None = None) -> str:
    image, boots, not_for = BOARDS[arch]
    version = (src / "VERSION").read_text(encoding="utf-8").strip()
    built = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "WQM-1 card",
        f"Firmware: {version}, commit {commit[:7]}",
        f"Image: {image}",
        f"Boots: {boots}",
        f"Does not boot: {not_for}",
        f"Setup network: {setup_network(src)}",
        f"Built: {built}",
    ]
    if run_url:
        lines.append(f"Build: {run_url}")
    lines.append("How to flash: docs/flash-a-card.md in the wqm-1 repo")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("src", type=Path)
    parser.add_argument("arch", choices=sorted(BOARDS))
    parser.add_argument("commit")
    parser.add_argument("--run", default="", help="URL of the build that made the card")
    args = parser.parse_args(argv)
    sys.stdout.write(label(args.src.resolve(), args.arch, args.commit, args.run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
