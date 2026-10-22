"""The day-by-day pass: thread state carries over midnight, and finished days come
from the cache file."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import capcom.changes.daily as daily
from capcom.changes.daily import day_tallies, local_midnight
from capcom.checks import run_checks
from capcom.cost.prices import load_prices
from capcom.types import Event, EventKind
from tests.changes.helpers import PROJECT, A, before, build, now_after, overnight
from tests.cost.fakes import req

TABLE = load_prices()
IDLE = ("idle_cache_rewrites", PROJECT)


def test_rewrites_after_an_overnight_pause_count_as_the_finding_counts_them() -> None:
    idx = build(before(40, 1), kind="idle")
    for i in range(28):
        idx.reqs += overnight(A - timedelta(days=28 - i))
    start = A - timedelta(days=28)
    days = day_tallies(idx, TABLE, [IDLE], start, now_after())[IDLE]
    counted = sum(t.count for d, t in days.items() if d < A)
    found = [
        f
        for f in run_checks(idx, TABLE, local_midnight(start), local_midnight(A), agent_dirs=[])
        if f.kind == "idle_cache_rewrites"
    ]
    # 28 daytime pauses and 27 overnight ones ending before the date
    assert found[0].metrics["rewrites"] == 28 + 27
    assert counted == 28 + 27


def test_a_run_across_midnight_is_one_run() -> None:
    night = local_midnight(A) - timedelta(hours=1)
    reqs = [
        req(session="hb", when=night + timedelta(minutes=2 * i), cache_read=50_000)
        for i in range(60)
    ]
    from tests.checks.helpers import SortedIndex

    idx = SortedIndex(reqs=reqs)
    key = ("heartbeat_growing_session", PROJECT)
    days = day_tallies(idx, TABLE, [key], A - timedelta(days=1), now_after())[key]
    assert sum(len(t.samples) for t in days.values()) == 1


def test_finished_days_are_read_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx = build(before(40, 2), kind="idle")
    cache = tmp_path / "days.sqlite"
    first = A - timedelta(days=28)
    cold = day_tallies(idx, TABLE, [IDLE], first, now_after(), cache)[IDLE]
    starts: list[object] = []
    real = daily._pass_days

    def spy(index, table, first, last, now, on_day):  # type: ignore[no-untyped-def]
        starts.append(first)
        real(index, table, first, last, now, on_day)

    monkeypatch.setattr(daily, "_pass_days", spy)
    warm = day_tallies(idx, TABLE, [IDLE], first, now_after(), cache)[IDLE]
    assert [t.count for t in warm.values()] == [t.count for t in cold.values()]
    # only the days from a day before the last activity on are read again
    assert starts and starts[0] >= A - timedelta(days=2)


def test_items_carried_through_midnight_count_on_each_day() -> None:
    from tests.checks.helpers import SortedIndex

    start = local_midnight(A - timedelta(days=3))
    listing = Event(
        EventKind.TOOL_SET,
        "s1",
        None,
        start + timedelta(minutes=1),
        "k1",
        {"source": "skill_listing", "names": ("a", "b"), "size_tokens": 2_000},
    )
    reqs = [
        req(session="s1", when=start + timedelta(hours=6 * i, minutes=2), cache_read=50_000)
        for i in range(12)
    ]
    idx = SortedIndex(reqs=reqs, evs=[listing])
    key = ("skills_unused", PROJECT)
    days = day_tallies(idx, TABLE, [key], A - timedelta(days=3), now_after())[key]
    values = [t.count / t.base for d, t in sorted(days.items()) if t.base]
    assert values == [2_000, 2_000, 2_000]
