# Flash a WQM-1 card

One file, one app. GitHub builds the card image, and Raspberry Pi Imager
writes it and checks the write. No `dd`, no disk numbers.

## 1. Which file

| Board | File |
|---|---|
| Raspberry Pi Zero W | `wqm1-pi-zero-<commit>.img.xz` |
| Raspberry Pi Zero 2 W | the same `wqm1-pi-zero-<commit>.img.xz` |
| Raspberry Pi 5 | `wqm1-pi-5-<commit>.img.xz` |

The Pi Zero file is 32-bit. It is the only card a Zero W can run, and a
Zero 2 W runs it too, so one file covers both bench boards. A Zero W given
the Pi 5 file does not boot.

## 2. Get the file

From GitHub, in the wqm-1 repo:

1. **Actions → Card image.**
2. Open the newest green run for the branch you want. If there is none, click
   **Run workflow**, pick the branch, leave *arch* on `armhf`, and click
   **Run workflow**. A build takes about ten minutes, most of it installing
   the firmware on an emulated Pi.
3. Under **Artifacts**, download `wqm1-pi-zero-<commit>`. Safari unzips it into
   a folder with three files:
   - `wqm1-pi-zero-<commit>.img.xz`: the card. Don't unzip it; Imager reads it as is.
   - `wqm1-pi-zero-<commit>.img.xz.sha256`: its checksum.
   - `wqm1-pi-zero-<commit>.txt`: what is on the card (the same text is written
     onto the card).

If the run was published, the same `.img.xz` is also under
**Releases → card-&lt;commit&gt;**, as a direct download.

The run's summary page prints the firmware version, the commit, the boards it
boots and the setup network name, so you can tell which build you have before
downloading it.

## 3. Write it with Raspberry Pi Imager

Install Raspberry Pi Imager from raspberrypi.com/software if the Mac doesn't
have it.

1. Put the microSD card in the Mac (16 GB is what we use).
2. **Choose Device:** Raspberry Pi Zero for a Zero W, or Raspberry Pi Zero 2 W.
3. **Choose OS:** scroll to the bottom, pick **Use custom**, and select the
   `.img.xz`.
4. **Choose Storage:** the SD card. Check the size so it isn't another disk.
5. **Next.** When Imager offers OS customisation (hostname, user, Wi-Fi), choose
   **No**, or skip every step. Customisation writes its own Wi-Fi and user over
   the card's setup files. The unit would then join that Wi-Fi instead of
   raising its setup network, and the card would carry a password.
6. Confirm the erase. Imager writes the card, then reads it back to verify it.
   Wait for **Write Successful**. Don't pull the card while it says
   *Verifying*. Imager ejects the card when it's done.

## 4. Check the card (10 seconds, optional)

Put the card back in the Mac. It mounts as `bootfs`.

```bash
cat /Volumes/bootfs/wqm1-card.txt
```

It shows the firmware version and commit, the boards the card boots, and the
setup network name. For a bench burn-in, add the flag, leave the card's folder,
and eject:

```bash
touch /Volumes/bootfs/burnin && cd ~ && diskutil eject /Volumes/bootfs
```

## 5. First boot

1. Card into the Pi on the HAT. Power it from the 24 V screw terminal, or from
   the Pi's own power port on the bench. **Never both at once.**
2. Give a Pi Zero W up to 10 minutes on its first boot. It grows the file
   system, makes its keys, and starts the firmware on one slow core.
3. The green light on the Pi stops blinking once Linux starts. `config.txt`
   turns it off. A light that isn't blinking does not mean the card isn't
   being read.
4. The setup network appears on your phone. Its name is the *Setup network*
   line on the card label.

## 6. No setup network after 10 minutes

Power the Pi off, put the card in the Mac, and read what the Pi wrote:

```bash
cat /Volumes/bootfs/wqm1-status.txt
```

| What you see | What it means | Do this |
|---|---|---|
| No `wqm1-status.txt` | The Pi never started Linux from this card. | Write it again with Imager, which verifies the write. Check it's the `wqm1-pi-zero` file for a Zero W. Try a second card and a known-good power supply. |
| `BOOT: stage=boot` and little else | Linux started, but the firmware didn't get far. | Paste the file to the firmware team. |
| `hotspot: failed: <reason>` | The radio couldn't raise the setup network. | Paste the file. |
| `station: ok <ip>` | The unit joined a Wi-Fi saved on the card, so it doesn't raise the setup network. | The card was customised in Imager. Write it again and choose **No** to customisation. |
| `power: undervoltage seen` | Weak supply or cable. | Use a better supply and a short cable. |
| `COMMISSION: READY` | The unit is fine. | Look for the network name from the card label again, and give the phone a minute to rescan. |

## For people building cards

`.github/workflows/card-image.yml` runs `tools/golden/build-golden.sh` on a
GitHub runner. `tools/golden/card-label.py` writes `wqm1-card.txt`, and
`tools/golden/card.sh check` refuses to upload a card that:

- is missing a kernel or boot file for its boards;
- has a `root=` that doesn't match the card's own partition;
- carries Wi-Fi, a password, a burn-in flag or an SSH flag;
- has already booted once.

The base images and their checksums are in `tools/golden/base-images.txt`;
`tools/golden/MANIFEST.md` has the details.
