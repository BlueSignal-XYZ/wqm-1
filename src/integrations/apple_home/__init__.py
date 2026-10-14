"""
Apple Home — the WQM-1 as a HomeKit accessory on the owner's own Wi-Fi.

Apple HomeView (announced 2026-10-13, on sale 2026-11-13) runs Apple's own
apps only. The one door a third-party product has onto that screen, onto an
iPhone's Home app and onto Siri is the Home app's accessory list, and the
Home app lists only HomeKit- or Matter-enabled accessories. So "the WQM-1 on
HomeView" means: the unit is a HomeKit accessory.

What this package does, and the rules it keeps:

* **Opt-in, owner-enabled, LAN-only.** ``apple_home_enabled`` is off by
  default. When on, the unit advertises one HomeKit accessory over mDNS on
  the local network and nothing else; the cloud never sees the setup code,
  never pairs, and cannot pair on anyone's behalf. The owner pairs by
  scanning a code shown on the unit's own Service Window.
* **Readings go out, nothing comes in.** The accessory exposes the readings
  the unit already takes and the irrigation hold's contact state. It takes
  no commands: no relay, no setting, no hold change can arrive through
  HomeKit. The read door cannot act (the smart-breaker rule, again).
* **Relay, never grade.** HomeKit has a water-temperature characteristic and
  a contact-sensor characteristic. It has nothing for pH, TDS or turbidity;
  those travel as custom characteristics that third-party HomeKit apps (Eve,
  Home+) show and Apple's Home app does not. Nothing here maps a reading onto
  Apple's ``AirQuality``/``WaterLevel`` verdict scales or onto a "leak
  detected" alarm — a LeakSensor that fires on turbidity would be a verdict
  about the water, which the unit never makes.
* **Uncertified accessory, said plainly.** The HomeKit Accessory Protocol
  is published by Apple for non-commercial use; a product sold with it built
  in belongs in Apple's MFi program, and a Matter-certified product may carry
  the "Works with Apple Home" badge. BlueSignal has neither today. The Home
  app therefore shows an "Uncertified Accessory" notice at pairing time — the
  same notice every Homebridge user sees — and the owner enables the feature
  themselves on an open-source firmware they run. Marketing copy must never
  say "Works with Apple Home", "HomeKit-certified" or "Matter". See
  marketplace/docs/operations/apple-homeview-launch.md.

The pure mapping is :mod:`accessory`; the HAP-python lifecycle is
:mod:`bridge`, which imports the library lazily so a unit without it still
boots, with the feature reporting ``library_missing`` rather than crashing.
"""

from integrations.apple_home.accessory import HomeSnapshot, snapshot_from_reading
from integrations.apple_home.bridge import AppleHomeBridge, HapUnavailable

__all__ = ["AppleHomeBridge", "HapUnavailable", "HomeSnapshot", "snapshot_from_reading"]
