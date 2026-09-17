# Hardware Overview

Board architecture, component selection, and power chain for the WQM-1.

## Form factor

- **Board dimensions:** 120 × 105 mm
- **Mounting:** stacks on a Raspberry Pi Zero 2W via the 40-pin GPIO
  header; standoffs on four corners
- **Connectors:** 24 V DC screw terminal, BNC (pH) / JST-XH (TDS, turbidity,
  1-Wire, I²C expansion) inputs for the wet-side probes, SMA for the LoRa
  antenna, SMA for GPS
- **Power:** the 24 V input is the only source for the analog rails
  (+6.5 V → +5VA → ±3.0 V) and the relay coils (VRLY). USB-C on the Pi powers
  only the Pi, the 3.3 V rail (LoRa, GPS) and the turbidity probe's 5 V.

## BOM reference

See [`hardware/bom/`](../hardware/bom/) for the full bill of materials.
Approximate cost: **~$147 per unit at 10-unit quantity.**

## Signal chain

| Block | Part | Notes |
|-------|------|-------|
| Analog-to-digital | **ADS1115** (single, I²C @ 0x48, on +5VA) | 16-bit, four channels; firmware uses PGA ±4.096 V on AIN0/2/3 and ±6.144 V on AIN1 (turbidity, 0–4.5 V chain) |
| pH analog front-end | **LMP91200** | Configurable-gain pH signal conditioner |
| TDS excitation | **CD4060** + **LM324** | Hardware square-wave + rectifier/LPF for AC excitation of the TDS probe (avoids electroplating). Firmware 2.1.1 does **not** drive this oscillator (sampling-only). Q4 band is a BUILD **ESTIMATE** — see below. |
| Turbidity | direct into ADS1115 (AIN1, LMV321 buffer) | No dedicated AFE |
| ORP | **not on the board** — optional RS485 digital probe | On rev Fin_3 the BNC front end is pH only and AIN3 is spare (`PH_INN`). See `config/pinmap.yaml`. Earlier revisions of this table showed analog ORP sharing the pH BNC; there is no such circuit. |
| Temperature | **DS18B20** (1-Wire on GPIO 4) | Digital, no ADC channel consumed |
| Flow meter (2.3.0) | inline pulse meter on **GPIO 26** (harness, see below), or a clamp-on ultrasonic meter on the RS485 bus | The WQM-1 as a CT clamp for water: the meter's lifetime totalizer is the evidence a QC settlement accrues from. No ADC channel; all four ADS1115 inputs are taken, so a 4–20 mA meter is a board revision |
| Relays | **4× G5Q-14** optoisolated | Dosing pumps, aerators, valves |
| Radio | **SX1262** (SPI) | LoRa / LoRaWAN, up to +22 dBm |
| GPS | **u-blox** module on UART0 | NMEA output on `/dev/serial0` |

## Flow-meter pulse harness (2.3.0) — install-day note, not a board feature

Fin_3 has no flow-meter header. BCM **6, 7 and 26** are the unused lines on
the Pi's 40-pin header; the firmware defaults to **26** (`flow_pulse_gpio`).
Most inline hall-effect meters (YF-S201 class) are **5 V open-collector**
sensors with a 5 V supply pin:

- Supply the meter from the Pi's 5 V pin.
- **Pull the signal line up to 3.3 V** (10 kΩ to the Pi's 3.3 V pin), and
  never to 5 V — the GPIO is not 5 V tolerant. An open-collector output only
  ever pulls the line low, so a 3.3 V pull-up is all the level shifting it
  needs. A meter with a push-pull 5 V output needs a divider or a level
  shifter instead; read the datasheet before wiring.
- Keep the signal run short and away from the pump leads; the firmware
  debounces at 500 µs in the kernel, which still passes 2 kHz.

The lifetime pulse count is persisted in the SQLite `meta` table on every
sample, so a reboot never restarts the totalizer. Losing the card does — and
the cloud records that as a counter reset rather than pretending otherwise.
A dedicated, protected header is on the list for the next board revision.

The clamp-on ultrasonic alternative (TUF-2000M class) needs no harness: it
joins the RS485 bus at its own address (`rs485_flow_addr`) and keeps its own
totalizer. Its register map is transcribed from the manual and **has not yet
been verified against a live meter**; the first bench read is the check.

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
 AMS1117 ──▶   3.3 V rail (PCA9306s, SX1262, GPS, LED — not CD4060/LM324)
    │
    ▼
 ±3.0 V analog rails (bipolar rails for the LMP91200, CD4060, and LM324)
```

The analog rails are isolated from the digital 3.3 V rail to keep
switching noise out of the sensor front-end.

AMS1117 **idle / LoRa TX current bands below are ESTIMATES**, not metered
values. See [BUILD provisional engineering estimates](#build-provisional-engineering-estimates-2026-09-04).

## Pinout

The active device-tree pin assignments are documented in
[`config/pinmap.yaml`](../config/pinmap.yaml) and match the
constants in `src/utils/config.py`.

## BUILD provisional engineering estimates (2026-09-04)

> **ESTIMATE / PROVISIONAL / pending scope or meter.**
> Every number in this section is an engineering stack-up, **not** a
> measured bench value. Do not treat them as production calibration,
> nameplate current, or firmware constants. Jacques approved recording
> these BUILD notes on 2026-09-04. Replace them when Q4 / VIN0 / rail
> current are benched; the HL-A250L-38 nameplate is still pending from
> Haolin. No L/day or AWG nameplate figures are recorded here.

### 1. Q4 @ U5 (CD4060) pin 7 — TDS excitation

Fin_3 RTC timing uses **R10 = 10 kΩ + C60 = 1 nF**. **R9 = 100 kΩ** is
the companion oscillator resistor, not the Q4 period-setter by itself.

| Quantity | ESTIMATE |
|----------|----------|
| f_Q4 | **≈ 2.8 kHz ± 25%** |
| Band | **2.1–3.5 kHz** |

Firmware does **not** drive this excitation. v2.1.1 stays sampling-only
on the TDS chain (`src/sensors/tds.py` reads ADS1115 AIN0).

### 2. VIN0 / TDS cal (AIN0)

The live analog path is the **LM324 rectifier + R47/R48 LPF** into
ADS1115 AIN0 (silk **0–2.3 V**). Older comments that named an **R57/R58
0.3125 hardware divider** were a **firmware comment bug** — that is not
the live Fin_3 divider. Firmware 2.1.1 still applies
`TDS_DIVIDER_RATIO` (0.3125) as a **software scale only**; that
constant is unchanged and is not a schematic claim.

| Item | Status |
|------|--------|
| Default ~500 ppm/V | **PROVISIONAL** starting coefficient |
| Primary one-point check | **1413 µS/cm** |
| Absolute V/ppm | **Unknown** until cell constant K — order-of-magnitude only |
| VIN0 @ 1413 µS/cm | **ESTIMATE ~0.3–1.5 V / ~5k–24k LSB** (FSR ±2.048 V); cell K unknown |

Do not hard-code fake cal tables as production truth. Field cal stays
the one-point wizard in [sensor-calibration.md](sensor-calibration.md).

### 3. 3.3 V AMS1117 rail

Loads on this rail: **PCA9306s, SX1262, GPS, LED**. CD4060 and LM324
sit on the **±3.0 V analog** rails, not this regulator.

| Condition | ESTIMATE band |
|-----------|----------------|
| Idle | **~25–60 mA** |
| LoRa TX @ 14 dBm | **~50–100 mA** |
| LoRa TX @ 22 dBm (max PA) | **~100–180 mA** |

**ESTIMATE stack-up, not metered.**
