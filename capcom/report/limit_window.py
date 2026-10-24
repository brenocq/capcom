"""`/api/limit-window`: one usage window of one account on its own page. How its
percentage rose (the readings, and the fitted percent of what was spent, adding up,
split by the page's group), what spent it (by project, agent type, model and token
type, in dollars and in fitted percent of the limit), its costliest sessions, its
spikes, and its own dollars per 1 %."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from capcom.cost.prices import canonical
from capcom.limits.accounts import load_known
from capcom.limits.detail import (
    START_FORMAT,
    Detail,
    by_category,
    find_window,
    own_rate,
    window_of,
    window_path,
)
from capcom.limits.model import (
    BUCKET,
    LABEL,
    Spend,
    Split,
    counted,
    load_samples,
    samples_path,
)
from capcom.limits.view import OTHER, TOP_MEMBERS
from capcom.report.api import (
    ApiError,
    Context,
    _count_prompts,
    _spike_marks,
    _window,
    limit_split,
)
from capcom.report.tally import iso, ms, project_display
from capcom.timeline.groups import MAIN_THREAD

STEPS = {"five_hour": timedelta(minutes=5), "seven_day": timedelta(hours=1)}


def _r(x: float | None, digits: int = 4) -> float | None:
    return None if x is None else round(x, digits)


def _parse(ctx: Context, query: Mapping[str, str]) -> tuple[str, Any, datetime]:
    account = (query.get("account") or "").strip()
    window = window_of(query.get("window") or "")
    raw = query.get("start") or ""
    if not account or window is None:
        raise ApiError(400, "bad_query", "account and window (5-hour or weekly) are needed")
    try:
        start = datetime.strptime(raw, START_FORMAT)
    except ValueError:
        try:
            start = datetime.fromisoformat(raw)
        except ValueError:
            raise ApiError(400, "bad_query", "start must be YYYY-MM-DDTHHMM") from None
    if start.tzinfo is None:
        start = start.replace(tzinfo=ctx.tz) if ctx.tz is not None else start.astimezone()
    return account, window, start


def limit_window(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    account, window, start = _parse(ctx, query)
    split = limit_split(query)
    samples = load_samples(samples_path(ctx.state_dir)) if ctx.state_dir is not None else []
    if not samples:
        raise ApiError(404, "not_found", "no usage readings recorded")
    names = ctx.names()
    d = find_window(
        ctx.index,
        ctx.table,
        samples,
        load_known(ctx.state_dir),
        ctx.now,
        account,
        window,
        start,
        label=lambda p: project_display(p, names),
    )
    if d is None:
        raise ApiError(404, "not_found", "no such window for that account")
    ctx.progress(0.5)
    return _reply(ctx, d, split)


class _Bands:
    """The fitted line split by one group: each member's fitted percent, adding up,
    marked at the line's times, so the members stack up to the line."""

    def __init__(self) -> None:
        self.run: dict[str, float] = {}
        self.marks: list[dict[str, float]] = []

    def add(self, key: str, percent: float) -> None:
        self.run[key] = self.run.get(key, 0.0) + percent

    def mark(self) -> None:
        self.marks.append(dict(self.run))

    def to_json(self) -> list[dict[str, Any]]:
        """The members with a share, largest at the end first, the ones past the
        first TOP_MEMBERS summed as one "(other)" last."""
        final = self.marks[-1] if self.marks else {}
        ranked = sorted((k for k, v in final.items() if v > 0), key=lambda k: (-final[k], k))
        top, rest = ranked[:TOP_MEMBERS], ranked[TOP_MEMBERS:]
        out = [{"key": k, "percent": [round(m.get(k, 0.0), 3) for m in self.marks]} for k in top]
        if rest:
            other = [round(sum(m.get(k, 0.0) for k in rest), 3) for m in self.marks]
            out.append({"key": OTHER, "percent": other})
        return out


def _reply(ctx: Context, d: Detail, split: Split) -> dict[str, Any]:
    seg, f, tz = d.segment, d.fit, ctx.tz
    since = seg.start
    until = min(seg.end, ctx.now)
    w = _window(ctx)
    step = STEPS[seg.window.value]
    times: list[int] = []
    fitted: list[float] = []
    spent: list[float] = []
    bands = _Bands()
    pct = {"project": dict[str, float](), "agent": dict[str, float](), "model": {}, "session": {}}
    cats = dict[str, float]()
    inferred_usd = 0.0
    # the spend adding up, for the window's own dollars per 1 %
    usd_at: list[tuple[datetime, float]] = []
    run_pct = run_usd = 0.0
    first = seg.points()[0] if seg.points() else (since, 0.0)
    # an unseen start draws the line from the first reading, and the bands on top of it
    base = 0.0 if seg.start_known else first[1]

    def mark(at: datetime) -> None:
        times.append(ms(at))
        fitted.append(round(base + run_pct, 3))
        spent.append(round(run_usd, 4))
        bands.mark()

    edge = since + step
    for req in ctx.index.requests(since=since, until=until):
        if not counted(req) or (d.keep is not None and not d.keep(req)):
            continue
        cost = w.add(req)
        if cost is None:
            continue
        while req.timestamp >= edge:
            mark(edge)
            edge += step
        model = canonical(ctx.table, req.model)
        one = Spend()
        one.add(req, model, cost.total, cost)
        p = f.percent(one) if f is not None else 0.0
        run_pct += p
        run_usd += cost.total
        usd_at.append((req.timestamp, run_usd))
        if d.inferred is not None and d.inferred(req):
            inferred_usd += cost.total
        agent = MAIN_THREAD
        if req.agent_id is not None:
            info = w.agents.get(req.agent_id)
            agent = (info.agent_type if info else None) or "(unknown agent)"
        for kind, key in (
            ("project", w.names.key(req.project_dir)),
            ("agent", agent),
            ("model", model),
            ("session", req.session_id),
        ):
            pct[kind][key] = pct[kind].get(key, 0.0) + p
        if f is None:
            continue
        kinds = by_category(f, one)
        for c, v in kinds.items():
            cats[c] = cats.get(c, 0.0) + v
        match split:
            case "category":
                for c, v in kinds.items():
                    bands.add(c, v)
            case "project":
                bands.add(project_display(req.project_dir, w.names), p)
            case "agent":
                bands.add(agent, p)
            case "model":
                bands.add(model, p)
    while edge <= until:
        mark(edge)
        edge += step
    mark(until)

    def usd_between(a: datetime, b: datetime) -> float:
        lo = next((u for t, u in reversed(usd_at) if t < a), 0.0)
        hi = next((u for t, u in reversed(usd_at) if t <= b), 0.0)
        return hi - lo

    own = own_rate(seg, usd_between)
    with_pct = f is not None

    def groups(groups: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
        out = w.groups_json(groups)
        for g in out:
            g["percent"] = _r(pct[kind].get(g["key"]), 3) if with_pct else None
        return out

    _count_prompts(ctx, w, since, until)
    sessions = w.session_rows(tz)[:10]
    for s in sessions:
        s["percent"] = _r(pct["session"].get(s["session_id"]), 3) if with_pct else None
    totals = w.total.to_json()
    for c, row in totals["categories"].items():
        row["percent"] = _r(cats.get(c), 3) if with_pct else None
    unavailable: list[str] = []
    acct = d.account
    peak = seg.last.used_percentage
    return {
        "account": {
            "id": acct.id,
            "label": acct.label,
            "current": acct.current,
            "several": d.several,
        },
        "window": seg.window.value,
        "label": LABEL[seg.window],
        "start": iso(seg.start, tz),
        "start_known": seg.start_known,
        "resets_at": iso(seg.resets_at, tz),
        "until": iso(until, tz),
        "current": seg.resets_at is not None and seg.resets_at > ctx.now,
        "peak": peak,
        "hit_limit": any(s.source == "rate_limit_error" for s in seg.samples),
        "readings": len(seg.samples),
        "points": [[ms(t), p] for t, p in seg.points()],
        "group": split,
        "fitted": {
            "times": times,
            "percent": fitted,
            "usd": spent,
            "base": base,
            "by": bands.to_json(),
        },
        "step_minutes": int(step.total_seconds() // 60),
        "bucket_minutes": int(BUCKET[seg.window].total_seconds() // 60),
        "fit": None
        if f is None
        else {
            "method": f.method,
            "usd_per_percent": _r(f.usd_per_percent),
            "rough": f.rough,
            "intervals": f.intervals,
            "note": f.note,
        },
        "own_usd_per_percent": _r(own),
        "fitted_percent": _r(run_pct, 3) if with_pct else None,
        "inferred_usd": _r(inferred_usd),
        "totals": totals,
        "by_project": groups(w.by_project, "project"),
        "by_agent": groups(w.by_agent, "agent"),
        "by_model": groups(w.by_model, "model"),
        "sessions": sessions,
        "sessions_count": len(w.sessions),
        "spikes": _spike_marks(ctx, since, until, w.infos, w.names, unavailable),
        "before": None if d.before is None else window_path(acct.id, d.before, tz),
        "after": None if d.after is None else window_path(acct.id, d.after, tz),
        "unavailable": unavailable,
    }
