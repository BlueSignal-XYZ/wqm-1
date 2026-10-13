"""
The MQTT publisher's lifecycle: connect, announce, push a state, stop.

:class:`HomeAssistantPublisher` owns one paho-mqtt client on its own network
thread. The library is imported lazily inside :class:`MqttAdapter`, so a unit
without ``paho-mqtt`` installed boots normally and the feature reports
``library_missing``; tests inject a fake adapter and never touch a socket.

What the unit never does over MQTT: subscribe. There is no command topic, no
``set`` topic, and the adapter has no method that could receive one. Readings
out, nothing in.

What never leaves this module: the broker password. :meth:`status` (for the
Service Window) reports the host, the port and whether a username and
password are set; :meth:`heartbeat` — what the cloud receives — is
``{"enabled", "connected"}`` and nothing more.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from integrations.home_assistant.discovery import (
    BANNED_NAME_WORDS,
    DEFAULT_DISCOVERY_PREFIX,
    ENTITIES,
    HOLD_ENTITY,
    device_name,
    discovery_messages,
    state_payload,
    topics_for,
)

logger = logging.getLogger("wqm1.home_assistant")

DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883


class MqttUnavailable(RuntimeError):
    """paho-mqtt is not installed on this unit."""


class MqttAdapter:
    """Everything the publisher asks of paho-mqtt, behind one small surface
    so a fake can stand in for it. Constructed only when the feature is on.

    ``start()`` connects asynchronously and runs paho's network loop on its
    own thread; ``publish()`` is thread-safe; ``stop()`` sends the retained
    ``offline`` availability (the same message the broker would send as the
    will) and disconnects.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        tls: bool,
        client_id: str,
        will_topic: str,
    ) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:  # pragma: no cover — exercised by the fake path
            raise MqttUnavailable("paho-mqtt is not installed") from e

        self._mqtt = mqtt
        self._host = host
        self._port = int(port)
        self._will_topic = will_topic
        self._connected = False
        self._last_error: str | None = None
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        if username:
            client.username_pw_set(username, password or None)
        if tls:
            client.tls_set()
        client.will_set(will_topic, payload="offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self._client = client

    # paho callbacks run on the network thread.
    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, rc: Any, *_: Any) -> None:
        ok = getattr(rc, "is_failure", None)
        failed = bool(ok) if ok is not None else int(rc) != 0
        self._connected = not failed
        self._last_error = None if not failed else f"connect refused: {rc}"
        if failed:
            logger.warning("Home Assistant broker refused the connection: %s", rc)

    def _on_disconnect(self, _client: Any, _userdata: Any, *args: Any) -> None:
        # VERSION2 signature: (client, userdata, disconnect_flags, reason_code,
        # properties). The reason code is the one argument with is_failure.
        self._connected = False
        rc = next((a for a in args if hasattr(a, "is_failure")), None)
        if rc is not None and rc.is_failure:
            self._last_error = f"disconnected: {rc}"

    def start(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            if self._connected:
                info = self._client.publish(self._will_topic, "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=3)
        except Exception:  # noqa: BLE001 — best effort on the way out
            pass
        finally:
            self._client.loop_stop()
            with contextlib.suppress(Exception):
                self._client.disconnect()
            self._connected = False

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=1, retain=retain)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error


AdapterFactory = Callable[..., Any]


class HomeAssistantPublisher:
    """One per unit. Mirrors :class:`integrations.apple_home.AppleHomeBridge`:
    ``configure(settings)`` every cycle (cheap, idempotent), one ``update``
    per sample, a ``status()`` for the Service Window and a ``heartbeat()``
    for the cloud.
    """

    def __init__(
        self,
        device_id: str,
        firmware_version: str,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self._device_id = str(device_id)
        self._fw = str(firmware_version)
        self._factory: AdapterFactory = adapter_factory or MqttAdapter
        self._lock = threading.RLock()
        self._adapter: Any = None
        self._enabled = False
        self._conn: dict[str, Any] = {}
        self._prefix = DEFAULT_DISCOVERY_PREFIX
        self._error: str | None = None
        self._last: dict[str, Any] | None = None
        self._announced = False
        self._pushes = 0

    # -- configuration -----------------------------------------------------

    @property
    def name(self) -> str:
        n = device_name(self._device_id)
        low = n.lower()
        if any(w in low for w in BANNED_NAME_WORDS):  # pragma: no cover — guarded by test
            raise ValueError("device name carries a banned word")
        return n

    @staticmethod
    def _connection_from(settings: Any) -> dict[str, Any]:
        tls = bool(getattr(settings, "mqtt_tls", False))
        try:
            port = int(getattr(settings, "mqtt_port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            port = DEFAULT_TLS_PORT if tls else DEFAULT_PORT
        return {
            "host": str(getattr(settings, "mqtt_host", "") or "").strip(),
            "port": port,
            "username": str(getattr(settings, "mqtt_username", "") or ""),
            "password": str(getattr(settings, "mqtt_password", "") or ""),
            "tls": tls,
        }

    def configure(self, settings: Any) -> None:
        """Snapshot the ``mqtt_*`` settings and start, restart or stop the
        client to match. Safe to call every cycle."""
        enabled = bool(getattr(settings, "mqtt_enabled", False))
        conn = self._connection_from(settings)
        prefix = str(getattr(settings, "mqtt_discovery_prefix", "") or "").strip("/")
        prefix = prefix or DEFAULT_DISCOVERY_PREFIX
        with self._lock:
            changed = self._adapter is not None and (conn != self._conn or prefix != self._prefix)
            self._conn = conn
            self._prefix = prefix
            if self._error == "no_host" and conn["host"]:
                self._error = None  # the host arrived; retry below
            if enabled and not conn["host"]:
                if self._adapter is not None:
                    self._stop_locked()
                self._error = "no_host"
            elif enabled and (self._adapter is None or changed) and self._error is None:
                if changed:
                    self._stop_locked()
                self._start_locked()
            elif not enabled and self._adapter is not None:
                self._stop_locked()
            if not enabled:
                # A disable clears a stuck error so a later enable retries.
                self._error = None
            self._enabled = enabled

    def _start_locked(self) -> None:
        t = topics_for(self._device_id, self._prefix)
        try:
            self._adapter = self._factory(
                host=self._conn["host"],
                port=self._conn["port"],
                username=self._conn["username"],
                password=self._conn["password"],
                tls=self._conn["tls"],
                client_id=t["node"],
                will_topic=t["availability"],
            )
            self._announced = False
            self._adapter.start()
            logger.info(
                "Home Assistant publisher '%s' connecting to %s:%d",
                self.name,
                self._conn["host"],
                self._conn["port"],
            )
        except MqttUnavailable:
            self._adapter = None
            self._error = "library_missing"
            logger.warning("MQTT enabled but paho-mqtt is not installed — not publishing")
        except Exception as e:  # noqa: BLE001 — the publisher must never take the unit down
            self._adapter = None
            self._error = f"start_failed: {e}"
            logger.error("Home Assistant publisher failed to start: %s", e)

    def _stop_locked(self) -> None:
        adapter, self._adapter = self._adapter, None
        self._announced = False
        if adapter is None:
            return
        try:
            adapter.stop()
            logger.info("Home Assistant publisher stopped")
        except Exception as e:  # noqa: BLE001
            logger.warning("Home Assistant publisher stop failed: %s", e)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    # -- per-sample --------------------------------------------------------

    def update(
        self,
        reading: dict[str, Any] | None,
        hold: dict[str, Any] | None = None,
        suspended: set[str] | None = None,
    ) -> dict[str, Any]:
        """Push one cycle's state. Pure mapping first, MQTT writes second,
        and an MQTT failure is logged, never raised into the sampling
        worker. Discovery is announced once per connection, on the first
        cycle the adapter reports connected."""
        state = state_payload(reading, hold, suspended)
        with self._lock:
            self._last = state
            self._push(state)
        return state

    def _push(self, state: dict[str, Any]) -> None:
        a = self._adapter
        if a is None or not a.connected:
            return
        t = topics_for(self._device_id, self._prefix)
        try:
            if not self._announced:
                for topic, payload in discovery_messages(self._device_id, self._fw, self._prefix):
                    a.publish(topic, payload, retain=True)
                a.publish(t["availability"], "online", retain=True)
                self._announced = True
            a.publish(t["state"], json.dumps(state, separators=(",", ":")), retain=False)
            self._pushes += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Home Assistant publish failed: %s", e)

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The ``home_assistant_status`` command's answer, for the Service
        Window. Names the broker and whether credentials are set — never the
        password."""
        with self._lock:
            a = self._adapter
            t = topics_for(self._device_id, self._prefix)
            err = self._error
            if err is None and a is not None:
                err = getattr(a, "last_error", None)
            return {
                "enabled": self._enabled,
                "running": a is not None,
                "connected": bool(a.connected) if a is not None else False,
                "announced": self._announced,
                "name": self.name,
                "host": self._conn.get("host", ""),
                "port": self._conn.get("port", DEFAULT_PORT),
                "tls": bool(self._conn.get("tls", False)),
                "usernameSet": bool(self._conn.get("username")),
                "passwordSet": bool(self._conn.get("password")),
                "discoveryPrefix": self._prefix,
                "stateTopic": t["state"],
                "availabilityTopic": t["availability"],
                "error": err,
                "pushes": self._pushes,
                "entities": [
                    {"key": e["key"], "name": e["name"], "unit": e["unit"]} for e in ENTITIES
                ]
                + [{"key": HOLD_ENTITY["key"], "name": HOLD_ENTITY["name"], "unit": ""}],
            }

    def heartbeat(self) -> dict[str, Any]:
        """``homeAssistant`` on the heartbeat: enabled and connected, nothing
        else. No host, no topic, no credential."""
        with self._lock:
            a = self._adapter
            return {
                "enabled": self._enabled,
                "connected": bool(a.connected) if a is not None else False,
            }
