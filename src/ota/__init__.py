"""
WQM-1 OTA update agent.

Package-level OTA: poll the cloud for a signed firmware bundle, verify it
(Ed25519 + sha256 + packaging sanity + downgrade guard), apply it atomically
via the /opt/bluesignal/current symlink, self-test, and roll back on failure.

Design: docs/architecture/ota-firmware-updates.md (marketplace repo).
Runbook: docs/ota-runbook.md (this repo).
"""

from ota.agent import OTAAgent

__all__ = ["OTAAgent"]
