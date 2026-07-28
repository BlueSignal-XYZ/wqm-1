"""
Tier-gating tests.

The requirement under test: gating must never disable fail-safe behaviour or
stop an already-running control loop when the device is offline or the flag
cannot be refreshed. A billing problem must not be able to kill livestock.
"""

import json

from control.channel import ChannelController
from control.relay import RelayController
from utils.config import CAUSE_STALENESS, CAUSE_WATCHDOG, CONTACT_NC, RELAY_PINS, ChannelConfig
from utils.tier import TierGate


def _gate(tmp_path, **state):
    path = tmp_path / "entitlement.json"
    if state:
        path.write_text(json.dumps(state))
    return TierGate(str(path))


def _aeration_controller():
    cfg = ChannelConfig(
        channel=3,
        role="aeration",
        contact=CONTACT_NC,
        fail_safe_state="run",
        commissioned=True,
        stale_cycles=1,
    )
    return ChannelController(RelayController(), {3: cfg})


def pin_of(channel):
    return RELAY_PINS[channel - 1]


class TestDefaults:
    def test_unprovisioned_device_has_no_control(self, tmp_path):
        gate = _gate(tmp_path)
        assert gate.granted is False
        assert gate.loop_may_run is False
        assert gate.allows_commissioning() is False

    def test_grant_persists_across_restart(self, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(True)
        assert TierGate(str(tmp_path / "entitlement.json")).granted is True

    def test_corrupt_cache_does_not_grant_control(self, tmp_path):
        path = tmp_path / "entitlement.json"
        path.write_text("{ not json")
        gate = TierGate(str(path))
        assert gate.granted is False


class TestOffline:
    def test_failed_refresh_is_not_a_revocation(self, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(True)
        gate.refresh(None)  # cloud unreachable
        assert gate.granted is True
        assert gate.allows_new_setpoints() is True

    def test_offline_forever_keeps_last_known_good(self, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(True)
        for _ in range(1000):
            gate.refresh(None)
        assert gate.loop_may_run is True


class TestLapse:
    def test_revocation_stops_new_setpoints(self, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(True)
        gate.refresh(False)
        assert gate.allows_new_setpoints() is False
        assert gate.allows_commissioning() is False
        assert gate.allows_manual_control() is False

    def test_revocation_does_not_stop_a_running_loop(self, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(True)
        gate.refresh(False)
        assert gate.loop_may_run is True, "a lapsed subscription must not stop control"


class TestStructuralIsolation:
    """
    ChannelController must have no way to consult entitlement.

    This is the guarantee that makes the requirement true by construction
    rather than by reviewer vigilance.
    """

    def test_channel_module_never_imports_tier(self):
        """Parse the AST — a docstring mentioning entitlement is fine, an import is not."""
        import ast

        import control.channel as channel_mod

        with open(channel_mod.__file__) as f:
            tree = ast.parse(f.read())

        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported += [f"{node.module}.{a.name}" for a in node.names]

        offenders = [m for m in imported if "tier" in m.lower()]
        assert offenders == [], f"channel.py must not reach entitlement: {offenders}"

    def test_controller_has_no_gate_attribute(self):
        cc = _aeration_controller()
        assert not any("tier" in a.lower() or "gate" in a.lower() for a in vars(cc))

    def test_failsafe_works_with_no_entitlement_at_all(self, gpio_pins, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(False)
        cc = _aeration_controller()
        cc.request(3, run=False, cause="setpoint")
        assert gpio_pins[pin_of(3)] == 1

        # No entitlement anywhere in scope — fail-safe still reverts.
        cc.revert_to_fail_safe(3, cause=CAUSE_WATCHDOG)
        assert gpio_pins[pin_of(3)] == 0
        assert cc.is_running(3) is True

    def test_staleness_reversion_works_after_revocation(self, gpio_pins, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(True)
        cc = _aeration_controller()
        cc.request(3, run=False, cause="setpoint")

        gate.refresh(False)  # subscription lapses mid-run
        cc.note_reading(3, valid=False)

        assert gpio_pins[pin_of(3)] == 0
        assert cc.is_running(3) is True
        assert cc.last_cause(3) == CAUSE_STALENESS

    def test_dwell_enforcement_unaffected_by_entitlement(self, tmp_path):
        gate = _gate(tmp_path)
        gate.refresh(False)
        cfg = ChannelConfig(
            channel=1,
            role="dosing",
            fail_safe_state="stop",
            commissioned=True,
            deadband=0.5,
        )
        cc = ChannelController(RelayController(), {1: cfg})
        assert cc.passes_deadband(1, value=8.1, threshold=8.0, operator=">") is False


class TestCloudIsNeverInTheActuationPath:
    """
    Requirement: no relay transition may block on, or wait for, a LoRa round
    trip or a cloud response.

    Proven structurally rather than by timing: the actuation module imports no
    radio, cloud, or network machinery at all, so there is no call it could
    block on. Cloud setpoints land in local config and are picked up by the
    next evaluation.
    """

    def test_channel_module_imports_nothing_networked(self):
        import ast

        import control.channel as channel_mod

        with open(channel_mod.__file__) as f:
            tree = ast.parse(f.read())

        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")

        forbidden = ("radio", "cloud", "socket", "requests", "urllib", "http")
        offenders = [m for m in imported if any(f in m.lower() for f in forbidden)]
        assert offenders == [], f"actuation must not reach the network: {offenders}"

    def test_actuation_works_with_no_cloud_or_radio_object(self, gpio_pins):
        """The controller is constructed with neither, and still actuates."""
        cfg = ChannelConfig(
            channel=1, role="dosing", fail_safe_state="stop", commissioned=True
        )
        cc = ChannelController(RelayController(), {1: cfg})
        assert cc.request(1, run=True, cause="setpoint") is True
        assert gpio_pins[RELAY_PINS[0]] == 1
