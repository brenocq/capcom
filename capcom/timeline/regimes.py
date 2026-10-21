"""What changed: days where the daily spend moved to a new level, and the sessions
and agent types behind the rise.

One statistic throughout, the mean daily spend: `before` over the BEFORE_DAYS days
before a day D, `after` over up to AFTER_DAYS days from D (at least MIN_AFTER_DAYS,
fewer at the end of the window). D is a regime change when `after` is at least
RATIO times the floored before level, max(before, MIN_RISE_USD), and most of the
after days are each that high too (one spiky day is not a new level, and a step
on a Friday still shows across a quiet weekend). The before window needs at least
MIN_ACTIVE_BEFORE days with spend: a level shift needs a level to shift from.

Around a step several days in a row pass that test: each run of passing days gives
one change, dated on the first day of the run that itself spent RATIO times the
floored before level (else the run's first day).

The rise (after minus before) is split by session and by agent type ("main" for
main threads), each member's after mean minus its before mean over the same days:
the top members, then a REST entry with what is left, so the entries add up to the
rise. Shares are of the rise.

Days are whole calendar days in `tz` (the local zone by default) inside
[since, until): a partial first or last day is left out. Requests are read once,
streamed; memory is one float per (day, member) with spend.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path

from capcom.cost.model import price
from capcom.cost.prices import is_placeholder
from capcom.timeline.groups import Grouper, ProjectNames
from capcom.types import Index, PriceTable

BEFORE_DAYS = 7
AFTER_DAYS = 7
MIN_AFTER_DAYS = 3
MIN_ACTIVE_BEFORE = 3  # days with spend in the before window
RATIO = 3.0
MIN_RISE_USD = 5.0  # a day: smaller shifts are noise, whatever their ratio
TOP = 3
REST = "(rest)"


@dataclass(frozen=True, slots=True)
class Contributor:
    """A session or an agent type behind a rise."""

    key: str  # session id, or agent type
    label: str  # session title (or id prefix), or agent type
    rise_usd: float  # mean daily spend after minus before
    share: float  # of the change's rise
    project: str = ""  # sessions: the project's display name


@dataclass(frozen=True, slots=True)
class RegimeChange:
    day: date
    before_usd: float  # mean daily spend of the BEFORE_DAYS days before `day`
    after_usd: float  # mean daily spend of the `after_days` days from `day`
    after_days: int
    sessions: tuple[Contributor, ...]  # the top sessions, then REST: they add up to the rise
    agent_types: tuple[Contributor, ...]  # the same by agent type

    @property
    def rise_usd(self) -> float:
        return self.after_usd - self.before_usd

    @property
    def ratio(self) -> float:
        return self.after_usd / self.before_usd if self.before_usd > 0 else float("inf")


def _midnight(d: date, tz: tzinfo | None) -> datetime:
    return (
        datetime.combine(d, time(), tzinfo=tz) if tz else datetime.combine(d, time()).astimezone()
    )


def _days(since: datetime, until: datetime, tz: tzinfo | None) -> list[date]:
    """The whole calendar days in [since, until)."""
    d = since.astimezone(tz).date()
    if _midnight(d, tz) < since:
        d += timedelta(days=1)
    out = []
    while _midnight(d + timedelta(days=1), tz) <= until:
        out.append(d)
        d += timedelta(days=1)
    return out


def _mean(row: dict[date, float], days: list[date]) -> float:
    return sum(row.get(d, 0.0) for d in days) / len(days)


def _top(
    by_day: dict[str, dict[date, float]],
    before: list[date],
    after: list[date],
    labels: dict[str, str],
    projects: dict[str, str],
    top: int,
    rise: float,
) -> tuple[Contributor, ...]:
    rises = {key: _mean(row, after) - _mean(row, before) for key, row in by_day.items()}
    order = sorted((k for k in rises if rises[k] > 0), key=lambda k: (-rises[k], k))[:top]
    out = [
        Contributor(k, labels.get(k, k), rises[k], rises[k] / rise, projects.get(k, ""))
        for k in order
    ]
    rest = rise - sum(c.rise_usd for c in out)
    if abs(rest) > 1e-9:
        out.append(Contributor(REST, "Other", rest, rest / rise))
    return tuple(out)


def _passes(daily: list[float], k: int, ratio: float, min_rise: float) -> tuple[bool, bool, float]:
    """Whether day k starts a new level, whether its before week has a level to shift
    from (MIN_ACTIVE_BEFORE days with spend), and the floored before level."""
    before = daily[k - BEFORE_DAYS : k]
    after = daily[k : k + AFTER_DAYS]
    floor = max(sum(before) / len(before), min_rise)
    active = sum(1 for x in before if x > 0) >= MIN_ACTIVE_BEFORE
    if len(after) < MIN_AFTER_DAYS:
        return False, active, floor
    high = sum(1 for x in after if x >= ratio * floor)
    return sum(after) / len(after) >= ratio * floor and 2 * high > len(after), active, floor


def find_changes(
    daily: list[float], ratio: float = RATIO, min_rise: float = MIN_RISE_USD
) -> list[tuple[int, float, float, int]]:
    """(day position, before mean, after mean, after days) of each level shift in
    `daily` (module doc). A run whose day has no level to shift from (too few active
    days before it) is skipped whole: its later days would compare the rise with
    itself."""
    out = []
    k = BEFORE_DAYS
    while k + MIN_AFTER_DAYS <= len(daily):
        if not _passes(daily, k, ratio, min_rise)[0]:
            k += 1
            continue
        run = [k]
        while (
            run[-1] + 1 + MIN_AFTER_DAYS <= len(daily)
            and _passes(daily, run[-1] + 1, ratio, min_rise)[0]
        ):
            run.append(run[-1] + 1)
        pick = next(
            (j for j in run if daily[j] >= ratio * _passes(daily, j, ratio, min_rise)[2]), run[0]
        )
        k = run[-1] + 1
        if not _passes(daily, pick, ratio, min_rise)[1]:
            continue
        before, after = daily[pick - BEFORE_DAYS : pick], daily[pick : pick + AFTER_DAYS]
        out.append((pick, sum(before) / len(before), sum(after) / len(after), len(after)))
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
    oldest first. A change needs BEFORE_DAYS whole days of the window before it and
    MIN_AFTER_DAYS from it, so pass a window that starts a week before the days of
    interest."""
    days = _days(since, until, tz)
    if len(days) < BEFORE_DAYS + MIN_AFTER_DAYS:
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
    for k, b, a, n in found:
        before, after = days[k - BEFORE_DAYS : k], days[k : k + n]
        out.append(
            RegimeChange(
                days[k],
                b,
                a,
                n,
                _top(by_session, before, after, titles, projects, top, a - b),
                _top(by_agent, before, after, {}, {}, top, a - b),
            )
        )
    return out
