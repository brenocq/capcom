"""capcom.checks: setup warnings from real usage (contract: capcom.types.ChecksModule).

Stub until the checks module is built."""

from __future__ import annotations

from datetime import datetime

from capcom.types import Finding, Index, PriceTable


def run_checks(index: Index, table: PriceTable, since: datetime, until: datetime) -> list[Finding]:
    raise NotImplementedError("capcom.checks.run_checks")
