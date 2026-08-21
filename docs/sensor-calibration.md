# Sensor Calibration

All calibration is done from the **Service Window** (the on-device web UI at
`http://<unit-ip>:8080`, under **Calibration**) with guided wizards. Each wizard
shows a **live voltage** for its channel, refreshed every two seconds, with a
*Use this value* button that fills the form. Save applies the calibration and
stamps `calibrated_at`, so the firmware's drift tracking can nag you when a
probe is due.

> **Enter the converter voltage, not a corrected one.** The number the wizard
> displays and fills is what the ADC sees; the unit applies the divider and
> temperature compensation itself. The TDS page additionally *reports* the
> compensated probe voltage — that one is information, never an input.
>
> Until firmware 2.1.2 the wizards asked you to "read the voltage" and nothing
> in the product displayed one, so calibration was effectively impossible in the
> field and units ran on factory nominals. A nominal reading looks exactly like
> a calibrated one on the dashboard: check **Never calibrated on this unit** on
> the Calibration index, or the absence of `/etc/bluesignal/calibration.yaml`.

The live reading comes from the firmware daemon over its command socket, not
from the Service Window opening the I²C bus. That boundary is deliberate: the
ADS1115 has one conversion register shared by all four channels, so two
independent readers can return one channel's voltage under another's name with
nothing raised and nothing logged.

Calibrating drifted or aged probes matters: the firmware detects drift and
flags "recalibrate soon", but it never auto-corrects — a correction without a
reference solution would corrupt the data record. The human recalibrates.

## Analog probes (Raspberry Pi hosts)

| Probe | Method | Cadence | Consumable life |
|---|---|---|---|
| pH (BNC) | Two-point: rinse in distilled water, stabilize in pH 7.00 (point 1), rinse, stabilize in pH 4.00 or 10.00 (point 2). Verify 6.95–7.05 back in 7.00. | 30 days (or on drift alert) | Electrode 12–18 months |
| TDS | Single-point in a conductivity standard (1413 µS/cm typical); set the point. | 180 days | Clean at every visit |
| Turbidity | Zero in clean water; for compliance sites cross-check a handheld turbidimeter and record both values. | 90 days | Wipe optics at every visit |
| Temperature (DS18B20) | Factory calibrated — no field calibration. | — | 5+ years |

> Analog ORP (AIN3) is non-functional on PCBA rev Fin_3 — the pin is spare,
> with no ORP circuit behind it, so the Service Window's ORP page cannot
> calibrate a base-unit channel and says so. Use the digital RS485 ORP probe
> below.

**If TDS reads far lower than the water should:** calibration is the test, not
the fix. Put the probe in a known standard and watch the live voltage. If it
barely moves between air and solution the probe is not conducting — check full
immersion, fouling, and wiring — and the wizard will refuse to solve a `k` from
it, because a `k` derived from a dead probe makes every later reading wrong
without ever looking wrong.

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
[firmware-overview.md](firmware-overview.md) (sensing architecture).
