"""Findings rolled up by kind across scopes, and the view's roll-up and footer."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from capcom.checks import rollup
from capcom.checks.cli import run_checks_view
from capcom.config import Config
from capcom.types import Finding
from tests.checks.helpers import began, index, run
from tests.cost.fakes import at


def _f(kind: str, scope: str, usd: float, title: str = "t") -> Finding:
    return Finding(kind, title, scope, "", usd, "", metrics={"overlaps": ()})


def test_a_kind_adds_up_its_scopes_with_their_shares() -> None:
    found = [
        _f("long_running_agents", "-p-a", 60.0),
        _f("long_running_agents", "-p-b", 40.0),
        _f("model_switches", "-p-a", 150.0),
        _f("agent_tools_unused", "coder", 3.0, title="The coder agent carries tools"),
    ]
    out = rollup(found)
    assert [r.kind for r in out] == ["model_switches", "long_running_agents", "agent_tools_unused"]
    long = out[1]
    assert long.saving_usd_per_week == pytest.approx(100.0)
    assert [(s.scope, s.share) for s in long.scopes] == [("-p-a", 0.6), ("-p-b", 0.4)]
    assert long.headline == "Long-running agents: up to $100.00/week across 2 projects"
    # a kind titled per scope gets one title across them
    assert (
        out[2].headline
        == "Agents carrying tools they never use: up to $3.00/week across 1 agent type"
    )


def test_the_same_scope_twice_adds_and_a_scope_of_all_is_not_added_again() -> None:
    out = rollup([_f("k", "x", 1.0), _f("k", "x", 2.0), _f("j", "all", 5.0), _f("j", "y", 2.0)])
    by = {r.kind: r for r in out}
    assert by["k"].saving_usd_per_week == pytest.approx(3.0) and len(by["k"].scopes) == 1
    assert by["j"].saving_usd_per_week == pytest.approx(5.0)


def test_the_view_prints_the_rollup_first_and_counts_small_findings(tmp_path: Path) -> None:
    # two projects with long contexts, one tiny model switch
    reqs = run(100, cache_read=200_000) + run(
        100, start=200, session="s2", project="-home-user-other", cache_read=200_000
    )
    reqs += run(1, start=400, session="s3", cache_read=10_000) + run(
        1, start=401, session="s3", model="claude-sonnet-5-5", cache_read=0, cache_write_5m=10_000
    )
    out = io.StringIO()
    config = Config(
        tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache", tmp_path / "state"
    )
    run_checks_view(config, 7, 10, index=began(index(reqs)), out=out, now=at(60 * 24))
    text = out.getvalue()
    head = text.index("By kind")
    assert (
        "Long-running agents: up to $10.64/week across 2 projects" in text[head:]
    )  # 2 days of logs
    assert text.index("By kind") < text.index("By scope")
    assert "Model switches" not in text  # $0.08/week: under the floor
    assert "1 more finding(s) under $0.10/week not listed" in text
    assert "Together: up to $10.64/week" in text


def test_a_judgement_call_is_listed_but_kept_out_of_the_total(tmp_path: Path) -> None:
    from tests.checks.test_sessions import _subagents

    subs, agents = _subagents()
    reqs = run(100, cache_read=200_000) + subs
    out = io.StringIO()
    config = Config(
        tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache", tmp_path / "state"
    )
    run_checks_view(
        config, 7, 10, index=began(index(reqs, agents=agents)), out=out, now=at(60 * 24)
    )
    text = out.getvalue()
    assert "The coder agent runs on Opus" in text
    assert "+ a judgement call: not added to the total" in text
    assert "* overlaps" not in text  # long-running is counted, not pushed out
    assert "Together: up to $5.32/week across 1 finding that doesn't overlap" in text


def test_the_website_total_leaves_judgement_calls_out_and_names_them() -> None:
    from capcom.report.tally import counted_findings, savings_total

    mix = Finding("model_mix", "T", "coder", "", 50.0, "", metrics={"judgement_call": True})
    long = _f("long_running_agents", "-p-a", 10.0)
    assert counted_findings([mix, long], {"-p-a"}) == [False, True]
    total = savings_total([mix, long], {"-p-a"})
    assert total is not None and total["usd_per_week"] == pytest.approx(10.0)
    assert total["judgement_calls"]["usd_per_week"] == 50.0
    assert total["judgement_calls"]["items"] == [
        {"kind": "model_mix", "scope": "coder", "usd_per_week": 50.0}
    ]
    # "of" leaves the judgement call out: every counted finding is all of them
    assert total["of"] == 1 and total["all"] is True
    assert "judgement calls on quality (1" in total["text"] and "$50.00/week more" in total["text"]


def test_the_website_total_applies_the_listing_floor() -> None:
    from capcom.report.tally import counted_findings, savings_total

    tiny = _f("model_switches", "-p-b", 0.05)
    long = _f("long_running_agents", "-p-a", 10.0)
    assert counted_findings([long, tiny], {"-p-a", "-p-b"}) == [True, False]
    total = savings_total([long, tiny], {"-p-a", "-p-b"})
    assert total is not None and total["of"] == 1 and total["all"] is True


def test_judgement_calls_come_last_in_the_rollup_and_say_so() -> None:
    from capcom.checks.cli import format_rollup

    mix = Finding("model_mix", "T", "coder", "", 50.0, "", metrics={"judgement_call": True})
    text = format_rollup([mix, _f("long_running_agents", "-p-a", 10.0)])
    lines = [line for line in text.splitlines() if line.startswith("  ") and "up to" in line]
    assert lines[0].startswith("  Long-running agents")
    assert lines[-1].endswith("(judgement call, not in the total)")
