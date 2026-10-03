"""
Site flow v2 (2026-10-03): the phone joins the unit, one pass, no typing.

What is pinned here, in the order an installer meets it:

1. The captive portal: a phone joining the setup hotspot lands on the setup
   page by itself (probe paths + foreign host names redirect).
2. On the hotspot, during FIRST setup only, the factory-PIN prompt is
   skipped — the WPA2 passphrase on the card is the proof of presence.
3. A carded unit (identity + cloud key from the bench) skips the identity
   and cloud-key steps.
4. The network step is last, and a successful join finishes setup on the
   unit: other saved Wi-Fi forgotten, setup recorded.
5. The virtual radio (``utils.netsim``) behaves like the real one and is the
   only radio a simulated unit can reach.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.test_service_window_setup import make_app


@pytest.fixture
def virtual_net(tmp_path, monkeypatch):
    path = tmp_path / "netsim.json"
    monkeypatch.setenv("WQM1_VIRTUAL_NET", str(path))
    from service_window import captive

    captive.reset_cache()
    return path


def _carded_identity(tmp_path, monkeypatch):
    ident = tmp_path / "bluesignal-identity.json"
    ident.write_text(
        json.dumps(
            {
                "serial": "WQM-10001",
                "dev_eui": "FEFFFF0000000001",
                "ap_passphrase": "cedar-river-42",
            }
        )
    )
    monkeypatch.setenv("BLUESIGNAL_IDENTITY_FILE", str(ident))
    return ident


# ── 1. Captive portal ────────────────────────────────────────────────────────


class TestCaptivePortal:
    @pytest.mark.parametrize(
        "path", ["/hotspot-detect.html", "/generate_204", "/connecttest.txt", "/ncsi.txt"]
    )
    def test_probe_paths_never_answer_success(self, tmp_path, mock_hardware, path, monkeypatch):
        """A 200 'Success' or a 204 is what tells a phone to SKIP the portal."""
        from service_window import captive

        monkeypatch.setattr(captive, "hotspot_up", lambda: True)
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            r = c.get(path)
        assert r.status_code == 302
        assert r.headers["Location"] == "http://192.168.4.1:8080/setup/"

    def test_foreign_host_on_the_hotspot_goes_to_setup(self, tmp_path, mock_hardware, monkeypatch):
        from service_window import captive

        monkeypatch.setattr(captive, "hotspot_up", lambda: True)
        app = make_app(tmp_path, mock_hardware, pin="8642", setup_completed=True)
        with app.test_client() as c:
            r = c.get("/", headers={"Host": "captive.apple.com"})
        assert r.status_code == 302
        assert r.headers["Location"] == "http://192.168.4.1:8080/setup/"

    @pytest.mark.parametrize("host", ["192.168.4.1:8080", "wqm1-0001.local:8080", "localhost"])
    def test_own_host_names_are_left_alone(self, tmp_path, mock_hardware, monkeypatch, host):
        from service_window import captive

        monkeypatch.setattr(captive, "hotspot_up", lambda: True)
        app = make_app(tmp_path, mock_hardware, pin="8642", setup_completed=True)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            r = c.get("/", headers={"Host": host})
        assert not (
            r.status_code == 302 and "192.168.4.1:8080/setup" in r.headers.get("Location", "")
        )

    def test_foreign_host_is_ignored_when_the_hotspot_is_down(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        """On the customer's LAN a DNS name must never bounce to 192.168.4.1."""
        from service_window import captive

        monkeypatch.setattr(captive, "hotspot_up", lambda: False)
        app = make_app(tmp_path, mock_hardware, pin="8642", setup_completed=True)
        with app.test_client() as c:
            r = c.get("/", headers={"Host": "wqm1.example.net"})
        assert "192.168.4.1" not in r.headers.get("Location", "")

    def test_captive_rule_matches_only_hotspot_traffic(self):
        from utils import netctl

        rule = netctl._captive_rule(8080, "wlan0")
        assert rule[rule.index("-s") + 1] == "192.168.4.0/24"
        assert rule[rule.index("-d") + 1] == "192.168.4.1"
        assert rule[rule.index("--dport") + 1] == "80"
        assert rule[rule.index("--to-ports") + 1] == "8080"

    def test_ap_fallback_installs_the_rule_every_boot(self):
        src = Path("scripts/wqm1-ap-fallback.py").read_text()
        assert "ensure_captive_redirect(service_window_port())" in src
        assert src.index("ensure_captive_redirect(") < src.index("ensure_reachable(ssid")

    def test_setup_sh_writes_the_dns_half(self):
        src = Path("setup.sh").read_text()
        assert "/etc/NetworkManager/dnsmasq-shared.d/wqm1-captive.conf" in src
        assert "address=/#/192.168.4.1" in src
        assert "dhcp-option=114" in src


# ── 2. No factory-PIN prompt on the hotspot during first setup ───────────────


class TestHotspotSkipsTheFactoryPin:
    def _app(self, tmp_path, mock_hardware, monkeypatch, on_hotspot, **kw):
        from service_window import captive

        monkeypatch.setattr(captive, "from_hotspot", lambda: on_hotspot)
        return make_app(tmp_path, mock_hardware, **kw)

    def test_first_setup_on_the_hotspot_opens_without_a_pin(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        app = self._app(tmp_path, mock_hardware, monkeypatch, True)
        with app.test_client() as c:
            r = c.get("/setup/")
        assert r.status_code == 200
        assert b"set this unit up" in r.data

    def test_off_the_hotspot_the_pin_is_still_asked(self, tmp_path, mock_hardware, monkeypatch):
        app = self._app(tmp_path, mock_hardware, monkeypatch, False)
        with app.test_client() as c:
            r = c.get("/setup/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_exemption_ends_once_the_factory_pin_is_gone(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        app = self._app(
            tmp_path, mock_hardware, monkeypatch, True, pin="8642", setup_completed=True
        )
        with app.test_client() as c:
            r = c.get("/setup/network")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_setting_the_pin_keeps_the_installer_signed_in(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        app = self._app(tmp_path, mock_hardware, monkeypatch, True)
        with app.test_client() as c:
            r = c.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
            assert r.status_code == 302
            # The factory PIN is gone, so the hotspot exemption no longer
            # applies — the session carries them on.
            assert c.get("/setup/sensors").status_code == 200


# ── 3. A carded unit skips identity and cloud key ────────────────────────────


class TestCardedUnit:
    def test_carded_unit_goes_pin_sensors_network(self, tmp_path, mock_hardware, monkeypatch):
        _carded_identity(tmp_path, monkeypatch)
        app = make_app(
            tmp_path, mock_hardware, config_extra={"api_key": "k" * 40, "cloud_enabled": True}
        )
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            r = c.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
            assert r.headers["Location"].endswith("/setup/sensors")
            assert c.get("/setup/identity").headers["Location"].endswith("/setup/sensors")
            assert c.get("/setup/cloud").headers["Location"].endswith("/setup/sensors")
            page = c.get("/setup/").data
        assert b"already knows who it" in page

    def test_card_without_a_cloud_key_still_asks_for_it(self, tmp_path, mock_hardware, monkeypatch):
        _carded_identity(tmp_path, monkeypatch)
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            r = c.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
        assert r.headers["Location"].endswith("/setup/identity")


# ── 4. The join finishes setup; the bench network does not follow the unit ──


class TestJoinFinishesSetup:
    def _client(self, tmp_path, mock_hardware, virtual_net, **kw):
        app = make_app(tmp_path, mock_hardware, pin="8642", **kw)
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["pin_verified"] = True
        return c, app

    def _cfg(self, app):
        return yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())

    def test_right_password_finishes_and_forgets_the_bench(
        self, tmp_path, mock_hardware, virtual_net
    ):
        from utils import netctl, netsim

        netctl.start_ap("WQM1-0001", "cedar-river-42")
        c, app = self._client(tmp_path, mock_hardware, virtual_net)
        page = c.get("/setup/network").data
        assert netsim.DEMO_HOME_SSID.encode() in page
        r = c.post(
            "/setup/network",
            data={
                "action": "join",
                "ssid": netsim.DEMO_HOME_SSID,
                "password": netsim.DEMO_HOME_PASSWORD,
            },
        )
        assert b"Setup is finished" in r.data
        state = json.loads(virtual_net.read_text())
        assert state["station"] == netsim.DEMO_HOME_SSID
        assert state["saved"] == [netsim.DEMO_HOME_SSID]  # BlueSignal-Shop forgotten
        assert state["ap"] is False
        assert self._cfg(app)["service_window"]["setup_completed"] is True

    def test_wrong_password_brings_the_hotspot_back_and_says_why(
        self, tmp_path, mock_hardware, virtual_net
    ):
        from utils import netctl, netsim

        netctl.start_ap("WQM1-0001", "cedar-river-42")
        c, app = self._client(tmp_path, mock_hardware, virtual_net)
        c.post(
            "/setup/network",
            data={"action": "join", "ssid": netsim.DEMO_HOME_SSID, "password": "nope"},
        )
        state = json.loads(virtual_net.read_text())
        assert state["ap"] is True and state["station"] is None
        assert netsim.BENCH_SSID in state["saved"]  # nothing forgotten on a failure
        assert b"wrong password" in c.get("/setup/network").data
        assert not (self._cfg(app).get("service_window") or {}).get("setup_completed")

    def test_no_network_site_forgets_every_saved_wifi(self, tmp_path, mock_hardware, virtual_net):
        from utils import netsim

        c, app = self._client(tmp_path, mock_hardware, virtual_net)
        r = c.post("/setup/network", data={"action": "declare", "backhaul": "none"})
        assert r.headers["Location"].endswith("/setup/done")
        c.post("/setup/done", data={})
        state = json.loads(virtual_net.read_text())
        assert state["saved"] == []
        assert netsim.BENCH_SSID not in state["saved"]
        assert self._cfg(app)["service_window"]["setup_completed"] is True

    def test_keep_finishes_on_the_network_already_in_use(
        self, tmp_path, mock_hardware, virtual_net
    ):
        from utils import netctl, netsim

        netctl.start_ap("WQM1-0001", "cedar-river-42")
        netctl.join_network(netsim.DEMO_HOME_SSID, netsim.DEMO_HOME_PASSWORD)
        c, app = self._client(tmp_path, mock_hardware, virtual_net)
        r = c.post("/setup/network", data={"action": "keep"})
        assert b"Joined Smith-Home" in r.data
        assert json.loads(virtual_net.read_text())["saved"] == [netsim.DEMO_HOME_SSID]


# ── 5. The virtual radio ─────────────────────────────────────────────────────


class TestVirtualRadio:
    def test_netctl_never_shells_out_for_a_virtual_unit(self, virtual_net, monkeypatch):
        from utils import netctl

        def boom(*_a, **_k):
            raise AssertionError("a simulated unit reached a real subprocess")

        monkeypatch.setattr(netctl, "_run", boom)
        assert netctl.start_ap("WQM1-0001", "cedar-river-42")["ok"]
        assert netctl.ap_active()
        assert netctl.scan_networks()
        netctl.join_network("Smith-Home", "riverstone42", "WQM1-0001", "cedar-river-42")
        assert netctl.station_connected()
        assert netctl.forget_saved_wifi("Smith-Home")["forgotten"] == ["BlueSignal-Shop"]
        assert netctl.ensure_captive_redirect(8080)["virtual"] is True

    def test_open_network_needs_no_password(self, virtual_net):
        from utils import netsim

        assert netsim.join_network("xfinitywifi", None)["connected"]

    def test_unknown_network_fails_and_restores_the_ap(self, virtual_net):
        from utils import netsim

        netsim.start_ap("WQM1-0001", "cedar-river-42")
        out = netsim.join_network("Nowhere", "x", "WQM1-0001", "cedar-river-42")
        assert not out["connected"] and out["ap_restored"]
        assert netsim.ap_active()

    def test_wifi_status_reports_the_virtual_radio(self, virtual_net):
        from utils import netinfo, netsim

        assert netinfo.wifi_status()["ssid"] is None
        netsim.join_network("Smith-Home", "riverstone42")
        status = netinfo.wifi_status()
        assert status["ssid"] == "Smith-Home"
        assert status["rssi_dbm"] is not None


class TestForgetOnRealNetworkManager:
    """``forget_saved_wifi`` against a scripted nmcli: deletes by profile
    name, compares by SSID, never touches the setup AP's profile."""

    def test_deletes_all_but_the_kept_ssid(self, monkeypatch):
        from utils import netctl

        calls: list[list[str]] = []

        def run(argv, timeout=15.0):
            calls.append(argv)
            a = argv[1:]
            if a[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                return (
                    0,
                    (
                        "Shop Bench:802-11-wireless\n"
                        "wqm1-setup-ap:802-11-wireless\n"
                        "Smith-Home:802-11-wireless\n"
                        "Wired connection 1:802-3-ethernet"
                    ),
                    "",
                )
            if a[:2] == ["-g", "802-11-wireless.ssid"]:
                return 0, {"Shop Bench": "BlueSignal-Shop", "Smith-Home": "Smith-Home"}[a[-1]], ""
            return 0, "", ""

        monkeypatch.setattr(netctl, "_run", run)
        out = netctl.forget_saved_wifi("Smith-Home")
        assert out["forgotten"] == ["BlueSignal-Shop"]
        deletes = [c for c in calls if c[1:3] == ["connection", "delete"]]
        assert deletes == [["nmcli", "connection", "delete", "Shop Bench"]]


class TestDemoUnit:
    """scripts/demo-unit.py — the unit a browser bot commissions."""

    def _mod(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "demo_unit", Path(__file__).parent.parent / "scripts" / "demo-unit.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_boots_factory_fresh_at_a_site_with_no_known_network(self, tmp_path):
        mod = self._mod()
        unit = mod.build_unit(tmp_path, 8080, 7)
        net = json.loads(unit["netsim"].read_text())
        assert net["ap"] is True and net["station"] is None
        assert "BlueSignal-Shop" in net["saved"]
        assert len(unit["ap_passphrase"]) >= 8  # WPA2 minimum, so card and unit agree
        cfg = yaml.safe_load(unit["config"].read_text())
        assert cfg["service_window"]["pin"] == "1234"
        assert cfg["cloud_api_base"].startswith("http://127.0.0.1")

    def test_cloud_is_never_production(self):
        mod = self._mod()
        assert mod.DEAD_CLOUD.startswith("http://127.0.0.1")
        assert "cloudfunctions.net" not in Path("scripts/demo-unit.py").read_text()

    def test_box_label_link_targets_cloud_demo_mode(self):
        mod = self._mod()
        url = mod.demo_claim_url("SIM-WQM1-00001", "demo" + "0" * 28)
        assert url.startswith("https://cloud.bluesignal.xyz/claim/SIM-WQM1-00001?demo=1#t=")


class TestProbePageAgreesWithItself:
    """The checkbox and the health card answered "is a flow meter fitted?"
    differently for a unit whose config never mentioned one: ticked above,
    "not installed" below. Opt-in probes are unticked when absent."""

    def _page(self, tmp_path, mock_hardware, **extra):
        app = make_app(tmp_path, mock_hardware, pin="8642", config_extra=extra or None)
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            return c.get("/setup/sensors").data.decode()

    def test_absent_flow_is_unticked_and_listed_as_not_fitted(self, tmp_path, mock_hardware):
        page = self._page(tmp_path, mock_hardware)
        assert 'name="flow_pulse_enabled" >' in page  # unticked
        assert "Not fitted on this unit:" in page and "flow meter" in page
        assert 'name="ph_enabled" checked' in page  # core four still default fitted

    def test_declared_flow_is_ticked_and_gets_a_card(self, tmp_path, mock_hardware):
        page = self._page(tmp_path, mock_hardware, flow_pulse_enabled=True)
        assert 'name="flow_pulse_enabled" checked' in page
        assert "flow meter" not in page.split("Not fitted on this unit:")[-1].split("</p>")[0]


# ── Scenario matrix (founder 2026-10-03: "test for failures … a variety of
#    scenarios"). Each test is one thing that happens at a real site. ─────────


def _seed_networks(path, extra):
    """Start the virtual radio at a site: hotspot up, the shop network saved,
    plus whatever networks this scenario needs in range."""
    from utils import netsim

    state = netsim._default_state()
    state.update(ap=True, ap_ssid="WQM1-0001")
    state["networks"] += extra
    path.write_text(json.dumps(state))


class TestSiteScenarios:
    def _client(self, tmp_path, mock_hardware, pin="8642", **kw):
        app = make_app(tmp_path, mock_hardware, pin=pin, **kw)
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["pin_verified"] = True
        return c, app

    def _done(self, app):
        cfg = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        return bool((cfg.get("service_window") or {}).get("setup_completed"))

    def test_hidden_network_typed_by_hand_that_is_not_there(
        self, tmp_path, mock_hardware, virtual_net
    ):
        _seed_networks(virtual_net, [])
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "join", "ssid": "Barn-5G", "password": "x" * 10})
        page = c.get("/setup/network").data
        assert b"No network named Barn-5G is in range" in page
        assert json.loads(virtual_net.read_text())["ap"] is True
        assert not self._done(app)

    def test_open_network_with_a_blank_password(self, tmp_path, mock_hardware, virtual_net):
        _seed_networks(virtual_net, [])
        c, app = self._client(tmp_path, mock_hardware)
        r = c.post("/setup/network", data={"action": "join", "ssid": "xfinitywifi", "password": ""})
        assert b"Joined xfinitywifi" in r.data
        assert self._done(app)

    def test_names_and_passwords_with_awkward_characters(
        self, tmp_path, mock_hardware, virtual_net
    ):
        ssid = 'O\'Brien "Home": 2.4G'
        password = 'p;a,s:s"w\\o rd!'
        _seed_networks(
            virtual_net, [{"ssid": ssid, "signal": 66, "secured": True, "password": password}]
        )
        c, app = self._client(tmp_path, mock_hardware)
        page = c.get("/setup/network").data.decode()
        assert "O&#39;Brien &#34;Home&#34;: 2.4G" in page  # escaped, not broken HTML
        r = c.post("/setup/network", data={"action": "join", "ssid": ssid, "password": password})
        assert b"Setup is finished" in r.data
        assert json.loads(virtual_net.read_text())["station"] == ssid

    def test_longest_wpa2_password(self, tmp_path, mock_hardware, virtual_net):
        password = "A1" * 31 + "Z"  # 63 characters, the WPA2 maximum
        _seed_networks(
            virtual_net, [{"ssid": "LongPass", "signal": 70, "secured": True, "password": password}]
        )
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "join", "ssid": "LongPass", "password": password})
        assert self._done(app)

    def test_wrong_then_right_clears_the_old_error(self, tmp_path, mock_hardware, virtual_net):
        from utils import netsim

        _seed_networks(virtual_net, [])
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "join", "ssid": "Smith-Home", "password": "nope"})
        assert "LAST_JOIN" in app.config
        c.post(
            "/setup/network",
            data={"action": "join", "ssid": "Smith-Home", "password": netsim.DEMO_HOME_PASSWORD},
        )
        assert "LAST_JOIN" not in app.config
        assert b"Could not join" not in c.get("/setup/network").data

    def test_three_wrong_passwords_never_strand_the_installer(
        self, tmp_path, mock_hardware, virtual_net
    ):
        _seed_networks(virtual_net, [])
        c, app = self._client(tmp_path, mock_hardware)
        for attempt in ("one", "two", "three"):
            c.post(
                "/setup/network", data={"action": "join", "ssid": "Smith-Home", "password": attempt}
            )
            assert json.loads(virtual_net.read_text())["ap"] is True, attempt
        assert not self._done(app)

    def test_lte_site_forgets_every_wifi_and_records_lte(
        self, tmp_path, mock_hardware, virtual_net
    ):
        _seed_networks(virtual_net, [])
        c, app = self._client(tmp_path, mock_hardware)
        r = c.post("/setup/network", data={"action": "declare", "backhaul": "lte"})
        assert r.headers["Location"].endswith("/setup/done")
        c.post("/setup/done", data={})
        cfg = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert cfg["backhaul"] == "lte"
        assert json.loads(virtual_net.read_text())["saved"] == []
        assert self._done(app)

    def test_declare_without_choosing_changes_nothing(self, tmp_path, mock_hardware, virtual_net):
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "declare"})
        cfg = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert "backhaul" not in cfg
        assert not self._done(app)

    @pytest.mark.parametrize("ssid", ["", "   ", "bad\x07name", "x" * 33])
    def test_unusable_network_names_never_touch_the_radio(
        self, tmp_path, mock_hardware, monkeypatch, ssid
    ):
        from utils import netctl

        called = []
        monkeypatch.setattr(netctl, "join_network", lambda *a, **k: called.append(a))
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "join", "ssid": ssid, "password": "whatever1"})
        assert called == []
        assert not self._done(app)

    def test_failed_join_with_no_hotspot_to_restore_still_says_why(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        from utils import netctl

        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *a, **k: {
                "ok": False,
                "connected": False,
                "ap_restored": False,
                "error": "radio busy",
            },
        )
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "cedar-river-42"))
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "join", "ssid": "Smith-Home", "password": "x" * 9})
        assert b"radio busy" in c.get("/setup/network").data
        assert not self._done(app)

    def test_a_profile_that_will_not_delete_does_not_block_finishing(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        """polkit refusing `nmcli connection delete` must not leave a unit
        half set up on the owner's network."""
        from utils import netctl

        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *a, **k: {"ok": True, "connected": True, "ap_restored": False, "error": None},
        )
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "cedar-river-42"))
        monkeypatch.setattr(
            netctl,
            "forget_saved_wifi",
            lambda keep, iface="wlan0": {
                "ok": False,
                "forgotten": [],
                "failed": ["BlueSignal-Shop"],
                "kept": keep,
            },
        )
        c, app = self._client(tmp_path, mock_hardware)
        r = c.post(
            "/setup/network", data={"action": "join", "ssid": "Smith-Home", "password": "x" * 9}
        )
        assert b"Setup is finished" in r.data
        assert b"The unit forgot" not in r.data  # never claims a forget that failed
        assert self._done(app)

    def test_keep_is_refused_while_only_the_hotspot_is_up(
        self, tmp_path, mock_hardware, virtual_net
    ):
        _seed_networks(virtual_net, [])
        c, app = self._client(tmp_path, mock_hardware)
        c.post("/setup/network", data={"action": "keep"})
        assert not self._done(app)

    def test_android_and_windows_probes_open_the_setup_page_too(
        self, tmp_path, mock_hardware, virtual_net
    ):
        _seed_networks(virtual_net, [])
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            for path in ("/generate_204", "/gen_204", "/connecttest.txt", "/redirect"):
                r = c.get(path)
                assert r.status_code == 302 and r.headers["Location"] == "/setup/", path

    def test_a_virtual_unit_behind_a_tunnel_never_redirect_loops(
        self, tmp_path, mock_hardware, virtual_net
    ):
        """A browser bot reaches the demo unit at a tunnel host name. The
        foreign-host rule must not bounce it to 192.168.4.1."""
        _seed_networks(virtual_net, [])
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            r = c.get("/setup/", headers={"Host": "demo-unit.trycloudflare.com"})
        assert r.status_code == 200


class TestNetworkModuleFailures:
    def test_captive_rule_without_iptables_is_logged_not_raised(self, monkeypatch):
        from utils import netctl

        monkeypatch.setattr(
            netctl, "_run", lambda argv, timeout=15.0: (127, "", "iptables not found")
        )
        out = netctl.ensure_captive_redirect(8080)
        assert out["ok"] is False and "iptables" in out["error"]

    def test_captive_rule_already_present_is_not_added_twice(self, monkeypatch):
        from utils import netctl

        calls = []

        def run(argv, timeout=15.0):
            calls.append(argv)
            return (0, "", "")  # -C says the rule exists

        monkeypatch.setattr(netctl, "_run", run)
        assert netctl.ensure_captive_redirect(8080)["already"] is True
        assert not any("-A" in c for c in calls)

    def test_corrupt_virtual_state_falls_back_to_the_demo_scene(self, virtual_net):
        from utils import netsim

        virtual_net.write_text("{not json")
        assert netsim.saved_wifi() == [netsim.BENCH_SSID]
        assert netsim.scan_networks()[0]["ssid"] == netsim.DEMO_HOME_SSID

    def test_forget_with_nothing_saved_is_a_clean_no_op(self, monkeypatch):
        from utils import netctl

        monkeypatch.setattr(netctl, "_run", lambda argv, timeout=15.0: (0, "", ""))
        assert netctl.forget_saved_wifi("Smith-Home") == {
            "ok": True,
            "forgotten": [],
            "failed": [],
            "kept": "Smith-Home",
        }
