"""
Cloud (HTTP/WiFi) transport for the WQM-1.

A native, store-and-forward HTTP path to the BlueSignal cloud that coexists with
LoRaWAN: it uploads buffered readings to the `ingestReading` endpoint and polls
`deviceCommands` for relay control. Enabled via config (`cloud_enabled`) once the
device has an API key — see CloudClient.
"""

from cloud.client import CloudClient

__all__ = ["CloudClient"]
