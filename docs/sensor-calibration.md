# Sensor Calibration

All calibration is done from the **Service Window** (the on-device web UI at
`http://<unit-ip>:8080`) with guided wizards. Each wizard watches for a stable
reading, applies the calibration, and stamps `calibrated_at` so the firmware's
drift tracking can nag you when a probe is due.

Calibrating drifted or aged probes matters: the firmware detects drift and
flags "recalibrate soon", but it never auto-corrects — a correction without a
reference solution would corrupt the data record. The human recalibrates.

## Analog probes (Raspberry Pi hosts)

| Probe | Method | Cadence | Consumable life |
|---|---|---|---|
| pH (BNC) | Two-point: rinse in distilled water, stabilize in pH 7.00 (point 1), rinse, stabilize in pH 4.00 or 10.00 (point 2). Verify 6.95–7.05 back in 7.00. | 30 days (or on drift alert) | Electrode 12–18 months |
| TDS | Single-point in a conductivity standard (1413 µS/cm typical); set the point. Default ~500 ppm/V is **PROVISIONAL** — see below. | 180 days | Clean at every visit |
| Turbidity | Zero in clean water; for compliance sites cross-check a handheld turbidimeter and record both values. | 90 days | Wipe optics at every visit |
| Temperature (DS18B20) | Factory calibrated — no field calibration. | — | 5+ years |

> Analog ORP (AIN3) is non-functional on PCBA rev Fin_3. Use the digital RS485
> ORP probe below.

### Analog TDS — BUILD provisional scale (2026-09-04)

> **ESTIMATE / PROVISIONAL / pending cell constant and meter.**
> These are **not** measured bench values. Firmware 2.1.1 samples AIN0
> only; it does not drive CD4060 excitation.

The analog TDS one-point wizard uses a conductivity standard
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

## RS485 digital probes (Honde Tech)

The digital probes hold their own calibration state on the probe. Calibrate
through **Service Window → RS485 Sensors → Calibrate**.

| Probe | Method | Cadence |
|---|---|---|
| Residual chlorine | Guided zero/slope wizard in a flow cell (15–30 L/h) against a lab-verified sample. Valid pH 5–9. Activate a new probe in 3M KCL before first use. | Every 1–2 months |
| 5-in-1 pH | Guided buffer wizard: 4.01 / 6.86 / 9.18 after stability in each buffer. | Quarterly, or on drift alert |
| 5-in-1 EC / conductivity | Slope against a known-conductivity solution; EC, TDS, and salinity all derive from this one electrode. | Quarterly, or on drift alert |
| Digital ORP (RD-ORP-WE-01) | Factory calibrated. | Replace electrode per vendor guidance |

## Drift & health

Every calibration re-anchors the drift baseline. Between calibrations the
firmware watches the long-window median against that baseline and raises a
`calibration_drift` event — surfaced on the dashboard and the Service Window
status page as a plain-language "recalibrate soon" — before readings go
meaningfully wrong. A flatlined or disconnected probe is reported as a fault
(not fake data) and its relay rules are suspended until it recovers.

See also: [getting-started.md](getting-started.md) (RS485 wiring & addresses),
[firmware-overview.md](firmware-overview.md) (sensing architecture),
[hardware-overview.md](hardware-overview.md) (TDS excitation / VIN0 / AMS1117
BUILD estimates).
