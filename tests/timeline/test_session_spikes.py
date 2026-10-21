"""Session-scope spikes on synthetic sessions: subagent fan-out across threads."""

from __future__ import annotations

from datetime import datetime, timedelta

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline import find_spikes
from capcom.timeline.spikes import SpikeParams, Turn, session_buckets
from capcom.types import Request, Spike, SpikeKind
from tests.cost.fakes import FakeIndex, at, req

TABLE = load_prices()
WINDOW = (at(-60), at(24 * 60))

# claude-opus-5-5: cache reads $0.2/M, 1h writes $8/M, output $20/M
CHEAP = {"cache_read": 250_000}  # $0.05
REWRITE = {"cache_write_1h": 300_000}  # $2.40


def run(
    agent: str | None, start: float, minutes: float, every: float = 1.0, **kw: int
) -> list[Request]:
    """A thread spending `kw` (default CHEAP) every `every` minutes for `minutes`."""
    usage = kw or CHEAP
    n = round(minutes / every)
    return [req(agent=agent, when=at(start + k * every), **usage) for k in range(n)]


def fan_out(at_min: float = 40, spawns: int = 5, main_from: float = 0) -> list[Request]:
    """A main thread at $0.05 a minute, then `spawns` subagents at once, each at
    $0.10 a minute for 12 minutes."""
    main = run(None, main_from, 80)
    subs = [r for k in range(spawns) for r in run(f"a{k}", at_min + 0.1 * k, 12, every=0.5)]
    return main + subs


def spikes(
    reqs: list[Request],
    since: datetime = WINDOW[0],
    until: datetime = WINDOW[1],
    params: SpikeParams | None = None,
) -> list[Spike]:
    ix = FakeIndex(reqs=reqs)
    if params is None:
        return find_spikes(ix, TABLE, since, until)
    return find_spikes(ix, TABLE, since, until, params=params)


def by_scope(found: list[Spike], scope: str) -> list[Spike]:
    return [s for s in found if s.scope == scope]


def test_fan_out_burst_is_one_session_spike() -> None:
    reqs = fan_out()
    found = spikes(reqs)
    assert by_scope(found, "thread") == []  # new subagents have no history of their own
    [s] = by_scope(found, "session")
    assert s.kind is SpikeKind.SLOPE_CHANGE
    assert s.session_id == "s1" and s.agent_id is None
    first_sub = min(r.timestamp for r in reqs if r.agent_id)
    assert at(39) < s.start <= first_sub < s.end
    # its requests are the session's in the span, across threads, in time order
    ordered = sorted(reqs, key=lambda r: r.timestamp)
    inside = [r for r in ordered if s.start <= r.timestamp < s.end]
    assert list(s.message_ids) == [r.message_id for r in inside]
    assert {r.agent_id for r in inside} >= {None, "a0", "a4"}
    assert abs(s.cost_usd - sum(price(r, TABLE).total for r in inside)) < 1e-9
    # the old rate: $0.10 per 2-minute bucket
    buckets = (s.end - s.start) / timedelta(minutes=2)
    assert abs(s.baseline_usd - 0.10 * round(buckets + 0.5)) < 0.11
    assert s.excess_usd > 3.0


def test_steady_orchestrator_has_no_session_spike() -> None:
    # a main thread and two subagents at any time: each runs 30 minutes, a new one
    # starts every 15 minutes, so threads come and go but the rate stays put
    reqs = run(None, 0, 150)
    for k in range(9):
        reqs += run(f"a{k}", 15 * k + 0.3, 30, every=0.5)
    assert spikes(reqs) == []


def test_one_thread_speeding_up_is_not_a_session_spike() -> None:
    # the main thread alone goes 4x as fast: a pace change of one thread, not fan-out
    slow = run(None, 0, 60, every=2, cache_read=2_000_000)
    fast = run(None, 60, 20, every=0.5, cache_read=2_000_000)
    assert by_scope(spikes(slow + fast), "session") == []


def test_few_spawns_on_a_busy_session_are_not_a_spike() -> None:
    # one more subagent on a session already spending $1 a minute
    busy = run(None, 0, 80, cache_read=5_000_000)
    assert spikes(busy + run("a0", 40, 12, every=0.5)) == []


def test_a_thread_spike_is_not_restated_by_the_session() -> None:
    # the main thread's spend rises 40x and stays up, beside a steady subagent, as a
    # new subagent starts: the main thread's slope change explains over 80 % of the
    # session's rise, so the rise is reported once, as the thread's
    step = {"cache_read": 10_000_000}  # $2.00
    reqs = run(None, 0, 40) + run(None, 40, 30, **step) + run("a0", 0.5, 70)
    reqs += run("a1", 40.2, 30, every=0.5, cache_read=1_000_000)  # $0.40 a minute
    found = spikes(reqs)
    assert [(s.scope, s.kind, s.agent_id) for s in found] == [
        ("thread", SpikeKind.SLOPE_CHANGE, None)
    ]


def test_a_thread_spike_inside_a_fan_out_is_counted_once() -> None:
    plain = fan_out()
    swapped = list(plain)
    k = next(i for i, r in enumerate(plain) if r.agent_id is None and r.timestamp == at(44))
    swapped[k] = req(when=at(44), **REWRITE)  # a main-thread cache rewrite mid-burst
    before = by_scope(spikes(plain), "session")
    found = spikes(swapped)
    [single] = by_scope(found, "thread")
    [session] = by_scope(found, "session")
    assert single.kind is SpikeKind.SINGLE_REQUEST
    assert single.message_ids[0] in session.message_ids
    # the rewrite's excess belongs to the thread spike: the session's is unchanged
    assert abs(before[0].excess_usd - session.excess_usd) < 1e-6
    assert abs(session.cost_usd - before[0].cost_usd - (2.40 - 0.05)) < 1e-6


def test_session_look_ahead_reads_past_until() -> None:
    reqs = fan_out()
    [s] = by_scope(spikes(reqs), "session")
    # the burst starts just before `until`: its window is read from past `until`
    got = by_scope(spikes(reqs, until=s.start + timedelta(seconds=30)), "session")
    assert [(g.start, g.message_ids) for g in got] == [(s.start, s.message_ids)]
    assert max(r.timestamp for r in reqs if r.message_id in got[0].message_ids) > s.start
    # a spike starting at `until` belongs to the next window, and the baseline
    # before `since` still counts
    assert by_scope(spikes(reqs, until=s.start), "session") == []
    got = by_scope(spikes(reqs, since=s.start), "session")
    assert [g.start for g in got] == [s.start]


def test_a_rewrite_in_the_baseline_does_not_hide_a_fan_out() -> None:
    # one main-thread cache rewrite before the burst counts at its thread's median
    # in the session's rates, so it does not lift the baseline over the burst
    for tokens in (1_000_000, 2_000_000, 4_000_000):  # $8, $16, $32
        reqs = fan_out()
        k = next(i for i, r in enumerate(reqs) if r.agent_id is None and r.timestamp == at(30))
        reqs[k] = req(when=at(30), cache_write_1h=tokens)
        found = spikes(reqs)
        assert [s.message_ids for s in by_scope(found, "thread")] == [(reqs[k].message_id,)]
        [s] = by_scope(found, "session")
        assert at(39) < s.start <= at(40.1)
        assert s.baseline_usd < 1.5  # the old rate: about $0.10 a bucket


def test_a_fan_out_under_the_dollar_floor_is_not_a_spike() -> None:
    # two subagents: 5x the rate, but $2 above the old one over the window
    assert spikes(fan_out(spawns=2)) == []


def test_a_short_baseline_is_enough() -> None:
    # a fan-out 7 minutes into a session: a 3-bucket baseline is all it has
    reqs = fan_out(at_min=7)
    assert len(by_scope(spikes(reqs), "session")) == 1
    assert by_scope(spikes(reqs, params=SpikeParams(session_min_baseline=10)), "session") == []


def test_long_pauses_are_cut_out() -> None:
    # a pause up to idle_cap keeps its empty 2-minute buckets; a longer one is cut
    reqs = [*run(None, 0, 1), *run(None, 4.5, 1), *run(None, 60, 1)]
    turns = [Turn(r.timestamp, r.message_id, 0.05, r.agent_id) for r in reqs]
    buckets = session_buckets(turns)
    assert [b.hi - b.lo for b in buckets] == [1, 0, 1, 1]
