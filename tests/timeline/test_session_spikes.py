"""Session-scope spikes on synthetic sessions: subagent fan-out across threads."""

from __future__ import annotations

from datetime import datetime, timedelta

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline import find_spikes
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


def fan_out(at_min: float = 40, spawns: int = 5) -> list[Request]:
    """A main thread at $0.05 a minute, then `spawns` subagents at once, each at
    $0.10 a minute for 12 minutes."""
    main = run(None, 0, 80)
    subs = [r for k in range(spawns) for r in run(f"a{k}", at_min + 0.1 * k, 12, every=0.5)]
    return main + subs


def spikes(
    reqs: list[Request], since: datetime = WINDOW[0], until: datetime = WINDOW[1]
) -> list[Spike]:
    return find_spikes(FakeIndex(reqs=reqs), TABLE, since, until)


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
