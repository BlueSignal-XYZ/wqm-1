"""Live hardware ports for Cloud pH calibration and RS485 sensor setup.

Applies commands through the existing ``CalibrationManager.calibrate_ph``
path and the Honde probe drivers. Config changes use the same YAML write
the rest of the firmware already uses.
"""

from __future__ import annotations

from typing import Any

from calibration.calibrate import CalibrationManager
from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
from sensors.modbus import ModbusBus
from service_window.config_editor import update_config
from utils.config import ADC_CH_PH

_SENSOR_BINDINGS: dict[str, tuple[str, str, type]] = {
    HondeChlorineSensor.__name__: ("rs485_chlorine", "_chlorine", HondeChlorineSensor),
    HondeOrpSensor.__name__: ("rs485_orp", "_orp", HondeOrpSensor),
    HondeMultiSensor.__name__: ("rs485_multi", "_multi", HondeMultiSensor),
}


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

    def rs485_add(self, sensor: str, address: int) -> list[dict[str, Any]]:
        prefix, attr, cls = _SENSOR_BINDINGS[sensor]
        self._write_local({f"{prefix}_enabled": True, f"{prefix}_addr": address})
        bus = self._ensure_bus()
        setattr(self._app, attr, cls(bus, address))
        self._sync()
        return self.rs485_list()

    def rs485_set_address(
        self, sensor: str, address: int, new_address: int
    ) -> list[dict[str, Any]]:
        prefix, attr, cls = _SENSOR_BINDINGS[sensor]
        probe = getattr(self._app, attr, None)
        if probe is not None:
            live = getattr(probe, "address", None)
            if live != address:
                raise RuntimeError("no probe at that address")
        else:
            probe = cls(self._ensure_bus(), address)
            setattr(self._app, attr, probe)
        probe.set_address(new_address)
        self._write_local({f"{prefix}_addr": new_address})
        self._sync()
        return self.rs485_list()

    def rs485_set_enabled(
        self, sensor: str, address: int | None, enabled: bool
    ) -> list[dict[str, Any]]:
        prefix, attr, cls = _SENSOR_BINDINGS[sensor]
        updates: dict[str, Any] = {f"{prefix}_enabled": enabled}
        if address is not None:
            updates[f"{prefix}_addr"] = address
        self._write_local(updates)
        if enabled:
            current = address
            if current is None:
                current = int(getattr(self._live_settings(), f"{prefix}_addr"))
            setattr(self._app, attr, cls(self._ensure_bus(), current))
        else:
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
        for name, (prefix, _attr, _cls) in _SENSOR_BINDINGS.items():
            rows.append(
                {
                    "sensor": name,
                    "address": int(getattr(settings, f"{prefix}_addr")),
                    "enabled": bool(getattr(settings, f"{prefix}_enabled")),
                }
            )
        return rows

    def _write_local(self, updates: dict[str, Any]) -> None:
        config = self._app._config
        path = str(config._base_path)
        update_config(path, updates)
        config.reload()

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
