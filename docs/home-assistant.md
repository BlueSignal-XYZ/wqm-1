# Home Assistant (and, through it, Google Home, Alexa and SmartThings)

The WQM-1 can publish its readings to the owner's own MQTT broker in the
format Home Assistant discovers automatically. Nothing is installed in Home
Assistant; the unit announces itself and the entities appear under
**Settings → Devices → MQTT**.

This is the second of the unit's two smart-home doors. The first is the
Apple Home accessory (`docs/apple-home.md`), which speaks HomeKit on the LAN.
Both are off by default, both send readings out and take nothing in, and
neither is a "Works with …" program: BlueSignal is in no Apple, Google,
Amazon or Samsung certification program and claims no badge.

## What Home Assistant sees

One device, `WQM-1 <last four of the label>`, with these entities:

| Entity              | Reading column      | Unit    | HA class                     |
| ------------------- | ------------------- | ------- | ---------------------------- |
| Water temperature   | `temp_c`            | °C      | temperature / measurement    |
| pH                  | `ph`                | pH      | ph / measurement             |
| TDS                 | `tds_ppm`           | ppm     | measurement                  |
| Turbidity           | `turbidity_ntu`     | NTU     | measurement                  |
| ORP                 | `orp_mv`            | mV      | measurement                  |
| Residual chlorine   | `chlorine_mgl`      | mg/L    | measurement                  |
| Conductivity        | `conductivity_uscm` | µS/cm   | measurement                  |
| Salinity            | `salinity_ppt`      | ppt     | measurement                  |
| Flow rate           | `flow_rate_gpm`     | gal/min | volume_flow_rate / measurement |
| Water produced      | `flow_total_gal`    | gal     | water / total_increasing     |
| Irrigation hold     | the relay contact   | on/off  | binary sensor                |

Every entity is a measurement with its unit. There is no "water quality"
entity, no score and no verdict — a dashboard that wants a colour writes its
own automation against a number it chose, and that is the owner's call, not
BlueSignal's. "Irrigation hold" is the relay contact (on = holding), never
the water.

A probe the unit has suspended is left out of the next state document, so its
entity reads **unknown** rather than the last number it saw. The device
goes **unavailable** when the unit disconnects (the broker sends the retained
`offline` will).

Through Home Assistant the same entities reach Google Home, Alexa and
SmartThings — Nabu Casa's cloud does it in one switch, or a self-hosted
bridge does the same. That is Home Assistant's feature and its badge; the
WQM-1 does not talk to Google or Amazon.

## Readings out, nothing in

The unit **subscribes to no topic**. There is no command topic, no `set`
topic, and the adapter has no method that could receive a message. No relay,
setting or hold can be driven over MQTT. `tests/test_home_assistant.py`
scans the module for a subscribe call and fails if one appears.

## Settings

| Key                     | Default         | Hot | Remote | Note                                                |
| ----------------------- | --------------- | --- | ------ | --------------------------------------------------- |
| `mqtt_enabled`          | `false`         | yes | yes    | the one switch; the Cloud device page offers it     |
| `mqtt_host`             | `""`            | yes | no     | broker address; the Service Window writes it        |
| `mqtt_port`             | `0`             | yes | no     | 0 = 1883 (8883 with TLS)                            |
| `mqtt_username`         | `""`            | yes | no     |                                                     |
| `mqtt_password`         | `""`            | yes | no     | stored on the unit, write-only in the Service Window |
| `mqtt_tls`              | `false`         | yes | no     |                                                     |
| `mqtt_discovery_prefix` | `homeassistant` | yes | no     | Home Assistant's default                            |

A remote (cloud) config can turn the feature on or off and can do nothing
else: it can never point the unit at a broker or hand it a credential. With
`mqtt_enabled` on and no host set the unit reports `no_host` and connects to
nothing.

## Topics

- Discovery: `<prefix>/sensor/wqm1_<label>/<entity>/config` and
  `<prefix>/binary_sensor/wqm1_<label>/irrigation_hold/config`, retained,
  published once per connection.
- State: `bluesignal/wqm1/wqm1_<label>/state`, one JSON document per
  sampling cycle, not retained.
- Availability: `bluesignal/wqm1/wqm1_<label>/availability`, retained
  `online` on connect and `offline` as the will.

Removing the device in Home Assistant clears the retained configs there; the
unit never publishes an empty config on its own.

## Heartbeat

`homeAssistant: {enabled, connected}` and nothing else. No host, no topic, no
credential reaches the cloud. The Cloud device page shows Off / On · not
connected / Connected.

## Set-up

1. In Home Assistant: Settings → Add-ons → Mosquitto broker → start it;
   Settings → People → Users → add a user for the unit (any name).
2. On the unit's Service Window → **Home Assistant**: broker address
   (`homeassistant.local` or the IP), the user, the password, tick
   *Publish to Home Assistant*, Save.
3. In Home Assistant: Settings → Devices & services → MQTT → the device
   appears within one sampling cycle.

## Bench test (not yet run on hardware)

1. Mosquitto on a laptop; `mosquitto_sub -v -t '#'`.
2. Enable on a simulated unit (`scripts/simulate-fleet.py --tier full`) with
   `mqtt_host` pointed at the laptop.
3. Expect eleven retained `config` messages, one `online`, then one `state`
   per cycle.
4. Pull a probe (or `turbidity:fault` in the simulator): the next state
   document omits that key and lists it under `faults`.
5. Stop the unit: `offline` arrives on the availability topic.
6. Point a real Home Assistant at the same broker and confirm the device
   card, then enable Nabu Casa's Google Assistant and confirm "Hey Google,
   what is the water temperature" answers from the entity.

## Not built, named

- A Matter device (the one route to a badge on every hub). It needs a Matter
  SDK on the unit and CSA certification; decide after the HomeView push.
- A direct Google Home or Alexa integration from the unit. Both are cloud
  programs with their own review; the Cloud side carries the Google
  cloud-to-cloud fulfillment (`marketplace/functions/v2/googleHome.js`), and
  Home Assistant covers the rest for now.
- Any topic the unit reads.
