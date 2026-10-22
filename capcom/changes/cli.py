"""`capcom mark`, `capcom changes` and `capcom unmark`: "Did it work?" in the terminal.

Row numbers are those `capcom checks` prints with its defaults (the last 14 days),
so `capcom mark 3` marks the third finding listed there; `kind@scope` (the id that
listing prints) works in any window.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from capcom.changes.measure import BEFORE_WEEKS, MIN_DAYS, MIN_REQUESTS
from capcom.changes.measures import MEASURES
from capcom.changes.store import MAX_NOTE_CHARS, clean_text
from capcom.config import Config
from capcom.types import ChangeMark, ChangeResult, Finding, Index, PriceTable, Verdict

CHECKS_DAYS = 14  # `capcom checks`' default window
DAY_CACHE = "changes-days.sqlite"  # finished days, in the cache folder
BAR = 28  # the chart's widest bar, in characters
VERDICTS = {
    Verdict.WORKING: "working",
    Verdict.NO_EFFECT: "no clear effect",
    Verdict.WORSE: "worse",
    Verdict.TOO_EARLY: "too early",
    Verdict.NO_DATA: "can't tell",
}


@dataclass(slots=True)
class _Context:
    index: Index
    table: PriceTable
    labels: dict[str, str]  # scope -> the name shown for it

    def label(self, scope: str) -> str:
        return self.labels.get(scope, scope)


def _open(config: Config, index: Index | None) -> _Context | None:
    from capcom.checks.cli import _open_index, _scope_label
    from capcom.cost.prices import load_prices
    from capcom.timeline.groups import ProjectNames

    index = index or _open_index(config)
    if index is None:
        return None
    sessions = list(index.sessions())
    dirs = {s.project_dir for s in sessions}
    names = ProjectNames.build(sessions, config.openclaw_root)
    from capcom.cost.top import project_label

    labels = {}
    for d in dirs:
        group = project_label(d)
        labels[group] = _scope_label(group, names, dirs)
    return _Context(index, load_prices(), labels)


def _findings(config: Config, ctx: _Context, now: datetime) -> list[Finding]:
    """The findings `capcom checks` lists with its defaults, in its order."""
    from capcom.checks import run_checks
    from capcom.cost.top import window_start
    from capcom.report.tally import MIN_LISTED_USD_PER_WEEK

    since = window_start(CHECKS_DAYS, now)
    assert since is not None
    every = run_checks(
        ctx.index,
        ctx.table,
        since,
        now,
        claude_root=config.claude_root,
        openclaw_root=config.openclaw_root,
    )
    return [f for f in every if f.saving_usd_per_week >= MIN_LISTED_USD_PER_WEEK]


def parse_day(text: str, now: datetime) -> date:
    """today, yesterday or YYYY-MM-DD, as a local date; never in the future."""
    today = now.astimezone().date()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        d = date.fromisoformat(text)
    except ValueError as err:
        raise ValueError(f"not a date: {text!r} (today, yesterday or YYYY-MM-DD)") from err
    if d > today:
        raise ValueError(f"{d} is in the future")
    return d


def _resolve(
    target: str, findings: Sequence[Finding], ctx: _Context
) -> tuple[str, str, str, float | None]:
    """(kind, scope, title, expected saving) of a row number or a kind@scope."""
    if target.isdigit():
        row = int(target)
        if not 1 <= row <= len(findings):
            raise ValueError(
                f"no finding {row}: `capcom checks` lists {len(findings)} in its default window"
            )
        f = findings[row - 1]
        return f.kind, f.scope, f.title, f.saving_usd_per_week
    kind, sep, scope = target.partition("@")
    if not sep or not kind or not scope:
        raise ValueError(f"not a row number or kind@scope: {target!r}")
    for f in findings:
        if f.kind == kind and scope in (f.scope, ctx.label(f.scope)):
            return f.kind, f.scope, f.title, f.saving_usd_per_week
    for raw, shown in ctx.labels.items():  # a project named as `capcom checks` names it
        if scope == shown:
            scope = raw
            break
    if kind not in MEASURES:
        raise ValueError(f"no finding {kind}@{scope} in the last {CHECKS_DAYS} days")
    return kind, scope, kind.replace("_", " ").capitalize(), None


def run_mark(
    config: Config,
    target: str,
    on: str = "today",
    other: bool = False,
    note: str = "",
    noticed: bool = False,
    out: TextIO | None = None,
    now: datetime | None = None,
    index: Index | None = None,
    path: Path | None = None,
) -> int:
    from capcom.changes.store import mark

    out = out or sys.stdout
    now = now or datetime.now(UTC)
    ctx = _open(config, index)
    if ctx is None:
        return 1
    try:
        day = parse_day(on, now)
        kind, scope, title, expected = _resolve(target, _findings(config, ctx, now), ctx)
        m = mark(
            kind,
            scope,
            day,
            title,
            what="other" if other else "suggested",
            note=note,
            expected_usd_per_week=expected,
            source="noticed" if noticed else "user",
            path=path,
        )
    except (ValueError, KeyError) as err:
        print(f"capcom: {err}", file=sys.stderr)
        return 1
    print(f"Marked #{m.id}: {m.title} [{ctx.label(m.scope)}], applied on {m.applied_on}.", file=out)
    measure = MEASURES.get(m.kind)
    if measure is None:
        print(
            f"capcom has no measure for {m.kind}: `capcom changes {m.id}` will say no data.",
            file=out,
        )
        return 0
    start = m.applied_on - timedelta(days=7 * BEFORE_WEEKS)
    print(
        f"Measured: {measure.label}, in {ctx.label(m.scope)}: the {BEFORE_WEEKS} weeks "
        f"from {start} to {m.applied_on - timedelta(days=1)} against the weeks from "
        f"{m.applied_on + timedelta(days=1)}.",
        file=out,
    )
    print(
        f"A verdict is due from {m.applied_on + timedelta(days=1 + MIN_DAYS)}, once "
        f"{MIN_REQUESTS} requests or more ran in scope; see `capcom changes {m.id}`.",
        file=out,
    )
    return 0


def run_unmark(
    config: Config, mark_id: int, out: TextIO | None = None, path: Path | None = None
) -> int:
    from capcom.changes.store import load, unmark

    out = out or sys.stdout
    found = {m.id: m for m in load(path)}
    try:
        unmark(mark_id, path=path)
    except KeyError:
        print(f"capcom: no open mark {mark_id} (see `capcom changes`)", file=sys.stderr)
        return 1
    m = found[mark_id]
    print(f"Unmarked #{m.id}: {m.title}, applied on {m.applied_on}.", file=out)
    return 0


# --------------------------------------------------------------------------- views


def _value(result: ChangeResult, v: float | None) -> str:
    m = MEASURES.get(result.mark.kind)
    if v is None:
        return "-"
    return m.fmt.format(v) if m else f"{v:,.2f}"


def _usd(v: float | None) -> str:
    if v is None:
        return "-"
    v = round(v, 2) + 0.0  # no "-$0.00"
    return f"${v:,.2f}" if v >= 0 else f"-${-v:,.2f}"


def format_list(results: Sequence[ChangeResult], ctx: _Context) -> str:
    if not results:
        return "No changes marked. Mark one with `capcom mark <row from capcom checks>`."
    lines = [
        f"{'#':>3}  {'applied':<10}  {'verdict':<15}  {'before':>9}  {'after':>9}  saving/week"
    ]
    for r in results:
        m = r.mark
        expected = (
            f" (expected {_usd(m.expected_usd_per_week)})"
            if m.expected_usd_per_week is not None
            else ""
        )
        saving = _usd(r.measured_usd_per_week) if r.measured_usd_per_week is not None else "-"
        verdict = VERDICTS[r.verdict]
        if r.verdict is Verdict.TOO_EARLY and r.verdict_due:
            verdict = f"due {r.verdict_due:%m-%d}"
        lines.append(
            f"{m.id:>3}  {m.applied_on.isoformat():<10}  {verdict:<15}  "
            f"{_value(r, r.before):>9}  {_value(r, r.after):>9}  {saving}{expected}"
        )
        other = ", another change" if m.what == "other" else ""
        lines.append(f"     {clean_text(m.title)} [{ctx.label(m.scope)}]{other}")
        lines.append(f"     {r.measure}")
    return "\n".join(lines)


def _chart(r: ChangeResult) -> list[str]:
    values = [p.value for p in r.weeks if p.value is not None]
    top = max(values, default=0.0)
    lines: list[str] = []
    shown_line = False
    for p in r.weeks:
        if p.after and not shown_line:
            lines.append(f"      {'-' * 4} applied {r.mark.applied_on} {'-' * 4}")
            shown_line = True
        if p.value is None:
            bar, text = "", "no requests"
        else:
            n = round(p.value / top * BAR) if top > 0 else 0
            bar = ("#" if p.after else "=") * max(n, 1 if p.value > 0 else 0)
            text = _value(r, p.value)
        lines.append(f"      {p.week_start}  {bar:<{BAR}}  {text:>9}  {p.requests:,} req")
    if not shown_line:
        lines.append(f"      {'-' * 4} applied {r.mark.applied_on} {'-' * 4}")
    return lines


def _table(title: str, rows: Sequence[tuple[str, str, str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(label) for label, _, _ in rows)
    lines = [f"    {title:<{width}}  {'before':>12}  {'after':>12}"]
    lines += [f"    {label:<{width}}  {b:>12}  {a:>12}" for label, b, a in rows]
    return lines


def format_detail(r: ChangeResult, ctx: _Context) -> str:
    m = r.mark
    what = "the suggested change" if m.what == "suggested" else "another change"
    marked = f", marked {m.marked_at.astimezone():%Y-%m-%d}" if m.marked_at else ""
    source = " (noticed by capcom)" if m.source == "noticed" else ""
    lines = [
        f"#{m.id}  {clean_text(m.title)}  [{ctx.label(m.scope)}]",
        f"    {m.kind}@{ctx.label(m.scope)}: {what} applied on {m.applied_on}{marked}{source}",
    ]
    if m.note:
        lines.append(f"    note: {clean_text(m.note, MAX_NOTE_CHARS)}")
    lines.append("")
    lines.append(f"    Verdict: {VERDICTS[r.verdict]}. {r.reason}")
    if r.weeks:
        lines += ["", f"    {r.measure}, weekly (= before, # after):", *_chart(r)]
        rng = (
            f" (weeks from {_value(r, r.before_range[0])} to {_value(r, r.before_range[1])})"
            if r.before_range
            else ""
        )
        lines += [
            "",
            f"    Before {_value(r, r.before)}{rng}, after {_value(r, r.after)}; requests per "
            f"week in scope {r.requests_per_week_before:,.0f} before, "
            f"{r.requests_per_week_after:,.0f} after.",
        ]
    saving = []
    if r.measured_usd_per_week is not None:
        saving.append(f"measured {_usd(r.measured_usd_per_week)}/week")
    if m.expected_usd_per_week is not None:
        saving.append(f"expected {_usd(m.expected_usd_per_week)}/week when marked")
    if saving:
        lines.append(f"    Saving: {', '.join(saving)} (API-equivalent).")
    if r.evidence:
        lines += ["", *_table("Evidence", r.evidence)]
    if r.quality:
        lines += [
            "",
            *_table("Quality (a judgement call)", r.quality),
            "    A cheaper model is worth keeping only if the work stays as good.",
        ]
    lines += [
        "",
        f"    Undo: capcom unmark {m.id}  (wrong date: unmark, then mark again with --on)",
    ]
    return "\n".join(lines)


def run_changes(
    config: Config,
    mark_id: int | None = None,
    days: int = 0,
    noticed: bool = False,
    out: TextIO | None = None,
    now: datetime | None = None,
    index: Index | None = None,
    path: Path | None = None,
    day_cache: Path | None = None,
) -> int:
    """The marks with their verdicts (`days`: those applied in the last N days, 0:
    all), one mark in detail, or (`noticed`) the changes capcom saw."""
    from capcom.changes.measure import measure_all
    from capcom.changes.store import load

    out = out or sys.stdout
    now = now or datetime.now(UTC)
    marks: list[ChangeMark] = load(path)
    if noticed:
        ctx = _open(config, index)
        return 1 if ctx is None else _print_noticed(config, ctx, now, marks, out)
    if mark_id is not None:
        marks = [m for m in marks if m.id == mark_id]
        if not marks:
            print(f"capcom: no open mark {mark_id} (see `capcom changes`)", file=sys.stderr)
            return 1
    elif days > 0:
        first = now.astimezone().date() - timedelta(days=days - 1)
        marks = [m for m in marks if m.applied_on >= first]
    ctx = _open(config, index)
    if ctx is None:
        return 1
    cache = day_cache or config.cache_dir / DAY_CACHE
    results = measure_all(ctx.index, ctx.table, marks, now, day_cache=cache)
    if mark_id is not None:
        print(format_detail(results[0], ctx), file=out)
    else:
        print(format_list(results, ctx), file=out)
        if results:
            print("\n`capcom changes ID` shows one in detail.", file=out)
    return 0


def _print_noticed(
    config: Config, ctx: _Context, now: datetime, marks: list[ChangeMark], out: TextIO
) -> int:
    from capcom.changes.noticed import noticed

    found = noticed(ctx.index, ctx.table, _findings(config, ctx, now), now, marks=marks)
    if not found:
        print("No changes noticed in the agent definitions.", file=out)
        return 0
    print("Changes capcom noticed (confirm one to measure it):\n", file=out)
    for n in found:
        on = n.since.astimezone().date()
        print(f"  {n.title} [{ctx.label(n.scope)}]", file=out)
        print(f"    {n.path}: now {n.detail}, changed {on}", file=out)
        target = shlex.quote(f"{n.kind}@{ctx.label(n.scope)}")
        print(f"    capcom mark {target} --on {on} --noticed", file=out)
    return 0
