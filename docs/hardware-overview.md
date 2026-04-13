# Hardware Overview

Board architecture, component selection, and power chain for the WQM-1.

## Form factor

- **Board dimensions:** 120 × 105 mm
- **Mounting:** stacks on a Raspberry Pi Zero 2W via the 40-pin GPIO
  header; standoffs on four corners
- **Connectors:** 24 V DC screw terminal, BNC/phoenix inputs for the
  wet-side probes, U.FL for the LoRa antenna, SMA for GPS

## BOM reference

See [`hardware/bom/`](../hardware/bom/) for the full bill of materials.
Approximate cost: **~$147 per unit at 10-unit quantity.**

## Signal chain

| Block | Part | Notes |
|-------|------|-------|
| Analog-to-digital | **ADS1115** (single, I²C @ 0x48) | 16-bit, four channels, ±6.144 V PGA range |
| pH analog front-end | **LMP91200** | Configurable-gain pH signal conditioner |
| TDS excitation | **CD4060** + **LM324** | Square-wave generator + buffer for AC excitation of the TDS probe (avoids electroplating) |
| Turbidity / ORP | direct into ADS1115 | No dedicated AFE |
| Temperature | **DS18B20** (1-Wire on GPIO 4) | Digital, no ADC channel consumed |
| Relays | **4× G5Q-14** optoisolated | Dosing pumps, aerators, valves |
| Radio | **SX1262** (SPI) | LoRa / LoRaWAN, up to +22 dBm |
| GPS | **u-blox** module on UART0 | NMEA output on `/dev/serial0` |

## Power chain

```
24 V DC in
    │
    ▼
 LMR51450 ──▶  5 V rail   (Pi Zero 2W, digital logic)
    │
    ▼
 TPS560430 ──▶ 6.5 V rail (headroom for the ±3.0 V analog supply)
    │
    ▼
 AMS1117 ──▶   3.3 V rail (I²C pull-ups, sensor bias)
    │
    ▼
 ±3.0 V analog rails (bipolar rails for the LMP91200 and LM324)
```

The analog rails are isolated from the digital 3.3 V rail to keep
switching noise out of the sensor front-end.

## Pinout

The active device-tree pin assignments are documented in
[`config/pinmap.yaml`](../config/pinmap.yaml) and match the
constants in `src/utils/config.py`.
