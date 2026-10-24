"""The website's JSON API (documented in API.md). Each endpoint is a function of a
`Context` (an open index, the prices, the clock) and the query; the server only
routes, encodes and handles errors.

Privacy: the payloads carry titles, project folders, paths, model, tool and agent
names, counts, times and dollars, and the session detail labels each subagent with
its spawn description (the few words the Agent call names the task with); never a
prompt or a reply.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from capcom.config import load_config
from capcom.cost.model import price
from capcom.cost.prices import canonical, is_placeholder
from capcom.cost.top import MAIN_THREAD, project_label
from capcom.report.analysis import Analysis, Unavailable
from capcom.report.calibration import calibration_json
from capcom.report.story import latest_change, regime_json
from capcom.report.tally import (
    MIN_LISTED_USD_PER_WEEK,
    PAUSE_SECONDS,
    Tally,
    Window,
    counted_findings,
    finding_overlaps,
    iso,
    ms,
    overlap_blockers,
    project_display,
    project_names,
    savings_total,
)
from capcom.timeline.groups import (
    OPENCLAW,
    OPENCLAW_ALL,
    TEMPORARY,
    TEMPORARY_LABEL,
    ProjectNames,
    folder_name,
)
from capcom.types import (
    AgentInfo,
    CauseKind,
    EventKind,
    Evidence,
    Explanation,
    Finding,
    GroupBy,
    Index,
    PriceTable,
    Request,
    Series,
    SessionInfo,
    Spike,
)

if TYPE_CHECKING:
    from capcom.limits.model import Split

LIVE_WINDOW = timedelta(minutes=10)
TOP_MEMBERS = 6  # series beyond these are folded into "other"
SPEND_DAYS = (7, 14, 30, 90)
OTHER = "(other)"
MAX_MARKERS = 12  # spike markers on a cumulative chart: the largest by excess
MAX_POINTS = 4000  # per-request columns of a session above this are thinned
MAX_SESSION_SPIKES = 8  # spikes listed on a session's page: the largest by excess

# Cumulative-chart step per window length, so a chart has a few hundred points.
_STEPS = {1: timedelta(minutes=5), 7: timedelta(minutes=30), 14: timedelta(hours=1)}
_STEPS |= {30: timedelta(hours=2), 90: timedelta(hours=6)}

LIMITS_DAYS = (7, 14, 30, 90)

# /api/spend?group=: other names accepted for a grouping (as `capcom top --by agent`
# and the session rows' `agent_type` say it)
GROUP_ALIASES = {"agent_type": GroupBy.AGENT.value}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(slots=True)
class Context:
    index: Index
    table: PriceTable
    analysis: Analysis
    now: datetime
    tz: tzinfo | None = None  # None: the machine's local time zone
    index_path: Path | None = None
    openclaw_root: Path | None = None  # None: from the configuration
    state_dir: Path | None = None  # where the usage samples are; None: no samples
    scanning: bool = False  # serve's first scan is still running: totals may be partial
    changes_path: Path | None = None  # the marks (changes.jsonl); None: capcom.changes' own
    day_cache: Path | None = None  # capcom.changes' finished days; None: not kept
    agent_dirs: Sequence[Path] | None = None  # noticed()'s definitions; None: its own
    watching: bool = True  # False: this server never scans (serve --no-scan), so "live" is stale
    # serve's job runner (capcom.report.jobs): `step(progress, partial)` after each
    # chunk of work, with a function that builds the reply so far; it may raise
    # jobs.Cancelled, or wait while a request of higher priority runs
    step: Callable[[float, Callable[[], Any] | None], None] | None = None

    def progress(self, fraction: float, partial: Callable[[], Any] | None = None) -> None:
        """Say how far the reply is (0..1) and, optionally, how to build it so far."""
        if self.step is not None:
            self.step(fraction, partial)

    def local(self, t: datetime) -> datetime:
        return t.astimezone(self.tz)

    def today(self) -> date:
        return self.local(self.now).date()

    def at_midnight(self, day: date) -> datetime:
        """The aware local midnight that starts `day` (its own UTC offset, so a
        daylight-saving change between it and today does not shift it)."""
        if self.tz is None:
            return datetime.combine(day, time()).astimezone()  # naive: the machine's zone
        return datetime.combine(day, time(), tzinfo=self.tz)

    def midnight(self, days_back: int = 0) -> datetime:
        """Local midnight of today's local date, `days_back` calendar days earlier."""
        return self.at_midnight(self.today() - timedelta(days=days_back))

    @property
    def openclaw_dir(self) -> str:
        root = self.openclaw_root or load_config().openclaw_root
        return folder_name(root)

    def names(self, infos: Mapping[str, SessionInfo] | None = None) -> ProjectNames:
        """The project display rule (capcom.timeline.groups.ProjectNames), from the
        cwds of every session (a temporary folder names its project)."""
        sessions = (infos or self.infos()).values()
        return project_names(sessions, self.openclaw_root or load_config().openclaw_root)

    def infos(self) -> dict[str, SessionInfo]:
        return {s.session_id: s for s in self.index.sessions()}

    def agents(self) -> dict[str, AgentInfo]:
        return {a.agent_id: a for a in self.index.agents()}


def _int(query: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, "bad_query", f"{name} must be an integer") from None
    if not lo <= value <= hi:
        raise ApiError(400, "bad_query", f"{name} must be between {lo} and {hi}")
    return value


def _window(ctx: Context, names: ProjectNames | None = None) -> Window:
    infos = ctx.infos()
    return Window(ctx.table, infos, ctx.agents(), names or ctx.names(infos))


# --------------------------------------------------------------------------- series


def _fold(series: Sequence[Series], keep: Sequence[str]) -> list[tuple[str, str, list[float]]]:
    """Series reduced to the members in `keep` (in that order) plus one "other" that
    sums the rest; every series has the same length."""
    by_key = {s.key: s for s in series}
    out = [(k, by_key[k].label, list(by_key[k].values)) for k in keep if k in by_key]
    rest = [s for s in series if s.key not in keep]
    if rest:
        n = len(rest[0].values)
        other = [sum(s.values[i] for s in rest) for i in range(n)]
        out.append((OTHER, OTHER, other))
    return out


def _tidy(series: list[Series], by: GroupBy, names: ProjectNames) -> list[Series]:
    """Series named like the rest of the site: web tools counted with output (as in
    every total), projects by their display name (OpenClaw's folders as one series,
    labeled so). Largest final value first, with
    "other" last."""
    if by is GroupBy.CATEGORY:
        web = next((s for s in series if s.key == "web"), None)
        series = [s for s in series if s.key != "web"]
        if web is not None:
            out = next((s for s in series if s.key == "output"), None)
            if out is None:
                series.append(Series("output", "Output", web.times, web.values))
            else:
                merged = tuple(a + b for a, b in zip(out.values, web.values, strict=True))
                series = [
                    Series(s.key, s.label, s.times, merged) if s is out else s for s in series
                ]
            series.sort(key=lambda s: (s.key == OTHER, -sum(s.values) if s.values else 0.0))
    elif by is GroupBy.PROJECT:
        labels = {TEMPORARY: "temporary folders", OPENCLAW: OPENCLAW_ALL, OTHER: OTHER}
        series = [
            Series(s.key, labels.get(s.key) or names.label(s.key), s.times, s.values)
            for s in series
        ]
    return series


def _cumulative_json(series: Sequence[Series], step: timedelta, until: datetime) -> dict[str, Any]:
    """Members (largest first, "other" last) and the total. Point i is the cumulative
    value at the end of step i, clamped to `until`."""
    members = [(s.key, s.label, s.values) for s in series]
    times: list[int] = []
    if series:
        times = [ms(min(t + step, until)) for t in series[0].times]
    n = len(times)
    total = [round(sum(m[2][i] for m in members), 6) for i in range(n)]
    return {
        "step_s": int(step.total_seconds()),
        "times": times,
        "members": [
            {"key": k, "label": label, "final": round(v[-1], 6) if v else 0.0}
            for k, label, v in members
        ],
        "values": [[round(x, 6) for x in v] for _, _, v in members],
        "total": total,
    }


def _spike_marks(
    ctx: Context,
    since: datetime,
    until: datetime,
    infos: Mapping[str, SessionInfo],
    names: ProjectNames,
    unavailable: list[str],
) -> dict[str, Any] | None:
    """The window's spikes (capcom.timeline.find_spikes) as chart markers: the
    MAX_MARKERS largest by excess, numbered in time order."""
    found: list[Spike] | None = _try(
        lambda: ctx.analysis.find_spikes(ctx.index, ctx.table, since, until), unavailable
    )
    if found is None:
        return None
    ctx.progress(0.6)
    marks = sorted(found, key=lambda sp: -sp.excess_usd)[:MAX_MARKERS]
    marks.sort(key=lambda sp: (sp.start, sp.session_id or ""))
    explained = _try(lambda: ctx.analysis.explain(ctx.index, ctx.table, marks), unavailable)
    ctx.progress(0.75)
    causes = _by_spike(explained or [])
    return {
        "count": len(found),
        "excess_usd": round(sum(sp.excess_usd for sp in found), 6),
        "explained": explained is not None,
        "marks": [
            {
                "n": i + 1,
                "key": spike_key(sp),
                "t": iso(sp.start, ctx.tz),
                "end": iso(sp.end, ctx.tz),
                "kind": sp.kind.value,
                "scope": sp.scope,
                "session_id": sp.session_id,
                "title": (info.title if (info := infos.get(sp.session_id or "")) else None),
                "color": info.color if info else None,
                "agent_id": sp.agent_id,
                "requests": len(sp.message_ids),
                "cost_usd": round(sp.cost_usd, 6),
                "excess_usd": round(sp.excess_usd, 6),
            }
            | _spike_project(sp, infos, names)
            | _headline(causes.get(sp, ()), sp.excess_usd)
            for i, sp in enumerate(marks)
        ],
    }


STORY_DAYS = 14  # Overview's "what changed": a change in the last two weeks


def _regime(ctx: Context, first_day: date, unavailable: list[str]) -> dict[str, Any] | None:
    """The latest change of the daily spend's level on or after `first_day`
    (capcom.report.story), None without one."""
    ctx.progress(0.8)
    change = _try(
        lambda: latest_change(
            ctx.index,
            ctx.table,
            first_day,
            ctx.now,
            ctx.tz,
            ctx.openclaw_root,
            fn=ctx.analysis.regimes(),
        ),
        unavailable,
    )
    return regime_json(change)


def _try(fn: Any, unavailable: list[str]) -> Any:
    try:
        return fn()
    except Unavailable as err:
        if err.module not in unavailable:
            unavailable.append(err.module)
        return None


# --------------------------------------------------------------------------- endpoints


def status(ctx: Context) -> dict[str, Any]:
    sessions = sum(1 for _ in ctx.index.sessions())
    out: dict[str, Any] = {"now": iso(ctx.now, ctx.tz), "sessions": sessions}
    out["scanning"] = ctx.scanning
    out["prices_checked"] = ctx.table.checked.isoformat()
    from capcom.report.didit import count

    out["changes"] = count(ctx)  # marked changes, the sidebar's badge
    if ctx.index_path is not None:
        st = ctx.index_path.stat()
        mtime = st.st_mtime
        wal = ctx.index_path.with_name(ctx.index_path.name + "-wal")
        with contextlib.suppress(OSError):  # writes land in the WAL until a checkpoint
            mtime = max(mtime, wal.stat().st_mtime)
        out["index"] = {
            "updated": iso(datetime.fromtimestamp(mtime).astimezone(), ctx.tz),
            "size_bytes": st.st_size,
        }
    return out


def colors(ctx: Context) -> dict[str, Any]:
    """One color per project, agent type and model, the same on every page
    (capcom.report.colors): assigned over every entity in the index."""
    from capcom.checks import default_agent_dirs
    from capcom.report.colors import entity_colors

    infos = ctx.infos()
    names = ctx.names(infos)
    dirs = ctx.agent_dirs
    if dirs is None:
        dirs = default_agent_dirs(sorted(set(names.paths.values())))
    return entity_colors(ctx.index, ctx.table, infos, names, dirs)


# today's same-time comparison waits for a baseline of at least $1, or an hour of today
SAME_HOUR_MIN_USD = 1.0


def _group(query: Mapping[str, str]) -> GroupBy:
    """The `group` of a query (default: by category); a 400 for an unknown one."""
    raw = query.get("group") or GroupBy.CATEGORY.value
    try:
        return GroupBy(GROUP_ALIASES.get(raw, raw))
    except ValueError:
        names = ", ".join([*GroupBy, *GROUP_ALIASES])
        raise ApiError(400, "bad_query", "group must be one of: " + names) from None


def overview(ctx: Context, query: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The Overview page; `group` splits today's cumulative chart (default: by category)."""
    group = _group(query or {})
    today_since = ctx.midnight()
    week_since = ctx.midnight(6)
    prev_since = ctx.midnight(7)
    hour_ago = ctx.now - timedelta(hours=1)
    infos = ctx.infos()
    names = ctx.names(infos)
    week, today = _window(ctx, names), _window(ctx, names)
    prev_usd = 0.0
    prev_same_hour = 0.0  # the previous 7 days, each up to now's local time of day
    clock = ctx.local(ctx.now).time()
    last_hour = Tally()
    for req in ctx.index.requests(since=prev_since):
        if req.timestamp < week_since:
            if not is_placeholder(req.model):
                usd = price(req, ctx.table).total
                prev_usd += usd
                if ctx.local(req.timestamp).time() < clock:
                    prev_same_hour += usd
            continue
        cost = week.add(req)
        if cost is None:
            continue
        if req.timestamp < today_since:
            prev_usd += cost.total
            if ctx.local(req.timestamp).time() < clock:
                prev_same_hour += cost.total
            continue
        today.add(req)
        if req.timestamp >= hour_ago:
            last_hour.add(req, cost)
    ctx.progress(0.2)

    unavailable: list[str] = []
    step = _STEPS[1]
    cum_json = _today_chart(ctx, group, names, unavailable)
    ctx.progress(0.3)
    marks = _spike_marks(ctx, today_since, ctx.now, infos, names, unavailable)
    spark_since = max(today_since, ctx.now - timedelta(hours=2))
    spark = _try(
        lambda: ctx.analysis.per_step(
            ctx.index, ctx.table, spark_since, ctx.now, GroupBy.CATEGORY, step
        ),
        unavailable,
    )
    spark_json = None
    if spark is not None:
        n = len(spark[0].values) if spark else 0
        spark_json = {
            "step_s": int(step.total_seconds()),
            "times": [ms(t) for t in spark[0].times] if spark else [],
            "values": [round(sum(s.values[i] for s in spark), 6) for i in range(n)],
        }

    def block(w: Window, since: datetime) -> dict[str, Any]:
        few = sum(1 for calls in w.agent_calls.values() if calls <= 2)
        return {
            "since": iso(since, ctx.tz),
            "totals": w.total.to_json(),
            "sessions": len(w.sessions),
            "by_project": w.groups_json(w.by_project),
            "by_agent": w.groups_json(w.by_agent),
            "by_model": w.groups_json(w.by_model),
            "subagents": {
                "count": len(w.subagents.members),
                "usd": round(w.subagents.tally.usd, 6),
                "two_tool_calls_or_fewer": few,
            },
        }

    # averages count only the days with history: from the first request's local
    # midnight, at most the 7 previous days and 6 full days of the week; a full day
    # counts as one whatever its length (daylight-saving changes)
    reqs = iter(ctx.index.requests())
    first = next(reqs, None)
    getattr(reqs, "close", lambda: None)()  # release the cursor
    first_day = ctx.local(first.timestamp).date() if first is not None else ctx.today()
    history = max(min((ctx.today() - first_day).days, 7), 0)  # previous days with data
    full_days = min(history, 6)
    today_s = max((ctx.now - today_since).total_seconds(), 0.0)
    elapsed = max(full_days * 86400 + today_s, 1.0)
    same_hour_avg = prev_same_hour / history if history else 0.0
    comparable = history > 0 and (same_hour_avg >= SAME_HOUR_MIN_USD or today_s >= 3600)
    running = sum(
        1
        for s in infos.values()
        if s.last_seen is not None and s.last_seen >= ctx.now - LIVE_WINDOW
    )
    return {
        "now": iso(ctx.now, ctx.tz),
        "running_sessions": running,
        "today": block(today, today_since),
        "week": block(week, week_since)
        | {
            "days": full_days + 1,
            "full_days": full_days,
            "elapsed_days": round(elapsed / 86400, 4),
        },
        "previous_7_days": {
            "since": iso(prev_since, ctx.tz),
            "days": history,
            "daily_average_usd": prev_usd / history if history else 0.0,
            "same_hour_average_usd": round(same_hour_avg, 6),
            "same_hour": clock.strftime("%H:%M"),
            "comparable": comparable,
        },
        "spending_now": {"usd_last_hour": round(last_hour.usd, 6), "spark": spark_json},
        "cumulative_today": cum_json,
        "group": group.value,
        "spikes_today": marks,
        "regime": _regime(ctx, ctx.today() - timedelta(days=STORY_DAYS - 1), unavailable),
        "limits": _limits(ctx, None, full=False),
        "unavailable": unavailable,
    }


def _today_chart(
    ctx: Context, group: GroupBy, names: ProjectNames, unavailable: list[str]
) -> dict[str, Any] | None:
    """Overview's cumulative chart of today, split by `group`: the one part of its
    reply that depends on the group."""
    step = _STEPS[1]
    cum = _try(
        lambda: ctx.analysis.cumulative(
            ctx.index, ctx.table, ctx.midnight(), ctx.now, group, step, top=TOP_MEMBERS
        ),
        unavailable,
    )
    return None if cum is None else _cumulative_json(_tidy(cum, group, names), step, ctx.now)


def overview_regroup(
    ctx: Context, query: Mapping[str, str], base: Mapping[str, Any]
) -> dict[str, Any]:
    """Overview's reply for the `group` of `query`, made from `base`, its reply for
    another group at the same state of the index: only today's chart is computed
    again (a few percent of the whole reply), up to the base's own `now`, so the
    chart and the totals still describe the same moment."""
    group = _group(query)
    ctx = replace(ctx, now=datetime.fromisoformat(base["now"]))
    unavailable = list(base.get("unavailable") or [])
    chart = _today_chart(ctx, group, ctx.names(), unavailable)
    return {**base, "cumulative_today": chart, "group": group.value, "unavailable": unavailable}


def spend(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    if days not in SPEND_DAYS:
        raise ApiError(400, "bad_query", f"days must be one of {', '.join(map(str, SPEND_DAYS))}")
    group = _group(query)
    since = ctx.midnight(days - 1)
    w = _window(ctx)
    hour_projects: dict[int, dict[str, float]] = {}  # local hour start (ms) -> project -> usd
    for req in ctx.index.requests(since=since):
        cost = w.add(req)
        if cost is not None and cost.total:
            per = hour_projects.setdefault(_hour_ms(ctx, req.timestamp), {})
            key = w.names.key(req.project_dir)
            per[key] = per.get(key, 0.0) + cost.total
    ctx.progress(0.15)

    unavailable: list[str] = []
    step = _STEPS[days]
    cum = _try(
        lambda: ctx.analysis.cumulative(
            ctx.index, ctx.table, since, ctx.now, group, step, top=TOP_MEMBERS
        ),
        unavailable,
    )
    ctx.progress(0.3)
    hourly = _try(
        lambda: ctx.analysis.per_step(
            ctx.index, ctx.table, since, ctx.now, group, timedelta(hours=1), top=TOP_MEMBERS
        ),
        unavailable,
    )
    ctx.progress(0.45)
    if cum is not None:
        cum = _tidy(cum, group, w.names)
    if hourly is not None:
        hourly = _tidy(hourly, group, w.names)
    cum_json = None if cum is None else _cumulative_json(cum, step, ctx.now)
    daily_json = heat_json = None
    if hourly is not None:
        keep = [m["key"] for m in cum_json["members"] if m["key"] != OTHER] if cum_json else []
        if not keep:
            keep = [s.key for s in hourly if s.key != OTHER]
        elif {s.key for s in hourly if s.key != OTHER} != set(keep):
            # a near tie ranked the members differently: fold the full series instead
            full = _try(
                lambda: ctx.analysis.per_step(
                    ctx.index, ctx.table, since, ctx.now, group, timedelta(hours=1)
                ),
                unavailable,
            )
            hourly = _tidy(full or [], group, w.names)
        daily_json, heat_json = _daily_and_heat(ctx, hourly, keep, days)
        heat_json |= _heat_hours(ctx, hourly, hour_projects, w.names)
    marks = _spike_marks(ctx, since, ctx.now, w.infos, w.names, unavailable)

    rows = w.session_rows(ctx.tz)
    temporary = [r for r in rows if r["temporary"]]
    return {
        "window": {
            "days": days,
            "since": iso(since, ctx.tz),
            "until": iso(ctx.now, ctx.tz),
            "group": group.value,
        },
        "totals": w.total.to_json(),
        "sessions_count": len(rows) - len(temporary),
        "temporary_sessions": len(temporary),
        "cumulative": cum_json,
        "spikes": marks,
        "regime": _regime(ctx, ctx.local(since).date(), unavailable),
        "daily": daily_json,
        "heatmap": heat_json,
        "context_buckets": w.context_json(),
        "sessions": rows[:10],
        "unavailable": unavailable,
    }


TYPICAL_DAYS = 14  # a typical day: the median of the days before it
DAY_STEP = timedelta(minutes=10)  # a day's cumulative chart


def day(ctx: Context, raw: str, query: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One local day: its spend adding up (split by the query's `group`, default by
    category) with its spikes, by project, agent type and model, its sessions, and
    the typical day before it to measure it against."""
    try:
        when = date.fromisoformat(raw)
    except ValueError:
        raise ApiError(400, "bad_query", "the day must be YYYY-MM-DD") from None
    group = _group(query or {})
    if when > ctx.today():
        raise ApiError(404, "not_found", "that day has not come yet")
    since = ctx.at_midnight(when)
    end = ctx.at_midnight(when + timedelta(days=1))
    until = min(end, ctx.now)
    w = _window(ctx)
    for req in ctx.index.requests(since=since, until=until):
        w.add(req)

    unavailable: list[str] = []
    cum_json = _day_chart(ctx, since, until, group, w.names, unavailable)
    typical = _typical(ctx, when, w)
    projects = w.groups_json(w.by_project)
    for p in projects:
        p["typical_usd"] = typical["by_project"].get(p["key"], 0.0) if typical else None
    rows = w.session_rows(ctx.tz)
    temporary = [r for r in rows if r["temporary"]]
    return {
        "day": when.isoformat(),
        "today": when == ctx.today(),
        "window": {"since": iso(since, ctx.tz), "until": iso(until, ctx.tz)},
        "totals": w.total.to_json(),
        "typical": None
        if typical is None
        else {k: v for k, v in typical.items() if k != "by_project"},
        "cumulative": cum_json,
        "group": group.value,
        "spikes": _spike_marks(ctx, since, until, w.infos, w.names, unavailable),
        "by_project": projects,
        "by_agent": w.groups_json(w.by_agent),
        "by_model": w.groups_json(w.by_model),
        "sessions_count": len(rows) - len(temporary),
        "temporary_sessions": len(temporary),
        "sessions": rows[:10],
        "unavailable": unavailable,
    }


def _day_chart(
    ctx: Context,
    since: datetime,
    until: datetime,
    group: GroupBy,
    names: ProjectNames,
    unavailable: list[str],
) -> dict[str, Any] | None:
    """A day's cumulative chart, split by `group`: the one part of its reply that
    depends on the group."""
    cum = _try(
        lambda: ctx.analysis.cumulative(
            ctx.index, ctx.table, since, until, group, DAY_STEP, top=TOP_MEMBERS
        ),
        unavailable,
    )
    return None if cum is None else _cumulative_json(_tidy(cum, group, names), DAY_STEP, until)


def day_regroup(ctx: Context, query: Mapping[str, str], base: Mapping[str, Any]) -> dict[str, Any]:
    """A day's reply for the `group` of `query`, made from `base`, its reply for
    another group at the same state of the index: only the chart is computed again,
    over the base's own window, so the chart and the totals describe the same span."""
    group = _group(query)
    since = datetime.fromisoformat(base["window"]["since"])
    until = datetime.fromisoformat(base["window"]["until"])
    unavailable = list(base.get("unavailable") or [])
    chart = _day_chart(ctx, since, until, group, ctx.names(), unavailable)
    return {**base, "cumulative": chart, "group": group.value, "unavailable": unavailable}


def _typical(ctx: Context, when: date, w: Window) -> dict[str, Any] | None:
    """The median of the days with spend among the TYPICAL_DAYS before `when`, in
    all, by category and by project: days off would pull a median of every day toward
    zero and make any working day look expensive. None without such a day."""
    days = [when - timedelta(days=k) for k in range(TYPICAL_DAYS, 0, -1)]
    total = dict.fromkeys(days, 0.0)
    by_category: dict[str, dict[date, float]] = {}
    by_project: dict[str, dict[date, float]] = {}
    for req in ctx.index.requests(since=ctx.at_midnight(days[0]), until=ctx.at_midnight(when)):
        d = ctx.local(req.timestamp).date()
        if d not in total or is_placeholder(req.model):
            continue
        cost = price(req, ctx.table)
        total[d] += cost.total
        key = w.names.key(req.project_dir)
        by_project.setdefault(key, dict.fromkeys(days, 0.0))[d] += cost.total
        parts = {
            "cache_read": cost.cache_read,
            "cache_write": cost.cache_write,
            "output": cost.output + cost.web,
            "input": cost.input,
        }
        for cat, usd in parts.items():
            by_category.setdefault(cat, dict.fromkeys(days, 0.0))[d] += usd

    active = [d for d in days if total[d] > 0]
    if not active:
        return None

    def median(values: Mapping[date, float]) -> float:
        return round(statistics.median(values[d] for d in active), 6)

    return {
        "days": len(active),
        "of_days": TYPICAL_DAYS,
        "usd": median(total),
        "categories": {k: median(v) for k, v in by_category.items()},
        "by_project": {k: median(v) for k, v in by_project.items()},
    }


def _daily_and_heat(
    ctx: Context, hourly: Sequence[Series], keep: Sequence[str], days: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Hourly steps (from local midnight, so each lies within one local hour) summed
    by local date and by local weekday and hour. The dates count back from today's
    local date, whatever daylight-saving changes lie between."""
    members = _fold(hourly, keep)
    today = ctx.today()
    dates = [today - timedelta(days=days - 1 - i) for i in range(days)]
    pos = {d: i for i, d in enumerate(dates)}
    per_day = [[0.0] * len(members) for _ in dates]
    cells = [[0.0] * 24 for _ in range(7)]
    times = hourly[0].times if hourly else ()
    for i, t in enumerate(times):
        local = ctx.local(t)
        day = pos.get(local.date())
        for j, (_, _, values) in enumerate(members):
            v = values[i]
            if not v:
                continue
            if day is not None:
                per_day[day][j] += v
            cells[local.weekday()][local.hour] += v
    daily = {
        "members": [{"key": k, "label": label} for k, label, _ in members],
        "days": [
            {
                "date": d.isoformat(),
                "values": [round(v, 6) for v in per_day[i]],
                "total": round(sum(per_day[i]), 6),
            }
            for i, d in enumerate(dates)
        ],
    }
    heat = {
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "cells": [[round(v, 6) for v in row] for row in cells],
        "max": round(max(max(row) for row in cells), 6),
    }
    return daily, heat


def _hour_ms(ctx: Context, t: datetime) -> int:
    """The start of the local clock hour holding `t`, in epoch ms."""
    return ms(ctx.local(t).replace(minute=0, second=0, microsecond=0))


def _heat_hours(
    ctx: Context,
    hourly: Sequence[Series],
    projects: Mapping[int, Mapping[str, float]],
    names: ProjectNames,
) -> dict[str, Any]:
    """The heatmap's hours one by one: each local hour with spend (from the same
    hourly steps as the cells) and its costliest project, so a page can lay out the
    past days by date and list the dates behind a weekday-and-hour cell; and the
    costliest project of each weekday-and-hour cell over the window."""
    hours: list[dict[str, Any]] = []
    for i, t in enumerate(hourly[0].times if hourly else ()):
        total = sum(s.values[i] for s in hourly)
        if total <= 0:
            continue
        per = projects.get(_hour_ms(ctx, t), {})
        top = max(per, key=lambda k: per[k]) if per else None
        hours.append(
            {
                "start": iso(t, ctx.tz),
                "usd": round(total, 6),
                "project": names.label(top) if top is not None else None,
            }
        )
    cells: list[list[dict[str, float]]] = [[{} for _ in range(24)] for _ in range(7)]
    for start, per in projects.items():
        local = ctx.local(datetime.fromtimestamp(start / 1000, UTC))
        cell = cells[local.weekday()][local.hour]
        for k, v in per.items():
            cell[k] = cell.get(k, 0.0) + v
    top_projects = [
        [names.label(max(c, key=lambda k: c[k])) if c else None for c in row] for row in cells
    ]
    return {"hours": hours, "top_projects": top_projects}


HOUR = timedelta(hours=1)
SPIKE_REACH = timedelta(hours=12)  # how far before an hour a spike overlapping it may start
_OFFSET_SPACE = re.compile(r" (\d\d:?\d\d)$")


def hour(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    """One local clock hour: what it cost by project, agent type and kind of token,
    the spikes that overlap it with their causes, and its costliest sessions."""
    raw = (query.get("start") or "").strip()
    raw = _OFFSET_SPACE.sub(r"+\1", raw)  # a "+" left unescaped in the query reads as a space
    try:
        start = datetime.fromisoformat(raw)
    except ValueError:
        msg = "start must be an ISO 8601 time, e.g. 2026-10-21T05:00:00+02:00"
        raise ApiError(400, "bad_query", msg) from None
    if start.tzinfo is None:  # a local wall time
        start = start.replace(tzinfo=ctx.tz) if ctx.tz is not None else start.astimezone()
    if _hour_ms(ctx, start) != ms(start):
        raise ApiError(400, "bad_query", "start must be the start of a local hour")
    if start > ctx.now:
        raise ApiError(404, "not_found", "that hour has not come yet")
    end = start + HOUR
    until = min(end, ctx.now)
    w = _window(ctx)
    for req in ctx.index.requests(since=start, until=until):
        w.add(req)

    unavailable: list[str] = []
    found: list[Spike] | None = _try(
        lambda: ctx.analysis.find_spikes(ctx.index, ctx.table, start - SPIKE_REACH, until),
        unavailable,
    )
    spikes_json: list[dict[str, Any]] | None = None
    causes_json: list[dict[str, Any]] | None = None
    if found is not None:
        over = [sp for sp in found if sp.end > start and sp.start < until]
        over = sorted(over, key=lambda sp: -sp.excess_usd)[:MAX_SESSION_SPIKES]
        explained = _try(lambda: ctx.analysis.explain(ctx.index, ctx.table, over), unavailable)
        by_spike = _by_spike(explained or [])
        spikes_json = [
            _spike_json(ctx, sp, by_spike.get(sp, []), w.infos, w.agents, w.names)
            if explained is not None
            else _spike_json(ctx, sp, None, w.infos, w.agents, w.names)
            for sp in over
        ]
        if explained is not None:
            causes_json = _cause_totals(explained)
    rows = w.session_rows(ctx.tz)
    return {
        "window": {
            "start": iso(start, ctx.tz),
            "end": iso(end, ctx.tz),
            "until": iso(until, ctx.tz),
        },
        "partial": until < end,
        "totals": w.total.to_json(),
        "by_project": w.groups_json(w.by_project),
        "by_agent": w.groups_json(w.by_agent),
        "spikes": spikes_json,
        "causes": causes_json,
        "sessions_count": len(rows),
        "sessions": rows[:5],
        "unavailable": unavailable,
    }


def _cause_totals(explained: Sequence[Explanation]) -> list[dict[str, Any]]:
    """The named causes (capcom.explain) of some spikes, largest first: each cause's
    excess over the baseline summed over the spikes, with the headline of its largest."""
    by: dict[str, list[Explanation]] = {}
    for e in explained:
        if e.cause != CauseKind.UNEXPLAINED and e.excess_usd > 0:
            by.setdefault(str(e.cause), []).append(e)
    out = [
        {
            "cause": cause,
            "cause_label": CAUSE_LABELS.get(cause, cause),
            "cause_color": cause_color(cause),
            "excess_usd": round(sum(e.excess_usd for e in group), 6),
            "spikes": len({e.spike for e in group}),
            "title": max(group, key=lambda e: e.excess_usd).title,
        }
        for cause, group in by.items()
    ]
    return sorted(out, key=lambda c: -c["excess_usd"])


def sessions(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 0, 3660)
    limit = _int(query, "limit", 200, 1, 5000)
    sort = query.get("sort") or "usd"
    keys = {"usd", "last", "peak_context", "active_seconds", "subagents", "requests"}
    if sort not in keys:
        raise ApiError(400, "bad_query", "sort must be one of: " + ", ".join(sorted(keys)))
    q = (query.get("q") or "").strip().lower()
    since = ctx.midnight(days - 1) if days > 0 else None
    w = _window(ctx)
    for req in ctx.index.requests(since=since):
        w.add(req)
    rows = w.session_rows(ctx.tz)
    if q:
        types: dict[str, set[str]] = {}
        for sid, row in w.sessions.items():
            types[sid] = {
                (w.agents[a].agent_type or "").lower() for a in row.agents if a in w.agents
            }
        rows = [
            r
            for r in rows
            if q in (r["title"] or "").lower()
            or q in r["project"].lower()
            or q in r["project_dir"].lower()
            or any(q in t for t in types.get(r["session_id"], ()))
        ]
    # numbers stay numbers, ISO times stay strings; a missing value sorts last
    rows.sort(key=lambda r: (r[sort] is not None, r[sort] or 0, r["usd"]), reverse=True)
    return {
        "window": {"days": days, "since": iso(since, ctx.tz), "until": iso(ctx.now, ctx.tz)},
        "total": len(rows),
        "temporary": sum(1 for r in rows if r["temporary"]),
        "sessions": rows[:limit],
    }


def session(ctx: Context, session_id: str) -> dict[str, Any]:
    info = next((s for s in ctx.index.sessions() if s.session_id == session_id), None)
    reqs = list(ctx.index.requests(session_id=session_id))
    if info is None and not reqs:
        raise ApiError(404, "not_found", "no such session")
    agent_infos = {a.agent_id: a for a in ctx.index.agents(session_id=session_id)}
    names = ctx.names()  # a temporary folder names its project
    w = Window(ctx.table, {info.session_id: info} if info else {}, agent_infos, names)

    agent_order: list[str] = []
    agent_pos: dict[str, int] = {}
    cols: dict[str, list[Any]] = {k: [] for k in ("t", "agent", "context", "usd", "output")}
    cols["cache_write"] = []
    cols["estimated"] = []
    models: dict[str, int] = {}
    efforts: dict[str, int] = {}
    main = _Span()
    spans: dict[str, _Span] = {}
    counted: list[Request] = []
    message_ids: list[str] = []
    for req in reqs:
        cost = w.add(req)
        if cost is None:
            continue
        counted.append(req)
        message_ids.append(req.message_id)
        aid = req.agent_id
        if aid is None:
            span = main
        else:
            if aid not in agent_pos:
                agent_pos[aid] = len(agent_order)
                agent_order.append(aid)
                spans[aid] = _Span()
            span = spans[aid]
        span.add(req, cost.total)
        cols["t"].append(ms(req.timestamp))
        cols["agent"].append(-1 if aid is None else agent_pos[aid])
        cols["context"].append(req.usage.context)
        cols["usd"].append(round(cost.total, 6))
        cols["output"].append(req.usage.output)
        cols["cache_write"].append(req.usage.cache_write)
        cols["estimated"].append(1 if req.output_estimated else 0)
        model = canonical(ctx.table, req.model)
        models[model] = models.get(model, 0) + 1
        if req.effort:
            efforts[req.effort] = efforts.get(req.effort, 0) + 1

    total = w.total.usd
    agents_json: list[dict[str, Any]] = []
    by_type: dict[str, list[_Span]] = {}
    for aid in agent_order:
        span, ai = spans[aid], agent_infos.get(aid)
        kind = (ai.agent_type if ai else None) or "(unknown agent)"
        by_type.setdefault(kind, []).append(span)
        agents_json.append(
            {"agent_id": aid, "agent_type": kind, "description": ai.description if ai else None}
            | span.to_json(ctx.tz)
            | {"finished": ai.finished if ai else None}
        )
    type_rows: list[dict[str, Any]] = []
    if main.requests:
        # one main thread per session: no spawns, and its calls are a count, not a median
        type_rows.append(
            _type_row(MAIN_THREAD, [main], total) | {"spawns": None, "median_tool_calls": None}
        )
    for kind, group in by_type.items():
        type_rows.append(_type_row(kind, group, total))
    type_rows.sort(key=lambda r: -float(r["usd"]))

    pauses = []
    for prev, nxt in pairwise(counted):
        gap = (nxt.timestamp - prev.timestamp).total_seconds()
        if gap >= PAUSE_SECONDS:
            pauses.append(
                {
                    "start": iso(prev.timestamp, ctx.tz),
                    "end": iso(nxt.timestamp, ctx.tz),
                    "minutes": round(gap / 60, 1),
                    "next_usd": round(_price_total(ctx, nxt), 6),
                    "next_cache_write": nxt.usage.cache_write,
                }
            )
    compactions = [
        {
            "t": iso(e.timestamp, ctx.tz),
            "agent_id": e.agent_id,
            "trigger": e.attrs.get("trigger"),
            "pre_tokens": e.attrs.get("pre_tokens"),
            "post_tokens": e.attrs.get("post_tokens"),
        }
        for e in ctx.index.events(session_id=session_id, kinds=frozenset({EventKind.COMPACTION}))
    ]

    calibration = None
    for cs in ctx.index.last_cost_states():
        if cs.session_id == session_id:
            calibration = calibration_json(ctx.index, cs, ctx.table, ctx.tz)
            break

    row = w.sessions.get(session_id)
    found: list[Spike] | None = None
    if counted:
        since, until = counted[0].timestamp, counted[-1].timestamp + timedelta(seconds=1)
        with contextlib.suppress(Unavailable):
            found = ctx.analysis.find_spikes(ctx.index, ctx.table, since, until, session_id)
    thinned = None
    if len(counted) > MAX_POINTS:
        ids = _thin_keep_ids(found or ())
        spiky = {i for i, m in enumerate(message_ids) if m in ids}
        kept = thin(cols["agent"], cols["usd"], cols["context"], spiky, limit=MAX_POINTS)
        cols = {k: [v[i] for i in kept] for k, v in cols.items()}
        thinned = {"requests": len(counted), "kept": len(kept), "spikes": len(spiky)}
    seen = [
        t for t in (reqs[-1].timestamp if reqs else None, info.last_seen if info else None) if t
    ]
    last_seen = max(seen) if seen else None
    first_seen = (info.first_seen if info else None) or (reqs[0].timestamp if reqs else None)
    project_dir = info.project_dir if info else reqs[0].project_dir
    return {
        "session": {
            "session_id": session_id,
            "title": info.title if info else None,
            "color": info.color if info else None,
            "title_custom": bool(info and info.custom_title),
            "project": project_display(project_dir, names),
            "project_path": names.path(project_dir),
            "project_dir": project_dir,
            "cwd": info.cwd if info else None,
            "git_branch": info.git_branch if info else None,
            "version": info.version if info else None,
            "entrypoint": info.entrypoint if info else None,
            "first_seen": iso(first_seen, ctx.tz),
            "last_seen": iso(last_seen, ctx.tz),
            "running": last_seen is not None and last_seen >= ctx.now - LIVE_WINDOW,
            "models": sorted(models, key=lambda m: -models[m]),
            "efforts": sorted(efforts),
            # requests per effort level, the most used first
            "effort_counts": dict(sorted(efforts.items(), key=lambda e: (-e[1], e[0]))),
        },
        "totals": w.total.to_json(),
        "active_seconds": round(row.active_seconds) if row else 0,  # None: placeholders only
        "calibration": calibration,
        "requests": cols,
        "thinned": thinned,
        "main": main.to_json(ctx.tz) if main.requests else None,
        "agents": agents_json,
        "agent_types": type_rows,
        "pauses": pauses,
        "compactions": compactions,
        "spikes": _session_spikes(ctx, found, info, names),
    }


@dataclass(slots=True)
class _Span:
    """One thread of a session (the main thread or a subagent)."""

    first: datetime | None = None
    last: datetime | None = None
    requests: int = 0
    usd: float = 0.0
    tool_calls: int = 0
    peak: int = 0

    def add(self, req: Request, usd: float) -> None:
        self.first = self.first or req.timestamp
        self.last = req.timestamp
        self.requests += 1
        self.usd += usd
        self.tool_calls += len(req.tool_calls)
        self.peak = max(self.peak, req.usage.context)

    def to_json(self, tz: tzinfo | None = None) -> dict[str, Any]:
        return {
            "first": iso(self.first, tz),
            "last": iso(self.last, tz),
            "requests": self.requests,
            "usd": round(self.usd, 6),
            "tool_calls": self.tool_calls,
            "peak_context": self.peak,
        }


def _type_row(kind: str, group: list[_Span], total: float) -> dict[str, Any]:
    usd = sum(g.usd for g in group)
    return {
        "agent_type": kind,
        "spawns": len(group),
        "tool_calls": sum(g.tool_calls for g in group),
        "median_tool_calls": statistics.median(g.tool_calls for g in group),
        "average_peak_context": round(sum(g.peak for g in group) / len(group)),
        "usd": round(usd, 6),
        "share": round(usd / total, 4) if total else 0.0,
    }


def _thin_keep_ids(found: Sequence[Spike]) -> set[str]:
    """The requests thinning keeps for spikes: every request of a thread's spike, and
    the first and last of a session-wide span (which may hold thousands)."""
    ids: set[str] = set()
    for sp in found:
        if sp.scope == "session":
            ids.update(sp.message_ids[:1] + sp.message_ids[-1:])
        else:
            ids.update(sp.message_ids)
    return ids


def _session_spikes(
    ctx: Context, found: Sequence[Spike] | None, info: SessionInfo | None, names: ProjectNames
) -> dict[str, Any] | None:
    """A session's spikes (the MAX_SESSION_SPIKES largest) with their causes; None
    when capcom.timeline is not available, `explained` false when capcom.explain
    is not."""
    if found is None:
        return None
    top = sorted(found, key=lambda sp: -sp.excess_usd)[:MAX_SESSION_SPIKES]
    try:
        causes: dict[Spike, list[Explanation]] | None = _by_spike(
            ctx.analysis.explain(ctx.index, ctx.table, top)
        )
    except Unavailable:
        causes = None
    infos = {info.session_id: info} if info else {}
    agents = {a.agent_id: a for a in ctx.index.agents(session_id=info.session_id)} if info else {}
    return {
        "count": len(found),
        "excess_usd": round(sum(sp.excess_usd for sp in found), 6),
        "explained": causes is not None,
        "items": [
            _spike_json(
                ctx, sp, None if causes is None else causes.get(sp, []), infos, agents, names
            )
            for sp in top
        ],
    }


def thin(
    threads: Sequence[int],
    usd: Sequence[float],
    context: Sequence[int],
    keep: set[int],
    limit: int = MAX_POINTS,
) -> list[int]:
    """Positions of the per-request points to keep for a session's charts, in order:
    at most about `limit` of them, however many threads or spike requests there are.

    The spike requests (`keep`) get up to half of `limit`: all of them when they fit,
    else they are cut into runs (in time order) that each keep their costliest
    request and their largest and smallest context. The other requests get the rest
    of `limit` the same way, in coarser runs: a thread with at least a run's worth of
    requests (the main thread, a long subagent) is cut into runs of its own, so its
    line keeps its shape; the requests of the smaller threads are cut together. The
    session's first and last request always stay."""
    n = len(usd)
    if n <= limit:
        return list(range(n))
    out = {0, n - 1}
    spikes = sorted(i for i in keep if 0 <= i < n)
    share = limit // 2
    if len(spikes) <= share:
        out.update(spikes)
    else:
        _runs(spikes, math.ceil(3 * len(spikes) / share), usd, context, out)
    budget = max(limit - len(out), limit // 4)
    by_thread: dict[int, list[int]] = {}
    for i, a in enumerate(threads):
        by_thread.setdefault(a, []).append(i)
    sizes = [len(idx) for idx in by_thread.values()]

    def points(run: int) -> int:
        big = sum(math.ceil(k / run) for k in sizes if k >= run)
        return 3 * (big + math.ceil(sum(k for k in sizes if k < run) / run))

    # the shortest run that fits the budget: at least 3 n / budget, and 6 n / budget
    # always fits (at most 2 n / run + 1 runs: one per big thread plus the leftovers)
    lo, hi = math.ceil(3 * n / budget), math.ceil(6 * n / budget)
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if points(mid) <= budget else (mid + 1, hi)
    small: list[int] = []
    for idx in by_thread.values():
        if len(idx) >= lo:
            _runs(idx, lo, usd, context, out)
        else:
            small.extend(idx)
    small.sort()
    _runs(small, lo, usd, context, out)
    return sorted(out)


def _runs(
    idx: Sequence[int], run: int, usd: Sequence[float], context: Sequence[int], out: set[int]
) -> None:
    """Add to `out` the costliest request and the largest and smallest context of
    each run of `run` positions of `idx`."""
    for c in range(0, len(idx), run):
        chunk = idx[c : c + run]
        out.add(max(chunk, key=usd.__getitem__))
        out.add(max(chunk, key=context.__getitem__))
        out.add(min(chunk, key=context.__getitem__))


def _price_total(ctx: Context, req: Request) -> float:
    return price(req, ctx.table).total


def live(ctx: Context) -> dict[str, Any]:
    since = ctx.now - LIVE_WINDOW
    infos = ctx.infos()
    names = ctx.names(infos)
    w = Window(ctx.table, infos, {}, names)
    for req in ctx.index.requests(since=since):
        w.add(req)
    ids = set(w.sessions)
    ids |= {sid for sid, s in infos.items() if s.last_seen is not None and s.last_seen >= since}
    out: list[dict[str, Any]] = []
    for sid in ids:
        info, row = infos.get(sid), w.sessions.get(sid)
        usd = row.tally.usd if row else 0.0
        project_dir = info.project_dir if info else (row.project_dir if row else "")
        out.append(
            {
                "session_id": sid,
                "title": info.title if info else None,
                "color": info.color if info else None,
                "project": project_display(project_dir, names),
                "agents_active": len(row.agents) if row else 0,
                "usd_last_10_min": round(usd, 6),
                "usd_per_hour": round(usd * 3600 / LIVE_WINDOW.total_seconds(), 6),
                "last_seen": iso(
                    (row.last if row else None) or (info.last_seen if info else None), ctx.tz
                ),
            }
        )
    out.sort(key=lambda s: (-float(s["usd_per_hour"] or 0), str(s["session_id"])))
    reply: dict[str, Any] = {"window_s": int(LIVE_WINDOW.total_seconds()), "sessions": out}
    reply["watching"] = ctx.watching
    if not ctx.watching:  # nothing new reaches the index: say how old it is, never "no session"
        reply["index_updated"] = status(ctx).get("index", {}).get("updated")
    return reply


# --------------------------------------------------------------------------- spikes

MAX_SPIKES = 40  # spikes listed on the Spikes page: the largest by excess
WINDOW_BARS = 36  # about this many bars in a spike's window chart
_BAR_STEPS = tuple(timedelta(minutes=m) for m in (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720))

# capcom.types.CauseKind and Confidence in words, as the CLI says them (an
# unknown id is shown as it is; without capcom.explain there are no causes to name)
CAUSE_LABELS: dict[str, str] = {}
CONFIDENCE_LABELS: dict[str, str] = {}
with contextlib.suppress(Exception):  # like Analysis, a broken module is only missing
    from capcom.explain import fmt as _fmt

    CAUSE_LABELS = {k.value: v for k, v in _fmt.CAUSE_LABELS.items()}
    CONFIDENCE_LABELS = {k.value: v for k, v in _fmt.CONFIDENCE_LABELS.items()}


# The cause colors: the validated categorical palette (style.css --s1..--s6, in its
# order) for the causes seen most, so a cause keeps its color across windows; the
# others and the unexplained rest in gray.
CAUSE_PALETTE = (
    CauseKind.TOOL_WAIT_EXPIRY,
    CauseKind.PARALLEL_SUBAGENTS,
    CauseKind.IDLE_REWRITE,
    CauseKind.CONTEXT_GROWTH,
    CauseKind.LATE_RESUME_EXPIRY,
    CauseKind.FAN_OUT,
)


def cause_color(cause: str) -> str:
    """A cause's color as a CSS value (CAUSE_PALETTE)."""
    for i, k in enumerate(CAUSE_PALETTE):
        if k.value == cause:
            return f"var(--s{i + 1})"
    return "var(--gray)"


def spike_key(sp: Spike) -> str:
    """A short stable id for a spike, for links (`/spikes?spike=<key>`)."""
    raw = "|".join(
        (sp.session_id or "", sp.agent_id or "", sp.start.isoformat(), sp.kind.value, sp.scope)
    )
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:12]


def _by_spike(explained: Sequence[Explanation]) -> dict[Spike, list[Explanation]]:
    out: dict[Spike, list[Explanation]] = {}
    for e in explained:
        out.setdefault(e.spike, []).append(e)
    return out


def _evidence_json(ev: Evidence, ctx: Context) -> dict[str, Any]:
    return {
        "label": ev.label,
        "session_id": ev.session_id,
        "agent_id": ev.agent_id,
        "timestamp": iso(ev.timestamp, ctx.tz),
        "message_id": ev.message_id,
        "event_key": ev.event_key,
    }


def _cause_json(e: Explanation, ctx: Context) -> dict[str, Any]:
    cause, conf = str(e.cause), str(e.confidence)
    return {
        "cause": cause,
        "cause_label": CAUSE_LABELS.get(cause, cause),
        "confidence": conf,
        "confidence_label": CONFIDENCE_LABELS.get(conf, conf),
        "excess_usd": round(e.excess_usd, 6),
        "title": e.title,
        "summary": e.summary,
        "advice": e.advice,
        "evidence": [_evidence_json(ev, ctx) for ev in e.evidence],
    }


def _headline(causes: Sequence[Explanation], excess_usd: float) -> dict[str, Any]:
    """The main cause of a spike for a marker or a list row: its largest named cause,
    never the unexplained rest. That rest is reported apart, with `mostly_unexplained`
    set when it is more than half the excess; a spike with no named cause is headed
    "No rule fits"."""
    rest = sum(e.excess_usd for e in causes if e.cause == CauseKind.UNEXPLAINED)
    out: dict[str, Any] = {
        "unexplained_usd": round(rest, 6),
        "mostly_unexplained": bool(causes) and rest > excess_usd / 2,
    }
    if not causes:
        none = {"cause": None, "cause_label": None, "cause_color": None, "confidence": None}
        return none | {"headline": None} | out
    named = [e for e in causes if e.cause != CauseKind.UNEXPLAINED]
    top = max(named or causes, key=lambda e: e.excess_usd)
    cause = str(top.cause)
    label = CAUSE_LABELS.get(cause, cause)
    return {
        "cause": cause,
        "cause_label": label,
        "cause_color": cause_color(cause),
        "confidence": str(top.confidence),
        "headline": top.title if named else label,
    } | out


def _spike_project(
    sp: Spike, infos: Mapping[str, SessionInfo], names: ProjectNames
) -> dict[str, str | None]:
    """The project of a spike's session: its shared label and its full folder."""
    info = infos.get(sp.session_id) if sp.session_id else None
    if info is None:
        return {"project": None, "project_path": None}
    return {
        "project": project_display(info.project_dir, names),
        "project_path": names.path(info.project_dir),
    }


def _spike_json(
    ctx: Context,
    sp: Spike,
    causes: Sequence[Explanation] | None,
    infos: Mapping[str, SessionInfo],
    agents: Mapping[str, AgentInfo],
    names: ProjectNames,
) -> dict[str, Any]:
    info = infos.get(sp.session_id) if sp.session_id else None
    agent = agents.get(sp.agent_id) if sp.agent_id else None
    return {
        "key": spike_key(sp),
        "kind": sp.kind.value,
        "scope": sp.scope,
        "start": iso(sp.start, ctx.tz),
        "end": iso(sp.end, ctx.tz),
        "session_id": sp.session_id,
        "agent_id": sp.agent_id,
        "agent_type": agent.agent_type if agent else None,
        "title": info.title if info else None,
        "color": info.color if info else None,
        **_spike_project(sp, infos, names),
        "requests": len(sp.message_ids),
        "cost_usd": round(sp.cost_usd, 6),
        "baseline_usd": round(sp.baseline_usd, 6),
        "excess_usd": round(sp.excess_usd, 6),
        "explanations": None if causes is None else [_cause_json(e, ctx) for e in causes],
    } | _headline(causes or (), sp.excess_usd)


def _spike_window(ctx: Context, sp: Spike) -> dict[str, Any]:
    """The spend of the spike's scope (its thread, or its whole session) in bars
    around the spike: what its requests cost at the baseline pace ("usual") and
    above it ("extra"), the extra adding up to the spike's excess."""
    span = sp.end - sp.start
    pad = min(max(span, timedelta(minutes=30)), timedelta(hours=3))
    lo, hi = sp.start - pad, sp.end + pad
    step = next((st for st in _BAR_STEPS if (hi - lo) / st <= WINDOW_BARS), _BAR_STEPS[-1])
    lo = datetime.fromtimestamp(lo.timestamp() // step.total_seconds() * step.total_seconds(), UTC)
    n = max(1, math.ceil((hi - lo) / step))
    usual, extra = [0.0] * n, [0.0] * n
    ids = set(sp.message_ids)
    share = sp.baseline_usd / len(ids) if ids else 0.0
    spiky: list[tuple[int, float]] = []
    for r in ctx.index.requests(since=lo, until=lo + n * step, session_id=sp.session_id):
        if is_placeholder(r.model) or (sp.scope != "session" and r.agent_id != sp.agent_id):
            continue
        k = min(n - 1, int((r.timestamp - lo) / step))
        cost = price(r, ctx.table).total
        if r.message_id in ids:
            spiky.append((k, cost))
        else:
            usual[k] += cost
    above = sum(max(0.0, c - share) for _, c in spiky)
    scale = sp.excess_usd / above if above > 0 else 0.0
    for k, cost in spiky:
        x = max(0.0, cost - share) * scale
        extra[k] += x
        usual[k] += cost - x
    return {
        "start": iso(lo, ctx.tz),
        "step_s": int(step.total_seconds()),
        "usual": [round(v, 6) for v in usual],
        "extra": [round(v, 6) for v in extra],
        "spike_start": iso(sp.start, ctx.tz),
        "spike_end": iso(sp.end, ctx.tz),
    }


def _project_filter(
    query: Mapping[str, str], infos: Mapping[str, SessionInfo], names: ProjectNames
) -> str | None:
    """The project label `?project=` names (a label, a full folder or a project
    folder), None when absent; a project no session has is a 400."""
    raw = query.get("project")
    if not raw:
        return None
    for info in infos.values():
        label = project_display(info.project_dir, names)
        if raw in (label, names.path(info.project_dir), info.project_dir):
            return label
    raise ApiError(400, "bad_query", "project names no known project")


def spikes(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    cause = query.get("cause") or None
    if cause is not None and cause not in {c.value for c in CauseKind}:
        raise ApiError(400, "bad_query", "cause names no known cause")
    since = ctx.midnight(days - 1)
    ctx.progress(0.02)
    found = ctx.analysis.detect(ctx.index, ctx.table, since, ctx.now)
    infos = ctx.infos()
    names = ctx.names(infos)
    agents = ctx.agents()
    items: dict[Spike, dict[str, Any]] = {}  # a listed spike's JSON, made once
    by: dict[Spike, list[Explanation]] = {}
    total = len(found)
    if ctx.step is None or total == 0:
        explained = ctx.analysis.explain(ctx.index, ctx.table, found)
    else:
        # serve's job: explain session by session (the sessions of the largest
        # spikes first, newest first), publishing the reply so far after each batch
        for batch in _explain_batches(found):
            ctx.progress(SPIKES_DETECTED + (1 - SPIKES_DETECTED) * len(by) / total)
            for sp in batch:
                by[sp] = []
            for e in ctx.analysis.explain(ctx.index, ctx.table, batch):
                by[e.spike].append(e)
            so_far = [e for sp in found if sp in by for e in by[sp]]
            ctx.progress(
                SPIKES_DETECTED + (1 - SPIKES_DETECTED) * len(by) / total,
                lambda so_far=so_far: _spikes_reply(  # type: ignore[misc]
                    ctx, query, days, since, found, so_far, infos, names, agents, items, set(by)
                ),
            )
        explained = [e for sp in found for e in by[sp]]
    return _spikes_reply(ctx, query, days, since, found, explained, infos, names, agents, items)


SPIKES_DETECTED = 0.25  # the share of a spikes job's progress that detection stands for
EXPLAIN_BATCH = 40  # spikes explained per chunk of a spikes job (whole sessions)


def _explain_batches(found: Sequence[Spike]) -> list[list[Spike]]:
    """The spikes in batches of whole sessions (one session is loaded once): first
    the sessions that hold the largest spikes (the ones the page lists), newest
    first, then the rest, newest first."""
    groups: dict[str, list[Spike]] = {}
    for sp in found:
        groups.setdefault(sp.session_id or "", []).append(sp)
    listed = {sp for sp in sorted(found, key=lambda sp: -sp.excess_usd)[:MAX_SPIKES]}
    order = sorted(
        groups.values(),
        key=lambda g: (not any(sp in listed for sp in g), -max(sp.start.timestamp() for sp in g)),
    )
    out: list[list[Spike]] = []
    for g in order:
        if not out or len(out[-1]) >= EXPLAIN_BATCH:
            out.append([])
        out[-1].extend(g)
    return out


def _spikes_reply(
    ctx: Context,
    query: Mapping[str, str],
    days: int,
    since: datetime,
    found: list[Spike],
    explained: list[Explanation],
    infos: Mapping[str, SessionInfo],
    names: ProjectNames,
    agents: Mapping[str, AgentInfo],
    items: dict[Spike, dict[str, Any]],
    done: set[Spike] | None = None,
) -> dict[str, Any]:
    """The Spikes reply; with `done`, the reply so far: only the spikes in `done`
    have their causes yet, and only those are listed."""
    where = {sp: _spike_project(sp, infos, names) for sp in found}
    projects: dict[str, list[Any]] = {}  # label -> [path, excess, spikes]
    for sp in found:
        p = where[sp]
        row = projects.setdefault(p["project"] or "", [p["project_path"], 0.0, 0])
        row[1] += sp.excess_usd
        row[2] += 1
    by_project = sorted(projects.items(), key=lambda kv: (kv[0] == "", -kv[1][1]))
    project = _project_filter(query, infos, names)
    if project is not None:
        found = [sp for sp in found if where[sp]["project"] == project]
        kept = set(found)
        explained = [e for e in explained if e.spike in kept]
    causes = _by_spike(explained)
    totals: dict[str, list[float]] = {}
    for e in explained:
        row = totals.setdefault(str(e.cause), [0.0, 0])
        row[0] += e.excess_usd
        row[1] += 1
    by_cause = sorted(
        totals.items(), key=lambda kv: (kv[0] == CauseKind.UNEXPLAINED.value, -kv[1][0])
    )
    # ?cause= keeps the spikes with an explanation of that cause; `causes` still
    # lists every cause (the page's filter chips)
    cause = query.get("cause") or None
    if cause is not None:
        found = [sp for sp in found if any(str(e.cause) == cause for e in causes.get(sp, ()))]
    shown = sorted(found, key=lambda sp: -sp.excess_usd)[:MAX_SPIKES]
    if done is not None:
        shown = [sp for sp in shown if sp in done]
    largest = spike_key(shown[0]) if shown else None
    wanted = query.get("spike")  # a linked spike is listed even when it is not among the largest
    if wanted and not any(spike_key(sp) == wanted for sp in shown):
        shown += [sp for sp in found if spike_key(sp) == wanted][:1]
    shown.sort(key=lambda sp: (sp.start, sp.session_id or "", sp.agent_id or ""))
    listed = []
    for i, sp in enumerate(shown):
        if sp not in items:
            item = _spike_json(ctx, sp, causes.get(sp, []), infos, agents, names)
            items[sp] = item | {"window": _spike_window(ctx, sp)}
        listed.append({"n": i + 1} | items[sp])
    # matched as capcom.explain.matched counts it: its causes other than UNEXPLAINED
    # cover at least KEEP_SHARE (a tenth) of the spike's excess
    explained_count = sum(
        1
        for sp in found
        if (
            got := sum(e.excess_usd for e in causes.get(sp, ()) if e.cause != CauseKind.UNEXPLAINED)
        )
        > 0
        and got >= 0.10 * sp.excess_usd
    )
    return {
        "days": days,
        "since": iso(since, ctx.tz),
        "until": iso(ctx.now, ctx.tz),
        "count": len(found),
        "excess_usd": round(sum(sp.excess_usd for sp in found), 6),
        "explained": explained_count,
        "largest": largest,
        "cause": cause,
        "project": project,
        "project_path": projects[project][0] if project in projects else None,
        "by_project": [
            {
                "project": k or None,
                "project_path": v[0],
                "excess_usd": round(v[1], 6),
                "spikes": int(v[2]),
            }
            for k, v in by_project
        ],
        "causes": [
            {
                "cause": k,
                "label": CAUSE_LABELS.get(k, k),
                "color": cause_color(k),
                "excess_usd": round(v[0], 6),
                "explanations": int(v[1]),
            }
            for k, v in by_cause
        ],
        "spikes": listed,
    }


# --------------------------------------------------------------------------- checks

ONE_COMMAND_CALLS = 2  # capcom.checks: a subagent with this many tool calls or fewer
_CALL_BUCKETS = ((0, 0), (1, 1), (2, 2), (3, 3), (4, 5), (6, 10), (11, 20), (21, None))
MAX_SQUARES = 120  # past this many tools or skills, a bar instead of squares
MAX_DOTS = 200


def _limits(ctx: Context, days: int | None, full: bool, split: Split = "project") -> dict[str, Any]:
    """The usage windows from capcom.limits: the samples in the state folder, projects
    named as everywhere else, each window's spend split by `split`."""
    from capcom.limits.view import limits_json

    names = ctx.names()
    return limits_json(
        ctx.index,
        ctx.table,
        ctx.state_dir,
        ctx.now,
        ctx.tz,
        days=days,
        full=full,
        label=lambda d: project_display(d, names),
        split=split,
    )


# /api/limits?group=: the groups a window's spend splits by (capcom.limits.model.SPLITS),
# in the names of /api/spend's groups (no split by session)
LIMIT_SPLITS: dict[str, Split] = {
    GroupBy.CATEGORY.value: "category",
    GroupBy.PROJECT.value: "project",
    GroupBy.MODEL.value: "model",
    GroupBy.AGENT.value: "agent",
}


def limit_split(query: Mapping[str, str]) -> Split:
    """The `group` of /api/limits and /api/limit-window: by category by default, a 400
    for a split by session or an unknown group."""
    raw = query.get("group") or GroupBy.CATEGORY.value
    split = LIMIT_SPLITS.get(GROUP_ALIASES.get(raw, raw))
    if split is None:
        raise ApiError(400, "bad_query", "group must be one of: " + ", ".join(LIMIT_SPLITS))
    return split


def limits(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    if days not in LIMITS_DAYS:
        raise ApiError(400, "bad_query", f"days must be one of {', '.join(map(str, LIMITS_DAYS))}")
    split = limit_split(query)
    return {"days": days, "group": split} | _limits(ctx, days, full=True, split=split)


def checks(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    since = ctx.midnight(days - 1)
    ctx.progress(0.02)
    every = ctx.analysis.checks(ctx.index, ctx.table, since, ctx.now)
    ctx.progress(0.9)
    # as `capcom checks`: findings under the floor are counted in one line, not listed
    found = [f for f in every if f.saving_usd_per_week >= MIN_LISTED_USD_PER_WEEK]
    small = [f for f in every if f.saving_usd_per_week < MIN_LISTED_USD_PER_WEEK]
    kinds = {f.kind for f in found}
    infos = ctx.infos()
    names = ctx.names(infos)
    dirs = {info.project_dir for info in infos.values()}
    counted = counted_findings(found, dirs)
    blockers = overlap_blockers(found, dirs)
    overlapping = any(finding_overlaps(f) for f in found)
    calls = _subagent_calls(ctx, since) if "one_command_subagents" in kinds else {}
    comps = _compactions(ctx, since) if "late_compactions" in kinds else {}
    marks, measure_name = _open_marks(ctx)
    rows = []
    for i, f in enumerate(found):
        named = f.scope in dirs or f.scope.startswith("temporary (") or f.scope == TEMPORARY_LABEL
        label = project_display(f.scope, names) if named else f.scope
        rows.append(
            {
                "n": i + 1,
                "kind": f.kind,
                "title": f.title,
                "scope": f.scope,
                "scope_label": label,
                "scope_path": names.path(f.scope) if named else None,
                "summary": f.summary,
                "saving_usd_per_week": round(f.saving_usd_per_week, 6),
                "advice": f.advice,
                "change": f.change,
                "evidence": [_evidence_json(ev, ctx) for ev in f.evidence],
                "metrics": {k: _plain(v) for k, v in f.metrics.items()},
                "overlaps": list(finding_overlaps(f)),
                "counted": counted[i],
                # the counted findings (their `n`) that kept this one out of the total
                "overlapped_by": [j + 1 for j in blockers[i]],
                "visual": _visual(f, calls, comps),
                "finding_id": f"{f.kind}@{f.scope}",
                "mark": marks.get(f"{f.kind}@{f.scope}"),
                "measure": measure_name(f.kind),
            }
        )
    total = savings_total(found, dirs)
    return {
        "days": days,
        "since": iso(since, ctx.tz),
        "until": iso(ctx.now, ctx.tz),
        "rollups": _rollups(found, rows),
        "findings": rows,
        "overlapping": overlapping,
        "total": total,
        "small": {
            "findings": len(small),
            "usd_per_week": round(sum(f.saving_usd_per_week for f in small), 6),
            "below_usd_per_week": MIN_LISTED_USD_PER_WEEK,
        },
        "footer": _checks_footer(total, len(small)),
    }


def _open_marks(ctx: Context) -> tuple[dict[str, Any], Any]:
    """The open mark of each finding ("kind@scope") as JSON, and the name of the
    measure capcom.changes would use for a kind (None when it does not say); no marks
    without capcom.changes."""
    from capcom.report import didit

    try:
        backend = ctx.analysis.changes()
        found = didit.open_by_finding(didit._marks(ctx))
    except (Unavailable, ValueError, OSError):  # no backend, or a marks file it refuses
        return {}, lambda kind: None
    label = didit.labeler(ctx)
    named = getattr(backend, "measure_name", None)

    def measure_name(kind: str) -> str | None:
        with contextlib.suppress(Exception):
            name = named(kind) if callable(named) else None
            return name if isinstance(name, str) else None
        return None

    return {fid: didit.mark_json(m, label) for fid, m in found.items()}, measure_name


def _rollups(found: Sequence[Finding], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per kind (capcom.checks.rollup): its saving across its scopes, the
    scopes' shares (labeled as the findings' rows) and the numbers of its findings
    in `findings`. Judgement calls come last, apart: they are never in the total."""
    from capcom.checks.rollup import rollup

    judged = {f.kind for f in found if f.metrics.get("judgement_call")}
    labels = {(r["kind"], r["scope"]): r for r in rows}
    left_out = [{found[n - 1].kind for n in row["overlapped_by"]} for row in rows]
    out = []
    for r in sorted(rollup(found, left_out), key=lambda r: r.kind in judged):
        members = [row for row in rows if row["kind"] == r.kind]
        out.append(
            {
                "kind": r.kind,
                "title": r.title,
                "headline": r.headline,
                "saving_usd_per_week": round(r.saving_usd_per_week, 6),
                "scope_noun": r.scope_noun,
                "scopes": [
                    {
                        "scope": sc.scope,
                        "scope_label": labels[(r.kind, sc.scope)]["scope_label"]
                        if (r.kind, sc.scope) in labels
                        else sc.scope,
                        "saving_usd_per_week": round(sc.saving_usd_per_week, 6),
                        "share": round(sc.share, 4),
                    }
                    for sc in r.scopes
                ],
                "findings": [row["n"] for row in members],
                "overlaps": list(r.overlaps),
                "judgement_call": r.kind in judged,
                # every finding of the kind counted in the total, some, or none
                "counted": (
                    "all"
                    if all(row["counted"] for row in members)
                    else "some"
                    if any(row["counted"] for row in members)
                    else "none"
                ),
            }
        )
    return out


def _checks_footer(total: Mapping[str, Any] | None, small: int) -> str:
    """What the checks total covers, in plain words, for the page's footer."""
    parts = [
        "Each saving is an estimate at this window's pace, net of the cost of a fresh "
        "start where the advice means one, and an upper bound (\u201cup to\u201d)."
    ]
    if total is not None:
        parts.append(
            f"The total adds {total['findings']} of the {total['of']} listed findings: "
            "where two findings may count the same dollars, only the larger is added."
        )
        if "judgement_calls" in total:
            parts.append(
                "Judgement calls on quality, such as a cheaper model for an agent, are "
                "not in it: they would shrink the other savings on the same requests."
            )
    if small:
        parts.append(
            f"Findings under ${MIN_LISTED_USD_PER_WEEK:.2f}/week are not listed and not in it."
        )
    parts.append("The money is API-equivalent at list prices, not what your plan bills.")
    return " ".join(parts)


def _subagent_calls(ctx: Context, since: datetime) -> dict[str, list[int]]:
    """Tool calls per subagent in the window, by agent type."""
    per_agent: dict[str, int] = {}
    for r in ctx.index.requests(since=since, until=ctx.now):
        if r.agent_id is not None and not is_placeholder(r.model):
            per_agent[r.agent_id] = per_agent.get(r.agent_id, 0) + len(r.tool_calls)
    agents = ctx.agents()
    out: dict[str, list[int]] = {}
    for aid, n in per_agent.items():
        info = agents.get(aid)
        out.setdefault((info.agent_type if info else None) or "", []).append(n)
    return out


def _compactions(ctx: Context, since: datetime) -> dict[str, list[int]]:
    """Context size when each compaction of the window started, by project label
    (as capcom.checks scopes them)."""
    infos = ctx.infos()
    out: dict[str, list[int]] = {}
    kinds = frozenset({EventKind.COMPACTION})
    for e in ctx.index.events(since=since, until=ctx.now, kinds=kinds):
        pre = e.attrs.get("pre_tokens")
        info = infos.get(e.session_id)
        if isinstance(pre, int) and not isinstance(pre, bool) and info is not None:
            out.setdefault(project_label(info.project_dir), []).append(pre)
    return out


def _num(metrics: Mapping[str, Any], key: str) -> int | None:
    v = metrics.get(key)
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _visual(
    f: Finding, calls: Mapping[str, list[int]], comps: Mapping[str, list[int]]
) -> dict[str, Any] | None:
    """A small chart for a finding, from its metrics (and, for two kinds, from the
    index): what was loaded against what was used, calls per subagent, compaction
    sizes, a size against its limit. None for kinds without one."""
    m = f.metrics
    pairs = {
        "mcp_servers_unused": ("tools_loaded", "tools_called", "MCP tools", "called"),
        "agent_tools_unused": ("tools_given", "tools_called", "Tools given", "called"),
        "skills_unused": ("skills_listed", "skills_invoked", "Skills listed", "invoked"),
        "claudeai_connectors_unused": (
            "sessions_listing",
            "sessions_using",
            "Sessions that loaded them",
            "called one",
        ),
    }
    if f.kind in pairs:
        total_key, used_key, label, verb = pairs[f.kind]
        total, used = _num(m, total_key), _num(m, used_key)
        if total is None or used is None or total <= 0:
            return None
        kind = "squares" if total <= MAX_SQUARES else "split"
        return {"type": kind, "label": label, "total": total, "filled": min(used, total)} | {
            "verb": verb
        }
    if f.kind == "one_command_subagents":
        values = calls.get(f.scope, [])
        if not values:
            return None
        buckets = []
        for lo, hi in _CALL_BUCKETS:
            count = sum(1 for v in values if v >= lo and (hi is None or v <= hi))
            label = str(lo) if lo == hi else (f"{lo}+" if hi is None else f"{lo}-{hi}")
            buckets.append(
                {
                    "label": label,
                    "count": count,
                    "small": hi is not None and hi <= ONE_COMMAND_CALLS,
                }
            )
        return {"type": "histogram", "label": "Subagents by number of tool calls"} | {
            "buckets": buckets
        }
    if f.kind == "late_compactions":
        sizes = sorted(comps.get(f.scope, []), reverse=True)[:MAX_DOTS]
        threshold = _num(m, "threshold")
        if not sizes or threshold is None:
            return None
        return {"type": "dots", "label": "Context size when each compaction started"} | {
            "values": sizes,
            "threshold": threshold,
        }
    gauges = {
        "large_memory": ("tokens", "budget_tokens", "Memory files, in tokens"),
        "long_running_agents": ("max_context", "threshold", "Largest context reached"),
    }
    if f.kind in gauges:
        value_key, limit_key, label = gauges[f.kind]
        value, limit = _num(m, value_key), _num(m, limit_key)
        if value is None or limit is None or limit <= 0:
            return None
        return {"type": "gauge", "label": label, "value": value, "limit": limit}
    return None


def _plain(obj: Any) -> Any:
    """A value from capcom.types as JSON-ready data (times as ISO strings)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj
