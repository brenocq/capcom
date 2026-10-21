"""Checks on how sessions run over days: sessions that never ended, heartbeats that
resume one growing session, and the model each agent type runs on. All sessions
are synthetic."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.checks import run_checks
from capcom.checks.rules import overlaps
from capcom.cost.model import price_usage
from capcom.types import EventKind, Finding, Request, SessionInfo, ToolCall
from tests.checks.helpers import (
    PROJECT,
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

HANDOFF = 40_000 * 5 / 1e6  # a 40k-token handoff at claude-opus-5-5's 5-minute write price


def _checks(idx: SortedIndex, agent_dirs: list[Path] | None = None) -> list[Finding]:
    return run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=agent_dirs or [])


def _with_spans(idx: SortedIndex, spans: dict[str, timedelta]) -> SortedIndex:
    idx.infos = [
        SessionInfo(sid, PROJECT, first_seen=UNTIL - span, last_seen=UNTIL)
        for sid, span in spans.items()
    ]
    return idx


# --------------------------------------------------------------------------- never ended


def _long_session(compactions: int, trigger: str = "manual"):  # type: ignore[no-untyped-def]
    """s1: 200 turns at 120k over three days, open for 10 days; s2: a fresh session
    of the same project at 40k."""
    reqs = run(200, step=21.6, cache_read=120_000)
    reqs += run(20, start=5_000, session="s2", cache_read=40_000)
    evs = [
        ev(EventKind.COMPACTION, 1_000 * (i + 1) + 0.5, pre_tokens=120_000, trigger=trigger)
        for i in range(compactions)
    ]
    spans = {"s1": timedelta(days=10), "s2": timedelta(hours=1)}
    return _with_spans(index(reqs, evs), spans)


def test_a_session_open_for_days_with_compactions_never_ended() -> None:
    idx = _long_session(3)
    f = only(_checks(idx), "session_never_ended")
    # a fresh session of the project holds 40k (rounded up to the 50k bucket edge):
    # each turn rereads 70k more, less a fresh start writing 50k on each day of use
    days = {r.timestamp.astimezone().date() for r in idx.reqs if r.session_id == "s1"}
    assert len(days) >= 3
    assert f.metrics["fresh_context"] == 50_000
    assert f.saving_usd_per_week == pytest.approx(
        200 * 70_000 * READ_RATE - len(days) * 50_000 * 5 / 1e6
    )
    assert f.metrics["sessions"] == 1 and f.metrics["longest_days"] == 10
    assert "handoff note" in f.advice
    assert f.evidence[0].session_id == "s1"


def test_two_manual_compactions_are_not_enough_but_two_automatic_ones_are() -> None:
    assert "session_never_ended" not in kinds(_checks(_long_session(2)))
    assert "session_never_ended" in kinds(_checks(_long_session(2, trigger="auto")))


def test_a_short_session_never_counts_as_never_ended() -> None:
    idx = _long_session(3)
    idx.infos = [replace(i, first_seen=UNTIL - timedelta(days=2)) for i in idx.infos]
    assert "session_never_ended" not in kinds(_checks(idx))


def test_never_ended_counts_only_below_the_long_context_line() -> None:
    reqs = run(200, cache_read=250_000) + run(20, start=300, session="s2", cache_read=40_000)
    evs = [ev(EventKind.COMPACTION, 50 * (i + 1) - 0.5, pre_tokens=250_000) for i in range(3)]
    idx = _with_spans(index(reqs, evs), {"s1": timedelta(days=10), "s2": timedelta(hours=1)})
    found = _checks(idx)
    f = only(found, "session_never_ended")
    assert f.saving_usd_per_week == pytest.approx(200 * 100_000 * READ_RATE - 50_000 * 5 / 1e6)
    # the part past 150k is long-running too: another way to the same rereads
    assert "long_running_agents" in kinds(found)
    assert "long_running_agents" in f.metrics["overlaps"]


# --------------------------------------------------------------------------- heartbeat


def _heartbeat(  # type: ignore[no-untyped-def]
    runs: int = 25, every: float = 30, grow: int = 4_000, jitter: bool = False
):
    """s3: `runs` runs of 3 turns, one every `every` minutes, each starting `grow`
    tokens bigger than the one before."""
    reqs: list[Request] = []
    for i in range(runs):
        start = i * every + (i % 2) * every * 0.6 * jitter
        reqs += run(3, start=start, session="s3", cache_read=40_000 + grow * (i + 1))
    evs = [ev(EventKind.COMPACTION, 10 + 100 * i, "s3", pre_tokens=60_000) for i in range(3)]
    return reqs, _with_spans(index(reqs, evs), {"s3": timedelta(days=12)})


def test_a_heartbeat_that_resumes_a_growing_session_is_flagged() -> None:
    reqs, idx = _heartbeat()
    found = _checks(idx)
    f = only(found, "heartbeat_growing_session")
    # a fresh run carries the smallest resumed context, 48k; each run of 3 turns
    # carries in what it started with above that
    expected = sum(max(0, r.usage.context - 48_000) for r in reqs) * READ_RATE
    assert f.saving_usd_per_week == pytest.approx(expected)
    assert f.metrics["resumes"] == 24 and f.metrics["interval_minutes"] == pytest.approx(30)
    assert "fresh" in f.advice and "/clear" in f.advice
    # a heartbeat open for days is not also a session that never ended
    assert "session_never_ended" not in kinds(found)


@pytest.mark.parametrize(
    "shape",
    [
        {"runs": 10},  # too few resumes
        {"grow": 0},  # the context does not grow
        {"jitter": True},  # no steady cadence
    ],
)
def test_irregular_short_or_flat_sessions_are_not_heartbeats(shape: dict[str, object]) -> None:
    _, idx = _heartbeat(**shape)  # type: ignore[arg-type]
    assert "heartbeat_growing_session" not in kinds(_checks(idx))


# --------------------------------------------------------------------------- model mix


def _subagents(  # type: ignore[no-untyped-def]
    model: str = "claude-opus-5-5", output: int = 300, spawns: int = 6, kind: str = "coder"
):
    reqs: list[Request] = []
    agents = []
    for i in range(spawns):
        aid = f"a{i}"
        for r in run(5, start=10 + i * 10, agent=aid, model=model, output=output):
            call = ToolCall(f"tu_{r.message_id}", "Bash", "pytest")
            reqs.append(replace(r, tool_calls=(call,)))
        agents.append(agent(aid, kind))
    return reqs, agents


def _as_sonnet(reqs: list[Request]) -> float:
    return sum(price_usage("claude-sonnet-5-5", r.usage, TABLE).total for r in reqs)


def _cost(reqs: list[Request]) -> float:
    return sum(price_usage(r.model, r.usage, TABLE).total for r in reqs)


def test_a_subagent_type_on_opus_with_short_tool_turns_gets_the_sonnet_saving(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "agents"
    folder.mkdir()
    (folder / "coder.md").write_text("---\nname: coder\nmodel: opus\n---\n")
    reqs, agents = _subagents()
    f = only(_checks(index(reqs, agents=agents), [folder]), "model_mix")
    assert f.scope == "coder" and f.metrics["model"] == "claude-opus-5-5"
    assert f.metrics["top_share"] == pytest.approx(1.0)
    assert f.saving_usd_per_week == pytest.approx(_cost(reqs) - _as_sonnet(reqs))
    assert f.change == "model: sonnet"
    assert str(folder / "coder.md") in f.advice and "judgement call" in f.advice
    # pricing the requests on Sonnet cuts every dollar they spent: it overlaps the rest
    assert {"long_running_agents", "agent_tools_unused"} <= set(overlaps("model_mix"))


def test_without_a_definition_file_model_mix_gives_no_line() -> None:
    reqs, agents = _subagents()
    f = only(_checks(index(reqs, agents=agents)), "model_mix")
    assert f.change is None and "no line to paste" in f.advice


@pytest.mark.parametrize(
    "shape",
    [
        {"model": "claude-sonnet-5-5"},  # already on Sonnet
        {"output": 4_000},  # long turns: the model's quality may matter
        {"spawns": 3},  # too few spawns
    ],
)
def test_other_agent_types_keep_their_model(shape: dict[str, object]) -> None:
    reqs, agents = _subagents(**shape)  # type: ignore[arg-type]
    assert "model_mix" not in kinds(_checks(index(reqs, agents=agents)))


def test_the_main_thread_is_not_judged_for_model_mix() -> None:
    reqs = run(50, output=300)
    assert "model_mix" not in kinds(_checks(index(reqs)))
    assert at(0) == SINCE  # the window the fixtures assume


def test_a_heartbeat_cut_back_by_compactions_still_carries_its_history() -> None:
    # the context grows, a compaction cuts it back, it grows again: the last runs
    # are no bigger than the first, but the runs carry about twice a fresh run
    reqs: list[Request] = []
    for i in range(26):
        reqs += run(3, start=i * 30, session="s4", cache_read=40_000 + 8_000 * (i % 13))
    idx = _with_spans(index(reqs), {"s4": timedelta(days=12)})
    f = only(_checks(idx), "heartbeat_growing_session")
    assert f.metrics["fresh_context"] == 40_000 and f.metrics["median_context"] == 88_000


def test_a_heartbeat_past_the_line_is_counted_once_as_a_heartbeat() -> None:
    reqs, idx = _heartbeat(grow=10_000)  # resumed contexts from 60k to 290k
    found = _checks(idx)
    f = only(found, "heartbeat_growing_session")
    expected = sum(max(0, r.usage.context - 60_000) for r in reqs) * READ_RATE
    assert f.saving_usd_per_week == pytest.approx(expected)
    assert "long_running_agents" not in kinds(found)


def test_a_heartbeat_run_is_charged_only_for_the_history_it_starts_with() -> None:
    # each run starts 4k bigger than the last and grows 5k, then 10k, within itself:
    # a fresh run would grow the same way
    reqs: list[Request] = []
    starts = []
    for i in range(25):
        start = 40_000 + 4_000 * (i + 1)
        starts.append(start)
        for j in range(3):
            reqs += run(1, start=i * 30 + j, session="s3", cache_read=start + 5_000 * j)
    idx = _with_spans(index(reqs), {"s3": timedelta(days=12)})
    f = only(_checks(idx), "heartbeat_growing_session")
    fresh = 48_000
    assert f.saving_usd_per_week == pytest.approx(
        sum(3 * max(0, s - fresh) for s in starts) * READ_RATE
    )


def test_a_slow_cadence_is_not_a_heartbeat_whatever_the_window() -> None:
    _, idx = _heartbeat(every=360)  # 25 runs, 4 a day over six days
    assert "heartbeat_growing_session" not in kinds(_checks(idx))


def test_a_pause_through_which_a_subagent_worked_is_not_a_resume() -> None:
    reqs, _ = _heartbeat()
    # in every gap a subagent of the session works until just before the next run
    reqs += [
        r
        for i in range(25)
        for r in run(1, start=i * 30 + 27, session="s3", agent=f"b{i}", cache_read=1_000)
    ]
    idx = _with_spans(
        index(reqs, agents=[agent(f"b{i}", "coder", "s3") for i in range(25)]),
        {"s3": timedelta(days=12)},
    )
    assert "heartbeat_growing_session" not in kinds(_checks(idx))
