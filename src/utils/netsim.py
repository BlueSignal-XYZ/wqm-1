"""
A virtual Wi-Fi radio for simulated units — the same contract as
``utils.netctl``, with no radio and no NetworkManager.

Why it exists: the full-tier simulator (``scripts/simulate-fleet.py``) and the
browser demo (``scripts/demo-unit.py``) run the real Service Window on a
laptop. Without this module the setup wizard's network step would shell
``nmcli`` on that laptop — scanning the developer's own Wi-Fi and, on a
"Join", actually reconnecting their machine. A simulated unit must never be
able to touch a real link, so the switch is an environment variable the
simulator sets and real units never carry:

    WQM1_VIRTUAL_NET=/path/to/netsim.json

The state lives in that JSON file (not in process memory) because the
firmware and the Service Window are separate processes and must agree on
what is connected. A missing file starts the demo scene: the setup AP up,
the bench network saved but out of range, and a homeowner's network in range
whose password is ``DEMO_HOME_PASSWORD``.

Every function returns the same shapes ``netctl`` returns and never raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENV_VAR = "WQM1_VIRTUAL_NET"

# The bench network every golden-image unit remembers. The wizard must forget
# it when a site network is joined — the demo shows that happening.
BENCH_SSID = "BlueSignal-Shop"
DEMO_HOME_SSID = "Smith-Home"
# A demo credential for a network that does not exist. Named so a scanner
# reads it as what it is.
DEMO_HOME_PASSWORD = "riverstone42"  # nosec B105 — virtual network, not a credential

_DEFAULT_NETWORKS: list[dict[str, Any]] = [
    {"ssid": DEMO_HOME_SSID, "signal": 78, "secured": True, "password": DEMO_HOME_PASSWORD},
    {"ssid": "Smith-Home-Guest", "signal": 61, "secured": True, "password": "guestpass2026"},  # nosec B105
    {"ssid": "Installer iPhone", "signal": 54, "secured": True, "password": "hotspot1234"},  # nosec B105
    {"ssid": "xfinitywifi", "signal": 31, "secured": False, "password": ""},  # nosec B105
]


def state_path() -> Path | None:
    raw = os.environ.get(ENV_VAR, "").strip()
    return Path(raw) if raw else None


def is_virtual() -> bool:
    return state_path() is not None


def _default_state() -> dict[str, Any]:
    return {
        "ap": False,
        "ap_ssid": None,
        "station": None,
        "saved": [BENCH_SSID],
        "networks": [dict(n) for n in _DEFAULT_NETWORKS],
        "joins": [],
    }


def read_state() -> dict[str, Any]:
    path = state_path()
    if path is None or not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return _default_state()
    base = _default_state()
    base.update({k: v for k, v in data.items() if k in base})
    return base


def _write(state: dict[str, Any]) -> None:
    path = state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)
    except OSError:
        pass


def station_connected() -> bool:
    return bool(read_state().get("station"))


def current_ssid() -> str | None:
    st = read_state()
    return st.get("station") or None


def rssi_dbm() -> int | None:
    st = read_state()
    ssid = st.get("station")
    if not ssid:
        return None
    for n in st["networks"]:
        if n["ssid"] == ssid:
            # Map 0–100 % onto roughly -95…-35 dBm, the scale netinfo grades.
            return int(-95 + 0.6 * int(n.get("signal", 0)))
    return None


def ap_active() -> bool:
    return bool(read_state().get("ap"))


def start_ap(ssid: str, passphrase: str) -> dict[str, Any]:
    if not passphrase or len(passphrase) < 8:
        return {"ok": False, "error": "passphrase must be at least 8 characters"}
    st = read_state()
    already = bool(st["ap"])
    st["ap"] = True
    st["ap_ssid"] = ssid
    st["station"] = None
    _write(st)
    out: dict[str, Any] = {"ok": True, "ssid": ssid, "address": "192.168.4.1"}
    if already:
        out["already"] = True
    return out


def stop_ap() -> dict[str, Any]:
    st = read_state()
    st["ap"] = False
    _write(st)
    return {"ok": True}


def scan_networks() -> list[dict[str, Any]]:
    st = read_state()
    nets = [
        {"ssid": n["ssid"], "signal": int(n.get("signal", 0)), "secured": bool(n.get("secured"))}
        for n in st["networks"]
    ]
    return sorted(nets, key=lambda n: -n["signal"])


def join_network(
    ssid: str,
    password: str | None,
    ap_ssid: str | None = None,
    ap_passphrase: str | None = None,
) -> dict[str, Any]:
    st = read_state()
    had_ap = bool(st["ap"])
    st["ap"] = False
    target = next((n for n in st["networks"] if n["ssid"] == ssid), None)
    ok = target is not None and (
        not target.get("secured") or (password or "") == target.get("password")
    )
    st["joins"] = (st.get("joins") or [])[-19:] + [{"ssid": ssid, "ok": ok}]
    if ok:
        st["station"] = ssid
        if ssid not in st["saved"]:
            st["saved"].append(ssid)
        _write(st)
        return {"ok": True, "connected": True, "ap_restored": False, "error": None}
    reason = (
        f"No network named {ssid} is in range."
        if target is None
        else "Secrets were required, but not provided (wrong password)."
    )
    restored = False
    if had_ap and ap_ssid and ap_passphrase:
        st["ap"] = True
        st["ap_ssid"] = ap_ssid
        restored = True
    _write(st)
    return {"ok": False, "connected": False, "ap_restored": restored, "error": reason}


def saved_wifi() -> list[str]:
    return list(read_state().get("saved") or [])


def forget_wifi(ssid: str) -> bool:
    st = read_state()
    if ssid not in st["saved"]:
        return False
    st["saved"] = [s for s in st["saved"] if s != ssid]
    if st.get("station") == ssid:
        st["station"] = None
    _write(st)
    return True
