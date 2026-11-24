# Commissioning simulator

This runs the real setup flow end to end on one Linux machine: a phone joins
the unit's hotspot, opens the setup page, picks a Wi-Fi network and sends the
password. Then the unit joins a home router, or says why it couldn't and
brings the hotspot back so the phone can try again.

```
sudo /path/to/venv/bin/python tools/sim/commission.py           # every scenario
sudo /path/to/venv/bin/python tools/sim/commission.py --list
sudo /path/to/venv/bin/python tools/sim/commission.py slow_address
```

Use a throwaway VM or container. The script needs root. It creates network
namespaces, two bridges and a system user (`wqm1sim`), and the join code
writes a few of its fixed paths. It needs `ip`, `dnsmasq`, `busybox` (for
`udhcpc`), `openssl`, `curl`, `setpriv`, and a Python with the repo's
requirements. Each scenario prints its checks, and the run writes
`report.json` to the sim directory (default `/run/wqm1-sim`). Logs from the
unit, the fake radio, both dnsmasq instances and the router's HTTP servers
are kept in the same directory.

## What is real

- **The code under test.** `utils.netctl`, `utils.join`, `utils.join_service`
  (as root) and `utils.captive_portal` (as an ordinary user, over the join
  socket) are the shipped modules. `unit.py` sets two seams and nothing else:
  the radio's bands, and where systemd units get written.
- **The network.** The phone, the unit and the router each run in their own
  namespace. All addresses, routes and sockets are real.
- **The hotspot.** The unit's own dnsmasq runs from the config `netctl`
  writes. It hands the phone a real DHCP lease and answers every DNS name
  with 192.168.4.1. The phone's captive checks (`captive.apple.com`,
  `connectivitycheck.gstatic.com`) are real HTTP requests to the portal.
- **The home network.** The router runs a real dnsmasq for DHCP and DNS. Its
  DHCP can be slowed (`--dhcp-reply-delay`) or turned off. The unit asks for
  its address with a real DHCP client (busybox `udhcpc`), which keeps asking
  after a timed-out `nmcli` exits, as NetworkManager does.
- **BlueSignal.** The router's DNS answers the shipped API host with itself.
  A real HTTPS server answers there with a certificate from a simulated CA,
  and the unit is told to trust that CA (`SSL_CERT_FILE`). The unit's
  verified-TLS check (`reach_bluesignal`) runs unchanged. The router also
  answers the sign-in probe with a 204, or with a redirect to model a guest
  network that has a sign-in page.

## What is faked, and why

This kernel has no `cfg80211`/`mac80211`, so `mac80211_hwsim` (virtual radios
with real hostapd and wpa_supplicant) can't run. QEMU can't emulate the Pi's
Broadcom radio either. So `fake_radio.py` stands in for `nmcli`, `iw`,
`systemctl` (for the two hotspot units), `hostapd_cli`, `iwgetid`,
`timedatectl`, `rfkill` and `iptables`. It answers in the shapes those tools
print, and `tests/test_sim_fake_radio.py` checks those shapes against the real
parsers.

Association is modelled by moving the unit's cable between two switches.
`brap` is the hotspot cell, which the phone is on. `brlan` is the home
network. "hostapd up" plugs the unit into `brap`. A good join plugs it into
`brlan`, where DHCP and everything above it is real.

## What it can't prove

Compared with a real Pi on a real router, the simulator doesn't show:

- radio association, the WPA2/WPA3 4-way handshake and PMF;
- how a real router steers between bands, and how brcmfmac firmware behaves
  (the 5 GHz refusal is scripted);
- range and RF noise;
- NetworkManager's and wpa_supplicant's real timings and wording. The fake
  uses the error text NetworkManager 1.52 prints and the timings set in
  `world_for()`;
- whether a real phone's OS opens the captive sheet. The checks prove the
  portal answers each probe the way the phone expects;
- the image itself: boot, first-boot scripts, systemd ordering and the
  hotspot guard.

## Scenarios

| Name | What happens | Expected |
| --- | --- | --- |
| `happy_path` | Hotspot, pick Wi-Fi, connect | joined |
| `wrong_password_then_retry` | Wrong password, then the right one | `wrong_password`, then joined |
| `slow_address` | Slow scan, then a router that answers DHCP 33 s after association, so the lease lands after the 55 s join budget | joined (fails on the code before #191) |
| `network_gone` | Router switched off after the list was shown | `not_found` |
| `dual_band_5ghz_refuses` | One SSID on both bands, Pi 4 radio, 5 GHz never answers | joined on 2.4 GHz |
| `no_dhcp` | Router hands out no address | `no_dhcp` |
| `no_bluesignal` | Internet up, BlueSignal unreachable | `no_bluesignal` after the 180 s device-approval hold (spec D5) |
| `captive_sign_in` | Guest network with a sign-in page | `captive_sign_in` |

For each failure, the checks confirm four things: the unit stored the reason,
the hotspot came back in time (about a minute, or 215 s for `no_bluesignal`),
the phone rejoined it, and the page shows the reason and offers a retry.

A slow DHCP server is modelled by starting the router's DHCP late, not with
dnsmasq's `--dhcp-reply-delay`. That option holds dnsmasq's only thread, so
it also stalls the REQUEST that follows the offer, which is a broken router
rather than a slow one.

The Wi-Fi names and passwords are made-up values for networks that exist
only in the simulator. Nothing prints them.
