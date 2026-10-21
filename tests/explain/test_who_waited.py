"""Cache expiries named by who was waiting, parallel subagents' own work, one context
threshold family, and the spikes view's labels (synthetic sessions)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from capcom.explain import explain, shared
from capcom.explain.cli import format_spikes
from capcom.explain.rules import _GROWTH_ADVICE, _LATE_COMPACTION_ADVICE
from capcom.types import CauseKind, Confidence, Explanation, Spike, SpikeKind
from tests.explain.helpers import TABLE, World, at, check_claims, cost, spike


def run(w: World, s: Spike) -> list[Explanation]:
    check_claims(w, s)
    out = explain(w.index, TABLE, [s])
    assert sum(e.excess_usd for e in out) == pytest.approx(s.excess_usd)
    return out


def first(out: list[Explanation]) -> Explanation:
    return out[0]


def late_result(w: World, minutes: float) -> None:
    """Move the last tool result to `minutes`: the call ran that long."""
    w.index.evs[-1] = replace(w.index.evs[-1], timestamp=at(minutes))


def session_spike(w: World, start: float, end: float, baseline: float = 0.0) -> Spike:
    reqs = list(w.index.requests(since=at(start), until=at(end)))
    return Spike(
        kind=SpikeKind.SLOPE_CHANGE,
        start=at(start),
        end=at(end),
        cost_usd=sum(cost(r) for r in reqs),
        baseline_usd=baseline,
        session_id="s1",
        message_ids=tuple(r.message_id for r in reqs),
        scope="session",
    )


# --------------------------------------------------------------------------- expiry_kind


@pytest.mark.parametrize(
    ("gap", "ran", "subagent", "want"),
    [
        (4, None, False, None),  # within the lifetime: never an expiry
        (4, 4, True, None),
        (12, 11, True, shared.TOOL_WAIT_EXPIRY),
        (12, 11, False, shared.TOOL_WAIT_EXPIRY),
        (12, 1, True, shared.LATE_RESUME_EXPIRY),  # the tool came back in time
        (12, None, True, shared.LATE_RESUME_EXPIRY),
        (12, None, False, shared.IDLE_EXPIRY),
    ],
)
def test_expiry_kind(gap: int, ran: int | None, subagent: bool, want: str | None) -> None:
    m = timedelta(minutes=1)
    got = shared.expiry_kind(gap * m, 5 * m, None if ran is None else ran * m, subagent)
    assert got == want
    if want is not None:
        assert shared.EXPIRY_SUB_KINDS[shared.EXPIRY_CAUSES[want]] == want


# --------------------------------------------------------------------------- who waited


def test_main_thread_waiting_on_its_own_tool() -> None:
    w = World()
    w.req(0, cache_write_5m=200_000)
    w.call(1, "Bash", "make", 10, cache_read=200_000)
    late_result(w, 20)
    r = w.req(20.1, cache_write_5m=200_000)
    e = first(run(w, spike([r])))
    assert e.cause is CauseKind.TOOL_WAIT_EXPIRY
    assert e.title == "Cache expired while Bash ran 19 min"
    assert "background" in e.advice and "permission prompt" in e.advice
    assert "break" not in e.advice


def test_tool_waits_in_a_group_name_one_tool_once() -> None:
    w = World()
    w.agent("a1", "coder", "tu_a")
    w.req(0, agent="a1", cache_write_5m=150_000)
    w.call(1, "Bash", "until", 10, agent="a1", cache_read=150_000)
    late_result(w, 11)
    r1 = w.req(11.05, agent="a1", cache_write_5m=150_000)
    w.call(12, "Bash", "timeout", 10, agent="a1", cache_read=150_000)
    late_result(w, 22)
    r2 = w.req(22.05, agent="a1", cache_write_5m=150_000)
    e = first(run(w, spike([r1, r2], agent="a1", kind=SpikeKind.SLOPE_CHANGE)))
    assert e.cause is CauseKind.TOOL_WAIT_EXPIRY
    assert e.title == "Cache expired 2 times while Bash ran"
    text = " ".join([e.title, e.summary, *(ev.label for ev in e.evidence)])
    assert "until" not in text and "timeout" not in text
    # one line per wait
    assert [ev.message_id for ev in e.evidence] == [r1.message_id, r2.message_id]
    assert all("while its Bash call ran" in ev.label for ev in e.evidence)


def test_user_away_on_five_minute_writes_gets_the_one_hour_note() -> None:
    w = World()
    p = w.req(0, cache_write_5m=100_000)
    w.api(p, cache_ttl="5m")
    r = w.req(40, cache_write_5m=100_000)
    w.api(r, cache_ttl="5m")
    e = first(run(w, spike([r])))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert shared.EXPIRY_SUB_KINDS[e.cause] == "idle_expiry"
    assert e.title == "Cache rebuilt after 40 min idle"
    assert "1-hour cache lifetime" in e.advice and "handoff" in e.advice


def test_user_away_on_one_hour_writes_has_no_one_hour_note() -> None:
    w = World()
    p = w.req(0, cache_write_1h=100_000)
    w.api(p, cache_ttl="1h")
    r = w.req(90, cache_write_1h=100_000)
    w.api(r, cache_ttl="1h")
    e = first(run(w, spike([r])))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert "1-hour" not in e.advice


def test_subagent_tool_back_in_time_then_resumed_late() -> None:
    # the tool came back within the lifetime; the agent had finished and the lead
    # came back to it 30 minutes later
    w = World()
    w.agent("a1", "coder", "tu_a")
    w.req(0, agent="a1", cache_write_5m=80_000)
    w.call(1, "Bash", "ls", 10, agent="a1", cache_read=80_000)
    r = w.req(31, agent="a1", cache_write_5m=80_000)
    e = first(run(w, spike([r], agent="a1")))
    assert e.cause is CauseKind.LATE_RESUME_EXPIRY
    assert "came when its lead resumed it 30 min after it had finished" in e.summary


def test_a_gap_within_the_lifetime_is_never_an_expiry() -> None:
    w = World()
    w.agent("a1", "coder", "tu_a")
    p = w.req(0, agent="a1", cache_write_1h=80_000)
    w.api(p, cache_ttl="1h")
    r = w.req(30, agent="a1", cache_write_1h=80_000)
    w.api(r, cache_ttl="1h")
    out = run(w, spike([r], agent="a1"))
    assert not {e.cause for e in out} & set(shared.EXPIRY_SUB_KINDS)


# --------------------------------------------------------------------------- parallel work


def _busy_agent(w: World, agent: str, start: float, turns: int) -> None:
    w.agent(agent, "coder", f"tu_{agent}")
    w.req(start, agent=agent, cache_write_5m=20_000)
    for j in range(1, turns):
        w.req(start + j, agent=agent, cache_read=20_000 + 2_000 * j, cache_write_5m=2_000)


def test_many_subagents_at_once_are_parallel_work() -> None:
    w = World()
    for k in range(6):
        _busy_agent(w, f"c{k}", 10 + 2 * k, 20)  # starts spread out: no fan-out burst
    s = session_spike(w, 10, 40)
    out = run(w, s)
    e = next(e for e in out if e.cause is CauseKind.PARALLEL_SUBAGENTS)
    assert e.confidence is Confidence.LIKELY
    assert e.title == "Parallel subagents' work, up to 6 at once"
    assert "fewer subagents at once" in e.advice
    assert not any(x.cause is CauseKind.UNEXPLAINED for x in out)


def test_subagents_one_after_another_are_not_parallel() -> None:
    w = World()
    for k in range(5):
        _busy_agent(w, f"c{k}", 10 + 6 * k, 5)
    out = run(w, session_spike(w, 10, 45))
    assert not any(e.cause is CauseKind.PARALLEL_SUBAGENTS for e in out)


def test_parallel_work_needs_a_session_spike() -> None:
    w = World()
    for k in range(6):
        _busy_agent(w, f"c{k}", 10 + 2 * k, 20)
    reqs = [r for r in w.index.reqs if r.agent_id == "c0"]
    out = run(w, spike(reqs, agent="c0", kind=SpikeKind.SLOPE_CHANGE))
    assert not any(e.cause is CauseKind.PARALLEL_SUBAGENTS for e in out)


# --------------------------------------------------------------------------- text


def test_one_context_threshold_family_in_advice() -> None:
    for text in (_GROWTH_ADVICE, _LATE_COMPACTION_ADVICE):
        assert "150k" in text and "100k" not in text and "200k" not in text
    assert "400k" in _LATE_COMPACTION_ADVICE
    assert shared.GROWTH_FLOOR < shared.LONG_CONTEXT < shared.LATE_COMPACTION


def test_spikes_view_labels_are_not_doubled() -> None:
    w = World()
    p = w.req(0, cache_write_1h=100_000)
    w.api(p, cache_ttl="1h")
    r = w.req(90, cache_write_1h=100_000, output=30_000)
    w.api(r, cache_ttl="1h")
    plain = w.req(200, cache_read=10_000, output=5_000)
    text = format_spikes(
        explain(w.index, TABLE, [spike([r]), spike([plain], baseline=0.001)]), 5, {}, {}
    )
    assert "nexplained, unexplained" not in text and ", clear cause" not in text
    assert "Unexplained: $" in text
    assert "Idle past cache lifetime: $" in text and "(clear cause)" in text
