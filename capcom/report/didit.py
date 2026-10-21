"""The "Did it work?" endpoints (API.md, "Did it work?"): the changes the user marked
as applied, each measured by capcom.changes before and after its date, the changes
capcom noticed, and the one write the website makes (`write`, behind the POST guard in
server.py).

capcom.changes is reached through `Analysis.changes()`, so the API runs, and is tested,
with a stand-in. A mark's note is the user's own words: it is sent back as text and the
pages render it with textContent.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from datetime import date, timedelta
from typing import Any

from capcom.report.api import ApiError, Context, _int
from capcom.report.tally import iso, project_display
from capcom.timeline.groups import TEMPORARY_LABEL
from capcom.types import ChangeMark, ChangeResult, Finding, Verdict

CHECK_DAYS = (7, 14, 30)  # the Checks windows: a mark answers a finding seen in one
MAX_NOTE = 500  # characters
MAX_FIELD = 300  # a kind or scope
WHAT = ("suggested", "other")
SOURCES = ("user", "noticed")
GIVEN = (Verdict.WORKING, Verdict.NO_EFFECT, Verdict.WORSE)  # a verdict, not a wait
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Label = Callable[[str], str]


def finding_id(kind: str, scope: str) -> str:
    """A finding's stable name across windows and scans: "kind@scope"."""
    return f"{kind}@{scope}"


def _backend(ctx: Context) -> Any:
    return ctx.analysis.changes()


def _marks(ctx: Context) -> list[ChangeMark]:
    return list(_backend(ctx).load(ctx.changes_path))


def open_by_finding(marks: Sequence[ChangeMark]) -> dict[str, ChangeMark]:
    """The open mark of each finding: the latest when there are several."""
    out: dict[str, ChangeMark] = {}
    for m in sorted(marks, key=lambda m: m.id):
        out[finding_id(m.kind, m.scope)] = m
    return out


def labeler(ctx: Context) -> Label:
    """A scope's label as Checks shows it: a project folder by its display name, an
    agent type or "all" as it is."""
    infos = ctx.infos()
    names = ctx.names(infos)
    dirs = {info.project_dir for info in infos.values()}

    def label(scope: str) -> str:
        named = scope in dirs or scope.startswith("temporary (") or scope == TEMPORARY_LABEL
        return project_display(scope, names) if named else scope

    return label


def _num(x: float | None, digits: int = 6) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(x, digits)


def mark_json(m: ChangeMark, label: Label) -> dict[str, Any]:
    return {
        "id": m.id,
        "finding_id": finding_id(m.kind, m.scope),
        "kind": m.kind,
        "scope": m.scope,
        "scope_label": "every project" if m.scope == "all" else label(m.scope),
        "applied_on": m.applied_on.isoformat(),
        "title": m.title,
        "what": m.what,
        "note": m.note,
        "expected_usd_per_week": _num(m.expected_usd_per_week),
        "marked_at": iso(m.marked_at) if m.marked_at else None,
        "source": m.source,
    }


def _triples(rows: Sequence[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [{"label": a, "before": b, "after": c} for a, b, c in rows]


def result_json(r: ChangeResult, label: Label, today: date, weeks: bool = False) -> dict[str, Any]:
    m = r.mark
    before_weeks = [w.week_start for w in r.weeks if not w.after]
    out = mark_json(m, label) | {
        "days_since": (today - m.applied_on).days,
        "measure": r.measure,
        "before": _num(r.before),
        "after": _num(r.after),
        "before_range": [_num(r.before_range[0]), _num(r.before_range[1])]
        if r.before_range
        else None,
        "requests_per_week_before": _num(r.requests_per_week_before, 2),
        "requests_per_week_after": _num(r.requests_per_week_after, 2),
        "measured_usd_per_week": _num(r.measured_usd_per_week),
        "verdict": r.verdict.value,
        "verdict_due": r.verdict_due.isoformat() if r.verdict_due else None,
        "reason": r.reason,
        "evidence": _triples(r.evidence),
        "quality": _triples(r.quality),
        "window": {
            "before_from": min(before_weeks).isoformat() if before_weeks else None,
            "before_to": (m.applied_on - timedelta(days=1)).isoformat(),
            "after_from": (m.applied_on + timedelta(days=1)).isoformat(),
        },
    }
    if weeks:
        out["weeks"] = [
            {
                "week_start": w.week_start.isoformat(),
                "value": _num(w.value),
                "requests": w.requests,
                "after": w.after,
            }
            for w in r.weeks
        ]
    return out


# --------------------------------------------------------------------------- reads


def route(ctx: Context, arg: str, query: Mapping[str, str]) -> dict[str, Any]:
    if arg == "":
        return summary(ctx)
    if arg == "noticed":
        return noticed(ctx, query)
    return one(ctx, arg)


def summary(ctx: Context) -> dict[str, Any]:
    """Every mark measured, newest first, with the counts by verdict and the measured
    saving against the expected one over the changes that have a verdict."""
    backend = _backend(ctx)
    label = labeler(ctx)
    today = ctx.today()
    marks = sorted(_marks(ctx), key=lambda m: (m.applied_on, m.id), reverse=True)
    cache = ctx.facts_cache  # the daily facts, shared by every mark and request
    results = [backend.measure(ctx.index, ctx.table, m, ctx.now, cache=cache) for m in marks]
    counts = {v.value: 0 for v in Verdict}
    for r in results:
        counts[r.verdict.value] += 1
    given = [r for r in results if r.verdict in GIVEN]
    measured = [r.measured_usd_per_week for r in given if r.measured_usd_per_week is not None]
    expected = [
        r.mark.expected_usd_per_week for r in given if r.mark.expected_usd_per_week is not None
    ]
    m_sum, e_sum = sum(measured), sum(expected)
    due = [r.verdict_due for r in results if r.verdict == Verdict.TOO_EARLY and r.verdict_due]
    return {
        "marks": len(marks),
        "counts": counts,
        "with_verdict": {
            "changes": len(given),
            "measured_usd_per_week": _num(m_sum) if measured else None,
            "expected_usd_per_week": _num(e_sum) if expected else None,
            "share": _num(m_sum / e_sum, 4) if measured and e_sum > 0 else None,
        },
        "next_verdict": min(due).isoformat() if due else None,
        "changes": [result_json(r, label, today) for r in results],
        "path": str(ctx.changes_path or backend.store_path()),
    }


def one(ctx: Context, raw: str) -> dict[str, Any]:
    if not raw.isdigit() or len(raw) > 9:
        raise ApiError(404, "not_found", "no such change")
    wanted = int(raw)
    mark = next((m for m in _marks(ctx) if m.id == wanted), None)
    if mark is None:
        raise ApiError(404, "not_found", "no such change")
    r = _backend(ctx).measure(ctx.index, ctx.table, mark, ctx.now, cache=ctx.facts_cache)
    return result_json(r, labeler(ctx), ctx.today(), weeks=True)


def _findings(ctx: Context, days: int) -> list[Finding]:
    since = ctx.midnight(days - 1)
    return ctx.analysis.checks(ctx.index, ctx.table, since, ctx.now)


def noticed(ctx: Context, query: Mapping[str, str]) -> dict[str, Any]:
    """Changes capcom saw in files it reads that match a finding of the Checks window
    and have no open mark yet."""
    days = _days(query)
    findings = _findings(ctx, days)
    saving = {finding_id(f.kind, f.scope): f.saving_usd_per_week for f in findings}
    marks = _marks(ctx)
    marked = open_by_finding(marks)
    label = labeler(ctx)
    rows = []
    since = ctx.midnight(days - 1)
    seen = _backend(ctx).noticed(ctx.index, ctx.table, findings, ctx.now, since=since, marks=marks)
    for n in seen:
        fid = finding_id(n.kind, n.scope)
        if fid in marked:
            continue
        rows.append(
            {
                "finding_id": fid,
                "kind": n.kind,
                "scope": n.scope,
                "scope_label": label(n.scope),
                "title": n.title,
                "path": str(n.path),
                "detail": n.detail,
                "since": iso(n.since, ctx.tz),
                "saving_usd_per_week": _num(saving.get(fid)),
            }
        )
    return {"days": days, "noticed": rows}


def count(ctx: Context) -> int | None:
    """How many changes are marked (the sidebar's badge); None without capcom.changes."""
    try:
        return len(_marks(ctx))
    except Exception:
        return None


def _days(query: Mapping[str, str]) -> int:
    days = _int(query, "days", 14, 1, 366)
    if days not in CHECK_DAYS:
        raise ApiError(400, "bad_query", "days must be one of 7, 14, 30")
    return days


# --------------------------------------------------------------------------- the write

FIELDS = {
    "mark": ({"action", "kind", "scope", "applied_on"}, {"what", "note", "days", "source"}),
    "edit": ({"action", "id"}, {"applied_on", "note"}),
    "unmark": ({"action", "id"}, set()),
}


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _text(body: Mapping[str, Any], key: str, limit: int) -> str:
    value = body[key]
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _bad(f"{key} must be text of 1 to {limit} characters")
    return value


def _note(body: Mapping[str, Any]) -> str:
    note = body.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_NOTE or _CONTROL.search(note):
        raise _bad(f"note must be one line of text, at most {MAX_NOTE} characters")
    return note.strip()


def first_day(ctx: Context) -> date | None:
    """The first local day the index holds a request (or a session) on."""
    requests = ctx.index.requests()  # ordered by time: only the first row is read
    try:
        first = next(requests, None)
    finally:
        close = getattr(requests, "close", None)
        if callable(close):
            close()
    seen = [i.first_seen for i in ctx.index.sessions() if i.first_seen is not None]
    if first is not None:
        seen.append(first.timestamp)
    return ctx.local(min(seen)).date() if seen else None


def _applied_on(ctx: Context, raw: Any) -> date:
    if not isinstance(raw, str) or not _DATE.fullmatch(raw):
        raise _bad("applied_on must be a date, YYYY-MM-DD")
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise _bad("applied_on must be a date, YYYY-MM-DD") from None
    if day > ctx.today():
        raise _bad("applied_on cannot be in the future")
    first = first_day(ctx)
    if first is None or day < first:
        raise _bad("applied_on is before the first day the logs hold")
    return day


def _id(body: Mapping[str, Any]) -> int:
    raw = body["id"]
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise _bad("id must be a number")
    return raw


def _open(marks: Sequence[ChangeMark], wanted: int) -> ChangeMark:
    mark = next((m for m in marks if m.id == wanted), None)
    if mark is None:
        raise ApiError(404, "not_found", "no such change")
    return mark


# A finding as a mark needs it: kind, scope, title, weekly saving
Known = tuple[str, str, str, float]


def _finding(
    ctx: Context, days: int, kind: str, scope: str, known: Callable[[int], Any] | None
) -> Known | None:
    """The finding a mark answers, from the cached Checks reply of that window when
    there is one (`known`), else from a Checks pass."""
    rows: Sequence[Known] | None = known(days) if known is not None else None
    if rows is None:
        rows = [(f.kind, f.scope, f.title, f.saving_usd_per_week) for f in _findings(ctx, days)]
    return next((r for r in rows if r[0] == kind and r[1] == scope), None)


def write(
    ctx: Context,
    body: Any,
    lock: AbstractContextManager[Any] | None = None,
    known: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Mark a finding as applied, edit a mark's date or note, or unmark it. `body` is the
    parsed JSON; anything outside the schema is refused before capcom.changes is
    called. The body is checked, and the finding looked up (`known(days)`: the cached
    Checks rows as `Known` tuples, or None), before `lock` is taken; under it only the
    marks are read again and written. Returns the mark as it now stands
    (`{"unmarked": id}` for an unmark)."""
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")
    action = body.get("action")
    if action not in FIELDS:
        raise _bad("action must be mark, edit or unmark")
    needed, optional = FIELDS[action]
    if missing := needed - body.keys():
        raise _bad("missing: " + ", ".join(sorted(missing)))
    if extra := body.keys() - needed - optional:
        raise _bad("unknown fields: " + ", ".join(sorted(extra)))
    backend = _backend(ctx)
    label = labeler(ctx)
    held = lock if lock is not None else nullcontext()
    if action == "unmark":
        wanted = _id(body)
        with held:
            mark = _open(_marks(ctx), wanted)
            backend.unmark(mark.id, path=ctx.changes_path)
        return {"unmarked": mark.id}
    if action == "edit":
        wanted = _id(body)
        if "applied_on" not in body and "note" not in body:
            raise _bad("an edit needs applied_on or note")
        changes: dict[str, Any] = {}
        if "applied_on" in body:
            changes["applied_on"] = _applied_on(ctx, body["applied_on"])
        if "note" in body:
            changes["note"] = _note(body)
        with held:
            mark = _open(_marks(ctx), wanted)
            edited = backend.edit(mark.id, path=ctx.changes_path, **changes)
        return {"mark": mark_json(edited, label)}
    kind = _text(body, "kind", MAX_FIELD)
    scope = _text(body, "scope", MAX_FIELD)
    day = _applied_on(ctx, body["applied_on"])
    what = body.get("what", "suggested")
    if what not in WHAT:
        raise _bad("what must be suggested or other")
    source = body.get("source", "user")
    if source not in SOURCES:
        raise _bad("source must be user or noticed")
    note = _note(body)
    days = body.get("days", 14)
    if not isinstance(days, int) or isinstance(days, bool) or days not in CHECK_DAYS:
        raise _bad("days must be one of 7, 14, 30")
    found = _finding(ctx, days, kind, scope, known)
    if found is None:
        raise _bad("no such finding in the Checks of that window")
    with held:
        # one open mark per finding: re-marking is Change the date, or Unmark first
        if finding_id(kind, scope) in open_by_finding(_marks(ctx)):
            raise ApiError(409, "already_marked", "this finding is already marked as applied")
        made = backend.mark(
            kind,
            scope,
            day,
            found[2],
            what=what,
            note=note,
            expected_usd_per_week=found[3],
            source=source,
            path=ctx.changes_path,
        )
    return {"mark": mark_json(made, label)}
