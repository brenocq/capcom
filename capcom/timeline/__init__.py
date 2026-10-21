"""capcom.timeline: spend series and spike detection (contract: capcom.types.TimelineModule).

Stubs until the timeline module is built."""

from __future__ import annotations

from datetime import datetime, timedelta

from capcom.types import GroupBy, Index, PriceTable, Series, Spike


def cumulative(
    index: Index, table: PriceTable, since: datetime, until: datetime, by: GroupBy, step: timedelta
) -> list[Series]:
    raise NotImplementedError("capcom.timeline.cumulative")


def per_step(
    index: Index, table: PriceTable, since: datetime, until: datetime, by: GroupBy, step: timedelta
) -> list[Series]:
    raise NotImplementedError("capcom.timeline.per_step")


def find_spikes(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    session_id: str | None = None,
) -> list[Spike]:
    raise NotImplementedError("capcom.timeline.find_spikes")
