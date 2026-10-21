"""`capcom spikes`: the spikes of the window ranked by excess, each with its causes,
confidence, excess and evidence, then the excess by cause."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TextIO

from capcom.config import Config
from capcom.explain import explain
from capcom.explain.fmt import CAUSE_LABELS, CONFIDENCE_LABELS, day_hm, usd
from capcom.types import CauseKind, Confidence, Explanation, Index, PriceTable, Spike

FindSpikes = Callable[[Index, PriceTable, datetime, datetime], list[Spike]]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _project(project_dir: str) -> str:
    from capcom.cost.top import project_label

    return project_label(project_dir)


def _open_index(config: Config) -> Index | None:
    """The store's index, read-only; None (after saying why) when it cannot be read."""
    import sqlite3

    from capcom.store.index import open_index

    try:
        return open_index(config.index_path, readonly=True)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as err:
        print(f"capcom: cannot read the index at {config.index_path} ({err})", file=sys.stderr)
        print("run `capcom scan` first", file=sys.stderr)
        return None


def format_spikes(
    explanations: Sequence[Explanation],
    limit: int,
    projects: dict[str, str],
    titles: dict[str, str],
) -> str:
    by_spike: dict[Spike, list[Explanation]] = {}
    for e in explanations:
        by_spike.setdefault(e.spike, []).append(e)
    spikes = sorted(by_spike, key=lambda s: (-s.excess_usd, s.start))
    total = sum(s.excess_usd for s in spikes)
    matched = sum(
        1 for s in spikes if any(e.cause is not CauseKind.UNEXPLAINED for e in by_spike[s])
    )
    lines = [
        f"{len(spikes)} spikes; {matched} matched to a cause in the logs; together "
        f"{usd(total)} above the usual pace.",
        "",
    ]
    for n, s in enumerate(spikes[:limit], 1):
        exps = by_spike[s]
        where = "all sessions"
        if s.session_id is not None:
            where = titles.get(s.session_id) or _project(projects.get(s.session_id, s.session_id))
            where = where if len(where) <= 48 else where[:47] + "…"
        if s.scope == "session":
            where += " (session-wide)"
        lines.append(f"{n:>3}. +{usd(s.excess_usd)}  {day_hm(s.start)}  {where}")
        lines.append(f"     {exps[0].title}")
        for e in exps:
            conf = CONFIDENCE_LABELS[e.confidence]
            label = (
                CAUSE_LABELS[e.cause]
                if e.confidence is not Confidence.UNEXPLAINED
                else ("Unexplained")
            )
            lines.append(f"     {label}, {conf}: {usd(e.excess_usd)}")
            if e is not exps[0]:
                lines.append(f"       {e.title}")
            if e.summary:
                lines.append(f"       {e.summary}")
            lines.extend(f"       - {ev.label}" for ev in e.evidence)
            if e.advice:
                lines.append(f"       What to change: {e.advice}")
        lines.append("")
    if len(spikes) > limit:
        lines.append(f"({len(spikes) - limit} more spikes; raise --limit to see them)")
        lines.append("")
    by_cause: dict[str, float] = {}
    for e in explanations:
        key = "Unexplained" if e.cause is CauseKind.UNEXPLAINED else CAUSE_LABELS[e.cause]
        by_cause[key] = by_cause.get(key, 0.0) + e.excess_usd
    lines.append("Excess by cause")
    width = max((len(k) for k in by_cause), default=0)
    for key, value in sorted(by_cause.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {key.ljust(width)}  {usd(value):>12}")
    return "\n".join(lines)


def run_spikes(
    config: Config,
    days: int,
    limit: int,
    index: Index | None = None,
    out: TextIO | None = None,
    now: datetime | None = None,
    find_spikes: FindSpikes | None = None,
    table: PriceTable | None = None,
) -> int:
    from capcom.cost.prices import load_prices
    from capcom.cost.top import window_start

    out = out or sys.stdout
    index = index or _open_index(config)
    if index is None:
        return 1
    table = table or load_prices()
    if find_spikes is None:
        from capcom import timeline

        find_spikes = timeline.find_spikes
    until = now or datetime.now(UTC)
    since = window_start(days, until) or EPOCH
    try:
        spikes = find_spikes(index, table, since, until)
    except NotImplementedError:
        print("capcom spikes: spike detection (capcom.timeline) is not built yet", file=sys.stderr)
        return 1
    window = "all time" if days <= 0 else f"last {days} days, since {since.astimezone():%Y-%m-%d}"
    print(f"capcom spikes, {window} (prices checked {table.checked})\n", file=out)
    if not spikes:
        print("no spikes in this window", file=out)
        return 0
    explanations = explain(index, table, spikes)
    projects: dict[str, str] = {}
    titles: dict[str, str] = {}
    for info in index.sessions():
        projects[info.session_id] = info.project_dir
        if info.title:
            titles[info.session_id] = info.title
    print(format_spikes(explanations, limit, projects, titles), file=out)
    return 0
