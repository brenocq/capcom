"""Each cause on a synthetic session built to show it, with its expected excess, and
the negative cases that must not raise a cause."""

from __future__ import annotations

from dataclasses import replace

import pytest

from capcom.explain import explain
from capcom.explain.fmt import clock
from capcom.types import CauseKind, Confidence, EventKind, Explanation, Spike, SpikeKind
from tests.explain.helpers import OPUS, SONNET, TABLE, World, check_claims, cost, prefix, spike


def run(w: World, *spikes: Spike) -> list[Explanation]:
    for s in spikes:
        check_claims(w, s)
    out = explain(w.index, TABLE, spikes)
    for s in spikes:
        mine = [e for e in out if e.spike is s]
        # every dollar of the excess is in an explanation, the rest in UNEXPLAINED
        assert mine and sum(e.excess_usd for e in mine) == pytest.approx(s.excess_usd)
        assert all(e.summary for e in mine)
    return out


def labels(e: Explanation) -> list[str]:
    return [ev.label for ev in e.evidence]


def has(e: Explanation, text: str) -> bool:
    return any(text in label for label in labels(e))


REST = "The rest of the spike: no rule fits"


def only(w: World, s: Spike, rest: float | None = None) -> Explanation:
    """The spike's one cause (or its one UNEXPLAINED), apart from the unmatched rest.
    The two add up to the spike's excess, so a test that pins the cause's dollars pins
    the rest's; `rest`, when given, is the rest's size in dollars."""
    out = run(w, s)
    assert sum(x.excess_usd for x in out) == pytest.approx(s.excess_usd)
    causes = [x for x in out if x.title != REST]
    assert len(causes) == 1, [(x.cause, x.excess_usd) for x in out]
    rests = [x for x in out if x.title == REST]
    assert all(x.cause is CauseKind.UNEXPLAINED for x in rests)
    if rest is not None:
        assert sum(x.excess_usd for x in rests) == pytest.approx(rest)
    return causes[0]


# --------------------------------------------------------------------------- rule 1


def test_idle_past_one_hour_ttl() -> None:
    w = World()
    r0 = w.req(0, cache_write_1h=200_000)
    r1 = w.req(2, cache_read=200_000)
    r2 = w.req(80, cache_write_1h=205_000)
    for r in (r0, r1, r2):
        w.api(r, cache_ttl="1h", shape_hash="h1")
    e = only(w, spike([r2], baseline=cost(r1)))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.confidence is Confidence.CLEAR
    # 200k tokens the previous request had cached, written at $10/M instead of read at $0.5/M
    assert e.excess_usd == pytest.approx(200_000 * 9.5 / 1e6)
    assert e.title == "Cache rebuilt after 78 min idle"
    assert has(e, "Gap of 78 min; cache lifetime 1 h (cacheControl ttl 1h)")
    assert has(e, f"Previous main-thread request at {clock(r1.timestamp)}")
    assert has(e, f"The request at {clock(r2.timestamp)} wrote 205,000 tokens to the cache")
    assert e.evidence[-1].message_id == r2.message_id
    assert "hour" in e.advice


def test_idle_subagent_known_from_its_5m_writes() -> None:
    w = World()
    w.agent("a1", "scout", "tu_x")
    w.req(0, agent="a1", cache_write_5m=50_000)
    w.req(1, agent="a1", cache_read=50_000, cache_write_5m=1_000)
    r2 = w.req(12, agent="a1", cache_write_5m=52_000)
    e = only(w, spike([r2], agent="a1"))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.confidence is Confidence.CLEAR
    assert e.excess_usd == pytest.approx(51_000 * (6.25 - 0.5) / 1e6)
    assert has(e, "cache lifetime 5 minutes (the previous request wrote 5-minute entries)")
    assert has(e, "Previous scout subagent request")


def test_idle_on_the_fallback_lifetime_is_likely() -> None:
    w = World()
    w.req(0, cache_write_5m=60_000)
    w.req(1, cache_read=60_000)  # reads only: its own writes say nothing
    r2 = w.req(30, cache_write_5m=60_000)
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.confidence is Confidence.LIKELY
    assert has(e, "cache lifetime 5 minutes (assumed: no cacheControl record)")


def test_away_summary_before_a_rewrite() -> None:
    w = World()
    w.req(0, cache_write_1h=100_000)
    w.req(1, cache_read=100_000)
    away = w.event(EventKind.AWAY, 15)
    r2 = w.req(20, cache_write_1h=100_000)
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.confidence is Confidence.LIKELY
    assert any(ev.event_key == away.key for ev in e.evidence)


def test_gap_within_the_lifetime_is_not_idle() -> None:
    w = World()
    w.req(0, cache_write_1h=100_000)
    w.req(1, cache_read=100_000)
    r2 = w.req(30, cache_write_1h=100_000)  # 29 min, the session writes 1-hour entries
    s = spike([r2], baseline=0.1)
    e = only(w, s)
    assert e.cause is CauseKind.UNEXPLAINED
    assert e.confidence is Confidence.UNEXPLAINED
    assert e.excess_usd == pytest.approx(s.excess_usd)
    assert has(e, "with no pause, compaction, model or tool change before it")


# --------------------------------------------------------------------------- rule 2


def test_rewrite_after_compaction() -> None:
    w = World()
    w.req(0, cache_write_1h=300_000)
    w.req(1, cache_read=300_000)
    comp = w.event(EventKind.COMPACTION, 2, trigger="auto", pre_tokens=300_000, post_tokens=40_000)
    r2 = w.req(3, cache_write_1h=40_000)
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.COMPACTION
    assert e.confidence is Confidence.CLEAR
    assert e.excess_usd == pytest.approx(40_000 * 9.5 / 1e6)
    assert e.title == "Cache rewritten after a compaction"
    assert has(e, f"compact_boundary at {clock(comp.timestamp)}, trigger auto")


def test_context_trimmed_without_a_compaction_record() -> None:
    # a 608k prompt, then 10 s later 522k with only its first 25k read from the cache
    w = World()
    w.req(0, cache_write_1h=600_000)
    w.req(1, cache_read=600_000, cache_write_1h=8_000)
    r = w.req(1 + 10 / 60, cache_read=25_000, cache_write_1h=497_000)
    e = only(w, spike([r], baseline=0.1))
    assert e.cause is CauseKind.COMPACTION
    assert e.confidence is Confidence.LIKELY
    assert e.title == "Context trimmed without a compaction record, cache rebuilt"
    assert e.excess_usd == pytest.approx(497_000 * 9.5 / 1e6)
    assert has(e, "The prompt went from 608k to 522k tokens between the requests at")
    assert has(e, "with no compact_boundary between them")
    assert has(e, "Only the first 25k were read from the cache")
    assert "shrank from 608k to 522k tokens with no compaction record" in e.summary


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (300_000, 285_000),  # 15k: 5 %, but under 20k tokens
        (1_000_000, 970_000),  # 30k: over 20k tokens, but 3 %
    ],
)
def test_a_small_shrink_is_not_a_trim(before: int, after: int) -> None:
    w = World()
    w.req(0, cache_write_1h=before)
    w.req(1, cache_read=before)
    r = w.req(1.2, cache_read=5_000, cache_write_1h=after - 5_000)
    e = only(w, spike([r], baseline=0.1))
    assert e.cause is CauseKind.UNEXPLAINED
    assert has(e, f"wrote {after - 5_000:,} tokens to the cache")


def test_late_compaction_and_the_cap() -> None:
    w = World()
    for k in range(5):
        w.req(k, cache_read=550_000)
    heavy = [
        w.req(10 + k, cache_read=c) for k, c in enumerate((600_000, 700_000, 800_000, 900_000))
    ]
    w.event(EventKind.COMPACTION, 14, trigger="auto", pre_tokens=968_686, post_tokens=41_278)
    w.req(15, cache_write_1h=41_278)
    s = spike(heavy, baseline=0.5, kind=SpikeKind.SLOPE_CHANGE)
    out = run(w, s)
    first = out[0]
    assert first.cause is CauseKind.COMPACTION
    assert first.confidence is Confidence.CLEAR
    # the turns above 400k: 200k + 300k + 400k + 500k extra reads at $0.5/M
    assert first.excess_usd == pytest.approx(0.70)
    assert first.title == "Compaction ran late, at 969k tokens"
    assert has(first, "preTokens 968,686, postTokens 41,278")
    assert has(first, "4 turns above 400k, averaging 750k")
    assert has(first, "The next request wrote 41k tokens to the cache")
    # the tokens above the 550k of the hour before are all above 400k, already claimed
    # by the late compaction: context growth has nothing left to claim
    assert all(e.cause is not CauseKind.CONTEXT_GROWTH for e in out)
    assert sum(e.excess_usd for e in out) <= s.excess_usd + 1e-12


def test_growth_takes_only_what_the_compaction_left() -> None:
    w = World()
    for k in range(5):
        w.req(k, cache_read=200_000)
    heavy = [w.req(10 + k, cache_read=c) for k, c in enumerate((450_000, 500_000))]
    w.event(EventKind.COMPACTION, 12, trigger="auto", pre_tokens=500_000, post_tokens=40_000)
    out = run(w, spike(heavy, kind=SpikeKind.SLOPE_CHANGE))
    by = {e.cause: e.excess_usd for e in out}
    # above 400k: 50k + 100k; above the 200k before, the rest: 200k + 200k
    assert by[CauseKind.COMPACTION] == pytest.approx(150_000 * 0.5 / 1e6)
    assert by[CauseKind.CONTEXT_GROWTH] == pytest.approx(400_000 * 0.5 / 1e6)


# --------------------------------------------------------------------------- rule 3


def test_model_switch() -> None:
    w = World()
    w.req(0, cache_write_5m=200_000)
    r1 = w.req(1, cache_read=200_000)
    model = w.event(EventKind.MODEL, 2.5, model=SONNET)
    r2 = w.req(3, model=SONNET, cache_write_5m=200_000)
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.MODEL_SWITCH
    assert e.confidence is Confidence.CLEAR
    assert e.excess_usd == pytest.approx(200_000 * (3.75 - 0.3) / 1e6)
    assert e.title == "Model switch rewrote the cache"
    assert has(e, f"message.model changed from {OPUS} to {SONNET} at {clock(r2.timestamp)}")
    assert any(ev.event_key == model.key for ev in e.evidence)
    assert r1.model == OPUS


def test_same_model_no_switch() -> None:
    w = World()
    w.req(0, cache_write_5m=200_000)
    w.req(1, cache_read=200_000)
    r2 = w.req(3, model="claude-opus-5-5-20261001", cache_read=200_000, output=200)
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.UNEXPLAINED


# --------------------------------------------------------------------------- rule 4


def test_shape_hash_change() -> None:
    w = World()
    w.event(
        EventKind.TOOL_SET, 0, source="api-request-shape", shape_hash="h1", names=("Read", "Bash")
    )
    r0 = w.req(0, cache_write_1h=100_000)
    r1 = w.req(1, cache_read=100_000)
    w.api(r1, cache_ttl="1h", shape_hash="h1")
    w.event(
        EventKind.TOOL_SET,
        2.5,
        source="api-request-shape",
        shape_hash="h2",
        names=("Read", "Bash", "mcp__srv__one"),
    )
    r2 = w.req(3, cache_write_1h=101_000)
    w.api(r2, cache_ttl="1h", shape_hash="h2")
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.TOOL_SET_CHANGE
    assert e.confidence is Confidence.CLEAR
    assert e.excess_usd == pytest.approx(100_000 * 9.5 / 1e6)
    assert has(e, "shapeHash changed between the requests at")
    assert has(e, "added mcp__srv__one")
    assert r0.usage.context == r1.usage.context


def test_tool_delta_attachment_is_likely() -> None:
    w = World()
    w.req(0, cache_write_1h=100_000)
    w.req(1, cache_read=100_000)
    w.event(
        EventKind.TOOL_SET, 2, source="deferred_tools_delta", added=("mcp__srv__one",), removed=()
    )
    r2 = w.req(3, cache_write_1h=100_000)
    e = only(w, spike([r2]))
    assert e.cause is CauseKind.TOOL_SET_CHANGE
    assert e.confidence is Confidence.LIKELY
    assert has(e, "deferred_tools_delta at")
    assert has(e, "added mcp__srv__one")


def test_initial_skill_listing_is_not_a_change() -> None:
    w = World()
    w.req(0, cache_write_1h=100_000)
    w.req(1, cache_read=100_000)
    w.event(EventKind.TOOL_SET, 2, source="skill_listing", names=("a",), is_initial=True)
    r2 = w.req(3, cache_write_1h=100_000)
    assert only(w, spike([r2])).cause is CauseKind.UNEXPLAINED


# --------------------------------------------------------------------------- rules 5 and 8


def _big_read(w: World, compaction_at: float | None = None) -> list:  # type: ignore[type-arg]
    w.req(0, cache_write_5m=50_000, tools=[("Read", "/home/user/proj/big.json")])
    w.event(
        EventKind.TOOL_RESULT,
        0.5,
        tool="Read",
        target="/home/user/proj/big.json",
        size_tokens=100_000,
        chars=400_000,
    )
    if compaction_at is not None:
        w.event(EventKind.COMPACTION, compaction_at, trigger="auto")
    reqs = [w.req(1, cache_read=50_000, cache_write_5m=100_000)]
    reqs += [w.req(2 + k, cache_read=150_000) for k in range(4)]
    return reqs


def test_large_tool_result() -> None:
    w = World()
    reqs = _big_read(w)
    e = only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.LARGE_TOOL_RESULT
    assert e.confidence is Confidence.LIKELY
    # written once at $6.25/M, then reread by 4 requests at $0.5/M
    assert e.excess_usd == pytest.approx(100_000 * 6.25 / 1e6 + 4 * 100_000 * 0.5 / 1e6)
    assert e.title == "A 100k-token tool result stayed in context"
    assert has(e, "Read of /home/user/proj/big.json returned about 100k tokens (estimated")
    assert has(e, f"Written to the cache at {clock(reqs[0].timestamp)}")
    assert has(e, "Reread by 4 later requests so far")
    assert "offset and limit" in e.advice


def test_large_tool_result_until_compaction() -> None:
    w = World()
    reqs = _big_read(w, compaction_at=3.5)
    e = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))[0]
    assert e.cause is CauseKind.LARGE_TOOL_RESULT
    assert e.excess_usd == pytest.approx(100_000 * 6.25 / 1e6 + 2 * 100_000 * 0.5 / 1e6)
    assert has(e, "Reread by 2 later requests until the compaction")


def test_small_tool_result_is_not_large() -> None:
    w = World()
    w.req(0, cache_write_5m=50_000, tools=[("Read", "/p/a.py")])
    w.event(EventKind.TOOL_RESULT, 0.5, tool="Read", target="/p/a.py", size_tokens=15_000)
    reqs = [w.req(1, cache_read=50_000, cache_write_5m=15_000)]
    reqs += [w.req(2 + k, cache_read=65_000) for k in range(4)]
    e = only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.UNEXPLAINED
    assert has(e, "No tool result above 20k tokens")


def test_long_subagent_report() -> None:
    w = World()
    w.req(0, cache_write_5m=20_000, tools=[("Agent", "scout")])
    w.event(EventKind.TOOL_RESULT, 1, tool="Agent", target="scout", size_tokens=30_000)
    reqs = [w.req(2, cache_read=20_000, cache_write_5m=30_000)]
    reqs += [w.req(3 + k, cache_read=50_000) for k in range(2)]
    e = only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.LONG_REPORT
    assert e.confidence is Confidence.LIKELY
    assert e.excess_usd == pytest.approx(30_000 * 6.25 / 1e6 + 2 * 30_000 * 0.5 / 1e6)
    assert has(e, "scout subagent reported about 30k tokens")


# --------------------------------------------------------------------------- rule 6


def test_context_growth() -> None:
    w = World()
    for m in (0, 10, 20):
        w.req(m, cache_read=50_000)
    reqs = [
        w.req(61 + k, cache_read=c)
        for k, c in enumerate((100_000, 150_000, 200_000, 250_000, 300_000, 350_000))
    ]
    e = only(w, spike(reqs, baseline=0.1, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.CONTEXT_GROWTH
    assert e.confidence is Confidence.CLEAR
    assert e.excess_usd == pytest.approx(1_050_000 * 0.5 / 1e6)
    assert e.title == "Context grew from 50k to 350k tokens"
    assert has(e, f"Crossed 150k at {clock(reqs[1].timestamp)}")
    assert has(e, f"Crossed 200k at {clock(reqs[2].timestamp)}")
    assert not has(e, "Crossed 400k")
    assert has(e, "No compaction in the window")


def test_small_contexts_do_not_count_as_growth() -> None:
    w = World()
    w.req(0, cache_read=20_000)
    reqs = [w.req(61 + k, cache_read=c) for k, c in enumerate((40_000, 60_000, 90_000))]
    assert only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE)).cause is CauseKind.UNEXPLAINED


# --------------------------------------------------------------------------- rule 7


def _fan_out(w: World, n: int) -> list:  # type: ignore[type-arg]
    types = ["scout", "scout", "coder", "scout", "coder", "scout"][:n]
    main = w.req(0, cache_read=50_000, tools=[("Agent", t) for t in types])
    reqs = [main]
    for k, (call, kind) in enumerate(zip(main.tool_calls, types, strict=True)):
        w.event(EventKind.SPAWN, 0.1, tool_use_id=call.tool_use_id, agent_type=kind)
        w.agent(f"a{k}", kind, call.tool_use_id)
        reqs.append(w.req(1 + k, agent=f"a{k}", cache_write_5m=30_000))
        reqs.append(w.req(1.5 + k, agent=f"a{k}", cache_read=30_000, tools=[("Bash", "ls")]))
    return reqs


def test_fan_out() -> None:
    w = World()
    reqs = _fan_out(w, 5)
    e = only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.FAN_OUT
    assert e.confidence is Confidence.CLEAR
    # the five prefixes only: 30k tokens written at $6.25/M each, not the agents' turns
    assert e.excess_usd == pytest.approx(5 * 30_000 * 6.25 / 1e6)
    assert e.excess_usd == pytest.approx(sum(prefix(r) for r in reqs[1::2]))
    assert "150k tokens in all, which cost $0.94" in e.summary
    assert "the rest is the agents' own work, not counted as fan-out" in e.summary
    assert "in the parent" in e.advice
    assert e.title == "5 subagents in under a minute"
    assert has(e, "5 Agent tool calls between")
    assert has(e, "scout \u00d73, coder \u00d72")
    assert has(e, "Each first request wrote about 30k tokens")
    assert has(e, "5 of them made two tool calls or fewer")


def test_fan_out_of_busy_agents_speaks_to_the_prefix() -> None:
    w = World()
    reqs = _fan_out(w, 5)
    for k in range(5):  # each agent goes on with ten more tool calls
        reqs += [
            w.req(2 + k + j / 20, agent=f"a{k}", cache_read=30_000, tools=[("Read", "x.py")])
            for j in range(10)
        ]
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    e = next(x for x in out if x.cause is CauseKind.FAN_OUT)
    assert e.excess_usd == pytest.approx(5 * 30_000 * 6.25 / 1e6)
    assert has(e, "0 of them made two tool calls or fewer")
    assert "in the parent" not in e.advice
    assert "narrower tool list" in e.advice and "fewer of them at once" in e.advice
    # the agents' own turns are no cause's: the unexplained rest, saying so
    rest = out[-1]
    assert rest.cause is CauseKind.UNEXPLAINED and rest.title == REST
    assert "the new subagents' own work" in rest.summary


def test_three_spawns_are_not_a_fan_out() -> None:
    w = World()
    reqs = _fan_out(w, 3)
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert all(e.cause is not CauseKind.FAN_OUT for e in out)


# --------------------------------------------------------------------------- rule 9


def test_thinking_heavy() -> None:
    w = World()
    reqs = [
        w.req(k, cache_read=20_000, output=10_000, thinking=8_000, effort="high") for k in range(3)
    ]
    e = only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.THINKING_HEAVY
    assert e.confidence is Confidence.CLEAR
    assert e.excess_usd == pytest.approx(3 * 8_000 * 25 / 1e6)
    assert has(e, "3 turns with 24k thinking tokens of 30k output")
    assert has(e, "Effort high on these turns")


def test_tool_heavy_turns_are_not_thinking_heavy() -> None:
    w = World()
    reqs = [
        w.req(
            k,
            cache_read=20_000,
            output=10_000,
            thinking=8_000,
            tools=[("Read", f"/p/{k}"), ("Grep", None)],
        )
        for k in range(3)
    ]
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert all(e.cause is not CauseKind.THINKING_HEAVY for e in out)


# --------------------------------------------------------------------------- rule 10


@pytest.mark.parametrize(("n", "conf"), [(8, Confidence.LIKELY), (12, Confidence.CLEAR)])
def test_polling(n: int, conf: Confidence) -> None:
    w = World()
    reqs = [w.call(k, "Bash", "sleep", 2_000, cache_read=80_000 + 2_000 * k) for k in range(n)]
    # the usual pace: the same turns rereading 80k each
    e = only(w, spike(reqs, baseline=n * 80_000 * 0.5 / 1e6, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.POLLING
    assert e.confidence is conf
    # the n - 1 repeats: their output, and their results reread by the later turns
    rereads = (n - 1) * (n - 2) // 2
    assert e.excess_usd == pytest.approx(((n - 1) * 100 * 25 + rereads * 2_000 * 0.5) / 1e6)
    assert e.title == f"The same Bash call {n} times in a row"
    assert has(e, f"{n} Bash (sleep) calls in a row")
    assert has(e, "result tokens that later turns reread")


def test_rereading_the_same_file_is_polling() -> None:
    w = World()
    reqs = [w.call(k, "Read", "/p/build.log", 3_000, cache_read=90_000) for k in range(6)]
    e = only(w, spike(reqs, baseline=6 * 90_000 * 0.5 / 1e6, kind=SpikeKind.SLOPE_CHANGE))
    assert e.cause is CauseKind.POLLING
    assert "Read the file once" in e.advice


@pytest.mark.parametrize(
    ("tool", "target"),
    [("Bash", "cd"), ("Bash", "python3"), ("Bash", "uv"), ("mcp__srv__exec", None)],
)
def test_ordinary_turns_are_not_polling(tool: str, target: str | None) -> None:
    # the reviewer's probe: Bash turns that start with `cd` look alike by their first word
    w = World()
    reqs = [w.call(k, tool, target, 2_000, cache_read=200_000 + 2_000 * k) for k in range(8)]
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert all(e.cause is not CauseKind.POLLING for e in out)


def test_different_files_are_not_polling() -> None:
    w = World()
    reqs = [w.call(k, "Read", f"/p/{k}.py", 3_000, cache_read=90_000) for k in range(6)]
    out = run(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE))
    assert all(e.cause is not CauseKind.POLLING for e in out)


def test_four_repeats_are_not_polling() -> None:
    w = World()
    reqs = [w.call(k, "Bash", "sleep", 2_000, cache_read=80_000) for k in range(4)]
    reqs.append(w.req(5, cache_read=80_000, tools=[("Read", "/p/a")]))
    assert only(w, spike(reqs, kind=SpikeKind.SLOPE_CHANGE)).cause is CauseKind.UNEXPLAINED


# --------------------------------------------------------------------------- allocation


def test_nothing_fits() -> None:
    w = World()
    for k in range(5):
        w.req(k, cache_read=50_000)
    r = w.req(6, cache_read=50_000, output=1_500)
    s = spike([r], baseline=0.0275)
    e = only(w, s)
    assert e.cause is CauseKind.UNEXPLAINED
    assert e.excess_usd == pytest.approx(s.excess_usd)
    assert e.title == "One request cost $0.06"
    assert has(e, "No pause, compaction, model switch or tool change in the window")
    assert has(e, "No tool result above 20k tokens")


def test_a_small_cause_is_only_the_closest() -> None:
    w = World()
    reqs = [w.req(k, cache_read=80_000, tools=[("Bash", "sleep")]) for k in range(5)]
    s = replace(spike(reqs, kind=SpikeKind.SLOPE_CHANGE), cost_usd=10.0, baseline_usd=0.0)
    e = only(w, s)
    assert e.cause is CauseKind.UNEXPLAINED
    assert e.excess_usd == 10.0
    assert has(e, "Closest: The same Bash call 5 times in a row, about $0.01")
    assert e.title == "Spend rose for 4 min"


def test_uncovered_rest_is_unexplained() -> None:
    w = World()
    w.req(0, cache_write_1h=200_000)
    w.req(2, cache_read=200_000)
    r2 = w.req(80, cache_write_1h=205_000)
    s = replace(spike([r2]), cost_usd=5.0, baseline_usd=0.0)
    out = run(w, s)
    assert [e.cause for e in out] == [CauseKind.IDLE_REWRITE, CauseKind.UNEXPLAINED]
    assert out[1].excess_usd == pytest.approx(5.0 - 1.90)
    assert sum(e.excess_usd for e in out) == pytest.approx(5.0)


def test_rest_is_unexplained_even_when_causes_cover_most() -> None:
    w = World()
    w.req(0, cache_write_1h=200_000)
    w.req(2, cache_read=200_000)
    r2 = w.req(80, cache_write_1h=205_000)
    s = replace(spike([r2]), cost_usd=2.0, baseline_usd=0.0)  # the idle rewrite is $1.90
    out = run(w, s)
    assert [e.cause for e in out] == [CauseKind.IDLE_REWRITE, CauseKind.UNEXPLAINED]
    assert out[1].title == REST
    assert out[1].excess_usd == pytest.approx(0.10)
    assert "no rule fits the other $0.10" in out[1].summary


def test_a_tiny_rest_is_folded_into_the_largest_cause() -> None:
    w = World()
    w.req(0, cache_write_1h=200_000)
    w.req(2, cache_read=200_000)
    r2 = w.req(80, cache_write_1h=205_000)
    s = replace(spike([r2]), cost_usd=1.93, baseline_usd=0.0)  # $0.03 left, 1.6 %
    e = only(w, s, rest=0.0)
    assert e.cause is CauseKind.IDLE_REWRITE
    assert len(run(w, s)) == 1
    assert e.excess_usd == pytest.approx(1.93)
    # the summary names the dollars the line shows, the folded cents included
    assert "$1.93 is counted here" in e.summary


def test_machine_wide_spike_and_order() -> None:
    w = World()
    w.req(0, cache_write_1h=200_000)
    w.req(2, cache_read=200_000)
    idle = w.req(80, cache_write_1h=200_000)
    w.req(70, session="s2", cache_write_5m=100_000)
    w.req(71, session="s2", cache_read=100_000)
    switch = w.req(80.5, session="s2", model=SONNET, cache_write_5m=100_000)
    wide = spike([idle, switch], session=None)
    one = spike([switch], session="s2")
    out = run(w, one, wide)
    assert out[0].spike is one and out[0].cause is CauseKind.MODEL_SWITCH
    k = next(i for i, e in enumerate(out) if e.spike is wide)
    assert all(e.spike is one for e in out[:k])
    rest = out[k:]
    assert all(e.spike is wide for e in rest)
    assert {e.cause for e in rest} == {
        CauseKind.IDLE_REWRITE,
        CauseKind.MODEL_SWITCH,
        CauseKind.UNEXPLAINED,  # the rest: outputs and new tokens
    }
    assert sum(e.excess_usd for e in rest) <= wide.excess_usd + 1e-12
    causes = [e for e in rest if e.cause is not CauseKind.UNEXPLAINED]
    assert {ev.session_id for e in causes for ev in e.evidence} == {"s1", "s2"}


def test_unknown_messages_are_unexplained() -> None:
    w = World()
    r = w.req(0, cache_read=10_000)
    s = replace(spike([r]), message_ids=("msg_missing",))
    e = only(w, s)
    assert e.cause is CauseKind.UNEXPLAINED
    assert has(e, "No request of the spike is in the index")


def _idle_world(tokens: int, ttl_known: bool = True) -> tuple[World, Spike]:
    """One idle rewrite of `tokens` (1-hour entries, CLEAR; or 5-minute entries on the
    assumed lifetime, LIKELY), in a spike made to cost $25."""
    w = World()
    if ttl_known:
        w.req(0, cache_write_1h=tokens)
        w.req(2, cache_read=tokens)
        r = w.req(80, cache_write_1h=tokens)
    else:
        w.req(0, cache_write_5m=tokens)
        w.req(1, cache_read=tokens)  # reads only: the lifetime is assumed
        r = w.req(30, cache_write_5m=tokens)
    return w, replace(spike([r]), cost_usd=25.0, baseline_usd=0.0)


def test_a_small_clear_cause_is_kept() -> None:
    w, s = _idle_world(200_000)  # $1.90 of $25: under a tenth, but CLEAR and over $0.50
    out = run(w, s)
    assert [(e.cause, e.confidence) for e in out] == [
        (CauseKind.IDLE_REWRITE, Confidence.CLEAR),
        (CauseKind.UNEXPLAINED, Confidence.UNEXPLAINED),
    ]
    assert out[0].excess_usd == pytest.approx(1.90)
    assert out[1].excess_usd == pytest.approx(25.0 - 1.90)


@pytest.mark.parametrize(
    ("tokens", "ttl_known"),
    [
        (40_000, True),  # CLEAR, but $0.38
        (100_000, False),  # $0.58, but LIKELY
    ],
)
def test_a_small_cause_under_the_floor_folds_into_the_rest(tokens: int, ttl_known: bool) -> None:
    w, s = _idle_world(tokens, ttl_known)
    e = only(w, s, rest=0.0)
    assert e.cause is CauseKind.UNEXPLAINED
    assert e.excess_usd == pytest.approx(25.0)
    assert has(e, "Closest: ")


def test_a_connector_removed_is_a_tool_change_not_a_trim() -> None:
    # an MCP server disconnects: its instructions and 55 deferred tools leave the
    # prompt, which shrinks from 210k to 180k with no compaction record
    w = World()
    w.req(0, cache_write_1h=200_000)
    w.req(1, cache_read=200_000, cache_write_1h=10_000)
    gone = tuple(f"mcp__srv__tool{k}" for k in range(55))
    w.event(EventKind.TOOL_SET, 1.5, source="mcp_instructions_delta", added=(), removed=("srv",))
    w.event(EventKind.TOOL_SET, 1.5, source="deferred_tools_delta", added=(), removed=gone)
    r = w.req(2, cache_read=4_000, cache_write_1h=176_000)
    e = only(w, spike([r], baseline=0.1))
    assert e.cause is CauseKind.TOOL_SET_CHANGE
    assert e.confidence is Confidence.LIKELY
    assert has(e, "deferred_tools_delta at")


def test_a_cold_cache_on_the_assumed_lifetime_is_not_a_trim() -> None:
    # 5-minute entries, reads only: the lifetime is assumed, and the prompt also shrank
    w = World()
    w.req(0, cache_write_5m=200_000)
    w.req(1, cache_read=200_000)
    r = w.req(30, cache_read=4_000, cache_write_5m=170_000)
    e = only(w, spike([r], baseline=0.1))
    assert e.cause is CauseKind.IDLE_REWRITE
    assert e.confidence is Confidence.LIKELY


def test_a_trim_and_a_logged_compaction_stay_apart() -> None:
    w = World()
    w.req(0, cache_write_1h=300_000)
    w.req(1, cache_read=300_000)
    w.event(EventKind.COMPACTION, 2, trigger="auto", pre_tokens=300_000, post_tokens=40_000)
    comp = w.req(3, cache_write_1h=40_000)
    w.req(4, cache_read=40_000, cache_write_1h=160_000)
    trim = w.req(4.2, cache_read=4_000, cache_write_1h=150_000)
    out = run(w, spike([comp, trim], baseline=0.1))
    titles = {e.title: e for e in out if e.cause is CauseKind.COMPACTION}
    assert set(titles) == {
        "Cache rewritten after a compaction",
        "Context trimmed without a compaction record, cache rebuilt",
    }
    logged = titles["Cache rewritten after a compaction"]
    trimmed = titles["Context trimmed without a compaction record, cache rebuilt"]
    assert logged.excess_usd == pytest.approx(40_000 * 9.5 / 1e6)
    assert trimmed.excess_usd == pytest.approx(150_000 * 9.5 / 1e6, abs=0.01)  # + tiny rest
    assert logged.advice != trimmed.advice


def test_a_small_clear_cause_under_the_share_floor_folds_into_the_rest() -> None:
    w, s = _idle_world(200_000)
    s = replace(s, cost_usd=100.0)  # $1.90 of $100: over $0.50, but under 3 %
    e = only(w, s, rest=0.0)
    assert e.cause is CauseKind.UNEXPLAINED
    assert e.excess_usd == pytest.approx(100.0)


def test_a_spike_is_matched_only_when_its_causes_cover_a_tenth() -> None:
    from capcom.explain import matched
    from capcom.explain.cli import format_spikes

    w, small = _idle_world(200_000)
    small = replace(small, cost_usd=50.0)  # $1.90 of $50: kept (3.8 %), not a tenth
    big = replace(small, cost_usd=10.0)  # $1.90 of $10: a tenth and more
    for s, want in ((small, False), (big, True)):
        out = run(w, s)
        assert any(e.cause is CauseKind.IDLE_REWRITE for e in out)
        assert matched(s, out) is want
    text = format_spikes(explain(w.index, TABLE, [small, big]), 5, {}, {})
    assert text.startswith("2 spikes; 1 matched to a cause")
