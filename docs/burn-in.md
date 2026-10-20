# Bench burn-in

A ten-minute check that the setup hotspot stays up on the HAT. It does not
run on a normal boot. The Captain reads the result from the card; SSH is
not required.

## Run it

1. Power the unit off and pull the card.
2. On the FAT boot partition (the one a Mac mounts), create an empty file
   named `burnin`. No extension.
3. Put the card back, seat the Pi on the HAT, and power it.
4. Wait at least 12 minutes. The check starts after the setup hotspot
   service and then samples every 30 seconds for 10 minutes.
5. Pull the card again and open `wqm1-burnin.txt` on the same partition.

`burnin` is deleted at the start of the run, so the next boot does not
spend another ten minutes unless you drop the file again. If the file
still says the run was in progress, or there is no `wqm1-burnin.txt`, the
unit lost power mid-check: drop `burnin` and boot once more.

Do this before the unit has joined a site network. The check fails when
the setup hotspot is not active. A saved bench network that associates
will bring the hotspot down, and the result will say so.

## What a pass looks like

```
BURN-IN: PASS
21 checks, hotspot stayed up, adc answered, no undervoltage
```

`FAIL` names the check number and which of these broke:

- hostapd was not beaconing (`hostapd_cli status` did not report
  `state=ENABLED`)
- the ADS1115 did not answer on I2C
- `vcgencmd get_throttled` (or the firmware sysfs node) showed
  undervoltage now or since this boot. The result says
  `weak power supply or cable` and appends `undervoltage seen (N)`.
  The same count is written to `wqm1-status.txt` as
  `power: undervoltage seen (N)`, and that boot's `COMMISSION` line
  fails with `weak power supply or cable`. A since-boot frequency cap
  with no undervoltage does not fail. A current throttle that is not
  undervoltage still fails.
- `bluesignal-wqm`, `bluesignal-service-window`, `bluesignal-ap-fallback`,
  or NetworkManager was not active, or its restart count climbed during
  the ten minutes

The same boot also writes `wqm1-status.txt`. Its first line is
`COMMISSION: READY` or `COMMISSION: FAIL: <reason>`, including the last
hotspot error when the AP did not come up. Neither file contains the
hotspot passphrase.

## Hotspot label

Print the setup-hotspot label on the bench computer, from the Pi serial,
before the unit ships. The installer scans it to join `WQM1-xxxx`.

```bash
python3 scripts/make-unit-label.py <pi-serial> --out-dir ./labels
```

The serial is the `Serial` line in `/proc/cpuinfo` (16 hex digits). The
script writes `<SSID>-hotspot-label.svg` and `<SSID>-hotspot-label.pdf`:
the SSID, the derived passphrase, and a Wi-Fi QR code
(`WIFI:T:WPA;S:<ssid>;P:<pass>;;`).

It prints the two file paths and nothing else. It does not write the
passphrase into `wqm1-status.txt` or any firmware log. Do not run it on a
field unit as part of boot. Details for the installer are in
[`installer-mistakes.md`](installer-mistakes.md).

## Where it is installed

`setup.sh` enables `bluesignal-burnin.service`. The unit stays installed
and does not start unless `/boot/firmware/burnin` or `/boot/burnin`
exists. The script is `scripts/burn-in.sh`.
