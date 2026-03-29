[![CERN-OHL-S-2.0](https://img.shields.io/badge/Hardware_License-CERN--OHL--S--2.0-blue)](LICENSE-HARDWARE)
[![GPL-3.0](https://img.shields.io/badge/Firmware_License-GPL--3.0-blue)](LICENSE-FIRMWARE)
[![Made with KiCad](https://img.shields.io/badge/Made_with-KiCad-orange)](https://www.kicad.org/)

# WQM-1 — Open Source Water Quality Monitor

> Six-channel water quality monitoring on a Raspberry Pi Zero 2W. Open hardware. Open firmware. Built by [BlueSignal](https://bluesignal.xyz).

## What It Is

The WQM-1 is a Raspberry Pi Zero 2W HAT (65 × 56.5 mm) designed for continuous, autonomous water quality monitoring. It combines precision analog sensing with long-range wireless connectivity in a compact, field-deployable package.

**Key hardware:**

- **Dual ADS1115** 16-bit ADCs (I²C) — six differential analog channels
- **SX1262 LoRa radio** — up to 15 km range via LoRaWAN
- **u-blox GPS** — georeferenced readings out of the box
- **MP1584EN buck converter** — wide-input power supply (7–28 V)
- **10 A relay output** — control dosing pumps, aerators, or valves

**Monitored parameters:**

- pH
- TDS (Total Dissolved Solids)
- Turbidity
- ORP (Oxidation-Reduction Potential)
- Temperature
- GPS location

**Data pipeline:**

Sensors → ADS1115 (I²C) → Pi Zero 2W → SQLite WAL buffer → LoRaWAN (Cayenne LPP, AES-128 encrypted)

## Applications

- **Aquaculture** — real-time pond and tank monitoring
- **Algae control** — early detection of harmful algal blooms
- **Stormwater MS4 compliance** — automated NPDES permit reporting
- **Residential well & cistern monitoring** — peace of mind for private water supplies
- **Environmental research** — low-cost, distributed sensor networks

## Buy or Build

You can build the WQM-1 yourself from the files in this repository, or buy it assembled, tested, and provisioned from [bluesignal.xyz](https://bluesignal.xyz).

The dev kit ships with cloud monitoring via [cloud.bluesignal.xyz](https://cloud.bluesignal.xyz) and optional integration with the [WaterQuality.Trading](https://waterquality.trading) marketplace.

## Repository Layout

```
wqm-1/
├── hardware/
│   ├── bom/        # BOM exports
│   └── fab/        # Gerbers and fab outputs
├── firmware/
│   ├── src/        # Firmware source
│   ├── lib/        # Shared libraries (ADS1115, SX1262, etc.)
│   └── config/     # Example config files (no real keys)
├── docs/           # Project documentation
├── images/         # Board photos and diagrams
└── .github/        # Issue templates
```

## Getting Started

See [docs/getting-started.md](docs/getting-started.md) for build and setup instructions.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

This project uses a **dual license** model:

| Component | License | File |
|-----------|---------|------|
| Hardware (schematics, PCB, BOM) | [CERN Open Hardware Licence v2 — Strongly Reciprocal](https://ohwr.org/cern_ohl_s_v2.txt) | [LICENSE-HARDWARE](LICENSE-HARDWARE) |
| Firmware (source code, scripts) | [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.en.html) | [LICENSE-FIRMWARE](LICENSE-FIRMWARE) |

**BlueSignal** builds the hardware. [**WaterQuality.Trading**](https://waterquality.trading) is the marketplace.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md).
