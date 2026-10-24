"""Spend's day and hour windows across daylight-saving changes (no index needed)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from capcom.report import api

BERLIN = ZoneInfo("Europe/Berlin")


def ctx(now: datetime) -> Any:
    return api.Context(index=None, table=None, analysis=None, now=now, tz=BERLIN)  # type: ignore[arg-type]


def test_the_night_clocks_fall_back_has_two_one_oclocks() -> None:
    c = ctx(datetime(2026, 10, 26, 12, tzinfo=UTC))
    starts = api._day_starts(c, date(2026, 10, 25))
    assert len(starts) == 25
    first, second = starts[2], starts[3]  # 02:00 CEST, then 02:00 CET
    assert first.hour == second.hour == 2
    assert api._hour_key(c, first) == "2026-10-25T02"  # the wall time reads as the first
    assert api._hour_key(c, second) == "2026-10-25T02+01:00"
    assert api._parse_hour(c, "2026-10-25T02") == first
    # a "+" left unescaped in a query reads as a space
    assert api._parse_hour(c, "2026-10-25T02 01:00") == second
    scope = api._scope_json(c, api._Scope("hour", first, second, day=first.date()), second)
    assert scope["next"] == "2026-10-25T02+01:00" and scope["prev"] == "2026-10-25T01"


def test_the_night_clocks_spring_forward_has_23_hours() -> None:
    c = ctx(datetime(2026, 3, 30, 12, tzinfo=UTC))
    starts = api._day_starts(c, date(2026, 3, 29))
    assert len(starts) == 23
    assert [t.hour for t in starts[:3]] == [0, 1, 3]
    assert api._hour_key(c, starts[2]) == "2026-03-29T03"


def test_an_hour_to_come_is_not_found() -> None:
    c = ctx(datetime(2026, 10, 24, 6, 30, tzinfo=UTC))  # 08:30 in Berlin
    assert api._parse_hour(c, "2026-10-24T08").hour == 8
    with pytest.raises(api.ApiError) as err:
        api._parse_hour(c, "2026-10-24T09")
    assert err.value.status == 404
