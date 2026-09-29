"""
utils/netctl — the mutation sibling of netinfo (commissioning plan, PR 4).

The boundary is the point: netinfo stays read-only (its own test pins that),
netctl is where the AP and the join live, and a failed join must bring the
AP straight back. nmcli is faked by argv so nothing here touches a radio.
"""

from __future__ import annotations

import pytest


class FakeNm:
    """Records every nmcli argv; scripted answers per subcommand."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.station = False
        self.ap = False
        self.join_rc = 0
        self.hotspot_rc = 0

    def run(self, argv, timeout=15.0):
        self.calls.append(argv)
        a = argv[1:] if argv and argv[0] == "nmcli" else argv
        if a[:4] == ["device", "wifi", "hotspot", "ifname"]:
            if self.hotspot_rc == 0:
                self.ap = True
            return self.hotspot_rc, "", "" if self.hotspot_rc == 0 else "hotspot failed"
        if a[:2] == ["connection", "down"]:
            self.ap = False
            return 0, "", ""
        if a[:2] == ["connection", "delete"]:
            return 0, "", ""
        if a[:2] == ["connection", "modify"]:
            return 0, "", ""
        if a[:3] == ["device", "wifi", "connect"]:
            if self.join_rc == 0:
                self.station = True
                return 0, "Device 'wlan0' successfully activated", ""
            return self.join_rc, "", "Error: Secrets were required, but not provided."
        if "device" in a and "status" in a:
            conn = "wqm1-setup-ap" if self.ap else ("PondHouse" if self.station else "--")
            state = "connected" if (self.ap or self.station) else "disconnected"
            return 0, f"wlan0:{state}:{conn}", ""
        if a[:4] == ["-t", "-f", "NAME,DEVICE", "connection"] or (
            "connection" in a and "show" in a and "--active" in a
        ):
            return 0, ("wqm1-setup-ap:wlan0" if self.ap else ""), ""
        if a[:3] == ["device", "wifi", "rescan"]:
            return 0, "", ""
        if a[:4] == ["-t", "-f", "SSID,SIGNAL,SECURITY", "device"]:
            return 0, "PondHouse:72:WPA2\nPondHouse:40:WPA2\nBarn:55:--\n:10:WPA2", ""
        return 0, "", ""


@pytest.fixture
def nm(monkeypatch):
    from utils import netctl

    fake = FakeNm()
    monkeypatch.setattr(netctl, "_run", fake.run)
    return fake


class TestBoundary:
    def test_netinfo_still_exposes_no_mutation(self):
        """netctl's existence must not have moved anything into netinfo."""
        from utils import netinfo

        forbidden = {"set_wifi", "join_network", "connect", "start_ap", "reconfigure"}
        assert not forbidden & set(dir(netinfo))

    def test_netctl_never_imports_flask_or_the_service_window(self):
        from pathlib import Path

        from utils import netctl

        src = Path(netctl.__file__).read_text()
        assert "flask" not in src.lower()
        assert "service_window" not in src


class TestAccessPoint:
    def test_start_ap_refuses_a_short_passphrase_rather_than_going_open(self, nm):
        from utils.netctl import start_ap

        r = start_ap("WQM1-0001", "short")
        assert r["ok"] is False
        assert not any("hotspot" in c for c in nm.calls)

    def test_start_ap_raises_wpa2_hotspot_named_for_the_unit(self, nm):
        from utils.netctl import AP_CONNECTION_NAME, start_ap

        r = start_ap("WQM1-0001", "river-cedar-42")
        assert r["ok"] is True
        assert r["address"] == "192.168.4.1"
        hotspot = next(c for c in nm.calls if "hotspot" in c)
        assert "ssid" in hotspot and hotspot[hotspot.index("ssid") + 1] == "WQM1-0001"
        assert hotspot[hotspot.index("password") + 1] == "river-cedar-42"
        assert hotspot[hotspot.index("con-name") + 1] == AP_CONNECTION_NAME

    def test_ensure_reachable_prefers_a_station_link(self, nm):
        from utils.netctl import ensure_reachable

        nm.station = True
        r = ensure_reachable("WQM1-0001", "river-cedar-42", grace_s=10, sleep=lambda s: None)
        assert r["mode"] == "station"
        assert not any("hotspot" in c for c in nm.calls)

    def test_ensure_reachable_raises_the_ap_after_the_grace_period(self, nm):
        from utils.netctl import ensure_reachable

        t = [0.0]

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s

        r = ensure_reachable("WQM1-0001", "river-cedar-42", grace_s=45, sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert t[0] >= 45


class TestJoin:
    def test_scan_lists_strongest_first_and_dedupes(self, nm):
        from utils.netctl import scan_networks

        nets = scan_networks()
        assert [n["ssid"] for n in nets] == ["PondHouse", "Barn"]
        assert nets[0]["signal"] == 72 and nets[0]["secured"] is True
        assert nets[1]["secured"] is False

    def test_good_join_tears_down_the_ap_and_stays_joined(self, nm):
        from utils.netctl import join_network

        nm.ap = True
        r = join_network(
            "PondHouse", "hunter22", ap_ssid="WQM1-0001", ap_passphrase="river-cedar-42"
        )
        assert r == {"ok": True, "connected": True, "ap_restored": False, "error": None}
        assert any(c[1:3] == ["connection", "down"] for c in nm.calls)

    def test_wrong_password_reraises_the_ap(self, nm):
        """The load-bearing case: an installer locked out by a typo is worse
        than no feature."""
        from utils.netctl import join_network

        nm.ap = True
        nm.join_rc = 4
        r = join_network(
            "PondHouse",
            "wrong",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=lambda s: None,
        )
        assert r["ok"] is False
        assert r["connected"] is False
        assert r["ap_restored"] is True
        assert "Secrets were required" in r["error"]
        assert nm.ap is True
        # The failed profile is dropped so NM does not keep retrying it.
        assert any(c[1:3] == ["connection", "delete"] and c[3] == "PondHouse" for c in nm.calls)

    def test_join_refuses_a_control_character_ssid(self, nm):
        from utils.netctl import join_network

        r = join_network("bad\x00ssid", "x")
        assert r["ok"] is False
        assert not any("connect" in c for c in nm.calls)


class TestApFallbackScript:
    def test_passphrase_is_never_a_shared_default(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "ap_fallback", Path(__file__).parent.parent / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        a = mod.derived_passphrase("10000000abcdef01")
        b = mod.derived_passphrase("10000000abcdef02")
        assert a != b and len(a) == 12 and len(b) == 12

    def test_credentials_come_from_the_identity_file_when_present(self, tmp_path, monkeypatch):
        import importlib.util
        import json
        from pathlib import Path

        from utils import identity

        f = tmp_path / "id.json"
        f.write_text(json.dumps({"serial": "WQM-10001", "ap_passphrase": "river-cedar-42"}))
        monkeypatch.setenv(identity.IDENTITY_FILE_ENV, str(f))
        spec = importlib.util.spec_from_file_location(
            "ap_fallback2", Path(__file__).parent.parent / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.resolve_credentials() == ("WQM1-0001", "river-cedar-42")

    def test_unit_file_runs_after_networkmanager_as_a_oneshot(self):
        from pathlib import Path

        unit = (
            Path(__file__).parent.parent / "systemd" / "bluesignal-ap-fallback.service"
        ).read_text()
        assert "After=NetworkManager.service" in unit
        assert "Type=oneshot" in unit
        assert "wqm1-ap-fallback.py" in unit
