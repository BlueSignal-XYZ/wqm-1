"""Regression tests for config-write failure handling in the service window.

Field symptom: every save in the service window returned a bare Flask 500. From
the browser that is indistinguishable from "the device rejected my API key", so
the operator had no way to learn the real cause — /etc/bluesignal was root-owned
while the service runs as an unprivileged user, so `_atomic_yaml_write` raised
PermissionError.

These tests pin both halves of the fix:
  1. `update_config` raises ConfigWriteError (not a bare OSError) and the
     message names the directory and the chown that fixes it.
  2. The Flask app turns that into a flash + redirect, never a 500.
"""

import os
import stat

import pytest
import yaml

from service_window.app import create_app
from service_window.config_editor import ConfigWriteError, update_config


@pytest.fixture
def readonly_config(tmp_path):
    """A config file inside a directory the process cannot write to.

    Reproduces the root-owned /etc/bluesignal case without needing root: the
    atomic write creates a .tmp sibling, which a read-only parent forbids.
    """
    d = tmp_path / "etc"
    d.mkdir()
    cfg = d / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            # setup_completed: the v2 setup funnel otherwise redirects every
            # request into /setup, and this file tests the config-write error
            # path, not the wizard.
            {"app_key": "A" * 32, "service_window": {"setup_completed": True}}
        )
    )
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x — no write
    yield cfg
    d.chmod(stat.S_IRWXU)  # restore so tmp_path cleanup can unlink


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
class TestConfigWriteError:
    def test_raises_config_write_error_not_oserror(self, readonly_config):
        with pytest.raises(ConfigWriteError):
            update_config(str(readonly_config), {"cloud_enabled": True})

    def test_message_names_the_directory_and_the_fix(self, readonly_config):
        with pytest.raises(ConfigWriteError) as exc:
            update_config(str(readonly_config), {"cloud_enabled": True})

        msg = str(exc.value)
        assert str(readonly_config.parent) in msg, "operator needs the path to chown"
        assert "chown" in msg, "message must carry the remedy, not just the symptom"

    def test_original_config_is_left_intact(self, readonly_config):
        with pytest.raises(ConfigWriteError):
            update_config(str(readonly_config), {"cloud_enabled": True})

        assert yaml.safe_load(readonly_config.read_text()) == {
            "app_key": "A" * 32,
            "service_window": {"setup_completed": True},
        }


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
class TestServiceWindowSurfacesTheReason:
    @pytest.fixture
    def client(self, tmp_path, readonly_config):
        from storage.database import WQM1Database

        # The error handler redirects to the referrer, or "/" when there is
        # none — so the index has to be able to render, which means a schema.
        db_path = str(tmp_path / "test.db")
        WQM1Database(path=db_path).close()

        app = create_app(
            {
                "TESTING": True,
                "DB_PATH": db_path,
                "CONFIG_PATH": str(readonly_config),
                "CAL_PATH": str(tmp_path / "cal.yaml"),
                "CMD_SOCK": str(tmp_path / "cmd.sock"),
                "PIN": "1234",
                "SECRET_KEY": "test-secret",
            }
        )
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["pin_verified"] = True
            yield c

    def test_save_redirects_instead_of_500(self, client):
        resp = client.post(
            "/provision/cloud",
            data={"api_key": "a1b2c3d4e5f6g7h8", "cloud_enabled": "on"},
            follow_redirects=False,
        )
        assert resp.status_code != 500, "a permission problem must not read as a server crash"
        assert resp.status_code == 302

    def test_operator_is_told_the_real_reason(self, client):
        resp = client.post(
            "/provision/cloud",
            data={"api_key": "a1b2c3d4e5f6g7h8", "cloud_enabled": "on"},
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        assert "chown" in body, "the page must show the fix, not a generic failure"
