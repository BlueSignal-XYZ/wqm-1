"""
Commissioning is where fitment gets declared.

The firmware can refuse to sample an undeclared probe (test_probe_fitment.py),
but something has to do the declaring, and the only person who knows what is
physically screwed into the enclosure is the installer standing in front of it.
Before this the wizard never asked — it assumed the analog four were present,
showed "pH probe is reading normally" for a bare board, and passed its own
go/no-go check.
"""

from pathlib import Path

import pytest
import yaml

from tests.test_service_window_setup import make_app


@pytest.fixture
def client(tmp_path, mock_hardware):
    app = make_app(tmp_path, mock_hardware)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["pin_verified"] = True
        yield c, app


def _config(app):
    return yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text()) or {}


class TestTheWizardAsks:
    def test_the_sensors_step_offers_a_fitment_checklist(self, client):
        c, _ = client
        body = c.get("/setup/sensors").data
        assert b"Which probes are fitted" in body
        for key in (b"ph_enabled", b"tds_enabled", b"turbidity_enabled", b"temperature_enabled"):
            assert key in body

    def test_it_explains_why_an_undeclared_probe_matters(self, client):
        c, _ = client
        body = c.get("/setup/sensors").data
        assert b"open input" in body
        assert b"not sampled" in body


class TestDeclarationIsWritten:
    def test_ticked_probes_are_recorded_as_fitted(self, client):
        c, app = client
        c.post("/setup/sensors", data={"ph_enabled": "on", "temperature_enabled": "on"})
        cfg = _config(app)
        assert cfg["ph_enabled"] is True
        assert cfg["temperature_enabled"] is True

    def test_unticked_probes_are_recorded_as_NOT_fitted(self, client):
        """The critical half. Writing only the ticked boxes would leave a
        removed probe declared forever, and the whole point is being able to
        say 'this channel has nothing on it'."""
        c, app = client
        c.post("/setup/sensors", data={"ph_enabled": "on"})
        cfg = _config(app)
        assert cfg["tds_enabled"] is False
        assert cfg["turbidity_enabled"] is False
        assert cfg["temperature_enabled"] is False

    def test_declaring_nothing_is_allowed_and_says_so(self, client):
        c, app = client
        resp = c.post("/setup/sensors", data={}, follow_redirects=True)
        assert all(_config(app)[k] is False for k in ("ph_enabled", "tds_enabled"))
        assert b"no water data" in resp.data

    def test_a_declaration_can_be_revised(self, client):
        c, app = client
        c.post("/setup/sensors", data={"ph_enabled": "on"})
        assert _config(app)["ph_enabled"] is True
        c.post("/setup/sensors", data={"tds_enabled": "on"})
        cfg = _config(app)
        assert cfg["ph_enabled"] is False, "un-ticking must be able to remove a probe"
        assert cfg["tds_enabled"] is True


class TestTheCheckboxesReflectRealState:
    def test_a_fitted_probe_renders_checked(self, client):
        c, _ = client
        c.post("/setup/sensors", data={"ph_enabled": "on"})
        assert b'name="ph_enabled" checked' in c.get("/setup/sensors").data.replace(b" >", b">")

    def test_an_unfitted_probe_renders_unchecked(self, client):
        c, _ = client
        c.post("/setup/sensors", data={"ph_enabled": "on"})
        body = c.get("/setup/sensors").data
        idx = body.index(b'name="tds_enabled"')
        assert b"checked" not in body[idx : idx + 40]

    def test_a_config_predating_these_keys_shows_the_analog_four_fitted(self, client):
        """Absent means fitted, matching health.py and the firmware defaults —
        an upgrade must not look like every probe fell off."""
        c, _ = client
        body = c.get("/setup/sensors").data.replace(b" >", b">")
        assert b'name="ph_enabled" checked' in body
        # ORP is the exception: no ORP hardware on PCBA Fin_3.
        idx = body.index(b'name="orp_enabled"')
        assert b"checked" not in body[idx : idx + 40]
