"""A mark measured on the logs (design.md, "Did it work?").

Weeks are local and start on the applied date's weekday, so the date is a
boundary: the before window is the 4 weeks ending at the start of the applied day
(clipped to where the logs begin), and the after window runs from the start of the
next day to `now` (the applied day itself, when the change was being made, is in
neither). Each week is one pass of the checks' facts over it (capcom.checks.facts),
so the measure counts what the finding counted.

Verdict, on rates only, so a busier or quieter week does not count:

- no data: nothing in scope before the date, or the kind has no measure;
- too early: under 7 days since the date, or fewer requests in scope than
  max(200, 20 % of a usual before week's);
- working / no effect / worse: the after rate below / inside / above the range
  of the full before weeks (lowest to highest). A usual week is the median
  requests of the full before weeks; a full week under 20 % of it is charted but
  left out of the range, its rate resting on too few requests.

Measured saving per week = (before - after) / scale x the after window's base per
week (requests, or runs) x the unit value (measures.py: what one counted unit was
worth in the before window, or in the after one when the before window counted
none). Given for working and worse (negative), not for no
effect, whose difference is within the usual weekly swing.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from statistics import median

from capcom.changes.measures import ERRORS_ROW, MEASURES, Measure, Tally
from capcom.checks.facts import Facts, collect
from capcom.types import ChangeMark, ChangeResult, Index, MeasurePoint, PriceTable, Verdict

BEFORE_WEEKS = 4
MIN_DAYS = 7
MIN_REQUESTS = 200
MIN_SHARE = 0.2  # of a usual before week's requests

FactsCache = dict[tuple[datetime, datetime], Facts]


def local_midnight(d: date) -> datetime:
    """The start of local day `d`, as an aware datetime."""
    return datetime.combine(d, time()).astimezone()


def _facts(
    index: Index, table: PriceTable, since: datetime, until: datetime, cache: FactsCache | None
) -> Facts:
    if cache is None:
        return collect(index, table, since, until)
    key = (since, until)
    if key not in cache:
        cache[key] = collect(index, table, since, until)
    return cache[key]


def _no_data(mark: ChangeMark, measure: str, reason: str) -> ChangeResult:
    return ChangeResult(
        mark=mark,
        measure=measure,
        before=None,
        after=None,
        before_range=None,
        weeks=(),
        requests_per_week_before=0.0,
        requests_per_week_after=0.0,
        measured_usd_per_week=None,
        verdict=Verdict.NO_DATA,
        verdict_due=None,
        reason=reason,
    )


def _weeks(
    applied: date, now: datetime, logs_start: datetime | None
) -> list[tuple[date, date, bool, bool]]:
    """(start, end exclusive, after, full) per week; ends clipped to `now`'s day."""
    out: list[tuple[date, date, bool, bool]] = []
    first_day = logs_start.astimezone().date() if logs_start else applied
    for k in range(BEFORE_WEEKS, 0, -1):
        start, end = applied - timedelta(days=7 * k), applied - timedelta(days=7 * (k - 1))
        if end <= first_day:
            continue
        out.append((start, end, False, start >= first_day))
    today = now.astimezone().date()
    start = applied + timedelta(days=1)
    while start <= today:
        end = start + timedelta(days=7)
        out.append((start, end, True, end <= today))
        start = end
    return out


def _fmt(m: Measure, v: float | None) -> str:
    return "-" if v is None else m.fmt.format(v)


def measure(
    index: Index,
    table: PriceTable,
    mark: ChangeMark,
    now: datetime,
    *,
    cache: FactsCache | None = None,
) -> ChangeResult:
    """`mark`'s finding measured before and after its date (module docstring).
    `cache` keeps the weekly facts for other marks measured in the same run."""
    m = MEASURES.get(mark.kind)
    if m is None:
        return _no_data(
            mark,
            "(no measure)",
            f"capcom has no measure for {mark.kind} yet, so it cannot tell whether this "
            "change worked.",
        )
    first = next(iter(index.requests()), None)
    logs_start = first.timestamp if first else None
    points: list[MeasurePoint] = []
    before, after = Tally(), Tally()
    full_values: list[float] = []
    full_requests: list[int] = []
    any_values: list[float] = []
    after_from = local_midnight(mark.applied_on + timedelta(days=1))
    for start, end, is_after, full in _weeks(mark.applied_on, now, logs_start):
        since = local_midnight(start)
        until = min(local_midnight(end), now)
        if since >= until:
            continue
        facts = _facts(index, table, since, until, cache)
        t = m.tally(facts, mark.scope, index)
        value = m.value(t)
        points.append(MeasurePoint(start, value if t.requests else None, t.requests, is_after))
        (after if is_after else before).merge(t)
        if not is_after and value is not None and t.requests:
            any_values.append(value)
            if full:
                full_values.append(value)
                full_requests.append(t.requests)
    before_start = local_midnight(mark.applied_on - timedelta(days=7 * BEFORE_WEEKS))
    if logs_start is not None:  # clipped to the logs' first day
        before_start = max(before_start, local_midnight(logs_start.astimezone().date()))
    before_days = max((local_midnight(mark.applied_on) - before_start) / timedelta(days=1), 0.0)
    after_days = max((now - after_from) / timedelta(days=1), 0.0)
    before_weeks = before_days / 7
    after_weeks = after_days / 7
    b, a = m.value(before), m.value(after)
    rpw_b = before.requests / before_weeks if before_weeks else 0.0
    rpw_a = after.requests / after_weeks if after_weeks else 0.0
    # a full week with a fraction of the usual workload swings on a few requests:
    # charted, but left out of the range
    usual = median(full_requests) if full_requests else 0.0
    steady = [v for v, n in zip(full_values, full_requests, strict=True) if n >= MIN_SHARE * usual]
    values = steady or full_values or any_values
    rng = (min(values), max(values)) if values else None
    evidence = tuple(
        (label, f(before, before_weeks), f(after, after_weeks) if after.requests else "-")
        for label, f in m.rows
    )
    quality = tuple(
        (label, f(before, before_weeks), f(after, after_weeks) if after.requests else "-")
        for label, f in m.quality
        if label != ERRORS_ROW or before.sums.get("flagged") or after.sums.get("flagged")
    )
    base = ChangeResult(
        mark=mark,
        measure=m.label,
        before=b,
        after=a,
        before_range=rng,
        weeks=tuple(points),
        requests_per_week_before=rpw_b,
        requests_per_week_after=rpw_a,
        measured_usd_per_week=None,
        verdict=Verdict.NO_DATA,
        verdict_due=None,
        reason="",
        evidence=evidence,
        quality=quality,
    )
    since_text = f"since {mark.applied_on + timedelta(days=1)}"
    if b is None or rng is None:
        return replace(
            base,
            reason=(
                f"Nothing to compare: no requests in {mark.scope} for this measure in the "
                f"{BEFORE_WEEKS} weeks before {mark.applied_on}."
            ),
        )
    usual = usual or rpw_b
    needed = max(MIN_REQUESTS, math.ceil(MIN_SHARE * usual))
    if after_days < MIN_DAYS or after.requests < needed:
        due: date | None = mark.applied_on + timedelta(days=1 + MIN_DAYS)
        if after.requests < needed:
            pace = after.requests / after_days if after_days and after.requests else 0.0
            if pace:
                days = math.ceil((needed - after.requests) / pace + after_days)
                due = max(due, mark.applied_on + timedelta(days=1 + days)) if due else None
            elif after_days >= MIN_DAYS:
                due = None  # no requests in scope since: no pace to project
        days_text = f"{after_days:.0f} day(s)" if after_days >= 1 else "less than a day"
        return replace(
            base,
            verdict=Verdict.TOO_EARLY,
            verdict_due=due,
            reason=(
                f"Too early: {days_text} and {after.requests:,} request(s) in scope "
                f"{since_text}; a verdict needs {MIN_DAYS} days and {needed:,} requests"
                + (f", expected by {due}." if due else ".")
            ),
        )
    if a is None:
        return replace(
            base,
            reason=f"Nothing to measure {since_text}: none of what this measure counts ran.",
        )
    lo, hi = rng
    if a < lo:
        verdict = Verdict.WORKING
    elif a > hi:
        verdict = Verdict.WORSE
    else:
        verdict = Verdict.NO_EFFECT
    # what one counted unit was worth before; after, when none was counted before
    priced = before if before.count else after
    unit = priced.usd / priced.count if priced.count else 0.0
    base_pw = after.base / after_weeks if after_weeks else 0.0
    saving = (b - a) / m.scale * base_pw * unit if verdict is not Verdict.NO_EFFECT else None
    span = f"{_fmt(m, lo)} to {_fmt(m, hi)}" if lo != hi else _fmt(m, lo)
    where = {
        Verdict.WORKING: "below",
        Verdict.WORSE: "above",
        Verdict.NO_EFFECT: "inside",
    }[verdict]
    move = "fell" if a < b else "rose" if a > b else "stayed"
    reason = (
        f"{m.label} {move} from {_fmt(m, b)} to {_fmt(m, a)} {since_text}, {where} the "
        f"before weeks' range ({span}), over {after.requests:,} requests in scope"
    )
    if saving is not None:
        reason += f"; about ${abs(saving):,.2f}/week {'saved' if saving >= 0 else 'more spent'}."
    else:
        reason += ": no clear effect."
    return replace(base, measured_usd_per_week=saving, verdict=verdict, reason=reason)
