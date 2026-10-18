"""Ten-minute bench burn-in.

Run from ``scripts/burn-in.sh`` only when an empty ``burnin`` file was on
the boot partition at boot. Every 30 seconds for 10 minutes it checks that
the setup hotspot is still active, the ADC answers, the Pi is not currently
undervoltage-throttled, and the commissioning services are not restarting.
The result is ``PASS`` or ``FAIL`` with the failing checks, written where
the shell points ``WQM1_BURNIN_FILE`` (the boot partition, so a Mac can
read it with the card in a reader).

No secrets. The hotspot passphrase is not an input to this check.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DURATION_S = 600
INTERVAL_S = 30
WATCHED_SERVICES = (
    "bluesignal-wqm.service",
    "bluesignal-service-window.service",
    "bluesignal-ap-fallback.service",
    "NetworkManager.service",
)


@dataclass
class Sample:
    hotspot_active: bool
    adc_ok: bool
    undervoltage: bool
    power: str = "unknown"
    restarting: list[str] = field(default_factory=list)

    def failures(self) -> list[str]:
        found: list[str] = []
        if not self.hotspot_active:
            found.append("hotspot down")
        if not self.adc_ok:
            found.append("adc not answering")
        if self.undervoltage:
            found.append(f"undervoltage ({self.power})")
        if self.restarting:
            found.append("restarting " + ",".join(self.restarting))
        return found


def evaluate(samples: list[Sample]) -> tuple[str, str]:
    """``(PASS|FAIL, detail)``. Any bad sample fails the run."""
    if not samples:
        return "FAIL", "no samples"
    bad: list[str] = []
    for index, sample in enumerate(samples, start=1):
        reasons = sample.failures()
        if reasons:
            bad.append(f"check {index}: " + "; ".join(reasons))
    if bad:
        return "FAIL", f"{len(bad)}/{len(samples)} checks failed; " + " | ".join(bad)
    return "PASS", f"{len(samples)} checks, hotspot stayed up, adc answered, no undervoltage"


def render_result(status: str, detail: str) -> str:
    return f"BURN-IN: {status}\n{detail}\n"


def run_burnin(
    *,
    duration_s: float = DURATION_S,
    interval_s: float = INTERVAL_S,
    probe: Callable[[], Sample],
    dest: Path,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Sample until ``duration_s`` has elapsed and write the result."""
    samples: list[Sample] = []
    start = clock()
    while True:
        samples.append(probe())
        if clock() - start >= duration_s:
            break
        sleep(interval_s)
    status, detail = evaluate(samples)
    text = render_result(status, detail)
    _write(dest, text)
    return text


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o644)
    tmp.replace(path)


def parse_systemctl_show(text: str) -> tuple[int, str]:
    """``(NRestarts, ActiveState)`` from ``systemctl show`` output."""
    restarts = 0
    state = ""
    for line in text.splitlines():
        if line.startswith("NRestarts="):
            try:
                restarts = int(line.split("=", 1)[1])
            except ValueError:
                restarts = 0
        elif line.startswith("ActiveState="):
            state = line.split("=", 1)[1].strip()
    return restarts, state


def service_is_restarting(previous: int | None, restarts: int, state: str) -> bool:
    """True unless the unit is active, or its restart count rose since the last sample.

    The first sample records the count and does not treat an earlier boot
    restart as a loop. A count that climbs during the ten minutes does.
    """
    if state != "active":
        return True
    return previous is not None and restarts > previous


def adc_answers(open_adc: Callable[[], object] | None = None) -> bool:
    """True when the ADS1115 config register can be read."""

    def _open() -> object:
        from sensors.ads1115 import ADS1115

        return ADS1115()

    try:
        (open_adc or _open)()
    except Exception:  # noqa: BLE001 — a dark bus is a failed check, not a crash
        return False
    return True


def hotspot_active() -> bool:
    """True when hostapd is beaconing (``hostapd_cli status`` state=ENABLED)."""
    from utils.netctl import ap_active

    try:
        return bool(ap_active())
    except Exception:  # noqa: BLE001
        return False


def undervoltage_now(power_text: str | None = None) -> tuple[bool, str]:
    """Current undervoltage or throttle, from ``parse_throttled`` text."""
    if power_text is None:
        from diagnostics.boot_status import read_power

        power_text = read_power()
    text = power_text.strip()
    # "now undervoltage" / "now throttled,freq-capped". since-boot alone is not.
    return text.startswith("now "), text or "unknown"


class RestartTracker:
    """Remembers NRestarts across samples of one burn-in run."""

    def __init__(self) -> None:
        self._previous: dict[str, int] = {}

    def update(self, snapshots: dict[str, tuple[int, str]]) -> list[str]:
        bad: list[str] = []
        for name in WATCHED_SERVICES:
            restarts, state = snapshots.get(name, (0, "unknown"))
            if service_is_restarting(self._previous.get(name), restarts, state):
                bad.append(name)
            self._previous[name] = restarts
        return bad


def live_probe(tracker: RestartTracker | None = None) -> Sample:
    """One sample against the real unit. Tests pass their own probe."""
    from diagnostics.boot_status import _run_text

    undervoltage, power = undervoltage_now()
    snapshots: dict[str, tuple[int, str]] = {}
    for name in WATCHED_SERVICES:
        _rc, text = _run_text(
            ["systemctl", "show", name, "-p", "NRestarts", "-p", "ActiveState"],
            timeout=5,
        )
        snapshots[name] = parse_systemctl_show(text)
    owned = tracker if tracker is not None else RestartTracker()
    return Sample(
        hotspot_active=hotspot_active(),
        adc_ok=adc_answers(),
        undervoltage=undervoltage,
        power=power,
        restarting=owned.update(snapshots),
    )


def main() -> int:
    duration = float(os.environ.get("WQM1_BURNIN_DURATION", str(DURATION_S)))
    interval = float(os.environ.get("WQM1_BURNIN_INTERVAL", str(INTERVAL_S)))
    dest = Path(os.environ.get("WQM1_BURNIN_FILE", "/boot/firmware/wqm1-burnin.txt"))
    tracker = RestartTracker()
    text = run_burnin(
        duration_s=duration,
        interval_s=interval,
        probe=lambda: live_probe(tracker),
        dest=dest,
    )
    return 0 if text.startswith("BURN-IN: PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
