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

- the NetworkManager connection `wqm1-setup-ap` was not active
- the ADS1115 did not answer on I2C
- `vcgencmd get_throttled` showed a current undervoltage, frequency cap,
  or throttle (a since-boot flag alone does not fail)
- `bluesignal-wqm`, `bluesignal-service-window`, `bluesignal-ap-fallback`,
  or NetworkManager was not active, or its restart count climbed during
  the ten minutes

The same boot also writes `wqm1-status.txt`. Its first line is
`COMMISSION: READY` or `COMMISSION: FAIL: <reason>`, including the last
hotspot error when the AP did not come up. Neither file contains the
hotspot passphrase.

## Where it is installed

`setup.sh` enables `bluesignal-burnin.service`. The unit stays installed
and does not start unless `/boot/firmware/burnin` or `/boot/burnin`
exists. The script is `scripts/burn-in.sh`.
