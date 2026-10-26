#!/usr/bin/env python3
"""
demo-unit.py — one virtual WQM-1, factory-fresh, ready for a person or a
browser bot to commission from a phone-sized browser.

    python3 scripts/demo-unit.py                 # Wi-Fi page: http://localhost:8081/setup/
    python3 scripts/demo-unit.py --reset         # wipe it back to factory state
    cloudflared tunnel --url http://localhost:8081   # give a remote bot a URL

What runs: the real firmware (``python -m main``, simulate_enabled), the
real Service Window, and the real port-80 Wi-Fi page (``utils.captive_portal``,
here on ``--portal-port``), plus a supervisor that relaunches them (systemd's
job on a real unit). The Wi-Fi page is what a phone on the setup network
sees; the Service Window is the bench page. The unit boots the way a unit
does at a well head:

* its identity came from the bench card (serial, DevEUI),
  so the wizard is the short, carded one: probes → join the owner's Wi-Fi;
* no known network is in range, so its open setup network ``BlueSignal Setup``
  is up;
* the bench network ``BlueSignal-Shop`` is still saved from the shop;
* the homeowner's network ``Smith-Home`` is in range (password printed below).

Everything network-side is the VIRTUAL radio (``utils/netsim.py``): the
laptop's own Wi-Fi is never scanned or touched. Cloud uploads go to a closed
loopback port, so readings buffer on the unit — the store-and-forward a site
with a dead uplink would show. Nothing here can reach production.

Serial is the reserved simulator id SIM-WQM1-00001: a stray record anywhere
is unmistakable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess  # nosec B404 — spawns this repo's own entry points
import sys
import time
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import yaml  # noqa: E402

from sensors.sim.unit import sim_identity, sim_serial  # noqa: E402
from utils import netsim  # noqa: E402
from utils.identity import ap_name  # noqa: E402

# A closed loopback port: every upload fails fast and the unit buffers.
DEAD_CLOUD = "http://127.0.0.1:9"
CLOUD_CLAIM_BASE = "https://cloud.bluesignal.xyz/claim/"


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--port", type=int, default=8080, help="Service Window (bench page)")
    p.add_argument("--portal-port", type=int, default=8081, help="the port-80 Wi-Fi page")
    p.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 for a LAN phone)")
    p.add_argument("--workdir", default="/tmp/wqm1-demo-unit")  # nosec B108 — a demo sandbox
    p.add_argument("--reset", action="store_true", help="wipe the unit back to factory state")
    p.add_argument("--seed", type=int, default=7)
    return p.parse_args(argv)


def build_unit(workdir: Path, port: int, seed: int) -> dict[str, Any]:
    """Write the identity file, firmware config and virtual-radio state for a
    factory-fresh carded unit. Idempotent: an existing unit is left alone."""
    workdir.mkdir(parents=True, exist_ok=True)
    serial = sim_serial(1)
    identity = workdir / "bluesignal-identity.json"
    config = workdir / "config.yaml"
    net = workdir / "netsim.json"
    if not identity.exists():
        # The setup network is open (install plan §5); the identity file's
        # ap_passphrase is a bench-card field the unit no longer reads.
        ident = sim_identity(1)
        # A claim token, as the bench script writes one — what the box label's
        # QR carries. Demo only: the reserved serial never exists in production.
        ident["claim_token"] = "demo" + "0" * 28
        identity.write_text(json.dumps(ident, indent=2))
    if not config.exists():
        cfg = {
            "simulate_enabled": True,
            "simulate_seed": seed,
            "board": "generic-linux",
            "sensor_read_s": 10,
            "sync_interval_s": 30,
            "heartbeat_s": 60,
            "gps_fix_s": 60,
            "max_retries": 1,
            "retry_delays": [0],
            "cloud_enabled": True,
            # The bench card wrote a key; this one goes nowhere.
            "api_key": "demounitplaceholderkey00000000000",
            "cloud_api_base": DEAD_CLOUD,
            "cloud_ingest_url": f"{DEAD_CLOUD}/ingestReading",
            "cloud_command_url": f"{DEAD_CLOUD}/v2/devices/{serial}/commands",
            "db_path": str(workdir / "wqm1.db"),
            "log_path": str(workdir / "wqm1.log"),
            "cmd_sock": str(workdir / "cmd.sock"),
            "service_window": {
                "port": port,
                "db_path": str(workdir / "wqm1.db"),
                "cal_path": str(workdir / "calibration.yaml"),
                "cmd_sock": str(workdir / "cmd.sock"),
                "config_path": str(config),
            },
        }
        config.write_text(yaml.safe_dump(cfg, sort_keys=False))
    if not net.exists():
        state = netsim._default_state()
        state.update(ap=True, ap_ssid=ap_name(serial), station=None)
        net.write_text(json.dumps(state, indent=2))
    ident = json.loads(identity.read_text())
    return {
        "serial": serial,
        "identity": identity,
        "config": config,
        "netsim": net,
        "ap_ssid": ap_name(serial),
        "claim_token": ident.get("claim_token"),
    }


def demo_claim_url(serial: str, token: str | None) -> str:
    """The box label's link, pointed at Cloud demo mode."""
    url = f"{CLOUD_CLAIM_BASE}{serial}?demo=1"
    return f"{url}#t={token}" if token else url


def _spawn(argv: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # nosec B603 — this repo's own entry points
        argv, cwd=str(_SRC), env=env, stdout=log.open("ab"), stderr=subprocess.STDOUT
    )


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    workdir = Path(args.workdir)
    if args.reset and workdir.exists():
        shutil.rmtree(workdir)
    unit = build_unit(workdir, args.port, args.seed)
    # The Wi-Fi page listens while this file says "up", as on a unit with
    # its setup network raised; a join writes "down: …" and it closes.
    mode = workdir / "portal.mode"
    if json.loads(unit["netsim"].read_text()).get("ap"):
        mode.write_text("up\n")
    env = dict(
        os.environ,
        PYTHONPATH=str(_SRC),
        BLUESIGNAL_IDENTITY_FILE=str(unit["identity"]),
        BLUESIGNAL_CONFIG=str(unit["config"]),
        WQM1_VIRTUAL_NET=str(unit["netsim"]),
        # One state directory for all three processes: the join's result
        # (join-state.json) is read by the page and the Service Window alike.
        BLUESIGNAL_STATE_DIR=str(workdir / "state"),
        WQM1_PORTAL_MODE_FILE=str(mode),
        WQM1_PORTAL_BIND=f"{args.host}:{args.portal_port}",
        WQM1_SCAN_CACHE=str(workdir / "scan.json"),
    )
    fw_argv = [sys.executable, "-m", "main", "--config", str(unit["config"])]
    sw_argv = [
        sys.executable,
        "-m",
        "service_window",
        "--config",
        str(unit["config"]),
        "--port",
        str(args.port),
        "--host",
        args.host,
    ]
    portal_argv = [sys.executable, "-m", "utils.captive_portal"]
    fw = _spawn(fw_argv, env, workdir / "firmware.out")
    sw = _spawn(sw_argv, env, workdir / "service_window.out")
    portal = _spawn(portal_argv, env, workdir / "portal.out")

    print(
        f"""
Virtual WQM-1 {unit["serial"]} is up (factory state).

  Wi-Fi page        http://localhost:{args.portal_port}/setup/   (the phone on the setup network)
  Bench page        http://localhost:{args.port}/setup/   (the Service Window)
  Setup network     {unit["ap_ssid"]}   open, no password   (virtual)
  Owner's Wi-Fi     {netsim.DEMO_HOME_SSID}   password {netsim.DEMO_HOME_PASSWORD}   (virtual)
  Bench network     {netsim.BENCH_SSID} — saved now, forgotten when setup finishes
  Box label link    {demo_claim_url(unit["serial"], unit["claim_token"])}

  Public URL for a remote browser bot:
      cloudflared tunnel --url http://localhost:{args.portal_port}

  Back to factory state:  python3 scripts/demo-unit.py --reset
  Files and logs:         {workdir}
Ctrl-C to stop.
""",
        flush=True,
    )

    stopping = False

    def _stop(*_a: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not stopping:
            time.sleep(1)
            # systemd's job on a real unit: finishing setup restarts the
            # firmware, and it must come back.
            if fw.poll() is not None:
                fw = _spawn(fw_argv, env, workdir / "firmware.out")
            if sw.poll() is not None:
                sw = _spawn(sw_argv, env, workdir / "service_window.out")
            if portal.poll() is not None:
                portal = _spawn(portal_argv, env, workdir / "portal.out")
    finally:
        for p in (fw, sw, portal):
            p.terminate()
        for p in (fw, sw, portal):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
