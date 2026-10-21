"""Spend series for the charts: USD per group member in equal time steps.

Steps start exactly at `since` and are `step` apart; the last one may run past
`until`, but only requests in [since, until) count. Every series of one call has
the same times. `per_step[k]` is the spend in step k; `cumulative[k]` is the spend
from `since` to the end of step k, so the last value is the window's total and
`cumulative[k] == sum(per_step[: k + 1])`.

Members with no spend in the window are left out; an empty window gives [].
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import accumulate
from pathlib import Path

from capcom.cost.model import price
from capcom.timeline.groups import Grouper
from capcom.types import GroupBy, Index, PriceTable, Series


def step_times(since: datetime, until: datetime, step: timedelta) -> tuple[datetime, ...]:
    """The step starts covering [since, until)."""
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("since and until must be timezone-aware")
    if until <= since:
        return ()
    n = -((since - until) // step)  # ceil((until - since) / step)
    return tuple(since + k * step for k in range(n))


def per_step(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    by: GroupBy,
    step: timedelta,
    openclaw_root: Path | None = None,
) -> list[Series]:
    """USD spent in each step per group member, largest total first."""
    times = step_times(since, until, step)
    if not times:
        return []
    grouper = Grouper.from_index(index, table, openclaw_root)
    labels: dict[str, str] = {}
    values: dict[str, list[float]] = {}
    for req in index.requests(since=since, until=until):
        k = (req.timestamp - since) // step
        if not 0 <= k < len(times):
            continue  # defensive: the index already filters the window
        for key, label, usd in grouper.members(req, by, price(req, table)):
            row = values.get(key)
            if row is None:
                row = values[key] = [0.0] * len(times)
                labels[key] = label
            row[k] += usd
    out = [
        Series(key, labels[key], times, tuple(row)) for key, row in values.items() if sum(row) > 0
    ]
    out.sort(key=lambda s: (-sum(s.values), s.key))
    return out


def cumulative(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    by: GroupBy,
    step: timedelta,
    openclaw_root: Path | None = None,
) -> list[Series]:
    """Cumulative USD per group member (through the end of each step), largest final
    value first."""
    return [
        Series(s.key, s.label, s.times, tuple(accumulate(s.values)))
        for s in per_step(index, table, since, until, by, step, openclaw_root)
    ]
