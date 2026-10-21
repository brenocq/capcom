"""Checks on how sessions ran: context size, compactions, cache rewrites, subagents,
reports and images. All sessions are synthetic."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.checks import run_checks
from capcom.checks.agents import find_definitions, read_tools
from capcom.checks.cli import run_checks_view
from capcom.config import Config
from capcom.types import EventKind, Finding, ToolCall
from tests.checks.helpers import (
    READ_RATE,
    SINCE,
    TABLE,
    UNTIL,
    SortedIndex,
    agent,
    ev,
    index,
    kinds,
    only,
    run,
)
from tests.cost.fakes import at

HANDOFF = 40_000 * 5 / 1e6  # a 40k-token handoff written at claude-opus-5-5's 5-minute price


def _checks(idx) -> list[Finding]:  # type: ignore[no-untyped-def]
    return run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=[])


# --------------------------------------------------------------------------- context


def test_turns_past_150k_count_their_excess_context_less_a_handoff() -> None:
    f = only(_checks(index(run(100, cache_read=200_000))), "long_running_agents")
    assert f.saving_usd_per_week == pytest.approx(100 * 50_000 * READ_RATE - HANDOFF)
    assert f.metrics["turns"] == 100 and f.metrics["max_context"] == 200_000
    assert f.metrics["handoffs"] == 1
    assert f.metrics["rereads_usd"] == pytest.approx(100 * 50_000 * READ_RATE)


def test_a_thread_that_barely_passes_the_line_saves_nothing() -> None:
    # 10 turns 50k past the line reread $0.10, less than the $0.20 handoff
    assert "long_running_agents" not in kinds(_checks(index(run(10, cache_read=200_000))))


def test_each_crossing_of_the_line_costs_one_handoff() -> None:
    # past the line, compacted below it, then past it again: two handoffs
    reqs = run(50, cache_read=200_000) + run(50, start=60, cache_read=200_000)
    evs = [ev(EventKind.COMPACTION, 55, pre_tokens=200_000)]
    f = only(_checks(index(reqs, evs)), "long_running_agents")
    assert f.metrics["handoffs"] == 2
    assert f.saving_usd_per_week == pytest.approx(100 * 50_000 * READ_RATE - 2 * HANDOFF)


def test_a_one_hour_thread_hands_off_at_the_one_hour_write_price() -> None:
    reqs = run(100, cache_read=200_000, cache_write_1h=1_000)
    f = only(_checks(index(reqs)), "long_running_agents")
    assert f.saving_usd_per_week == pytest.approx(100 * 51_000 * READ_RATE - 40_000 * 8 / 1e6)


def test_contexts_under_150k_are_not_long_running() -> None:
    assert "long_running_agents" not in kinds(_checks(index(run(50, cache_read=100_000))))


def test_a_late_compaction_takes_the_part_above_400k_from_long_running() -> None:
    reqs = run(30, cache_read=500_000) + run(10, start=40, cache_read=100_000)
    evs = [ev(EventKind.COMPACTION, 35, pre_tokens=520_000, trigger="auto")]
    found = _checks(index(reqs, evs))
    late = only(found, "late_compactions")
    assert late.saving_usd_per_week == pytest.approx(30 * 100_000 * READ_RATE - HANDOFF)
    assert late.metrics["max_pre_tokens"] == 520_000
    long = only(found, "long_running_agents")
    assert long.saving_usd_per_week == pytest.approx(30 * 250_000 * READ_RATE - HANDOFF)


def test_without_a_compaction_long_running_keeps_the_whole_excess() -> None:
    found = _checks(index(run(10, cache_read=500_000)))
    assert "late_compactions" not in kinds(found)
    long = only(found, "long_running_agents")
    assert long.saving_usd_per_week == pytest.approx(10 * 350_000 * READ_RATE - HANDOFF)


def test_an_early_compaction_is_not_late() -> None:
    reqs = run(10, cache_read=290_000)
    evs = [ev(EventKind.COMPACTION, 15, pre_tokens=300_000)]
    assert "late_compactions" not in kinds(_checks(index(reqs, evs)))


# --------------------------------------------------------------------------- cache rewrites


def _rewrite_after(gap_minutes: float, **usage: int) -> list:  # type: ignore[type-arg]
    return run(1) + run(1, start=1 + gap_minutes, cache_read=0, **usage)


def test_a_rewrite_after_idling_past_5_minutes_costs_the_write_above_a_read() -> None:
    found = _checks(index(_rewrite_after(30, cache_write_5m=100_000)))
    f = only(found, "idle_cache_rewrites")
    # 100k tokens written at $5/M instead of read at $0.20/M
    assert f.saving_usd_per_week == pytest.approx(100_000 * (5 - 0.2) / 1e6)
    assert f.metrics["rewrites"] == 1
    assert f.evidence[0].message_id is not None


def test_a_short_pause_is_not_an_idle_rewrite() -> None:
    assert "idle_cache_rewrites" not in kinds(
        _checks(index(_rewrite_after(2, cache_write_5m=100_000)))
    )


def test_the_one_hour_lifetime_covers_a_30_minute_pause() -> None:
    assert "idle_cache_rewrites" not in kinds(
        _checks(index(_rewrite_after(30, cache_write_1h=100_000)))
    )
    f = only(_checks(index(_rewrite_after(70, cache_write_1h=100_000))), "idle_cache_rewrites")
    assert f.saving_usd_per_week == pytest.approx(100_000 * (8 - 0.2) / 1e6)


def test_a_model_switch_rewrite_is_its_own_finding() -> None:
    reqs = run(1) + run(1, start=2, model="claude-sonnet-5-5", cache_read=0, cache_write_5m=100_000)
    found = _checks(index(reqs))
    p = TABLE.models["claude-sonnet-5-5"]
    f = only(found, "model_switches")
    assert f.saving_usd_per_week == pytest.approx(100_000 * (p.cache_write_5m - p.cache_read) / 1e6)
    assert "idle_cache_rewrites" not in kinds(found)


def test_same_model_and_no_rewrite_gives_no_switch() -> None:
    reqs = run(1) + run(1, start=2, model="claude-sonnet-5-5", cache_read=100_000)
    assert "model_switches" not in kinds(_checks(index(reqs)))
    assert "model_switches" not in kinds(_checks(index(_rewrite_after(1, cache_write_5m=100_000))))


def test_long_contexts_are_priced_at_the_read_price_apart_from_rewrites() -> None:
    # a 200k thread that idled and rewrote its whole prompt: the rewrite finding
    # counts the write premium, long-running only the read of the tokens past 150k
    reqs = run(40, cache_read=200_000) + run(1, start=71, cache_read=0, cache_write_5m=200_000)
    found = _checks(index(reqs))
    idle = only(found, "idle_cache_rewrites")
    long = only(found, "long_running_agents")
    assert idle.saving_usd_per_week == pytest.approx(200_000 * (5 - 0.2) / 1e6)
    assert long.saving_usd_per_week == pytest.approx(41 * 50_000 * READ_RATE - HANDOFF)
    # together no more than what the tokens past 150k and the rewrite premium cost
    assert idle.metrics["overlaps"] == () and long.metrics["overlaps"] == ()


def test_a_late_compaction_and_long_running_split_the_same_turns() -> None:
    reqs = run(10, cache_read=500_000, cache_write_5m=20_000)
    evs = [ev(EventKind.COMPACTION, 15, pre_tokens=520_000)]
    found = _checks(index(reqs, evs))
    late, long = only(found, "late_compactions"), only(found, "long_running_agents")
    # 520k each: 250k in the band, 120k above it, all at the read price
    assert long.saving_usd_per_week == pytest.approx(10 * 250_000 * READ_RATE - HANDOFF)
    assert late.saving_usd_per_week == pytest.approx(10 * 120_000 * READ_RATE - HANDOFF)
    assert "late_compactions" not in long.metrics["overlaps"]


def test_the_checks_view_marks_overlapping_findings_and_totals_the_rest(tmp_path: Path) -> None:
    def view(idx) -> str:  # type: ignore[no-untyped-def]
        out = io.StringIO()
        config = Config(tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache")
        run_checks_view(config, 7, 10, index=idx, out=out, now=at(60 * 24))
        return out.getvalue()

    reqs = run(40, cache_read=200_000) + run(1, start=71, cache_read=0, cache_write_5m=200_000)
    text = view(index(reqs))
    assert "Long-running agents" in text and "Cache rewrites after the user was away" in text
    assert "* overlaps" not in text and "Overlapping" not in text
    # an unused connector rides in those same prompts: its dollars overlap them
    evs = [
        ev(
            EventKind.TOOL_SET,
            0,
            source="deferred_tools_delta",
            added=("mcp__claude_ai_Docs__read",),
            size_tokens=5_000,
        )
    ]
    text = view(index(reqs, evs))
    assert "claude.ai connectors" in text
    assert "* overlaps #1: not added to the total" in text
    assert "the total covers only findings that don't overlap" in text


# --------------------------------------------------------------------------- tool waits


def _tool_wait(  # type: ignore[no-untyped-def]
    ran: float, then: float = 0.5, agent_id: str | None = "a1", tool: str = "Bash"
):
    """A thread whose request at minute 1 calls `tool`; its result comes `ran`
    minutes later and the next request, `then` minutes after it, rewrites 100k."""
    first = run(1, agent=agent_id)[0]
    call = ToolCall(f"tu_{first.message_id}", tool, "pytest")
    first = replace(first, tool_calls=(call,))
    result = ev(
        EventKind.TOOL_RESULT, 1 + ran, agent=agent_id, tool_use_id=call.tool_use_id, tool=tool
    )
    second = run(1, start=1 + ran + then, agent=agent_id, cache_read=0, cache_write_5m=100_000)
    agents = [agent(agent_id, "coder")] if agent_id else []
    return [first, *second], [result], agents


def test_a_subagent_cache_expiring_during_a_tool_call_is_its_own_finding() -> None:
    reqs, evs, agents = _tool_wait(12)
    found = _checks(index(reqs, evs, agents))
    f = only(found, "subagent_cache_expiry")
    assert f.saving_usd_per_week == pytest.approx(100_000 * (5 - 0.2) / 1e6)
    assert f.change is None  # no setting changes a subagent's cache lifetime
    assert "background" in f.advice
    assert "approve the command sooner" in f.advice  # a permission wait looks the same
    assert "Bash (1)" in f.summary and "12 min" in f.summary
    assert f.metrics["rewrites"] == 1 and f.metrics["tools"] == ("Bash",)
    assert "Bash (pytest)" in f.evidence[0].label
    assert "idle_cache_rewrites" not in kinds(found)  # counted once


@pytest.mark.parametrize(
    ("ran", "then", "agent_id", "kind"),
    [
        # the tool came back in time; the subagent then sat until its parent resumed it
        (3, 20, "a1", "subagent_resumed_late"),
        (12, 5, "a1", "subagent_cache_expiry"),  # the result came back past the lifetime
        (3, 20, None, "idle_cache_rewrites"),  # the main thread: the user was away
        (12, 0.5, None, "subagent_cache_expiry"),  # a main thread's long call is a tool wait
    ],
)
def test_each_pause_has_one_of_three_causes(
    ran: float, then: float, agent_id: str | None, kind: str
) -> None:
    reqs, evs, agents = _tool_wait(ran, then, agent_id)
    found = _checks(index(reqs, evs, agents))
    causes = {"subagent_cache_expiry", "subagent_resumed_late", "idle_cache_rewrites"}
    assert [k for k in kinds(found) if k in causes] == [kind]
    f = only(found, kind)
    assert f.metrics["cause"] in ("tool_wait_expiry", "late_resume_expiry", "idle_expiry")
    assert f.saving_usd_per_week == pytest.approx(100_000 * (5 - 0.2) / 1e6)


def test_a_late_resume_says_to_start_a_fresh_agent() -> None:
    reqs, evs, agents = _tool_wait(3, 20, "a1")
    f = only(_checks(index(reqs, evs, agents)), "subagent_resumed_late")
    assert f.metrics["cause"] == "late_resume_expiry"
    assert "fresh agent" in f.advice and "23 min" in f.summary


def test_an_away_summary_confirms_the_user_was_away() -> None:
    reqs = _rewrite_after(30, cache_write_5m=100_000)
    f = only(_checks(index(reqs, [ev(EventKind.AWAY, 20)])), "idle_cache_rewrites")
    assert f.metrics["after_away_summary"] == 1 and f.metrics["cause"] == "idle_expiry"
    assert "away_summary" in f.evidence[0].label


@pytest.mark.parametrize("agent_id", [None, "a1"])
def test_a_pause_under_5_minutes_is_never_an_expiry(agent_id: str | None) -> None:
    reqs = run(1, agent=agent_id) + run(
        1, start=5.5, agent=agent_id, cache_read=0, cache_write_5m=100_000
    )
    found = _checks(index(reqs, agents=[agent("a1", "coder")]))
    assert not {"subagent_cache_expiry", "subagent_resumed_late", "idle_cache_rewrites"} & set(
        kinds(found)
    )


# --------------------------------------------------------------------------- subagents


def _subagents(calls: tuple[str, ...]):  # type: ignore[no-untyped-def]
    reqs = run(10)  # the parent: 50k read per request, $0.01 each
    agents = []
    for i in range(3):
        aid = f"a{i}"
        reqs += run(
            1, start=20 + i, agent=aid, cache_read=0, cache_write_5m=40_000, calls={0: calls}
        )
        reqs += run(1, start=20.5 + i, agent=aid, cache_read=40_000)
        agents.append(agent(aid, "scout", tool_use_id=f"tu_{i}"))
    return index(reqs, agents=agents)


def test_subagents_for_one_command_save_their_cost_less_a_parent_turn() -> None:
    f = only(_checks(_subagents(("Bash",))), "one_command_subagents")
    agent_cost = 40_000 * 5e-6 + 40_000 * READ_RATE
    assert f.saving_usd_per_week == pytest.approx(3 * (agent_cost - 50_000 * READ_RATE))
    assert f.scope == "scout" and f.metrics["one_command"] == 3


def test_subagents_with_real_work_are_not_one_command() -> None:
    assert "one_command_subagents" not in kinds(_checks(_subagents(("Read", "Grep", "Bash"))))


def _report(tokens: int, compact_at: float | None = None):  # type: ignore[no-untyped-def]
    evs = [
        ev(EventKind.SPAWN, 0.1, tool_use_id="tu_x", agent_type="scout"),
        ev(EventKind.TOOL_RESULT, 0.5, tool="Agent", tool_use_id="tu_x", size_tokens=tokens),
    ]
    if compact_at is not None:
        evs.append(ev(EventKind.COMPACTION, compact_at, pre_tokens=100_000))
    return index(run(100), evs)


def test_a_long_report_saves_its_excess_over_25_lines_on_every_later_turn() -> None:
    f = only(_checks(_report(5_000)), "long_subagent_reports")
    assert f.scope == "scout"
    assert f.saving_usd_per_week == pytest.approx((5_000 - 500) * 100 * READ_RATE)


def test_a_compaction_ends_the_report_rereads() -> None:
    f = only(_checks(_report(5_000, compact_at=50.5)), "long_subagent_reports")
    assert f.saving_usd_per_week == pytest.approx((5_000 - 500) * 50 * READ_RATE)


def test_a_short_report_is_not_flagged() -> None:
    assert "long_subagent_reports" not in kinds(_checks(_report(800)))


# --------------------------------------------------------------------------- images


def test_images_in_tool_results_save_half_their_rereads() -> None:
    evs = [ev(EventKind.TOOL_RESULT, 0.5, tool="Read", is_image=True, size_tokens=1_600)]
    f = only(_checks(index(run(100), evs)), "images_in_tool_results")
    assert f.saving_usd_per_week == pytest.approx(0.5 * 1_600 * 100 * READ_RATE)
    assert "Read (1)" in f.summary


def test_text_tool_results_are_not_images() -> None:
    evs = [ev(EventKind.TOOL_RESULT, 0.5, tool="Read", is_image=False, size_tokens=1_600)]
    assert "images_in_tool_results" not in kinds(_checks(index(run(100), evs)))


# --------------------------------------------------------------------------- the whole run


def test_events_before_the_window_still_set_what_its_requests_carry() -> None:
    evs = [ev(EventKind.TOOL_RESULT, -120, tool="Read", is_image=True, size_tokens=1_600)]
    f = only(_checks(index(run(100), evs)), "images_in_tool_results")
    assert f.saving_usd_per_week == pytest.approx(0.5 * 1_600 * 100 * READ_RATE)


def test_findings_are_ranked_by_weekly_saving() -> None:
    reqs = run(10, cache_read=500_000) + _rewrite_after(30, cache_write_5m=100_000)
    found = _checks(index(reqs))
    assert len(found) >= 2
    savings = [f.saving_usd_per_week for f in found]
    assert savings == sorted(savings, reverse=True)


def test_logs_that_begin_inside_the_window_set_its_pace() -> None:
    reqs = run(100, start=60 * 24 * 5.5, cache_read=200_000)  # logs begin 1.5 days before the end
    f = only(
        run_checks(SortedIndex(reqs=reqs), TABLE, SINCE, UNTIL, agent_dirs=[]),
        "long_running_agents",
    )
    span = UNTIL - reqs[0].timestamp
    assert f.saving_usd_per_week == pytest.approx(
        (100 * 50_000 * READ_RATE - HANDOFF) * timedelta(days=7) / span
    )


def test_no_requests_gives_no_findings() -> None:
    assert _checks(index([])) == []


# --------------------------------------------------------------------------- agent definitions


def test_read_tools_takes_inline_and_block_lists(tmp_path: Path) -> None:
    inline = tmp_path / "a.md"
    inline.write_text("---\nname: a\ntools: Read, 'Grep', Bash\n---\nbody\n")
    block = tmp_path / "b.md"
    block.write_text("---\ntools:\n  - Read\n  - Edit\nmodel: opus\n---\n")
    none = tmp_path / "c.md"
    none.write_text("---\nname: c\n---\ntools: Read\n")
    plain = tmp_path / "d.md"
    plain.write_text("tools: Read\n")
    assert read_tools(inline) == ("Read", "Grep", "Bash")
    assert read_tools(block) == ("Read", "Edit")
    assert read_tools(none) is None
    assert read_tools(plain) is None
    assert read_tools(tmp_path / "missing.md") is None
    defs = find_definitions([tmp_path / "nowhere", tmp_path])
    assert sorted(defs) == ["a", "b", "c", "d"]


def test_definitions_are_keyed_by_their_frontmatter_name(tmp_path: Path) -> None:
    (tmp_path / "file-stem.md").write_text("---\nname: 'reviewer'\ntools: Read\n---\n")
    (tmp_path / "plain.md").write_text("---\ntools: Bash\n---\n")
    defs = find_definitions([tmp_path])
    assert sorted(defs) == ["plain", "reviewer"]
    assert defs["reviewer"].path == tmp_path / "file-stem.md"
    assert defs["reviewer"].tools == ("Read",)


def test_a_long_line_is_cut_and_its_rest_not_read_as_a_line(tmp_path: Path) -> None:
    long = tmp_path / "long.md"
    # the rest of the long line would read as a `tools:` line if it were not skipped
    long.write_text("---\ndescription: " + "x" * 5000 + "tools: Write\ntools: Read\n---\n")
    assert read_tools(long) == ("Read",)
    runaway = tmp_path / "runaway.md"
    runaway.write_text("---\ntools: Read\nnote: " + "y" * (3 << 20) + "\ntools: Bash\n---\n")
    assert read_tools(runaway) == ("Read",)


# --------------------------------------------------------------------------- the terminal view


def test_the_view_prints_ranked_findings_with_their_change(tmp_path: Path) -> None:
    evs = [
        ev(
            EventKind.TOOL_SET,
            0,
            source="mcp_instructions_delta",
            added=("claude.ai Docs",),
            size_tokens=20_000,
        )
    ]
    idx = index(run(200), evs)
    config = Config(tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache")
    out = io.StringIO()
    assert run_checks_view(config, days=7, limit=5, index=idx, out=out, now=at(60 * 24)) == 0
    text = out.getvalue()
    assert "claude.ai connectors in sessions that never use them" in text
    assert "ENABLE_CLAUDEAI_MCP_SERVERS" in text
    assert "/week" in text
    # the total the website shows, worded the same way
    assert "Together: up to $" in text and (
        "that doesn't overlap" in text or "that don't overlap" in text
    )


def test_the_view_says_so_when_the_index_is_empty(tmp_path: Path) -> None:
    config = Config(tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache")
    out = io.StringIO()
    assert run_checks_view(config, days=0, limit=5, index=index([]), out=out, now=at(60)) == 0
    assert "no findings" in out.getvalue() or "no requests" in out.getvalue()
