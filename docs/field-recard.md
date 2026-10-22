# Field re-card: a new card, the same unit

Use this when an installed unit runs older firmware that cannot get
online, and you want the fixed firmware without losing the unit's
identity. The first case was WQM-10001 on 2026-10-21. It ran the 2.4.0
golden image, joins from the setup page failed with "not found", and the
saved site Wi-Fi was never tried at boot. The fixes are in the PR that
adds this file.

The unit keeps its label, its Ed25519 key, its cloud key and the site's
Wi-Fi. Cloud sees the same device, so the assignment in ops stays valid.
Expect about ten minutes on site with a Mac.

## Before the visit

1. Merge the firmware fix and build a golden image from master
   (`tools/golden/MANIFEST.md`).
2. Flash it to a new card with Raspberry Pi Imager or `dd`.
3. **Do not boot the new card in any Pi.** The first Pi a card boots in
   is the Pi it belongs to (`/var/lib/wqm1/card-binding`). Boot it in a
   bench Pi, then move it to the field Pi, and the unit counts as moved.
   It then refuses the saved Wi-Fi until someone joins again on the setup
   page, which is the step you are trying to skip.
4. On the Mac, install e2fsprogs once: `brew install e2fsprogs`. In bash:

   ```bash
   DEBUGFS="$(brew --prefix e2fsprogs)/sbin/debugfs"
   ```

## On site

### 1. Take the files off the old card

Power the unit off, pull the card and put it in the Mac. Find the Linux
partition (`Linux`, usually `diskNs2`):

```bash
diskutil list
OLD=/dev/disk4s2        # yours from the list above
mkdir -p ~/wqm-10001 && cd ~/wqm-10001
sudo "$DEBUGFS" -R "ls -l /var/lib/bluesignal" "$OLD"
for f in device-ed25519.key device-http.key device-register.json; do
  sudo "$DEBUGFS" -R "dump /var/lib/bluesignal/$f $f" "$OLD"
done
sudo "$DEBUGFS" -R "ls -l /etc/NetworkManager/system-connections" "$OLD"
sudo "$DEBUGFS" -R "dump /etc/NetworkManager/system-connections/<SITE>.nmconnection site.nmconnection" "$OLD"
sudo chown "$USER" ./*
ls -l
```

`device-ed25519.key` must be 32 bytes. If a file is missing from the
`ls` it was never written; skip it. Do not copy `wqm1.db`.

The boot partition mounts in Finder as `bootfs`. Copy
`bluesignal-identity.json` from it if it is still there. The card
consumer may have moved it on first boot, in which case it is in
`/var/lib/bluesignal`; dump it from there the same way. Also copy
`wqm1-status.txt` from `bootfs`. It is the old card's last word on why it
stayed offline, and is worth keeping.

Eject the old card and keep it. It is the fallback.

### 2. Write them onto the new card

Put the new, never-booted card in the Mac.

```bash
diskutil list
NEW=/dev/disk4s2        # yours
sudo "$DEBUGFS" -R "stat /var/lib/bluesignal" "$NEW"   # note User and Group
```

If `/var/lib/bluesignal` does not exist, create it with
`sudo "$DEBUGFS" -w -R "mkdir /var/lib/bluesignal" "$NEW"` and use the
uid/gid of `/home/pi` (`stat /home/pi`).

```bash
U=1000; G=1000          # from the stat above
for f in device-ed25519.key device-http.key device-register.json; do
  [ -f "$f" ] || continue
  sudo "$DEBUGFS" -w -R "write $f /var/lib/bluesignal/$f" "$NEW"
  sudo "$DEBUGFS" -w -R "sif /var/lib/bluesignal/$f mode 0100600" "$NEW"
  sudo "$DEBUGFS" -w -R "sif /var/lib/bluesignal/$f uid $U" "$NEW"
  sudo "$DEBUGFS" -w -R "sif /var/lib/bluesignal/$f gid $G" "$NEW"
done
C=/etc/NetworkManager/system-connections
sudo "$DEBUGFS" -w -R "write site.nmconnection $C/<SITE>.nmconnection" "$NEW"
sudo "$DEBUGFS" -w -R "sif $C/<SITE>.nmconnection mode 0100600" "$NEW"
sudo "$DEBUGFS" -w -R "sif $C/<SITE>.nmconnection uid 0" "$NEW"
sudo "$DEBUGFS" -w -R "sif $C/<SITE>.nmconnection gid 0" "$NEW"
sudo "$DEBUGFS" -R "ls -l /var/lib/bluesignal" "$NEW"
sudo "$DEBUGFS" -R "ls -l $C" "$NEW"
```

Each file should show `100600`. The key files belong to the uid from the
stat; the Wi-Fi profile belongs to `0 0`.

Copy `bluesignal-identity.json` into the new card's `bootfs` in Finder.
Eject.

### 3. Boot it

Put the card in the unit's Pi, the one on the HAT, and power on. Within
two minutes the unit joins the saved network by itself. Check, in order:

1. ops → Units, or Cloud: the unit's last check-in moves.
2. If not: the hotspot comes up as the label's name (`WQM1-0001`, not
   `WQM1-xxxx`). Join it from a phone. The setup page opens on
   "Continue — connect to Wi-Fi" once the PIN is set, and a join there
   now scans before connecting.
3. If you need to leave: the unit retries the saved network every five
   minutes while no phone is on its hotspot. A router that comes back
   later is picked up without a visit.
4. If it is still offline, pull the card and read `wqm1-status.txt` on
   `bootfs`. It now names NetworkManager's reason (`station: fail: …`).

## What not to do

- Do not boot the new card anywhere before the field Pi (step 3 above).
- Do not generate a new key. A missing key makes a new device as far as
  Cloud is concerned, and the label's QR no longer matches it.
- Do not paste the site's Wi-Fi password into chat, a ticket or this
  repo. It lives in the profile file and nowhere else.
