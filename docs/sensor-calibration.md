# Sensor Calibration

Calibration and RS485 sensor setup for a claimed unit are done in
**BlueSignal Cloud**, by the **owner**, through device commands. The local
unit page is off after #157; there is no installer wizard on the LAN.

The command formats are in [device-commands.md](device-commands.md). This
page is the field procedure. **Unverified on hardware** until the Captain
benches it. Do not treat anything below as a bench result.

> **Enter the converter voltage, not a corrected one.** Analog pH `point`
> reads what the ADC sees. The unit applies the divider and temperature
> compensation itself. A compensated or handheld-corrected number is not an
> input.
>
> Until firmware 2.1.2 nothing in the product displayed a voltage, so
> analog calibration was effectively impossible in the field and units ran
> on factory nominals. A nominal reading looks exactly like a calibrated
> one on the dashboard: check the absence of a `calibrated_at` stamp, or
> the absence of `/etc/bluesignal/calibration.yaml`.

The analog read still comes from the firmware daemon, which owns the I²C
bus. The ADS1115 has one conversion register shared by all four channels,
so a second process opening the bus can return one channel's voltage under
another's name with nothing raised and nothing logged. Cloud commands ask
the daemon; they do not open the bus themselves.

Calibrating drifted or aged probes matters: the firmware detects drift and
flags "recalibrate soon", but it never auto-corrects — a correction without
a reference solution would corrupt the data record. The owner recalibrates
from Cloud. Each applied pH calibration stamps `calibrated_at`, so drift
tracking can nag when a probe is due.

## Analog pH (Raspberry Pi hosts)

Two-point only: **4.0** then **7.0**. Cloud `ph_calibrate` with
`probe: analog`. The unit rejects **10.0** and the RD-PETSTS-01 buffer set
(4.01 / 6.86 / 9.18).

1. Rinse in distilled water. Put the electrode in pH 4.0 buffer. Queue
   `start`, then `point` with `buffer` 4.0, and wait for `applied`. Each
   analog `point` is the median of 5 converter reads. If the spread is
   above 0.02 V the unit rejects with `voltage not stable`; leave the
   electrode still and queue `point` again.
2. Rinse. Put the electrode in pH 7.0 buffer. Queue `point` with
   `buffer` 7.0.
3. Queue `finish`. Cloud should show slope, offset, the two buffers, and
   `measuredAt`. A rejected result is the unit's message in words.

Cadence: 30 days, or on a drift alert. Electrode life is typically 12–18
months.

Verify 6.95–7.05 back in 7.0 after `finish`. That check is unverified on
hardware.

> Analog ORP (AIN3) is non-functional on PCBA rev Fin_3 — the pin is spare,
> with no ORP circuit behind it. Use the digital RS485 ORP probe below.

## Analog TDS and turbidity

Cloud commands for TDS and turbidity are not in this firmware change.
Until they ship, BlueSignal staff set those coefficients on the bench.
Owners do not open a local page to do it. Unverified on hardware.

The stored coefficients still work the way they did:

**TDS** is a single-point in a conductivity standard (1413 µS/cm typical).
Default ~500 ppm/V is **PROVISIONAL** — see below. Cadence 180 days. Clean
at every visit.

**If TDS reads far lower than the water should:** calibration is the test,
not the fix. Put the probe in a known standard. If the converter voltage
barely moves between air and solution the probe is not conducting — check
full immersion, fouling, and wiring — and do not solve a `k` from a dead
probe. A `k` derived that way makes every later reading wrong without ever
looking wrong.

**Turbidity** is a zero in clean water; for compliance sites cross-check a
handheld turbidimeter and record both values. Cadence 90 days. Wipe optics
at every visit.

**Temperature (DS18B20)** is factory calibrated — no field calibration.

### Analog TDS — BUILD provisional scale (2026-09-04)

> **ESTIMATE / PROVISIONAL / pending cell constant and meter.**
> These are **not** measured bench values. Firmware 2.1.1 samples AIN0
> only; it does not drive CD4060 excitation.

The analog TDS one-point uses a conductivity standard
(**1413 µS/cm** typical). The default **~500 ppm/V** coefficient is a
provisional starting point. Absolute V/ppm is unknown until the cell
constant is known — **order-of-magnitude only**.

At 1413 µS/cm, VIN0 is **ESTIMATED ~0.3–1.5 V (~5k–24k LSB at FSR
±2.048 V)**; cell K unknown. Do not treat that band, or the 500 ppm/V
default, as production truth, and do not invent cal tables.

The live Fin_3 path into AIN0 is the **LM324 rectifier + R47/R48 LPF**
(silk 0–2.3 V), not an R57/R58 0.3125 hardware divider. Q4 excitation
and AMS1117 current bands are in
[hardware-overview.md](hardware-overview.md#build-provisional-engineering-estimates-2026-09-04).
Replace these notes when Jacques benches Q4 / VIN0 / current; the
HL-A250L-38 nameplate is still pending from Haolin.

## RS485 digital probes

The digital probes hold their own calibration state on the probe. The
owner adds, re-addresses, enables, and disables them from Cloud with
`rs485_config`. Cloud wire names only: `residual_chlorine`,
`RD-ORP-WE-01`, and `RD-PETSTS-01`. Anything else is rejected with
`sensor not allowed`. Addresses are 1–247. `add` and `enable` check
that the serial port opens; they do not confirm a probe answers. After
adding, confirm a live reading on the dashboard before you leave.

| Probe | Method | Cadence |
|---|---|---|
| Residual chlorine | Zero/slope in a flow cell (15–30 L/h) against a lab-verified sample. Valid pH 5–9. Activate a new probe in 3M KCL before first use. Cloud does not yet send those cal steps — only add / address / enable. Unverified on hardware. | Every 1–2 months |
| 5-in-1 pH (RD-PETSTS-01) | Cloud `ph_calibrate` with `probe: rs485_multi`. Buffers **4.01**, **6.86**, and **9.18** only, after the reading is stable in each. The unit rejects 4.0, 7.0, and 10.0. | Quarterly, or on drift alert |
| 5-in-1 EC / conductivity | Slope against a known-conductivity solution; EC, TDS, and salinity all derive from this one electrode. Not a Cloud command in this change. Unverified on hardware. | Quarterly, or on drift alert |
| Digital ORP (RD-ORP-WE-01) | Factory calibrated. | Replace electrode per vendor guidance |

pH on the 5-in-1: `start`, then one `point` per buffer, then `finish`.
Wait for a stable reading before each `point`. The unit writes the
buffer register and does not re-check stability. Cloud shows the
buffers and `measuredAt`. Slope and offset stay on the probe.

## Drift & health

Every calibration re-anchors the drift baseline. Between calibrations the
firmware watches the long-window median against that baseline and raises a
`calibration_drift` event — surfaced on the Cloud dashboard as a
plain-language "recalibrate soon" — before readings go meaningfully wrong.
A flatlined or disconnected probe is reported as a fault (not fake data)
and its relay rules are suspended until it recovers.

See also: [device-commands.md](device-commands.md) (wire formats),
[getting-started.md](getting-started.md) (RS485 wiring & addresses),
[firmware-overview.md](firmware-overview.md) (sensing architecture),
[hardware-overview.md](hardware-overview.md) (TDS excitation / VIN0 / AMS1117
BUILD estimates).
