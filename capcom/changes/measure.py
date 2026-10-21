"""A mark measured on the logs (design.md, "Did it work?").

The measure is taken per local day: one pass of the checks' facts over each day
(capcom.checks.facts), so it counts what the finding counted. The before window is
the 4 weeks ending at the start of the applied day (clipped to where the logs
begin), the after window runs from the start of the next day to `now`; the applied
day itself, when the change was being made, is in neither. Days are added up into
weeks starting on the applied weekday (the date is a boundary) for the chart and
the before range shown.

Verdict, on rates only, so a busier or quieter day does not count:

- no data ("can't tell"): the kind has no measure; nothing in scope before the
  date; fewer than 2 before weeks with MIN_REQUESTS requests in scope; the
  measure's habit (e.g. subagent tool calls for tool waits) near zero before and
  present after (that work only started after the date); or the requests in scope
  per week changed 4-fold either way (the workload is not comparable);
- too early: under 7 days since the date, or fewer requests in scope than
  max(200, 20 % of a usual before week's);
- working / worse: the after days' requests-weighted rate below / above the before
  days', with both a permutation test over days at 95 % (2,000 seeded shuffles
  of the days between before and after, two-sided) and a difference larger than
  the before days' typical swing (their weighted mean absolute deviation from the
  before rate); otherwise no clear effect. A clear verdict is "can't tell" when
  the first and second half of the before days differ by the same test: the
  measure was already moving before the date.

Measured saving per week = (before - after) / scale x the after window's base per
week (requests, or runs) x the unit value (measures.py: what one counted unit was
worth in the before window, or in the after one when the before window counted
none). Given for working and worse (negative), not for no clear effect.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from random import Random
from statistics import median

from capcom.changes.measures import ERRORS_ROW, MEASURES, Measure, Tally
from capcom.checks.facts import Facts, collect
from capcom.types import ChangeMark, ChangeResult, Index, MeasurePoint, PriceTable, Verdict

BEFORE_WEEKS = 4
MIN_DAYS = 7
PERMUTATIONS = 2_000
ALPHA = 0.05  # the permutation test's level: 95 %
HABIT_NEAR_ZERO = 0.1  # the habit before, per week, under this share of after's
WORKLOAD_FOLD = 4.0  # requests in scope per week changed this much: not comparable
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


def human_day(d: date, today: date | None = None) -> str:
    """ "Oct 19"; with the year when it is not `today`'s year."""
    text = f"{d:%b} {d.day}"
    return text if today is None or d.year == today.year else f"{text}, {d.year}"


Day = tuple[float, float]  # (the day's value, its weight: the measure's base)


def _day_points(m: Measure, tallies: Sequence[Tally]) -> list[Day]:
    out: list[Day] = []
    for t in tallies:
        v = m.value(t)
        w = float(len(t.samples)) if m.median else t.base
        if v is not None and w > 0:
            out.append((v, w))
    return out


def _wmean(days: Sequence[Day]) -> float:
    total = sum(w for _, w in days)
    return sum(v * w for v, w in days) / total if total else 0.0


def swing(days: Sequence[Day]) -> float:
    """The days' typical swing: their weighted mean absolute deviation."""
    mean = _wmean(days)
    total = sum(w for _, w in days)
    return sum(abs(v - mean) * w for v, w in days) / total if total else 0.0


def p_value(before: Sequence[Day], after: Sequence[Day], seed: int) -> float:
    """Two-sided permutation test of the difference in weighted means: the share of
    PERMUTATIONS shuffles of the days that differ at least as much."""
    observed = abs(_wmean(after) - _wmean(before))
    pooled = [*before, *after]
    k = len(after)
    rng = Random(seed)
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pooled)
        if abs(_wmean(pooled[:k]) - _wmean(pooled[k:])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (PERMUTATIONS + 1)


def measure_name(kind: str) -> str | None:
    """What a mark of finding `kind` is measured by, or None without a measure."""
    m = MEASURES.get(kind)
    return m.label if m else None


def _tally(
    m: Measure,
    index: Index,
    table: PriceTable,
    scope: str,
    since: datetime,
    until: datetime,
    cache: FactsCache | None,
) -> Tally:
    return m.tally(_facts(index, table, since, until, cache), scope, index)


def measure(
    index: Index,
    table: PriceTable,
    mark: ChangeMark,
    now: datetime,
    *,
    cache: FactsCache | None = None,
) -> ChangeResult:
    """`mark`'s finding measured before and after its date (module docstring).
    `cache` keeps the daily facts for other marks measured in the same run."""
    m = MEASURES.get(mark.kind)
    if m is None:
        return _no_data(
            mark,
            "(no measure)",
            f"Can't tell: capcom has no measure for {mark.kind} yet.",
        )
    today = now.astimezone().date()
    first = next(iter(index.requests()), None)
    logs_start = first.timestamp if first else None
    first_day = logs_start.astimezone().date() if logs_start else mark.applied_on
    points: list[MeasurePoint] = []
    before, after = Tally(), Tally()
    days_before: list[Tally] = []
    days_after: list[Tally] = []
    full_values: list[float] = []
    full_requests: list[int] = []
    week_requests: list[int] = []
    any_values: list[float] = []
    for start, end, is_after, full in _weeks(mark.applied_on, now, logs_start):
        week = Tally()
        d = max(start, first_day)
        while d < end:
            since, until = local_midnight(d), min(local_midnight(d + timedelta(days=1)), now)
            d += timedelta(days=1)
            if since >= until:
                continue
            t = _tally(m, index, table, mark.scope, since, until, cache)
            (days_after if is_after else days_before).append(t)
            week.merge(t)
        value = m.value(week)
        points.append(
            MeasurePoint(start, value if week.requests else None, week.requests, is_after)
        )
        (after if is_after else before).merge(week)
        if not is_after:
            week_requests.append(week.requests)
            if value is not None and week.requests:
                any_values.append(value)
                if full:
                    full_values.append(value)
                    full_requests.append(week.requests)
    before_start = local_midnight(mark.applied_on - timedelta(days=7 * BEFORE_WEEKS))
    if logs_start is not None:  # clipped to the logs' first day
        before_start = max(before_start, local_midnight(logs_start.astimezone().date()))
    after_from = local_midnight(mark.applied_on + timedelta(days=1))
    before_days = max((local_midnight(mark.applied_on) - before_start) / timedelta(days=1), 0.0)
    after_days = max((now - after_from) / timedelta(days=1), 0.0)
    before_weeks = before_days / 7
    after_weeks = after_days / 7
    b, a = m.value(before), m.value(after)
    rpw_b = before.requests / before_weeks if before_weeks else 0.0
    rpw_a = after.requests / after_weeks if after_weeks else 0.0
    # a full week with a fraction of the usual workload swings on a few requests:
    # charted, but left out of the range shown
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
    on = human_day(mark.applied_on, today)
    since_text = f"since {human_day(mark.applied_on + timedelta(days=1), today)}"
    if b is None or rng is None:
        return replace(
            base,
            reason=(
                f"Can't tell: nothing in scope to measure in the {BEFORE_WEEKS} weeks before {on}."
            ),
        )
    enough = sum(1 for n in week_requests if n >= MIN_REQUESTS)
    if enough < 2:
        return replace(
            base,
            reason=(
                f"Can't tell: only {enough} of the weeks before {on} had {MIN_REQUESTS} "
                "requests in scope or more, too little to know what was usual before."
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
                + (f", expected by {human_day(due, today)}." if due else ".")
            ),
        )
    if a is None:
        return replace(
            base,
            reason=f"Can't tell: nothing this measure counts ran {since_text}.",
        )
    habit_b = before.sums.get("habit", before.requests) / before_weeks if before_weeks else 0.0
    habit_a = after.sums.get("habit", after.requests) / after_weeks if after_weeks else 0.0
    if habit_a > 0 and habit_b < HABIT_NEAR_ZERO * habit_a:
        return replace(
            base,
            reason=(
                f"Can't tell: this kind of work ({m.habit}) only started after the date "
                f"({habit_b:,.0f} a week before, {habit_a:,.0f} {since_text}), so there's "
                "nothing to compare."
            ),
        )
    if rpw_b and not 1 / WORKLOAD_FOLD < rpw_a / rpw_b < WORKLOAD_FOLD:
        return replace(
            base,
            reason=(
                f"Can't tell: the work in scope changed too much to compare ({rpw_b:,.0f} "
                f"requests a week before, {rpw_a:,.0f} {since_text})."
            ),
        )
    pts_b, pts_a = _day_points(m, days_before), _day_points(m, days_after)
    typical = swing(pts_b)
    seed = zlib.crc32(f"{mark.kind}@{mark.scope}@{mark.applied_on}".encode())
    p = p_value(pts_b, pts_a, seed) if pts_b and pts_a else 1.0
    clear = p < ALPHA and abs(a - b) > typical
    if clear:
        # the same test between the first and second half of the before days: a
        # measure already moving before the date can't be told apart from a change
        half = len(pts_b) // 2
        early, late = pts_b[:half], pts_b[half:]
        moving = early and late and abs(_wmean(late) - _wmean(early)) > typical
        if moving and p_value(early, late, seed) < ALPHA:
            return replace(
                base,
                reason=(
                    f"Can't tell: {m.label[0].lower()}{m.label[1:]} was already moving "
                    f"before the date ({_fmt(m, _wmean(early))} in the first half of the "
                    f"before days, {_fmt(m, _wmean(late))} in the second), so a change "
                    "since can't be told apart from that."
                ),
            )
    verdict = Verdict.NO_EFFECT if not clear else Verdict.WORKING if a < b else Verdict.WORSE
    # what one counted unit was worth before; after, when none was counted before
    priced = before if before.count else after
    unit = priced.usd / priced.count if priced.count else 0.0
    base_pw = after.base / after_weeks if after_weeks else 0.0
    saving = (b - a) / m.scale * base_pw * unit if verdict is not Verdict.NO_EFFECT else None
    move = "fell" if a < b else "rose" if a > b else "stayed"
    reason = (
        f"{m.label} {move} from {_fmt(m, b)} to {_fmt(m, a)} {since_text}, over "
        f"{after.requests:,} requests in scope; the before days swung by about "
        f"{_fmt(m, typical)}"
    )
    if saving is not None:
        reason += (
            f", and chance alone would rarely move it this much (p = {p:.3f}): about "
            f"${abs(saving):,.2f}/week {'saved' if saving >= 0 else 'more spent'}."
        )
    elif abs(a - b) <= typical:
        reason += ", more than the difference: no clear effect."
    else:
        reason += f", and chance alone could move it this much (p = {p:.2f}): no clear effect."
    return replace(base, measured_usd_per_week=saving, verdict=verdict, reason=reason)
