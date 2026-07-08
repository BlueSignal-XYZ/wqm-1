"""Smarter sensing: adaptive sampling and per-sensor health monitoring.

Pure-logic modules (injected clock, no hardware imports) consumed by the
sampling worker:

- :class:`sensing.adaptive.AdaptiveSampler` — event-driven sampling rate.
- :class:`sensing.monitor.SensorMonitor` — flatline/spike/drift detection
  and the plain-language ``sensorHealth`` map shipped in heartbeats.
"""
