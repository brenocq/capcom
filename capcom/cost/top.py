"""Terminal views of the totals: `capcom top` and `capcom calibrate`."""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TextIO

from capcom.config import Config
from capcom.cost.calibrate import TARGET_SPREAD, calibrate
from capcom.cost.model import price
from capcom.cost.prices import ZERO_MODEL, is_placeholder, load_prices, normalize
from capcom.timeline.groups import ProjectNames, is_temporary, temporary_project
from capcom.types import Calibration, Index, PriceTable, Request

GROUPINGS = ("project", "session", "agent", "model", "day")
TEMPORARY = "(temporary)"  # project folders under /tmp, grouped as one
MAIN_THREAD = "main"


@dataclass(slots=True)
class Row:
    """Totals of one group in `capcom top`."""

    key: str
    label: str
    requests: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    usd_input: float = 0.0
    usd_cache_read: float = 0.0
    usd_cache_write: float = 0.0
    usd_output: float = 0.0  # output and web tools
    estimated_output: int = 0
    inexact_price: int = 0  # priced at a related model's rates
    unpriced: int = 0  # no price at all: counted at $0
    unpriced_models: set[str] = field(default_factory=set)
    path: str = ""  # a project's full folder, printed beside its name

    @property
    def total(self) -> float:
        return self.usd_input + self.usd_cache_read + self.usd_cache_write + self.usd_output

    @property
    def flags(self) -> str:
        return (
            ("~" if self.estimated_output else "")
            + ("?" if self.inexact_price else "")
            + ("!" if self.unpriced else "")
        )


def project_label(project_dir: str) -> str:
    """The folder name; a temporary folder (groups.is_temporary) is "(temporary)",
    or "temporary (<project folder>)" when its name holds the project it was made for."""
    if not is_temporary(project_dir):
        return project_dir
    inner = temporary_project(project_dir)
    return f"temporary ({inner})" if inner else TEMPORARY


def group_key(
    req: Request,
    by: str,
    titles: Mapping[str, str],
    agent_types: Mapping[str, str],
    projects: ProjectNames | None = None,
) -> tuple[str, str]:
    """(key, label) of the group `req` belongs to; projects are named by `projects`
    (capcom.timeline.groups.ProjectNames, as on the website) when given."""
    if by == "project":
        if projects is not None:
            key = projects.key(req.project_dir)
            return key, projects.label(key)
        label = project_label(req.project_dir)
        return label, label
    if by == "session":
        return req.session_id, titles.get(req.session_id) or req.session_id[:8]
    if by == "agent":
        if req.agent_id is None:
            return MAIN_THREAD, MAIN_THREAD
        kind = agent_types.get(req.agent_id) or "(unknown agent)"
        return kind, kind
    if by == "model":
        model = normalize(req.model)
        return model, model
    if by == "day":
        day = req.timestamp.astimezone().date().isoformat()  # local time
        return day, day
    raise ValueError(f"unknown grouping {by!r}; one of {', '.join(GROUPINGS)}")


def aggregate(
    requests: Iterable[Request],
    by: str,
    table: PriceTable,
    titles: Mapping[str, str] | None = None,
    agent_types: Mapping[str, str] | None = None,
    projects: ProjectNames | None = None,
) -> list[Row]:
    """Rows by `by`, largest total first (days: newest first)."""
    titles = titles or {}
    agent_types = agent_types or {}
    rows: dict[str, Row] = {}
    for req in requests:
        if is_placeholder(req.model):
            continue  # not a model call (same rule as calibrate)
        key, label = group_key(req, by, titles, agent_types, projects)
        row = rows.get(key)
        if row is None:
            row = rows[key] = Row(key, label)
            if projects is not None and by == "project":
                row.path = projects.path(key)
        cost = price(req, table)
        u = req.usage
        row.requests += 1
        row.input += u.input
        row.cache_read += u.cache_read
        row.cache_write += u.cache_write
        row.output += u.output
        row.usd_input += cost.input
        row.usd_cache_read += cost.cache_read
        row.usd_cache_write += cost.cache_write
        row.usd_output += cost.output + cost.web
        row.estimated_output += req.output_estimated
        if cost.priced_as == ZERO_MODEL:
            row.unpriced += 1
            row.unpriced_models.add(normalize(req.model) or "(no model name)")
        elif not cost.exact_price:
            row.inexact_price += 1
    if by == "day":
        return sorted(rows.values(), key=lambda r: r.key, reverse=True)
    return sorted(rows.values(), key=lambda r: (-r.total, r.key))


def tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    return str(n)


def usd(x: float) -> str:
    return f"${x:,.2f}"


def _table(header: list[str], body: list[list[str]], left: int = 1, last_left: bool = False) -> str:
    """Plain-text columns: the first `left` columns (and the last one if `last_left`)
    left-aligned, the rest right-aligned."""
    widths = [max(len(r[i]) for r in [header, *body]) for i in range(len(header))]

    def line(cells: list[str]) -> str:
        out = [
            c.ljust(w) if i < left or (last_left and i == len(cells) - 1) else c.rjust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ]
        return "  ".join(out).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line(header), rule, *(line(r) for r in body)])


def _shorten(text: str, width: int = 48) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def format_top(rows: list[Row], by: str, limit: int) -> str:
    shown = rows[:limit] if limit > 0 else rows
    header = [by, "req", "input", "read", "write", "output"]
    header += ["$input", "$read", "$write", "$output", "total", ""]
    folders = by == "project" and any(r.path for r in shown)
    if folders:
        header.append("folder")
    body = []
    for r in shown:
        body.append(
            [
                _shorten(r.label),
                str(r.requests),
                tokens(r.input),
                tokens(r.cache_read),
                tokens(r.cache_write),
                tokens(r.output),
                usd(r.usd_input),
                usd(r.usd_cache_read),
                usd(r.usd_cache_write),
                usd(r.usd_output),
                usd(r.total),
                r.flags,
                *([r.path] if folders else []),
            ]
        )
    total = Row("", f"all ({len(rows)} {by}s)" if len(rows) != 1 else "all")
    for r in rows:
        for name in (
            "requests",
            "input",
            "cache_read",
            "cache_write",
            "output",
            "usd_input",
            "usd_cache_read",
            "usd_cache_write",
            "usd_output",
            "estimated_output",
            "inexact_price",
            "unpriced",
        ):
            setattr(total, name, getattr(total, name) + getattr(r, name))
        total.unpriced_models |= r.unpriced_models
    body.append(
        [
            total.label,
            str(total.requests),
            tokens(total.input),
            tokens(total.cache_read),
            tokens(total.cache_write),
            tokens(total.output),
            usd(total.usd_input),
            usd(total.usd_cache_read),
            usd(total.usd_cache_write),
            usd(total.usd_output),
            usd(total.total),
            total.flags,
            *([""] if folders else []),
        ]
    )
    text = _table(header, body, last_left=folders)
    notes = ["API-equivalent cost at the list prices; not the bill."]
    if total.estimated_output:
        share = total.estimated_output / total.requests if total.requests else 0.0
        notes.append(
            f"~ {share:.0%} of requests ({total.estimated_output}) have output estimated "
            "from content length; output totals are lower bounds (the true output can be "
            "higher than capcom's estimate)"
        )
    if total.inexact_price:
        notes.append(
            f"? {total.inexact_price} requests of models the price table lacks, priced at "
            "the closest version of the same family (or without a fast-mode rate)"
        )
    if total.unpriced:
        names = ", ".join(sorted(total.unpriced_models))
        notes.append(f"! {total.unpriced} requests of models with no price, counted at $0: {names}")
    if limit > 0 and len(rows) > limit:
        notes.append(f"{len(rows) - limit} more rows (--limit)")
    return text + "\n\n" + "\n".join(notes)


def _open_index(config: Config) -> Index | None:
    """The store's index, read-only; None (after saying why) when it cannot be read."""
    from capcom.store.index import open_index

    try:
        return open_index(config.index_path, readonly=True)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as err:
        print(f"capcom: cannot read the index at {config.index_path} ({err})", file=sys.stderr)
        print("run `capcom scan` first", file=sys.stderr)
        return None


def window_start(days: int, now: datetime | None = None) -> datetime | None:
    """Local midnight `days - 1` days before today (days <= 0: no window), so the
    window holds `days` whole local days, today included."""
    if days <= 0:
        return None
    local_now = (now or datetime.now(UTC)).astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(days=days - 1)).astimezone(UTC)


def _titles(index: Index) -> dict[str, str]:
    return {s.session_id: s.title for s in index.sessions() if s.title}


def run_top(
    config: Config,
    by: str,
    days: int,
    limit: int,
    index: Index | None = None,
    out: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    out = out or sys.stdout
    if by not in GROUPINGS:
        print(f"capcom top: --by must be one of {', '.join(GROUPINGS)}", file=sys.stderr)
        return 2
    index = index or _open_index(config)
    if index is None:
        return 1
    table = load_prices()
    since = window_start(days, now)
    titles = _titles(index) if by == "session" else {}
    agent_types: dict[str, str] = {}
    if by == "agent":
        agent_types = {a.agent_id: a.agent_type for a in index.agents() if a.agent_type}
    projects = None
    if by == "project":  # named as on the website
        projects = ProjectNames.build(index.sessions(), config.openclaw_root)
    rows = aggregate(index.requests(since=since), by, table, titles, agent_types, projects)
    window = "all time"
    if since is not None:
        window = f"last {days} days, local time, since {since.astimezone():%Y-%m-%d %H:%M}"
    print(f"capcom top, by {by}, {window} (prices checked {table.checked})\n", file=out)
    if not rows:
        print("no requests in this window", file=out)
        return 0
    print(format_top(rows, by, limit), file=out)
    return 0


def _spread(capcom_usd: float, cc_usd: float) -> str:
    if cc_usd == 0:
        return "-" if capcom_usd == 0 else "new"
    return f"{(capcom_usd - cc_usd) / cc_usd:+.1%}"


def format_calibration(checks: list[Calibration], titles: Mapping[str, str], limit: int) -> str:
    ordered = sorted(checks, key=lambda c: -c.claude_code_usd)
    shown = ordered[:limit] if limit > 0 else ordered
    header = ["session", "cost-state", "capcom", "spread", "scope"]
    body = []
    for c in shown:
        cal = c
        label = titles.get(cal.session_id) or cal.session_id[:8]
        scope = "comparable" if c.comparable else ", ".join(c.reasons)
        body.append(
            [
                _shorten(label, 40),
                usd(cal.claude_code_usd),
                usd(cal.capcom_usd),
                _spread(cal.capcom_usd, cal.claude_code_usd),
                scope,
            ]
        )
    parts = [_table(header, body, left=1, last_left=True)]
    if limit > 0 and len(ordered) > limit:
        parts.append(f"{len(ordered) - limit} more sessions (--limit)")

    def summary(name: str, group: list[Calibration]) -> str:
        if not group:
            return f"{name}: none"
        cc = sum(c.claude_code_usd for c in group)
        mine = sum(c.capcom_usd for c in group)
        within = sum(1 for c in group if abs(c.spread) <= TARGET_SPREAD)
        return (
            f"{name}: {len(group)} sessions, {within} within {TARGET_SPREAD:.0%}, "
            f"cost-state {usd(cc)}, capcom {usd(mine)} ({_spread(mine, cc)})"
        )

    comparable = [c for c in checks if c.comparable]
    parts.append(summary("comparable", comparable))
    parts.append(summary("all", checks))
    per_model: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for c in comparable:
        for model, (mine, cc) in c.per_model.items():
            per_model[model][0] += mine
            per_model[model][1] += cc
    if per_model:
        rows = [
            [m, usd(cc), usd(mine), _spread(mine, cc)]
            for m, (mine, cc) in sorted(per_model.items(), key=lambda kv: -kv[1][1])
        ]
        parts.append(
            "comparable sessions by model:\n"
            + _table(["model", "cost-state", "capcom", "spread"], rows)
        )
    parts.append(_by_reason([c for c in checks if not c.comparable]))
    parts.append(
        "Not comparable: windows with calls the transcript lacks (away summaries, compactions,\n"
        "WebFetch / WebSearch side calls, or cost-state's uncached input far above the log's),\n"
        "replies another session's log also holds (a forked or copied session), estimated\n"
        "output, a model only cost-state knows, or under $0.10. cost-state counts from its\n"
        "process start (startTime) to the first record after it."
    )
    return "\n\n".join(parts)


def _by_reason(checks: list[Calibration]) -> str:
    """The non-comparable sessions and their dollars per reason, and the part where
    that reason is the only one."""
    if not checks:
        return "not comparable: none"
    per: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0])
    for c in checks:
        for reason in c.reasons:
            row = per[reason]
            row[0] += 1
            row[1] += c.claude_code_usd
            row[2] += c.capcom_usd
            if len(c.reasons) == 1:
                row[3] += 1
                row[4] += c.claude_code_usd
    rows = [
        [reason, str(int(n)), usd(cc), usd(mine), _spread(mine, cc), str(int(n1)), usd(cc1)]
        for reason, (n, cc, mine, n1, cc1) in sorted(per.items(), key=lambda kv: (-kv[1][1], kv[0]))
    ]
    cc = sum(c.claude_code_usd for c in checks)
    head = (
        f"not comparable: {len(checks)} sessions, cost-state {usd(cc)}. By reason: a session "
        "with several reasons counts under each\n(the rows add up to more); the last two "
        "columns are the sessions with that reason alone."
    )
    header = ["reason", "sessions", "cost-state", "capcom", "spread", "alone", "cost-state"]
    return head + "\n" + _table(header, rows)


def run_calibrate(
    config: Config,
    limit: int,
    index: Index | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    index = index or _open_index(config)
    if index is None:
        return 1
    checks = calibrate(index, load_prices())
    if not checks:
        print("no session has a cost-state record", file=out)
        return 0
    print(format_calibration(checks, _titles(index), limit), file=out)
    return 0
