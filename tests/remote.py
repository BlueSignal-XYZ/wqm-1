"""Where a test request comes from.

Flask's test client sends from 127.0.0.1. The Service Window treats that as
the unit itself: the bench SSH tunnel (Decision 2, docs/install-build-plan.md).
A test about a phone has to say where the phone is.
"""

from flask.testing import FlaskClient

HOME_NETWORK = "10.0.0.5"
HOTSPOT = "192.168.4.23"
# A phone on the setup network, through the port-80 relay: the relay connects
# from 127.0.0.1 and marks the request. Since install plan §3 this is the only
# way a phone reaches the Service Window; directly on 8080 it gets 404. §4
# removes the relay.
HOTSPOT_RELAYED = "relayed-from-hotspot"


def _apply(environ: dict, addr: str) -> None:
    if addr == HOTSPOT_RELAYED:
        from utils.captive_portal import RELAY_VALUE

        environ["REMOTE_ADDR"] = "127.0.0.1"
        environ["HTTP_X_WQM1_RELAY"] = RELAY_VALUE
        return
    environ["REMOTE_ADDR"] = addr
    environ.pop("HTTP_X_WQM1_RELAY", None)


def from_addr(client, addr):
    """This client's requests come from addr."""
    _apply(client.environ_base, addr)
    return client


def clients_from(app, addr):
    """Every ``app.test_client()`` of this app sends from addr."""

    class _Client(FlaskClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            _apply(self.environ_base, addr)

    app.test_client_class = _Client
    return app
