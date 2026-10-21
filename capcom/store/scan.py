"""Scan the logs into the index (built in milestone 1 by the store module)."""

from __future__ import annotations

from capcom.config import Config
from capcom.types import ScanStats


def run_scan(config: Config, quiet: bool = False) -> ScanStats:
    raise NotImplementedError("capcom.store.scan.run_scan: milestone 1, store module")
