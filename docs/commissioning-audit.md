# Commissioning audit

Reviewed against the 2026-10-18 field logs: a Pi Zero W on BCM43430/1
firmware 7.45.98 (TOB) never kept the setup hotspot up (`nl80211: kernel
reports: key setting validation failed`, `brcmf_vif_set_mgmt_ie: vndr ie
set error : -52`, then NetworkManager `supplicant-timeout`). The same
image on firmware 7.45.96.s1 raised `WQM1-E794`. A card from that second
Pi still booted on the first as hostname `wqm1-e794` while the SSID
followed serial `86544fd8`.

Ranked by what it does to an installer who is standing at the unit with a
phone. Items under "Fixed in the hotspot pass" landed with the 7.45.98
radio fix. The captive-portal item is fixed in the port-80 change. The
rest are not fixed; each has the smallest change that would close it.
Nothing below was re-checked on a HAT.

## Fixed in the hotspot pass

1. **Hotspot security the 7.45.98 firmware rejects.** `nmcli device wifi
   hotspot` activated NetworkManager's default (WPA+RSN, TKIP allowed, PMF
   left at the default). That default is what produced the vendor-IE and
   key-validation errors, and the SSID then blinked because the oneshot
   exited and systemd restarted it. The profile is now deleted and
   rewritten on every raise, then modified *before* `connection up`:
   `802-11-wireless-security.proto=rsn`, pairwise `ccmp`, group `ccmp`,
   `pmf=1` (disabled), `key-mgmt=wpa-psk`, `802-11-wireless.powersave=2`
   (disabled). An existing profile that `connection add` cannot replace
   is modified in place, so a card already in the field picks this up at
   the next boot. SSID and passphrase derivation are unchanged
   (`WQM1-` + last four of the device id; `ap_passphrase`, else
   `sha256("wqm1-ap:"+pi_serial)[:12]`).

2. **The documented address was not the address the AP used.** The old
   code set `192.168.4.1/24` only after the hotspot had activated, and
   did not activate again. The working log showed `10.42.0.1`. Captive
   DNS and the port-80 NAT rule both match `192.168.4.1`, so a phone
   could associate and still not open the setup page. The address is now
   on the profile before `connection up`.

3. **`StartLimitIntervalSec` was in `[Service]`.** systemd logged
   `Unknown key` and did not apply the cap, so a radio that could not
   come up restarted forever and the SSID blinked. Both
   `StartLimitIntervalSec=600` and `StartLimitBurst=8` are in `[Unit]`.

4. **A failed AP left no reason on the boot partition.** The journal on
   these images was the only copy of `802.1X supplicant took too long to
   authenticate`, and it did not survive a power cycle. `wqm1-status.txt`
   now has `hotspot: failed: <reason>` and the first line
   `COMMISSION: FAIL: <reason>`. The passphrase is stripped before the
   write.

5. **Hostname stuck to the first Pi.** `wqm1-firstboot` set
   `wqm1-xxxx` once and recorded `/var/lib/wqm1-firstboot/identity-done`
   on the card. Moving the card to another Pi kept that hostname while
   `ap_name()` read the new serial, so mDNS said `wqm1-e794` and the
   SSID said `WQM1-4FD8`. Every boot now sets the hostname from the same
   `ap_name()` the SSID uses, and updates it when they differ.

6. **A bad Wi-Fi password restored the hotspot only once.** `join_network`
   now uses the same three attempts as boot before it reports the AP gone.

7. **No bench check and no single verdict.** `wqm1-status.txt` is a
   self-check every boot (board, revision, Wi-Fi chip and firmware,
   kernel, undervoltage, ADC, 1-Wire, GPS, hotspot, portal, SSID, uptime,
   `COMMISSION: READY` or `FAIL`). `scripts/burn-in.sh` runs for ten
   minutes when an empty `burnin` file is on the boot partition and
   writes `wqm1-burnin.txt`. See [burn-in.md](burn-in.md).

## Fixed in the port-80 change

1. **Captive check stayed on port 8080.** The phone's probe hit port 80
   and was redirected to `http://192.168.4.1:8080/setup/`. Some handsets
   treat a captive portal that leaves port 80 as "no internet" and never
   show the page. `bluesignal-captive-portal.service` now binds
   `192.168.4.1:80` as the install user (`CAP_NET_BIND_SERVICE`, not a
   root Flask). Apple, Android, and Windows probe URLs get `302` to
   `http://192.168.4.1/setup/`. A typed `http://` name gets the same
   redirect. Requests for `192.168.4.1` itself are proxied to the Service
   Window, which stays on 8080. Shared-mode dnsmasq still answers every
   name with `192.168.4.1` and sends DHCP option 114 (RFC 8910) for that
   URL. The legacy iptables NAT rule is deleted so it cannot steal port
   80. When the unit joins the site network the listener closes.
   `wqm1-status.txt` has `portal: up (80)` or `portal: down: <reason>`,
   and `COMMISSION: FAIL` when the hotspot is up and port 80 does not
   answer. Not re-checked on a phone.

## Still open

### 1. Undervoltage drops the AP and the ADC together

The Zero W log shows `brcmf_cfg80211_set_power_mgmt: power save enabled`
during probe, and a marginal 5 V rail (HAT + AP) throttles the chip.
The status file and the burn-in now report `vcgencmd get_throttled`.
They do not keep the rail up. Current undervoltage fails the
`COMMISSION` line. A since-boot flag is recorded and does not, by
itself, fail the line.

**Fix:** bench supply at 5.1 V / 2.5 A, and treat `power: now
undervoltage` as a failed board before it ships. No firmware change
holds the AP up through a brownout.

**Impact:** the SSID appears, the phone connects, then both disappear.
Looks like the hotspot-security failure in the hotspot pass and is not.

### 2. Country is forced to US when the regdom is unset

`prepare_wifi_radio` and `wqm1-firstboot` set the regulatory domain to
US when it is empty, `00`, or `UNSET`. They do not overwrite a country
that is already set. A unit whose home AP is on channel 12 or 13 will
not list that network in the setup scan, and the installer cannot join
what the phone can see.

**Fix:** a country field on the network step, written with `iw reg set`
before the scan. Until that exists, set the domain on the card at the
bench for any site outside the US. Do not stop defaulting an unset
domain to US; an unset domain is why `wlan0` stays down.

**Impact:** "my network is not in the list." The hotspot itself is up.

### 3. Pi 3, Pi 4, Pi 5, and the Zero 2 W are unverified with this profile

The security set is the one 7.45.98 requires and 7.45.96.s1 already
accepted a superset of. The CYW43455 (Pi 3B+, 3A+, 4) and the Zero 2 W
module were not booted with PMF forced off. Pi 5's hotspot path is
unverified entirely, and so is the RP1 I2C path the burn-in's ADC check
uses. `docs/raspberry-pi.md` already says the cabled behaviour is
unverified.

**Fix:** one burn-in (`burnin` on the boot partition) per board, read
`wqm1-burnin.txt` and the `wifi:` line of `wqm1-status.txt`. Do not
branch the security profile on the chip unless a board rejects WPA2-PSK
CCMP.

**Impact:** a board we have not sat on the HAT may fail the same way the
7.45.98 Zero W did, or the ADC check may fail on a Pi 5 for a different
reason than a dead converter.

### 4. `wqm1-firstboot` is only on the golden image

`setup.sh` does not install it. A card built by hand keeps whatever
hostname the Imager wrote, which will not track a Pi swap. Golden cards
are the field path, and those now rename themselves every boot.

**Fix:** have `scripts/wqm1-ap-fallback.py` call the same hostname
helper when `/usr/local/sbin/wqm1-firstboot` is absent. Not done here:
the field failure was a golden card, and a second writer of
`/etc/hostname` needs its own test on a non-golden image.

**Impact:** a one-off install, not a golden card. The SSID is still
right; mDNS may not match the label.

### 5. Join can finish before the unit has an address

`station_connected` is NetworkManager state `connected`. That is usually
after DHCP, and it is not a check that the address answers. `_finish_setup`
then forgets every other saved network, including the bench, and the
hotspot is already down. A unit that is "connected" with no route is
reachable only by someone who is already on that L2 network, or by
pulling the card.

**Fix:** in `join_network`, require an IPv4 address on `wlan0` before
returning connected, and do not call `_finish_setup` without one. On
failure the existing restore path brings the AP back.

**Impact:** the installer sees "Setup is finished", goes home, and the
unit is not on the network. The card still has `wqm1-status.txt`.

### 6. Nothing re-raises the hotspot after the fallback service has succeeded

`bluesignal-ap-fallback.service` is a oneshot. It exits 0 when the AP is
up or a station is associated. A later crash of NetworkManager, or a
join whose three restore attempts all fail, leaves the installer with
no SSID until the next boot. `Restart=on-failure` does not run, because
the service already succeeded.

**Fix:** a timer, or a path in the Service Window, that calls `start_ap`
while setup is not finished and no station is connected. The three
retries in `join_network` cover the bad-password case only.

**Impact:** rare next to a radio that never comes up, and the installer has no page to read
the error on. The status file still has the last failure if the boot
path was the one that failed.

### 7. A saved profile whose name is not the SSID survives a bad password

`join_network` deletes a connection named exactly the SSID. `nmcli
device wifi connect` names the profile after the SSID, so the usual
case is covered. A profile that was created under another name for that
same SSID is left in place, and NetworkManager can keep trying it and
refuse to raise the AP.

**Fix:** delete every wireless profile whose `802-11-wireless.ssid`
matches and whose state is not connected, using the same listing
`saved_wifi` already does.

**Impact:** only a card that already has a hand-named profile. Golden
images ship with no Wi-Fi profiles.

### 8. Power save is turned on by the driver before the profile exists

`prepare_wifi_radio` now runs `iw dev wlan0 set power_save off`, and the
AP profile sets `802-11-wireless.powersave=2`. NetworkManager has
historically applied the powersave property more reliably to station
mode than to AP mode. If a bench unit still shows power save on after
the AP is up, the profile property is not the thing holding it off.

**Fix:** confirm with `iw dev wlan0 get power_save` during the burn-in.
If it still says on, call `iw set power_save off` again after
`connection up`. Not done until that reading exists; guessing a second
call is not a fix.

**Impact:** the AP associates and then stalls under load. Looks like a
weak signal.

### 9. GPS and 1-Wire do not gate the verdict

No GPS fix, and no DS18B20, are normal on the bench (indoor, probe not
fitted). The status file reports them (`gps`, `onewire`) and
`COMMISSION` stays `READY` without them. An installer who needed a fix
or a temperature reading can miss that those lines say `no fix` / `no
probe`.

**Fix:** leave the verdict as it is. Call the lines out on the setup
"sensors" step, which already asks what is fitted. Do not fail
commissioning because the bench is indoors.

**Impact:** low. The lines are on the card.

## Not a defect

- AP and station are never up together. `brcmfmac` concurrent AP+STA is
  the failure mode this design refuses. The phone loses the page when
  the join starts; a wrong password brings the hotspot back.
- The station grace period is 45 s only when a Wi-Fi profile is already
  saved. An unconfigured unit does not wait.
- The factory PIN is rejected before the radio is touched, so a join
  cannot finish on the factory PIN and then strand the phone.
- Captive DNS (`address=/#/192.168.4.1`, DHCP option 114) is in
  `dnsmasq-shared.d` and applies to the hotspot's shared method only. It
  does not hijack DNS after the unit has joined the site. The port-80
  listener closes on that join; it is not left bound on the site LAN.
