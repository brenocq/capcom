"""capcom's totals against Claude Code's cost-state, per session, for the session page
and `capcom calibrate`: which part of the session the cost-states compare (the steps
of the processes that wrote one, "n of m processes" when some wrote none), and the
note that explains the difference: spend in a process that wrote no cost-state, the
output above capcom's estimate (a range: "≥ $X, up to $Y"), the calls Claude Code
counted that the log does not record, and the unexplained rest.

Claude Code's cost-state counts only the processes that wrote one
(capcom.cost.calibrate): a process that was killed or is still running is in
capcom's session total but not in the comparison. Both pages show the same two
numbers, the compared part and the whole session, so one session never shows two
unexplained totals.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, TextIO

from capcom.config import Config
from capcom.cost.calibrate import TARGET_SPREAD, check_session, session_steps
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
CATEGORY_WORDS = {
    "cache_read": "cache reads",
    "input": "uncached input",
    "output": "output",
    "cache_write": "cache writes",
}


def model_label(model: str) -> str:
    """'claude-opus-5-5' -> 'Opus 5.5'; another name as it is."""
    parts = model.removeprefix("claude-").split("-")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[1:]):
        return model
    return f"{parts[0].capitalize()} {'.'.join(parts[1:])}"


@dataclass(frozen=True, slots=True)
class Part:
    """The part of a session the cost-states compare: the steps of the processes
    that wrote one."""

    since: datetime | None  # the first step's start (None: unknown, the whole log)
    until: datetime | None  # the last step's end (None: open)
    first: datetime | None  # the first and last request inside a step
    last: datetime | None
    process: int | None  # the processes compared (steps), None without any
    processes: int  # those and the stretches of requests outside every step
    earlier: bool  # requests before the first step
    session_usd: float  # every request of the session
    later: bool = False  # requests after the last step: a process with no cost-state yet
    gaps: bool = False  # requests between steps: a process that wrote no cost-state

    @property
    def whole(self) -> bool:
        return not self.earlier and not self.later and not self.gaps


def part_of(index: Index, cs: CostState, table: PriceTable) -> Part:
    """Where `cs`'s steps sit in its session (one pass over its requests).

    Processes are counted from the steps, plus one per stretch of requests outside
    every step (a process that wrote no cost-state). Such a process between two
    steps that logged nothing leaves no trace, so `processes` is a lower bound."""
    st = session_steps(index, cs)
    since = st[0].since if st else None
    until = st[-1].until if st else None
    total, earlier, later, gaps, outside_run = 0.0, False, False, False, False
    runs = 0
    first = last = None
    for r in index.requests(session_id=cs.session_id):
        if is_placeholder(r.model):
            continue
        total += price(r, table).total
        if any(s.holds(r.timestamp) for s in st):
            first = first or r.timestamp
            last = r.timestamp
            outside_run = False
            continue
        if not outside_run:
            runs += 1
            outside_run = True
        if first is None:
            earlier = True
        elif until is not None and r.timestamp >= until:
            later = True
        else:
            gaps = True
    process = len(st) or None
    return Part(since, until, first, last, process, len(st) + runs, earlier, total, later, gaps)


def _when(t: datetime | None, tz: tzinfo | None) -> str:
    if t is None:
        return "?"
    local = t.astimezone(tz)
    return f"{local:%b} {local.day} {local:%H:%M}"


def _which(p: Part) -> str:
    n = p.process or 0
    if p.processes <= 1:
        return "one process"
    return f"{n} of {p.processes} processes"


def part_text(p: Part, tz: tzinfo | None = None) -> str:
    """'Oct 16 10:02 to Oct 21 14:10, 2 of 3 processes', or 'whole session'; with
    ', later requests not in it' when the session ran on after the last cost-state."""
    if p.whole:
        return "whole session"
    span = f"{_when(p.since or p.first, tz)} to {_when(p.last, tz)}"
    tail = ", later requests not in it" if p.later else ""
    return f"{span}, {_which(p)}{tail}"


def _signed(x: float) -> str:
    return ("-" if x < 0 else "+") + usd(abs(x))


def output_range(c: Calibration) -> str | None:
    """'≥ $X, up to $Y' when cost-state counted more output than capcom estimated."""
    if c.output_above_usd < SAME_USD:
        return None
    return f"≥ {usd(c.capcom_usd)}, up to {usd(c.capcom_usd + c.output_above_usd)}"


def unlogged_text(c: Calibration) -> str | None:
    """'calls Claude Code counted that the log doesn't record: $N (mostly cache
    reads on Opus 5.5)'."""
    if c.unlogged_usd < SAME_USD:
        return None
    items = [(usd_, m, cat) for m, parts in c.unlogged.items() for cat, usd_ in parts.items()]
    _, model, cat = max(items)
    return (
        f"calls Claude Code counted that the log doesn't record: {usd(c.unlogged_usd)} "
        f"(mostly {CATEGORY_WORDS.get(cat, cat)} on {model_label(model)})"
    )


def difference_note(c: Calibration, p: Part, tz: tzinfo | None = None) -> str | None:
    """Why the totals differ, in a few short sentences: the spend in processes that
    wrote no cost-state, the output range, the calls the log does not record, and
    the unexplained rest. None when there is nothing to say."""
    out = []
    outside = p.session_usd - c.capcom_usd
    if outside >= SAME_USD:
        span = f"from {_when(p.since or p.first, tz)}"
        if p.later:
            span += f" to {_when(p.last, tz)}, later requests not in it"
        out.append(
            f"Claude Code's cost-state counts only {_which(p)} ({span}): capcom counts "
            f"{usd(c.capcom_usd)} for that part, {usd(p.session_usd)} for the whole "
            f"session; the other {usd(outside)} is spend in a Claude Code process that "
            "wrote no cost-state."
        )
    rng = output_range(c)
    if rng:
        out.append(
            f"capcom's total for it is {rng}: Claude Code counted more output than "
            "capcom's estimate, and the log has no true count."
        )
    calls = unlogged_text(c)
    if calls:
        out.append(calls[0].upper() + calls[1:] + ".")
    if (rng or calls) and abs(c.rest_usd) >= SAME_USD:
        out.append(f"The rest, {_signed(c.rest_usd)}, is unexplained.")
    return " ".join(out) or None


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
        "outside_usd": round(c.outside_usd, 6),
        "output_above_usd": round(c.output_above_usd, 6),
        "output_range": output_range(c),
        "unlogged_usd": round(c.unlogged_usd, 6),
        "unlogged_text": unlogged_text(c),
        "rest_usd": round(c.rest_usd, 6),
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


def rest_summary(checks: list[Calibration]) -> str:
    """Where the difference of the sessions that are not comparable goes."""
    rest = [c for c in checks if not c.comparable]
    a = sum(c.outside_usd for c in rest)
    b = sum(c.output_above_usd for c in rest)
    cc = sum(c.unlogged_usd for c in rest)
    d = sum(c.rest_usd for c in rest)
    line = (
        f"on the rest: {usd(a)} in processes with no cost-state, {usd(b)} of output above "
        f"capcom's estimate, {usd(cc)} in calls the log doesn't show, {_signed(d)} unexplained"
    )
    more = sum(c.outside_usd for c in checks if c.comparable)
    if more >= SAME_USD:
        line += f"\n(comparable sessions: {usd(more)} more in processes with no cost-state)"
    return line


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
    notes = [f"{c.session_id[:8]}  {n}" for c, p, _ in rows if (n := difference_note(c, p, tz))]
    if notes:
        parts.append(
            "Notes (cost-state covers only the part in `compares`, and capcom's column is "
            "that part too):\n" + "\n".join(notes)
        )
    parts.append(_summary("comparable", [c for c in checks if c.comparable]))
    parts.append(_summary("all", checks))
    parts.append(rest_summary(checks))
    parts.append(_by_reason([c for c in checks if not c.comparable]))
    print("\n\n".join(parts), file=out)
    return 0
