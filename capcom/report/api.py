"""The website's JSON API (documented in API.md). Each endpoint is a function of a
`Context` (an open index, the prices, the clock) and the query; the server only
routes, encodes and handles errors.

Privacy: the payloads carry titles, project folders, paths, model, tool and agent
names, counts, times and dollars; never a prompt, a reply or a spawn description.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from typing import Any

from capcom.config import load_config
from capcom.cost.calibrate import check_session
from capcom.cost.model import price
from capcom.cost.prices import is_placeholder, normalize
from capcom.cost.top import MAIN_THREAD, project_label
from capcom.report.analysis import Analysis, Unavailable
from capcom.report.tally import (
    PAUSE_SECONDS,
    Tally,
    Window,
    counted_findings,
    finding_overlaps,
    iso,
    ms,
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

LIVE_WINDOW = timedelta(minutes=10)
TOP_MEMBERS = 6  # series beyond these are folded into "other"
SPEND_DAYS = (7, 14, 30, 90)
OTHER = "(other)"
MAX_MARKERS = 12  # spike markers on a cumulative chart: the largest by excess
MAX_POINTS = 4000  # per-request columns of a session above this are thinned
MAX_SESSION_SPIKES = 8  # spikes listed on a session's page: the largest by excess

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

# Cumulative-chart step per window length, so a chart has a few hundred points.
_STEPS = {1: timedelta(minutes=5), 7: timedelta(minutes=30), 14: timedelta(hours=1)}
_STEPS |= {30: timedelta(hours=2), 90: timedelta(hours=6)}

LIMITS_NOTE = (
    "No limit data yet. The 5-hour and weekly percentages are recorded from milestone 5 on; "
    "capcom shows no estimate until then."
)


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
    scanning: bool = False  # serve's first scan is still running: totals may be partial

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
    titles: Mapping[str, str | None],
    unavailable: list[str],
) -> dict[str, Any] | None:
    """The window's spikes (capcom.timeline.find_spikes) as chart markers: the
    MAX_MARKERS largest by excess, numbered in time order."""
    found: list[Spike] | None = _try(
        lambda: ctx.analysis.find_spikes(ctx.index, ctx.table, since, until), unavailable
    )
    if found is None:
        return None
    marks = sorted(found, key=lambda sp: -sp.excess_usd)[:MAX_MARKERS]
    marks.sort(key=lambda sp: (sp.start, sp.session_id or ""))
    explained = _try(lambda: ctx.analysis.explain(ctx.index, ctx.table, marks), unavailable)
    causes = _by_spike(explained or [])
    return {
        "count": len(found),
        "excess_usd": round(sum(sp.excess_usd for sp in found), 6),
        "explained": explained is not None,
        "marks": [
            {
                "n": i + 1,
                "key": spike_key(sp),
                "t": ms(sp.start),
                "end": ms(sp.end),
                "kind": sp.kind.value,
                "scope": sp.scope,
                "session_id": sp.session_id,
                "title": titles.get(sp.session_id) if sp.session_id else None,
                "agent_id": sp.agent_id,
                "requests": len(sp.message_ids),
                "cost_usd": round(sp.cost_usd, 6),
                "excess_usd": round(sp.excess_usd, 6),
            }
            | _headline(causes.get(sp, ()))
            for i, sp in enumerate(marks)
        ],
    }


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


# today's same-time comparison waits for a baseline of at least $1, or an hour of today
SAME_HOUR_MIN_USD = 1.0


def overview(ctx: Context) -> dict[str, Any]:
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

    unavailable: list[str] = []
    step = _STEPS[1]
    cum = _try(
        lambda: ctx.analysis.cumulative(
            ctx.index, ctx.table, today_since, ctx.now, GroupBy.CATEGORY, step
        ),
        unavailable,
    )
    if cum is not None:
        cum = _tidy(cum, GroupBy.CATEGORY, names)
    titles = {sid: s.title for sid, s in infos.items()}
    marks = _spike_marks(ctx, today_since, ctx.now, titles, unavailable)
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
        "cumulative_today": None if cum is None else _cumulative_json(cum, step, ctx.now),
        "spikes_today": marks,
        "limits": {"available": False, "message": LIMITS_NOTE},
        "unavailable": unavailable,
    }


def spend(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    if days not in SPEND_DAYS:
        raise ApiError(400, "bad_query", f"days must be one of {', '.join(map(str, SPEND_DAYS))}")
    try:
        group = GroupBy(query.get("group") or GroupBy.CATEGORY.value)
    except ValueError:
        raise ApiError(400, "bad_query", "group must be one of: " + ", ".join(GroupBy)) from None
    since = ctx.midnight(days - 1)
    w = _window(ctx)
    for req in ctx.index.requests(since=since):
        w.add(req)

    unavailable: list[str] = []
    step = _STEPS[days]
    cum = _try(
        lambda: ctx.analysis.cumulative(
            ctx.index, ctx.table, since, ctx.now, group, step, top=TOP_MEMBERS
        ),
        unavailable,
    )
    hourly = _try(
        lambda: ctx.analysis.per_step(
            ctx.index, ctx.table, since, ctx.now, group, timedelta(hours=1), top=TOP_MEMBERS
        ),
        unavailable,
    )
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
    titles = {sid: info.title for sid, info in w.infos.items()}
    marks = _spike_marks(ctx, since, ctx.now, titles, unavailable)

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
        "daily": daily_json,
        "heatmap": heat_json,
        "context_buckets": w.context_json(),
        "sessions": rows[:10],
        "unavailable": unavailable,
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
    efforts: set[str] = set()
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
        model = normalize(req.model)
        models[model] = models.get(model, 0) + 1
        if req.effort:
            efforts.add(req.effort)

    total = w.total.usd
    agents_json: list[dict[str, Any]] = []
    by_type: dict[str, list[_Span]] = {}
    for aid in agent_order:
        span, ai = spans[aid], agent_infos.get(aid)
        kind = (ai.agent_type if ai else None) or "(unknown agent)"
        by_type.setdefault(kind, []).append(span)
        agents_json.append(
            {"agent_id": aid, "agent_type": kind}
            | span.to_json()
            | {"finished": ai.finished if ai else None}
        )
    type_rows: list[dict[str, Any]] = []
    if main.requests:
        type_rows.append(_type_row(MAIN_THREAD, [main], total) | {"spawns": 0})
    for kind, group in by_type.items():
        type_rows.append(_type_row(kind, group, total))
    type_rows.sort(key=lambda r: -float(r["usd"]))

    pauses = []
    for prev, nxt in pairwise(counted):
        gap = (nxt.timestamp - prev.timestamp).total_seconds()
        if gap >= PAUSE_SECONDS:
            pauses.append(
                {
                    "start": ms(prev.timestamp),
                    "end": ms(nxt.timestamp),
                    "minutes": round(gap / 60, 1),
                    "next_usd": round(_price_total(ctx, nxt), 6),
                    "next_cache_write": nxt.usage.cache_write,
                }
            )
    compactions = [
        {
            "t": ms(e.timestamp),
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
            c = check_session(ctx.index, cs, ctx.table)
            calibration = {
                "claude_code_usd": round(c.claude_code_usd, 6),
                "capcom_usd": round(c.capcom_usd, 6),
                "spread": None if c.claude_code_usd == 0 else round(c.spread, 6),
                "comparable": c.comparable,
                "reasons": list(c.reasons),
                "reason_labels": [REASON_LABELS.get(r, r) for r in c.reasons],
            }
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
        },
        "totals": w.total.to_json(),
        "active_seconds": round(row.active_seconds) if row else 0,  # None: placeholders only
        "calibration": calibration,
        "requests": cols,
        "thinned": thinned,
        "main": main.to_json() if main.requests else None,
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

    def to_json(self) -> dict[str, Any]:
        return {
            "first": ms(self.first) if self.first else None,
            "last": ms(self.last) if self.last else None,
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
    return {"window_s": int(LIVE_WINDOW.total_seconds()), "sessions": out}


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


def _headline(causes: Sequence[Explanation]) -> dict[str, Any]:
    """The main cause of a spike (its largest share) for a marker or a list row."""
    if not causes:
        return {"cause": None, "cause_label": None, "confidence": None, "headline": None}
    top = max(causes, key=lambda e: e.excess_usd)
    cause = str(top.cause)
    return {
        "cause": cause,
        "cause_label": CAUSE_LABELS.get(cause, cause),
        "confidence": str(top.confidence),
        "headline": top.title,
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
        "start": ms(sp.start),
        "end": ms(sp.end),
        "session_id": sp.session_id,
        "agent_id": sp.agent_id,
        "agent_type": agent.agent_type if agent else None,
        "title": info.title if info else None,
        "project": project_display(info.project_dir, names) if info else None,
        "requests": len(sp.message_ids),
        "cost_usd": round(sp.cost_usd, 6),
        "baseline_usd": round(sp.baseline_usd, 6),
        "excess_usd": round(sp.excess_usd, 6),
        "explanations": None if causes is None else [_cause_json(e, ctx) for e in causes],
    } | _headline(causes or ())


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
        "start": ms(lo),
        "step_s": int(step.total_seconds()),
        "usual": [round(v, 6) for v in usual],
        "extra": [round(v, 6) for v in extra],
        "spike_start": ms(sp.start),
        "spike_end": ms(sp.end),
    }


def spikes(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    since = ctx.midnight(days - 1)
    found, explained = ctx.analysis.spikes(ctx.index, ctx.table, since, ctx.now)
    causes = _by_spike(explained)
    totals: dict[str, list[float]] = {}
    for e in explained:
        row = totals.setdefault(str(e.cause), [0.0, 0])
        row[0] += e.excess_usd
        row[1] += 1
    by_cause = sorted(
        totals.items(), key=lambda kv: (kv[0] == CauseKind.UNEXPLAINED.value, -kv[1][0])
    )
    shown = sorted(found, key=lambda sp: -sp.excess_usd)[:MAX_SPIKES]
    wanted = query.get("spike")  # a linked spike is listed even when it is not among the largest
    if wanted and not any(spike_key(sp) == wanted for sp in shown):
        shown += [sp for sp in found if spike_key(sp) == wanted][:1]
    shown.sort(key=lambda sp: (sp.start, sp.session_id or "", sp.agent_id or ""))
    infos = ctx.infos()
    names = ctx.names(infos)
    agents = ctx.agents()
    items = []
    for i, sp in enumerate(shown):
        item = _spike_json(ctx, sp, causes.get(sp, []), infos, agents, names)
        items.append({"n": i + 1} | item | {"window": _spike_window(ctx, sp)})
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
        "causes": [
            {
                "cause": k,
                "label": CAUSE_LABELS.get(k, k),
                "excess_usd": round(v[0], 6),
                "explanations": int(v[1]),
            }
            for k, v in by_cause
        ],
        "spikes": items,
    }


# --------------------------------------------------------------------------- checks

ONE_COMMAND_CALLS = 2  # capcom.checks: a subagent with this many tool calls or fewer
_CALL_BUCKETS = ((0, 0), (1, 1), (2, 2), (3, 3), (4, 5), (6, 10), (11, 20), (21, None))
MAX_SQUARES = 120  # past this many tools or skills, a bar instead of squares
MAX_DOTS = 200


def checks(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    days = _int(query, "days", 14, 1, 366)
    since = ctx.midnight(days - 1)
    found = ctx.analysis.checks(ctx.index, ctx.table, since, ctx.now)
    kinds = {f.kind for f in found}
    infos = ctx.infos()
    names = ctx.names(infos)
    dirs = {info.project_dir for info in infos.values()}
    counted = counted_findings(found, dirs)
    overlapping = any(finding_overlaps(f) for f in found)
    calls = _subagent_calls(ctx, since) if "one_command_subagents" in kinds else {}
    comps = _compactions(ctx, since) if "late_compactions" in kinds else {}
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
                "visual": _visual(f, calls, comps),
            }
        )
    total = savings_total(found, dirs)
    return {
        "days": days,
        "since": iso(since, ctx.tz),
        "until": iso(ctx.now, ctx.tz),
        "findings": rows,
        "overlapping": overlapping,
        "total": total,
    }


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
