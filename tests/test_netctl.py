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
        self.hotspot_fails = 0
        self.hotspot_err = "device not ready"
        self.add_rc = 0
        self.saved: list[str] = []
        self.iface_state = ""

    def run(self, argv, timeout=15.0):
        self.calls.append(argv)
        a = argv[1:] if argv and argv[0] == "nmcli" else argv
        if a[:2] == ["connection", "add"]:
            if self.add_rc != 0:
                return self.add_rc, "", "Error: a connection with this name already exists"
            return 0, "", ""
        if a[:2] == ["connection", "up"]:
            if self.hotspot_rc != 0:
                return self.hotspot_rc, "", self.hotspot_err
            if self.hotspot_fails > 0:
                self.hotspot_fails -= 1
                return 1, "", self.hotspot_err
            self.ap = True
            return 0, "", ""
        if a[:4] == ["-t", "-f", "NAME,TYPE", "connection"] and "--active" not in a:
            lines = [f"{name}:802-11-wireless" for name in self.saved]
            return 0, "\n".join(lines), ""
        if a[:2] == ["-g", "802-11-wireless.ssid"]:
            return 0, a[-1], ""
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
            if self.iface_state:
                state = self.iface_state
                conn = "--"
            else:
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
        assert not any(c[1:3] == ["connection", "add"] for c in nm.calls)
        assert not any(c[1:3] == ["connection", "up"] for c in nm.calls)

    def test_start_ap_raises_wpa2_hotspot_named_for_the_unit(self, nm):
        from utils.netctl import AP_CONNECTION_NAME, start_ap

        r = start_ap("WQM1-0001", "river-cedar-42")
        assert r["ok"] is True
        assert r["address"] == "192.168.4.1"
        delete = next(c for c in nm.calls if c[1:3] == ["connection", "delete"])
        add = next(c for c in nm.calls if c[1:3] == ["connection", "add"])
        modify = next(c for c in nm.calls if c[1:3] == ["connection", "modify"])
        up = next(c for c in nm.calls if c[1:3] == ["connection", "up"])
        # Recreate, then write security, then activate. Already-flashed
        # cards never get NetworkManager's default hotspot.
        assert nm.calls.index(delete) < nm.calls.index(add) < nm.calls.index(modify)
        assert nm.calls.index(modify) < nm.calls.index(up)
        assert delete[3] == AP_CONNECTION_NAME
        assert add[add.index("ssid") + 1] == "WQM1-0001"
        assert add[add.index("con-name") + 1] == AP_CONNECTION_NAME
        props = dict(zip(modify[4::2], modify[5::2], strict=True))
        assert props == {
            "connection.interface-name": "wlan0",
            "connection.autoconnect": "no",
            "802-11-wireless.ssid": "WQM1-0001",
            "802-11-wireless.mode": "ap",
            "802-11-wireless.powersave": "2",
            "802-11-wireless-security.key-mgmt": "wpa-psk",
            "802-11-wireless-security.proto": "rsn",
            "802-11-wireless-security.pairwise": "ccmp",
            "802-11-wireless-security.group": "ccmp",
            "802-11-wireless-security.pmf": "1",
            "802-11-wireless-security.psk": "river-cedar-42",
            "ipv4.method": "shared",
            "ipv4.addresses": "192.168.4.1/24",
        }
        assert "sae" not in modify
        assert up[3] == AP_CONNECTION_NAME

    def test_existing_profile_is_updated_when_add_cannot_replace_it(self, nm):
        """delete + add is the normal path. When the old profile is still
        there, modify writes the same security before activation."""
        from utils.netctl import start_ap

        nm.add_rc = 10
        r = start_ap("WQM1-4FD8", "river-cedar-42")
        assert r["ok"] is True
        modify = next(c for c in nm.calls if c[1:3] == ["connection", "modify"])
        props = dict(zip(modify[4::2], modify[5::2], strict=True))
        assert props["802-11-wireless-security.proto"] == "rsn"
        assert props["802-11-wireless-security.pairwise"] == "ccmp"
        assert props["802-11-wireless-security.group"] == "ccmp"
        assert props["802-11-wireless-security.pmf"] == "1"
        assert props["802-11-wireless-security.key-mgmt"] == "wpa-psk"
        assert props["802-11-wireless.powersave"] == "2"
        assert props["802-11-wireless.ssid"] == "WQM1-4FD8"
        assert props["802-11-wireless-security.psk"] == "river-cedar-42"
        up = next(c for c in nm.calls if c[1:3] == ["connection", "up"])
        assert nm.calls.index(modify) < nm.calls.index(up)

    def test_ensure_reachable_prefers_a_station_link(self, nm):
        from utils.netctl import ensure_reachable

        nm.station = True
        r = ensure_reachable("WQM1-0001", "river-cedar-42", grace_s=10, sleep=lambda s: None)
        assert r["mode"] == "station"
        assert not any(c[1:3] == ["connection", "up"] for c in nm.calls)

    def test_ensure_reachable_raises_the_ap_after_the_grace_period(self, nm):
        from utils.netctl import ensure_reachable

        t = [0.0]

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s

        nm.saved = ["PondHouse"]
        r = ensure_reachable("WQM1-0001", "river-cedar-42", grace_s=45, sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert t[0] >= 45

    def test_unconfigured_unit_raises_the_ap_without_the_station_grace(self, nm):
        """No saved Wi-Fi: nothing can associate, so the 45s wait is skipped
        and the radio is unblocked before the hotspot command."""
        from utils.netctl import ensure_reachable

        t = [0.0]

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s

        r = ensure_reachable("WQM1-0001", "river-cedar-42", grace_s=45, sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert t[0] < 45
        assert any(c[:3] == ["rfkill", "unblock", "wifi"] for c in nm.calls)
        assert any(c[:3] == ["ip", "link", "set"] for c in nm.calls)
        assert ["iw", "dev", "wlan0", "set", "power_save", "off"] in nm.calls

    def test_hotspot_is_retried_when_the_interface_is_not_ready(self, nm):
        from utils.netctl import ensure_reachable

        nm.hotspot_fails = 1
        nm.iface_state = "unavailable"
        t = [0.0]

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s

        r = ensure_reachable(
            "WQM1-0001",
            "river-cedar-42",
            grace_s=45,
            radio_wait_s=4,
            sleep=sleep,
            clock=clock,
        )
        assert r["ap"] is True
        activations = [c for c in nm.calls if c[1:3] == ["connection", "up"]]
        assert len(activations) == 2


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

    def test_a_failed_restore_is_retried(self, nm):
        from utils.netctl import join_network

        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_fails = 1
        nm.hotspot_err = (
            "Error: Connection activation failed: 802.1X supplicant took too long to authenticate."
        )
        r = join_network(
            "PondHouse",
            "wrong",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=lambda s: None,
        )
        assert r["ap_restored"] is True
        activations = [c for c in nm.calls if c[1:3] == ["connection", "up"]]
        assert len(activations) == 2

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

    def test_derived_passphrase_is_the_sha256_prefix(self):
        import hashlib

        from utils.netctl import derived_passphrase

        serial = "1000000086544fd8"
        expected = hashlib.sha256(f"wqm1-ap:{serial}".encode()).hexdigest()[:12]
        assert derived_passphrase(serial) == expected

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
        # The hotspot is what creates a network on an unconfigured unit.
        # Waiting for network-online blocks on a connection that will not come,
        # and the main app's health must not gate the script. The comment in
        # the unit file names the target it refuses to order on; the
        # directives themselves must not.
        directives = "\n".join(
            line for line in unit.splitlines() if line and not line.lstrip().startswith("#")
        )
        assert "network-online.target" not in directives
        assert "bluesignal-wqm" not in directives
        assert "Restart=on-failure" in directives

    def test_start_limit_keys_are_in_the_unit_section(self):
        import configparser
        import shutil
        import subprocess
        from pathlib import Path

        path = Path(__file__).parent.parent / "systemd" / "bluesignal-ap-fallback.service"
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read(path)
        assert parser.get("Unit", "StartLimitIntervalSec") == "600"
        assert parser.get("Unit", "StartLimitBurst") == "8"
        assert not parser.has_option("Service", "StartLimitIntervalSec")
        assert not parser.has_option("Service", "StartLimitBurst")
        exe = shutil.which("systemd-analyze")
        if not exe:
            return
        result = subprocess.run(
            [exe, "verify", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        combined = result.stdout + result.stderr
        assert "StartLimitIntervalSec" not in combined
        assert "Unknown key" not in combined

    def test_failed_activation_is_written_to_the_status_file(self, tmp_path, monkeypatch):
        import importlib.util
        from pathlib import Path

        from utils import netctl

        spec = importlib.util.spec_from_file_location(
            "ap_fallback_fail", Path(__file__).parent.parent / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fake = FakeNm()
        fake.hotspot_rc = 1
        fake.hotspot_err = (
            "Error: Connection activation failed: 802.1X supplicant took too long to authenticate."
        )
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(mod, "resolve_credentials", lambda: ("WQM1-4FD8", "river-cedar-42"))
        status = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        assert mod.main() == 1
        text = status.read_text(encoding="utf-8")
        assert (
            "hotspot: failed: Error: Connection activation failed: "
            "802.1X supplicant took too long to authenticate."
        ) in text
        assert "ssid: WQM1-4FD8" in text
        assert "COMMISSION: FAIL:" in text
        assert "river-cedar-42" not in text
        assert "passphrase" not in text.lower()

    def test_a_passphrase_echoed_by_nm_is_not_written_to_the_status_file(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "ap_fallback_redact", Path(__file__).parent.parent / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        line = mod.hotspot_failure_status(
            {"error": "psk river-cedar-42 rejected"}, "river-cedar-42"
        )
        assert line == "failed: psk [redacted] rejected"
        assert "river-cedar-42" not in line
