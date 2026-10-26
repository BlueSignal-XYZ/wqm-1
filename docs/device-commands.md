# Device commands (firmware)

Wire formats for Cloud commissioning commands. The unit pulls work on the
existing `command_poll_s` tick and posts one result per command. Server and
firmware tests must match this page.

The live unit `BS-WQM1-0000a92e4e7d` runs older firmware. These routes are
additive: ingest, `GET .../config`, and the legacy `deviceCommands` poll are
unchanged. Unverified on hardware.

Owner-only on the Cloud side. The unit authenticates with its device key.

## Pending

`GET {cloud_api_base}/v2/devices/{id}/commands/pending`

Header: `X-API-Key` (device key). No body.

```json
{
  "commands": [
    {
      "id": "cmd-1",
      "type": "ph_calibrate",
      "payload": {},
      "expiresAt": 1700000060000
    }
  ]
}
```

`expiresAt` is Unix time in milliseconds. The unit rejects an expired command
and still posts `result`.

Allowed `type` values: `ph_calibrate`, `rs485_config`. Anything else is
`rejected` with message `unknown type`. Extra fields on the command or the
payload, and non-finite numbers, are rejected with a plain message.

## `ph_calibrate`

```json
{
  "step": "start",
  "probe": "analog",
  "buffer": 4.0
}
```

| Field | Required | Values |
|---|---|---|
| `step` | always | `start`, `point`, `finish` |
| `probe` | always | `analog`, `rs485_multi` |
| `buffer` | `point` only | see below |

`buffer` is rejected on `start` and `finish`.

Analog (BNC / `calibrate_ph`): **4.0** and **7.0** only.

Honde RD-PETSTS-01 (`calibrate_ph_point`): **4.01**, **6.86**, and **9.18** only.

`10.0` is rejected. A buffer from the other probe's set is rejected.

`start` clears the in-memory session. `point` reads the converter voltage
(analog) or writes the buffer register (5-in-1). `finish` on analog calls
`CalibrationManager.calibrate_ph` and pushes the slope into the live pH
driver.

## `rs485_config`

```json
{
  "op": "add",
  "sensor": "HondeChlorineSensor",
  "address": 5,
  "newAddress": 7,
  "enabled": true
}
```

| Field | Used by |
|---|---|
| `op` | `add`, `set_address`, `enable`, `disable` |
| `sensor` | always |
| `address` | `add` and `set_address` (current); optional on enable/disable |
| `newAddress` | `set_address` |
| `enabled` | `enable` (`true`), `disable` (`false`) |

Addresses are 1–247. Allowed `sensor` values, imported from the drivers:

- residual chlorine: `HondeChlorineSensor`
- ORP RD-ORP-WE-01: `HondeOrpSensor`
- 5-in-1 RD-PETSTS-01: `HondeMultiSensor`

Any other model (including the flow meter) is rejected.

## Result

`POST {cloud_api_base}/v2/devices/{id}/commands/{cmdId}/result`

Header: `X-API-Key` (device key).

```json
{
  "status": "applied",
  "result": {
    "slope": 5.0,
    "offset": -1.0,
    "buffers": [4.0, 7.0],
    "measuredAt": "2026-10-26T14:00:00Z"
  },
  "message": "pH calibration applied"
}
```

| Field | Notes |
|---|---|
| `status` | `applied` or `rejected` |
| `result` | omitted on most rejections |
| `message` | at most 200 characters, plain words |

Analog pH `offset` is the intercept so `pH = slope * V + offset`
(`7.0 - slope * V@7`). RS485 pH `finish` may send `slope` and `offset` as
null; the coefficients live on the probe.

An RS485 result is:

```json
{
  "status": "applied",
  "result": {
    "sensors": [
      {"sensor": "HondeChlorineSensor", "address": 5, "enabled": true}
    ]
  },
  "message": "RS485 sensor added"
}
```

The pending dispatcher has no path to a coil, a smart breaker, or a host
pin. Legacy `deviceCommands` types (`relay`, `awg`, …) stay on the existing
poll and are unknown types on this route.
