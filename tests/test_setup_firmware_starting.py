"""Setup routes while bluesignal-wqm is absent or not READY.

The Service Window can bind :8080 before the firmware process sends READY=1.
Sensor/probe pages, identity, the status file, readings preview, and the
go/no-go self-check must 200 with starting copy and poll, never 500, and
never a false 'probe missing' tick. A status file that already says the
ADC is faulted (#139) still requires the confirm tick.
"""

from pathlib import Path

import yaml

from tests.test_service_window_setup import make_app

STARTING = b"still starting"
CHECKING = b"Checking sensors"
FALSE_MISSING = b"Finish without probe"
NO_DATA = b"no data for"


def _client(tmp_path, mock_hardware, monkeypatch, status_text="firmware: 2.4.0\n"):
    status = tmp_path / "wqm1-status.txt"
    status.write_text(status_text, encoding="utf-8")
    monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
    app = make_app(tmp_path, mock_hardware)
    assert not Path(app.config["CMD_SOCK"]).exists()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["pin_verified"] = True
    return client, app


class TestSetupWhenFirmwareIsAbsent:
    def test_welcome_identity_cloud_sensors_done_are_200(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch)
        for path in (
            "/setup/",
            "/setup/pin",
            "/setup/identity",
            "/setup/cloud",
            "/setup/sensors",
            "/setup/network",
            "/setup/done",
        ):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert STARTING in resp.data
            assert b"firmware-starting" in resp.data
            assert b"5000" in resp.data

    def test_identity_shows_a_device_id_not_an_error(self, tmp_path, mock_hardware, monkeypatch):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch)
        resp = client.get("/setup/identity")
        assert resp.status_code == 200
        page = resp.data.decode()
        assert "Device ID" in page
        assert "Internal Server Error" not in page
        assert "BS-WQM1-" in page or "WQM-" in page

    def test_sensors_preview_is_starting_not_missing_probe(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch)
        resp = client.get("/setup/sensors")
        assert resp.status_code == 200
        page = resp.data.lower()
        assert STARTING.lower() in page
        assert CHECKING.lower() in page
        assert NO_DATA.lower() not in page
        assert FALSE_MISSING.lower() not in page
        assert b">checking<" in resp.data
        assert b"not detected" not in resp.data

    def test_done_self_check_is_starting_not_fault(self, tmp_path, mock_hardware, monkeypatch):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch)
        resp = client.get("/setup/done")
        assert resp.status_code == 200
        page = resp.data.lower()
        assert STARTING.lower() in page
        assert CHECKING.lower() in page
        assert NO_DATA.lower() not in page
        assert FALSE_MISSING.lower() not in page
        assert b"confirm_adc" not in resp.data

    def test_empty_database_still_renders(self, tmp_path, mock_hardware, monkeypatch):
        status = tmp_path / "wqm1-status.txt"
        status.write_text("firmware: 2.4.0\n", encoding="utf-8")
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        app = make_app(tmp_path, mock_hardware)
        app.config["DB_PATH"] = str(tmp_path / "missing" / "wqm1.db")
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["pin_verified"] = True
            sensors = client.get("/setup/sensors")
            done = client.get("/setup/done")
        assert sensors.status_code == 200
        assert done.status_code == 200
        assert STARTING in sensors.data
        assert STARTING in done.data
        assert FALSE_MISSING not in sensors.data
        assert FALSE_MISSING not in done.data

    def test_finish_does_not_demand_a_missing_probe_tick(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, app = _client(tmp_path, mock_hardware, monkeypatch)
        client.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
        resp = client.post("/setup/done", data={}, follow_redirects=False)
        assert resp.status_code == 302
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["service_window"]["setup_completed"] is True


class TestMissingProbeFromStatusFile:
    """#139: a published adc: faulted is a real miss, even if cmd.sock is down."""

    def test_faulted_status_still_requires_the_confirm_tick(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, app = _client(
            tmp_path,
            mock_hardware,
            monkeypatch,
            status_text="adc: faulted\nonewire: ok\n",
        )
        client.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
        page = client.get("/setup/done").data.decode()
        assert "Finish without probe ADC" in page
        assert "not detected" in page
        client.post("/setup/done", data={}, follow_redirects=True)
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert not (saved.get("service_window") or {}).get("setup_completed")
        client.post("/setup/done", data={"confirm_adc": "yes"}, follow_redirects=False)
        saved = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert saved["service_window"]["setup_completed"] is True

    def test_sensors_do_not_invent_a_miss_from_an_empty_status(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, _app = _client(
            tmp_path, mock_hardware, monkeypatch, status_text="adc: unknown\nonewire:\n"
        )
        page = client.get("/setup/sensors").data.decode()
        assert "checking" in page
        assert "Finish without probe" not in page
