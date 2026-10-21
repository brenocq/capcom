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
from capcom.report.tally import MIN_LISTED_USD_PER_WEEK  # the website's floor too
from capcom.timeline.groups import TEMPORARY_LABEL, ProjectNames
from capcom.types import Finding, Index

WIDTH = 88
ROLLUP_SCOPES = 4  # scopes named on a roll-up line


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


def _overlapped_by(
    findings: list[Finding], counted: list[bool | None], dirs: set[str]
) -> list[int]:
    """For each finding left out of the total, the number (1-based) of the first
    counted finding it shares dollars with (the total's own test); 0 for the
    counted ones."""
    from capcom.report.tally import overlap_blockers

    blockers = overlap_blockers(findings, dirs)
    return [hit[0] + 1 if counted[i] is False and hit else 0 for i, hit in enumerate(blockers)]


def finding_id(f: Finding, scope: str | None = None) -> str:
    """A finding's stable id across windows: "kind@scope", the scope as shown
    (`scope`, a project's name) or as the finding has it."""
    return f"{f.kind}@{f.scope if scope is None else scope}"


def format_findings(
    findings: list[Finding],
    limit: int,
    names: ProjectNames | None = None,
    dirs: set[str] | None = None,
    counted: list[bool | None] | None = None,
) -> str:
    """Findings as numbered blocks: saving, title and scope, summary, advice, the
    change to paste, and the first evidence lines. Project scopes (folders in `dirs`)
    are named by `names`, the website's rule, when given. A finding `counted` marks
    as left out of the total is starred, with the finding it overlaps; one it marks
    None (a judgement call) gets a dagger."""
    blocks: list[str] = []
    known = dirs if dirs is not None else set(names.paths) if names is not None else set()
    flags: list[bool | None] = list(counted) if counted is not None else [True] * len(findings)
    by = _overlapped_by(findings, flags, known)
    for i, f in enumerate(findings[: max(limit, 0)], 1):
        scope = _scope_label(f.scope, names, known)
        star = {True: " ", False: "*", None: "+"}[flags[i - 1]]
        lines = [f"{i:>2}.{star}${f.saving_usd_per_week:,.2f}/week  {f.title}  [{scope}]"]
        # the stable id `capcom mark` takes (or this row's number, in this window)
        lines.append(f"    id {finding_id(f, scope)}  (capcom mark {i})")
        if flags[i - 1] is None:
            lines.append("    + a judgement call: not added to the total")
        elif not flags[i - 1]:
            what = f"#{by[i - 1]}" if by[i - 1] else "a larger finding"
            lines.append(f"    * overlaps {what}: not added to the total")
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
    every = run_checks(
        index,
        table,
        since,
        until,
        claude_root=config.claude_root,
        openclaw_root=config.openclaw_root,
    )
    findings = [f for f in every if f.saving_usd_per_week >= MIN_LISTED_USD_PER_WEEK]
    small = [f for f in every if f.saving_usd_per_week < MIN_LISTED_USD_PER_WEEK]
    print("Savings are weekly, at this window's pace.\n", file=out)
    if not findings:
        print("no findings in this window", file=out)
        _print_small(small, out)
        return 0
    sessions = list(index.sessions())
    dirs = {s.project_dir for s in sessions}
    names = ProjectNames.build(sessions, config.openclaw_root)
    from capcom.report.tally import counted_findings, savings_total  # the website's rule

    print(format_rollup(findings, names, dirs), file=out)
    print("\nBy scope:\n", file=out)
    # a judgement call (model_mix) shrinks every saving on the same requests: it
    # stays out of the total, and the total is taken over the other findings
    judged = [bool(f.metrics.get("judgement_call")) for f in findings]
    flags = counted_findings(findings, dirs)  # never counts a judgement call
    counted: list[bool | None] = [None if j else c for j, c in zip(judged, flags, strict=True)]
    print(format_findings(findings, limit, names, dirs, counted), file=out)
    print(file=out)
    if any(judged):
        print(
            _wrap(
                "+ A judgement call on quality (a cheaper model for an agent): its saving "
                "would shrink the others on the same requests, so it is left out of the "
                "total.",
                "  ",
                first="",
            ),
            file=out,
        )
    if any(c is False for c in counted):
        print(
            _wrap(
                "* Overlapping: it counts some of the same dollars as the finding it "
                "names (a cost carried in prompts is also part of the context and cache "
                "rewrites it rode in), so it is left out of the total; the total covers "
                "only findings that don't overlap.",
                "  ",
                first="",
            ),
            file=out,
        )
    _print_small(small, out)
    total = savings_total(findings, dirs)
    if total is not None:
        print(f"Together: {total['text']}.", file=out)
    return 0


def _print_small(small: list[Finding], out: TextIO) -> None:
    if small:
        usd = sum(f.saving_usd_per_week for f in small)
        print(
            f"{len(small)} more finding(s) under ${MIN_LISTED_USD_PER_WEEK:.2f}/week not listed "
            f"(${usd:,.2f}/week together, not in the total).",
            file=out,
        )


def format_rollup(
    findings: list[Finding], names: ProjectNames | None = None, dirs: set[str] | None = None
) -> str:
    """One line per kind across its scopes, largest first, with the scopes' shares."""
    from capcom.checks.rollup import rollup

    known = dirs if dirs is not None else set(names.paths) if names is not None else set()
    lines = ["By kind, across scopes (kinds may overlap; see the total at the end):"]
    judged = {f.kind for f in findings if f.metrics.get("judgement_call")}
    for r in sorted(rollup(findings), key=lambda r: r.kind in judged):  # judgement calls last
        note = " (judgement call, not in the total)" if r.kind in judged else ""
        lines.append(f"  {r.headline}{note}")
        if len(r.scopes) > 1:
            parts = [
                f"{_scope_label(s.scope, names, known)} {s.share:.0%}"
                for s in r.scopes[:ROLLUP_SCOPES]
            ]
            more = len(r.scopes) - ROLLUP_SCOPES
            tail = f", {more} more" if more > 0 else ""
            lines.append(_wrap(", ".join(parts) + tail, "      "))
    return "\n".join(lines)
