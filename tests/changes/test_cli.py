"""`capcom mark`, `capcom changes` and `capcom unmark` on a synthetic index."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import capcom.config
from capcom.changes import load
from capcom.changes.cli import parse_day, run_changes, run_mark, run_unmark
from capcom.cli import build_parser
from tests.changes.helpers import PROJECT, A, after, before, build, now_after


def _out() -> io.StringIO:
    return io.StringIO()


def test_mark_a_row_then_list_detail_and_unmark(tmp_path: Path) -> None:
    config = capcom.config.load_config()
    # in a subagent: a main thread's lifetime has no setting, so no saving to list
    idx = build({**before(40, 3), **after(40, 3)}, agent="a1")
    p = tmp_path / "c.jsonl"
    now = now_after()
    out = _out()
    assert (
        run_mark(
            config,
            "1",
            on=A.isoformat(),
            note="ran builds in the background",
            out=out,
            now=now,
            index=idx,
            path=p,
        )
        == 0
    )
    text = out.getvalue()
    assert "Marked #1" in text and "Tool-wait rewrites per 1,000 requests" in text
    assert "A verdict is due from 2026-10-08" in text
    [m] = load(p)
    # the cache-lifetime finding covers every project: the lifetime is one setting
    assert (m.kind, m.scope, m.applied_on) == ("subagent_cache_expiry", "all", A)
    assert m.expected_usd_per_week is not None

    out = _out()
    assert run_changes(config, out=out, now=now, index=idx, path=p) == 0
    assert "no clear effect" in out.getvalue()

    out = _out()
    assert run_changes(config, 1, out=out, now=now, index=idx, path=p) == 0
    detail = out.getvalue()
    assert f"applied {A}" in detail and "Evidence" in detail and "capcom unmark 1" in detail
    assert "=" * 10 in detail and "#" * 10 in detail  # before and after bars
    assert "note: ran builds in the background" in detail

    out = _out()
    assert run_unmark(config, 1, out=out, path=p) == 0
    assert load(p) == []
    assert run_unmark(config, 1, out=_out(), path=p) == 1


def test_mark_by_id_and_refusals(tmp_path: Path) -> None:
    config = capcom.config.load_config()
    idx = build({**before(40, 3), **after(40, 3)})
    p = tmp_path / "c.jsonl"
    kw = {"now": now_after(), "index": idx, "path": p}
    target = f"subagent_cache_expiry@{PROJECT}"
    assert run_mark(config, target, on="yesterday", other=True, out=_out(), **kw) == 0  # type: ignore[arg-type]
    assert load(p)[0].what == "other"
    assert run_mark(config, target, on="yesterday", out=_out(), **kw) == 1  # type: ignore[arg-type]
    assert run_mark(config, "99", out=_out(), **kw) == 1  # type: ignore[arg-type]
    assert run_mark(config, "nonsense@x", out=_out(), **kw) == 1  # type: ignore[arg-type]
    assert run_mark(config, target, on="2999-01-01", out=_out(), **kw) == 1  # type: ignore[arg-type]


def test_parse_day() -> None:
    now = now_after()
    today = now.astimezone().date()
    assert parse_day("today", now) == today
    assert (today - parse_day("yesterday", now)).days == 1
    with pytest.raises(ValueError):
        parse_day("soon", now)


def test_the_commands_parse() -> None:
    parser = build_parser()
    args = parser.parse_args(["mark", "7", "--on", "today", "--note", "x"])
    assert (args.finding, args.on, args.note, args.other) == ("7", "today", "x", False)
    assert parser.parse_args(["changes", "3"]).id == 3
    assert parser.parse_args(["changes", "--noticed"]).noticed
    assert parser.parse_args(["unmark", "3"]).id == 3


def test_the_mark_hint_is_printed_only_in_the_default_window() -> None:
    from capcom.checks.cli import format_findings
    from capcom.types import Finding

    f = Finding("idle_cache_rewrites", "Idle", "all", "s", 2.0, "a")
    assert "capcom mark 1" in format_findings([f], 5, mark_rows=True)
    plain = format_findings([f], 5)
    assert "id idle_cache_rewrites@all" in plain and "capcom mark" not in plain
