"""Where a test request comes from.

Flask's test client sends from 127.0.0.1. The Service Window treats that as
the unit itself: the bench SSH tunnel (Decision 2, docs/install-build-plan.md).
A test about a phone has to say where the phone is.
"""

from flask.testing import FlaskClient

HOME_NETWORK = "10.0.0.5"
HOTSPOT = "192.168.4.23"


def from_addr(client, addr):
    """This client's requests come from addr."""
    client.environ_base["REMOTE_ADDR"] = addr
    return client


def clients_from(app, addr):
    """Every ``app.test_client()`` of this app sends from addr."""

    class _Client(FlaskClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.environ_base["REMOTE_ADDR"] = addr

    app.test_client_class = _Client
    return app
