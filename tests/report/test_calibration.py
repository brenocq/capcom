"""The calibration note: spend outside every process step, the output range, the
calls the log does not record, and the unexplained rest (synthetic numbers)."""

from __future__ import annotations

from datetime import UTC, datetime

from capcom.report.calibration import (
    Part,
    difference_note,
    model_label,
    output_range,
    rest_summary,
    unlogged_text,
)
from capcom.types import Calibration

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
        comparable=False,
    )
    assert output_range(c) == "≥ $650.00, up to $730.00"
    assert unlogged_text(c) == (
        "calls Claude Code counted that the log doesn't record: $97.00 "
        "(mostly cache reads on Opus 5.5)"
    )
    note = difference_note(c, _part(1000.0, gaps=True), UTC)
    assert note is not None
    assert "3 of 4 processes" in note
    assert "the other $350.00 is spend in a Claude Code process that wrote no cost-state" in note
    assert "≥ $650.00, up to $730.00" in note
    assert "Calls Claude Code counted that the log doesn't record: $97.00" in note
    assert "The rest, +$6.00, is unexplained." in note


def test_no_note_when_everything_matches() -> None:
    c = Calibration("s1", capcom_usd=10.0, claude_code_usd=10.0)
    assert difference_note(c, _part(10.0), UTC) is None
    assert output_range(c) is None and unlogged_text(c) is None


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
        ),
    ]
    text = rest_summary(checks)
    assert text.splitlines()[0] == (
        "on the rest: $5.00 in processes with no cost-state, $10.00 of output above "
        "capcom's estimate, $15.00 in calls the log doesn't show, +$5.00 unexplained"
    )
    assert "comparable sessions: $1.00 more in processes with no cost-state" in text
