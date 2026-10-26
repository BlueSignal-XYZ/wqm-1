"""The Wi-Fi page on port 80 (install plan §4).

The listener serves the page and its API itself. These tests run it on
127.0.0.1 with the Service Window and the firmware not running, against the
virtual radio (``utils.netsim``) or a cached scan. They pin:

* I7: the page and a join work with nothing else running;
* I1: nothing but the page and its API answers on port 80;
* I5: the page never reloads itself or replaces its form;
* I2: the typed password reaches no response, log, or file;
* the same keys in ``setup_copy.en`` and ``setup_copy.es``.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import string
import threading
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from utils import captive_portal, join, netsim, setup_copy
from utils.wifi_setup import annotate_network

PASSWORD = "riverstone42"  # nosec B105 — the virtual network's demo password


@pytest.fixture
def unit(tmp_path, monkeypatch):
    """A virtual unit on its setup network. No firmware, no Service Window."""
    monkeypatch.setenv("WQM1_VIRTUAL_NET", str(tmp_path / "netsim.json"))
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "mode"))
    state = netsim._default_state()
    state.update(ap=True, ap_ssid="WQM1-0001")
    netsim._write(state)
    return tmp_path


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def portal(unit):
    server = captive_portal.make_server("127.0.0.1", 0)
    thread = _serve(server)
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _call(server, method, path, body=None, headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    sent = {"Host": "192.168.4.1", "Connection": "close"}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    conn.request(method, path, body=data, headers=sent)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp, payload


def _json(server, method, path, body=None, headers=None):
    resp, payload = _call(server, method, path, body=body, headers=headers)
    return resp.status, json.loads(payload)


def _wait_done(timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not join._lock_held():
            return join.read_state()
        time.sleep(0.02)
    raise AssertionError("join did not finish")


# --- I7: works with nothing else running -------------------------------------


class TestWorksAlone:
    def test_the_page_and_a_join_need_no_service_window_and_no_firmware(self, portal):
        resp, page = _call(portal, "GET", "/setup/")
        assert resp.status == 200
        assert b'id="status"' in page

        status, nets = _json(portal, "GET", "/api/networks")
        assert status == 200
        assert netsim.DEMO_HOME_SSID in [n["ssid"] for n in nets["networks"]]

        status, answer = _json(
            portal, "POST", "/api/join", {"ssid": netsim.DEMO_HOME_SSID, "password": PASSWORD}
        )
        assert status == 202
        assert answer == {"state": "joining", "ssid": netsim.DEMO_HOME_SSID}
        assert _wait_done()["state"] == "joined"
        assert netsim.read_state()["station"] == netsim.DEMO_HOME_SSID

        status, state = _json(portal, "GET", "/api/state")
        assert status == 200
        assert state["state"] == "joined"
        assert state["ssid"] == netsim.DEMO_HOME_SSID

    def test_a_wrong_password_comes_back_as_its_reason(self, portal):
        status, _answer = _json(
            portal, "POST", "/api/join", {"ssid": netsim.DEMO_HOME_SSID, "password": "wrongpass1"}
        )
        assert status == 202
        _wait_done()
        status, state = _json(portal, "GET", "/api/state")
        assert state["state"] == "failed"
        assert state["reason"] == "wrong_password"
        assert netsim.ap_active(), "the setup network is back (I6)"

    def test_the_join_goes_through_the_join_service(self, portal):
        seen = []
        portal.join_request = lambda ssid, password, hidden: (
            seen.append((ssid, password, hidden)) or {"state": "joining", "ssid": ssid}
        )
        status, _ = _json(
            portal, "POST", "/api/join", {"ssid": netsim.DEMO_HOME_SSID, "password": PASSWORD}
        )
        assert status == 202
        assert seen == [(netsim.DEMO_HOME_SSID, PASSWORD, False)]


# --- I1: nothing else answers --------------------------------------------------


class TestNothingElseAnswers:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/settings"),
            ("GET", "/relays"),
            ("POST", "/relays/1"),
            ("GET", "/setup/network"),
            ("POST", "/setup/network"),
            ("GET", "/setup/identity"),
            ("GET", "/calibration"),
            ("POST", "/provision"),
            ("GET", "/api/unknown"),
            ("GET", "/api/join"),
            ("POST", "/api/state"),
            ("PUT", "/api/join"),
            ("DELETE", "/api/state"),
            ("POST", "/setup/"),
        ],
    )
    def test_service_window_paths_are_404(self, portal, method, path):
        resp, page = _call(portal, method, path)
        assert resp.status == 404
        assert b'href="/setup/"' in page
        assert resp.getheader("Location") is None

    def test_classify_names_only_the_page_and_four_routes(self):
        assert captive_portal.classify_request("/", "192.168.4.1") == "page"
        assert captive_portal.classify_request("/setup/", "192.168.4.1") == "page"
        assert captive_portal.classify_request("/api/networks", "192.168.4.1") == "api:networks"
        assert captive_portal.classify_request("/api/state", "192.168.4.1") == "api:state"
        assert captive_portal.classify_request("/api/join", "192.168.4.1", "POST") == "api:join"
        assert captive_portal.classify_request("/api/rescan", "192.168.4.1", "POST") == "api:rescan"
        assert captive_portal.classify_request("/settings", "192.168.4.1") == "not_found"
        assert captive_portal.classify_request("/api/join", "example.com", "POST") == "redirect"

    def test_another_sites_post_is_refused(self, portal):
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}):
            status, answer = _json(
                portal,
                "POST",
                "/api/join",
                {"ssid": netsim.DEMO_HOME_SSID, "password": PASSWORD},
                headers=headers,
            )
            assert status == 403, headers
            assert answer == {"error": "try_again"}
        assert not join._lock_held()
        assert join.read_state()["state"] == "idle"

    def test_the_page_itself_may_post(self, portal):
        portal.join_request = lambda ssid, password, hidden: {"state": "joining", "ssid": ssid}
        status, _ = _json(
            portal,
            "POST",
            "/api/join",
            {"ssid": netsim.DEMO_HOME_SSID, "password": PASSWORD},
            headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://192.168.4.1"},
        )
        assert status == 202


# --- I5: no reload, no form replacement ----------------------------------------


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.meta: list[dict[str, str]] = []
        self.srcs: list[str] = []
        self.links: list[str] = []
        self.forms = 0
        self.ids: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "script":
            self._in_script = True
            self.scripts.append("")
        if tag == "meta":
            self.meta.append(a)
        if tag == "form":
            self.forms += 1
        if tag == "link":
            self.links.append(a.get("href", ""))
        for key in ("src", "href", "action"):
            if key in a:
                self.srcs.append(a[key])
        if "id" in a:
            self.ids.append(a["id"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts[-1] += data


def _parse(lang: str) -> _Page:
    parser = _Page()
    parser.feed(captive_portal.render_page(lang).decode("utf-8"))
    return parser


@pytest.mark.parametrize("lang", ["en", "es"])
class TestNoReload:
    def test_the_rendered_page_never_reloads_or_navigates(self, lang):
        page = _parse(lang)
        code = "\n".join(page.scripts)
        for banned in ("location.reload", "location.href =", "location.href=", "location.replace("):
            assert banned not in code, banned
        assert re.search(r"\blocation\s*\.\s*(assign|reload|replace)\b", code) is None
        assert re.search(r"\blocation\s*=", code) is None
        for meta in page.meta:
            assert meta.get("http-equiv", "").lower() != "refresh"
        html_text = captive_portal.render_page(lang).decode("utf-8").lower()
        assert 'http-equiv="refresh"' not in html_text

    def test_status_changes_edit_text_and_never_rebuild_the_form(self, lang):
        page = _parse(lang)
        code = "\n".join(page.scripts)
        assert page.forms == 1
        assert "innerHTML" not in code
        assert "outerHTML" not in code
        assert "document.write" not in code
        assert ".submit()" not in code
        assert "status.textContent" in code
        assert "setInterval(poll,3000)" in code

    def test_nothing_on_the_page_is_fetched_from_elsewhere(self, lang):
        page = _parse(lang)
        assert page.links == []
        for target in page.srcs:
            assert not target.startswith(("http:", "https:", "//")), target
        code = "\n".join(page.scripts)
        assert re.search(r"fetch\(\s*\"https?:", code) is None

    def test_the_page_does_not_name_the_unit(self, lang):
        body = captive_portal.render_page(lang)
        assert b"WQM" not in body
        assert b"BS-WQM1" not in body
        assert b"192.168.4.1" not in body


class TestHeaders:
    def test_the_page_forbids_anything_external(self, portal):
        resp, _ = _call(portal, "GET", "/setup/")
        policy = resp.getheader("Content-Security-Policy") or ""
        assert "default-src 'none'" in policy
        assert "connect-src 'self'" in policy
        assert resp.getheader("Cache-Control") == "no-store"

    def test_head_has_headers_and_no_body(self, portal):
        resp, payload = _call(portal, "HEAD", "/setup/")
        assert resp.status == 200
        assert payload == b""
        assert int(resp.getheader("Content-Length") or 0) > 0


# --- Language -------------------------------------------------------------------


class TestCopy:
    def test_english_and_spanish_have_the_same_keys(self):
        assert set(setup_copy.en) == set(setup_copy.es)

    def test_every_sentence_is_filled_and_keeps_its_placeholders(self):
        fields = string.Formatter()
        for key, english in setup_copy.en.items():
            spanish = setup_copy.es[key]
            assert english.strip() and spanish.strip(), key
            names_en = {f for _, f, _, _ in fields.parse(english) if f}
            names_es = {f for _, f, _, _ in fields.parse(spanish) if f}
            assert names_en == names_es, key
            assert names_en <= {"ssid"}, key

    def test_every_reason_the_join_can_return_has_a_sentence(self):
        from utils.netctl import _STATION_FAIL

        for reason in [*_STATION_FAIL, "interrupted", "no_bluesignal"]:
            assert reason in setup_copy.en, reason

    def test_every_refusal_and_password_code_has_a_sentence(self):
        for key in (
            "refused_band",
            "refused_enterprise",
            "refused_sae_only",
            "refused_wep",
            "password_needed",
            "password_char",
            "password_short",
            "password_long",
            "trailing_space",
            "pick_first",
            "network_name_bad",
            "try_again",
            "rescan_failed",
        ):
            assert key in setup_copy.en, key

    @pytest.mark.parametrize(
        ("header", "lang"),
        [
            ("", "en"),
            ("es", "es"),
            ("es-MX,es;q=0.9,en;q=0.5", "es"),
            ("en-US,en;q=0.9,es;q=0.8", "en"),
            ("fr-FR,es;q=0.6", "es"),
            ("de-DE", "en"),
            ("es;q=bad,en", "en"),
        ],
    )
    def test_language_follows_accept_language(self, header, lang):
        assert setup_copy.pick_language(header) == lang

    def test_the_page_is_served_in_the_phones_language(self, portal):
        resp, page = _call(portal, "GET", "/setup/", headers={"Accept-Language": "es-US,es"})
        assert b'<html lang="es">' in page
        assert setup_copy.es["connect"].encode() in page
        resp, page = _call(portal, "GET", "/setup/", headers={"Accept-Language": "en-US"})
        assert b'<html lang="en">' in page

    def test_copy_in_the_script_cannot_close_it(self):
        assert "</" not in captive_portal._script_json({"x": "</script><b>"})


# --- The network list ---------------------------------------------------------


def _cached(server, rows):
    server.networks_source = lambda: [annotate_network(r) for r in rows]


class TestNetworks:
    def test_joinable_first_then_refused_with_a_code(self, portal):
        _cached(
            portal,
            [
                {"ssid": "Office5G", "signal": 90, "secured": True, "freq_mhz": 5180},
                {"ssid": "Corp", "signal": 80, "security": "enterprise"},
                {"ssid": "PondHouse", "signal": 60, "secured": True, "freq_mhz": 2437},
                {"ssid": "Cafe", "signal": 50, "secured": False, "freq_mhz": 2412},
                {"ssid": "", "signal": 40, "hidden": True},
            ],
        )
        status, view = _json(portal, "GET", "/api/networks")
        assert status == 200
        names = [n["ssid"] for n in view["networks"]]
        assert names[:2] == ["PondHouse", "Cafe"]
        assert set(names[2:]) == {"Office5G", "Corp"}
        by = {n["ssid"]: n for n in view["networks"]}
        assert by["Office5G"]["note"] == "refused_band"
        assert by["Corp"]["note"] == "refused_enterprise"
        assert by["Cafe"]["note"] == "open_network"
        assert by["Cafe"]["joinable"] is True
        assert view["hidden"] is True
        assert view["only_24"] is False
        assert "" not in names

    def test_only_5ghz_in_range_says_so(self, portal):
        _cached(portal, [{"ssid": "Office5G", "signal": 90, "secured": True, "freq_mhz": 5180}])
        _status, view = _json(portal, "GET", "/api/networks")
        assert view["only_24"] is True

    def test_weak_and_phone_hotspot_are_flagged(self, portal):
        _cached(
            portal,
            [
                {"ssid": "Barn", "signal": 10, "secured": True, "freq_mhz": 2412},
                {"ssid": "Dana's iPhone", "signal": 70, "secured": True, "freq_mhz": 2412},
            ],
        )
        _status, view = _json(portal, "GET", "/api/networks")
        by = {n["ssid"]: n for n in view["networks"]}
        assert by["Barn"]["weak"] is True
        assert by["Dana's iPhone"]["phone"] is True
        assert by["Barn"]["phone"] is False

    def test_a_hostile_network_name_is_data_not_markup(self, portal):
        _cached(portal, [{"ssid": "<img src=x onerror=alert(1)>", "signal": 70, "secured": True}])
        resp, payload = _call(portal, "GET", "/api/networks")
        assert resp.getheader("Content-Type") == "application/json"
        assert json.loads(payload)["networks"][0]["ssid"] == "<img src=x onerror=alert(1)>"

    def test_the_cached_scan_is_what_a_real_unit_lists(self, tmp_path, monkeypatch):
        from utils import netctl

        monkeypatch.delenv("WQM1_VIRTUAL_NET", raising=False)
        monkeypatch.setenv("WQM1_SCAN_CACHE", str(tmp_path / "scan.json"))
        assert captive_portal.load_networks() == []
        netctl.write_scan_cache([{"ssid": "PondHouse", "signal": 60, "secured": True}])
        assert [n["ssid"] for n in captive_portal.load_networks()] == ["PondHouse"]
        assert (tmp_path / "scan.json").stat().st_mode & 0o777 == 0o644


# --- Join validation ------------------------------------------------------------


class TestJoinValidation:
    @pytest.fixture
    def started(self, portal):
        calls = []

        def fake(ssid, password, hidden):
            calls.append({"ssid": ssid, "password": password, "hidden": hidden})
            return {"state": "joining", "ssid": ssid}

        portal.join_request = fake
        _cached(
            portal,
            [
                {"ssid": "PondHouse", "signal": 60, "secured": True, "freq_mhz": 2437},
                {"ssid": "Cafe", "signal": 50, "secured": False, "freq_mhz": 2412},
                {"ssid": "Office5G", "signal": 90, "secured": True, "freq_mhz": 5180},
                {"ssid": "OldRouter", "signal": 40, "security": "wep"},
            ],
        )
        return portal, calls

    def _post(self, server, body):
        return _json(server, "POST", "/api/join", body)

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            ({}, "pick_first"),
            ({"ssid": ""}, "pick_first"),
            ({"ssid": "   ", "hidden": True}, "pick_first"),
            ({"ssid": "x" * 33, "password": "hunter2222"}, "network_name_bad"),
            ({"ssid": "Bad\nName", "password": "hunter2222"}, "network_name_bad"),
            ({"ssid": "Office5G", "password": "hunter2222"}, "refused_band"),
            ({"ssid": "OldRouter", "password": "hunter2222"}, "refused_wep"),
            ({"ssid": "PondHouse"}, "password_needed"),
            ({"ssid": "PondHouse", "password": "short"}, "password_short"),
            ({"ssid": "PondHouse", "password": "x" * 64}, "password_long"),
            ({"ssid": "PondHouse", "password": "tab\there1"}, "password_char"),
            ({"ssid": "PondHouse", "password": 12345678}, "try_again"),
            ({"ssid": "PondHouse", "password": "hunter2222 "}, "trailing_space"),
        ],
    )
    def test_refused_before_the_radio_is_touched(self, started, body, error):
        server, calls = started
        status, answer = self._post(server, body)
        assert status == 400
        assert answer == {"error": error}
        assert calls == []

    def test_an_acknowledged_trailing_space_is_trimmed(self, started):
        server, calls = started
        status, _ = self._post(
            server, {"ssid": "PondHouse", "password": "hunter2222 ", "trim_ack": True}
        )
        assert status == 202
        assert calls[-1]["password"] == "hunter2222"

    def test_a_kept_trailing_space_is_kept(self, started):
        server, calls = started
        status, _ = self._post(
            server, {"ssid": "PondHouse", "password": "hunter2222 ", "keep_space": True}
        )
        assert status == 202
        assert calls[-1]["password"] == "hunter2222 "

    def test_an_open_network_needs_no_password(self, started):
        server, calls = started
        status, _ = self._post(server, {"ssid": "Cafe", "password": ""})
        assert status == 202
        assert calls[-1] == {"ssid": "Cafe", "password": None, "hidden": False}

    def test_a_typed_name_joins_as_hidden(self, started):
        server, calls = started
        status, answer = self._post(
            server, {"ssid": "  Barn Loft ", "password": "hunter2222", "hidden": True}
        )
        assert status == 202
        assert answer["ssid"] == "Barn Loft"
        assert calls[-1] == {"ssid": "Barn Loft", "password": "hunter2222", "hidden": True}

    def test_a_name_not_in_the_list_joins_as_hidden(self, started):
        server, calls = started
        status, _ = self._post(server, {"ssid": "Elsewhere", "password": "hunter2222"})
        assert status == 202
        assert calls[-1]["hidden"] is True

    def test_busy_names_the_running_join(self, portal):
        portal.join_request = lambda *a, **k: {"state": "busy", "ssid": "SmithHome"}
        _cached(portal, [{"ssid": "PondHouse", "signal": 60, "secured": True}])
        status, answer = _json(
            portal, "POST", "/api/join", {"ssid": "PondHouse", "password": "hunter2222"}
        )
        assert status == 409
        assert answer == {"state": "busy", "ssid": "SmithHome"}

    def test_a_join_service_that_is_down_says_try_again(self, portal):
        portal.join_request = lambda *a, **k: {"error": "unavailable"}
        _cached(portal, [{"ssid": "PondHouse", "signal": 60, "secured": True}])
        status, answer = _json(
            portal, "POST", "/api/join", {"ssid": "PondHouse", "password": "hunter2222"}
        )
        assert status == 503
        assert answer == {"error": "try_again"}

    def test_a_large_or_broken_body_is_refused(self, portal):
        resp, payload = _call(
            portal,
            "POST",
            "/api/join",
            raw=b"{" + b" " * (captive_portal.MAX_BODY + 1),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 413
        resp, payload = _call(portal, "POST", "/api/join", raw=b"not json")
        assert resp.status == 400
        assert json.loads(payload) == {"error": "try_again"}

    def test_a_started_join_is_watched_for_registration(self, started, monkeypatch):
        server, _calls = started
        kicks = []
        monkeypatch.setattr(captive_portal, "_kick_register", lambda: kicks.append(1))
        monkeypatch.setattr(join, "read_state", lambda: {"state": "joined", "ssid": "PondHouse"})
        status, _ = self._post(server, {"ssid": "PondHouse", "password": "hunter2222"})
        assert status == 202
        assert captive_portal.check_watched_join() == "joined"
        assert kicks == [1]
        assert captive_portal.check_watched_join() is None


class TestWatchedJoin:
    def test_a_failed_join_kicks_nothing(self, monkeypatch):
        kicks = []
        monkeypatch.setattr(captive_portal, "_kick_register", lambda: kicks.append(1))
        monkeypatch.setattr(join, "read_state", lambda: {"state": "failed", "ssid": "PondHouse"})
        captive_portal.watch_join("PondHouse")
        assert captive_portal.check_watched_join() == "failed"
        assert kicks == []

    def test_a_running_join_is_checked_again_later(self, monkeypatch):
        kicks = []
        monkeypatch.setattr(captive_portal, "_kick_register", lambda: kicks.append(1))
        states = iter(
            [{"state": "joining", "ssid": "PondHouse"}, {"state": "joined", "ssid": "PondHouse"}]
        )
        monkeypatch.setattr(join, "read_state", lambda: next(states))
        captive_portal.watch_join("PondHouse")
        now = time.monotonic()
        assert captive_portal.check_watched_join(now) == "joining"
        assert captive_portal.check_watched_join(now + 0.5) is None, "throttled"
        assert captive_portal.check_watched_join(now + 3) == "joined"
        assert kicks == [1]

    def test_the_watch_ends(self, monkeypatch):
        monkeypatch.setattr(join, "read_state", lambda: {"state": "joining", "ssid": "x"})
        captive_portal.watch_join("x")
        late = time.monotonic() + captive_portal.JOIN_WATCH_FOR_S + 1
        assert captive_portal.check_watched_join(late) is None
        assert captive_portal._watching == {}


# --- Rescan -----------------------------------------------------------------


class TestRescan:
    def test_a_good_rescan_says_ok(self, portal):
        portal.rescan_request = lambda: {"ok": True, "count": 3}
        assert _json(portal, "POST", "/api/rescan", {}) == (200, {"ok": True})

    def test_a_failed_rescan_keeps_the_list_and_says_so(self, portal):
        portal.rescan_request = lambda: {"ok": False, "count": 0}
        assert _json(portal, "POST", "/api/rescan", {}) == (
            200,
            {"ok": False, "error": "rescan_failed"},
        )
        portal.rescan_request = lambda: {"error": "unavailable"}
        assert _json(portal, "POST", "/api/rescan", {})[1]["error"] == "rescan_failed"


# --- State ------------------------------------------------------------------


class TestState:
    def _write(self, **fields):
        join._write_state(fields)

    def test_a_fresh_failure_is_shown_by_its_code(self, unit):
        self._write(state="failed", ssid="PondHouse", reason="no_dhcp", message="x")
        view = captive_portal.state_view()
        assert view["state"] == "failed"
        assert view["reason"] == "no_dhcp"
        assert "message" not in view

    def test_an_old_failure_is_not_news(self, unit):
        old = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write(state="failed", ssid="PondHouse", reason="no_dhcp", at=old)
        assert captive_portal.state_view()["state"] == "idle"

    def test_an_unknown_reason_reads_as_could_not_join(self, unit):
        self._write(state="failed", ssid="PondHouse", reason="mystery")
        assert captive_portal.state_view()["reason"] == "no_association"

    def test_no_join_yet_is_idle(self, unit):
        assert captive_portal.state_view() == {
            "state": "idle",
            "ssid": None,
            "reason": None,
            "at": None,
        }


# --- I2: the password stays out of everything the page touches ---------------


class TestPasswordStaysOut:
    SECRET = "Sp3cial-Pa55-wqm"  # nosec B105 — a test value searched for below

    def test_a_join_through_the_page_never_writes_or_logs_the_password(self, portal, unit, caplog):
        caplog.set_level(logging.DEBUG)
        status, answer = _json(
            portal,
            "POST",
            "/api/join",
            {"ssid": netsim.DEMO_HOME_SSID, "password": self.SECRET},
        )
        assert status == 202
        assert self.SECRET not in json.dumps(answer)
        _wait_done()
        _status, state = _json(portal, "GET", "/api/state")
        assert self.SECRET not in json.dumps(state)
        assert self.SECRET not in caplog.text
        for path in Path(unit).rglob("*"):
            if path.is_file():
                assert self.SECRET.encode() not in path.read_bytes(), path
