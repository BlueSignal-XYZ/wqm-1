# Apple Home

The WQM-1 can show its readings in the Home app on an iPhone, an iPad, a
HomePod and — from 2026-11-13 — Apple HomeView, Apple's smart-home display.
HomeView runs Apple's own apps only; the one door a third-party product has
onto it is the Home app's accessory list, and that list is HomeKit- or
Matter-enabled accessories. So the unit becomes a HomeKit accessory on the
owner's own Wi-Fi.

Source: `src/integrations/apple_home/`. Off by default. The owner turns it on
from the Service Window (**Apple Home** page) or from the Cloud device page
(`apple_home_enabled`), pairs by scanning the code the Service Window shows,
and the Home app lists the unit from then on.

## What the Home app shows

| Service | HomeKit type | Value |
|---|---|---|
| Water temperature | TemperatureSensor | the last `temp_c` the unit stored; StatusActive false when the probe gave no number this cycle; StatusFault when the monitor has suspended the probe |
| Irrigation hold | ContactSensor | the hold relay's contact: **open = holding**, closed = clear. StatusActive mirrors `irrigation_hold_enabled`; StatusFault mirrors the hold's probe-fault state |

pH, TDS, turbidity and flow rate travel on the temperature service as custom
characteristics (stable UUIDs in a BlueSignal namespace). Apple's Home app
does not display custom characteristics; third-party HomeKit apps such as
Eve or Home+ show them by name. The bluesignal.xyz page says so in as many
words — the Home app sees temperature and the hold's contact, nothing more.

"Irrigation hold" describes the relay contact, never the water. Nothing in
this package maps a reading onto a LeakSensor, an AirQuality scale or any
other verdict Apple's UI would render as a judgement. The accessory name is
`WQM-1 <last four of the label>`; a banned-word list keeps *safe*, *clean*,
*leak*, *alarm* and the rest out of every name the Home app could print.

## Readings out, nothing in

The accessory carries no writable characteristic. No relay, setting or hold
can be driven from the Home app, from Siri, or from a HomeKit automation.
The read door cannot act — the same rule as the smart-breaker integration.

## Settings

| Key | Default | Hot | Remote | Notes |
|---|---|---|---|---|
| `apple_home_enabled` | `false` | yes | yes | start / stop the accessory on the next cycle |
| `apple_home_port` | `51826` | no | **no** | HAP listen port; a remote config cannot move a LAN listener |

The pairing state and the pincode persist in `apple-home.state` beside the
database. Deleting the file means re-pairing.

## Where the setup code may go

- The Service Window's **Apple Home** page (login required, LAN only) shows
  the code and a QR until the unit is paired, and hides both after.
- The `apple_home_status` command (Unix socket, local) returns them for that
  page and for nothing else.
- The cloud heartbeat carries `appleHome: {enabled, paired}` and **nothing
  else** — no code, no URI, no name. The cloud cannot pair and never sees
  what would let it.

## Certification, stated plainly

Apple publishes the HomeKit Accessory Protocol for non-commercial use;
products sold with it built in belong in Apple's MFi program, and a
Matter-certified product may carry the "Works with Apple Home" badge.
BlueSignal has neither today, so:

- the Home app shows an **Uncertified Accessory** notice at pairing time
  (the same one every Homebridge user taps through — *Add Anyway*);
- the feature ships **off**, on an open-source firmware the owner runs, and
  the owner enables it;
- no BlueSignal page, label or message may say "Works with Apple Home",
  "HomeKit-certified", "MFi" or "Matter". The marketing rule and the
  certification path (Matter bridge → CSA certification) are in
  `marketplace/docs/operations/apple-homeview-launch.md`.

## Bench test

1. `apple_home_enabled: true` in the Service Window → Apple Home.
2. Confirm the page reads *advertising on port 51826* and shows a code.
3. On an iPhone on the same Wi-Fi: Home → + → Add Accessory → scan → *Add
   Anyway*. The unit appears as `WQM-1 xxxx` with a temperature tile.
4. Warm the temperature probe; the tile moves within one sample interval.
5. Enable the irrigation hold with a trip the simulator can drive
   (`turbidity:spike@N`); the contact sensor reads *Open* while holding.
6. Reboot the unit: the accessory re-advertises and the iPhone reconnects
   without re-pairing (the state file survived).
7. On HomeView, once one is on the bench: the same tiles on the Home face.
   **No HomeView has been paired yet** — that bench pass is what gates the
   bluesignal.xyz page's publish flag.

## Not built

- Control (relay as a Switch, the hold as a Valve): deliberately. A write
  door needs the rules-engine guards re-derived for HomeKit automations.
- Matter. The certification route; a separate bridge, after the first
  HomeKit pairing is observed in the field.
- Per-channel HomeKit alerts for pH / TDS / turbidity thresholds: a
  threshold on Apple's screen is a verdict by another name; readings only.
