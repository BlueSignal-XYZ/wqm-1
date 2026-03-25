# Contributing to WQM-1

Thank you for your interest in contributing to WQM-1! This project is open hardware and open firmware — community contributions help make water quality monitoring accessible to everyone.

## Where Things Go

- **Hardware contributions** (schematics, PCB changes, BOM updates) go in `/hardware`.
- **Firmware contributions** (drivers, sensor logic, radio code) go in `/firmware`.
- **Documentation** goes in `/docs`.

## Out of Scope

The following are **not** part of this repository and PRs touching these areas will be closed:

- Cloud platform code (cloud.bluesignal.xyz)
- Device provisioning systems
- OTA update infrastructure
- Marketplace integration (WaterQuality.Trading)

These are maintained separately by BlueSignal.

## Requirements

### Firmware

- All firmware PRs must **compile cleanly** for the Raspberry Pi Zero 2W (ARM).
- Include a brief description of what you tested and how.
- Do not commit real API keys, LoRa session keys, or other secrets. Use the example config files in `firmware/config/`.

### Hardware

- Use **KiCad 8.x** for all hardware contributions.
- Include updated BOM exports when component changes are made.
- Follow the existing schematic and layout conventions.

## Commit Style

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(firmware): add turbidity sensor averaging
fix(hardware): correct ADS1115 I2C address on sheet 2
docs: update getting-started guide for v1.2
```

## Pull Request Process

1. Fork the repository and create a branch from `main`.
2. Make your changes and ensure they meet the requirements above.
3. Open a pull request with a clear description of the change and its motivation.
4. Respond to review feedback promptly.

## Code of Conduct

This project follows the [Contributor Covenant v2.1](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
