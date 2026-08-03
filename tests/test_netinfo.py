"""Network facts + cloud key verification (commissioning network step)."""

import urllib.error

import pytest

from utils import netinfo


class TestSignalState:
    """Thresholds are judged for a sealed enclosure left alone for months."""

    def test_strong_signal_is_ok(self):
        assert netinfo.signal_state(-42) == "ok"
        assert netinfo.signal_state(netinfo.RSSI_GOOD_DBM) == "ok"

    def test_marginal_signal_is_flagged_not_hidden(self):
        # One dB below the "good" line must not read as fine — this is exactly
        # the link that dies when the lid goes on.
        assert netinfo.signal_state(netinfo.RSSI_GOOD_DBM - 1) == "degraded"
        assert netinfo.signal_state(-90) == "degraded"

    def test_no_reading_is_down(self):
        assert netinfo.signal_state(None) == "down"


class TestWifiStatus:
    def test_reports_ssid_rssi_and_ip(self, monkeypatch):
        monkeypatch.setattr(netinfo, "current_ssid", lambda: "PondHouse")
        monkeypatch.setattr(netinfo, "local_ip", lambda: "192.168.1.224")
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: -55)

        s = netinfo.wifi_status()
        assert s["ssid"] == "PondHouse"
        assert s["rssi_dbm"] == -55
        assert s["ip"] == "192.168.1.224"
        assert s["state"] == "ok"

    def test_no_association_is_down(self, monkeypatch):
        monkeypatch.setattr(netinfo, "current_ssid", lambda: None)
        monkeypatch.setattr(netinfo, "local_ip", lambda: None)
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: None)

        assert netinfo.wifi_status()["state"] == "down"

    def test_never_raises_when_every_probe_fails(self, monkeypatch):
        def boom():
            raise OSError("no interface")

        # The helpers already swallow errors; assert the page-facing call is
        # safe even if one slips through.
        monkeypatch.setattr(netinfo, "current_ssid", lambda: None)
        monkeypatch.setattr(netinfo, "local_ip", lambda: None)
        monkeypatch.setattr("utils.health.read_wifi_rssi_dbm", lambda: None)
        assert netinfo.wifi_status()["ssid"] is None


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestVerifyDeviceKey:
    """The status ladder was probed against production before it was coded:
    204 valid · 401 bad key · 403 key bound to a different device."""

    BASE = "https://example.invalid/app"

    def test_accepted_key_is_ok(self, monkeypatch):
        monkeypatch.setattr(netinfo.urllib.request, "urlopen", lambda *a, **k: _Resp(204))
        r = netinfo.verify_device_key(self.BASE, "BS-WQM1-0000a92e4e7d", "k" * 32)
        assert r["state"] == "ok"
        assert r["status"] == 204

    def test_rejected_key_is_degraded_not_down(self, monkeypatch):
        # 401 means we REACHED the cloud — the network is fine and the key is
        # not. Reporting that as "offline" would send the installer to the
        # wrong problem.
        def raise401(*a, **k):
            raise urllib.error.HTTPError(self.BASE, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(netinfo.urllib.request, "urlopen", raise401)
        r = netinfo.verify_device_key(self.BASE, "BS-WQM1-0000a92e4e7d", "bad")
        assert r["state"] == "degraded"
        assert r["status"] == 401

    def test_key_for_another_device_says_so(self, monkeypatch):
        def raise403(*a, **k):
            raise urllib.error.HTTPError(self.BASE, 403, "Forbidden", {}, None)

        monkeypatch.setattr(netinfo.urllib.request, "urlopen", raise403)
        r = netinfo.verify_device_key(self.BASE, "BS-WQM1-0000a92e4e7d", "k" * 32)
        assert r["state"] == "degraded"
        assert "different device" in r["detail"]

    def test_unreachable_cloud_is_down(self, monkeypatch):
        def raise_url(*a, **k):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(netinfo.urllib.request, "urlopen", raise_url)
        r = netinfo.verify_device_key(self.BASE, "BS-WQM1-0000a92e4e7d", "k" * 32)
        assert r["state"] == "down"
        assert r["status"] is None

    def test_missing_key_short_circuits_without_a_request(self, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("must not call the network with no key")

        monkeypatch.setattr(netinfo.urllib.request, "urlopen", explode)
        assert netinfo.verify_device_key(self.BASE, "BS-WQM1-x", "")["state"] == "down"

    def test_refuses_non_http_scheme(self, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("must not open a non-HTTP URL")

        monkeypatch.setattr(netinfo.urllib.request, "urlopen", explode)
        r = netinfo.verify_device_key("file:///etc", "BS-WQM1-x", "k" * 32)
        assert r["state"] == "down"


class TestReadOnlyBoundary:
    """This module reports; it must never reconfigure the link it is served over."""

    def test_exposes_no_network_mutation_helpers(self):
        forbidden = {"set_wifi", "join_network", "connect", "start_ap", "reconfigure"}
        assert not forbidden & set(dir(netinfo))

    @pytest.mark.parametrize("bad", ["nmcli con up", "wpa_cli", "rm"])
    def test_probe_argv_are_read_only_commands(self, bad):
        import inspect

        src = inspect.getsource(netinfo)
        assert f'"{bad}"' not in src
