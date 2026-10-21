"""Spike detection on synthetic threads: both kinds, with negative cases."""

from __future__ import annotations

from datetime import timedelta

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline import find_spikes
from capcom.timeline.spikes import SpikeParams
from capcom.types import Request, SpikeKind
from tests.cost.fakes import T0, FakeIndex, at, req

TABLE = load_prices()
WINDOW = (T0 - timedelta(hours=1), T0 + timedelta(days=1))

# claude-opus-5-5: cache reads $0.2/M, 1h writes $8/M, output $20/M
CHEAP = {"cache_read": 250_000}  # $0.05
DEAR = {"cache_read": 250_000, "output": 7_500}  # $0.20
REWRITE = {"cache_write_1h": 300_000}  # $2.40


def thread(
    costs: list[dict[str, int]],
    session: str = "s1",
    agent: str | None = None,
    start: float = 0,
    every: float = 1,
) -> list[Request]:
    return [
        req(session=session, agent=agent, when=at(start + k * every), **u)
        for k, u in enumerate(costs)
    ]


def spikes(reqs: list[Request], **kw: object) -> list:  # type: ignore[type-arg]
    return find_spikes(FakeIndex(reqs=reqs), TABLE, *WINDOW, **kw)  # type: ignore[arg-type]


def test_single_request_spike() -> None:
    reqs = thread([CHEAP] * 25 + [REWRITE] + [CHEAP] * 10)
    found = spikes(reqs)
    assert len(found) == 1
    s = found[0]
    assert s.kind is SpikeKind.SINGLE_REQUEST
    assert s.message_ids == (reqs[25].message_id,)
    assert s.start == reqs[25].timestamp < s.end
    assert abs(s.cost_usd - price(reqs[25], TABLE).total) < 1e-9
    assert abs(s.baseline_usd - 0.05) < 1e-9
    assert s.session_id == "s1" and s.agent_id is None
    assert abs(s.excess_usd - (s.cost_usd - 0.05)) < 1e-9


def test_single_request_negatives() -> None:
    # too little history: the first requests of a thread are never spikes
    assert spikes(thread([CHEAP] * 3 + [REWRITE])) == []
    # under the ratio: 4x the median
    assert spikes(thread([DEAR] * 25 + [{"cache_read": 250_000, "output": 37_500}])) == []
    # over the ratio but under the absolute floor ($0.01 -> $0.10)
    tiny = {"cache_read": 50_000}
    assert spikes(thread([tiny] * 25 + [{"output": 5_000}])) == []
    # a placeholder reply is not a request
    synth = req(when=at(30), model="<synthetic>", output=10**7)
    assert spikes([*thread([CHEAP] * 25), synth]) == []


def test_median_is_per_thread() -> None:
    # the main thread is expensive, the subagent cheap: a subagent request at the
    # main thread's level is a spike only within its own thread
    main = thread([REWRITE] * 25)
    sub = thread([CHEAP] * 25 + [REWRITE], agent="a1", start=0.5)
    found = spikes(main + sub)
    assert [(s.kind, s.agent_id) for s in found] == [(SpikeKind.SINGLE_REQUEST, "a1")]


def test_slope_change() -> None:
    reqs = thread([CHEAP] * 30 + [DEAR] * 20)
    found = spikes(reqs)
    assert [s.kind for s in found] == [SpikeKind.SLOPE_CHANGE]
    s = found[0]
    assert s.start == reqs[30].timestamp  # where the rate changed
    assert s.message_ids[0] == reqs[30].message_id
    ids = [r.message_id for r in reqs]
    assert list(s.message_ids) == ids[30 : 30 + len(s.message_ids)]
    assert len(s.message_ids) >= 10
    assert abs(s.cost_usd - 0.20 * len(s.message_ids)) < 1e-9
    assert abs(s.baseline_usd - 0.05 * len(s.message_ids)) < 1e-9
    assert s.excess_usd > 1.0


def test_slope_change_from_pace() -> None:
    # same cost per request, but requests come 4x as often: the rate per active
    # minute rises (per_minute=True); per turn, the default, it does not
    slow = thread([DEAR] * 30, every=2)
    fast = thread([DEAR] * 20, start=58.5, every=0.5)
    found = spikes(slow + fast, params=SpikeParams(per_minute=True))
    assert [s.kind for s in found] == [SpikeKind.SLOPE_CHANGE]
    assert found[0].start == fast[0].timestamp
    assert spikes(slow + fast) == []


def test_slope_negatives() -> None:
    # a steady thread
    assert spikes(thread([DEAR] * 60)) == []
    # a rise that does not last: 3 expensive turns, then back
    assert spikes(thread([CHEAP] * 30 + [DEAR] * 3 + [CHEAP] * 20)) == []
    # a lasting rise under the dollar floor
    tiny_a, tiny_b = {"cache_read": 50_000}, {"cache_read": 150_000}
    assert spikes(thread([tiny_a] * 30 + [tiny_b] * 20)) == []
    # too short a thread for a baseline and a window
    assert spikes(thread([CHEAP] * 10 + [DEAR] * 10)) == []
    # an idle gap does not dilute the baseline into a fake rise
    before = thread([DEAR] * 30)
    after = thread([DEAR] * 20, start=30 + 240)
    assert spikes(before + after) == []


def test_single_request_is_not_also_a_slope_change() -> None:
    found = spikes(thread([CHEAP] * 30 + [{"cache_write_1h": 2_000_000}] + [CHEAP] * 20))
    assert [s.kind for s in found] == [SpikeKind.SINGLE_REQUEST]


def test_window_session_filter_and_order() -> None:
    s1 = thread([CHEAP] * 25 + [REWRITE], session="s1")
    s2 = thread([CHEAP] * 20 + [REWRITE], session="s2", start=0.25)
    found = spikes(s1 + s2)
    assert [s.session_id for s in found] == ["s2", "s1"]  # time order
    ix = FakeIndex(reqs=s1 + s2)
    assert [s.session_id for s in find_spikes(ix, TABLE, *WINDOW, session_id="s1")] == ["s1"]
    # a spike before `since` is not reported, but the history before it still counts
    since = s1[25].timestamp
    got = find_spikes(ix, TABLE, since, WINDOW[1])
    assert [s.session_id for s in got] == ["s1"]
    assert find_spikes(ix, TABLE, since, since) == []
    assert find_spikes(FakeIndex(), TABLE, *WINDOW) == []


def test_small_outliers_do_not_read_as_slope_changes() -> None:
    # rewrites under the dollar floor are not reported, and do not inflate a window
    small = {"cache_write_1h": 120_000}  # $0.96: 19x the median, excess under $1
    costs = [CHEAP] * 30 + [small] + [CHEAP] * 12 + [small] + [CHEAP] * 15
    assert spikes(thread(costs)) == []


def test_a_lasting_big_step_is_one_slope_change() -> None:
    # 8x per turn from turn 30 on: not ten single-request spikes but one slope change
    step = {"cache_read": 2_000_000}  # $0.40
    reqs = thread([CHEAP] * 30 + [step] * 20)
    found = spikes(reqs)
    assert [s.kind for s in found] == [SpikeKind.SLOPE_CHANGE]
    assert found[0].start == reqs[30].timestamp
