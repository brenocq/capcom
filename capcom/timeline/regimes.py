"""What changed: days where the daily spend moved to a new level, and the sessions
and agent types behind the rise.

A day D is a regime change when the median spend of the AFTER_DAYS days from D is at
least RATIO times the median of the BEFORE_DAYS days before it, and higher by at
least MIN_RISE_USD a day. Around a step, several days in a row pass that test (the
day before the step too, since two of its next three days are high): each run of
passing days gives one change, dated on the first day of the run that itself spent
RATIO times the before level (else the run's first day), and the next change
can only start after the run.

The rise is split by session and by agent type ("main" for main threads): each
member's mean daily spend over the after days minus its mean over the before days.
The top members by rise are kept with their share of the members' positive rises.

Days are calendar days in `tz` (the local zone by default). Requests are read once,
streamed; memory is one float per (day, member) with spend.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from capcom.cost.model import price
from capcom.cost.prices import is_placeholder
from capcom.timeline.groups import Grouper, ProjectNames
from capcom.types import Index, PriceTable

BEFORE_DAYS = 7
AFTER_DAYS = 3
RATIO = 3.0
MIN_RISE_USD = 5.0  # a day: smaller shifts are noise, whatever their ratio
TOP = 3


@dataclass(frozen=True, slots=True)
class Contributor:
    """A session or an agent type behind a rise."""

    key: str  # session id, or agent type
    label: str  # session title (or id prefix), or agent type
    rise_usd: float  # mean daily spend after minus before
    share: float  # of the members' positive rises
    project: str = ""  # sessions: the project's display name


@dataclass(frozen=True, slots=True)
class RegimeChange:
    day: date
    before_usd: float  # median daily spend of the BEFORE_DAYS days before `day`
    after_usd: float  # median daily spend of the AFTER_DAYS days from `day`
    sessions: tuple[Contributor, ...]
    agent_types: tuple[Contributor, ...]

    @property
    def ratio(self) -> float:
        return self.after_usd / self.before_usd if self.before_usd > 0 else float("inf")


def _days(since: datetime, until: datetime, tz: tzinfo | None) -> list[date]:
    first, last = since.astimezone(tz).date(), (until - timedelta(microseconds=1)).astimezone(tz)
    n = (last.date() - first).days + 1
    return [first + timedelta(days=k) for k in range(max(0, n))]


def _top(
    by_day: dict[str, dict[date, float]],
    before: list[date],
    after: list[date],
    labels: dict[str, str],
    projects: dict[str, str],
    top: int,
) -> tuple[Contributor, ...]:
    rises: dict[str, float] = {}
    for key, row in by_day.items():
        a = sum(row.get(d, 0.0) for d in after) / len(after)
        b = sum(row.get(d, 0.0) for d in before) / len(before)
        if a - b > 0:
            rises[key] = a - b
    total = sum(rises.values())
    order = sorted(rises, key=lambda k: (-rises[k], k))[:top]
    return tuple(
        Contributor(k, labels.get(k, k), rises[k], rises[k] / total, projects.get(k, ""))
        for k in order
    )


def find_changes(
    daily: list[float],
    before_days: int = BEFORE_DAYS,
    after_days: int = AFTER_DAYS,
    ratio: float = RATIO,
    min_rise: float = MIN_RISE_USD,
) -> list[tuple[int, float, float]]:
    """(day position, before median, after median) of each level shift in `daily`."""
    out = []
    k = before_days
    while k + after_days <= len(daily):
        b = statistics.median(daily[k - before_days : k])
        a = statistics.median(daily[k : k + after_days])
        if not (a >= ratio * b and a - b >= min_rise):
            k += 1
            continue
        run = [k]
        while run[-1] + 1 + after_days <= len(daily):
            j = run[-1] + 1
            bj = statistics.median(daily[j - before_days : j])
            aj = statistics.median(daily[j : j + after_days])
            if not (aj >= ratio * bj and aj - bj >= min_rise):
                break
            run.append(j)
        pick = next(
            (
                j
                for j in run
                if daily[j] >= ratio * statistics.median(daily[j - before_days : j])
                and daily[j] - statistics.median(daily[j - before_days : j]) >= min_rise
            ),
            run[0],
        )
        b = statistics.median(daily[pick - before_days : pick])
        a = statistics.median(daily[pick : pick + after_days])
        out.append((pick, b, a))
        k = run[-1] + 1
    return out


def regime_changes(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    tz: tzinfo | None = None,
    openclaw_root: Path | None = None,
    top: int = TOP,
) -> list[RegimeChange]:
    """The days in [since, until) where the daily spend's level rose (module doc),
    oldest first. A change needs BEFORE_DAYS days of the window before it and
    AFTER_DAYS from it, so pass a window that starts a week before the days of
    interest."""
    days = _days(since, until, tz)
    if len(days) < BEFORE_DAYS + AFTER_DAYS:
        return []
    grouper = Grouper.from_index(index, table, openclaw_root)
    daily: dict[date, float] = defaultdict(float)
    by_session: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    by_agent: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    project_of: dict[str, str] = {}
    for r in index.requests(since=since, until=until):
        if is_placeholder(r.model):
            continue
        usd = price(r, table).total
        if usd == 0:
            continue
        d = r.timestamp.astimezone(tz).date()
        daily[d] += usd
        by_session[r.session_id][d] += usd
        by_agent[grouper.agent(r.session_id, r.agent_id)[0]][d] += usd
        project_of.setdefault(r.session_id, r.project_dir)
    found = find_changes([daily.get(d, 0.0) for d in days])
    if not found:
        return []
    names = ProjectNames.build(list(index.sessions()), openclaw_root)
    projects = {sid: names.label(p) for sid, p in project_of.items()}
    titles = {sid: grouper.session(sid)[1] for sid in by_session}
    out = []
    for k, b, a in found:
        before, after = days[k - BEFORE_DAYS : k], days[k : k + AFTER_DAYS]
        out.append(
            RegimeChange(
                days[k],
                b,
                a,
                _top(by_session, before, after, titles, projects, top),
                _top(by_agent, before, after, {}, {}, top),
            )
        )
    return out
