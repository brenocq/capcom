"""The calibration note: spend outside every process step, the output range, the
calls the log does not record, and the unexplained rest (synthetic numbers)."""

from __future__ import annotations

from datetime import UTC, datetime

from capcom.cost.prices import load_prices
from capcom.report.calibration import (
    Part,
    difference_note,
    logged_more_mostly,
    model_label,
    output_range,
    part_of,
    rest_summary,
    unlogged_mostly,
    unlogged_text,
)
from capcom.types import Calibration, CostState
from tests.cost.fakes import FakeIndex, at, req

T = datetime(2026, 10, 5, 19, 33, tzinfo=UTC)


def _part(session_usd: float, gaps: bool = False) -> Part:
    return Part(T, None, T, T, 3, 4 if gaps else 3, False, session_usd, gaps=gaps)


def test_model_label() -> None:
    assert model_label("claude-opus-5-5") == "Opus 5.5"
    assert model_label("claude-haiku-4-5") == "Haiku 4.5"
    assert model_label("gpt-x") == "gpt-x"


def test_note_names_every_part_of_the_gap() -> None:
    c = Calibration(
        "s1",
        capcom_usd=650.0,
        claude_code_usd=833.0,
        outside_usd=350.0,
        output_above={"claude-opus-5-5": 80.0},
        unlogged={"claude-opus-5-5": {"cache_read": 90.0, "input": 7.0}},
        logged_more={"claude-haiku-4-5": {"output": 2.0}},
        comparable=False,
    )
    assert output_range(c) == "≥ $650.00, up to $730.00"
    assert unlogged_text(c) == (
        "by token counts, Claude Code saw about $97.00 in calls the log doesn't record "
        "(mostly cache reads on Opus 5.5)"
    )
    assert unlogged_mostly(c) == "mostly cache reads on Opus 5.5"
    assert logged_more_mostly(c) == "mostly output on Haiku 4.5"
    note = difference_note(c, _part(1000.0, gaps=True), UTC)
    assert note is not None
    assert "3 of 4 processes" in note
    assert "the other $350.00 is spend in a Claude Code process that wrote no cost-state" in note
    assert "≥ $650.00, up to $730.00" in note
    assert "about $80.00 more output than capcom's estimate" in note
    assert "By token counts, Claude Code saw about $97.00 in calls the log" in note
    assert "The log counts about $2.00 more than Claude Code's estimate (mostly output" in note
    assert "The rest, +$8.00, is unexplained." in note


def test_no_note_when_everything_matches() -> None:
    c = Calibration("s1", capcom_usd=10.0, claude_code_usd=10.0)
    assert difference_note(c, _part(10.0), UTC) is None
    assert output_range(c) is None and unlogged_text(c) is None
    assert unlogged_mostly(c) is None and logged_more_mostly(c) is None


def test_rest_summary() -> None:
    checks = [
        Calibration("a", 10.0, 10.0, outside_usd=1.0),
        Calibration(
            "b",
            50.0,
            80.0,
            comparable=False,
            outside_usd=5.0,
            output_above={"m": 10.0},
            unlogged={"m": {"input": 15.0}},
            logged_more={"m": {"output": 1.0}},
        ),
    ]
    text = rest_summary(checks)
    assert text.splitlines()[0] == (
        "on the rest: $5.00 in processes with no cost-state; by token counts, about $10.00 "
        "of output above capcom's estimate, $15.00 in calls the log doesn't show and $1.00 "
        "the log counts more; +$6.00 unexplained"
    )
    assert "comparable sessions: $1.00 more in processes with no cost-state" in text


def test_processes_are_counted_by_the_gaps_between_steps() -> None:
    # Steps [0, 60) and [100, 130) (a restore); requests at 70 and 140 lie in two
    # different gaps, so 2 + 2 processes, though no request sits in the second step.
    start = int(at(0).timestamp() * 1000)

    def cs(offset: int, total: float, when: int, nxt: int | None, dur: int) -> CostState:
        return CostState(
            "s1", offset, total, {}, start, None, at(when), None if nxt is None else at(nxt), dur
        )

    idx = FakeIndex(
        reqs=[req("s1", when=at(t), output=1000) for t in (5, 70, 140)],
        costs=[cs(100, 1.0, 50, 60, 3_600_000), cs(200, 1.5, 125, 130, 3_600_000 + 1_500_000)],
    )
    p = part_of(idx, idx.costs[-1], load_prices())
    assert (p.process, p.processes) == (2, 4)
    assert p.gaps and p.later and not p.earlier
