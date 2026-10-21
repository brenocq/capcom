"""Session-scope spikes: fan-out counted as new threads (with or without an Agent
call), other rules per thread through one ledger, and the cap at the spike's excess."""

from __future__ import annotations

import pytest

from capcom.explain import explain
from capcom.types import (
    AgentInfo,
    CauseKind,
    Confidence,
    EventKind,
    Explanation,
    Request,
    Spike,
    SpikeKind,
)
from tests.explain.helpers import TABLE, World, at, check_claims, cost, prefix


def session_spike(w: World, start: float, end: float, baseline: float = 0.0) -> Spike:
    """A session spike over [start, end) minutes: every request of every thread in it."""
    reqs = [r for r in w.index.requests(since=at(start), until=at(end))]
    return Spike(
        kind=SpikeKind.SLOPE_CHANGE,
        start=at(start),
        end=at(end),
        cost_usd=sum(cost(r) for r in reqs),
        baseline_usd=baseline,
        session_id="s1",
        agent_id=None,
        message_ids=tuple(r.message_id for r in reqs),
        scope="session",
    )


def run(w: World, s: Spike) -> list[Explanation]:
    check_claims(w, s)
    out = explain(w.index, TABLE, [s])
    assert sum(e.excess_usd for e in out) <= s.excess_usd + 1e-12
    return out


def by_cause(out: list[Explanation]) -> dict[CauseKind, Explanation]:
    return {e.cause: e for e in out}


def has(e: Explanation, text: str) -> bool:
    return any(text in ev.label for ev in e.evidence)


def steady_main(w: World, until: float) -> list[Request]:
    """A main thread with one cheap turn a minute from minute 0."""
    w.req(0, cache_write_5m=40_000)
    return [w.req(k, cache_read=40_000) for k in range(1, int(until))]


def new_agent(w: World, agent: str, minute: float) -> list[Request]:
    """A short subagent: a first request that writes its prefix, then one lookup."""
    return [
        w.req(minute, agent=agent, cache_write_5m=30_000),
        w.req(minute + 0.5, agent=agent, cache_read=30_000, tools=[("Bash", "ls")]),
    ]


def test_spawned_fan_out_counts_only_new_threads() -> None:
    w = World()
    main = steady_main(w, 20)
    # an agent already running before the span is not new
    old = [w.req(2 + k, agent="old", cache_read=20_000) for k in range(16)]
    w.agent("old", "coder", "toolu_old")
    call = w.req(10, cache_read=40_000, tools=[("Agent", None)] * 6)
    types = ["coder", "coder", "scout", "coder", "scout", "coder"]
    new: list[Request] = []
    for k, (tc, kind) in enumerate(zip(call.tool_calls, types, strict=True)):
        w.event(EventKind.SPAWN, 10.05, tool_use_id=tc.tool_use_id, agent_type=kind)
        w.agent(f"a{k}", kind, tc.tool_use_id)
        new += new_agent(w, f"a{k}", 10.5 + 0.4 * k)
    in_span = [r for r in [*main, *old, call] if at(10) <= r.timestamp < at(20)]
    s = session_spike(w, 10, 20, baseline=sum(cost(r) for r in in_span))
    found = by_cause(run(w, s))
    assert set(found) == {CauseKind.FAN_OUT, CauseKind.UNEXPLAINED}  # fan-out and the rest
    e = found[CauseKind.FAN_OUT]
    # only the prefixes: each new agent's first request's cache writes
    assert e.excess_usd == pytest.approx(sum(prefix(r) for r in new[::2]))
    assert e.confidence is Confidence.CLEAR
    assert e.title == "6 new subagents in 2 min"
    assert has(e, "6 new subagents in 2 min: coder \u00d74, scout \u00d72")
    assert has(e, "6 from Agent calls")
    assert has(e, "Each first request wrote about 30k tokens")
    assert "Agent call" not in e.advice and "workflow" not in e.advice


def test_workflow_fan_out_without_spawn() -> None:
    w = World()
    main = steady_main(w, 20)
    new: list[Request] = []
    for k in range(5):
        if k < 4:  # the fifth has no meta.json at all
            w.index.agent_infos.append(AgentInfo(f"wf{k}", "s1", agent_type="workflow-subagent"))
        new += new_agent(w, f"wf{k}", 12 + 0.7 * k)
    in_span = [r for r in main if r.timestamp >= at(10)]
    s = session_spike(w, 10, 20, baseline=sum(cost(r) for r in in_span))
    e = by_cause(run(w, s))[CauseKind.FAN_OUT]
    assert e.excess_usd == pytest.approx(sum(prefix(r) for r in new[::2]))
    assert e.title == "5 new subagents in 3 min"
    assert has(e, "workflow-subagent \u00d74, unknown type \u00d71")
    assert has(e, "4 from a workflow, without an Agent call; 1 without an Agent call")
    assert "workflow" in e.advice


def test_spawns_without_meta_are_matched_by_time() -> None:
    w = World()
    steady_main(w, 20)
    call = w.req(10, cache_read=40_000, tools=[("Agent", None)] * 4)
    for k, tc in enumerate(call.tool_calls):
        w.event(EventKind.SPAWN, 10.05 + k, tool_use_id=tc.tool_use_id, agent_type="scout")
        new_agent(w, f"a{k}", 10.5 + k)
    e = by_cause(run(w, session_spike(w, 10, 20)))[CauseKind.FAN_OUT]
    assert has(e, "4 new subagents in 3 min: scout \u00d74")
    assert has(e, "4 from Agent calls")


def test_two_threads_each_doubling_is_not_fan_out() -> None:
    w = World()
    w.req(0, cache_write_5m=40_000)
    w.req(0.2, agent="b", cache_write_5m=30_000)
    w.agent("b", "coder", "toolu_b")
    for k in range(1, 30):
        n = 2 if k >= 20 else 1  # both threads twice as busy from minute 20
        for j in range(n):
            w.req(k + j / 2, cache_read=40_000)
            w.req(k + j / 2 + 0.1, agent="b", cache_read=30_000)
    out = run(w, session_spike(w, 20, 30, baseline=0.5))
    assert all(e.cause is not CauseKind.FAN_OUT for e in out)


def test_three_new_threads_are_not_fan_out() -> None:
    w = World()
    steady_main(w, 20)
    for k in range(3):
        new_agent(w, f"a{k}", 11 + k)
    out = run(w, session_spike(w, 10, 20))
    assert all(e.cause is not CauseKind.FAN_OUT for e in out)


def test_fan_out_is_capped_at_the_spike_excess() -> None:
    w = World()
    steady_main(w, 20)
    new: list[Request] = []
    for k in range(8):
        w.index.agent_infos.append(AgentInfo(f"wf{k}", "s1", agent_type="workflow-subagent"))
        new += new_agent(w, f"wf{k}", 11 + 0.3 * k)
    s = session_spike(w, 10, 20)
    agents = sum(cost(r) for r in new)
    # a baseline that leaves less than the new threads cost
    s = Spike(**{f: getattr(s, f) for f in s.__slots__} | {"baseline_usd": s.cost_usd - agents / 2})
    out = run(w, s)
    e = by_cause(out)[CauseKind.FAN_OUT]
    assert e.excess_usd == pytest.approx(s.excess_usd)
    assert e.excess_usd < agents
    assert sum(x.excess_usd for x in out) == pytest.approx(s.excess_usd)


def test_other_rules_run_per_thread_in_a_session_spike() -> None:
    w = World()
    w.req(0, cache_write_5m=150_000)
    w.req(1, cache_read=150_000)
    idle = w.req(40, cache_write_5m=150_000)  # the main thread comes back past 5 minutes
    new: list[Request] = []
    for k in range(4):
        w.index.agent_infos.append(AgentInfo(f"wf{k}", "s1", agent_type="workflow-subagent"))
        new += new_agent(w, f"wf{k}", 40.5 + 0.5 * k)
    found = by_cause(run(w, session_spike(w, 40, 50)))
    assert found[CauseKind.IDLE_REWRITE].excess_usd > 0
    assert found[CauseKind.FAN_OUT].excess_usd == pytest.approx(sum(prefix(r) for r in new[::2]))
    assert idle.message_id not in {ev.message_id for ev in found[CauseKind.FAN_OUT].evidence}


def test_session_spike_is_labeled_in_the_spikes_view() -> None:
    from capcom.explain.cli import format_spikes

    w = World()
    steady_main(w, 20)
    for k in range(4):
        new_agent(w, f"a{k}", 11 + k)
    s = session_spike(w, 10, 20)
    text = format_spikes(explain(w.index, TABLE, [s]), 5, {"s1": "-home-user-proj"}, {})
    assert "(session-wide)" in text.splitlines()[2]


def test_parallel_verifiers_are_charged_their_prefixes_only() -> None:
    # eleven agents of one type started from one turn, each writing a 43k prefix, then
    # working through a dozen tool calls on a context that grows to about 100k
    w = World()
    main = steady_main(w, 30)
    call = w.req(10, cache_read=40_000, tools=[("Agent", None)] * 11)
    firsts: list[Request] = []
    work: list[Request] = []
    for k, tc in enumerate(call.tool_calls):
        w.event(EventKind.SPAWN, 10.02, tool_use_id=tc.tool_use_id, agent_type="paper-verifier")
        w.agent(f"v{k}", "paper-verifier", tc.tool_use_id)
        firsts.append(w.req(10.1 + 0.05 * k, agent=f"v{k}", cache_write_5m=43_000))
        ctx = 43_000
        for j in range(12):
            work.append(
                w.req(
                    11 + k * 0.1 + j,
                    agent=f"v{k}",
                    cache_read=ctx,
                    cache_write_5m=5_000,
                    output=800,
                    tools=[("Read", f"paper/sec{j}.tex")],
                )
            )
            ctx += 5_000
    in_span = [r for r in [*main, call] if at(10) <= r.timestamp < at(30)]
    s = session_spike(w, 10, 30, baseline=sum(cost(r) for r in in_span))
    out = run(w, s)
    found = by_cause(out)
    e = found[CauseKind.FAN_OUT]
    assert e.title == "11 new subagents in under a minute"
    assert e.excess_usd == pytest.approx(sum(prefix(r) for r in firsts))
    assert e.excess_usd < 0.25 * sum(cost(r) for r in [*firsts, *work])
    assert has(e, "0 of them made two tool calls or fewer")
    assert "in the parent" not in e.advice and "fewer of them at once" in e.advice
    # the agents' own work goes to the other rules or to the unexplained rest, and every
    # dollar of the excess is in one explanation
    assert sum(x.excess_usd for x in out) == pytest.approx(s.excess_usd)
    assert set(found) == {CauseKind.FAN_OUT, CauseKind.CONTEXT_GROWTH, CauseKind.UNEXPLAINED}


def test_spikes_view_totals_by_cause_add_up() -> None:
    from capcom.explain.cli import format_spikes

    w = World()
    steady_main(w, 20)
    for k in range(4):
        new_agent(w, f"a{k}", 11 + k)
    w.req(25, cache_write_5m=40_000)  # the main thread back past its 5-minute cache
    spikes = [session_spike(w, 10, 20), session_spike(w, 25, 26)]
    text = format_spikes(explain(w.index, TABLE, spikes), 5, {"s1": "-home-user-proj"}, {})
    rows = text.split("Excess by cause\n")[1].splitlines()
    by_cause = sum(float(row.split("$")[1].replace(",", "")) for row in rows)
    assert by_cause == pytest.approx(sum(s.excess_usd for s in spikes), abs=0.01 * len(rows))
    assert "2 spikes; 2 matched to a cause" in text


def test_small_fan_out_shows_beside_growth_and_the_rest() -> None:
    # eleven verifiers whose prefixes are under a tenth of a spike
    # mostly made of their own long work
    w = World()
    main = steady_main(w, 60)
    call = w.req(10, cache_read=40_000, tools=[("Agent", None)] * 11)
    firsts: list[Request] = []
    for k, tc in enumerate(call.tool_calls):
        w.event(EventKind.SPAWN, 10.02, tool_use_id=tc.tool_use_id, agent_type="paper-verifier")
        w.agent(f"v{k}", "paper-verifier", tc.tool_use_id)
        firsts.append(w.req(10.1 + 0.05 * k, agent=f"v{k}", cache_write_5m=43_000))
        ctx = 43_000
        for j in range(40):
            w.req(11 + k * 0.02 + j, agent=f"v{k}", cache_read=ctx, cache_write_5m=4_000,
                  tools=[("Read", f"paper/sec{j}.tex")])  # fmt: skip
            ctx += 4_000
    in_span = [r for r in [*main, call] if at(10) <= r.timestamp < at(60)]
    s = session_spike(w, 10, 60, baseline=sum(cost(r) for r in in_span))
    out = run(w, s)
    found = by_cause(out)
    fan = found[CauseKind.FAN_OUT]
    assert fan.excess_usd == pytest.approx(sum(prefix(r) for r in firsts))
    assert fan.excess_usd < 0.1 * s.excess_usd
    assert fan.confidence is Confidence.CLEAR
    assert set(found) == {CauseKind.FAN_OUT, CauseKind.CONTEXT_GROWTH, CauseKind.UNEXPLAINED}
    assert sum(e.excess_usd for e in out) == pytest.approx(s.excess_usd)


def test_spikes_view_uses_the_shared_project_names() -> None:
    from pathlib import Path

    from capcom.explain.cli import format_spikes
    from capcom.timeline.groups import ProjectNames

    w = World()
    steady_main(w, 20)
    for k in range(4):
        new_agent(w, f"a{k}", 11 + k)
    s = session_spike(w, 10, 20)
    names = ProjectNames.build(
        [("-home-u-Github-proj", "/home/u/Github/proj")],
        openclaw_root=Path("/home/u/.openclaw"),
        home=Path("/home/u"),
    )
    exps = explain(w.index, TABLE, [s])
    text = format_spikes(exps, 5, {"s1": "-home-u-Github-proj"}, {}, names)
    assert "proj (session-wide)" in text and "-home-u-Github-proj" not in text
