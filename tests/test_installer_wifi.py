"""Installer setup: 2.4 GHz, signal bars, network types, passwords, probes."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.test_netctl import FakeNm
from tests.test_service_window_setup import make_app
from utils.wifi_setup import (
    BAND_MESSAGE,
    CAPTIVE_JOIN_MESSAGE,
    TRAILING_SPACE_WARNING,
    WEAK_RSSI_DBM,
    WEAK_SIGNAL_MESSAGE,
    annotate_network,
    band_notice,
    bars_for_dbm,
    block_reason,
    classify_nmcli_security,
    classify_probe,
    find_network,
    format_station_rssi,
    interpret_sign_in_response,
    is_weak_rssi,
    normalize_password,
    parse_iw_link,
    parse_iw_scan,
    parse_nmcli_wifi,
    password_problem,
    probe_rows,
    unconfirmed_missing,
    unseen_ssid_notice,
    wifi_connect_argv,
)

IW_MIXED = """
BSS 00:11:22:33:44:55(on wlan0)
    freq: 2412
    signal: -42.00 dBm
    SSID: Office
    RSN:
         * Authentication suites: IEEE 802.1X
BSS 00:11:22:33:44:66(on wlan0)
    freq: 2437
    signal: -50.00 dBm
    SSID: Home
    RSN:
         * Authentication suites: SAE
         * Authentication suites: PSK
BSS 00:11:22:33:44:77(on wlan0)
    freq: 2462
    signal: -55.00 dBm
    SSID: Only3
    RSN:
         * Authentication suites: SAE
BSS 00:11:22:33:44:88(on wlan0)
    freq: 5180
    signal: -40.00 dBm
    SSID: Home-5G
    RSN:
         * Authentication suites: PSK
BSS 00:11:22:33:44:99(on wlan0)
    freq: 2412
    signal: -80.00 dBm
    SSID:
    RSN:
         * Authentication suites: PSK
BSS 00:11:22:33:44:aa(on wlan0)
    freq: 2412
    signal: -90.00 dBm
    SSID: Cafe
BSS 00:11:22:33:44:bb(on wlan0)
    freq: 2412
    signal: -71.00 dBm
    SSID: Barn
    capability: ESS Privacy
"""

NMCLI_MIXED = "\n".join(
    [
        "Office:80:WPA2 802.1X:2412 MHz",
        "Only3:70:WPA3:2437 MHz",
        "Home:60:WPA2 WPA3:2462 MHz",
        "Cafe:30:--:2412 MHz",
        "Home-5G:90:WPA2:5180 MHz",
        "Guest\\:2.4:55:WPA2:2412 MHz",
        ":15:WPA2:2437 MHz",
    ]
)


def _by_ssid(rows: list[dict], ssid: str) -> dict:
    found = find_network(rows, ssid)
    assert found is not None, ssid
    return found


class TestBand:
    def test_iw_scan_separates_2_4_from_5_ghz(self):
        rows = parse_iw_scan(IW_MIXED)
        assert _by_ssid(rows, "Home")["band"] == "2.4"
        assert _by_ssid(rows, "Home")["freq_mhz"] == 2437
        five = _by_ssid(rows, "Home-5G")
        assert five["band"] == "5"
        assert five["freq_mhz"] == 5180
        assert five["joinable"] is False
        assert block_reason(five) == BAND_MESSAGE

    def test_no_2_4_network_uses_the_installer_sentence(self):
        only_five = parse_iw_scan(
            "BSS aa(on wlan0)\n\tfreq: 5180\n\tsignal: -40.00 dBm\n\tSSID: Only5\n\tRSN:\n"
        )
        assert band_notice(only_five) == BAND_MESSAGE
        assert band_notice([]) == BAND_MESSAGE
        mixed = parse_iw_scan(IW_MIXED)
        assert band_notice(mixed) is None

    def test_a_typed_name_that_was_not_seen_says_2_4_only(self):
        rows = parse_iw_scan(IW_MIXED)
        assert unseen_ssid_notice(rows, "Home") is None
        assert unseen_ssid_notice(rows, "Upstairs-5G") == BAND_MESSAGE
        assert unseen_ssid_notice(rows, "Home-5G") == BAND_MESSAGE

    def test_hidden_ssid_is_kept(self):
        rows = parse_iw_scan(IW_MIXED)
        hidden = [row for row in rows if row["hidden"]]
        assert len(hidden) == 1
        assert hidden[0]["ssid"] == ""
        assert hidden[0]["band"] == "2.4"
        assert hidden[0]["joinable"] is False

    def test_nmcli_freq_and_escaped_colon(self):
        rows = parse_nmcli_wifi(NMCLI_MIXED)
        assert _by_ssid(rows, "Home-5G")["band"] == "5"
        assert _by_ssid(rows, "Guest:2.4")["band"] == "2.4"
        assert _by_ssid(rows, "Guest:2.4")["ssid"] == "Guest:2.4"
        assert any(row["hidden"] for row in rows)
        assert band_notice(rows) is None


class TestRssi:
    def test_bars_and_the_weak_line(self):
        assert WEAK_RSSI_DBM == -70
        assert bars_for_dbm(-50) == 4
        assert bars_for_dbm(-60) == 3
        assert bars_for_dbm(-70) == 2
        assert bars_for_dbm(-71) == 1
        assert bars_for_dbm(-80) == 1
        assert bars_for_dbm(-81) == 0
        assert is_weak_rssi(-70) is False
        assert is_weak_rssi(-71) is True
        assert is_weak_rssi(None) is False
        weak = _by_ssid(parse_iw_scan(IW_MIXED), "Barn")
        assert weak["rssi_dbm"] == -71
        assert weak["weak"] is True
        assert weak["bars"] == 1
        cafe = _by_ssid(parse_iw_scan(IW_MIXED), "Cafe")
        assert cafe["rssi_dbm"] == -90
        assert cafe["bars"] == 0

    def test_iw_link_and_status_line(self):
        assert parse_iw_link("signal: -54 dBm\n") == -54
        assert parse_iw_link("not associated\n") is None
        assert format_station_rssi(-54) == "-54 dBm"
        assert format_station_rssi(None) == "unknown"

    def test_a_successful_join_records_station_rssi(self, monkeypatch, tmp_path):
        from utils.netctl import join_network

        fake = FakeNm()
        fake.iw_link = "Connected to aa:bb\n\tsignal: -54 dBm\n\tSSID: PondHouse\n"
        monkeypatch.setattr("utils.netctl._run", fake.run)
        monkeypatch.setattr("utils.netctl.reach_gateway_or_dns", lambda *a, **k: True)
        monkeypatch.setattr("utils.netctl.probe_sign_in_page", lambda *a, **k: "clear")
        status = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        fake.ap = True
        result = join_network(
            "PondHouse",
            "hunter22",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=lambda _s: None,
        )
        assert result["connected"] is True
        assert result["rssi"] == -54
        text = status.read_text(encoding="utf-8")
        assert "station_rssi: -54 dBm" in text
        assert "station: ok 192.168.1.50" in text


class TestSecurity:
    def test_iw_enterprise_sae_and_open(self):
        rows = parse_iw_scan(IW_MIXED)
        office = _by_ssid(rows, "Office")
        assert office["security"] == "enterprise"
        assert office["joinable"] is False
        assert "WPA2-Enterprise" in (block_reason(office) or "")
        only3 = _by_ssid(rows, "Only3")
        assert only3["security"] == "sae_only"
        assert "WPA3 only" in (block_reason(only3) or "")
        home = _by_ssid(rows, "Home")
        assert home["security"] == "wpa_psk"
        assert home["joinable"] is True
        assert block_reason(home) is None
        cafe = _by_ssid(rows, "Cafe")
        assert cafe["security"] == "open"
        assert cafe["secured"] is False
        assert "sign-in page" in (cafe["security_note"] or "")
        barn = _by_ssid(rows, "Barn")
        assert barn["security"] == "wep"

    def test_nmcli_security_words(self):
        assert classify_nmcli_security("WPA2") == "wpa_psk"
        assert classify_nmcli_security("WPA2 802.1X") == "enterprise"
        assert classify_nmcli_security("WPA3") == "sae_only"
        assert classify_nmcli_security("WPA2 WPA3") == "wpa_psk"
        assert classify_nmcli_security("--") == "open"
        assert classify_nmcli_security("WEP") == "wep"
        rows = parse_nmcli_wifi(NMCLI_MIXED)
        assert _by_ssid(rows, "Office")["security"] == "enterprise"
        assert _by_ssid(rows, "Only3")["security"] == "sae_only"
        assert _by_ssid(rows, "Home")["security"] == "wpa_psk"
        assert _by_ssid(rows, "Cafe")["security"] == "open"

    def test_sign_in_page_responses(self):
        assert interpret_sign_in_response(204, "", b"") == "clear"
        assert interpret_sign_in_response(302, "http://login.example/go", b"") == "captive"
        assert interpret_sign_in_response(511, "", b"") == "captive"
        html = b"<html><title>Sign in</title></html>"
        assert interpret_sign_in_response(200, "", html) == "captive"
        assert interpret_sign_in_response(0, "", b"") == "unknown"

    def test_join_with_an_address_and_a_sign_in_page(self, monkeypatch, tmp_path):
        from utils.netctl import join_network

        fake = FakeNm()
        fake.ap = True
        monkeypatch.setattr("utils.netctl._run", fake.run)
        monkeypatch.setattr("utils.netctl.reach_gateway_or_dns", lambda *a, **k: True)
        monkeypatch.setattr("utils.netctl.probe_sign_in_page", lambda *a, **k: "captive")
        status = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        result = join_network(
            "PondHouse",
            "hunter22",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=lambda _s: None,
        )
        assert result["connected"] is False
        assert result["reason"] == "captive_sign_in"
        assert result["message"] == CAPTIVE_JOIN_MESSAGE
        assert result["ap_restored"] is True
        assert "sign-in page" in result["message"]
        assert "station: fail: sign-in page" in status.read_text(encoding="utf-8")


class TestPasswords:
    def test_spaces_and_symbols_stay_one_argument(self):
        secret = 'pa ss$word"!'
        argv = wifi_connect_argv("Pond House", secret, "wlan0")
        assert argv[argv.index("password") + 1] == secret
        assert secret in argv
        assert not any(" " in part and part != secret and part != "Pond House" for part in argv)

    def test_trailing_space_is_removed_with_a_warning_and_the_middle_stays(self):
        trimmed, warning = normalize_password("hunter22 ", keep_trailing_space=False)
        assert trimmed == "hunter22"
        assert warning == TRAILING_SPACE_WARNING
        kept, no_warning = normalize_password("hunter22 ", keep_trailing_space=True)
        assert kept == "hunter22 "
        assert no_warning is None
        middle, mid_warning = normalize_password("hunt er22", keep_trailing_space=False)
        assert middle == "hunt er22"
        assert mid_warning is None
        leading, lead_warning = normalize_password(" hunter22", keep_trailing_space=False)
        assert leading == " hunter22"
        assert lead_warning is None

    def test_length_is_8_to_63_for_wpa(self):
        assert password_problem("", secured=False) is None
        assert password_problem("", secured=True)
        assert "too short" in (password_problem("short", secured=True) or "")
        assert "too short" in (password_problem("seven77", secured=False) or "")
        assert password_problem("eightchr", secured=True) is None
        assert password_problem("A" * 63, secured=True) is None
        assert "too long" in (password_problem("A" * 64, secured=True) or "")
        assert password_problem('a b$c"d!', secured=True) is None
        assert "cannot type" in (password_problem("hunter2\n2", secured=True) or "")
        assert "cannot type" in (password_problem("hunter2\x7f2", secured=True) or "")

    def test_join_passes_the_password_through_argv(self, monkeypatch):
        from utils.netctl import join_network

        fake = FakeNm()
        monkeypatch.setattr("utils.netctl._run", fake.run)
        monkeypatch.setattr("utils.netctl.reach_gateway_or_dns", lambda *a, **k: True)
        secret = 'a b$c"d!'
        join_network("PondHouse", secret, sleep=lambda _s: None)
        connect = next(c for c in fake.calls if c[1:4] == ["device", "wifi", "connect"])
        assert connect[connect.index("password") + 1] == secret


class TestProbes:
    def test_detected_and_missing(self):
        rows = probe_rows("ok", "no probe")
        assert rows[0]["name"] == "ADC"
        assert rows[0]["kind"] == "detected"
        assert rows[1]["name"] == "1-Wire"
        assert rows[1]["kind"] == "missing"
        assert rows[1]["confirm"] == "Finish without probe 1-Wire"
        assert classify_probe("faulted") == "missing"
        assert classify_probe("absent") == "missing"
        assert classify_probe("") == "unchecked"
        pending = unconfirmed_missing(rows, {})
        assert [row["id"] for row in pending] == ["onewire"]
        assert unconfirmed_missing(rows, {"confirm_onewire": "yes"}) == []


def _client(tmp_path, mock_hardware, pin="8642"):
    app = make_app(tmp_path, mock_hardware, pin=pin, open_window=True)
    return app.test_client(), app


class TestSetupPage:
    def test_landing_page_mentions_mobile_data(self, tmp_path, mock_hardware, monkeypatch):
        # The hotspot lands on the Wi-Fi page now; the note moved with it.
        from utils import netctl

        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(netctl, "scan_networks", lambda iface="wlan0": [])
        client, _app = _client(tmp_path, mock_hardware, pin="1234")
        page = client.get("/setup/", follow_redirects=True).data.decode()
        assert "turn off mobile data" in page
        assert "stay connected to WQM1-" in page

    def test_network_page_shows_bars_and_a_weak_warning(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 80, "secured": True, "rssi_dbm": -40},
                {"ssid": "FarBarn", "signal": 20, "secured": True, "rssi_dbm": -85},
            ],
        )
        client, _app = _client(tmp_path, mock_hardware)
        page = client.get("/setup/network").data.decode()
        assert "4 of 4 bars" in page
        assert "1 of 4 bars" in page or "0 of 4 bars" in page
        assert WEAK_SIGNAL_MESSAGE in page
        assert "Show password" in page
        assert "The password ends with a space" in page

    def test_empty_scan_says_2_4_ghz_only(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(netctl, "scan_networks", lambda iface="wlan0": [])
        client, _app = _client(tmp_path, mock_hardware)
        page = client.get("/setup/network").data.decode()
        assert BAND_MESSAGE in page

    def test_enterprise_and_wpa3_are_explained_and_not_joined(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        from utils import netctl

        calls: list[tuple] = []
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {
                    "ssid": "Corp",
                    "signal": 70,
                    "secured": True,
                    "security": "enterprise",
                    "freq_mhz": 2412,
                },
                {
                    "ssid": "Only3",
                    "signal": 70,
                    "secured": True,
                    "security": "sae_only",
                    "freq_mhz": 2437,
                },
            ],
        )
        monkeypatch.setattr(netctl, "join_network", lambda *a, **k: calls.append(a))
        client, _app = _client(tmp_path, mock_hardware)
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "Corp", "password": "hunter22"},
            follow_redirects=True,
        )
        page = client.get("/setup/network").data.decode()
        assert "WPA2-Enterprise" in page
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "Only3", "password": "hunter22"},
            follow_redirects=True,
        )
        page = client.get("/setup/network").data.decode()
        assert "WPA3 only" in page
        assert calls == []

    def test_five_gigahertz_is_refused_with_the_sentence(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        from utils import netctl

        calls: list[tuple] = []
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "Home-5G", "signal": 90, "secured": True, "freq_mhz": 5180}
            ],
        )
        monkeypatch.setattr(netctl, "join_network", lambda *a, **k: calls.append(a))
        client, _app = _client(tmp_path, mock_hardware)
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "Home-5G", "password": "hunter22"},
            follow_redirects=True,
        )
        assert BAND_MESSAGE in client.get("/setup/network").data.decode()
        assert calls == []

    def test_an_unseen_name_is_still_tried_and_the_sentence_is_kept(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        from utils import netctl

        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 70, "secured": True, "freq_mhz": 2412}
            ],
        )
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))
        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *a, **k: {
                "ok": False,
                "connected": False,
                "ap_restored": True,
                "error": "not found",
                "message": (
                    "That network was not found. Move the unit closer to the router and scan again."
                ),
                "reason": "not_found",
                "ip": None,
            },
        )
        client, _app = _client(tmp_path, mock_hardware)
        client.post(
            "/setup/network",
            data={"action": "join", "hidden_ssid": "Barn-Hidden", "password": "hunter22"},
        )
        page = client.get("/setup/network").data.decode()
        assert "That network was not found" in page
        assert BAND_MESSAGE in page

    def test_trailing_space_warns_before_the_join(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        seen: list[str | None] = []

        def _join(ssid, password, ap_ssid=None, ap_passphrase=None, **_k):
            seen.append(password)
            return {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.50",
            }

        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 70, "secured": True, "freq_mhz": 2412}
            ],
        )
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))
        monkeypatch.setattr(netctl, "join_network", _join)
        monkeypatch.setattr(
            netctl,
            "forget_saved_wifi",
            lambda keep, iface="wlan0": {"ok": True, "forgotten": [], "failed": [], "kept": keep},
        )
        client, _app = _client(tmp_path, mock_hardware)
        first = client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22 "},
        )
        assert b"Removed a space at the end" in first.data
        assert seen == []
        client.post(
            "/setup/network",
            data={
                "action": "join",
                "ssid": "PondHouse",
                "password": "hunter22",
                "trim_ack": "yes",
            },
        )
        assert seen == ["hunter22"]

    def test_special_characters_reach_the_join(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        seen: list[str | None] = []
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 70, "secured": True, "freq_mhz": 2412}
            ],
        )
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))

        def _join(ssid, password, ap_ssid=None, ap_passphrase=None, **_k):
            seen.append(password)
            return {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.20",
            }

        monkeypatch.setattr(netctl, "join_network", _join)
        monkeypatch.setattr(
            netctl,
            "forget_saved_wifi",
            lambda keep, iface="wlan0": {"ok": True, "forgotten": [], "failed": [], "kept": keep},
        )
        client, _app = _client(tmp_path, mock_hardware)
        secret = 'a b$c"d!'
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": secret},
        )
        assert seen == [secret]

    def test_short_and_long_passwords_never_touch_the_radio(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        from utils import netctl

        calls: list[tuple] = []
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 70, "secured": True, "freq_mhz": 2412}
            ],
        )
        monkeypatch.setattr(netctl, "join_network", lambda *a, **k: calls.append(a))
        client, _app = _client(tmp_path, mock_hardware)
        page = ""
        for password in ("short", "A" * 64):
            page = client.post(
                "/setup/network",
                data={"action": "join", "ssid": "PondHouse", "password": password},
                follow_redirects=True,
            ).data.decode()
            assert "8 to 63" in page
        assert calls == []

    def test_finish_requires_a_tick_when_a_probe_is_missing(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        status = tmp_path / "wqm1-status.txt"
        status.write_text(
            "adc: faulted\nonewire: ok\nhotspot: up\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        client, app = _client(tmp_path, mock_hardware)
        client.post("/setup/done", data={}, follow_redirects=True)
        page = client.get("/setup/done").data.decode()
        assert "Finish without probe ADC" in page
        assert "not detected" in page
        assert "detected" in page
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert not (saved.get("service_window") or {}).get("setup_completed")
        client.post("/setup/done", data={"confirm_adc": "yes"}, follow_redirects=False)
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["service_window"]["setup_completed"] is True

    def test_join_does_not_wait_on_the_probes(self, tmp_path, mock_hardware, monkeypatch):
        """Getting online is the only on-site step (2026-10-22). A missing
        probe is Cloud's quiet-probe alert, not a reason to stay offline."""
        from utils import netctl

        status = tmp_path / "wqm1-status.txt"
        status.write_text("adc: absent\nonewire: no probe\n", encoding="utf-8")
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        calls: list[tuple] = []
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 70, "secured": True, "freq_mhz": 2412}
            ],
        )

        def _join(*a, **k):
            calls.append(a)
            return {"connected": False, "error": "wrong password", "ap_restored": True}

        monkeypatch.setattr(netctl, "join_network", _join)
        client, _app = _client(tmp_path, mock_hardware)
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
            follow_redirects=True,
        )
        page = client.get("/setup/network").data.decode()
        assert "Finish without probe" not in page
        assert len(calls) == 1


def test_percent_only_rows_still_get_bars():
    row = annotate_network({"ssid": "PondHouse", "signal": 72, "secured": True})
    assert row["secured"] is True
    assert row["bars"] == bars_for_dbm(row["rssi_dbm"])
    assert row["band"] == "unknown"
    assert row["joinable"] is True
