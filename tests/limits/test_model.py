"""Segments, the fit and the forecast on synthetic samples and requests."""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.cost.prices import load_prices
from capcom.cost.top import project_label
from capcom.limits.model import (
    NOT_ENOUGH,
    Interval,
    Spend,
    analyze,
    append_samples,
    fit,
    forecast,
    intervals,
    load_samples,
    nnls,
    sample_json,
    samples_path,
    seed_samples,
    segments,
)
from capcom.types import Event, EventKind, LimitWindow
from tests.limits.fakes import T0, FakeIndex, minutes, req, sample

TABLE = load_prices()
H5 = LimitWindow.FIVE_HOUR
D7 = LimitWindow.SEVEN_DAY


def test_load_skips_bad_and_partial_lines(tmp_path: Path) -> None:
    good = sample(T0, 10, resets=T0 + timedelta(hours=4))
    path = tmp_path / "limits.jsonl"
    path.write_text(
        sample_json(good) + "\n" + "garbage\n" + '{"window": "monthly", "time": "x"}\n'
        '{"time": "2026-10-01T09:00:00+00:00", "window": "five_hour", "used_percentage": 20}\n'
        + sample_json(sample(T0 + minutes(90), 30))[:-5]  # partial: still being written
    )
    got = load_samples(path)
    assert [s.used_percentage for s in got] == [10, 20]
    assert got[0] == good and got[1].source == "statusline"
    assert load_samples(tmp_path / "missing.jsonl") == []


def test_segments_cut_at_resets_and_drop_stale_readings() -> None:
    r1 = T0 + timedelta(hours=3)
    r2 = r1 + timedelta(hours=6)
    ss = [
        sample(T0, 10, resets=r1),
        sample(T0 + minutes(30), 25, resets=r1),
        sample(T0 + minutes(40), 22, resets=r1),  # a stale session: dropped
        sample(T0 + minutes(50), 25, resets=r1 + timedelta(seconds=3)),  # jitter: same reset
        sample(T0 + minutes(60), 40, resets=r1),
        sample(r2 - timedelta(hours=4), 5, resets=r2),
        sample(r2 - timedelta(hours=1), 9, window=D7),  # other window
    ]
    segs = segments(ss, H5)
    assert len(segs) == 2
    assert [s.used_percentage for s in segs[0].samples] == [10, 25, 40]
    assert segs[0].start == r1 - timedelta(hours=5) and segs[0].start_known
    ivs = intervals(segs)
    assert [round(i.rise) for i in ivs] == [10, 15, 15, 5]
    assert ivs[0].start == segs[0].start


def test_segments_never_overlap() -> None:
    reset = T0 + timedelta(hours=4)  # its window would start an hour before T0
    ss = [sample(T0 - minutes(30), 100, source="rate_limit_error"), sample(T0, 10, resets=reset)]
    segs = segments(ss, H5)
    assert len(segs) == 2
    assert segs[1].start == segs[0].last.time and not segs[1].start_known


def test_nnls_recovers_nonnegative_solution() -> None:
    rng = random.Random(3)
    true = [0.5, 0.0, 2.0]
    rows = [[rng.uniform(0, 10) for _ in true] for _ in range(30)]
    b = [sum(w * x for w, x in zip(true, r, strict=True)) for r in rows]
    got = nnls(rows, b)
    assert got == pytest.approx(true, abs=1e-6)
    # a column that only hurts is held at zero rather than going negative
    b2 = [r[0] - 0.3 * r[1] for r in rows]
    assert min(nnls(rows, b2)) >= 0


# Known weights, percent per million tokens, for a synthetic subscription.
WEIGHTS = {
    ("claude-opus-5-5", "output"): 40.0,
    ("claude-opus-5-5", "cache_read"): 1.0,
    ("claude-opus-5-5", "cache_write"): 12.0,
    ("claude-opus-5-5", "input"): 10.0,
    ("claude-sonnet-5-5", "output"): 16.0,
    ("claude-sonnet-5-5", "cache_read"): 0.4,
}


def synthetic(n_windows: int, seed: int = 1, noise: float = 0.0) -> tuple[FakeIndex, list]:
    """Requests every few minutes and a reading every 20 minutes, with the
    percentage computed from WEIGHTS (rounded to whole points like Claude Code)."""
    rng = random.Random(seed)
    reqs, ss = [], []
    t = T0
    for _ in range(n_windows):
        reset = t + timedelta(hours=5)
        total = 0.0
        for step in range(1, 15):  # 4 h 40 min of readings
            end = t + minutes(20 * step)
            for k in range(4):
                when = end - minutes(20) + minutes(5 * k + 1)
                model = "claude-opus-5-5" if rng.random() < 0.6 else "claude-sonnet-5-5"
                u = {
                    "output": rng.randint(500, 40_000),
                    "cache_read": rng.randint(50_000, 900_000),
                    "cache_write_1h": rng.randint(0, 60_000) if model.startswith("claude-o") else 0,
                    "input": rng.randint(0, 30_000) if model.startswith("claude-o") else 0,
                }
                reqs.append(req(when, model=model, **u))
                cats = {
                    "output": u["output"],
                    "cache_read": u["cache_read"],
                    "cache_write": u["cache_write_1h"],
                    "input": u["input"],
                }
                total += sum(WEIGHTS.get((model, c), 0.0) * v / 1e6 for c, v in cats.items())
            pct = round(total + rng.uniform(-noise, noise))
            ss.append(sample(end, pct, resets=reset))
        t = reset
    return FakeIndex(reqs), ss


def test_fit_recovers_known_weights() -> None:
    index, ss = synthetic(12)
    view = analyze(index, TABLE, ss, H5, now=ss[-1].time + minutes(1))
    f = view.fit
    assert f is not None and f.method == "per_type"
    assert f.intervals >= 100
    for key in [("claude-opus-5-5", "output"), ("claude-opus-5-5", "cache_read")]:
        assert f.weights[key] == pytest.approx(WEIGHTS[key], rel=0.25)
    assert f.weights[("claude-sonnet-5-5", "output")] == pytest.approx(16.0, rel=0.35)
    assert f.mean_abs_error < 1.0  # whole-point rounding


def test_fit_falls_back_to_percent_per_dollar_with_few_samples() -> None:
    index, ss = synthetic(1)
    few = ss[:4]
    view = analyze(index, TABLE, few, H5, now=few[-1].time + minutes(1))
    f = view.fit
    assert f is not None and f.method == "per_usd"
    assert "too few samples" in f.note
    assert f.per_usd > 0 and f.weights == {}


def test_not_enough_samples() -> None:
    index, ss = synthetic(1)
    one = [ss[0].__class__(ss[0].time, H5, ss[0].used_percentage, None, "s1")]
    view = analyze(index, TABLE, one, H5, now=one[0].time + minutes(1))
    assert view.fit is None and view.forecast is None and view.reason == NOT_ENOUGH
    assert fit(H5, [(Interval(T0, T0 + minutes(5), 3), Spend())], 1) is None


def test_forecast_is_steady_with_steady_spend() -> None:
    now = T0
    reset = now + timedelta(hours=2)
    fc = forecast(H5, 40.0, 38.0, [2.0] * 12, now, reset)  # 2 points per 15 minutes
    assert fc.median == pytest.approx(56.0) and fc.low == fc.high == pytest.approx(56.0)
    assert fc.runs_out_at is None
    assert fc.fan[0] == (now, 40.0, 40.0, 40.0) and fc.fan[-1][0] == reset


def test_forecast_range_and_running_out() -> None:
    now = T0
    reset = now + timedelta(hours=3)
    rates = [0.0, 0.0, 10.0, 12.0, 0.0, 9.0, 0.0, 11.0] * 2
    fc = forecast(H5, 60.0, 60.0, rates, now, reset)
    assert fc.low < fc.median < fc.high
    assert fc.runs_out_at is not None and now < fc.runs_out_at < reset
    # with no spend the value stays where it is
    flat = forecast(H5, 60.0, 60.0, [0.0] * 8, now, reset)
    assert flat.median == flat.high == 60.0


def test_analyze_forecasts_the_current_window() -> None:
    index, ss = synthetic(8)
    last_reset = ss[-1].resets_at
    assert last_reset is not None
    now = ss[-1].time + minutes(10)
    # half the last window's readings: the window is current, with spend after them
    view = analyze(index, TABLE, ss[:-7], H5, now=ss[-8].time + minutes(10))
    assert view.current and view.forecast is not None
    fc = view.forecast
    assert fc.now >= fc.reading
    assert fc.low <= fc.median <= fc.high
    # the true value at the end of the readings is inside a generous range
    assert fc.low - 10 <= ss[-1].used_percentage <= fc.high + 10
    shown = view.shown[-1]
    assert shown.by_project[0][0] == project_label("-home-user-alpha") and shown.usd > 0
    assert shown.by_project[0][2] is not None
    # after the reset there is no forecast, but the history stays
    later = analyze(index, TABLE, ss, H5, now=last_reset + minutes(5))
    assert not later.current and later.forecast is None
    assert later.reason == "no reading in the current window"
    del now


def test_seed_from_rate_limit_errors(tmp_path: Path) -> None:
    reset = 1_791_000_000

    def ev(i: int, **attrs: object) -> Event:
        return Event(EventKind.API_ERROR, "s1", None, T0 + minutes(i), f"k{i}", attrs)  # type: ignore[arg-type]

    index = FakeIndex(
        events=[
            ev(1, status=429, error="rate_limit", rate_limit_type="seven_day", resets_at=reset),
            ev(2, status=429, error="rate_limit"),  # does not say which window
            ev(3, status=None, error="EAI_AGAIN"),
            ev(4, status=429, error="rate_limit", window="five_hour"),
        ]
    )
    found = seed_samples(index)
    assert found.errors == 3 and found.unknown_window == 1
    assert [(s.window, s.used_percentage, s.source) for s in found.samples] == [
        (D7, 100.0, "rate_limit_error"),
        (H5, 100.0, "rate_limit_error"),
    ]
    assert found.samples[0].resets_at is not None
    assert found.samples[0].resets_at.timestamp() == reset
    added = append_samples(tmp_path, found.samples)
    assert len(added) == 2
    assert append_samples(tmp_path, found.samples) == []  # a second seed adds nothing
    assert load_samples(samples_path(tmp_path)) == found.samples


def test_stale_window_readings_make_no_segment() -> None:
    r1 = T0 + timedelta(hours=2)
    r2 = r1 + timedelta(hours=5)
    ss = [
        sample(T0, 70, resets=r1),
        sample(r1 + minutes(10), 5, resets=r2),
        sample(r1 + minutes(20), 80, resets=r1),  # an idle session showing the old window
        sample(r1 + minutes(30), 9, resets=r2),
        sample(r1 + minutes(40), 90, resets=r1 - minutes(30)),  # resets before its time
        sample(r1 + minutes(50), 12, resets=r2),
    ]
    segs = segments(ss, H5)
    assert len(segs) == 2
    assert [s.used_percentage for s in segs[1].samples] == [5, 9, 12]


def test_card_reading_is_the_current_windows_last() -> None:
    index, ss = synthetic(2)
    stale = sample(ss[-1].time + minutes(5), 3, resets=ss[-1].resets_at)  # lower: dropped
    view = analyze(index, TABLE, [*ss, stale], H5, now=stale.time + minutes(1))
    assert view.reading is not None and view.reading == ss[-1]


def test_seed_ignores_implausible_reset_times() -> None:
    def ev(i: int, resets: object) -> Event:
        attrs = {"error": "rate_limit", "rate_limit_type": "five_hour", "resets_at": resets}
        return Event(EventKind.API_ERROR, "s1", None, T0 + minutes(i), f"k{i}", attrs)  # type: ignore[arg-type]

    found = seed_samples(FakeIndex(events=[ev(1, -1e20), ev(2, 1e20), ev(3, 1e10 * 9)]))
    assert len(found.samples) == 3
    assert [s.resets_at for s in found.samples[:2]] == [None, None]


def test_few_intervals_give_no_forecast_range() -> None:
    index, ss = synthetic(1)
    few = ss[:4]
    view = analyze(index, TABLE, few, H5, now=few[-1].time + minutes(1))
    assert view.fit is not None and view.fit.intervals < 6
    assert view.forecast is None
    assert view.reason is not None and "needs 6" in view.reason
