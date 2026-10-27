"""The Wi-Fi join: one at a time, proven by reaching BlueSignal (install plan §2).

Invariants pinned here (plan, "What must always be true"):

* I2 — the home Wi-Fi password is never in a log, a state file, the status
  file on the card, or ``join.read_state()``.
* I3 — a network that has reached BlueSignal is never deleted because a later
  join failed. A wrong password re-typed for a proven network keeps its
  profile.
* I4 — at most one join runs at a time.

nmcli, the radio, the clock and HTTPS are fakes. Nothing here touches a
real network.
"""

from __future__ import annotations

import email.message
import importlib
import importlib.util
import json
import logging
import ssl
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from utils import join, netctl

# The real network checks, taken before conftest stubs them for every test.
REAL_HTTPS_STATUS = netctl._https_status
REAL_HTTP_DATE = netctl._http_date

HOME = "SmithHome"
RIGHT = "Sup3r-Secret-Pw!"
API = "https://api.bluesignal.test/app"


class Radio:
    """nmcli and the radio around one join. Profiles are name -> SSID."""

    def __init__(self, networks: dict[str, str], profiles: dict[str, str] | None = None):
        self.networks = networks
        self.profiles = dict(profiles or {})
        self.station: str | None = None
        self.ap = True
        self.calls: list[list[str]] = []
        self.echo_password = False

    def nmcli(self, *args, timeout=15.0):
        a = [str(x) for x in args]
        self.calls.append(a)
        if a[:3] == ["device", "wifi", "connect"]:
            ssid = a[3]
            name = a[a.index("name") + 1] if "name" in a else ssid
            password = a[a.index("password") + 1] if "password" in a else None
            # NetworkManager creates the profile before it activates it.
            self.profiles[name] = ssid
            if self.networks.get(ssid) == password:
                self.station = ssid
                return 0, "Device 'wlan0' successfully activated", ""
            err = "Error: Secrets were required, but not provided."
            if self.echo_password and password:
                err = f"{err} (tried {password})"
            return 4, "", err
        if a[:2] == ["connection", "delete"]:
            self.profiles.pop(a[2], None)
            return 0, "", ""
        if a[:2] == ["connection", "modify"] and a[3] == "connection.id":
            ssid = self.profiles.pop(a[2])
            self.profiles[a[4]] = ssid
            return 0, "", ""
        if a[:4] == ["-t", "-f", "NAME,TYPE", "connection"]:
            return 0, "\n".join(f"{n}:802-11-wireless" for n in self.profiles), ""
        if a[:2] == ["-g", "802-11-wireless.ssid"]:
            return 0, self.profiles.get(a[-1], a[-1]), ""
        return 0, "", ""

    def snap(self, iface="wlan0"):
        if not self.station:
            return {"ssid": None, "usable": [], "link_local": [], "gateway": None, "dns": []}
        return {
            "ssid": self.station,
            "usable": ["192.168.1.57"],
            "link_local": [],
            "gateway": "192.168.1.1",
            "dns": ["192.168.1.1"],
        }

    def names_for(self, ssid: str) -> list[str]:
        return [name for name, s in self.profiles.items() if s == ssid]


@pytest.fixture
def radio(monkeypatch, tmp_path):
    r = Radio({HOME: RIGHT, "BlueSignal-Shop": "shop-pass-1"})
    monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "boot" / "wqm1-status.txt"))
    monkeypatch.setattr(netctl, "_nmcli", r.nmcli)
    monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": r.ap)
    monkeypatch.setattr(netctl, "stop_ap", lambda iface="wlan0": setattr(r, "ap", False))

    def start_ap(ssid, passphrase, iface="wlan0"):
        r.ap = True
        return {"ok": True}

    monkeypatch.setattr(netctl, "start_ap", start_ap)
    monkeypatch.setattr(netctl, "_disarm_portal", lambda reason: None)
    monkeypatch.setattr(netctl, "_arm_portal", lambda: None)
    monkeypatch.setattr(netctl, "wait_for_ssid", lambda *a, **k: True)
    monkeypatch.setattr(netctl, "read_iface_net", r.snap)
    monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: True)
    monkeypatch.setattr(netctl, "_sign_in_verdict", lambda: "clear")
    monkeypatch.setattr(netctl, "read_station_rssi", lambda iface="wlan0": -52)
    monkeypatch.setattr(netctl, "wait_until_reachable", lambda **k: "up (80)")
    monkeypatch.setattr(netctl, "_confirm_moved_card", lambda: None)
    monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))
    return r


def _wait_done(timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = join.read_state()
        if state["state"] != "joining" and not join._lock_held():
            return state
        time.sleep(0.01)
    raise AssertionError("the join never finished")


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _join_direct(ssid: str, password: str, *, reach=None, **kwargs) -> dict:
    return netctl.join_network(
        ssid,
        password,
        ap_ssid="WQM1-0001",
        ap_passphrase="river-cedar-42",
        sleep=lambda _s: None,
        api_base=API,
        reach=reach or (lambda *a, **k: {"ok": True, "clock": None}),
        **kwargs,
    )


# -- I3: the proven profile survives ------------------------------------------


class TestProvenNetworkSurvives:
    def test_wrong_password_for_a_proven_network_keeps_its_profile(self, radio):
        """Fails on master: the failure path ran ``nmcli connection delete <ssid>``."""
        radio.profiles = {HOME: HOME}
        result = _join_direct(HOME, "wrong-password")
        assert result["reason"] == "wrong_password"
        assert radio.names_for(HOME) == [HOME]
        assert not any(name.startswith(netctl.TRY_PROFILE_PREFIX) for name in radio.profiles)

    def test_the_join_is_tried_under_a_temporary_profile(self, radio):
        _join_direct(HOME, RIGHT)
        connect = next(c for c in radio.calls if c[:3] == ["device", "wifi", "connect"])
        name = connect[connect.index("name") + 1]
        assert name.startswith(netctl.TRY_PROFILE_PREFIX)
        assert connect[connect.index("password") + 1] == RIGHT

    def test_success_renames_the_profile_and_drops_older_ones_for_that_ssid(self, radio):
        radio.profiles = {HOME: HOME, "Smith old": HOME}
        result = _join_direct(HOME, RIGHT)
        assert result["connected"] is True
        assert radio.names_for(HOME) == [HOME]
        renames = [c for c in radio.calls if c[:2] == ["connection", "modify"]]
        promoted = [c for c in renames if c[2].startswith(netctl.TRY_PROFILE_PREFIX)]
        assert promoted and promoted[0][3:] == ["connection.id", HOME]

    def test_older_profiles_are_deleted_only_after_the_new_one_has_the_name(self, radio):
        radio.profiles = {HOME: HOME, "Smith old": HOME}
        _join_direct(HOME, RIGHT)
        calls = [
            c for c in radio.calls if c[:2] in (["connection", "modify"], ["connection", "delete"])
        ]
        promote = next(i for i, c in enumerate(calls) if c[2].startswith(netctl.TRY_PROFILE_PREFIX))
        deletes = [i for i, c in enumerate(calls) if c[1] == "delete"]
        assert deletes and min(deletes) > promote
        # Nothing was ever deleted by the SSID's name, which a second profile
        # carrying that name would share.
        assert all(calls[i][2].startswith(netctl.OLD_PROFILE_PREFIX) for i in deletes)

    def test_a_failed_rename_keeps_the_older_profile(self, radio, monkeypatch):
        radio.profiles = {HOME: HOME}
        real = radio.nmcli

        def nmcli(*args, timeout=15.0):
            a = [str(x) for x in args]
            if a[:2] == ["connection", "modify"] and a[2].startswith(netctl.TRY_PROFILE_PREFIX):
                radio.calls.append(a)
                return 10, "", "Error: failed to modify connection"
            return real(*args, timeout=timeout)

        monkeypatch.setattr(netctl, "_nmcli", nmcli)
        result = _join_direct(HOME, RIGHT)
        assert result["connected"] is True
        # The old profile has its name back; the proven one is still saved.
        assert HOME in radio.profiles
        assert [n for n in radio.names_for(HOME) if n.startswith(netctl.TRY_PROFILE_PREFIX)]
        assert not [p for p in radio.profiles if p.startswith(netctl.OLD_PROFILE_PREFIX)]

    def test_no_bluesignal_fails_and_keeps_the_proven_profile(self, radio):
        radio.profiles = {HOME: HOME}
        result = _join_direct(HOME, RIGHT, reach=lambda *a, **k: {"ok": False, "clock": None})
        assert result["connected"] is False
        assert result["reason"] == "no_bluesignal"
        assert result["ap_restored"] is True
        assert radio.names_for(HOME) == [HOME]

    def test_a_hidden_network_is_probed_directly_under_the_temporary_name(self, radio):
        _join_direct(HOME, RIGHT, hidden=True)
        connects = [c for c in radio.calls if c[:3] == ["device", "wifi", "connect"]]
        assert len(connects) == 1
        assert connects[0][-2:] == ["hidden", "yes"]
        assert connects[0][connects[0].index("name") + 1].startswith(netctl.TRY_PROFILE_PREFIX)


# -- Success means BlueSignal answered ---------------------------------------


class TestReachBlueSignal:
    def test_any_https_answer_counts_and_the_url_is_the_api_host(self):
        seen = []
        result = netctl.reach_bluesignal(API, probe=lambda url: seen.append(url) or "ok")
        assert result["ok"] is True
        assert seen == ["https://api.bluesignal.test/"]

    def test_blocked_for_two_minutes_then_reachable_joins(self):
        c = FakeClock()
        result = netctl.reach_bluesignal(
            API,
            sleep=c.sleep,
            clock=c.clock,
            probe=lambda url: "ok" if c.t >= 120 else "fail",
        )
        assert result["ok"] is True
        assert 120 <= c.t < 180

    def test_blocked_past_three_minutes_gives_up_at_three(self):
        c = FakeClock()
        result = netctl.reach_bluesignal(
            API,
            sleep=c.sleep,
            clock=c.clock,
            probe=lambda url: "ok" if c.t >= 200 else "fail",
        )
        assert result["ok"] is False
        assert 180 <= c.t < 200

    def test_blocked_for_two_hundred_seconds_fails_the_join_as_no_bluesignal(self, radio):
        c = FakeClock()
        result = netctl.join_network(
            HOME,
            RIGHT,
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=c.sleep,
            clock=c.clock,
            api_base=API,
            reach=lambda base, **k: netctl.reach_bluesignal(
                base, probe=lambda url: "ok" if c.t >= 200 else "fail", **k
            ),
        )
        assert result["reason"] == "no_bluesignal"
        assert result["message"] == netctl._JOIN_MESSAGES["no_bluesignal"]

    def test_no_configured_api_host_is_not_a_success(self):
        assert netctl.reach_bluesignal("", probe=lambda url: "ok")["ok"] is False


# -- The bounded clock fix ----------------------------------------------------


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _clock_case(header: datetime | None, *, synced: bool = False):
    moved: list[datetime] = []
    dates: list[str] = []
    state = {"set": False}

    def probe(url):
        return "ok" if state["set"] else "time"

    def set_time(when):
        moved.append(when)
        state["set"] = True
        return True

    result = netctl.reach_bluesignal(
        API,
        hold_s=0,
        probe=probe,
        http_date=lambda url: dates.append(url) or header,
        now=lambda: NOW,
        set_time=set_time,
        ntp_synced=lambda: synced,
    )
    return result, moved, dates


class TestClockFix:
    def test_one_year_eleven_months_behind_is_set_forward_and_retried(self):
        header = datetime(2028, 9, 26, 12, 0, tzinfo=UTC)
        result, moved, dates = _clock_case(header)
        assert moved == [header]
        assert result["ok"] is True
        assert dates == ["http://api.bluesignal.test/"]

    def test_exactly_two_years_behind_is_accepted(self):
        header = datetime(2028, 10, 26, 12, 0, tzinfo=UTC)
        result, moved, _ = _clock_case(header)
        assert moved == [header]
        assert result["ok"] is True

    def test_two_years_and_one_day_is_refused(self):
        result, moved, _ = _clock_case(datetime(2028, 10, 27, 12, 0, tzinfo=UTC))
        assert moved == []
        assert result["ok"] is False

    def test_a_clock_ahead_of_the_header_is_never_moved_back(self):
        result, moved, _ = _clock_case(NOW - timedelta(days=1))
        assert moved == []
        assert result["ok"] is False

    def test_a_date_in_2099_is_refused(self):
        result, moved, _ = _clock_case(datetime(2099, 1, 1, tzinfo=UTC))
        assert moved == []
        assert result["ok"] is False

    def test_a_synced_clock_is_left_alone(self):
        result, moved, dates = _clock_case(datetime(2027, 1, 1, tzinfo=UTC), synced=True)
        assert moved == []
        assert dates == []

    def test_the_clock_is_set_forward_at_most_once_per_boot(self):
        header = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
        first, moved, _ = _clock_case(header)
        assert moved == [header]
        assert netctl.clock_step_marker().exists()
        second, moved_again, dates = _clock_case(header)
        assert moved_again == []
        assert dates == [], "no Date header is even read"
        assert second["clock"] == {"action": "refused", "reason": "once_per_boot"}
        assert second["ok"] is False

    def test_a_marker_that_cannot_be_written_still_refuses_a_second_step(
        self, monkeypatch, tmp_path
    ):
        # /run/bluesignal not writable: the cap must fail closed, not open.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv("WQM1_CLOCK_STEP_MARKER", str(blocker / "clock-stepped"))
        header = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
        _first, moved, _ = _clock_case(header)
        assert moved == [header]
        assert not netctl.clock_step_marker().exists()
        second, moved_again, _ = _clock_case(header)
        assert moved_again == []
        assert second["clock"] == {"action": "refused", "reason": "once_per_boot"}

    def test_a_failed_step_does_not_use_up_the_boot(self):
        result = netctl._repair_clock(
            "api.bluesignal.test",
            http_date=lambda url: datetime(2027, 1, 1, tzinfo=UTC),
            now=lambda: NOW,
            set_time=lambda when: False,
            ntp_synced=lambda: False,
        )
        assert result["action"] == "set_failed"
        assert not netctl.clock_step_marker().exists()

    def test_the_cap_handles_29_february(self):
        leap = datetime(2028, 2, 29, tzinfo=UTC)
        assert netctl.clock_step_allowed(leap, datetime(2030, 2, 28, tzinfo=UTC))
        assert not netctl.clock_step_allowed(leap, datetime(2030, 3, 1, tzinfo=UTC))


# -- The real HTTPS and Date checks (conftest stubs them everywhere else) ------


class _Resp:
    def __init__(self, headers=None):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _cert_error(code: int, message: str = "certificate verify failed"):
    err = ssl.SSLCertVerificationError(1, message)
    err.verify_code = code
    err.verify_message = message
    return err


def _headers(**fields):
    msg = email.message.Message()
    for key, value in fields.items():
        msg[key] = value
    return msg


class TestRealHttpsCheck:
    def _status(self, monkeypatch, outcome):
        def fake_urlopen(request, timeout=None, context=None):
            assert request.full_url.startswith("https://")
            assert context is not None and context.verify_mode == ssl.CERT_REQUIRED
            if isinstance(outcome, BaseException):
                raise outcome
            return _Resp()

        monkeypatch.setattr(netctl.urllib.request, "urlopen", fake_urlopen)
        return REAL_HTTPS_STATUS("https://api.bluesignal.test/")

    def test_any_answer_is_ok(self, monkeypatch):
        assert self._status(monkeypatch, None) == "ok"

    def test_an_http_error_status_is_still_an_answer(self, monkeypatch):
        error = urllib.error.HTTPError("https://api.bluesignal.test/", 404, "Not Found", None, None)
        assert self._status(monkeypatch, error) == "ok"

    @pytest.mark.parametrize("code", [9, 10])
    def test_a_certificate_time_error_is_time(self, monkeypatch, code):
        wrapped = urllib.error.URLError(_cert_error(code))
        assert self._status(monkeypatch, wrapped) == "time"
        assert self._status(monkeypatch, _cert_error(code)) == "time"

    def test_a_not_yet_valid_message_is_time_without_a_code(self, monkeypatch):
        err = _cert_error(0, "certificate is not yet valid")
        assert self._status(monkeypatch, urllib.error.URLError(err)) == "time"

    def test_another_certificate_error_is_a_failure(self, monkeypatch):
        err = _cert_error(62, "hostname mismatch")
        assert self._status(monkeypatch, urllib.error.URLError(err)) == "fail"

    @pytest.mark.parametrize(
        "error",
        [OSError("unreachable"), urllib.error.URLError(OSError("no route")), ValueError("x")],
    )
    def test_no_answer_is_a_failure(self, monkeypatch, error):
        assert self._status(monkeypatch, error) == "fail"


class TestRealDateHeader:
    def _date(self, monkeypatch, outcome):
        class FakeOpener:
            def open(self, request, timeout=None):
                assert request.full_url.startswith("http://")
                if isinstance(outcome, BaseException):
                    raise outcome
                return _Resp(outcome)

        monkeypatch.setattr(netctl, "_DATE_OPENER", FakeOpener())
        return REAL_HTTP_DATE("http://api.bluesignal.test/")

    def test_a_redirect_to_https_is_read_not_followed(self):
        """A host that sends plain HTTP on to HTTPS answers 301 with a Date.

        Following it would fail the certificate check the wrong clock is
        failing in the first place, so the Date must come from the 301.
        A real local server, because a redirect is the opener's business.
        """
        import http.server

        followed: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):  # noqa: N802 - http.server's name
                if self.path != "/":
                    followed.append(self.path)
                self.send_response_only(301)  # no automatic Date of its own
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/next")
                self.send_header("Date", "Mon, 26 Oct 2026 12:00:00 GMT")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            got = REAL_HTTP_DATE(f"http://127.0.0.1:{server.server_port}/", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        assert got == datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        assert followed == [], "the redirect was followed"

    def test_a_good_header_is_read_as_utc(self, monkeypatch):
        got = self._date(monkeypatch, _headers(Date="Mon, 26 Oct 2026 12:00:00 GMT"))
        assert got == datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def test_an_offset_is_converted_to_utc(self, monkeypatch):
        got = self._date(monkeypatch, _headers(Date="Mon, 26 Oct 2026 07:00:00 -0500"))
        assert got == datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def test_an_error_status_still_carries_a_date(self, monkeypatch):
        error = urllib.error.HTTPError(
            "http://api.bluesignal.test/",
            404,
            "Not Found",
            _headers(Date="Mon, 26 Oct 2026 12:00:00 GMT"),
            None,
        )
        assert self._date(monkeypatch, error) == datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "outcome",
        [
            _headers(),
            _headers(Date="not a date"),
            # -0000 means "no zone information": parsed naive, never trusted.
            _headers(Date="Mon, 26 Oct 2026 12:00:00 -0000"),
            OSError("unreachable"),
        ],
        ids=["missing", "garbage", "naive", "no-answer"],
    )
    def test_anything_else_is_none(self, monkeypatch, outcome):
        assert self._date(monkeypatch, outcome) is None


# -- join.start_join / join.read_state ----------------------------------------


class TestStartJoin:
    def test_wrong_password_is_failed_and_survives_a_restart(self, radio):
        assert join.start_join(HOME, "wrong-password")["state"] == "joining"
        state = _wait_done()
        assert state["state"] == "failed"
        assert state["reason"] == "wrong_password"
        assert state["ap_restored"] is True

        importlib.reload(join)  # a restart of the process that served the page
        again = join.read_state()
        assert again["state"] == "failed"
        assert again["reason"] == "wrong_password"
        assert again["ssid"] == HOME

    def test_a_second_join_while_one_runs_is_busy_with_the_first_ssid(self, monkeypatch):
        release = threading.Event()
        calls = []

        def slow_join(ssid, *a, **k):
            calls.append(ssid)
            release.wait(5)
            return {"connected": False, "reason": "no_association", "message": "x"}

        monkeypatch.setattr(netctl, "join_network", slow_join)
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))
        first = join.start_join(HOME, RIGHT)
        time.sleep(0.1)
        second = join.start_join("Neighbour", "other-pass-1")
        release.set()
        _wait_done()
        assert first == {"state": "joining", "ssid": HOME}
        assert second == {"state": "busy", "ssid": HOME}
        assert calls == [HOME]

    def test_joined_is_recorded_before_the_follow_up_runs(self, radio):
        """A follow-up that hangs or dies must not leave a proven join
        reading as joining or interrupted (review of #167)."""
        release = threading.Event()
        seen: list[str] = []

        def slow_follow_up(_result):
            seen.append(join.read_state()["state"])
            release.wait(5)

        join.start_join(HOME, RIGHT, on_joined=slow_follow_up)
        end = time.monotonic() + 5
        while not seen and time.monotonic() < end:
            time.sleep(0.01)
        assert seen == ["joined"]
        assert join.read_state()["state"] == "joined"
        assert join.wait_for_follow_up(timeout_s=0.05) is False
        release.set()
        assert join.wait_for_follow_up(timeout_s=5) is True
        assert join.read_state()["state"] == "joined"

    def test_the_lock_is_free_again_after_a_join(self, radio):
        join.start_join(HOME, RIGHT)
        _wait_done()
        assert not join._lock_held()
        assert join.start_join(HOME, RIGHT)["state"] == "joining"
        _wait_done()

    def test_older_networks_are_forgotten_only_after_bluesignal_answered(self, radio, monkeypatch):
        radio.profiles = {"BlueSignal-Shop": "BlueSignal-Shop"}
        order: list[str] = []
        real_forget = netctl.forget_saved_wifi
        verdict = {"ok": False}

        def reach(*a, **k):
            order.append("reach")
            return {"ok": verdict["ok"], "clock": None}

        def forget(keep, iface="wlan0"):
            order.append("forget")
            return real_forget(keep, iface)

        monkeypatch.setattr(netctl, "reach_bluesignal", reach)
        monkeypatch.setattr(netctl, "forget_saved_wifi", forget)

        join.start_join(HOME, RIGHT, api_base=API)
        assert _wait_done()["reason"] == "no_bluesignal"
        assert order == ["reach"]
        assert "BlueSignal-Shop" in radio.profiles

        order.clear()
        verdict["ok"] = True
        join.start_join(HOME, RIGHT, api_base=API)
        state = _wait_done()
        assert state["state"] == "joined"
        assert order == ["reach", "forget"]
        assert "BlueSignal-Shop" not in radio.profiles
        assert state["forgotten"] == ["BlueSignal-Shop"]

    def test_the_worker_waits_its_head_start_before_the_radio(self, radio, monkeypatch):
        events: list[str] = []
        monkeypatch.setattr(join, "START_DELAY_S", 0.25)
        monkeypatch.setattr(join, "_sleep", lambda s: events.append(f"sleep {s}"))
        real = netctl.join_network
        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *a, **k: events.append("join") or real(*a, **k),
        )
        join.start_join(HOME, RIGHT)
        _wait_done()
        assert events[:2] == ["sleep 0.25", "join"]

    def test_the_shipped_head_start_is_one_and_a_half_seconds(self):
        spec = importlib.util.spec_from_file_location("join_as_shipped", join.__file__)
        shipped = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shipped)
        assert shipped.START_DELAY_S == 1.5

    def test_a_join_that_died_mid_way_reads_as_failed(self, radio):
        join.ensure_state_dir()
        join.state_path().write_text(json.dumps({"state": "joining", "ssid": HOME}))
        state = join.read_state()
        assert state["state"] == "failed"
        assert state["reason"] == "interrupted"

    def test_the_state_file_is_private(self, radio):
        join.start_join(HOME, RIGHT)
        _wait_done()
        assert join.state_path().stat().st_mode & 0o777 == 0o600


# -- I2: the password goes nowhere --------------------------------------------


class TestPasswordGoesNowhere:
    def _files(self, *roots: Path) -> list[Path]:
        return [p for root in roots if root.exists() for p in root.rglob("*") if p.is_file()]

    @pytest.mark.parametrize("password", ["wrong-but-secret-1", RIGHT])
    def test_password_is_in_no_log_file_or_state(self, radio, caplog, tmp_path, password):
        radio.echo_password = True
        caplog.set_level(logging.DEBUG)
        join.start_join(HOME, password, api_base=API)
        state = _wait_done()

        assert password not in caplog.text
        for path in self._files(join.state_dir(), tmp_path / "boot"):
            assert password.encode() not in path.read_bytes(), path
        assert password not in json.dumps(state)


# -- W7: the last five attempts on the card -----------------------------------


class TestAttemptsOnTheCard:
    def test_the_last_five_attempts_are_kept_newest_first(self, tmp_path, monkeypatch):
        from diagnostics.boot_status import JOIN_HISTORY, record_join_attempt

        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        for i in range(JOIN_HISTORY + 1):
            record_join_attempt(f"Net{i}", "wrong_password")
        lines = [ln for ln in status.read_text().splitlines() if ln.startswith("join_")]
        assert len(lines) == JOIN_HISTORY
        assert lines[0].startswith("join_1:") and lines[0].endswith("Net5 wrong_password")
        assert lines[-1].endswith("Net1 wrong_password")

    def test_a_line_holding_the_typed_password_is_redacted(self, tmp_path, monkeypatch):
        from diagnostics.boot_status import record_join_attempt

        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        record_join_attempt("hunter2-network", "joined", secret="hunter2-network")
        text = status.read_text()
        assert "hunter2-network" not in text
        assert "join_1: [redacted]" in text
