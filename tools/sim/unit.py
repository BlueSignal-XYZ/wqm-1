#!/usr/bin/env python3
"""The unit's processes for the commissioning simulator.

``commission.py`` runs these inside the unit's network namespace, with the
fake radio tools first on ``PATH`` and every ``WQM1_*`` path pointed into the
sim directory. The code under test is the real code: ``utils.join_service``
as root, ``utils.captive_portal`` as an unprivileged user, as their systemd
units run them on a Pi.

Two seams are set here, both for things this machine cannot answer:

* the radio's bands (no ``/proc/device-tree/model`` in a container):
  ``WQM1_SIM_RADIO=dual`` is a Pi 3B+/4B radio, anything else a 2.4 GHz-only
  one (Zero W, Zero 2 W, 3B);
* where systemd units are written (``ensure_ap_units``), so nothing lands in
  the machine's own ``/etc/systemd/system``.

    python3 tools/sim/unit.py start-ap | join-service | portal
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def _seams() -> None:
    from utils import netctl, wifi_radio

    dual = os.environ.get("WQM1_SIM_RADIO") == "dual"
    wifi_radio._CACHED_BANDS = frozenset({"2.4", "5"}) if dual else frozenset({"2.4"})
    sim = Path(os.environ["WQM1_SIM_DIR"])
    netctl.SYSTEMD_DIR = sim / "systemd"


def main(argv: list[str]) -> int:
    import logging

    _seams()
    what = argv[1] if len(argv) > 1 else ""
    if what == "start-ap":
        fmt = "%(asctime)s %(levelname)s [start-ap] %(message)s"
        logging.basicConfig(level=logging.INFO, format=fmt)
        from utils import netctl
        from utils.identity import SETUP_SSID

        result = netctl.start_ap(SETUP_SSID)
        netctl.refresh_scan_cache()
        print("ok" if result.get("ok") else f"failed: {result.get('error')}")
        return 0 if result.get("ok") else 1
    if what == "join-service":
        from utils import join_service

        return join_service.main()
    if what == "portal":
        from utils import captive_portal

        return captive_portal.main()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
