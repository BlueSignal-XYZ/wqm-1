"""Apply Cloud queued commands for pH calibration and RS485 sensor setup.

Commands from ``GET .../commands/pending`` enter only through
``DeviceCommandDispatcher``. The allowed types are ``ph_calibrate`` and
``rs485_config``. Any other type, extra field, non-finite number, missing
string ``id``, or expired ``expiresAt`` is rejected with a short plain
message. This module does not import actuation drivers and does not offer a
path to them.
"""

from __future__ import annotations

import json
import logging
import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
from utils.atomic import atomic_write_text

logger = logging.getLogger("wqm1.device_commands")

COMMAND_TYPES = frozenset({"ph_calibrate", "rs485_config"})
COMMAND_KEYS = frozenset({"id", "type", "payload", "expiresAt"})
PH_KEYS = frozenset({"step", "probe", "buffer"})
PH_STEPS = frozenset({"start", "point", "finish"})
PH_PROBES = frozenset({"analog", "rs485_multi"})
RS485_KEYS = frozenset({"op", "sensor", "address", "newAddress", "enabled"})
RS485_OPS = frozenset({"add", "set_address", "enable", "disable"})
# Server #1479 wire names. Class names are internal only.
WIRE_SENSORS: dict[str, type] = {
    "residual_chlorine": HondeChlorineSensor,
    "RD-ORP-WE-01": HondeOrpSensor,
    "RD-PETSTS-01": HondeMultiSensor,
}
ALLOWED_SENSORS = frozenset(WIRE_SENSORS)
ANALOG_BUFFERS = frozenset({4.0, 7.0})
# Labels come from the probe driver so a rename there fails this import, not a string search.
RS485_BUFFER_LABELS = {round(float(label), 2): label for label in HondeMultiSensor.PH_CAL_POINTS}
MESSAGE_MAX = 200
# Analog `point` samples. Spread is converter volts, not pH.
ANALOG_POINT_READS = 5
ANALOG_POINT_MAX_SPREAD_V = 0.02
HANDLED_COMMANDS_MAX = 64
# When Cloud omits a usable expiresAt, keep the id this long so a reboot
# or a flood of later commands cannot re-apply it.
HANDLED_WITHOUT_EXPIRES_MS = 24 * 60 * 60 * 1000


class CommandHardware(Protocol):
    """Ports the dispatcher needs. No actuation methods live here."""

    def read_ph_adc_volts(self) -> float: ...

    def apply_analog_ph(self, v_ph4: float, v_ph7: float) -> dict[str, float]: ...

    def calibrate_rs485_ph_point(self, buffer: str) -> None: ...

    def rs485_add(self, sensor: str, address: int) -> list[dict[str, Any]]: ...

    def rs485_set_address(
        self, sensor: str, address: int, new_address: int
    ) -> list[dict[str, Any]]: ...

    def rs485_set_enabled(
        self, sensor: str, address: int | None, enabled: bool
    ) -> list[dict[str, Any]]: ...

    def rs485_list(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CommandOutcome:
    status: str
    result: dict[str, Any] | None = None
    message: str = ""


@dataclass(frozen=True)
class _Handled:
    outcome: CommandOutcome
    expires_at_ms: int


def _clip(message: str) -> str:
    return message[:MESSAGE_MAX]


def _rejected(message: str) -> CommandOutcome:
    return CommandOutcome(status="rejected", message=_clip(message))


def _applied(message: str, result: dict[str, Any] | None = None) -> CommandOutcome:
    return CommandOutcome(status="applied", result=result, message=_clip(message))


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _unexpected(keys: set[str], allowed: frozenset[str]) -> str | None:
    extra = sorted(key for key in keys if key not in allowed)
    if extra:
        return f"unexpected field: {extra[0]}"
    return None


def _measured_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _buffer_label(probe: str, value: Any) -> str | CommandOutcome:
    if not _is_number(value):
        return _rejected("buffer must be a number")
    if not math.isfinite(float(value)):
        return _rejected("non-finite number")
    rounded = round(float(value), 2)
    if probe == "analog":
        if rounded not in ANALOG_BUFFERS:
            return _rejected("buffer not allowed for analog")
        return f"{rounded:.1f}"
    if rounded not in RS485_BUFFER_LABELS:
        return _rejected("buffer not allowed for rs485_multi")
    return RS485_BUFFER_LABELS[rounded]


def _modbus_addr(payload: dict[str, Any], key: str) -> int | CommandOutcome:
    if key not in payload:
        return _rejected(f"missing {key}")
    value = payload[key]
    if not _is_number(value):
        return _rejected(f"{key} must be a number")
    if not math.isfinite(float(value)):
        return _rejected("non-finite number")
    if isinstance(value, float) and not value.is_integer():
        return _rejected(f"{key} must be an integer")
    address = int(value)
    if not 1 <= address <= 247:
        return _rejected(f"{key} must be 1-247")
    return address


class DeviceCommandDispatcher:
    """Validates one pending command and applies it through CommandHardware."""

    def __init__(
        self,
        hardware: CommandHardware,
        now_ms: Callable[[], int] | None = None,
        cache_path: Path | str | None = None,
    ) -> None:
        self._hardware = hardware
        self._now_ms = now_ms or (lambda: int(datetime.now(UTC).timestamp() * 1000))
        self._analog_volts: dict[float, float] = {}
        self._rs485_buffers: list[float] = []
        self._analog_started = False
        self._rs485_started = False
        self._cache_path = Path(cache_path) if cache_path is not None else None
        self._handled: OrderedDict[str, _Handled] = self._load_handled()

    def apply(self, command: dict[str, Any]) -> CommandOutcome:
        if not isinstance(command, dict):
            return _rejected("command must be an object")
        cmd_id = command.get("id")
        if not isinstance(cmd_id, str) or not cmd_id:
            return _rejected("missing id")
        self._evict_expired()
        if cmd_id in self._handled:
            self._handled.move_to_end(cmd_id)
            return self._handled[cmd_id].outcome
        keep_until = self._keep_until_ms(command.get("expiresAt"))
        unexpected = _unexpected(set(command), COMMAND_KEYS)
        if unexpected:
            return self._store(cmd_id, _rejected(unexpected), keep_until)
        cmd_type = command.get("type")
        if not isinstance(cmd_type, str) or not cmd_type:
            return self._store(cmd_id, _rejected("missing type"), keep_until)
        if cmd_type not in COMMAND_TYPES:
            return self._store(cmd_id, _rejected("unknown type"), keep_until)
        expires_at = command.get("expiresAt")
        if expires_at is None:
            return self._store(cmd_id, _rejected("missing expiresAt"), keep_until)
        if not _finite(expires_at):
            return self._store(cmd_id, _rejected("non-finite number"), keep_until)
        if self._now_ms() >= int(expires_at):
            return self._store(cmd_id, _rejected("expired"), keep_until)
        payload = command.get("payload")
        if not isinstance(payload, dict):
            return self._store(cmd_id, _rejected("payload must be an object"), keep_until)
        try:
            if cmd_type == "ph_calibrate":
                outcome = self._ph_calibrate(payload)
            else:
                outcome = self._rs485_config(payload)
        except Exception as exc:  # noqa: BLE001 — one failed command must post rejected
            outcome = _rejected(str(exc) or "command failed")
        return self._store(cmd_id, outcome, keep_until)

    def _keep_until_ms(self, raw: Any) -> int:
        if _finite(raw):
            return int(raw)
        return self._now_ms() + HANDLED_WITHOUT_EXPIRES_MS

    def _evict_expired(self) -> None:
        now = self._now_ms()
        for cmd_id in [key for key, row in self._handled.items() if now >= row.expires_at_ms]:
            del self._handled[cmd_id]

    def _store(self, cmd_id: Any, outcome: CommandOutcome, keep_until: int) -> CommandOutcome:
        if not isinstance(cmd_id, str) or not cmd_id:
            return outcome
        self._evict_expired()
        self._handled[cmd_id] = _Handled(outcome=outcome, expires_at_ms=keep_until)
        self._handled.move_to_end(cmd_id)
        self._save_handled()
        return outcome

    def _load_handled(self) -> OrderedDict[str, _Handled]:
        if self._cache_path is None or not self._cache_path.is_file():
            return OrderedDict()
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Handled-command cache unreadable: %s", exc)
            return OrderedDict()
        if not isinstance(raw, list):
            return OrderedDict()
        handled: OrderedDict[str, _Handled] = OrderedDict()
        fallback = self._now_ms() + HANDLED_WITHOUT_EXPIRES_MS
        for item in raw:
            if not (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], dict)
            ):
                continue
            body = item[1]
            status = body.get("status")
            if status not in ("applied", "rejected"):
                continue
            expires = body.get("expiresAt")
            keep_until = int(expires) if _finite(expires) else fallback
            handled[item[0]] = _Handled(
                outcome=CommandOutcome(
                    status=status,
                    result=body.get("result") if isinstance(body.get("result"), dict) else None,
                    message=str(body.get("message") or ""),
                ),
                expires_at_ms=keep_until,
            )
        return handled

    def _save_handled(self) -> None:
        if self._cache_path is None:
            return
        payload = [
            [
                cmd_id,
                {
                    "status": row.outcome.status,
                    "result": row.outcome.result,
                    "message": row.outcome.message,
                    "expiresAt": row.expires_at_ms,
                },
            ]
            for cmd_id, row in self._handled.items()
        ]
        try:
            atomic_write_text(
                self._cache_path,
                json.dumps(payload, separators=(",", ":")),
            )
        except OSError as exc:
            logger.warning("Handled-command cache not written: %s", exc)

    def _ph_calibrate(self, payload: dict[str, Any]) -> CommandOutcome:
        unexpected = _unexpected(set(payload), PH_KEYS)
        if unexpected:
            return _rejected(unexpected)
        step = payload.get("step")
        probe = payload.get("probe")
        if step not in PH_STEPS:
            return _rejected("unknown step")
        if probe not in PH_PROBES:
            return _rejected("unknown probe")
        if step == "start":
            if "buffer" in payload:
                return _rejected("buffer is only valid for point")
            if probe == "analog":
                self._analog_volts = {}
                self._analog_started = True
            else:
                self._rs485_buffers = []
                self._rs485_started = True
            return _applied("calibration started", {"buffers": [], "measuredAt": _measured_at()})
        if step == "point":
            label = _buffer_label(probe, payload.get("buffer"))
            if isinstance(label, CommandOutcome):
                return label
            started = self._analog_started if probe == "analog" else self._rs485_started
            if not started:
                return _rejected("calibration has not started")
            if probe == "analog":
                volts = self._read_analog_point()
                if isinstance(volts, CommandOutcome):
                    return volts
                self._analog_volts[round(float(payload["buffer"]), 2)] = volts
                return _applied(
                    f"pH {label} point stored",
                    {"buffers": sorted(self._analog_volts), "measuredAt": _measured_at()},
                )
            self._hardware.calibrate_rs485_ph_point(label)
            value = round(float(payload["buffer"]), 2)
            if value not in self._rs485_buffers:
                self._rs485_buffers.append(value)
            return _applied(
                f"pH {label} point stored",
                {"buffers": list(self._rs485_buffers), "measuredAt": _measured_at()},
            )
        if "buffer" in payload:
            return _rejected("buffer is only valid for point")
        started = self._analog_started if probe == "analog" else self._rs485_started
        if not started:
            return _rejected("calibration has not started")
        if probe == "analog":
            return self._finish_analog()
        return self._finish_rs485()

    def _read_analog_point(self) -> float | CommandOutcome:
        samples: list[float] = []
        for _ in range(ANALOG_POINT_READS):
            volts = float(self._hardware.read_ph_adc_volts())
            if not math.isfinite(volts):
                return _rejected("non-finite number")
            samples.append(volts)
        spread = max(samples) - min(samples)
        if spread > ANALOG_POINT_MAX_SPREAD_V:
            return _rejected("voltage not stable")
        return float(median(samples))

    def _finish_analog(self) -> CommandOutcome:
        if 4.0 not in self._analog_volts or 7.0 not in self._analog_volts:
            return _rejected("missing calibration points")
        v4 = self._analog_volts[4.0]
        v7 = self._analog_volts[7.0]
        if abs(v7 - v4) < 0.001:
            return _rejected("voltages too close")
        applied = self._hardware.apply_analog_ph(v4, v7)
        slope = float(applied["slope"])
        offset = float(applied["offset"])
        self._analog_volts = {}
        self._analog_started = False
        return _applied(
            "pH calibration applied",
            {
                "slope": slope,
                "offset": offset,
                "buffers": [4.0, 7.0],
                "measuredAt": _measured_at(),
            },
        )

    def _finish_rs485(self) -> CommandOutcome:
        if not self._rs485_buffers:
            return _rejected("missing calibration points")
        buffers = list(self._rs485_buffers)
        self._rs485_buffers = []
        self._rs485_started = False
        return _applied(
            "pH calibration applied",
            {
                "slope": None,
                "offset": None,
                "buffers": buffers,
                "measuredAt": _measured_at(),
            },
        )

    def _rs485_config(self, payload: dict[str, Any]) -> CommandOutcome:
        unexpected = _unexpected(set(payload), RS485_KEYS)
        if unexpected:
            return _rejected(unexpected)
        op = payload.get("op")
        sensor = payload.get("sensor")
        if op not in RS485_OPS:
            return _rejected("unknown op")
        if not isinstance(sensor, str) or sensor not in ALLOWED_SENSORS:
            return _rejected("sensor not allowed")
        if op == "add":
            address = _modbus_addr(payload, "address")
            if isinstance(address, CommandOutcome):
                return address
            sensors = self._hardware.rs485_add(sensor, address)
            return _applied("RS485 sensor added", {"sensors": sensors})
        if op == "set_address":
            address = _modbus_addr(payload, "address")
            if isinstance(address, CommandOutcome):
                return address
            new_address = _modbus_addr(payload, "newAddress")
            if isinstance(new_address, CommandOutcome):
                return new_address
            sensors = self._hardware.rs485_set_address(sensor, address, new_address)
            return _applied("RS485 address set", {"sensors": sensors})
        enabled = payload.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return _rejected("enabled must be a boolean")
        if op == "enable" and enabled is False:
            return _rejected("enabled must be true for enable")
        if op == "disable" and enabled is True:
            return _rejected("enabled must be false for disable")
        maybe_addr: int | None = None
        if "address" in payload:
            parsed = _modbus_addr(payload, "address")
            if isinstance(parsed, CommandOutcome):
                return parsed
            maybe_addr = parsed
        sensors = self._hardware.rs485_set_enabled(sensor, maybe_addr, op == "enable")
        return _applied(
            "RS485 sensor enabled" if op == "enable" else "RS485 sensor disabled",
            {"sensors": sensors},
        )
