"""
AWG circuit controller — interlock ordering, fail-safe matrix, relay_only,
and the "never energise behind the operator's back" rule.

Driven entirely through FakeSmartBreaker; no credentials, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from integrations.smart_breaker import build_smart_breaker
from integrations.smart_breaker.base import (
    AuthError,
    FailSafeMode,
    RateLimited,
    SmartBreakerClient,
    Unreachable,
)
from integrations.smart_breaker.controller import SmartBreakerController
from integrations.smart_breaker.fake import FakeSmartBreaker


def _settings(**over):
    base = dict(
        smart_breaker_vendor="ableedge",
        smart_breaker_device_id="dev-1",
        smart_breaker_site_id="site-1",
        smart_breaker_circuit_label="AWG-1",
        smart_breaker_circuit_amps=20,
        smart_breaker_interlock_relay=3,
        smart_breaker_poll_s=60,
        smart_breaker_fail_safe="off",
        smart_breaker_unreachable_grace_s=300,
        smart_breaker_auth_mode="direct",
        smart_breaker_api_base="https://api.example.test/api/v1",
        smart_breaker_token_url="https://api.example.test/oauth2/token",
        smart_breaker_client_id="",
        smart_breaker_client_secret="",
        smart_breaker_subscription_key="",
    )
    base.update(over)
    return SimpleNamespace(**base)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


class RecordingRelays:
    """Relay double that records (channel, state) in order and can fault."""

    def __init__(self) -> None:
        self.log: list[tuple[int, bool]] = []
        self.fail = False

    def set(self, ch: int, state: bool) -> None:
        if self.fail:
            raise RuntimeError("GPIO busy")
        self.log.append((ch, state))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(clock):
    return FakeSmartBreaker(device_id="dev-1", is_on=False, clock=clock)


@pytest.fixture
def relays():
    return RecordingRelays()


@pytest.fixture
def events():
    return []


def make(fake, relays, clock, events, **over):
    settings = _settings(**over)
    return SmartBreakerController(
        lambda: settings, fake, relays, clock=clock, event_sink=events.append
    )


class TestFakeBackend:
    def test_implements_the_protocol(self, fake):
        assert isinstance(fake, SmartBreakerClient)

    def test_round_trip(self, fake):
        fake.authenticate()
        assert fake.authenticated
        fake.set_circuit(True, "t")
        assert fake.get_status().is_on is True
        fake.current_a = 7.5
        assert fake.get_power().current_a == 7.5
        assert [c[0] for c in fake.calls] == [
            "authenticate",
            "set_circuit",
            "get_status",
            "get_power",
        ]

    def test_failure_injection(self, fake):
        fake.fail_next(RateLimited("429"))
        with pytest.raises(RateLimited):
            fake.get_status()
        fake.get_status()  # one-shot
        fake.unreachable = True
        with pytest.raises(Unreachable):
            fake.get_status()
        fake.unreachable = False
        fake.reject_auth = True
        with pytest.raises(AuthError):
            fake.get_status()


class TestRequestOn:
    def test_breaker_closes_before_interlock_energises(self, fake, relays, clock, events):
        order: list[str] = []
        fake_set = fake.set_circuit
        fake.set_circuit = lambda on, reason: (order.append("breaker"), fake_set(on, reason))
        relays.set = lambda ch, st: order.append(f"relay{ch}={st}")  # type: ignore[method-assign]
        ctl = make(fake, relays, clock, events)

        result = ctl.request(True, source="cloud", reason="test")

        assert result["ok"] is True
        assert result["breaker"] == "confirmed"
        assert result["interlockRelay"] == 3
        assert order == ["breaker", "relay3=True"]
        assert fake.is_on is True

    def test_failed_close_leaves_interlock_open_and_is_not_queued(
        self, fake, relays, clock, events
    ):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)

        result = ctl.request(True, source="cloud")

        assert result["ok"] is False
        assert result["breaker"] == "unconfirmed"
        assert "Unreachable" in result["error"]
        assert relays.log == []
        assert ctl.status()["pendingCommand"] is None
        assert ctl.status()["desired"] is None

    def test_reason_defaults_to_source(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events)
        ctl.request(True, source="service_window")
        assert fake.calls[-1] == ("set_circuit", (True, "WQM-1 service_window"))


class TestRequestOff:
    def test_interlock_drops_before_breaker_opens(self, fake, relays, clock, events):
        order: list[str] = []
        fake_set = fake.set_circuit
        fake.set_circuit = lambda on, reason: (order.append("breaker"), fake_set(on, reason))
        relays.set = lambda ch, st: order.append(f"relay{ch}={st}")  # type: ignore[method-assign]
        fake.is_on = True
        ctl = make(fake, relays, clock, events)

        result = ctl.request(False, source="cloud")

        assert result["ok"] is True
        assert order == ["relay3=False", "breaker"]
        assert fake.is_on is False

    def test_failed_open_still_drops_interlock_and_queues(self, fake, relays, clock, events):
        fake.is_on = True
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)

        result = ctl.request(False, source="cloud")

        assert result["ok"] is False
        assert result["breaker"] == "unconfirmed"
        assert result["interlockOk"] is True
        assert relays.log == [(3, False)]
        assert ctl.status()["pendingCommand"] == "open"
        assert ctl.status()["desired"] is False

    def test_queued_open_is_delivered_when_link_returns(self, fake, relays, clock, events):
        fake.is_on = True
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)
        ctl.request(False, source="cloud")
        fake.unreachable = False

        ctl.poll()

        assert fake.is_on is False
        assert ctl.status()["pendingCommand"] is None
        assert ctl.link_ok

    def test_relay_fault_is_reported_not_raised(self, fake, relays, clock, events):
        relays.fail = True
        ctl = make(fake, relays, clock, events)
        result = ctl.request(False, source="cloud")
        assert result["ok"] is True  # the breaker did open
        assert result["interlockOk"] is False


class TestNoInterlockRelay:
    def test_zero_means_no_relay_is_touched(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events, smart_breaker_interlock_relay=0)
        r = ctl.request(True, source="cloud")
        assert r["ok"] is True
        assert r["interlockRelay"] is None
        assert relays.log == []

    def test_headerless_board_has_no_relays(self, fake, clock, events):
        ctl = SmartBreakerController(lambda: _settings(), fake, None, clock, events.append)
        assert ctl.request(False, source="cloud")["ok"] is True


class TestFailSafeOff:
    def test_not_applied_inside_grace(self, fake, relays, clock, events):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)
        clock.advance(100)
        ctl.poll()
        assert ctl.status()["failSafeApplied"] is None
        assert relays.log == []
        assert events == []

    def test_applied_once_grace_elapses_since_boot(self, fake, relays, clock, events):
        """A unit that never reaches the vendor after boot must still fail safe."""
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)
        clock.advance(299)
        ctl.poll()
        assert ctl.status()["failSafeApplied"] is None
        clock.advance(1)
        ctl.poll()

        st = ctl.status()
        assert st["failSafeApplied"] == "off"
        assert st["desired"] is False
        assert st["pendingCommand"] == "open"
        assert relays.log == [(3, False)]
        assert len(events) == 1
        assert events[0]["type"] == "smart_breaker_failsafe"
        assert events[0]["details"]["mode"] == "off"
        assert events[0]["details"]["interlockDropped"] is True
        assert events[0]["details"]["downSeconds"] == 300

    def test_applied_only_once(self, fake, relays, clock, events):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)
        clock.advance(300)
        ctl.poll()
        clock.advance(300)
        ctl.poll()
        assert relays.log == [(3, False)]
        assert len(events) == 1

    def test_link_loss_after_good_start(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events)
        ctl.poll()  # link ok — clock at 1000
        assert ctl.link_ok
        fake.unreachable = True
        clock.advance(200)
        ctl.poll()  # down since 1200
        clock.advance(200)
        ctl.poll()  # 200 s down < 300
        assert ctl.status()["failSafeApplied"] is None
        assert ctl.status()["unreachableForS"] == 200
        clock.advance(100)
        ctl.poll()
        assert ctl.status()["failSafeApplied"] == "off"

    def test_recovery_delivers_open_and_emits_restored(self, fake, relays, clock, events):
        fake.is_on = True
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)
        clock.advance(300)
        ctl.poll()
        fake.unreachable = False
        clock.advance(60)

        ctl.poll()

        assert fake.is_on is False  # queued open landed
        st = ctl.status()
        assert st["linkOk"] is True
        assert st["failSafeApplied"] is None
        assert st["pendingCommand"] is None
        assert [e["type"] for e in events] == ["smart_breaker_failsafe", "smart_breaker_restored"]
        assert events[1]["details"]["failSafe"] == "off"

    def test_zero_grace_is_immediate(self, fake, relays, clock, events):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events, smart_breaker_unreachable_grace_s=0)
        ctl.poll()
        assert ctl.status()["failSafeApplied"] == "off"

    def test_auth_failure_counts_as_link_loss(self, fake, relays, clock, events):
        fake.reject_auth = True
        ctl = make(fake, relays, clock, events)
        clock.advance(300)
        ctl.poll()
        assert ctl.status()["failSafeApplied"] == "off"
        assert "AuthError" in ctl.status()["lastError"]

    def test_rate_limit_counts_as_link_loss(self, fake, relays, clock, events):
        fake.rate_limited = True
        ctl = make(fake, relays, clock, events)
        clock.advance(300)
        ctl.poll()
        assert "RateLimited" in ctl.status()["lastError"]
        assert ctl.status()["failSafeApplied"] == "off"

    def test_request_failure_also_triggers_evaluation(self, fake, relays, clock, events):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events)
        clock.advance(300)
        r = ctl.request(True, source="cloud")
        assert r["ok"] is False
        assert ctl.status()["failSafeApplied"] == "off"


class TestFailSafeLast:
    def test_nothing_actuated_but_event_emitted(self, fake, relays, clock, events):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events, smart_breaker_fail_safe="last")
        clock.advance(300)
        ctl.poll()
        st = ctl.status()
        assert st["failSafeApplied"] == "last"
        assert st["pendingCommand"] is None
        assert st["desired"] is None
        assert relays.log == []
        assert events[0]["details"]["mode"] == "last"


class TestFailSafeOn:
    def test_interlock_energised_no_breaker_command_queued(self, fake, relays, clock, events):
        fake.unreachable = True
        ctl = make(fake, relays, clock, events, smart_breaker_fail_safe="on")
        clock.advance(300)
        ctl.poll()
        st = ctl.status()
        assert st["failSafeApplied"] == "on"
        assert relays.log == [(3, True)]
        assert st["pendingCommand"] is None
        assert events[0]["details"]["interlockEnergised"] is True


class TestFailSafeModeIsHot:
    def test_mode_change_applies_without_rebuild(self, fake, relays, clock, events):
        settings = _settings()
        ctl = SmartBreakerController(lambda: settings, fake, relays, clock, events.append)
        assert ctl.fail_safe_mode is FailSafeMode.OFF
        settings.smart_breaker_fail_safe = "last"
        assert ctl.fail_safe_mode is FailSafeMode.LAST

    def test_invalid_mode_is_loud(self):
        with pytest.raises(ValueError):
            FailSafeMode.parse("maybe")


class TestPollTelemetry:
    def test_status_and_power_are_sampled(self, fake, relays, clock, events):
        fake.is_on = True
        fake.connected = True
        fake.current_a = 9.0
        fake.voltage_v = 240.0
        fake.energy_delivered_wh = 12.0
        ctl = make(fake, relays, clock, events)

        snap = ctl.poll()

        assert snap["breaker"] == {
            "isOn": True,
            "connected": True,
            "position": "close",
            "observedAt": 1000.0,
        }
        assert snap["power"]["currentA"] == 9.0
        assert snap["power"]["voltageV"] == 240.0
        assert snap["power"]["energyDeliveredWh"] == 12.0
        assert snap["circuitAmps"] == 20
        assert snap["circuitLabel"] == "AWG-1"

    def test_power_failure_is_telemetry_not_link_loss(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events)
        fake.get_status()  # warm nothing; just ensure fake works
        fake.calls.clear()
        # get_status succeeds, then get_power fails
        original_power = fake.get_power

        def flaky_power():
            fake.calls.append(("get_power", None))
            raise Unreachable("meter offline")

        fake.get_power = flaky_power  # type: ignore[method-assign]
        snap = ctl.poll()
        assert snap["linkOk"] is True
        assert snap["power"] is None
        fake.get_power = original_power  # type: ignore[method-assign]

    def test_external_change_is_noticed(self, fake, relays, clock, events, caplog):
        ctl = make(fake, relays, clock, events)
        ctl.request(True, source="cloud")
        fake.is_on = False  # someone flipped it in the Eaton app
        with caplog.at_level("WARNING", logger="wqm1.smart_breaker"):
            ctl.poll()
        assert "someone else" in caplog.text

    def test_circuit_amps_zero_reports_none(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events, smart_breaker_circuit_amps=0)
        assert ctl.status()["circuitAmps"] is None


class TestRelayOnly:
    def test_request_maps_to_relay(self, relays, clock, events):
        settings = _settings(smart_breaker_vendor="relay_only", smart_breaker_interlock_relay=2)
        ctl = SmartBreakerController(lambda: settings, None, relays, clock, events.append)
        r = ctl.request(True, source="cloud")
        assert r == {
            "ok": True,
            "state": True,
            "source": "cloud",
            "vendor": "relay_only",
            "interlockRelay": 2,
            "breaker": "n/a",
        }
        assert relays.log == [(2, True)]
        assert ctl.status()["desired"] is True

    def test_relay_only_without_channel_refuses(self, relays, clock, events):
        settings = _settings(smart_breaker_vendor="relay_only", smart_breaker_interlock_relay=0)
        ctl = SmartBreakerController(lambda: settings, None, relays, clock, events.append)
        assert ctl.request(True, source="cloud")["ok"] is False

    def test_poll_is_a_noop(self, relays, clock, events):
        settings = _settings(smart_breaker_vendor="relay_only", smart_breaker_interlock_relay=2)
        ctl = SmartBreakerController(lambda: settings, None, relays, clock, events.append)
        snap = ctl.poll()
        assert snap["vendor"] == "relay_only"
        assert relays.log == []


class TestDisabled:
    def test_vendor_none_refuses_requests(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events, smart_breaker_vendor="none")
        assert ctl.request(True, source="cloud") == {
            "ok": False,
            "error": "smart breaker integration disabled",
        }
        assert not ctl.enabled

    def test_missing_client_refuses(self, relays, clock, events):
        ctl = SmartBreakerController(lambda: _settings(), None, relays, clock, events.append)
        assert "not initialised" in ctl.request(True, source="cloud")["error"]


class TestEventSink:
    def test_sink_failure_never_breaks_control(self, fake, relays, clock):
        sink = MagicMock(side_effect=RuntimeError("queue gone"))
        settings = _settings(smart_breaker_unreachable_grace_s=0)
        ctl = SmartBreakerController(lambda: settings, fake, relays, clock, sink)
        fake.unreachable = True
        ctl.poll()  # must not raise
        assert ctl.status()["failSafeApplied"] == "off"

    def test_no_sink_is_fine(self, fake, relays, clock):
        settings = _settings(smart_breaker_unreachable_grace_s=0)
        ctl = SmartBreakerController(lambda: settings, fake, relays, clock, None)
        fake.unreachable = True
        ctl.poll()
        assert ctl.status()["failSafeApplied"] == "off"


class TestShutdown:
    def test_shutdown_sends_nothing(self, fake, relays, clock, events):
        ctl = make(fake, relays, clock, events)
        ctl.request(True, source="cloud")
        fake.calls.clear()
        ctl.shutdown()
        assert fake.calls == []
        assert fake.is_on is True  # a service restart must not open the breaker


class TestBuildSmartBreaker:
    def test_none_vendor_builds_nothing(self):
        assert build_smart_breaker(lambda: _settings(smart_breaker_vendor="none")) is None

    def test_relay_only_needs_channel_and_relays(self, relays):
        s = _settings(smart_breaker_vendor="relay_only", smart_breaker_interlock_relay=0)
        assert build_smart_breaker(lambda: s, relays) is None
        s = _settings(smart_breaker_vendor="relay_only", smart_breaker_interlock_relay=1)
        assert build_smart_breaker(lambda: s, None) is None
        ctl = build_smart_breaker(lambda: s, relays)
        assert ctl is not None and ctl.vendor == "relay_only"

    def test_ableedge_without_device_id_is_disabled(self, relays):
        s = _settings(smart_breaker_device_id="")
        assert build_smart_breaker(lambda: s, relays) is None

    def test_ableedge_without_credentials_is_disabled(self, relays, caplog):
        s = _settings()  # credentials empty
        with caplog.at_level("ERROR", logger="wqm1.smart_breaker"):
            assert build_smart_breaker(lambda: s, relays) is None
        assert "credentials incomplete" in caplog.text

    def test_cloud_proxy_not_yet_available(self, relays, caplog):
        s = _settings(
            smart_breaker_auth_mode="cloud_proxy",
            smart_breaker_client_id="a",
            smart_breaker_client_secret="b",
            smart_breaker_subscription_key="c",
        )
        with caplog.at_level("ERROR", logger="wqm1.smart_breaker"):
            assert build_smart_breaker(lambda: s, relays) is None
        assert "cloud_proxy" in caplog.text

    def test_ableedge_with_credentials_builds_a_controller(self, relays):
        s = _settings(
            smart_breaker_client_id="a",
            smart_breaker_client_secret="b",
            smart_breaker_subscription_key="c",
        )
        ctl = build_smart_breaker(lambda: s, relays)
        assert ctl is not None
        assert ctl.vendor == "ableedge"
        assert ctl._client.device_id == "dev-1"

    def test_bad_url_is_disabled(self, relays):
        s = _settings(
            smart_breaker_client_id="a",
            smart_breaker_client_secret="b",
            smart_breaker_subscription_key="c",
            smart_breaker_api_base="ftp://nope",
        )
        assert build_smart_breaker(lambda: s, relays) is None

    def test_unknown_vendor_is_disabled(self, relays):
        assert build_smart_breaker(lambda: _settings(smart_breaker_vendor="span"), relays) is None

    def test_no_secrets_are_logged(self, relays, caplog):
        s = _settings(
            smart_breaker_client_id="cid-visible-ok",
            smart_breaker_client_secret="SUPERSECRET",
            smart_breaker_subscription_key="SUBKEY123",
        )
        with caplog.at_level("DEBUG"):
            build_smart_breaker(lambda: s, relays)
        assert "SUPERSECRET" not in caplog.text
        assert "SUBKEY123" not in caplog.text
