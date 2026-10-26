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
        self.ipv4 = "192.168.1.50"
        self.ipv4_script: list[str] | None = None
        self.ipv4_i = 0
        self._last_ipv4 = ""
        self.gateway = "192.168.1.1"
        self.dns = "192.168.1.1"
        self.reach = True
        self.regdom = ""
        self.iw_scan = ""
        self.dnsmasq_rc = 0
        self.dnsmasq_err = "dnsmasq failed"
        self.hostapd = False
        self.dnsmasq = False
        self.managed = "yes"
        # Realistic scan model (off by default so the older tests keep their
        # always-visible list). On: NetworkManager's list is EMPTY after the
        # radio was unmanaged for hostapd, fills only on a rescan, and a
        # connect to an SSID not in it answers "No network with SSID found"
        # — what WQM-10001 hit in the field on 2026-10-21.
        self.realistic_scan = False
        self.visible = ["PondHouse", "Barn"]
        self.cache: set[str] = set()
        self.rescans_before_seen = 0
        self.hidden_join_ok = True
        # Bringing a SAVED profile up (boot path). Fails by default, as a
        # network that is out of range does.
        self.station_up_rc = 4
        self.station_up_err = (
            "Error: Connection activation failed: The Wi-Fi network could not be found."
        )
        # NetworkManager not answering the saved-profile listing yet (boot).
        self.list_fails = 0
        # `iw dev wlan0 station dump` while the hotspot is up.
        self.ap_station_dump = ""

    def _wifi_list(self):
        rows = ["PondHouse:72:WPA2", "PondHouse:40:WPA2", "Barn:55:--", ":10:WPA2"]
        if not self.realistic_scan:
            return "\n".join(rows)
        keep = [r for r in rows if r.split(":")[0] in self.cache]
        return "\n".join(keep)

    def run(self, argv, timeout=15.0):
        self.calls.append(argv)
        a0 = argv[1:] if argv and argv[0] == "nmcli" else argv
        if self.realistic_scan:
            if a0[:5] == ["device", "set", "wlan0", "managed", "no"]:
                self.cache = set()
            if a0[:3] == ["device", "wifi", "rescan"]:
                if self.rescans_before_seen > 0:
                    self.rescans_before_seen -= 1
                else:
                    self.cache = set(self.visible)
                return 0, "", ""
            if a0[:3] == ["device", "wifi", "connect"]:
                ssid = a0[3]
                hidden = "hidden" in a0
                if ssid not in self.cache and not (hidden and self.hidden_join_ok):
                    return 10, "", f"Error: No network with SSID '{ssid}' found."
        if argv and argv[0] == "systemctl":
            action = argv[1] if len(argv) > 1 else ""
            unit = argv[2] if len(argv) > 2 else ""
            if action == "start" and unit == "wqm1-setup-hostapd.service":
                if self.hotspot_rc != 0:
                    return self.hotspot_rc, "", self.hotspot_err
                if self.hotspot_fails > 0:
                    self.hotspot_fails -= 1
                    return 1, "", self.hotspot_err
                self.ap = True
                self.hostapd = True
                return 0, "", ""
            if action == "stop" and unit == "wqm1-setup-hostapd.service":
                self.ap = False
                self.hostapd = False
                return 0, "", ""
            if action == "start" and unit == "wqm1-setup-dnsmasq.service":
                if self.dnsmasq_rc != 0:
                    return self.dnsmasq_rc, "", self.dnsmasq_err
                self.dnsmasq = True
                return 0, "", ""
            if action == "stop" and unit == "wqm1-setup-dnsmasq.service":
                self.dnsmasq = False
                return 0, "", ""
            return 0, "", ""
        if argv and argv[0] == "hostapd_cli":
            if self.ap:
                return 0, "state=ENABLED", ""
            return 1, "", "Failed to connect to hostapd - wpactrl_open: No such file or directory"
        if argv[:3] == ["iw", "reg", "get"] and self.regdom:
            return 0, f"country {self.regdom}: DFS-FCC", ""
        if argv[:4] == ["iw", "dev", "wlan0", "scan"]:
            return 0, self.iw_scan, ""
        if argv[:5] == ["iw", "dev", "wlan0", "station", "dump"]:
            return 0, (self.ap_station_dump if self.ap else ""), ""
        if argv[:4] == ["iw", "dev", "wlan0", "link"]:
            return 0, getattr(self, "iw_link", "") or "", ""
        a = argv[1:] if argv and argv[0] == "nmcli" else argv
        if a[:4] == ["device", "set", "wlan0", "managed"]:
            self.managed = a[4]
            return 0, "", ""
        if a[:2] == ["connection", "add"]:
            if self.add_rc != 0:
                return self.add_rc, "", "Error: a connection with this name already exists"
            return 0, "", ""
        if a[:2] == ["connection", "up"] and len(a) > 2 and a[2] in self.saved:
            if self.realistic_scan and a[2] not in self.cache:
                return 10, "", f"Error: No network with SSID '{a[2]}' found."
            if self.station_up_rc == 0:
                self.station = True
                return 0, "Connection successfully activated", ""
            return self.station_up_rc, "", self.station_up_err
        if a[:2] == ["connection", "up"]:
            if self.hotspot_rc != 0:
                return self.hotspot_rc, "", self.hotspot_err
            if self.hotspot_fails > 0:
                self.hotspot_fails -= 1
                return 1, "", self.hotspot_err
            self.ap = True
            return 0, "", ""
        if a[:4] == ["-t", "-f", "NAME,TYPE", "connection"] and "--active" not in a:
            if self.list_fails > 0:
                self.list_fails -= 1
                return 8, "", "Error: NetworkManager is not running."
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
        if a[:2] == ["-t", "-f"] and str(a[2]).startswith("SSID,SIGNAL,SECURITY") and "device" in a:
            return 0, self._wifi_list(), ""
        if a[:2] == ["device", "disconnect"]:
            self.station = False
            self.ap = False
            return 0, "", ""
        if a[:2] == ["-g", "IP4.DNS"]:
            ip = self._last_ipv4
            if not self.station or not ip or ip.startswith("169.254."):
                return 0, "", ""
            return 0, self.dns, ""
        if a[:4] == ["ip", "-4", "-o", "addr"]:
            ip = self._take_ipv4()
            if not ip:
                return 0, "", ""
            scope = "link" if ip.startswith("169.254.") else "global"
            return 0, f"2: wlan0 inet {ip}/24 scope {scope} dynamic wlan0", ""
        if a[:4] == ["ip", "-4", "route", "show"]:
            ip = self._last_ipv4
            if not ip or ip.startswith("169.254.") or not self.gateway:
                return 0, "", ""
            return 0, f"default via {self.gateway} dev wlan0 proto dhcp src {ip}", ""
        return 0, "", ""

    def _take_ipv4(self) -> str:
        if not self.station:
            self._last_ipv4 = ""
            return ""
        if self.ipv4_script is not None:
            if self.ipv4_i < len(self.ipv4_script):
                value = self.ipv4_script[self.ipv4_i]
                self.ipv4_i += 1
            else:
                value = self.ipv4_script[-1]
        else:
            value = self.ipv4
        self._last_ipv4 = value
        return value


@pytest.fixture
def nm(monkeypatch):
    from utils import netctl

    fake = FakeNm()
    monkeypatch.setattr(netctl, "_run", fake.run)
    monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: fake.reach)
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


def _conf(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key] = value
    return found


def _at(calls: list[list[str]], pred) -> int:
    for index, call in enumerate(calls):
        if pred(call):
            return index
    raise AssertionError("expected call was not made")


class TestAccessPoint:
    def test_start_ap_refuses_a_short_passphrase_rather_than_going_open(self, nm):
        from utils.netctl import start_ap

        r = start_ap("WQM1-0001", "short")
        assert r["ok"] is False
        assert not any(
            c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"] for c in nm.calls
        )

    def test_hostapd_config_is_wpa_psk_only(self):
        from utils.netctl import hostapd_config

        text = hostapd_config("WQM1-4FD8", "river-cedar-42", country="US", channel=6)
        conf = _conf(text)
        assert conf["interface"] == "wlan0"
        assert conf["driver"] == "nl80211"
        assert conf["ssid"] == "WQM1-4FD8"
        assert conf["hw_mode"] == "g"
        assert conf["channel"] in {"6", "11"}
        assert conf["country_code"] == "US"
        assert conf["wpa"] == "2"
        assert conf["wpa_key_mgmt"] == "WPA-PSK"
        assert conf["wpa_passphrase"] == "river-cedar-42"
        assert conf["rsn_pairwise"] == "CCMP"
        assert conf["ieee80211w"] == "0"
        assert "SHA256" not in text
        assert "SAE" not in text
        assert "sae" not in text.lower()

    def test_no_passphrase_is_an_open_network(self):
        """Install plan §5: the setup network is open."""
        from utils.netctl import hostapd_config

        text = hostapd_config("BlueSignal Setup", None, country="US", channel=6)
        conf = _conf(text)
        assert conf["ssid"] == "BlueSignal Setup"
        assert conf["auth_algs"] == "1"
        assert conf["ieee80211w"] == "0"
        for key in ("wpa", "wpa_key_mgmt", "wpa_passphrase", "rsn_pairwise"):
            assert key not in conf, key
        assert "wpa" not in text.replace("ctrl_interface", "")
        assert hostapd_config("BlueSignal Setup", "", country="US") == text

    def test_an_eight_character_passphrase_is_unchanged(self):
        from utils.netctl import HOSTAPD_CTRL, hostapd_config

        text = hostapd_config("WQM1-4FD8", "12345678", country="US", channel=6)
        assert text.splitlines() == [
            "interface=wlan0",
            "driver=nl80211",
            "ssid=WQM1-4FD8",
            "hw_mode=g",
            "channel=6",
            "country_code=US",
            "ieee80211d=1",
            "auth_algs=1",
            "wpa=2",
            "wpa_key_mgmt=WPA-PSK",
            "wpa_passphrase=12345678",
            "rsn_pairwise=CCMP",
            "ieee80211w=0",
            f"ctrl_interface={HOSTAPD_CTRL}",
            "ctrl_interface_group=0",
        ]

    def test_start_ap_raises_the_open_setup_network(self, nm):
        import os
        from pathlib import Path

        from utils.netctl import ap_credentials, start_ap

        ssid, passphrase = ap_credentials()
        assert (ssid, passphrase) == ("BlueSignal Setup", None)
        assert start_ap(ssid, passphrase)["ok"] is True
        conf = _conf(Path(os.environ["WQM1_HOSTAPD_CONF"]).read_text())
        assert conf["ssid"] == "BlueSignal Setup"
        assert "wpa" not in conf and "wpa_passphrase" not in conf

    def test_a_failed_join_brings_the_open_network_back(self, nm):
        from utils.netctl import join_network, start_ap

        assert start_ap("BlueSignal Setup")["ok"] is True
        nm.join_rc = 4
        result = join_network(
            "PondHouse", "wrong-pass", ap_ssid="BlueSignal Setup", sleep=lambda _s: None
        )
        assert result["connected"] is False
        assert result["ap_restored"] is True
        assert nm.ap is True

    def test_country_follows_the_regulatory_domain(self, nm):
        from utils.netctl import hostapd_config, wifi_country

        nm.regdom = "DE"
        assert wifi_country() == "DE"
        text = hostapd_config("WQM1-4FD8", "river-cedar-42", country=wifi_country())
        assert _conf(text)["country_code"] == "DE"
        assert "iw" in nm.calls[0] and nm.calls[0][1] == "reg"

    def test_start_ap_writes_hostapd_and_dnsmasq_then_starts_them(self, nm, monkeypatch):
        import os
        from pathlib import Path

        from utils.captive_portal import dnsmasq_ap_config
        from utils.netctl import start_ap

        monkeypatch.setenv(
            "WQM1_PORTAL_MODE_FILE", str(Path(os.environ["WQM1_HOSTAPD_CONF"]).parent / "mode")
        )
        r = start_ap("WQM1-4FD8", "river-cedar-42")
        assert r["ok"] is True
        assert r["address"] == "192.168.4.1"
        assert nm.ap is True
        assert nm.dnsmasq is True
        assert nm.managed == "no"
        hostapd = Path(os.environ["WQM1_HOSTAPD_CONF"]).read_text(encoding="utf-8")
        dns = Path(os.environ["WQM1_DNSMASQ_CONF"]).read_text(encoding="utf-8")
        conf = _conf(hostapd)
        assert conf["ssid"] == "WQM1-4FD8"
        assert conf["wpa_key_mgmt"] == "WPA-PSK"
        assert conf["ieee80211w"] == "0"
        assert conf["rsn_pairwise"] == "CCMP"
        assert conf["wpa_passphrase"] == "river-cedar-42"
        assert "SHA256" not in hostapd
        assert "bind-interfaces" in dns
        assert "interface=wlan0" in dns
        assert "dhcp-range=192.168.4.10,192.168.4.254,255.255.255.0,12h" in dns
        assert "address=/#/192.168.4.1" in dns
        assert 'dhcp-option=114,"http://192.168.4.1/setup/"' in dns
        assert dns == dnsmasq_ap_config()
        assert ":8080" not in dns
        managed_no = _at(
            nm.calls, lambda c: c[:6] == ["nmcli", "device", "set", "wlan0", "managed", "no"]
        )
        addr = _at(nm.calls, lambda c: c[:3] == ["ip", "addr", "replace"])
        hostapd_start = _at(
            nm.calls, lambda c: c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"]
        )
        dns_start = _at(
            nm.calls, lambda c: c[:3] == ["systemctl", "start", "wqm1-setup-dnsmasq.service"]
        )
        assert managed_no < addr < hostapd_start < dns_start
        assert not any(c[1:3] == ["connection", "up"] for c in nm.calls)
        from utils.captive_portal import portal_should_bind, read_mode

        assert read_mode() == "up"
        assert portal_should_bind(read_mode(), True) is True
        mode = Path(os.environ["WQM1_HOSTAPD_CONF"]).stat().st_mode & 0o777
        assert mode == 0o600

    def test_portal_still_answers_a_captive_probe(self, nm, monkeypatch, tmp_path):
        import http.client
        import threading

        from utils.captive_portal import PORTAL_URL, make_server
        from utils.netctl import start_ap

        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "mode"))
        assert start_ap("WQM1-4FD8", "river-cedar-42")["ok"] is True
        portal = make_server("127.0.0.1", 0)
        thread = threading.Thread(target=portal.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", portal.server_address[1], timeout=2)
            conn.request(
                "GET",
                "/hotspot-detect.html",
                headers={"Host": "captive.apple.com", "Connection": "close"},
            )
            resp = conn.getresponse()
            body = resp.read()
            location = resp.getheader("Location")
            conn.close()
        finally:
            portal.shutdown()
            thread.join(timeout=2)
        assert resp.status == 302
        assert location == PORTAL_URL
        assert b"Success" not in body

    def test_scan_while_the_ap_is_up_uses_iw(self, nm):
        from utils.netctl import scan_networks, start_ap

        nm.iw_scan = (
            "BSS aa:bb:cc:dd:ee:01(on wlan0)\n"
            "\tsignal: -40.00 dBm\n"
            "\tSSID: PondHouse\n"
            "\tRSN:\n"
            "BSS aa:bb:cc:dd:ee:02(on wlan0)\n"
            "\tsignal: -70.00 dBm\n"
            "\tSSID: Barn\n"
        )
        assert start_ap("WQM1-4FD8", "river-cedar-42")["ok"] is True
        # start_ap scans once with nmcli before it unmanages wlan0 (§4's
        # cached list); from here on the AP holds the radio.
        after_ap = len(nm.calls)
        nets = scan_networks()
        assert [n["ssid"] for n in nets] == ["PondHouse", "Barn"]
        assert nets[0]["secured"] is True
        assert nets[0]["signal"] == 100
        assert nets[1]["secured"] is False
        calls = nm.calls[after_ap:]
        assert any(c[:4] == ["iw", "dev", "wlan0", "scan"] for c in calls)
        assert not any(c[1:4] == ["device", "wifi", "rescan"] for c in calls)

    def test_start_ap_caches_the_scan_before_it_takes_the_radio(self, nm):
        from utils.netctl import read_scan_cache, start_ap

        assert start_ap("WQM1-4FD8", "river-cedar-42")["ok"] is True
        cached = read_scan_cache()
        assert cached["at"]
        visible = [n["ssid"] for n in cached["networks"] if not n["hidden"]]
        assert visible == ["PondHouse", "Barn"]
        assert any(n["hidden"] for n in cached["networks"]), "kept so the page asks for the name"
        listed = next(i for i, c in enumerate(nm.calls) if c[1:3] == ["-t", "-f"] and "list" in c)
        unmanaged = next(
            i
            for i, c in enumerate(nm.calls)
            if c[1:6] == ["device", "set", "wlan0", "managed", "no"]
        )
        assert listed < unmanaged

    def test_a_failed_rescan_keeps_the_cached_list(self, nm):
        from utils.netctl import read_scan_cache, refresh_scan_cache, start_ap

        assert start_ap("WQM1-4FD8", "river-cedar-42")["ok"] is True
        before = read_scan_cache()["networks"]
        real = nm.run

        def failing(argv, timeout=15.0):
            if argv[:5] == ["iw", "dev", "wlan0", "scan", "ap-force"]:
                nm.calls.append(argv)
                return 240, "", "command failed: Operation not supported (-95)"
            return real(argv, timeout)

        from utils import netctl

        netctl._run = failing  # restored by the nm fixture's monkeypatch
        assert refresh_scan_cache() == {"ok": False, "count": 0}
        assert read_scan_cache()["networks"] == before
        assert any(c[:5] == ["iw", "dev", "wlan0", "scan", "ap-force"] for c in nm.calls)
        assert not any(c[1:5] == ["device", "set", "wlan0", "managed"] for c in nm.calls[-3:])

    def test_a_rescan_with_the_ap_up_uses_ap_force(self, nm):
        from utils.netctl import read_scan_cache, refresh_scan_cache, start_ap

        assert start_ap("WQM1-4FD8", "river-cedar-42")["ok"] is True
        nm.iw_scan = "BSS aa:bb:cc:dd:ee:09(on wlan0)\n\tsignal: -50.00 dBm\n\tSSID: Loft\n"
        assert refresh_scan_cache() == {"ok": True, "count": 1}
        assert [n["ssid"] for n in read_scan_cache()["networks"]] == ["Loft"]
        assert nm.calls[-1][:5] == ["iw", "dev", "wlan0", "scan", "ap-force"]
        assert nm.ap is True, "the setup network stayed up"

    def test_stop_orders_dnsmasq_then_hostapd_then_nm(self, nm):
        from utils.netctl import start_ap, stop_ap

        assert start_ap("WQM1-0001", "river-cedar-42")["ok"]
        nm.calls.clear()
        assert stop_ap()["ok"]
        dns_stop = _at(
            nm.calls, lambda c: c[:3] == ["systemctl", "stop", "wqm1-setup-dnsmasq.service"]
        )
        host_stop = _at(
            nm.calls, lambda c: c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"]
        )
        flush = _at(nm.calls, lambda c: c[:4] == ["ip", "addr", "flush", "dev"])
        managed_yes = _at(
            nm.calls, lambda c: c[:6] == ["nmcli", "device", "set", "wlan0", "managed", "yes"]
        )
        assert dns_stop < host_stop < flush < managed_yes
        assert nm.ap is False
        assert nm.managed == "yes"

    def test_dnsmasq_failure_tears_the_ap_back_down(self, nm):
        from utils.netctl import start_ap

        nm.dnsmasq_rc = 1
        r = start_ap("WQM1-0001", "river-cedar-42")
        assert r["ok"] is False
        assert "dnsmasq" in r["error"]
        assert nm.ap is False
        assert nm.managed == "yes"

    def test_a_leftover_nm_profile_is_deleted_and_not_activated(self, nm):
        from utils.netctl import AP_CONNECTION_NAME, start_ap

        r = start_ap("WQM1-4FD8", "river-cedar-42")
        assert r["ok"] is True
        assert any(c[1:4] == ["connection", "delete", AP_CONNECTION_NAME] for c in nm.calls)
        assert not any(c[1:3] == ["connection", "up"] for c in nm.calls)
        assert not any(c[1:3] == ["connection", "add"] for c in nm.calls)

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

    def test_moved_card_does_not_join_the_old_site_wifi(self, nm, tmp_path, monkeypatch):
        """A commissioned card in a new Pi must not stay on the old site network."""
        from utils.cardmove import CardBinding, load, save
        from utils.netctl import ensure_reachable

        path = tmp_path / "card-binding"
        save(
            CardBinding(
                commissioned_serial="00000000aabbccdd",
                moved_from="00000000aabbccdd",
                moved_to="1000000086544fd8",
                wifi_reconfirm=True,
            ),
            path,
        )
        monkeypatch.setenv("WQM1_CARD_STATE", str(path))
        nm.station = True
        nm.saved = ["PondHouse"]
        t = [0.0]

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s

        r = ensure_reachable("WQM1-4FD8", "river-cedar-42", grace_s=45, sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert t[0] < 45
        assert [
            "nmcli",
            "connection",
            "modify",
            "PondHouse",
            "connection.autoconnect",
            "no",
        ] in nm.calls
        assert ["nmcli", "device", "disconnect", "wlan0"] in nm.calls
        assert load(path).wifi_reconfirm is True

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
        activations = [
            c for c in nm.calls if c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"]
        ]
        assert len(activations) == 2


class TestJoin:
    def test_scan_lists_strongest_first_and_dedupes(self, nm):
        from utils.netctl import scan_networks

        nets = scan_networks()
        visible = [n["ssid"] for n in nets if n["ssid"]]
        assert visible == ["PondHouse", "Barn"]
        assert nets[0]["signal"] == 72 and nets[0]["secured"] is True
        assert nets[1]["secured"] is False
        assert any(n.get("hidden") for n in nets)

    def test_good_join_tears_down_the_ap_and_stays_joined(self, nm):
        from utils.netctl import join_network

        nm.ap = True
        r = join_network(
            "PondHouse", "hunter22", ap_ssid="WQM1-0001", ap_passphrase="river-cedar-42"
        )
        assert r["ok"] is True
        assert r["connected"] is True
        assert r["ap_restored"] is False
        assert r["error"] is None
        assert r["ip"] == "192.168.1.50"
        assert r["reason"] is None
        assert any(c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"] for c in nm.calls)
        assert nm.ap is False
        assert nm.managed == "yes"

    def test_a_successful_join_confirms_the_moved_card(self, nm, tmp_path, monkeypatch):
        from utils.cardmove import CardBinding, load, save
        from utils.netctl import join_network

        path = tmp_path / "card-binding"
        save(
            CardBinding(
                commissioned_serial="00000000aabbccdd",
                moved_from="00000000aabbccdd",
                moved_to="1000000086544fd8",
                wifi_reconfirm=True,
            ),
            path,
        )
        monkeypatch.setenv("WQM1_CARD_STATE", str(path))
        monkeypatch.setattr("utils.cardmove.get_pi_serial", lambda: "1000000086544fd8")
        nm.ap = True
        r = join_network(
            "PondHouse", "hunter22", ap_ssid="WQM1-4FD8", ap_passphrase="river-cedar-42"
        )
        assert r["ok"] is True
        binding = load(path)
        assert binding.commissioned_serial == "1000000086544fd8"
        assert binding.wifi_reconfirm is False
        assert binding.moved_from == ""

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
        # The failed attempt's temporary profile is dropped so NM does not keep
        # retrying it. The SSID's own profile is never deleted by a failure (I3).
        deleted = [
            c[3] for c in nm.calls if c[1:3] == ["connection", "delete"] and c[3] != "wqm1-setup-ap"
        ]
        assert deleted and all(name.startswith("bs-try-") for name in deleted)
        assert "PondHouse" not in deleted
        # Stop (managed yes) happens before the join; the restore sets managed no
        # and starts hostapd again.
        connect = _at(nm.calls, lambda c: c[1:4] == ["device", "wifi", "connect"])
        managed_yes = _at(
            nm.calls, lambda c: c[:6] == ["nmcli", "device", "set", "wlan0", "managed", "yes"]
        )
        managed_no = _at(
            nm.calls, lambda c: c[:6] == ["nmcli", "device", "set", "wlan0", "managed", "no"]
        )
        restored = _at(
            nm.calls, lambda c: c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"]
        )
        assert managed_yes < connect < managed_no < restored

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
        assert r["reason"] == "wrong_password"
        activations = [
            c for c in nm.calls if c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"]
        ]
        assert len(activations) == 2

    def test_join_refuses_a_control_character_ssid(self, nm):
        from utils.netctl import join_network

        r = join_network("bad\x00ssid", "x")
        assert r["ok"] is False
        assert not any("connect" in c for c in nm.calls)


def _clock():
    t = [0.0]

    def clock():
        return t[0]

    def sleep(seconds):
        t[0] += float(seconds)

    return t, clock, sleep


class TestJoinWaitsForAnAddress:
    """Success is association plus a DHCP address plus a gateway or DNS.
    Every failure brings the hotspot and the portal back, and the reason
    is stored before that raise."""

    def _join(self, nm, tmp_path, monkeypatch, **kwargs):
        from utils.captive_portal import read_mode
        from utils.netctl import join_network

        mode = tmp_path / "portal-mode"
        status = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(mode))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        nm.ap = True
        seen = []
        t, clock, sleep = _clock()

        def on_failure(pending):
            seen.append(
                {
                    "ap": nm.ap,
                    "mode": read_mode(),
                    "message": pending.get("message"),
                    "reason": pending.get("reason"),
                }
            )

        result = join_network(
            "PondHouse",
            kwargs.pop("password", "hunter22"),
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=sleep,
            clock=clock,
            on_failure=on_failure,
            **kwargs,
        )
        return result, seen, t, status, read_mode()

    def test_associated_without_an_address_fails_and_restores(self, nm, tmp_path, monkeypatch):
        nm.ipv4 = ""
        result, seen, t, status, mode = self._join(nm, tmp_path, monkeypatch)
        assert result["connected"] is False
        assert result["reason"] == "no_dhcp"
        assert "no DHCP" in result["message"]
        assert result["ap_restored"] is True
        assert nm.ap is True
        assert mode == "up"
        assert seen and seen[0]["ap"] is False
        assert seen[0]["mode"] == "down: joining"
        assert t[0] >= 55
        text = status.read_text(encoding="utf-8")
        assert "station: fail: no address" in text

    def test_link_local_only_is_not_a_lease(self, nm, tmp_path, monkeypatch):
        nm.ipv4 = "169.254.42.9"
        result, seen, t, status, mode = self._join(nm, tmp_path, monkeypatch)
        assert result["connected"] is False
        assert result["reason"] == "no_dhcp"
        assert "169.254" in result["message"]
        assert result["ap_restored"] is True
        assert nm.ap is True
        assert mode == "up"
        assert seen and seen[0]["mode"] == "down: joining"
        assert t[0] >= 55
        assert "station: fail: no address" in status.read_text(encoding="utf-8")

    def test_a_late_dhcp_lease_within_the_timeout_succeeds(self, nm, tmp_path, monkeypatch):
        from utils.netctl import ADDRESS_POLL_S

        nm.ipv4_script = ["", "", "10.1.2.3"]
        result, seen, t, status, mode = self._join(nm, tmp_path, monkeypatch)
        assert result["connected"] is True
        assert result["ip"] == "10.1.2.3"
        assert result["ap_restored"] is False
        assert nm.ap is False
        assert mode == "down: station"
        assert seen == []
        assert t[0] < 55
        assert t[0] >= ADDRESS_POLL_S
        text = status.read_text(encoding="utf-8")
        assert "station: ok 10.1.2.3" in text
        assert "portal: down: station" in text

    def test_wrong_password_fails_fast_and_restores(self, nm, tmp_path, monkeypatch):
        nm.join_rc = 4
        result, seen, t, status, mode = self._join(nm, tmp_path, monkeypatch, password="wrong")
        assert result["connected"] is False
        assert result["reason"] == "wrong_password"
        assert "wrong password" in result["message"]
        assert "Secrets were required" in result["error"]
        assert result["ap_restored"] is True
        assert nm.ap is True
        assert mode == "up"
        assert seen and seen[0]["ap"] is False
        assert seen[0]["mode"] == "down: joining"
        assert t[0] < 5
        assert "station: fail: wrong password" in status.read_text(encoding="utf-8")

    def test_no_gateway_fails_and_restores(self, nm, tmp_path, monkeypatch):
        nm.reach = False
        result, seen, t, status, mode = self._join(nm, tmp_path, monkeypatch)
        assert result["connected"] is False
        assert result["reason"] == "no_internet"
        assert "no internet" in result["message"]
        assert result["ip"] is None
        assert result["ap_restored"] is True
        assert nm.ap is True
        assert mode == "up"
        assert seen and seen[0]["mode"] == "down: joining"
        assert t[0] >= 55
        assert "station: fail: no internet" in status.read_text(encoding="utf-8")

    def test_a_refusing_gateway_counts_as_reachable(self):
        """Connection refused means the host answered. An empty target does not.
        Nothing here leaves the machine."""
        import socket
        import threading

        from utils.netctl import _dns_answered, _tcp_answered, reach_gateway_or_dns

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        threading.Thread(target=listener.accept, daemon=True).start()
        try:
            assert _tcp_answered("127.0.0.1", listener.getsockname()[1], 1.0) is True
        finally:
            listener.close()
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        refused = closed.getsockname()[1]
        closed.close()
        assert _tcp_answered("127.0.0.1", refused, 1.0) is True
        assert _tcp_answered("", 80, 0.2) is False
        assert _dns_answered("", 0.2) is False
        assert reach_gateway_or_dns(None, [], deadline=0) is False
        assert reach_gateway_or_dns("127.0.0.1", []) is True

    def test_usable_ipv4_rejects_link_local_and_empty(self):
        from utils.netctl import usable_ipv4

        assert usable_ipv4("192.168.1.50")
        assert usable_ipv4("10.0.0.8")
        assert not usable_ipv4("169.254.1.1")
        assert not usable_ipv4("0.0.0.0")
        assert not usable_ipv4("")

    def test_boot_does_not_treat_link_local_as_a_station(self, nm):
        from utils.netctl import ensure_reachable

        nm.station = True
        nm.ipv4 = "169.254.8.8"
        t, clock, sleep = _clock()
        result = ensure_reachable(
            "WQM1-0001",
            "river-cedar-42",
            grace_s=0,
            radio_wait_s=0,
            sleep=sleep,
            clock=clock,
        )
        assert result["mode"] == "ap"
        assert result["ap"] is True
        assert result["station_failure"] == "no address"

    def test_boot_records_a_station_that_has_an_address(self, nm, tmp_path, monkeypatch):
        import importlib.util
        from pathlib import Path

        from utils import netctl

        nm.station = True
        spec = importlib.util.spec_from_file_location(
            "ap_fallback_station", Path(__file__).parent.parent / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(netctl, "_run", nm.run)
        monkeypatch.setattr(mod, "resolve_credentials", lambda: ("WQM1-4FD8", "river-cedar-42"))
        status = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        assert mod.main() == 0
        text = status.read_text(encoding="utf-8")
        assert "hotspot: station" in text
        assert "station: ok 192.168.1.50" in text
        assert "portal: down: station" in text


class TestApFallbackScript:
    def test_no_passphrase_is_derived_any_more(self):
        """Install plan §5 deleted derived_passphrase with its callers."""
        import importlib.util
        from pathlib import Path

        from utils import netctl

        spec = importlib.util.spec_from_file_location(
            "ap_fallback", Path(__file__).parent.parent / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert not hasattr(mod, "derived_passphrase")
        assert not hasattr(netctl, "derived_passphrase")

    def test_the_setup_network_is_open_whatever_the_card_says(self, tmp_path, monkeypatch):
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
        # A bench card that still carries ap_passphrase does not close it.
        assert mod.resolve_credentials() == ("BlueSignal Setup", None)

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

    def test_setup_ap_units_verify(self):
        import configparser
        import shutil
        import subprocess
        from pathlib import Path

        root = Path(__file__).parent.parent / "systemd"
        names = ("wqm1-setup-hostapd.service", "wqm1-setup-dnsmasq.service")
        for name in names:
            parser = configparser.ConfigParser(strict=False, interpolation=None)
            parser.optionxform = str
            parser.read(root / name)
            assert parser.get("Unit", "StartLimitIntervalSec") == "600"
            assert parser.get("Unit", "StartLimitBurst") == "8"
            assert parser.get("Service", "Restart") == "on-failure"
            assert "WantedBy" not in parser.sections()
        hostapd = (root / names[0]).read_text()
        dnsmasq = (root / names[1]).read_text()
        assert "ExecStart=/usr/sbin/hostapd /run/wqm1/hostapd.conf" in hostapd
        assert "hostapd -B" not in hostapd
        assert "ExecStart=/usr/sbin/dnsmasq --keep-in-foreground" in dnsmasq
        assert "BindsTo=wqm1-setup-hostapd.service" in dnsmasq
        exe = shutil.which("systemd-analyze")
        if not exe:
            return
        result = subprocess.run(
            [exe, "verify", *[str(root / name) for name in names]],
            capture_output=True,
            text=True,
            check=False,
        )
        combined = result.stdout + result.stderr
        assert "Unknown key" not in combined
        assert "StartLimitIntervalSec" not in combined

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


class TestJoinAfterTheHotspotLetsGo:
    """Field, 2026-10-21 (WQM-10001): the router was in the same room and the
    join still failed "not found". The hotspot had held wlan0, so
    NetworkManager's scan list was empty, and the join connected before any
    rescan. These tests use the realistic scan model."""

    def _join(self, nm, **kw):
        from utils.netctl import join_network

        t, clock, sleep = _clock()
        nm.ap = True
        nm.realistic_scan = True
        r = join_network(
            kw.pop("ssid", "PondHouse"),
            "hunter22",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=sleep,
            clock=clock,
        )
        return r, t

    def test_the_join_rescans_before_it_connects(self, nm):
        r, _ = self._join(nm)
        assert r["ok"] is True, r
        rescan = _at(nm.calls, lambda c: c[1:4] == ["device", "wifi", "rescan"])
        connect = _at(nm.calls, lambda c: c[1:4] == ["device", "wifi", "connect"])
        assert rescan < connect

    def test_a_scan_that_takes_a_few_tries_still_joins(self, nm):
        nm.rescans_before_seen = 3
        r, t = self._join(nm)
        assert r["ok"] is True, r
        assert t[0] < 20  # waited for the scan, did not burn the whole budget

    def test_a_network_missing_from_every_scan_is_tried_as_hidden(self, nm):
        nm.visible = []  # never in a scan: a hidden SSID, or a scan that keeps missing it
        r, _ = self._join(nm)
        assert r["ok"] is True, r
        assert any(c[1:4] == ["device", "wifi", "connect"] and "hidden" in c for c in nm.calls)

    def test_a_network_that_really_is_not_there_fails_and_restores_the_hotspot(self, nm):
        nm.visible = []
        nm.hidden_join_ok = False
        r, t = self._join(nm, ssid="Batcave")
        assert r["ok"] is False
        assert r["reason"] == "not_found"
        assert r["ap_restored"] is True
        assert nm.ap is True
        connects = [c for c in nm.calls if c[1:4] == ["device", "wifi", "connect"]]
        assert len(connects) == 3  # scan, rescan, hidden — then it gives up
        assert t[0] < 90  # bounded: the installer gets the hotspot back


class TestBootUsesTheSavedNetwork:
    """Field, 2026-10-21: a saved network written to WQM-10001's card was
    never used. Boot now asks again when NetworkManager is slow to answer,
    and brings the saved profile up itself."""

    def _boot(self, nm, tmp_path, monkeypatch, **kw):
        from utils.netctl import ensure_reachable

        t, clock, sleep = _clock()
        nm.saved = ["Batcave"]
        nm.visible = ["Batcave"]
        nm.realistic_scan = True
        r = ensure_reachable("WQM1-0001", "rain-delta-29", sleep=sleep, clock=clock, **kw)
        return r, t

    def test_a_saved_network_in_range_is_joined_not_the_hotspot(self, nm, tmp_path, monkeypatch):
        nm.station_up_rc = 0
        r, _ = self._boot(nm, tmp_path, monkeypatch)
        assert r["mode"] == "station", r
        assert not any(
            c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"] for c in nm.calls
        )
        rescan = _at(nm.calls, lambda c: c[1:4] == ["device", "wifi", "rescan"])
        up = _at(nm.calls, lambda c: c[1:4] == ["connection", "up", "Batcave"])
        assert rescan < up

    def test_networkmanager_slow_to_answer_still_gets_the_grace(self, nm, tmp_path, monkeypatch):
        nm.list_fails = 3  # the first three listings fail, as at a slow boot
        nm.station_up_rc = 0
        r, _ = self._boot(nm, tmp_path, monkeypatch)
        assert r["mode"] == "station", r

    def test_a_saved_network_that_fails_falls_back_to_the_hotspot_with_a_reason(
        self, nm, tmp_path, monkeypatch
    ):
        nm.visible = []  # out of range
        from utils.netctl import ensure_reachable

        t, clock, sleep = _clock()
        nm.saved = ["Batcave"]
        nm.realistic_scan = True
        r = ensure_reachable("WQM1-0001", "rain-delta-29", sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert r["station_failure"] == "network not found"
        assert t[0] < 170  # inside the oneshot's TimeoutStartSec=180

    def test_no_attempt_runs_past_the_grace_deadline(self, nm, tmp_path, monkeypatch):
        """Bugbot on #153 (110f345): an attempt started late in the grace
        could block JOIN_TIMEOUT_S past it, against TimeoutStartSec=180."""
        from utils import netctl
        from utils.netctl import ensure_reachable

        t, clock, sleep = _clock()
        nm.saved = ["Batcave"]
        nm.visible = ["Batcave"]
        nm.station_up_rc = 4  # every attempt fails, slowly
        starts: list[float] = []
        real_run = nm.run

        def slow_run(argv, timeout=15.0):
            if argv[1:4] == ["connection", "up", "Batcave"]:
                starts.append(t[0])
                t[0] += netctl.JOIN_TIMEOUT_S
            return real_run(argv, timeout)

        monkeypatch.setattr(netctl, "_run", slow_run)
        r = ensure_reachable("WQM1-0001", "rain-delta-29", sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert starts
        assert max(starts) - min(starts) + netctl.JOIN_TIMEOUT_S <= netctl.AP_GRACE_S


class TestRetryTheSavedNetwork:
    """bluesignal-wifi-retry.timer: the router came back after the Pi did.
    The retry may only act when the hotspot is idle."""

    def _retry(self, nm, monkeypatch, tmp_path):
        from utils.netctl import retry_saved_station

        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        t, clock, sleep = _clock()
        return retry_saved_station("WQM1-0001", "rain-delta-29", sleep=sleep, clock=clock), t

    def test_an_idle_hotspot_gives_way_to_the_saved_network(self, nm, monkeypatch, tmp_path):
        nm.ap = True
        nm.saved = ["Batcave"]
        nm.visible = ["Batcave"]
        nm.realistic_scan = True
        nm.station_up_rc = 0
        r, _ = self._retry(nm, monkeypatch, tmp_path)
        assert r["mode"] == "station", r
        assert nm.ap is False

    def test_never_while_a_phone_is_on_the_hotspot(self, nm, monkeypatch, tmp_path):
        nm.ap = True
        nm.saved = ["Batcave"]
        nm.ap_station_dump = "Station 3a:1f:00:11:22:33 (on wlan0)\n\tinactive time: 10 ms\n"
        r, _ = self._retry(nm, monkeypatch, tmp_path)
        assert r == {"skipped": "a phone is on the hotspot"}
        assert nm.ap is True
        assert not any(
            c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"] for c in nm.calls
        )

    def test_nothing_saved_means_nothing_happens(self, nm, monkeypatch, tmp_path):
        nm.ap = True
        r, _ = self._retry(nm, monkeypatch, tmp_path)
        assert r == {"skipped": "no saved network"}
        assert nm.ap is True

    def test_a_network_still_away_brings_the_hotspot_back(self, nm, monkeypatch, tmp_path):
        nm.ap = True
        nm.saved = ["Batcave"]
        nm.visible = []
        nm.realistic_scan = True
        r, t = self._retry(nm, monkeypatch, tmp_path)
        assert r["mode"] == "ap" and r["ap"] is True
        assert r["station_failure"] == "network not found"
        assert nm.ap is True
        assert t[0] < 150  # inside the unit's TimeoutStartSec
        assert "station: fail: network not found" in (tmp_path / "wqm1-status.txt").read_text()

    def test_a_phone_that_joins_during_the_checks_is_not_cut_off(self, nm, monkeypatch, tmp_path):
        """Claude review on #153 (1baf59c): a phone that joined between the
        client count and stop_ap lost the hotspot."""
        from utils import netctl

        counts = iter([0, 1])
        monkeypatch.setattr(netctl, "ap_clients", lambda iface="wlan0": next(counts))
        nm.ap = True
        nm.saved = ["Batcave"]
        nm.visible = ["Batcave"]
        nm.station_up_rc = 0
        r, _ = self._retry(nm, monkeypatch, tmp_path)
        assert r == {"skipped": "a phone is on the hotspot"}
        assert nm.ap is True
        assert not any(
            c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"] for c in nm.calls
        )

    def test_an_error_right_after_stop_ap_still_restores(self, nm, monkeypatch, tmp_path):
        """Claude review on #153 (209d29a): stop_ap ran before the try, so a
        SIGTERM or error just after it skipped the restore."""
        from utils import netctl

        def broken_disarm(*_a, **_k):
            raise SystemExit(0)  # what the script's SIGTERM handler raises

        monkeypatch.setattr(netctl, "_disarm_portal", broken_disarm)
        nm.ap = True
        nm.saved = ["Batcave"]
        with pytest.raises(SystemExit):
            self._retry(nm, monkeypatch, tmp_path)
        assert nm.ap is True

    def test_a_moved_card_does_not_take_the_old_sites_wifi(self, nm, monkeypatch, tmp_path):
        """Bugbot on #153 (110f345): boot held the old site's profile on a
        moved card, and the retry timer joined it anyway."""
        from utils import netctl

        monkeypatch.setattr(netctl, "wifi_reconfirm_required", lambda: True)
        nm.ap = True
        nm.saved = ["Batcave"]
        nm.visible = ["Batcave"]
        nm.station_up_rc = 0
        r, _ = self._retry(nm, monkeypatch, tmp_path)
        assert r == {"skipped": "card moved; setup must confirm the site network"}
        assert nm.ap is True
        assert not any(c[1:3] == ["connection", "up"] for c in nm.calls)

    def test_the_retry_stays_inside_its_unit_timeout(self, nm, monkeypatch, tmp_path):
        """Bugbot on #153 (110f345): two saved networks that associate and
        never get an address ran past TimeoutStartSec=150, and systemd killed
        the retry with the hotspot down."""
        from utils import netctl
        from utils.netctl import retry_saved_station

        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        t, clock, sleep = _clock()
        nm.ap = True
        nm.saved = ["Batcave", "Batcave-5G"]
        nm.visible = ["Batcave", "Batcave-5G"]
        nm.station_up_rc = 0
        nm.ipv4 = "169.254.7.7"  # associated, no lease
        ups = []
        real_run = nm.run

        def slow_run(argv, timeout=15.0):
            if argv[1:3] == ["connection", "up"] and argv[3] in nm.saved:
                ups.append(argv[3])
                t[0] += netctl.JOIN_TIMEOUT_S
            return real_run(argv, timeout)

        monkeypatch.setattr(netctl, "_run", slow_run)
        r = retry_saved_station("WQM1-0001", "rain-delta-29", sleep=sleep, clock=clock)
        assert r["mode"] == "ap" and r["ap"] is True
        assert ups == ["Batcave"]
        assert t[0] < 150
        assert nm.ap is True

    def test_an_error_mid_retry_still_brings_the_hotspot_back(self, nm, monkeypatch, tmp_path):
        """Bugbot on #153 (110f345): an exception after stop_ap left the unit
        with no hotspot until a reboot."""
        from utils import netctl

        def broken_wait(*_a, **_k):
            raise RuntimeError("iw went away")

        monkeypatch.setattr(netctl, "wait_for_ssid", broken_wait)
        nm.ap = True
        nm.saved = ["Batcave"]
        with pytest.raises(RuntimeError):
            self._retry(nm, monkeypatch, tmp_path)
        assert nm.ap is True


def test_systemds_sigterm_lets_the_retry_bring_the_hotspot_back(monkeypatch):
    """Bugbot on #153 (110f345): systemd's SIGTERM at TimeoutStartSec ended
    the retry with no cleanup. The script turns it into SystemExit, which
    runs the retry's own restore."""
    import importlib.util
    import os
    import signal
    from pathlib import Path

    from utils import netctl

    path = Path(__file__).resolve().parent.parent / "scripts" / "wqm1-ap-fallback.py"
    spec = importlib.util.spec_from_file_location("wqm1_ap_fallback_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cleaned = []

    def killed_mid_retry(*_a, **_k):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            cleaned.append(True)

    monkeypatch.setattr(module, "resolve_credentials", lambda: ("WQM1-0001", "rain-delta-29"))
    monkeypatch.setattr(netctl, "retry_saved_station", killed_mid_retry)
    previous = signal.getsignal(signal.SIGTERM)
    # Without the script's handler this one gets the signal and nothing is
    # raised, so the test fails instead of killing the test run.
    signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        with pytest.raises(SystemExit) as stopped:
            module.retry()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert stopped.value.code == 0
    assert cleaned == [True]


def test_a_sigterm_during_the_restore_waits_for_the_hotspot(nm, monkeypatch, tmp_path):
    """Claude review on #153 (2910740): a SIGTERM that lands while the retry
    is already restoring the hotspot must not cut the restore short."""
    import os
    import signal

    from utils import netctl
    from utils.netctl import retry_saved_station

    monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
    monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
    nm.ap = True
    nm.saved = ["Batcave"]
    nm.visible = []
    nm.realistic_scan = True
    finished = []
    real_restore = netctl._restore_ap

    def restore_then_finish(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)  # systemd's stop, mid-restore
        result = real_restore(*args, **kwargs)
        finished.append(result)
        return result

    def stop(_signum, _frame):
        raise SystemExit(0)

    monkeypatch.setattr(netctl, "_restore_ap", restore_then_finish)
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        t, clock, sleep = _clock()
        with pytest.raises(SystemExit):
            retry_saved_station("WQM1-0001", "rain-delta-29", sleep=sleep, clock=clock)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert finished == [True]
    assert nm.ap is True
    assert "hotspot: up" in (tmp_path / "wqm1-status.txt").read_text()


def test_the_sigterm_hold_leaves_a_c_handler_alone(monkeypatch):
    """Claude review on #153 (5e946a4): getsignal returns None for a handler
    installed from C, and signal.signal(SIGTERM, None) raises TypeError."""
    import signal

    from utils import netctl

    set_calls = []
    monkeypatch.setattr(signal, "getsignal", lambda _signum: None)
    monkeypatch.setattr(signal, "signal", lambda *a: set_calls.append(a))
    ran = []
    with netctl._sigterm_held():
        ran.append(True)
    assert ran == [True]
    assert set_calls == []


def test_the_retry_timer_is_installed_and_enabled():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    setup = (root / "setup.sh").read_text()
    assert 'systemd/bluesignal-wifi-retry.timer" /etc/systemd/system/' in setup
    assert 'systemd/bluesignal-wifi-retry.service" /etc/systemd/system/' in setup
    assert "systemctl enable bluesignal-wifi-retry.timer" in setup
    unit = (root / "systemd" / "bluesignal-wifi-retry.service").read_text()
    assert "wqm1-ap-fallback.py --retry" in unit
