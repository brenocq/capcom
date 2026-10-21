"""Checks on how sessions ran: context size, compactions, cache rewrites, subagents,
reports and images. All sessions are synthetic."""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.checks import run_checks
from capcom.checks.agents import find_definitions, read_tools
from capcom.checks.cli import run_checks_view
from capcom.config import Config
from capcom.types import EventKind, Finding
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


def _checks(idx) -> list[Finding]:  # type: ignore[no-untyped-def]
    return run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=[])


# --------------------------------------------------------------------------- context


def test_turns_past_150k_count_their_excess_context() -> None:
    f = only(_checks(index(run(10, cache_read=200_000))), "long_running_agents")
    assert f.saving_usd_per_week == pytest.approx(10 * 50_000 * READ_RATE)
    assert f.metrics["turns"] == 10 and f.metrics["max_context"] == 200_000


def test_contexts_under_150k_are_not_long_running() -> None:
    assert "long_running_agents" not in kinds(_checks(index(run(50, cache_read=100_000))))


def test_a_late_compaction_takes_the_part_above_400k_from_long_running() -> None:
    reqs = run(10, cache_read=500_000) + run(10, start=20, cache_read=100_000)
    evs = [ev(EventKind.COMPACTION, 15, pre_tokens=520_000, trigger="auto")]
    found = _checks(index(reqs, evs))
    late = only(found, "late_compactions")
    assert late.saving_usd_per_week == pytest.approx(10 * 100_000 * READ_RATE)
    assert late.metrics["max_pre_tokens"] == 520_000
    long = only(found, "long_running_agents")
    assert long.saving_usd_per_week == pytest.approx(10 * 250_000 * READ_RATE)


def test_without_a_compaction_long_running_keeps_the_whole_excess() -> None:
    found = _checks(index(run(10, cache_read=500_000)))
    assert "late_compactions" not in kinds(found)
    long = only(found, "long_running_agents")
    assert long.saving_usd_per_week == pytest.approx(10 * 350_000 * READ_RATE)


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
    reqs = run(10, start=60 * 24 * 5.5, cache_read=200_000)  # logs begin 1.5 days before the end
    f = only(
        run_checks(SortedIndex(reqs=reqs), TABLE, SINCE, UNTIL, agent_dirs=[]),
        "long_running_agents",
    )
    span = UNTIL - reqs[0].timestamp
    assert f.saving_usd_per_week == pytest.approx(
        10 * 50_000 * READ_RATE * timedelta(days=7) / span
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


def test_the_view_says_so_when_the_index_is_empty(tmp_path: Path) -> None:
    config = Config(tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache")
    out = io.StringIO()
    assert run_checks_view(config, days=0, limit=5, index=index([]), out=out, now=at(60)) == 0
    assert "no findings" in out.getvalue() or "no requests" in out.getvalue()
