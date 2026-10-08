#!/usr/bin/env python3
"""
simulate-fleet.py — run N virtual WQM-1 units against a LOCAL emulator.

Two tiers (commissioning plan, Track B):

  lite  (any N, default for N > 10)  — src/sensors/sim/unit.py VirtualUnit:
        the real SamplingWorker + WQM1Database + CloudClient per unit,
        stepped by hand on a compressed clock. Proves the cloud at scale.
  full  (N ≤ 10)                     — real `python -m main --config …`
        firmware processes with simulate_enabled, each with its own Service
        Window on its own port. Proves the commissioning wizard. With
        --commission the setup wizard is driven the way an installer's phone
        drives it (site flow v2: PIN → probes → join the owner's Wi-Fi).

Every full-tier unit gets a VIRTUAL Wi-Fi radio (``utils/netsim.py``,
``WQM1_VIRTUAL_NET``): its setup hotspot is up, the bench network is saved,
and the homeowner network ``Smith-Home`` is in range. Without it, the
wizard's network step would scan — and on Join, reconnect — the Wi-Fi of the
laptop running the simulator.

The unit's cloud endpoints MUST be loopback. There is no flag to relax
that: this script pointed at production is the most damaging thing in the
repo, and the refusal lives in sensors/sim/unit.py where a test pins it.

Serials are SIM-WQM1-00001 … (utils/identity.SIM_SERIAL_PREFIX) — neither a
label nor a Pi-derived id, so a stray record is unmistakable and greppable.

API keys come from `--keys keys.json` (`{"SIM-WQM1-00001": "<api key>", …}`),
which the marketplace's scripts/seed-fleet.cjs writes after claiming the
serials in the emulator. Without one every unit reports with a placeholder
key and the ingest answers 401 — the buffer then proves store-and-forward
instead, which is also a valid run.

Examples
  python3 scripts/simulate-fleet.py --count 100 --cycles 43200 \\
      --cloud-api-base http://localhost:5001/waterquality-trading/us-central1/app \\
      --ingest-url    http://localhost:5001/waterquality-trading/us-central1/ingestReading \\
      --keys /tmp/fleet-keys.json --faults "tds:no_conduction@400,flow:reset@20000"

  # Drive the irrigation hold: relay 3 holds when turbidity >= 8 NTU; the
  # scripted spike (120 NTU for 10 cycles from cycle 30) trips it, and the
  # release delay passes on the compressed clock.
  python3 scripts/simulate-fleet.py --count 1 --cycles 120 --faults "turbidity:spike@30" \
      --hold-relay 3 --hold-turbidity-ntu 8 --hold-release-min 10 --cloud-api-base … --ingest-url …

  python3 scripts/simulate-fleet.py --count 10 --tier full --commission --workdir /tmp/fleet \\
      --cloud-api-base http://localhost:5001/... --ingest-url http://localhost:5001/... --keys …
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 — spawns this repo's own firmware entry points
import sys
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import yaml  # noqa: E402

from sensors.sim.unit import (  # noqa: E402
    NotAnEmulator,
    VirtualUnit,
    assert_emulator_endpoint,
    sim_identity,
    sim_serial,
)
from utils.config import Settings  # noqa: E402

PLACEHOLDER_KEY = "simulatedapikeyplaceholder00000000"
FACTORY_PIN = "1234"
SIM_PIN = "2468"


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--tier", choices=("auto", "lite", "full"), default="auto")
    p.add_argument("--cycles", type=int, default=1440, help="sampling cycles per unit (lite)")
    p.add_argument("--interval-s", type=int, default=60, help="simulated seconds per cycle")
    p.add_argument("--sync-every", type=int, default=50)
    p.add_argument("--cloud-api-base", required=True)
    p.add_argument("--ingest-url", required=True)
    p.add_argument("--keys", help="JSON map serial -> api key from seed-fleet.cjs")
    p.add_argument("--faults", default="", help="channel:kind[@cycle],… applied to EVERY unit")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--workdir", default=None, help="where per-unit files go (default: temp)")
    p.add_argument("--commission", action="store_true", help="full tier: drive the setup wizard")
    p.add_argument("--base-port", type=int, default=8100)
    p.add_argument(
        "--run-s", type=int, default=60, help="full tier: seconds to leave units running"
    )
    p.add_argument(
        "--hold-relay", type=int, default=0, help="irrigation hold relay 1-4 (0 = hold off)"
    )
    p.add_argument("--hold-turbidity-ntu", type=float, default=8.0)
    p.add_argument("--hold-release-min", type=int, default=10)
    p.add_argument("--json", action="store_true", help="print a machine-readable summary")
    return p.parse_args(argv)


def _keys(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise SystemExit("--keys must be a JSON object of serial -> api key")
    return {str(k): str(v) for k, v in data.items()}


def _hold_values(args: argparse.Namespace) -> dict[str, Any]:
    """The irrigation-hold settings a run enables (empty when --hold-relay 0)."""
    if not args.hold_relay:
        return {}
    return {
        "irrigation_hold_enabled": True,
        "irrigation_hold_relay": args.hold_relay,
        "irrigation_hold_turbidity_ntu": args.hold_turbidity_ntu,
        "irrigation_hold_release_min": args.hold_release_min,
    }


def _settings(
    interval_s: int, faults: str, seed: int, hold: dict[str, Any] | None = None
) -> Settings:
    s = Settings()
    for key, value in (hold or {}).items():
        setattr(s, key, value)
    s.simulate_enabled = True
    s.simulate_faults = faults
    s.simulate_seed = seed
    s.sensor_read_s = interval_s
    s.flow_pulse_enabled = True
    s.cloud_enabled = True
    s.board = "generic-linux"
    return s


# ---------------------------------------------------------------------------
# lite
# ---------------------------------------------------------------------------


def run_lite(args: argparse.Namespace, workdir: Path, keys: dict[str, str]) -> dict[str, Any]:
    units: list[VirtualUnit] = []
    for i in range(1, args.count + 1):
        serial = sim_serial(i)
        s = _settings(args.interval_s, args.faults, args.seed + i, _hold_values(args))
        units.append(
            VirtualUnit(
                i,
                db_path=str(workdir / f"{serial}.db"),
                cloud_api_base=args.cloud_api_base,
                ingest_url=args.ingest_url,
                api_key=keys.get(serial, PLACEHOLDER_KEY),
                settings=s,
                faults=args.faults,
            )
        )
    started = time.monotonic()
    for u in units:
        u.run(args.cycles, sync_every=args.sync_every)
        u.heartbeat()
    elapsed = time.monotonic() - started
    summary = {
        "tier": "lite",
        "count": args.count,
        "cycles": args.cycles,
        "elapsed_s": round(elapsed, 2),
        "units": [
            {
                "serial": u.serial,
                "dev_eui": u.dev_eui,
                "samples": u.samples,
                "uploaded": u.uploaded,
                "pending": u.pending(),
                "clock_jumps": u.clock.jumped_at,
                "irrigation_hold": u.hold.payload(),
            }
            for u in units
        ],
    }
    for u in units:
        u.close()
    return summary


# ---------------------------------------------------------------------------
# full
# ---------------------------------------------------------------------------


def _unit_files(
    workdir: Path, i: int, args: argparse.Namespace, key: str, port: int
) -> dict[str, Path]:
    serial = sim_serial(i)
    d = workdir / serial
    d.mkdir(parents=True, exist_ok=True)
    identity = d / "bluesignal-identity.json"
    identity.write_text(json.dumps(sim_identity(i), indent=2))
    cfg = {
        "simulate_enabled": True,
        "simulate_seed": args.seed + i,
        "simulate_faults": args.faults,
        "board": "generic-linux",
        "sensor_read_s": max(5, min(args.interval_s, 60)),
        # Schema minimums (utils/config.py): sync 30 s, heartbeat 60 s, GPS 60 s.
        "sync_interval_s": 30,
        "heartbeat_s": 60,
        "gps_fix_s": 60,
        "max_retries": 1,
        "retry_delays": [0],
        "flow_pulse_enabled": True,
        **_hold_values(args),
        "cloud_enabled": True,
        "api_key": key,
        "cloud_api_base": args.cloud_api_base,
        "cloud_ingest_url": args.ingest_url,
        "cloud_command_url": f"{args.cloud_api_base.rstrip('/')}/v2/devices/{serial}/commands",
        "db_path": str(d / "wqm1.db"),
        "log_path": str(d / "wqm1.log"),
        "cmd_sock": str(d / "cmd.sock"),
        "service_window": {
            "port": port,
            "pin": FACTORY_PIN,
            "db_path": str(d / "wqm1.db"),
            "cal_path": str(d / "calibration.yaml"),
            "cmd_sock": str(d / "cmd.sock"),
            "config_path": str(d / "config.yaml"),
        },
    }
    config = d / "config.yaml"
    config.write_text(yaml.safe_dump(cfg, sort_keys=False))
    netsim_state = d / "netsim.json"
    netsim_state.write_text(json.dumps(virtual_site_state(serial), indent=2))
    return {"dir": d, "identity": identity, "config": config, "netsim": netsim_state}


def virtual_site_state(serial: str) -> dict[str, Any]:
    """A unit just powered on at a site: no known network in range, so its
    setup hotspot is up; the bench network is still saved from the shop."""
    from utils import netsim
    from utils.identity import ap_name

    state = netsim._default_state()
    state.update(ap=True, ap_ssid=ap_name(serial), station=None)
    return state


class _Http:
    """Cookie-carrying client for one Service Window."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def get(self, path: str) -> tuple[int, str]:
        with self.opener.open(self.base + path, timeout=10) as r:  # nosec B310 — loopback only
            return r.status, r.read().decode()

    def post(self, path: str, form: dict[str, str]) -> tuple[int, str]:
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        with self.opener.open(req, timeout=10) as r:  # nosec B310 — loopback only
            return r.status, r.read().decode()


def commission_over_http(base: str, api_key: str, pin: str = SIM_PIN) -> list[str]:
    """Walk the setup wizard the way an installer's phone does (site flow v2).

    A full-tier unit is carded (identity file + cloud key in its config), so
    the walk is welcome → pin → sensors → network, and the JOIN finishes
    setup. Returns the step list completed, in order. ``api_key`` is kept for
    the uncarded case: if the wizard asks for a key, it is given.
    """
    import urllib.parse  # noqa: F401 — used by _Http.post

    from utils import netsim

    c = _Http(base)
    done: list[str] = []
    # On a virtual unit the browser counts as "on the hotspot", so first setup
    # needs no PIN; logging in with the factory PIN still works and is what a
    # LAN visit does.
    c.post("/login", {"pin": FACTORY_PIN})
    _, welcome = c.get("/setup/")
    done.append("welcome")
    carded = "already knows who it" in welcome
    c.post("/setup/pin", {"pin": pin, "pin_confirm": pin})
    done.append("pin")
    if not carded:
        c.get("/setup/identity")
        done.append("identity")
        c.post("/setup/cloud", {"api_key": api_key})
        done.append("cloud")
    c.post(
        "/setup/sensors",
        {
            "ph_enabled": "on",
            "tds_enabled": "on",
            "turbidity_enabled": "on",
            "temperature_enabled": "on",
            "flow_pulse_enabled": "on",
        },
    )
    done.append("sensors")
    _, joined = c.post(
        "/setup/network",
        {
            "action": "join",
            "ssid": netsim.DEMO_HOME_SSID,
            "password": netsim.DEMO_HOME_PASSWORD,
            "gps_enabled": "on",
        },
    )
    if "Setup is finished" in joined:
        done.append("network")
    return done


def run_full(args: argparse.Namespace, workdir: Path, keys: dict[str, str]) -> dict[str, Any]:
    if args.count > 10:
        raise SystemExit("full tier is for N ≤ 10 (real processes); use --tier lite above that")
    procs: list[subprocess.Popen[bytes]] = []
    units: list[dict[str, Any]] = []
    env = dict(os.environ, PYTHONPATH=str(_SRC))
    try:
        for i in range(1, args.count + 1):
            serial = sim_serial(i)
            port = args.base_port + i
            files = _unit_files(workdir, i, args, keys.get(serial, PLACEHOLDER_KEY), port)
            unit_env = dict(
                env,
                BLUESIGNAL_IDENTITY_FILE=str(files["identity"]),
                BLUESIGNAL_CONFIG=str(files["config"]),
                WQM1_VIRTUAL_NET=str(files["netsim"]),
            )
            fw = subprocess.Popen(  # nosec B603 — our own entry point, loopback config
                [sys.executable, "-m", "main", "--config", str(files["config"])],
                cwd=str(_SRC),
                env=unit_env,
                stdout=(files["dir"] / "firmware.out").open("wb"),
                stderr=subprocess.STDOUT,
            )
            sw = subprocess.Popen(  # nosec B603
                [
                    sys.executable,
                    "-m",
                    "service_window",
                    "--config",
                    str(files["config"]),
                    "--port",
                    str(port),
                ],
                cwd=str(_SRC),
                env=unit_env,
                stdout=(files["dir"] / "service_window.out").open("wb"),
                stderr=subprocess.STDOUT,
            )
            procs += [fw, sw]
            units.append({"serial": serial, "port": port, "dir": str(files["dir"]), "steps": []})

        time.sleep(3)  # let the Service Windows bind
        if args.commission:
            for u in units:
                u["steps"] = commission_over_http(
                    f"http://127.0.0.1:{u['port']}", keys.get(u["serial"], PLACEHOLDER_KEY)
                )
        time.sleep(args.run_s)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    for u in units:
        cfg = yaml.safe_load(Path(u["dir"], "config.yaml").read_text()) or {}
        u["setup_completed"] = bool((cfg.get("service_window") or {}).get("setup_completed"))
        u["pin_changed"] = str((cfg.get("service_window") or {}).get("pin")) != FACTORY_PIN
        net = json.loads(Path(u["dir"], "netsim.json").read_text())
        u["wifi"] = net.get("station")
        u["saved_wifi"] = net.get("saved")
    return {"tier": "full", "count": args.count, "units": units}


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        assert_emulator_endpoint(args.cloud_api_base)
        assert_emulator_endpoint(args.ingest_url)
    except NotAnEmulator as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    tier = args.tier if args.tier != "auto" else ("full" if args.count <= 10 else "lite")
    workdir = Path(args.workdir) if args.workdir else Path(f"/tmp/wqm1-fleet-{int(time.time())}")  # nosec B108
    workdir.mkdir(parents=True, exist_ok=True)
    keys = _keys(args.keys)
    summary = run_full(args, workdir, keys) if tier == "full" else run_lite(args, workdir, keys)
    summary["workdir"] = str(workdir)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{summary['tier']} tier: {summary['count']} unit(s) in {workdir}")
        for u in summary["units"]:
            print("  ", json.dumps(u))
    return 0


if __name__ == "__main__":
    sys.exit(main())
