"""Tests for the cloud provisioning routes in service_window/routes/provision.py."""

from pathlib import Path

import pytest
import yaml

from service_window.app import create_app


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test.db")
    config_path = str(tmp_path / "config.yaml")
    cal_path = str(tmp_path / "cal.yaml")

    from storage.database import WQM1Database

    db = WQM1Database(path=db_path)
    db.close()

    Path(config_path).write_text(yaml.safe_dump({"app_key": "A" * 32}))

    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": db_path,
            "CONFIG_PATH": config_path,
            "CAL_PATH": cal_path,
            "CMD_SOCK": str(tmp_path / "cmd.sock"),
            "PIN": "1234",
            "SECRET_KEY": "test-secret",
        }
    )
    return app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["pin_verified"] = True
        yield c


class TestCloudProvisioningRoute:
    def test_set_cloud_enables_with_key(self, client, app):
        resp = client.post(
            "/provision/cloud",
            data={
                "api_key": "a1b2c3d4e5f6g7h8",
                "cloud_enabled": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config["cloud_enabled"] is True
        assert config["api_key"] == "a1b2c3d4e5f6g7h8"

    def test_set_cloud_rejects_invalid_key(self, client, app):
        resp = client.post(
            "/provision/cloud",
            data={
                "api_key": "bad!key@#$",
                "cloud_enabled": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config.get("api_key") != "bad!key@#$"

    def test_set_cloud_rejects_non_https_ingest_url(self, client, app):
        resp = client.post(
            "/provision/cloud",
            data={
                "api_key": "a1b2c3d4e5f6g7h8",
                "cloud_ingest_url": "http://insecure.test/ingest",
                "cloud_enabled": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config.get("cloud_ingest_url") != "http://insecure.test/ingest"

    def test_set_cloud_rejects_non_https_command_url(self, client, app):
        resp = client.post(
            "/provision/cloud",
            data={
                "api_key": "a1b2c3d4e5f6g7h8",
                "cloud_command_url": "http://insecure.test/commands",
                "cloud_enabled": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config.get("cloud_command_url") != "http://insecure.test/commands"

    def test_set_cloud_requires_key_to_enable(self, client, app):
        Path(app.config["CONFIG_PATH"]).write_text(yaml.safe_dump({"app_key": "A" * 32}))
        resp = client.post(
            "/provision/cloud",
            data={"cloud_enabled": "on"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config.get("cloud_enabled") is not True

    def test_set_cloud_disables_without_key(self, client, app):
        resp = client.post(
            "/provision/cloud",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config["cloud_enabled"] is False

    def test_set_cloud_with_custom_urls(self, client, app):
        resp = client.post(
            "/provision/cloud",
            data={
                "api_key": "a1b2c3d4e5f6g7h8",
                "cloud_ingest_url": "https://custom.test/ingest",
                "cloud_command_url": "https://custom.test/commands",
                "cloud_enabled": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config["cloud_ingest_url"] == "https://custom.test/ingest"
        assert config["cloud_command_url"] == "https://custom.test/commands"

    def test_set_cloud_enables_with_existing_key(self, client, app):
        Path(app.config["CONFIG_PATH"]).write_text(
            yaml.safe_dump({"app_key": "A" * 32, "api_key": "existingkey12345a"})
        )
        resp = client.post(
            "/provision/cloud",
            data={"cloud_enabled": "on"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        config = yaml.safe_load(Path(app.config["CONFIG_PATH"]).read_text())
        assert config["cloud_enabled"] is True
