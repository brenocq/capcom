"""Each dollar is claimed once, the api-request record is picked by time, fan-out needs
a burst, and removed tokens are priced at the request's own rate."""

from __future__ import annotations

from dataclasses import replace

import pytest

from capcom.explain import explain
from capcom.types import CauseKind, EventKind, Explanation, Spike, SpikeKind
from tests.explain.helpers import LONG, TABLE, World, at, check_claims, cost, spike


def run(w: World, s: Spike) -> list[Explanation]:
    check_claims(w, s)
    out = explain(w.index, TABLE, [s])
    assert sum(e.excess_usd for e in out) <= s.excess_usd + 1e-12
    return out


def causes(out: list[Explanation]) -> dict[CauseKind, float]:
    return {e.cause: e.excess_usd for e in out}


# --------------------------------------------------------------------------- api-request pick


def test_the_next_turns_record_does_not_describe_a_reply() -> None:
    # the reviewer's case: the reply's own record 20 s before it, and the next turn's
    # record, with another shape hash, half a second after it
    w = World()
    p = w.req(0, cache_write_5m=100_000)
    w.api(p, seconds_before=20, shape_hash="H1", cache_ttl="5m")
    r = w.req(30, cache_write_5m=100_000)
    w.api(r, seconds_before=20, shape_hash="H1", cache_ttl="5m")
    w.api(r, seconds_before=-0.5, shape_hash="H2", cache_ttl="5m")
    out = run(w, spike([r]))
    assert out[0].cause is CauseKind.IDLE_REWRITE
    assert any("cacheControl ttl 5m" in ev.label for ev in out[0].evidence)


def test_a_record_for_another_model_is_skipped() -> None:
    # a side query on another model, with a 1-hour ttl, logged just before the reply
    w = World()
    p = w.req(0, cache_write_5m=100_000)
    w.api(p, seconds_before=10, cache_ttl="5m")
    w.api(p, seconds_before=1, model="claude-haiku-5-5", cache_ttl="1h")
    r = w.req(30, cache_write_5m=100_000)
    out = run(w, spike([r]))
    assert out[0].cause is CauseKind.IDLE_REWRITE
    assert any(
        "cache lifetime 5 minutes (cacheControl ttl 5m)" in ev.label for ev in out[0].evidence
    )


def test_a_subagent_record_does_not_describe_the_main_thread() -> None:
    w = World()
    p = w.req(0, cache_write_5m=100_000)
    w.api(p, seconds_before=10, cache_ttl="5m")
    w.api(p, seconds_before=1, query_source="agent:custom", cache_ttl="1h")
    r = w.req(30, cache_write_5m=100_000)
    assert run(w, spike([r]))[0].cause is CauseKind.IDLE_REWRITE


def test_a_record_too_long_before_is_not_the_reply() -> None:
    w = World()
    p = w.req(0, cache_read=100_000)  # reads only: its writes say nothing of the lifetime
    w.api(p, seconds_before=600, cache_ttl="1h")
    r = w.req(30, cache_write_5m=100_000)
    e = run(w, spike([r]))[0]
    assert e.cause is CauseKind.IDLE_REWRITE
    assert any("assumed: no cacheControl record" in ev.label for ev in e.evidence)


# --------------------------------------------------------------------------- fan-out


def test_one_spawn_an_hour_is_not_a_fan_out() -> None:
    # the reviewer's probe: an orchestrator starting one coder per hour
    w = World()
    reqs = []
    for h in range(4):
        m = w.req(h * 60, cache_read=60_000, tools=[("Agent", "coder")])
        tid = m.tool_calls[0].tool_use_id
        w.event(EventKind.SPAWN, h * 60 + 0.1, tool_use_id=tid, agent_type="coder")
        w.agent(f"a{h}", "coder", tid)
        reqs += [m, w.req(h * 60 + 1, agent=f"a{h}", cache_write_5m=30_000)]
        reqs += [
            w.req(h * 60 + 2 + k, agent=f"a{h}", cache_read=30_000 + 2_000 * k) for k in range(10)
        ]
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert all(e.cause is not CauseKind.FAN_OUT for e in out)


def test_fan_out_counts_only_the_burst() -> None:
    w = World()
    main = w.req(0, cache_read=50_000, tools=[("Agent", "scout")] * 4)
    reqs = [main]
    for k, call in enumerate(main.tool_calls):
        w.event(EventKind.SPAWN, 0.1, tool_use_id=call.tool_use_id, agent_type="scout")
        w.agent(f"a{k}", "scout", call.tool_use_id)
        reqs.append(w.req(1 + k, agent=f"a{k}", cache_write_5m=30_000))
    late = w.req(20, cache_read=50_000, tools=[("Agent", "coder")])
    w.event(EventKind.SPAWN, 20.1, tool_use_id=late.tool_calls[0].tool_use_id, agent_type="coder")
    w.agent("b", "coder", late.tool_calls[0].tool_use_id)
    lone = w.req(21, agent="b", cache_write_5m=30_000)
    reqs += [late, lone]
    by = causes(run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE)))
    assert by[CauseKind.FAN_OUT] == pytest.approx(sum(cost(r) for r in reqs[1:5]))


# --------------------------------------------------------------------------- one claim each


def test_every_rule_claims_from_what_the_others_left() -> None:
    w = World()
    w.req(0, cache_write_5m=150_000)
    w.req(1, cache_read=150_000, tools=[("Read", "/p/big.json")])
    w.event(EventKind.TOOL_RESULT, 1.1, tool="Read", target="/p/big.json", size_tokens=60_000)
    idle = w.req(40, cache_write_5m=210_000, output=6_000, thinking=5_000)  # past 5 min
    later = [w.req(41 + k, cache_read=210_000 + 80_000 * k) for k in range(5)]
    s = spike([idle, *later], kind=SpikeKind.SLOPE_CHANGE)
    out = run(w, s)
    by = causes(out)
    # the rewrite claims the 150k cached before the pause; the result's 60k written
    # with it are left to the result rule, then reread by the later turns
    assert by[CauseKind.IDLE_REWRITE] == pytest.approx(150_000 * 5.75 / 1e6)
    assert by[CauseKind.LARGE_TOOL_RESULT] == pytest.approx(
        (60_000 * 6.25 + 5 * 60_000 * 0.5) / 1e6
    )
    # growth over the 150k of the hour before takes only what is above both claims
    assert by[CauseKind.CONTEXT_GROWTH] == pytest.approx(80_000 * (1 + 2 + 3 + 4) * 0.5 / 1e6)


# --------------------------------------------------------------------------- long context


def test_removed_tokens_are_priced_at_the_requests_rate() -> None:
    w = World()
    w.req(0, model=LONG, cache_write_5m=150_000, tools=[("Read", "/p/big.json")])
    w.event(EventKind.TOOL_RESULT, 0.5, tool="Read", target="/p/big.json", size_tokens=100_000)
    reqs = [w.req(1, model=LONG, cache_read=150_000, cache_write_5m=100_000)]
    reqs += [w.req(2 + k, model=LONG, cache_read=250_000) for k in range(2)]
    by = causes(run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE)))
    # above 200k the whole request is at the long-context rates: the 100k cost those
    # rates, and taking them out does not re-price the rest at the base rates
    assert by[CauseKind.LARGE_TOOL_RESULT] == pytest.approx(
        (100_000 * 7.5 + 2 * 100_000 * 0.6) / 1e6
    )


# --------------------------------------------------------------------------- what the pause was


def test_a_long_tool_call_is_not_a_break() -> None:
    w = World()
    w.agent("a1", "coder", "tu_c")
    w.req(0, agent="a1", cache_write_5m=200_000)
    p = w.call(1, "Bash", "for", 10, agent="a1", cache_read=200_000)
    # the call's result comes back 10 minutes later, just before the rewrite
    w.index.evs[-1] = replace(w.index.evs[-1], timestamp=at(11))
    r = w.req(11.05, agent="a1", cache_write_5m=200_000)
    e = run(w, spike([r], agent="a1"))[0]
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.title == "Cache expired while Bash (for) ran 10 min"
    assert any("The Bash (for) call ran for 10 min" in ev.label for ev in e.evidence)
    assert "background" in e.advice
    assert "while Bash (for) ran" in e.summary
    assert p.tool_calls


def test_a_pause_past_the_lifetime_wins_over_a_tool_change() -> None:
    w = World()
    p = w.req(0, cache_write_1h=300_000)
    w.api(p, cache_ttl="1h", shape_hash="h1")
    w.event(EventKind.TOOL_SET, 30, source="api-request-shape", shape_hash="h2")
    r = w.req(240, cache_write_1h=300_000)
    w.api(r, cache_ttl="1h", shape_hash="h2")
    e = run(w, spike([r]))[0]
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.title == "Cache rebuilt after 4 h idle"


def test_a_cache_lifetime_change_rewrites_the_cache() -> None:
    w = World()
    w.req(0, cache_write_5m=500_000)
    p = w.req(0.2, cache_read=500_000)
    w.api(p, seconds_before=3, cache_ttl="5m", shape_hash="h1")
    r = w.req(0.4, cache_write_1h=500_000)
    w.api(r, seconds_before=3, cache_ttl="1h", shape_hash="h1")
    e = run(w, spike([r]))[0]
    assert e.cause is CauseKind.TOOL_SET_CHANGE
    assert e.title == "Cache lifetime changed from 5m to 1h"
    assert e.excess_usd == pytest.approx(500_000 * 9.5 / 1e6)


def test_a_spike_inside_one_spawned_agent_is_not_its_fan_out() -> None:
    w = World()
    main = w.req(0, cache_read=50_000, tools=[("Agent", "coder")] * 4)
    for k, call in enumerate(main.tool_calls):
        w.event(EventKind.SPAWN, 0.1, tool_use_id=call.tool_use_id, agent_type="coder")
        w.agent(f"a{k}", "coder", call.tool_use_id)
        w.req(1, agent=f"a{k}", cache_write_5m=30_000)
    # five minutes into its work, one agent's own pace rises
    reqs = [w.req(6 + k, agent="a0", cache_read=200_000, output=8_000) for k in range(4)]
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE, agent="a0"))
    assert all(e.cause is not CauseKind.FAN_OUT for e in out)
