"""Spend series for the charts: USD per group member in equal time steps.

Steps start exactly at `since`, converted to UTC, and are `step` apart in absolute
time (a local-midnight `since` with a one-day step stays 24 hours apart across a
daylight-saving change; calendar-day bars are the report's job). The last step may
run past `until`, but only requests in [since, until) count. Every series of one
call has the same times. `per_step[k]` is the spend in step k; `cumulative[k]` is
the spend from `since` to the end of step k, so the last value is the window's
total and `cumulative[k] == sum(per_step[: k + 1])`.

Members with no spend in the window are left out; an empty window gives []. With
`top`, the largest `top` members are kept and the rest are folded into one series
keyed OTHER, placed last.

Memory: rows are sparse while requests are read, and a step with no new spend
reuses the previous value's float object, so a long fine-grained series costs a
pointer per step.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capcom.cost.model import price
from capcom.timeline.groups import Grouper
from capcom.types import GroupBy, Index, PriceTable, Series

OTHER = "(other)"
OTHER_LABEL = "Other"
_ZERO = 0.0


def step_times(since: datetime, until: datetime, step: timedelta) -> tuple[datetime, ...]:
    """The step starts covering [since, until), aware UTC."""
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("since and until must be timezone-aware")
    since, until = since.astimezone(UTC), until.astimezone(UTC)
    if until <= since:
        return ()
    n = -((since - until) // step)  # ceil((until - since) / step)
    return tuple(since + k * step for k in range(n))


Row = dict[int, float]  # step index -> USD, only the steps with spend


def _rows(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    by: GroupBy,
    step: timedelta,
    top: int | None,
    openclaw_root: Path | None,
) -> tuple[tuple[datetime, ...], list[tuple[str, str, Row]]]:
    """The step times and the (key, label, sparse row) of each member, largest total
    first, with the members past `top` folded into OTHER (last)."""
    if top is not None and top < 1:
        raise ValueError("top must be at least 1")
    times = step_times(since, until, step)
    if not times:
        return times, []
    start = times[0]
    grouper = Grouper.from_index(index, table, openclaw_root)
    labels: dict[str, str] = {}
    rows: dict[str, Row] = {}
    for req in index.requests(since=since, until=until):
        k = (req.timestamp - start) // step
        if not 0 <= k < len(times):
            continue  # defensive: the index already filters the window
        for key, label, usd in grouper.members(req, by, price(req, table)):
            if usd == 0:
                continue
            row = rows.get(key)
            if row is None:
                row = rows[key] = {}
                labels[key] = label
            row[k] = row.get(k, 0.0) + usd
    totals = {key: sum(row.values()) for key, row in rows.items()}
    order = sorted((key for key in rows if totals[key] > 0), key=lambda k: (-totals[k], k))
    out = [(key, labels[key], rows[key]) for key in order[:top]]
    if top is not None and len(order) > top:
        other: Row = {}
        for key in order[top:]:
            for k, usd in rows.pop(key).items():
                other[k] = other.get(k, 0.0) + usd
        out.append((OTHER, OTHER_LABEL, other))
    return times, out


def _steps(row: Row, n: int) -> Iterator[float]:
    for k in range(n):
        yield row.get(k, _ZERO)


def _running(row: Row, n: int) -> Iterator[float]:
    run = _ZERO
    for k in range(n):
        if k in row:
            run += row[k]
        yield run  # the same object until the value changes


def per_step(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    by: GroupBy,
    step: timedelta,
    openclaw_root: Path | None = None,
    top: int | None = None,
) -> list[Series]:
    """USD spent in each step per group member, largest total first."""
    times, rows = _rows(index, table, since, until, by, step, top, openclaw_root)
    return [Series(key, label, times, tuple(_steps(row, len(times)))) for key, label, row in rows]


def cumulative(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    by: GroupBy,
    step: timedelta,
    openclaw_root: Path | None = None,
    top: int | None = None,
) -> list[Series]:
    """Cumulative USD per group member (through the end of each step), largest final
    value first."""
    times, rows = _rows(index, table, since, until, by, step, top, openclaw_root)
    return [Series(key, label, times, tuple(_running(row, len(times)))) for key, label, row in rows]
