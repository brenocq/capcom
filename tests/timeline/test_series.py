"""Chart series: step alignment, grouping, cumulative vs per step, empty windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline import cumulative, per_step
from capcom.timeline.series import step_times
from capcom.types import AgentInfo, GroupBy, Series
from tests.cost.fakes import T0, FakeIndex, at, req

TABLE = load_prices()
OC = Path("/home/user/.openclaw")
HOUR = timedelta(hours=1)


def usd(**usage: int) -> float:
    return price(req(**usage), TABLE).total


def sample() -> FakeIndex:
    return FakeIndex(
        reqs=[
            req(when=at(5), project="-home-user-a", output=10_000),  # $0.20
            req(when=at(65), project="-home-user-a", output=5_000),  # $0.10
            req(when=at(70), project="-tmp-x", output=20_000, agent="ag1"),  # $0.40
            req(when=at(130), project="-tmp-y", output=1_000, model="claude-haiku-4-5"),
            req(when=at(170), project="-home-user--openclaw-w", cache_read=1_000_000),
            req(when=at(175), project="-home-user-a", model="<synthetic>", output=99_999),
        ],
        agent_infos=[AgentInfo("ag1", "s1", agent_type="scout")],
    )


def by_key(series: list[Series]) -> dict[str, tuple[float, ...]]:
    return {s.key: s.values for s in series}


def close(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == len(b) and all(abs(x - y) < 1e-9 for x, y in zip(a, b, strict=True))


def test_step_times_start_at_since_and_cover_until() -> None:
    assert step_times(T0, T0 + 3 * HOUR, HOUR) == (T0, T0 + HOUR, T0 + 2 * HOUR)
    # a partial last step still gets a slot
    assert len(step_times(T0, T0 + 3 * HOUR + timedelta(seconds=1), HOUR)) == 4
    assert step_times(T0, T0, HOUR) == ()
    assert step_times(T0, T0 - HOUR, HOUR) == ()
    with pytest.raises(ValueError):
        step_times(T0, T0 + HOUR, timedelta(0))


def test_local_midnight_since_is_kept() -> None:
    tz = timezone(timedelta(hours=-3))
    since = datetime(2026, 10, 20, 0, 0, tzinfo=tz)
    series = per_step(
        sample(), TABLE, since, since + timedelta(days=2), GroupBy.PROJECT, timedelta(days=1), OC
    )
    assert series
    for s in series:
        assert s.times == (since, since + timedelta(days=1))
        assert s.times[0].utcoffset() == since.utcoffset()


def test_per_step_buckets_and_groups_by_project() -> None:
    series = per_step(sample(), TABLE, T0, T0 + 3 * HOUR, GroupBy.PROJECT, HOUR, OC)
    got = by_key(series)
    assert set(got) == {"-home-user-a", "temporary", "openclaw"}
    assert close(got["-home-user-a"], (usd(output=10_000), usd(output=5_000), 0.0))
    haiku = price(req(output=1_000, model="claude-haiku-4-5"), TABLE).total
    assert close(got["temporary"], (0.0, usd(output=20_000), haiku))
    assert close(got["openclaw"], (0.0, 0.0, usd(cache_read=1_000_000)))
    # every series has the same times; largest total first
    assert len({s.times for s in series}) == 1
    totals = [sum(s.values) for s in series]
    assert totals == sorted(totals, reverse=True)


def test_cumulative_is_running_sum_of_per_step() -> None:
    ix = sample()
    for by in GroupBy:
        steps = per_step(ix, TABLE, T0, T0 + 3 * HOUR, by, timedelta(minutes=30), OC)
        cums = cumulative(ix, TABLE, T0, T0 + 3 * HOUR, by, timedelta(minutes=30), OC)
        assert [s.key for s in steps] == [c.key for c in cums]
        for s, c in zip(steps, cums, strict=True):
            assert c.times == s.times and c.label == s.label
            running = 0.0
            for v, cv in zip(s.values, c.values, strict=True):
                running += v
                assert abs(running - cv) < 1e-9
        finals = [c.values[-1] for c in cums]
        assert finals == sorted(finals, reverse=True)


def test_groupings_agree_on_the_total() -> None:
    ix = sample()
    totals = {
        by: sum(c.values[-1] for c in cumulative(ix, TABLE, T0, T0 + 3 * HOUR, by, HOUR, OC))
        for by in GroupBy
    }
    expected = sum(price(r, TABLE).total for r in ix.reqs if r.model != "<synthetic>")
    for total in totals.values():
        assert abs(total - expected) < 1e-9


def test_other_groupings() -> None:
    ix = sample()
    agents = by_key(per_step(ix, TABLE, T0, T0 + 3 * HOUR, GroupBy.AGENT, 3 * HOUR, OC))
    assert set(agents) == {"main", "scout"}
    models = by_key(per_step(ix, TABLE, T0, T0 + 3 * HOUR, GroupBy.MODEL, 3 * HOUR, OC))
    assert set(models) == {"claude-opus-5-5", "claude-haiku-4-5-20251001"}
    cats = by_key(per_step(ix, TABLE, T0, T0 + 3 * HOUR, GroupBy.CATEGORY, 3 * HOUR, OC))
    assert set(cats) == {"output", "cache_read"}  # members with no spend are left out
    sessions = per_step(ix, TABLE, T0, T0 + 3 * HOUR, GroupBy.SESSION, 3 * HOUR, OC)
    assert [(s.key, s.label) for s in sessions] == [("s1", "s1")]


def test_window_bounds_since_inclusive_until_exclusive() -> None:
    ix = FakeIndex(reqs=[req(when=T0, output=1000), req(when=T0 + HOUR, output=1000)])
    got = per_step(ix, TABLE, T0, T0 + HOUR, GroupBy.SESSION, HOUR, OC)
    assert close(got[0].values, (usd(output=1000),))


def test_empty_windows() -> None:
    ix = sample()
    # no requests in the window
    later = T0 + timedelta(days=3)
    assert per_step(ix, TABLE, later, later + HOUR, GroupBy.PROJECT, HOUR, OC) == []
    assert cumulative(ix, TABLE, later, later + HOUR, GroupBy.CATEGORY, HOUR, OC) == []
    # an empty interval
    assert cumulative(ix, TABLE, T0, T0, GroupBy.PROJECT, HOUR, OC) == []
    # only placeholder replies
    only_synth = FakeIndex(reqs=[req(when=at(1), model="<synthetic>", output=10)])
    assert per_step(only_synth, TABLE, T0, T0 + HOUR, GroupBy.MODEL, HOUR, OC) == []


def test_naive_datetimes_rejected() -> None:
    naive = datetime(2026, 10, 20)
    with pytest.raises(ValueError):
        per_step(sample(), TABLE, naive, naive + HOUR, GroupBy.PROJECT, HOUR, OC)
    assert T0.tzinfo is UTC
