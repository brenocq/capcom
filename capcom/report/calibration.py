"""capcom's totals against Claude Code's cost-state, per session, for the session page
and `capcom calibrate`: which part of the session a cost-state compares (its process's
window: start to end, and "process n of m" when the session was resumed), and the one
line that explains why the session's own total differs when it does.

Claude Code's cost-state counts one process only (capcom.cost.calibrate): a resumed
session's earlier processes are in capcom's session total but not in the comparison.
Both pages show the same two numbers, the compared part and the whole session, so
one session never shows two unexplained totals.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, TextIO

from capcom.config import Config
from capcom.cost.calibrate import TARGET_SPREAD, check_session, window
from capcom.cost.model import price
from capcom.cost.prices import is_placeholder, load_prices
from capcom.cost.top import _by_reason, _shorten, _spread, _table, usd
from capcom.timeline.groups import ProjectNames
from capcom.types import Calibration, CostState, Index, PriceTable, SessionInfo

# Why a session's totals cannot be compared with Claude Code's cost-state
# (capcom.cost.calibrate reason ids), in words for the report.
REASON_LABELS = {
    "WebFetch": "WebFetch calls, whose page summaries Claude Code bills separately",
    "WebSearch": "web searches, billed per search",
    "away": "away summaries, which the log does not record",
    "compaction": "compaction summaries, which the log does not record",
    "calls not in the log": "model calls missing from the log",
    "shares replies with another session": "replies shared with another session",
    "model only in cost-state": "a model only Claude Code recorded",
    "no startTime": "no process start time, so the window is a guess",
    "below $0.10": "a total under $0.10, where fixed side calls weigh too much",
    "estimated output": "estimated output tokens",
}
SAME_USD = 0.005  # totals closer than this are the same total


@dataclass(frozen=True, slots=True)
class Part:
    """The part of a session a cost-state compares."""

    since: datetime | None  # the process start (None: unknown, the whole log)
    until: datetime | None  # the first record after the cost-state (None: open)
    first: datetime | None  # the first and last request inside the window
    last: datetime | None
    process: int | None  # n of `processes`, None when the start is not known
    processes: int  # processes that wrote a cost-state (at least 1)
    earlier: bool  # requests before the window: a resumed session
    session_usd: float  # every request of the session
    later: bool = False  # requests after the window: a process with no cost-state yet

    @property
    def whole(self) -> bool:
        return not self.earlier and not self.later


def part_of(index: Index, cs: CostState, table: PriceTable) -> Part:
    """Where `cs`'s window sits in its session (one pass over its requests).

    Processes are counted from the starts the cost-states record, plus the ones that
    wrote none: one more when the session has requests before the first recorded
    start, and one more when it has requests after this (the last) cost-state's
    window. Processes with no cost-state between two recorded starts leave no trace
    in the logs, so `processes` is a lower bound."""
    since, until = window(cs)
    starts_fn = getattr(index, "process_starts", None)
    starts = (starts_fn(cs.session_id) if starts_fn else {}).get(cs.session_id, [])
    if cs.start_time_ms is not None and cs.start_time_ms not in starts:
        starts = sorted([*starts, cs.start_time_ms])
    first_start = datetime.fromtimestamp(starts[0] / 1000, UTC) if starts else None
    total, earlier, later, before_first = 0.0, False, False, False
    first = last = None
    for r in index.requests(session_id=cs.session_id):
        if is_placeholder(r.model):
            continue
        total += price(r, table).total
        if first_start is not None and r.timestamp < first_start:
            before_first = True
        if since is not None and r.timestamp < since:
            earlier = True
        elif until is None or r.timestamp < until:
            first = first or r.timestamp
            last = r.timestamp
        else:
            later = True
    ahead = 1 if before_first else 0
    process = starts.index(cs.start_time_ms) + 1 + ahead if cs.start_time_ms is not None else None
    processes = max(len(starts), 1) + ahead + (1 if later else 0)
    return Part(since, until, first, last, process, processes, earlier, total, later)


def _when(t: datetime | None, tz: tzinfo | None) -> str:
    if t is None:
        return "?"
    local = t.astimezone(tz)
    return f"{local:%b} {local.day} {local:%H:%M}"


def part_text(p: Part, tz: tzinfo | None = None) -> str:
    """'Oct 16 10:02 to Oct 21 14:10, process 2 of 3', or 'whole session'; with
    ', later requests not in it' when the session ran on after the cost-state."""
    if p.whole:
        return "whole session"
    span = f"{_when(p.since or p.first, tz)} to {_when(p.last, tz)}"
    tail = ", later requests not in it" if p.later else ""
    if p.process is not None and p.processes > 1:
        return f"{span}, process {p.process} of {p.processes}{tail}"
    return f"{span}, one process{tail}"


def difference_note(c: Calibration, p: Part, tz: tzinfo | None = None) -> str | None:
    """One line on why the session's total is not the compared total; None when
    they agree."""
    if abs(p.session_usd - c.capcom_usd) < SAME_USD:
        return None
    which = (
        f"process {p.process} of {p.processes}"
        if p.process is not None and p.processes > 1
        else "one process"
    )
    span = f"from {_when(p.since or p.first, tz)}"
    if p.later:
        span += f" to {_when(p.last, tz)}, later requests not in it"
    return (
        f"Claude Code's cost-state counts only {which} ({span}): "
        f"capcom counts {usd(c.capcom_usd)} for that part, {usd(p.session_usd)} for the "
        "whole session."
    )


def calibration_json(
    index: Index, cs: CostState, table: PriceTable, tz: tzinfo | None = None
) -> dict[str, Any]:
    """The session page's `calibration` (API.md)."""
    c = check_session(index, cs, table)
    p = part_of(index, cs, table)
    return {
        "claude_code_usd": round(c.claude_code_usd, 6),
        "capcom_usd": round(c.capcom_usd, 6),
        "spread": None if c.claude_code_usd == 0 else round(c.spread, 6),
        "comparable": c.comparable,
        "reasons": list(c.reasons),
        "reason_labels": [REASON_LABELS.get(r, r) for r in c.reasons],
        "part": {
            "since": _iso(p.since, tz),
            "until": _iso(p.until, tz),
            "first": _iso(p.first, tz),
            "last": _iso(p.last, tz),
            "process": p.process,
            "processes": p.processes,
            "whole_session": p.whole,
            "text": part_text(p, tz),
        },
        "session_usd": round(p.session_usd, 6),
        "note": difference_note(c, p, tz),
    }


def _iso(t: datetime | None, tz: tzinfo | None) -> str | None:
    return None if t is None else t.astimezone(tz).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- CLI


def _matches(project: str, info: SessionInfo | None, names: ProjectNames) -> bool:
    if info is None:
        return False
    label = names.label(names.key(info.project_dir))
    return project in (label, names.path(info.project_dir), info.project_dir)


def format_rows(
    rows: list[tuple[Calibration, Part, SessionInfo | None]],
    names: ProjectNames,
    tz: tzinfo | None = None,
) -> str:
    header = ["session", "project", "title", "cost-state", "capcom", "spread", "compares", "scope"]
    body = []
    for c, p, info in rows:
        project = names.label(names.key(info.project_dir)) if info else "?"
        body.append(
            [
                c.session_id[:8],
                _shorten(project, 24),
                _shorten((info.title if info else None) or "", 32),
                usd(c.claude_code_usd),
                usd(c.capcom_usd),
                _spread(c.capcom_usd, c.claude_code_usd),
                part_text(p, tz),
                "comparable" if c.comparable else ", ".join(c.reasons),
            ]
        )
    return _table(header, body, left=3, last_left=True)


def _summary(name: str, group: list[Calibration]) -> str:
    if not group:
        return f"{name}: none"
    cc = sum(c.claude_code_usd for c in group)
    mine = sum(c.capcom_usd for c in group)
    within = sum(1 for c in group if abs(c.spread) <= TARGET_SPREAD)
    return (
        f"{name}: {len(group)} sessions, {within} within {TARGET_SPREAD:.0%}, "
        f"cost-state {usd(cc)}, capcom {usd(mine)} ({_spread(mine, cc)})"
    )


def run_calibrate(
    config: Config,
    limit: int,
    days: int = 0,
    project: str | None = None,
    index: Index | None = None,
    out: TextIO | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> int:
    """`capcom calibrate`: the sessions with a cost-state (written in the last `days`
    local days, 0: all; of `project` when given), costliest first, each with the
    part of the session it compares."""
    from capcom.cost.top import _open_index, window_start

    out = out or sys.stdout
    index = index or _open_index(config)
    if index is None:
        return 1
    table = load_prices()
    infos: Mapping[str, SessionInfo] = {s.session_id: s for s in index.sessions()}
    names = ProjectNames.build(list(infos.values()), config.openclaw_root)
    since = window_start(days, now) if days > 0 else None
    states = []
    for cs in index.last_cost_states():
        info = infos.get(cs.session_id)
        # active in the window: its last record, or its cost-state's
        seen = max((t for t in (cs.timestamp, info.last_seen if info else None) if t), default=None)
        if since is not None and (seen is None or seen < since):
            continue
        if project and not _matches(project, info, names):
            continue
        states.append(cs)
    if not states:
        print("no session with a cost-state matches", file=out)
        return 0
    checks = [check_session(index, cs, table) for cs in states]
    pairs = sorted(zip(checks, states, strict=True), key=lambda x: -x[0].claude_code_usd)
    shown = pairs[:limit] if limit > 0 else pairs
    rows = [(c, part_of(index, cs, table), infos.get(c.session_id)) for c, cs in shown]
    parts = [format_rows(rows, names, tz)]
    if len(pairs) > len(shown):
        parts.append(f"{len(pairs) - len(shown)} more sessions (--limit)")
    notes = [n for c, p, _ in rows if (n := difference_note(c, p, tz))]
    if notes:
        parts.append(
            f"{len(notes)} of these were resumed: cost-state covers only the part in "
            "`compares`, and capcom's column is that part too; the session page shows "
            "both it and the whole session."
        )
    parts.append(_summary("comparable", [c for c in checks if c.comparable]))
    parts.append(_summary("all", checks))
    parts.append(_by_reason([c for c in checks if not c.comparable]))
    print("\n\n".join(parts), file=out)
    return 0
