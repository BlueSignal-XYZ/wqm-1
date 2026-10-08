#!/usr/bin/env python3
"""
Model the WQM-1's electrical load across a day.

    python3 scripts/load-profile.py                 # table, using config defaults
    python3 scripts/load-profile.py --config /etc/bluesignal/config.yaml
    python3 scripts/load-profile.py --json          # machine-readable, for charting

───────────────────────────────────────────────────────────────────────────────
WHAT THIS IS, AND — MORE IMPORTANTLY — WHAT IT IS NOT
───────────────────────────────────────────────────────────────────────────────
The DUTY CYCLES here are facts: they are read from the same `Settings` the
firmware runs on, so `sensor_read_s`, `lora_tx_s`, `command_poll_s` and the
rest are whatever that unit is actually configured to do.

The CURRENTS are not facts. They are datasheet-typical figures for the parts on
the board, and no one has put a meter on a WQM-1. They are marked `measured=
False` and the summary says so on every run.

That distinction is the whole point of the file. On 2026-08-21 a pack ran flat,
a 66.6 °C reading nearby got promoted from coincidence to cause, and a
production threshold moved on the strength of it. A model whose assumptions are
labelled cannot do that to you: when a number here disagrees with reality, the
answer is to measure that one component and set `measured=True`, not to argue.

**How to make this real, for about $15 and an afternoon:** put an inline DC
power meter on the pack feed. Log it for 24 h. The total tells you whether the
bottom line below is right; watching it while you `systemctl stop
bluesignal-wqm` tells you how much is the platform versus the firmware, which
is the single most useful split for deciding what to buy.

───────────────────────────────────────────────────────────────────────────────
THE STRUCTURE THE MODEL EXPOSES
───────────────────────────────────────────────────────────────────────────────
Loads fall into three kinds, and they respond to completely different fixes:

  · CONTINUOUS — the SoC, the regulators, anything that is simply on. Duty
    cycling the firmware does nothing to these. Only different hardware, or
    sleeping the whole board, moves them.
  · POLLED — work on a fixed interval. These scale directly with config, and
    they are the only things a software change can actually reduce.
  · KEEP-ALIVE — the subtle one. A poll fast enough to prevent the Wi-Fi radio
    entering power save costs far more than the poll itself, because it holds
    a ~100 mW radio awake around the clock to move a few hundred bytes.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SECONDS_PER_DAY = 86_400

# The pack feeds a DC-DC converter; everything below is referred to the 5 V rail
# the Pi and HAT actually run on. Convert to pack amp-hours at the end.
RAIL_V = 5.0
# Conversion efficiency, pack -> 5 V rail. A small buck at light load is not
# the 95% its datasheet headline suggests.
DCDC_EFFICIENCY = 0.85
PACK_NOMINAL_V = 24.0


@dataclass
class Load:
    """One consumer.

    `idle_ma` is drawn whenever the unit is powered. `active_ma` is the ADDER
    while the thing is working, for `active_s` per event. A purely continuous
    load leaves active_ma at zero.
    """

    name: str
    kind: str  # continuous | polled | keepalive
    idle_ma: float
    active_ma: float = 0.0
    active_s: float = 0.0
    events_per_day: float = 0.0
    measured: bool = False
    note: str = ""
    source: str = ""

    def idle_wh(self) -> float:
        return self.idle_ma / 1000.0 * RAIL_V * 24.0

    def active_wh(self) -> float:
        seconds = self.active_s * self.events_per_day
        return self.active_ma / 1000.0 * RAIL_V * (seconds / 3600.0)

    def daily_wh(self) -> float:
        return self.idle_wh() + self.active_wh()

    def duty(self) -> float:
        return min(1.0, self.active_s * self.events_per_day / SECONDS_PER_DAY)


@dataclass
class Profile:
    loads: list[Load] = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    def daily_wh(self) -> float:
        return sum(load.daily_wh() for load in self.loads)

    def average_w(self) -> float:
        return self.daily_wh() / 24.0

    def pack_ah_per_day(self) -> float:
        return self.daily_wh() / DCDC_EFFICIENCY / PACK_NOMINAL_V

    def by_kind(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for load in self.loads:
            out[load.kind] = out.get(load.kind, 0.0) + load.daily_wh()
        return out


def load_settings(config_path: str | None) -> dict:
    """The duty cycles, from the same schema the firmware runs on."""
    from utils.config import Settings

    settings = Settings()
    values = {
        "sensor_read_s": settings.sensor_read_s,
        "lora_tx_s": settings.lora_tx_s,
        "gps_fix_s": settings.gps_fix_s,
        "gps_fix_timeout_s": settings.gps_fix_timeout_s,
        "command_poll_s": settings.command_poll_s,
        "sync_interval_s": settings.sync_interval_s,
        "heartbeat_s": settings.heartbeat_s,
    }
    if config_path:
        import yaml

        raw = yaml.safe_load(Path(config_path).read_text()) or {}
        for key in values:
            if isinstance(raw.get(key), int):
                values[key] = raw[key]
    return values


def build(settings: dict) -> Profile:
    per_day = lambda interval: SECONDS_PER_DAY / max(1, interval)  # noqa: E731

    loads = [
        # ── Continuous ──────────────────────────────────────────────────────
        Load(
            name="Pi Zero 2 W (idle)",
            kind="continuous",
            idle_ma=120,
            measured=False,
            source="typical for Zero 2 W, headless, Wi-Fi associated",
            note=(
                "The floor. No firmware change touches this — only different "
                "compute, or genuinely sleeping the board between duties, "
                "which this unit cannot do while it must stay reachable."
            ),
        ),
        Load(
            name="HAT quiescent (ADS1115, LM324, CD4060, regulators)",
            kind="continuous",
            idle_ma=12,
            measured=False,
            source="sum of datasheet quiescent figures",
        ),
        Load(
            name="Status LEDs",
            kind="continuous",
            idle_ma=6,
            measured=False,
            source="one or two indicators lit",
        ),
        # ── Keep-alive ──────────────────────────────────────────────────────
        Load(
            name=f"Wi-Fi held awake by command poll (every {settings['command_poll_s']}s)",
            kind="keepalive",
            # The poll's own bytes are negligible. What costs is that a request
            # every few seconds never lets the radio reach a deep power-save
            # state, so it idles associated around the clock.
            idle_ma=45 if settings["command_poll_s"] <= 30 else 8,
            active_ma=60,
            active_s=0.35,
            events_per_day=per_day(settings["command_poll_s"]),
            measured=False,
            source="Wi-Fi associated-idle vs power-save delta, Zero 2 W",
            note=(
                f"{per_day(settings['command_poll_s']):,.0f} HTTP requests a day. "
                "The threshold in this model is 30s: poll slower than that and "
                "the radio can sleep between requests, which is worth far more "
                "than the requests cost. THIS IS THE ASSUMPTION MOST WORTH "
                "MEASURING — it is the difference between a big lever and none."
            ),
        ),
        # ── Polled ──────────────────────────────────────────────────────────
        Load(
            name=f"Sensor read (every {settings['sensor_read_s']}s)",
            kind="polled",
            idle_ma=0,
            active_ma=25,
            active_s=2.0,
            events_per_day=per_day(settings["sensor_read_s"]),
            measured=False,
            source="ADC conversions + CPU wake; DS18B20 dominates the 2s",
        ),
        Load(
            name=f"LoRa uplink (every {settings['lora_tx_s']}s)",
            kind="polled",
            idle_ma=0,
            active_ma=120,
            active_s=0.15,
            events_per_day=per_day(settings["lora_tx_s"]),
            measured=False,
            source="SX1262 TX at +22 dBm, SF7-ish airtime",
            note="Loud but brief. Bursts this short cost almost nothing per day.",
        ),
        Load(
            name=f"Cloud sync (every {settings['sync_interval_s']}s)",
            kind="polled",
            idle_ma=0,
            active_ma=70,
            active_s=3.0,
            events_per_day=per_day(settings["sync_interval_s"]),
            measured=False,
            source="TLS handshake + batch upload",
        ),
        Load(
            name=f"Heartbeat (every {settings['heartbeat_s']}s)",
            kind="polled",
            idle_ma=0,
            active_ma=70,
            active_s=1.5,
            events_per_day=per_day(settings["heartbeat_s"]),
            measured=False,
            source="TLS handshake + small POST",
        ),
        Load(
            name=f"GPS fix (every {settings['gps_fix_s'] / 3600:.0f}h)",
            kind="polled",
            # Backup-mode leakage between fixes, once the module is actually
            # slept. Before the 2026-08-21 change this was ~11 mA CONTINUOUS
            # tracking, i.e. ~1.3 Wh/day rather than ~0.02.
            idle_ma=0.02,
            active_ma=35,
            active_s=settings["gps_fix_timeout_s"],
            events_per_day=per_day(settings["gps_fix_s"]),
            measured=False,
            source="u-blox acquiring vs backup",
            note=(
                "Was every 600s and never slept. The unit is bolted to a "
                "structure; the coordinate cannot change."
            ),
        ),
    ]
    return Profile(loads=loads, settings=settings)


# ── Generation, for comparing against the load ──────────────────────────────
#
# A deliberately simple clear-sky model: a half-sine across the daylight window,
# scaled so its integral equals panel_w x peak_sun_hours x system_efficiency.
# It is not a solar simulator and does not try to be — its job is to put the
# SHAPE of generation next to the shape of load, and the shape is the whole
# argument. Real output on any given day is lower and lumpier.
#
# Sunrise/sunset are Lago Vista, TX (30.4 N).
MONTHS = {
    "June": {"psh": 6.0, "sunrise": 6.5, "sunset": 20.5},
    "March": {"psh": 4.8, "sunrise": 7.5, "sunset": 19.6},
    "December": {"psh": 3.4, "sunrise": 7.3, "sunset": 17.6},
}
SYSTEM_EFFICIENCY = 0.70


def generation_curve(panel_w: float, month: str) -> list[float]:
    """Hourly average watts delivered to the load/pack, hour 0..23."""
    import math

    m = MONTHS[month]
    daylight = m["sunset"] - m["sunrise"]
    daily_wh = panel_w * m["psh"] * SYSTEM_EFFICIENCY
    # Integral of sin over [0, pi] is 2, so a half-sine of peak P over `daylight`
    # hours delivers P * daylight * 2 / pi watt-hours.
    peak_w = daily_wh * math.pi / (2 * daylight)
    out = []
    for hour in range(24):
        centre = hour + 0.5
        if centre < m["sunrise"] or centre > m["sunset"]:
            out.append(0.0)
            continue
        frac = (centre - m["sunrise"]) / daylight
        out.append(round(peak_w * math.sin(math.pi * frac), 3))
    return out


def battery_trace(profile: Profile, panel_w: float, month: str) -> list[float]:
    """Cumulative Wh into/out of the pack across one day, starting at zero.

    The end value is the day's net. Negative means the pack finished lower than
    it started, which repeated over enough days is exactly how a unit goes dark
    with nothing in any log to explain it.
    """
    load_w = profile.average_w()
    gen = generation_curve(panel_w, month)
    running = 0.0
    out = []
    for hour in range(24):
        running += gen[hour] - load_w
        out.append(round(running, 3))
    return out


def hourly_watts(profile: Profile) -> list[float]:
    """Average power per hour of day.

    Flat, deliberately: every duty cycle in this firmware is a fixed interval,
    so there is no diurnal shape to model. That is itself the finding — the
    load is a constant, and a constant load against a solar supply is the
    hardest case there is, because consumption does not fall at night when
    generation stops.
    """
    return [profile.average_w()] * 24


def render_table(profile: Profile) -> str:
    lines = []
    total = profile.daily_wh()
    lines.append(f"{'Load':<52} {'kind':<11} {'Wh/day':>8} {'share':>7}")
    lines.append("-" * 82)
    for load in sorted(profile.loads, key=lambda x: -x.daily_wh()):
        share = 100 * load.daily_wh() / total if total else 0
        lines.append(f"{load.name:<52} {load.kind:<11} {load.daily_wh():>8.2f} {share:>6.1f}%")
    lines.append("-" * 82)
    lines.append(f"{'TOTAL':<52} {'':<11} {total:>8.2f} {100.0:>6.1f}%")
    lines.append("")
    lines.append(f"Average draw          {profile.average_w():.2f} W continuous")
    lines.append(
        f"Pack draw             {profile.pack_ah_per_day():.2f} Ah/day at {PACK_NOMINAL_V:.0f} V"
    )
    lines.append("")
    by_kind = profile.by_kind()
    for kind in ("continuous", "keepalive", "polled"):
        wh = by_kind.get(kind, 0.0)
        lines.append(f"  {kind:<12} {wh:>6.2f} Wh/day  ({100 * wh / total:.0f}%)")
    lines.append("")
    lines.append("EVERY CURRENT ABOVE IS A DATASHEET ESTIMATE, NOT A MEASUREMENT.")
    lines.append("Put a meter on the pack feed before sizing anything from it.")
    return "\n".join(lines)


def sizing(profile: Profile) -> str:
    """Panel and pack, sized for the worst month rather than today."""
    daily_wh = profile.daily_wh()
    rows = []
    # Peak sun hours, Austin/Lago Vista. December is the binding constraint and
    # is roughly HALF of June — sizing to a summer figure is the classic way to
    # build something that dies in January.
    for month, psh in (("June", 6.0), ("March", 4.8), ("December", 3.4)):
        # System efficiency: panel heat derate, MPPT, charge, wiring.
        break_even = daily_wh / (psh * 0.70)
        rows.append(f"  {month:<9} {psh:>4.1f} peak-sun-h   break-even {break_even:>5.1f} W")
    lines = ["Panel sizing", *rows, ""]
    december = daily_wh / (3.4 * 0.70)
    lines.append(f"  Break-even in December is {december:.0f} W. That is the size at which the")
    lines.append("  unit survives an AVERAGE December day and dies on a cloudy one.")
    lines.append(f"  Practical target is 2-3x: {2 * december:.0f}-{3 * december:.0f} W.")
    lines.append("")
    for days in (3, 5):
        # NMC, 80% usable depth of discharge.
        wh = daily_wh * days / 0.8
        lines.append(
            f"  {days}-day autonomy needs {wh:.0f} Wh usable "
            f"= {wh / PACK_NOMINAL_V:.1f} Ah at {PACK_NOMINAL_V:.0f} V"
        )
    return "\n".join(lines)


def thermal(profile: Profile) -> str:
    """Every watt drawn inside a sealed box is a watt of heat inside that box.

    This is not an analogy. Essentially all of the electrical power consumed by
    the electronics is dissipated as heat within the enclosure — the fraction
    leaving as RF is negligible — so the load model IS a heat model, and the
    two problems have one lever.

    The enclosure's thermal resistance is the missing number. A small sealed
    plastic box with no ventilation typically sits somewhere around 8-15 °C/W;
    with vents or a metal wall it falls sharply. Nobody has measured this one,
    so the range is given rather than a figure — but the SHAPE of the answer
    does not depend on picking a value: reducing load cools the box, and in a
    sealed enclosure there is no other passive way to do it.
    """
    w = profile.average_w()
    lines = [
        "Thermal consequence",
        "",
        f"  {w:.2f} W of load is {w:.2f} W of heat, released inside the enclosure.",
        "",
    ]
    for r_th in (8, 12, 15):
        lines.append(f"  At {r_th:>2} °C/W enclosure resistance: {w * r_th:>5.1f} °C above ambient")
    lines.append("")
    lines.append("  On a 39 °C (103 °F) afternoon that is the electronics' own contribution,")
    lines.append("  BEFORE any solar gain on the box. It compounds twice over: heat lowers")
    lines.append("  panel output (~0.4%/°C above 25 °C cell temp) and accelerates pack")
    lines.append("  degradation, so a hot box generates less and stores worse.")
    lines.append("")
    keepalive = profile.by_kind().get("keepalive", 0.0) / 24.0
    lines.append(f"  The {keepalive:.2f} W keep-alive term is therefore worth roughly")
    lines.append(
        f"  {keepalive * 8:.1f}-{keepalive * 15:.1f} °C of enclosure temperature on its own."
    )
    lines.append("  Shade and ventilation are still the bigger levers — but load reduction")
    lines.append("  and cooling are the same action here, not two competing projects.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="read duty cycles from a unit's config.yaml")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    profile = build(load_settings(args.config))

    if args.json:
        print(
            json.dumps(
                {
                    "settings": profile.settings,
                    "dailyWh": round(profile.daily_wh(), 3),
                    "averageW": round(profile.average_w(), 3),
                    "packAhPerDay": round(profile.pack_ah_per_day(), 3),
                    "byKind": {k: round(v, 3) for k, v in profile.by_kind().items()},
                    "hourlyW": [round(w, 3) for w in hourly_watts(profile)],
                    "months": MONTHS,
                    "generation": {
                        f"{panel}W-{month}": generation_curve(panel, month)
                        for panel in (10, 20, 30)
                        for month in MONTHS
                    },
                    "battery": {
                        f"{panel}W-{month}": battery_trace(profile, panel, month)
                        for panel in (10, 20, 30)
                        for month in MONTHS
                    },
                    "loads": [
                        {
                            "name": load.name,
                            "kind": load.kind,
                            "dailyWh": round(load.daily_wh(), 3),
                            "idleWh": round(load.idle_wh(), 3),
                            "activeWh": round(load.active_wh(), 3),
                            "eventsPerDay": round(load.events_per_day, 1),
                            "dutyPct": round(100 * load.duty(), 3),
                            "measured": load.measured,
                            "source": load.source,
                            "note": load.note,
                        }
                        for load in sorted(profile.loads, key=lambda x: -x.daily_wh())
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(render_table(profile))
    print()
    print(sizing(profile))
    print()
    print(thermal(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
