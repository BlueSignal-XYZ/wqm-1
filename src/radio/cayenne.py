"""
Cayenne LPP Encoder/Decoder

Encodes sensor readings into Cayenne Low Power Payload format for LoRaWAN.
Channel assignments match the BlueSignal platform contract.
"""

import struct
from typing import Any

# Cayenne LPP type codes
LPP_TEMPERATURE = 0x67  # 2 bytes, 0.1°C signed
LPP_ANALOG_INPUT = 0x02  # 2 bytes, 0.01 signed
LPP_GPS = 0x88  # 9 bytes: lat(3) lon(3) alt(3)
LPP_DIGITAL_INPUT = 0x00  # 1 byte, unsigned
LPP_LUMINOSITY = 0x65  # 2 bytes, unsigned 0-65535 — reused as a plain counter

# Channel assignments (platform contract — DO NOT CHANGE without coordination)
CH_TEMPERATURE = 1
CH_PH = 2
CH_TDS = 3
CH_TURBIDITY = 4
CH_ORP = 5
CH_GPS = 6
# RS485 sensor expansion (additive — v1.1.0 decoders ignore unknown
# channels). Channels 7-9 are ALREADY CLAIMED by the platform contract
# (relay_state / battery_voltage / calibration_status in the TTN decoder's
# CAYENNE_TO_CANONICAL map), so the new sensors start at 10.
CH_CHLORINE = 10
CH_CONDUCTIVITY = 11
CH_SALINITY = 12

# Relay control telemetry (v2.3, Commercial tier). CH7 is the already-claimed
# relay_state slot in the platform contract; 13-17 are new and additive, so
# older decoders simply ignore them.
#
# A LoRaWAN payload is far too small for a per-transition audit trail, so only
# a SUMMARY rides the uplink: current load state, contact wear, and the cause
# of the most recent transition. The full trail lives in SQLite WAL and syncs
# over a real backhaul.
CH_RELAY_STATE = 7
CH_RELAY_CYCLES_BASE = 13  # 13,14,15,16 -> channels 1-4
CH_RELAY_CAUSE = 17

# Cause encoded as a small int so it fits a single byte.
CAUSE_CODES = {
    "setpoint": 1,
    "manual": 2,
    "staleness": 3,
    "watchdog": 4,
    "commissioning_test": 5,
    "ota": 6,
}
CODE_TO_CAUSE = {v: k for k, v in CAUSE_CODES.items()}

# Analog-input LPP packs value*100 into a signed 16-bit int, capping at
# 327.67. EC spans 0-10,000 µS/cm, so it rides the channel in mS/cm.
_US_PER_MS = 1000.0


def encode(data: dict[str, Any]) -> bytes:
    """
    Encode a reading dict into Cayenne LPP binary payload.

    Args:
        data: Dict with keys: temp_c, ph, tds_ppm, turbidity_ntu, orp_mv,
              lat, lon, alt_m

    Returns:
        Cayenne LPP encoded bytes (max ~31 bytes for full reading)
    """
    buf = bytearray()

    # CH1: Temperature (0.1°C resolution, signed)
    temp = data.get("temp_c")
    if temp is not None:
        val = max(-32768, min(32767, int(round(temp * 10))))
        buf.append(CH_TEMPERATURE)
        buf.append(LPP_TEMPERATURE)
        buf += struct.pack(">h", val)

    # CH2: pH (analog input, 0.01 resolution)
    ph = data.get("ph")
    if ph is not None:
        val = max(-32768, min(32767, int(round(ph * 100))))
        buf.append(CH_PH)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # CH3: TDS (analog input, 0.01 resolution)
    tds = data.get("tds_ppm")
    if tds is not None:
        val = max(-32768, min(32767, int(round(tds * 100))))
        buf.append(CH_TDS)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # CH4: Turbidity (analog input, 0.01 resolution)
    turb = data.get("turbidity_ntu")
    if turb is not None:
        val = max(-32768, min(32767, int(round(turb * 100))))
        buf.append(CH_TURBIDITY)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # CH5: ORP (analog input, 0.01 resolution)
    orp = data.get("orp_mv")
    if orp is not None:
        val = max(-32768, min(32767, int(round(orp * 100))))
        buf.append(CH_ORP)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # CH10: Residual chlorine (analog input, 0.01 mg/L resolution)
    chlorine = data.get("chlorine_mgl")
    if chlorine is not None:
        val = max(-32768, min(32767, int(round(chlorine * 100))))
        buf.append(CH_CHLORINE)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # CH11: Conductivity in mS/cm (0.01 mS = 10 µS resolution; see _US_PER_MS)
    ec = data.get("conductivity_uscm")
    if ec is not None:
        val = max(-32768, min(32767, int(round(ec / _US_PER_MS * 100))))
        buf.append(CH_CONDUCTIVITY)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # CH12: Salinity (analog input, 0.01 ppt resolution)
    salinity = data.get("salinity_ppt")
    if salinity is not None:
        val = max(-32768, min(32767, int(round(salinity * 100))))
        buf.append(CH_SALINITY)
        buf.append(LPP_ANALOG_INPUT)
        buf += struct.pack(">h", val)

    # --- Relay control summary (Commercial tier) ---
    # Emitted only when the device actually has control configured, so
    # monitoring-only units keep byte-for-byte identical payloads.
    relay_state = data.get("relay_state")
    if relay_state is not None:
        buf.append(CH_RELAY_STATE)
        buf.append(LPP_DIGITAL_INPUT)
        buf.append(int(relay_state) & 0x0F)

    cycles = data.get("relay_cycles") or {}
    for channel in (1, 2, 3, 4):
        count = cycles.get(channel, cycles.get(str(channel)))
        if count is None:
            continue
        buf.append(CH_RELAY_CYCLES_BASE + channel - 1)
        buf.append(LPP_LUMINOSITY)
        buf += struct.pack(">H", max(0, min(65535, int(count))))

    cause = data.get("relay_last_cause")
    if cause is not None:
        code = CAUSE_CODES.get(str(cause))
        if code is not None:
            buf.append(CH_RELAY_CAUSE)
            buf.append(LPP_DIGITAL_INPUT)
            buf.append(code)

    # CH6: GPS (lat/lon in 0.0001°, alt in 0.01 m)
    lat = data.get("lat")
    lon = data.get("lon")
    alt = data.get("alt_m")
    if lat is not None and lon is not None:
        buf.append(CH_GPS)
        buf.append(LPP_GPS)
        buf += _int24(int(round(lat * 10000)))
        buf += _int24(int(round(lon * 10000)))
        buf += _int24(int(round((alt or 0) * 100)))

    return bytes(buf)


def decode(payload: bytes) -> dict[str, Any]:
    """
    Decode Cayenne LPP binary payload back to a reading dict.

    Useful for parsing downlink commands or testing.
    """
    result: dict[str, Any] = {}
    i = 0
    while i < len(payload) - 1:
        channel = payload[i]
        lpp_type = payload[i + 1]
        i += 2

        if lpp_type == LPP_TEMPERATURE and i + 2 <= len(payload):
            val = struct.unpack(">h", payload[i : i + 2])[0]
            result[_CHANNEL_TO_KEY.get(channel, f"ch{channel}")] = val / 10.0
            i += 2

        elif lpp_type == LPP_ANALOG_INPUT and i + 2 <= len(payload):
            val = struct.unpack(">h", payload[i : i + 2])[0] / 100.0
            if channel == CH_CONDUCTIVITY:
                val *= _US_PER_MS  # channel carries mS/cm; reading key is µS/cm
            result[_CHANNEL_TO_KEY.get(channel, f"ch{channel}")] = val
            i += 2

        elif lpp_type == LPP_GPS and i + 9 <= len(payload):
            lat = _from_int24(payload[i : i + 3]) / 10000.0
            lon = _from_int24(payload[i + 3 : i + 6]) / 10000.0
            alt = _from_int24(payload[i + 6 : i + 9]) / 100.0
            result["lat"] = lat
            result["lon"] = lon
            result["alt_m"] = alt
            i += 9
        else:
            break  # unknown type, stop parsing

    return result


# Channel-to-key mapping for decode
_CHANNEL_TO_KEY = {
    CH_TEMPERATURE: "temp_c",
    CH_PH: "ph",
    CH_TDS: "tds_ppm",
    CH_TURBIDITY: "turbidity_ntu",
    CH_ORP: "orp_mv",
    CH_CHLORINE: "chlorine_mgl",
    CH_CONDUCTIVITY: "conductivity_uscm",
    CH_SALINITY: "salinity_ppt",
}


def _int24(val: int) -> bytes:
    """Pack a signed integer into 3 bytes big-endian."""
    if val < 0:
        val = (1 << 24) + val
    return bytes([(val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF])


def _from_int24(data: bytes) -> int:
    """Unpack 3 bytes big-endian into signed integer."""
    val = (data[0] << 16) | (data[1] << 8) | data[2]
    if val >= (1 << 23):
        val -= 1 << 24
    return val
