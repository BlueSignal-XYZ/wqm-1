"""Live hardware ports for Cloud pH calibration and RS485 sensor setup.

Applies commands through the existing ``CalibrationManager.calibrate_ph``
path and the RS485 probe drivers. Config changes use the same YAML write
the rest of the firmware already uses.
"""

from __future__ import annotations

from typing import Any

from calibration.calibrate import CalibrationManager
from cloud.device_commands import WIRE_SENSORS
from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
from sensors.modbus import ModbusBus
from service_window.config_editor import update_config
from utils.config import ADC_CH_PH

_PREFIX_ATTR: dict[type, tuple[str, str]] = {
    HondeChlorineSensor: ("rs485_chlorine", "_chlorine"),
    HondeOrpSensor: ("rs485_orp", "_orp"),
    HondeMultiSensor: ("rs485_multi", "_multi"),
}


def _binding(sensor: str) -> tuple[str, str, type]:
    cls = WIRE_SENSORS[sensor]
    prefix, attr = _PREFIX_ATTR[cls]
    return prefix, attr, cls


class Wqm1CommandHardware:
    """Adapter from a running ``WQM1App`` to ``CommandHardware``."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def read_ph_adc_volts(self) -> float:
        adc = self._app._adc
        if adc is None:
            raise RuntimeError(self._app._adc_error or "no ADC on this board")
        return float(adc.read_voltage(ADC_CH_PH))

    def apply_analog_ph(self, v_ph4: float, v_ph7: float) -> dict[str, float]:
        cal = self._app._cal
        if not isinstance(cal, CalibrationManager):
            raise RuntimeError("calibration manager is not ready")
        slope = float(cal.calibrate_ph(v_ph4, v_ph7))
        ph = self._app._ph
        if ph is not None and hasattr(ph, "set_calibration"):
            ph.set_calibration(v_ph4, v_ph7, calibrated=True)
        return {"slope": slope, "offset": 7.0 - slope * v_ph7}

    def calibrate_rs485_ph_point(self, buffer: str) -> None:
        probe = self._app._multi
        if probe is None:
            raise RuntimeError("5-in-1 probe is not enabled")
        probe.calibrate_ph_point(buffer)

    def read_rs485_ph(self) -> float:
        probe = self._app._multi
        if probe is None:
            raise RuntimeError("5-in-1 probe is not enabled")
        values = probe.read_all()
        if not isinstance(values, dict) or "ph" not in values:
            live = getattr(probe, "address", "?")
            raise RuntimeError(f"no response at address {live}")
        return float(values["ph"])

    def rs485_add(self, sensor: str, address: int) -> list[dict[str, Any]]:
        prefix, attr, cls = _binding(sensor)
        bus = self._ensure_bus()
        probe = cls(bus, address)
        self._confirm_answers(probe, address)
        self._write_local({f"{prefix}_enabled": True, f"{prefix}_addr": address})
        setattr(self._app, attr, probe)
        self._sync()
        return self.rs485_list()

    def rs485_set_address(
        self, sensor: str, address: int, new_address: int
    ) -> list[dict[str, Any]]:
        prefix, attr, _cls = _binding(sensor)
        probe = getattr(self._app, attr, None)
        if probe is None:
            raise RuntimeError("no probe at that address")
        live = getattr(probe, "address", None)
        if live != address:
            raise RuntimeError("no probe at that address")
        self._reject_address_in_use(new_address, except_prefix=prefix)
        probe.set_address(new_address)
        updates = {f"{prefix}_addr": new_address}
        try:
            self._write_local(updates)
        except Exception:
            try:
                self._write_local(updates)
            except Exception as exc:
                raise RuntimeError(
                    f"probe is at address {new_address}; config write failed"
                ) from exc
        self._sync()
        return self.rs485_list()

    def rs485_set_enabled(
        self, sensor: str, address: int | None, enabled: bool
    ) -> list[dict[str, Any]]:
        prefix, attr, cls = _binding(sensor)
        updates: dict[str, Any] = {f"{prefix}_enabled": enabled}
        if address is not None:
            updates[f"{prefix}_addr"] = address
        if enabled:
            current = address
            if current is None:
                current = int(getattr(self._live_settings(), f"{prefix}_addr"))
            probe = cls(self._ensure_bus(), current)
            self._confirm_answers(probe, current)
            self._write_local(updates)
            setattr(self._app, attr, probe)
        else:
            self._write_local(updates)
            setattr(self._app, attr, None)
        self._sync()
        return self.rs485_list()

    def _live_settings(self) -> Any:
        config = getattr(self._app, "_config", None)
        if config is not None and hasattr(config, "settings"):
            return config.settings
        return self._app._settings

    def rs485_list(self) -> list[dict[str, Any]]:
        settings = self._live_settings()
        rows: list[dict[str, Any]] = []
        for name, cls in WIRE_SENSORS.items():
            prefix, _attr = _PREFIX_ATTR[cls]
            rows.append(
                {
                    "sensor": name,
                    "address": int(getattr(settings, f"{prefix}_addr")),
                    "enabled": bool(getattr(settings, f"{prefix}_enabled")),
                }
            )
        return rows

    def _confirm_answers(self, probe: Any, address: int) -> None:
        if hasattr(probe, "read_all"):
            value = probe.read_all()
        elif hasattr(probe, "read"):
            value = probe.read()
        else:
            raise RuntimeError(f"no response at address {address}")
        if value is None:
            raise RuntimeError(f"no response at address {address}")

    def _reject_address_in_use(self, new_address: int, except_prefix: str) -> None:
        settings = self._live_settings()
        for _name, cls in WIRE_SENSORS.items():
            prefix, _attr = _PREFIX_ATTR[cls]
            if prefix == except_prefix:
                continue
            if not bool(getattr(settings, f"{prefix}_enabled", False)):
                continue
            if int(getattr(settings, f"{prefix}_addr")) == new_address:
                raise RuntimeError(f"address {new_address} is already in use")

    def _write_local(self, updates: dict[str, Any]) -> None:
        config = self._app._config
        path = str(config._base_path)
        update_config(path, updates)
        config.reload()
        # Overlay wins on reload; pin this command's writes so they are not undone.
        settings = config.settings
        for key, value in updates.items():
            setattr(settings, key, value)

    def _ensure_bus(self) -> ModbusBus:
        bus = self._app._modbus_bus
        if bus is not None:
            return bus
        settings = self._live_settings()
        bus = ModbusBus(settings.rs485_port, baudrate=settings.rs485_baud)
        self._app._modbus_bus = bus
        return bus

    def _sync(self) -> None:
        sync = getattr(self._app, "_sync_sampler", None)
        if callable(sync):
            sync()
