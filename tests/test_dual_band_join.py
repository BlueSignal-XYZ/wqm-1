"""A radio that can join 5 GHz tries the same network once more on 2.4 GHz.

Jacques, 2026-11-17: a Pi 4 (dual band) on a Spectrum router never
associated (``join_1: … no_association``), and "most of our customers will
have 5 GHz". A Pi 4 joins with no band lock, so wpa_supplicant may pick the
5 GHz side of a dual-band name; when that first try cannot get on, the same
name is tried once more locked to 2.4 GHz. The Pi Zero 2 W (2.4 GHz only) is
locked from the start and never makes the extra try.
"""

from __future__ import annotations

import pytest

from tests.test_netctl import FakeNm

TIMEOUT_UP = "Error: Connection activation failed: (7) Timeout was reached."


@pytest.fixture
def nm(monkeypatch):
    from utils import netctl

    fake = FakeNm()
    monkeypatch.setattr(netctl, "_run", fake.run)
    monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: fake.reach)
    monkeypatch.setattr(netctl, "probe_sign_in_page", lambda *a, **k: "clear")
    monkeypatch.setattr(netctl, "reach_bluesignal", lambda *a, **k: {"ok": True})
    return fake


@pytest.fixture
def clock():
    now = [0.0]

    class Clock:
        def __call__(self):
            return now[0]

        def sleep(self, s):
            now[0] += s

        def advance(self, s):
            now[0] += s

        @property
        def now(self):
            return now[0]

    return Clock()


@pytest.fixture
def bands(monkeypatch):
    from utils import wifi_radio

    def set_bands(*names):
        monkeypatch.setattr(wifi_radio, "_CACHED_BANDS", frozenset(names))

    set_bands("2.4")
    return set_bands


def _flat(argv):
    return argv[1:] if argv and argv[0] == "nmcli" else argv


def _try_ups(nm):
    return [
        _flat(c)
        for c in nm.calls
        if _flat(c)[:2] == ["connection", "up"]
        and len(_flat(c)) > 2
        and _flat(c)[2].startswith("bs-try-")
    ]


def _try_adds(nm):
    return [_flat(c) for c in nm.calls if _flat(c)[:2] == ["connection", "add"]]


def _failed_join(netctl, clock, password="hunter22"):
    return netctl.join_network(
        "PondHouse",
        password,
        ap_ssid="BlueSignal Setup",
        ap_passphrase=None,
        sleep=clock.sleep,
        clock=clock,
    )


class TestDualBandRadiosTryTheTwoPointFourSide:
    @pytest.fixture
    def five_ghz_refused(self, nm, monkeypatch):
        """The router's 5 GHz side never accepts this radio; 2.4 GHz does."""
        from utils import netctl

        timeouts = []
        real = nm.run

        def run(argv, timeout=15.0):
            a = _flat(argv)
            if a[:2] == ["connection", "up"] and len(a) > 2 and a[2] in nm.try_profiles:
                nm.calls.append(argv)
                timeouts.append(timeout)
                if "802-11-wireless.band" not in nm.try_profiles[a[2]]["argv"]:
                    return 4, "", TIMEOUT_UP
                nm.station = True
                return 0, "Connection successfully activated", ""
            return real(argv, timeout=timeout)

        monkeypatch.setattr(netctl, "_run", run)
        return timeouts

    def test_a_pi_4_that_cannot_get_on_tries_the_same_name_on_2_4_ghz(
        self, nm, bands, five_ghz_refused, clock
    ):
        from utils import netctl

        bands("2.4", "5")
        nm.ap = True
        r = _failed_join(netctl, clock)
        assert r["connected"] is True, r
        adds = _try_adds(nm)
        assert len(adds) == 2
        assert "802-11-wireless.band" not in adds[0]
        assert adds[1][adds[1].index("802-11-wireless.band") + 1] == "bg"
        assert adds[1][adds[1].index("ssid") + 1] == "PondHouse"

    def test_the_first_try_leaves_room_for_the_second(self, nm, bands, five_ghz_refused, clock):
        from utils import netctl

        bands("2.4", "5")
        nm.ap = True
        _failed_join(netctl, clock)
        assert five_ghz_refused[0] <= netctl.FIRST_BAND_ATTEMPT_S
        assert five_ghz_refused[1] >= netctl.MIN_ATTEMPT_S

    def test_both_sides_refusing_still_gives_the_hotspot_back_within_the_budget(
        self, nm, bands, clock, monkeypatch
    ):
        from utils import netctl

        bands("2.4", "5")
        nm.ap = True
        real = nm.run
        restored_at = []

        def run(argv, timeout=15.0):
            a = _flat(argv)
            if a[:2] == ["connection", "up"] and len(a) > 2 and a[2].startswith("bs-try-"):
                nm.calls.append(argv)
                clock.advance(timeout)  # nmcli waits its whole timeout
                return 4, "", TIMEOUT_UP
            if argv[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"]:
                restored_at.append(clock.now)
            return real(argv, timeout=timeout)

        monkeypatch.setattr(netctl, "_run", run)
        r = _failed_join(netctl, clock)
        assert r["ap_restored"] is True
        assert len(_try_ups(nm)) == 2
        assert restored_at[0] <= netctl.JOIN_BUDGET_S + 1, restored_at

    def test_a_wrong_password_on_a_dual_band_radio_is_tried_once_on_2_4(self, nm, bands, clock):
        """brcmfmac on a 5 GHz BSS can fail the handshake with the right
        password; NetworkManager words that as "Secrets were required"."""
        from utils import netctl

        bands("2.4", "5")
        nm.ap = True
        nm.join_rc = 4
        r = _failed_join(netctl, clock)
        assert r["reason"] == "wrong_password"
        assert len(_try_ups(nm)) == 2
        assert "802-11-wireless.band" in _try_adds(nm)[1]

    def test_a_radio_already_on_the_network_is_left_to_get_its_address(self, nm, bands, clock):
        from utils import netctl

        bands("2.4", "5")
        nm.ap = True
        nm.join_rc = 4
        nm.iw_link = "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: PondHouse\n"
        _failed_join(netctl, clock)
        assert len(_try_ups(nm)) == 1

    def test_a_2_4_only_radio_never_makes_the_extra_try(self, nm, bands, clock):
        from utils import netctl

        bands("2.4")
        nm.ap = True
        nm.join_rc = 4
        r = _failed_join(netctl, clock)
        assert r["reason"] == "wrong_password"
        assert len(_try_ups(nm)) == 1
        assert "802-11-wireless.band" in _try_adds(nm)[0]

    def test_not_found_still_rescans_and_probes_first(self, nm, bands, clock, monkeypatch):
        """A network missing from the scan is the scan's problem, not the band's."""
        from tests.test_netctl import NOT_FOUND_UP
        from utils import netctl

        bands("2.4", "5")
        nm.ap = True
        real = nm.run
        stages = []

        def run(argv, timeout=15.0):
            a = _flat(argv)
            if a[:2] == ["connection", "up"] and len(a) > 2 and a[2].startswith("bs-try-"):
                nm.calls.append(argv)
                prof = nm.try_profiles[a[2]]
                stages.append(prof["hidden"])
                if not prof["hidden"]:
                    return 10, "", NOT_FOUND_UP
                nm.station = True
                return 0, "Connection successfully activated", ""
            return real(argv, timeout=timeout)

        monkeypatch.setattr(netctl, "_run", run)
        assert _failed_join(netctl, clock)["connected"] is True
        assert stages == [False, False, True]
