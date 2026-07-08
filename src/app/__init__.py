"""WQM-1 v2 application core: supervisor + worker threads.

Replaces the v1 single-threaded 1s-tick loop in main.py. Each I/O domain
(sensing, GPS, LoRa, cloud) runs in its own supervised thread so a slow GPS
fix or a cloud retry can never stall sensor reads, relay rules, or the LoRa
Class-A receive windows.
"""
