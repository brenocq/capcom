"""One usage window on its own: the account's segment that starts at a given time,
with the account's fit of that window and the test of which requests are the
account's, for the window page (`/limits/window/...`) to read what spent it."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from urllib.parse import quote

from capcom.cost.top import project_label
from capcom.limits.accounts import Account, Known, Owners, accounts_of, attribute
from capcom.limits.model import (
    CATEGORIES,
    Fit,
    Segment,
    Spend,
    WindowView,
    prepare,
    scan_groups,
)
from capcom.types import Index, LimitSample, LimitWindow, PriceTable, Request

SLUG = {LimitWindow.FIVE_HOUR: "5-hour", LimitWindow.SEVEN_DAY: "weekly"}
START_FORMAT = "%Y-%m-%dT%H%M"  # a window's start in its URL, local time


def window_path(account: str, seg: Segment, tz: tzinfo | None) -> str:
    """The page of a window: /limits/window/<account id>/<5-hour|weekly>/<start>."""
    start = seg.start.astimezone(tz).strftime(START_FORMAT)
    return f"/limits/window/{quote(account, safe='')}/{SLUG[seg.window]}/{start}"


def window_of(slug: str) -> LimitWindow | None:
    for w, s in SLUG.items():
        if slug in (s, w.value):
            return w
    return None


@dataclass(slots=True)
class Detail:
    account: Account
    several: bool  # more than one account recorded
    view: WindowView  # the account's whole window (its segments and fit)
    segment: Segment
    before: Segment | None  # the window before and after it, for the page's arrows
    after: Segment | None
    keep: Callable[[Request], bool] | None  # the account's requests (None: all)
    inferred: Callable[[Request], bool] | None  # put on the account by time only

    @property
    def fit(self) -> Fit | None:
        return self.view.fit


def _match(segs: Sequence[Segment], start: datetime) -> int | None:
    """The segment starting in the same minute as `start`, else the one holding it."""
    minute = start.replace(second=0, microsecond=0)
    for i, seg in enumerate(segs):
        if abs(seg.start.replace(second=0, microsecond=0) - minute) < timedelta(minutes=1):
            return i
    for i, seg in enumerate(segs):
        if seg.start <= start < seg.end:
            return i
    return None


def find_window(
    index: Index,
    table: PriceTable,
    samples: Sequence[LimitSample],
    known: Known,
    now: datetime,
    account_id: str,
    window: LimitWindow,
    start: datetime,
    label: Callable[[str], str] = project_label,
) -> Detail | None:
    """The window of `account_id` (its id) starting at `start`, or None when the
    account or such a window is not in the samples."""
    tagged = attribute(samples, known)
    accounts = accounts_of(tagged, known)
    acct = next((a for a in accounts if a.id == account_id), None)
    if acct is None:
        return None
    owners = Owners(tagged, known) if len(accounts) > 1 else None
    keep: Callable[[Request], bool] | None = None
    inferred: Callable[[Request], bool] | None = None
    if owners is not None:
        o, aid = owners, acct.id
        keep = lambda r: o(r)[0] == aid  # noqa: E731
        inferred = lambda r: o(r)[1]  # noqa: E731
    own = [s for s in tagged if s.account == acct.id]
    prep = prepare(own, window, now, label, shown=1, keep=keep, inferred=inferred)
    scan_groups(index, table, [prep.group])
    view = prep.finish()
    i = _match(view.segments, start)
    if i is None:
        return None
    segs = view.segments
    return Detail(
        account=acct,
        several=len(accounts) > 1,
        view=view,
        segment=segs[i],
        before=segs[i - 1] if i > 0 else None,
        after=segs[i + 1] if i + 1 < len(segs) else None,
        keep=keep,
        inferred=inferred,
    )


def by_category(f: Fit, spend: Spend) -> dict[str, float]:
    """The fitted percent of `spend` by token category (the merged kinds' share by
    their dollars; a per-dollar fit by each category's dollars)."""
    out = dict.fromkeys(CATEGORIES, 0.0)
    if f.method == "per_type":
        for (m, c), n in spend.tokens.items():
            out[c] += f.weights.get((m, c), 0.0) * n / 1e6
        if f.merged_per_usd:
            for (m, c), usd in spend.usd_by.items():
                if (m, c) not in f.weights:
                    out[c] += f.merged_per_usd * usd
    else:
        for (_, c), usd in spend.usd_by.items():
            out[c] += f.per_usd * usd
    return out


def own_rate(seg: Segment, spend_until: Callable[[datetime, datetime], float]) -> float | None:
    """The window's own dollars per 1 %: what was spent from its start (or its first
    reading, when its start is not known) to its last reading, over the rise between
    them; None without a rise of at least a point or any spend. `spend_until(a, b)`
    is the account's spend between two times."""
    pts = seg.points()
    if len(pts) < 2:
        return None
    (t0, p0), (t1, p1) = pts[0], pts[-1]
    rise = p1 - p0
    usd = spend_until(t0, t1)
    if rise < 1 or usd <= 0:
        return None
    return usd / rise
