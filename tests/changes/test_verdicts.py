"""Verdicts on synthetic weeks: the after rate against the before weeks' range."""

from __future__ import annotations

from datetime import timedelta

import pytest

from capcom.changes import measure
from capcom.changes.measure import local_midnight
from capcom.checks import run_checks
from capcom.cost.prices import load_prices
from capcom.types import Verdict
from tests.changes.helpers import PROJECT, A, after, before, build, mark_of, now_after, span

TABLE = load_prices()


def _measure(days, kind="tool", finding="subagent_cache_expiry", now=None):  # type: ignore[no-untyped-def]
    idx = build(days, kind)
    return measure(idx, TABLE, mark_of(finding), now or now_after())


def test_rewrites_gone_after_the_date_is_working_with_a_saving() -> None:
    r = _measure({**before(40, 3), **after(40, 0)})
    assert r.verdict is Verdict.WORKING
    assert r.measure == "Tool-wait rewrites per 1,000 requests"
    assert r.before == pytest.approx(3 / 46 * 1000)
    assert r.after == 0
    assert r.before_range == (pytest.approx(3 / 46 * 1000), pytest.approx(3 / 46 * 1000))
    assert r.measured_usd_per_week is not None and r.measured_usd_per_week > 0
    assert "fell" in r.reason
    # four full before weeks on the applied weekday, then weeks from the day after
    befores = [p for p in r.weeks if not p.after]
    assert [p.week_start for p in befores] == [A - timedelta(days=7 * k) for k in (4, 3, 2, 1)]
    assert r.weeks[len(befores)].week_start == A + timedelta(days=1)
    labels = [row[0] for row in r.evidence]
    assert "Rewrites per week" in labels
    assert ("Rewrites per week", "21.0", "0.0") in r.evidence


def test_the_saving_is_the_rate_drop_times_the_after_workload_times_the_unit_price() -> None:
    r = _measure({**before(40, 3), **after(40, 0)})
    idx = build({**before(40, 3), **after(40, 0)})
    start = local_midnight(A - timedelta(days=28))
    found = [
        f
        for f in run_checks(idx, TABLE, start, local_midnight(A), agent_dirs=[])
        if f.kind == "subagent_cache_expiry"
    ]
    assert found and found[0].metrics["rewrites"] == 28 * 3  # the finding counts the same
    weeks = (local_midnight(A) - min(r.timestamp for r in idx.reqs)) / timedelta(days=7)
    unit = found[0].saving_usd_per_week * weeks / (28 * 3)  # one rewrite's excess
    assert r.measured_usd_per_week == pytest.approx(
        r.before / 1000 * r.requests_per_week_after * unit, rel=1e-6
    )


def test_the_same_rate_after_is_no_clear_effect() -> None:
    r = _measure({**before(40, 3), **after(40, 3)})
    assert r.verdict is Verdict.NO_EFFECT
    assert r.measured_usd_per_week is None
    assert "no clear effect" in r.reason


def test_a_doubled_workload_at_the_same_rate_is_still_no_effect() -> None:
    r = _measure({**before(40, 3), **after(80, 6)})
    assert r.verdict is Verdict.NO_EFFECT
    assert r.after == pytest.approx(r.before)
    assert r.requests_per_week_after > 1.8 * r.requests_per_week_before


def test_a_rate_inside_varied_before_weeks_is_no_effect_and_above_is_worse() -> None:
    weeks = {}
    for k, e in zip((4, 3, 2, 1), (2, 4, 3, 3), strict=True):
        start = A - timedelta(days=7 * k)
        weeks |= span(start, start + timedelta(days=7), 40, e)
    assert _measure({**weeks, **after(40, 3)}).verdict is Verdict.NO_EFFECT
    worse = _measure({**weeks, **after(40, 8)})
    assert worse.verdict is Verdict.WORSE
    assert worse.measured_usd_per_week is not None and worse.measured_usd_per_week < 0
    assert worse.before_range == (pytest.approx(2 / 44 * 1000), pytest.approx(4 / 48 * 1000))


def test_under_seven_days_is_too_early_with_the_due_date() -> None:
    r = _measure({**before(40, 3), **after(40, 0, days=4)}, now=now_after(4))
    assert r.verdict is Verdict.TOO_EARLY
    assert r.verdict_due == A + timedelta(days=8)
    assert r.measured_usd_per_week is None


def test_too_few_requests_after_is_too_early_projected_at_the_pace() -> None:
    # 10 requests a day: 140 after two weeks, under the 200 minimum
    r = _measure({**before(40, 3), **after(10, 0)})
    assert r.verdict is Verdict.TOO_EARLY
    assert r.verdict_due is not None and r.verdict_due > A + timedelta(days=15)
    assert "200" in r.reason


def test_the_minimum_is_a_fifth_of_a_usual_before_week() -> None:
    # 1,400 requests a week before: the minimum is 280, more than 200
    r = _measure({**before(200, 0), **after(19, 0)})  # 266 after 14 days
    assert r.verdict is Verdict.TOO_EARLY
    assert "280" in r.reason


def test_nothing_in_scope_before_is_no_data() -> None:
    idx = build({**before(40, 3), **after(40, 0)})
    r = measure(idx, TABLE, mark_of("subagent_cache_expiry", scope="-other"), now_after())
    assert r.verdict is Verdict.NO_DATA


def test_a_kind_without_a_measure_is_no_data_with_a_reason() -> None:
    idx = build({**before(40, 3), **after(40, 0)})
    r = measure(idx, TABLE, mark_of("one_command_subagents"), now_after())
    assert r.verdict is Verdict.NO_DATA
    assert "no measure" in r.reason


def test_the_before_window_is_clipped_to_where_the_logs_begin() -> None:
    days = {**span(A - timedelta(days=10), A, 40, 3), **after(40, 0)}
    r = _measure(days)
    befores = [p for p in r.weeks if not p.after]
    assert len(befores) == 2  # the first one partial: not in the range, but measured
    assert r.verdict is Verdict.WORKING
    assert r.requests_per_week_before == pytest.approx(46 * 7, rel=0.01)


def test_idle_rewrites_are_measured_on_their_own() -> None:
    r = _measure({**before(40, 3), **after(40, 0)}, kind="idle", finding="idle_cache_rewrites")
    assert r.verdict is Verdict.WORKING
    other = _measure({**before(40, 3), **after(40, 0)}, kind="idle")
    assert other.verdict is Verdict.NO_DATA or other.before == 0
    assert PROJECT
