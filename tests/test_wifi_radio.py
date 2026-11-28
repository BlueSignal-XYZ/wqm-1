"""Dual-band SSIDs, soft 5 GHz notes, hidden names, and per-board radio policy."""

from __future__ import annotations

from tests.test_installer_wifi import _by_ssid
from utils import captive_portal
from utils.wifi_radio import (
    allows_5ghz,
    bands_from_board,
    parse_iw_phy,
    reset_cache,
    station_bands,
    station_nm_settings,
)
from utils.wifi_setup import (
    BAND_SOFT_MESSAGE,
    annotate_network,
    block_code,
    parse_iw_scan,
    parse_nmcli_wifi,
)

IW_DUAL = """
BSS 00:11:22:33:44:01(on wlan0)
    freq: 5180
    signal: -40.00 dBm
    SSID: Spectrum Mobile
    RSN:
         * Authentication suites: PSK
BSS 00:11:22:33:44:02(on wlan0)
    freq: 2437
    signal: -65.00 dBm
    SSID: Spectrum Mobile
    RSN:
         * Authentication suites: PSK
"""

IW_PHY_DUAL = """
Wiphy phy0
        Band 1:
                Frequencies:
                        * 2412 MHz [1] (20.0 dBm)
                        * 2437 MHz [6] (20.0 dBm)
        Band 2:
                Frequencies:
                        * 5180 MHz [36] (20.0 dBm)
                        * 5200 MHz [40] (disabled)
"""

IW_PHY_2G4 = """
Wiphy phy0
        Band 1:
                Frequencies:
                        * 2412 MHz [1] (20.0 dBm)
                        * 2437 MHz [6] (20.0 dBm)
                        * 2462 MHz [11] (20.0 dBm)
"""


class TestScanGrouping:
    def test_dual_band_same_ssid_is_selectable_as_2_4(self):
        rows = parse_iw_scan(IW_DUAL)
        row = _by_ssid(rows, "Spectrum Mobile")
        assert row["band"] == "2.4"
        assert row["freq_mhz"] == 2437
        assert row["joinable"] is True
        assert block_code(row) is None
        assert not row.get("band_note")
        assert "2.4" in row["bands"] and "5" in row["bands"]

    def test_nmcli_dual_band_same_ssid_keeps_the_2_4_side(self):
        text = "\n".join(
            [
                "Spectrum Mobile:90:WPA2:5180 MHz",
                "Spectrum Mobile:50:WPA2:2437 MHz",
            ]
        )
        row = _by_ssid(parse_nmcli_wifi(text), "Spectrum Mobile")
        assert row["band"] == "2.4"
        assert row["joinable"] is True
        assert block_code(row) is None

    def test_5ghz_only_seen_is_selectable_with_a_soft_note(self):
        rows = parse_iw_scan(
            "BSS aa(on wlan0)\n\tfreq: 5180\n\tsignal: -40.00 dBm\n"
            "\tSSID: Spectrum Mobile\n\tRSN:\n\t * Authentication suites: PSK\n"
        )
        row = _by_ssid(rows, "Spectrum Mobile")
        assert row["band"] == "5"
        assert row["joinable"] is True
        assert row["band_note"] == BAND_SOFT_MESSAGE
        assert block_code(row) is None

    def test_hidden_network_stays_off_the_list_and_is_typed(self):
        raw = annotate_network(
            {"ssid": "", "signal": 40, "secured": True, "hidden": True, "freq_mhz": 2412}
        )
        assert raw["hidden"] is True
        assert raw["joinable"] is False
        args, err = captive_portal.validate_join(
            {"ssid": "Barn-Hidden", "password": "hunter2222", "hidden": True},
            [raw],
        )
        assert err == ""
        assert args["ssid"] == "Barn-Hidden"
        assert args["hidden"] is True

    def test_typed_other_network_is_allowed_even_when_the_scan_says_5ghz(self):
        rows = parse_iw_scan(
            "BSS aa(on wlan0)\n\tfreq: 5180\n\tsignal: -40.00 dBm\n"
            "\tSSID: Spectrum Mobile\n\tRSN:\n\t * Authentication suites: PSK\n"
        )
        args, err = captive_portal.validate_join(
            {"ssid": "Spectrum Mobile", "password": "hunter2222", "hidden": True},
            rows,
        )
        assert err == ""
        assert args["hidden"] is True
        args, err = captive_portal.validate_join(
            {"ssid": "Spectrum Mobile", "password": "hunter2222", "hidden": False},
            rows,
        )
        assert err == ""
        assert args["ssid"] == "Spectrum Mobile"


class TestBoardPolicy:
    def setup_method(self):
        reset_cache()

    def teardown_method(self):
        reset_cache()

    def test_zero_w_zero_2w_and_3b_are_2_4_only(self):
        for model in (
            "Raspberry Pi Zero W Rev 1.1",
            "Raspberry Pi Zero 2 W Rev 1.0",
            "Raspberry Pi 3 Model B Rev 1.2",
        ):
            bands = bands_from_board(model)
            assert bands == frozenset({"2.4"}), model
            assert allows_5ghz(bands) is False
            assert station_nm_settings(bands) == ["802-11-wireless.band", "bg"]

    def test_3b_plus_and_pi_4_may_join_5ghz(self):
        for model in (
            "Raspberry Pi 3 Model B Plus Rev 1.3",
            "Raspberry Pi 3 Model A Plus Rev 1.0",
            "Raspberry Pi 4 Model B Rev 1.4",
            "Raspberry Pi 400",
        ):
            bands = bands_from_board(model)
            assert "5" in bands, model
            assert allows_5ghz(bands) is True
            assert station_nm_settings(bands) == []

    def test_revision_codes_match_the_board_policy(self):
        # New-style: bit 23 set, type in bits 4..11. Warranty/memory bits vary.
        assert bands_from_board(revision="0xa02082") == frozenset({"2.4"})  # 3B, type 0x08
        assert "5" in (bands_from_board(revision="0xa020d3") or set())  # 3B+, type 0x0D
        assert "5" in (bands_from_board(revision="0xa03111") or set())  # 4B, type 0x11

    def test_live_radio_wins_over_the_board_model(self):
        assert "5" in parse_iw_phy(IW_PHY_DUAL)
        assert parse_iw_phy(IW_PHY_2G4) == frozenset({"2.4"})
        assert "5" in station_bands(iw_phy_text=IW_PHY_DUAL, model="Raspberry Pi Zero W Rev 1.1")
        assert station_bands(iw_phy_text=IW_PHY_2G4, model="Raspberry Pi 4 Model B") == frozenset(
            {"2.4"}
        )


class TestJoinUsesBoardPolicy:
    def setup_method(self):
        reset_cache()

    def teardown_method(self):
        reset_cache()

    def test_2_4_only_join_locks_the_profile_to_bg(self, monkeypatch, tmp_path):
        from tests.test_netctl import FakeNm
        from utils import netctl
        from utils.wifi_radio import detect_station_bands

        detect_station_bands(iw_phy_text=IW_PHY_2G4)
        fake = FakeNm()
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: True)
        monkeypatch.setattr(netctl, "probe_sign_in_page", lambda *a, **k: "clear")
        monkeypatch.setattr(netctl, "reach_bluesignal", lambda *a, **k: {"ok": True})
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        fake.ap = True
        result = netctl.join_network(
            "PondHouse",
            "hunter22",
            ap_ssid="WQM1-0001",
            ap_passphrase=None,
            sleep=lambda _s: None,
        )
        assert result["connected"] is True
        added = [c for c in fake.calls if "connection" in c and "add" in c]
        assert added, fake.calls
        argv = added[0]
        # nmcli argv may be prefixed with nmcli
        flat = argv[1:] if argv and argv[0] == "nmcli" else argv
        assert "802-11-wireless.band" in flat
        assert flat[flat.index("802-11-wireless.band") + 1] == "bg"

    def test_dual_band_join_does_not_lock_the_band(self, monkeypatch, tmp_path):
        from tests.test_netctl import FakeNm
        from utils import netctl
        from utils.wifi_radio import detect_station_bands

        detect_station_bands(iw_phy_text=IW_PHY_DUAL)
        fake = FakeNm()
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: True)
        monkeypatch.setattr(netctl, "probe_sign_in_page", lambda *a, **k: "clear")
        monkeypatch.setattr(netctl, "reach_bluesignal", lambda *a, **k: {"ok": True})
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        fake.ap = True
        result = netctl.join_network(
            "PondHouse",
            "hunter22",
            ap_ssid="WQM1-0001",
            ap_passphrase=None,
            sleep=lambda _s: None,
        )
        assert result["connected"] is True
        added = [c for c in fake.calls if "connection" in c and "add" in c]
        assert added
        argv = added[0]
        flat = argv[1:] if argv and argv[0] == "nmcli" else argv
        assert "802-11-wireless.band" not in flat
