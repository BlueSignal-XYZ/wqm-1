#!/usr/bin/env python3
"""Write the setup-AP failure clue onto the boot partition.

Run by ``wqm1-apfail.service`` when ``bluesignal-ap-fallback.service``
enters the failed state. The journal may not survive a power cut; the
FAT boot partition does. Passphrases are stripped before the write.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from diagnostics.ap_fail import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
