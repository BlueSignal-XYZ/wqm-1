"""The commissioning simulator's fake radio answers in shapes the real join
code parses. The simulator itself needs root and namespaces (see
``tools/sim/README.md``); these checks keep its fakes from drifting away from
``utils.netctl`` and ``utils.wifi_setup`` without either.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"sim_{name}", ROOT / "tools" / "sim" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def radio(tmp_path, monkeypatch):
    fake = _load("fake_radio")
    monkeypatch.setenv("WQM1_SIM_DIR", str(tmp_path))
    # No namespaces here: every ip / ip netns call is a no-op.
    monkeypatch.setattr(
        fake.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", ""),
    )
    monkeypatch.setattr(fake.time, "sleep", lambda s: None)
    commission = _load("commission")
    sc = commission.SCENARIOS[0]
    (tmp_path / "world.json").write_text(json.dumps(commission.world_for(sc)))
    return fake


def test_the_rescan_list_parses_as_the_real_scan_does(radio):
    from utils.wifi_setup import parse_nmcli_wifi

    assert radio.nmcli(["device", "wifi", "rescan", "ifname", "wlan0"])[0] == 0
    assert radio.nmcli(["device", "wifi", "rescan", "ifname", "wlan0"])[0] == 0
    rc, out, _ = radio.nmcli(["-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list"])
    assert rc == 0
    names = [n["ssid"] for n in parse_nmcli_wifi(out)]
    assert names == ["PondHouse-Sim", "Neighbor-Sim"]


def test_a_just_released_radio_lists_nothing_until_a_rescan(radio):
    radio.nmcli(["device", "set", "wlan0", "managed", "no"])
    radio.nmcli(["device", "set", "wlan0", "managed", "yes"])
    _, out, _ = radio.nmcli(["-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list"])
    assert out == ""


def test_the_hotspot_scan_parses_as_the_real_iw_scan_does(radio):
    from utils.netctl import parse_iw_scan

    _, out, _ = radio.iw(["dev", "wlan0", "scan"])
    found = parse_iw_scan(out)
    assert {n["ssid"] for n in found} == {"PondHouse-Sim", "Neighbor-Sim"}
    assert all(n["security"] == "wpa_psk" for n in found)


def test_device_status_reads_as_the_real_state_queries_expect(radio):
    _, out, _ = radio.nmcli(["-t", "-f", "DEVICE,STATE", "device", "status"])
    assert out == "wlan0:disconnected"
    radio.nmcli(["device", "set", "wlan0", "managed", "no"])
    _, out, _ = radio.nmcli(["-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"])
    assert out == "wlan0:unmanaged:--"


def test_a_profile_is_added_renamed_and_read_back(radio):
    argv = ["connection", "add", "type", "wifi", "con-name", "bs-try-1", "ifname", "wlan0"]
    argv += ["ssid", "PondHouse-Sim", "802-11-wireless.band", "bg", "wifi-sec.psk", "x" * 8]
    assert radio.nmcli(argv)[0] == 0
    radio.nmcli(["connection", "modify", "bs-try-1", "connection.id", "PondHouse-Sim"])
    _, out, _ = radio.nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
    assert out == "PondHouse-Sim:802-11-wireless"
    _, ssid, _ = radio.nmcli(["-g", "802-11-wireless.ssid", "connection", "show", "PondHouse-Sim"])
    assert ssid == "PondHouse-Sim"


def test_a_wrong_password_answers_as_networkmanager_does(radio):
    from utils.netctl import _classify_connect_error

    argv = ["connection", "add", "type", "wifi", "con-name", "bs-try-2", "ifname", "wlan0"]
    radio.nmcli([*argv, "ssid", "PondHouse-Sim", "wifi-sec.psk", "not-the-password"])
    radio.nmcli(["device", "wifi", "rescan"])
    radio.nmcli(["device", "wifi", "rescan"])
    rc, _, err = radio.nmcli(["connection", "up", "bs-try-2", "ifname", "wlan0"])
    assert rc != 0
    assert _classify_connect_error(err) == "wrong_password"


def test_a_network_not_in_the_scan_answers_not_found(radio):
    from utils.netctl import _classify_connect_error

    argv = ["connection", "add", "type", "wifi", "con-name", "bs-try-3", "ifname", "wlan0"]
    radio.nmcli([*argv, "ssid", "PondHouse-Sim", "wifi-sec.psk", "whatever1"])
    rc, _, err = radio.nmcli(["connection", "up", "bs-try-3", "ifname", "wlan0"])
    assert rc != 0
    assert _classify_connect_error(err) == "not_found"


def test_the_log_never_carries_a_password(radio):
    assert radio._safe(["wifi-sec.psk", "secret-ish", "ssid", "Home"]) == [
        "wifi-sec.psk",
        "<hidden>",
        "ssid",
        "Home",
    ]


def test_every_scenario_has_a_reason_the_page_knows():
    from utils import setup_copy

    commission = _load("commission")
    for sc in commission.SCENARIOS:
        assert sc.expect == "joined" or sc.expect in setup_copy.en, sc.name
