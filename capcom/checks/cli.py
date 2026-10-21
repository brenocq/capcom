"""`capcom checks`: setup warnings in the terminal, ranked by weekly saving."""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from datetime import UTC, datetime
from typing import TextIO

from capcom.config import Config
from capcom.cost.prices import load_prices
from capcom.cost.top import window_start
from capcom.timeline.groups import TEMPORARY_LABEL, ProjectNames
from capcom.types import Finding, Index

WIDTH = 88


def _open_index(config: Config) -> Index | None:
    from capcom.store.index import open_index

    try:
        return open_index(config.index_path, readonly=True)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as err:
        print(f"capcom: cannot read the index at {config.index_path} ({err})", file=sys.stderr)
        print("run `capcom scan` first", file=sys.stderr)
        return None


def _wrap(text: str, indent: str, first: str | None = None) -> str:
    """`text` filled to WIDTH; the first line starts with `first` (default `indent`),
    the next ones with `indent`."""
    return textwrap.fill(
        text, WIDTH, initial_indent=indent if first is None else first, subsequent_indent=indent
    )


def _scope_label(scope: str, names: ProjectNames | None, dirs: set[str]) -> str:
    """A project scope named as on the website (ProjectNames); other scopes as is."""
    if names is None:
        return scope
    named = scope in dirs or scope.startswith("temporary (") or scope == TEMPORARY_LABEL
    return names.label(names.key(scope)) if named else scope


def format_findings(
    findings: list[Finding],
    limit: int,
    names: ProjectNames | None = None,
    dirs: set[str] | None = None,
) -> str:
    """Findings as numbered blocks: saving, title and scope, summary, advice, the
    change to paste, and the first evidence lines. Project scopes (folders in `dirs`)
    are named by `names`, the website's rule, when given."""
    blocks: list[str] = []
    known = dirs if dirs is not None else set(names.paths) if names is not None else set()
    for i, f in enumerate(findings[: max(limit, 0)], 1):
        scope = _scope_label(f.scope, names, known)
        lines = [f"{i:>2}. ${f.saving_usd_per_week:,.2f}/week  {f.title}  [{scope}]"]
        lines.append(_wrap(f.summary, "    "))
        lines.append(_wrap(f.advice, "    "))
        if f.change:
            lines.extend("      " + c for c in f.change.splitlines())
        lines.extend(_wrap(e.label, "      ", first="    - ") for e in f.evidence[:3])
        blocks.append("\n".join(lines))
    hidden = len(findings) - len(blocks)
    if hidden > 0:
        blocks.append(f"... {hidden} more (--limit {len(findings)} shows all)")
    return "\n\n".join(blocks)


def run_checks_view(
    config: Config,
    days: int,
    limit: int,
    index: Index | None = None,
    out: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    """Print the findings of the last `days` local days (0: all time)."""
    from capcom.checks import run_checks

    out = out or sys.stdout
    index = index or _open_index(config)
    if index is None:
        return 1
    table = load_prices()
    until = now or datetime.now(UTC)
    since = window_start(days, until)
    if since is None:
        first = next(iter(index.requests()), None)
        if first is None:
            print("no requests in the index", file=out)
            return 0
        since = first.timestamp
    window = "all time" if days <= 0 else f"last {days} days"
    print(
        f"capcom checks, {window}, since {since.astimezone():%Y-%m-%d %H:%M} "
        f"(prices checked {table.checked})",
        file=out,
    )
    findings = run_checks(
        index,
        table,
        since,
        until,
        claude_root=config.claude_root,
        openclaw_root=config.openclaw_root,
    )
    shown = findings[: max(limit, 0)]
    if any(f.metrics.get("overlaps") for f in shown):
        note = "some overlap, so add only the total below"
    else:
        note = "they count different dollars"
    print(f"Savings are weekly at this window's pace; {note}.\n", file=out)
    if not findings:
        print("no findings in this window", file=out)
        return 0
    sessions = list(index.sessions())
    dirs = {s.project_dir for s in sessions}
    names = ProjectNames.build(sessions, config.openclaw_root)
    print(format_findings(findings, limit, names, dirs), file=out)
    from capcom.report.tally import savings_total  # the website's total, worded the same

    total = savings_total(findings, dirs)
    if total is not None:
        print(f"\nTogether: {total['text']}.", file=out)
    return 0
