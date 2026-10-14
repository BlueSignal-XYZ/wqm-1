"""
The HomeKit accessory's lifecycle: start, push a snapshot, stop.

:class:`AppleHomeBridge` owns one HAP-python ``AccessoryDriver`` on its own
thread. The library is imported lazily inside :class:`HapAdapter`, so a unit
without ``HAP-python`` installed boots normally and the feature reports
``library_missing``; tests inject a fake adapter and never touch the network.

Threading: ``update()`` is called from the sampling worker once per cycle and
only sets characteristic values (HAP-python's ``set_value`` is thread-safe —
it schedules the HAP event on the driver's loop). ``configure()`` is called
from the same worker and from the config-reload command, so start/stop are
serialised by a lock.

What never leaves this module: the setup code and the setup URI are returned
by :meth:`status` for the Service Window (a logged-in page on the unit's own
LAN) and by nothing else. :meth:`heartbeat` — what the cloud receives — is
``{"enabled", "paired"}`` and nothing more.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from integrations.apple_home.accessory import (
    BANNED_NAME_WORDS,
    CUSTOM_CHARACTERISTICS,
    SERVICES,
    HomeSnapshot,
    accessory_name,
    snapshot_from_reading,
)

logger = logging.getLogger("wqm1.apple_home")

DEFAULT_PORT = 51826
# HAP-python persists the pairing keys and the pincode here; losing the file
# means re-pairing, so it lives beside the database, not in /tmp.
STATE_FILENAME = "apple-home.state"

MANUFACTURER = "BlueSignal XYZ, Inc."
MODEL = "WQM-1"


class HapUnavailable(RuntimeError):
    """HAP-python is not installed on this unit."""


class HapAdapter:
    """Everything the bridge asks of HAP-python, behind one small surface so
    a fake can stand in for it. Constructed only when the feature is enabled.

    ``start()`` runs the driver's asyncio loop on a daemon thread; ``stop()``
    asks the loop to stop and joins the thread.
    """

    def __init__(
        self,
        *,
        name: str,
        device_id: str,
        firmware_version: str,
        persist_file: Path,
        port: int,
    ) -> None:
        try:
            from pyhap.accessory import Accessory
            from pyhap.accessory_driver import AccessoryDriver
            from pyhap.characteristic import Characteristic
            from pyhap.const import CATEGORY_SENSOR
        except ImportError as e:  # pragma: no cover — exercised by the fake path
            raise HapUnavailable("HAP-python is not installed") from e

        persist_file.parent.mkdir(parents=True, exist_ok=True)
        self._driver = AccessoryDriver(port=port, persist_file=str(persist_file))
        acc = Accessory(self._driver, name)
        acc.category = CATEGORY_SENSOR
        acc.set_info_service(
            firmware_revision=firmware_version,
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=device_id,
        )
        self._chars: dict[str, Any] = {}
        for svc in SERVICES:
            if svc["type"] == "TemperatureSensor":
                s = acc.add_preload_service(
                    "TemperatureSensor", chars=["StatusActive", "StatusFault"]
                )
                s.configure_char("Name", value=svc["name"])
                self._chars["temperature"] = s.get_characteristic("CurrentTemperature")
                self._chars["temperature_active"] = s.get_characteristic("StatusActive")
                self._chars["temperature_fault"] = s.get_characteristic("StatusFault")
                # The channels HomeKit has no type for ride on this service
                # as custom characteristics (read + notify, never write).
                for key, spec in CUSTOM_CHARACTERISTICS.items():
                    props: dict[str, Any] = {
                        "Format": "float",
                        "Permissions": ["pr", "ev"],
                        "minValue": spec["min"],
                        "maxValue": spec["max"],
                        "minStep": spec["step"],
                    }
                    if spec["unit"]:
                        props["unit"] = spec["unit"]
                    ch = Characteristic(spec["display_name"], spec["type_id"], props)
                    s.add_characteristic(ch)
                    self._chars[f"custom:{key}"] = ch
            elif svc["type"] == "ContactSensor":
                s = acc.add_preload_service("ContactSensor", chars=["StatusActive", "StatusFault"])
                s.configure_char("Name", value=svc["name"])
                self._chars["contact"] = s.get_characteristic("ContactSensorState")
                self._chars["contact_active"] = s.get_characteristic("StatusActive")
                self._chars["contact_fault"] = s.get_characteristic("StatusFault")
        self._driver.add_accessory(acc)
        self._accessory = acc
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._driver.start, name="apple-home-hap", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        try:
            self._driver.stop()
        finally:
            if self._thread is not None:
                self._thread.join(timeout=10)
                self._thread = None

    def set(self, key: str, value: Any) -> None:
        ch = self._chars.get(key)
        if ch is not None:
            ch.set_value(value)

    @property
    def paired(self) -> bool:
        return bool(self._driver.state.paired)

    @property
    def setup_code(self) -> str:
        return self._driver.state.pincode.decode()

    @property
    def setup_uri(self) -> str:
        return self._accessory.xhm_uri()


AdapterFactory = Callable[..., Any]


class AppleHomeBridge:
    """One per unit. Mirrors :class:`control.irrigation_hold.IrrigationHold`'s
    shape: ``configure(settings)`` every cycle (cheap, idempotent), one call
    per sample, a ``status()`` for the Service Window and a ``heartbeat()``
    for the cloud.
    """

    def __init__(
        self,
        device_id: str,
        firmware_version: str,
        state_dir: Path | str,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self._device_id = str(device_id)
        self._fw = str(firmware_version)
        self._state_dir = Path(state_dir)
        self._factory: AdapterFactory = adapter_factory or HapAdapter
        self._lock = threading.RLock()
        self._adapter: Any = None
        self._enabled = False
        self._port = DEFAULT_PORT
        self._error: str | None = None
        self._last: HomeSnapshot | None = None
        self._pushes = 0

    # -- configuration -----------------------------------------------------

    @property
    def name(self) -> str:
        n = accessory_name(self._device_id)
        low = n.lower()
        if any(w in low for w in BANNED_NAME_WORDS):  # pragma: no cover — guarded by test
            raise ValueError("accessory name carries a banned word")
        return n

    def configure(self, settings: Any) -> None:
        """Snapshot ``apple_home_enabled`` / ``apple_home_port`` and start or
        stop the accessory to match. Safe to call every cycle."""
        enabled = bool(getattr(settings, "apple_home_enabled", False))
        try:
            port = int(getattr(settings, "apple_home_port", DEFAULT_PORT) or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        with self._lock:
            port_changed = self._adapter is not None and port != self._port
            self._port = port
            if enabled and (self._adapter is None or port_changed) and self._error is None:
                if port_changed:
                    self._stop_locked()
                self._start_locked()
            elif not enabled and self._adapter is not None:
                self._stop_locked()
            if not enabled:
                # A disable clears a stuck error so a later enable retries.
                self._error = None
            self._enabled = enabled

    def _start_locked(self) -> None:
        try:
            self._adapter = self._factory(
                name=self.name,
                device_id=self._device_id,
                firmware_version=self._fw,
                persist_file=self._state_dir / STATE_FILENAME,
                port=self._port,
            )
            self._adapter.start()
            logger.info(
                "Apple Home accessory '%s' advertising on port %d (paired=%s)",
                self.name,
                self._port,
                self._adapter.paired,
            )
            if self._last is not None:
                self._push(self._last)
        except HapUnavailable:
            self._adapter = None
            self._error = "library_missing"
            logger.warning("Apple Home enabled but HAP-python is not installed — not advertising")
        except Exception as e:  # noqa: BLE001 — the accessory must never take the unit down
            self._adapter = None
            self._error = f"start_failed: {e}"
            logger.error("Apple Home accessory failed to start: %s", e)

    def _stop_locked(self) -> None:
        adapter, self._adapter = self._adapter, None
        if adapter is None:
            return
        try:
            adapter.stop()
            logger.info("Apple Home accessory stopped")
        except Exception as e:  # noqa: BLE001
            logger.warning("Apple Home accessory stop failed: %s", e)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    # -- per-sample --------------------------------------------------------

    def update(
        self,
        reading: dict[str, Any] | None,
        hold: dict[str, Any] | None = None,
        suspended: set[str] | None = None,
    ) -> HomeSnapshot:
        """Push one cycle's values. Pure mapping first, HAP writes second, and
        a HAP failure is logged, never raised into the sampling worker."""
        snap = snapshot_from_reading(reading, hold, suspended)
        with self._lock:
            self._last = snap
            if self._adapter is not None:
                self._push(snap)
        return snap

    def _push(self, snap: HomeSnapshot) -> None:
        a = self._adapter
        if a is None:
            return
        try:
            if snap.temperature_c is not None:
                a.set("temperature", snap.temperature_c)
            a.set("temperature_active", snap.temperature_active)
            a.set("temperature_fault", 1 if snap.temperature_fault else 0)
            a.set("contact", snap.contact_state)
            a.set("contact_active", snap.hold_enabled)
            a.set("contact_fault", 1 if snap.hold_fault else 0)
            for key, value in snap.custom.items():
                if value is not None:
                    a.set(f"custom:{key}", value)
            self._pushes += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Apple Home characteristic update failed: %s", e)

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The ``apple_home_status`` command's answer, for the Service Window
        only: carries the setup code and URI so the page can draw the QR."""
        with self._lock:
            a = self._adapter
            out: dict[str, Any] = {
                "enabled": self._enabled,
                "running": a is not None,
                "paired": bool(a.paired) if a is not None else False,
                "name": self.name,
                "port": self._port,
                "error": self._error,
                "pushes": self._pushes,
                "services": [dict(s) for s in SERVICES],
            }
            if a is not None and not a.paired:
                out["setupCode"] = a.setup_code
                out["setupUri"] = a.setup_uri
            return out

    def heartbeat(self) -> dict[str, Any]:
        """``appleHome`` on the heartbeat: enabled and paired, nothing else.
        No code, no URI, no name — the cloud has no business pairing."""
        with self._lock:
            a = self._adapter
            return {
                "enabled": self._enabled,
                "paired": bool(a.paired) if a is not None else False,
            }
