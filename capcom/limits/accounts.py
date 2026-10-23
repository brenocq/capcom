"""The usage windows per account, for a user who switches Claude Code between accounts.

Each account has its own 5-hour and weekly windows, so its samples are cut into
segments, fitted and forecast on their own. Samples recorded by this version carry
their account (`LimitSample.account`, the account's id); older ones are attributed by
their weekly reset time, which falls at a fixed time of the week for each account,
and, for the 5-hour ones, by the session that reported them; what nothing tells
apart is "unknown". With a single account (or none known) everything is that one
account's, as before accounts were recorded.

Requests reach an account through their session: the sessions the samples and the
recorder's session log (`limits.sessions.jsonl`) put on each account, at the time of
each entry (a session can switch with `/login`). The spend of a session no sample
names goes, by time, to the account Claude Code was logged in with then (the
recorder logs each switch), or else to the account of the latest reading; that spend
is flagged as inferred.

The combined capacity: before each account's weekly limit, (100 - percent now) times
the account's own fitted dollars per 1 %, their sum, and when all of it is spent at
the pace of the last 24 hours (all accounts' requests).
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from capcom.cost.top import project_label
from capcom.limits.model import (
    BURST,
    LENGTH,
    SAME_RESET,
    Bins,
    Group,
    Prepared,
    Spend,
    WindowView,
    _dt,
    agent_types,
    prepare,
    scan_groups,
    scan_spend,
)
from capcom.limits.record import ACCOUNTS_FILE, SESSIONS_FILE, same_cycle
from capcom.types import Index, LimitSample, LimitWindow, PriceTable, Request

UNKNOWN = "unknown"
PACE = timedelta(hours=24)  # the combined capacity's pace: spend over this long
SLACK_S = 600.0  # weekly reset times closer than this are one cycle


@dataclass(frozen=True, slots=True)
class Account:
    id: str  # oauthAccount.accountUuid, or UNKNOWN (samples from before accounts were
    # recorded, on no known account's weekly cycle)
    email: str | None
    current: bool  # the account Claude Code was last seen logged in with

    @property
    def label(self) -> str:
        if self.email:
            return self.email
        return "earlier account" if self.id == UNKNOWN else f"account {self.id[:8]}"


@dataclass(slots=True)
class Known:
    """What the recorder keeps beside the samples: the current account, each
    account's email, the account each session was seen on, and each switch."""

    current: str | None = None
    emails: dict[str, str] | None = None
    sessions: list[tuple[datetime, str, str]] | None = None  # (time, session id, account)
    switches: list[tuple[datetime, str]] | None = None  # (time, account), in time order


def load_known(state_dir: Path | None) -> Known:
    """The recorder's account files in `state_dir`; missing or bad ones are empty."""
    out = Known(None, {}, [], [])
    if state_dir is None:
        return out
    try:
        with (state_dir / ACCOUNTS_FILE).open(encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            cur, emails = d.get("current"), d.get("emails")
            out.current = cur if isinstance(cur, str) and cur else None
            if isinstance(emails, dict):
                out.emails = {
                    k: v for k, v in emails.items() if isinstance(k, str) and isinstance(v, str)
                }
    except (OSError, ValueError):
        pass
    assert out.sessions is not None and out.switches is not None
    try:
        with (state_dir / SESSIONS_FILE).open("rb") as f:
            for line in f:
                if not line.endswith(b"\n"):
                    break  # a recorder still writing
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                t, acct, sid = _dt(e.get("time")), e.get("account"), e.get("session_id")
                if t is None or not isinstance(acct, str) or not acct:
                    continue
                if isinstance(sid, str) and sid:
                    out.sessions.append((t, sid, acct))
                elif sid is None:
                    out.switches.append((t, acct))
    except OSError:
        pass
    out.sessions.sort()
    out.switches.sort()
    return out


# --------------------------------------------------------------------------- samples


def _phase_match(anchors: Mapping[str, list[float]], resets: datetime) -> list[str]:
    r = resets.timestamp()
    return [a for a, ps in anchors.items() if any(same_cycle(p, r, SLACK_S) for p in ps)]


def attribute(samples: Sequence[LimitSample], known: Known) -> list[LimitSample]:
    """Every sample with its account: its own label; else, for a weekly one, the
    account whose weekly cycle its reset time falls on; else the account of the same
    session (a sample of the same refresh, the session log, or its nearest earlier
    reading); for a 5-hour one its session places only from later on, the one account
    whose readings around it share its reset time; else UNKNOWN. With one account
    known and no sample evidently of another, the unknown ones are that account's."""
    anchors: dict[str, list[float]] = {}
    for s in samples:
        if s.account and s.window == LimitWindow.SEVEN_DAY and s.resets_at is not None:
            ps = anchors.setdefault(s.account, [])
            r = s.resets_at.timestamp()
            if not any(abs(r - p) < SLACK_S for p in ps):
                ps.append(r)
    out: list[LimitSample | None] = []
    other = False  # a weekly sample on no known account's cycle
    for s in samples:
        if s.account:
            out.append(s)
        elif s.window == LimitWindow.SEVEN_DAY and s.resets_at is not None:
            hits = _phase_match(anchors, s.resets_at)
            if len(hits) == 1:
                out.append(replace(s, account=hits[0]))
            else:
                other = other or not hits
                out.append(replace(s, account=UNKNOWN) if not hits else None)
        else:
            out.append(None)
    # the rest by their session: the same refresh, the session log, the nearest reading
    # (one on no known account's cycle too: the session was not on a known one then)
    by_session: dict[str, list[tuple[datetime, str]]] = {}
    for got in out:
        if got is not None and got.session_id and got.account:
            by_session.setdefault(got.session_id, []).append((got.time, got.account))
    for t, sid, logged in known.sessions or ():
        by_session.setdefault(sid, []).append((t, logged))
    for entries in by_session.values():
        entries.sort()
    found: list[tuple[str | None, bool]] = [
        (None, True)
        if got is not None
        else _session_entry(by_session.get(s.session_id or ""), s.time)
        for s, got in zip(samples, out, strict=True)
    ]
    # a 5-hour reading its session cannot place (the session has no entry until later)
    # goes with the account whose readings around it share its reset time: one
    # account's window, not a stray window of one reading on another account
    resets: dict[str, list[tuple[datetime, datetime]]] = {}
    for s, got, (acct, sure) in zip(samples, out, found, strict=True):
        a = got.account if got is not None else acct if sure else None
        if a and s.window == LimitWindow.FIVE_HOUR and s.resets_at is not None:
            resets.setdefault(a, []).append((s.time, s.resets_at))
    done: list[LimitSample] = []
    for s, got, (acct, sure) in zip(samples, out, found, strict=True):
        if got is not None:
            done.append(got)
            continue
        if not sure and s.window == LimitWindow.FIVE_HOUR and s.resets_at is not None:
            acct = _same_reset(resets, s) or acct
        done.append(replace(s, account=acct or UNKNOWN))
    named = {s.account for s in done if s.account != UNKNOWN}
    if len(named) == 1 and not other:
        only = next(iter(named))
        done = [replace(s, account=only) if s.account == UNKNOWN else s for s in done]
    return done


def _session_at(entries: Sequence[tuple[datetime, str]] | None, t: datetime) -> str | None:
    """The account of a session at `t`: its latest entry at or before `t` (a moment
    of slack for one refresh's samples), else its first."""
    return _session_entry(entries, t)[0]


def _session_entry(
    entries: Sequence[tuple[datetime, str]] | None, t: datetime
) -> tuple[str | None, bool]:
    """`_session_at`, and whether an entry at or before `t` says so (not only a later
    one: the session may have been on another account then)."""
    if not entries:
        return None, False
    i = bisect.bisect_right(entries, (t + BURST, "￿")) - 1
    return entries[max(i, 0)][1], i >= 0


def _same_reset(
    resets: Mapping[str, list[tuple[datetime, datetime]]], s: LimitSample
) -> str | None:
    """The one account with a 5-hour reading within a window's length of `s` whose
    reset is the same as `s`'s; None when none or several."""
    assert s.resets_at is not None
    span = LENGTH[LimitWindow.FIVE_HOUR]
    hits = [
        a
        for a, rs in resets.items()
        if any(abs(t - s.time) <= span and abs(r - s.resets_at) < SAME_RESET for t, r in rs)
    ]
    return hits[0] if len(hits) == 1 else None


def accounts_of(samples: Sequence[LimitSample], known: Known) -> list[Account]:
    """The accounts the (attributed) samples hold: the current one first, then by
    their latest reading, newest first; the unknown one last."""
    latest: dict[str, datetime] = {}
    for s in samples:
        a = s.account or UNKNOWN
        if a not in latest or s.time > latest[a]:
            latest[a] = s.time
    current = known.current if known.current in latest else None
    if current is None and latest:
        named = [a for a in latest if a != UNKNOWN]
        current = max(named or list(latest), key=lambda a: latest[a])
    emails = known.emails or {}
    order = sorted(latest, key=lambda a: (a != current, a == UNKNOWN, -latest[a].timestamp()))
    return [Account(a, emails.get(a), a == current) for a in order]


class Owners:
    """Which account a request's session was on at the request's time, and whether
    that is inferred (a session no sample or log entry names: by time)."""

    def __init__(self, samples: Iterable[LimitSample], known: Known) -> None:
        self.sessions: dict[str, list[tuple[datetime, str]]] = {}
        timeline: list[tuple[datetime, str]] = []
        for s in samples:
            if s.account is None:
                continue
            if s.source == "statusline":
                timeline.append((s.time, s.account))
            if s.session_id:
                self.sessions.setdefault(s.session_id, []).append((s.time, s.account))
        for t, sid, acct in known.sessions or ():
            self.sessions.setdefault(sid, []).append((t, acct))
        for entries in self.sessions.values():
            entries.sort()
        self.switches = list(known.switches or ())
        self.timeline = sorted(timeline)
        self._memo: dict[tuple[str, datetime], tuple[str, bool]] = {}
        # the last request asked about: one pass asks once per account for each
        self._last: tuple[Request | None, tuple[str, bool]] = (None, (UNKNOWN, True))

    def at(self, t: datetime) -> str:
        """The account in use at `t`: the latest switch the recorder logged, else the
        latest reading's account, else the first reading's."""
        for seq in (self.switches, self.timeline):
            i = bisect.bisect_right(seq, (t, "￿")) - 1
            if i >= 0:
                return seq[i][1]
        return self.timeline[0][1] if self.timeline else UNKNOWN

    def __call__(self, r: Request) -> tuple[str, bool]:
        if self._last[0] is r:
            return self._last[1]
        key = (r.session_id, r.timestamp)
        got = self._memo.get(key)
        if got is None:
            acct = _session_at(self.sessions.get(r.session_id), r.timestamp)
            got = (acct, False) if acct is not None else (self.at(r.timestamp), True)
            self._memo[key] = got
        self._last = (r, got)
        return got


# --------------------------------------------------------------------------- per account


@dataclass(slots=True)
class AccountView:
    account: Account
    views: dict[LimitWindow, WindowView]
    samples: list[LimitSample]


@dataclass(slots=True)
class Share:
    """An account's part of the combined weekly capacity."""

    account: Account
    percent: float | None  # the weekly percentage now (0 once its window reset)
    usd_per_percent: float | None  # the account's own fit
    usd_left: float | None  # (100 - percent) x usd_per_percent
    resets_at: datetime | None  # its weekly reset, when its window is current


@dataclass(slots=True)
class Combined:
    shares: list[Share]
    usd_left: float | None  # the sum over the accounts with a fit
    pace_usd_per_hour: float  # all accounts' spend over the last PACE
    runs_out_at: datetime | None  # when the sum is spent at that pace
    reset_first: Share | None  # an account whose week resets before then

    @property
    def missing(self) -> list[Account]:
        return [s.account for s in self.shares if s.usd_left is None]


def _share(a: AccountView, now: datetime) -> Share:
    v = a.views[LimitWindow.SEVEN_DAY]
    pct: float | None = None
    resets: datetime | None = None
    if v.forecast is not None:
        pct, resets = v.forecast.now, v.forecast.resets_at
    elif v.reading is not None:
        last = v.segments[-1]
        pct = v.reading.used_percentage if v.current else 0.0
        resets = last.resets_at if v.current else None
    per = v.fit.usd_per_percent if v.fit is not None else None
    pct = None if pct is None else min(pct, 100.0)  # usage stops at the limit
    left = None if pct is None or per is None else (100.0 - pct) * per
    return Share(a.account, pct, per, left, resets)


def combined(
    index: Index,
    table: PriceTable,
    accounts: Sequence[AccountView],
    now: datetime,
    spent: Spend | None = None,
) -> Combined:
    """The combined capacity; `spent`: every account's spend over the last `PACE`,
    when the caller read it already (`pace_bins`)."""
    shares = [_share(a, now) for a in accounts]
    lefts = [s.usd_left for s in shares if s.usd_left is not None]
    total = sum(lefts) if lefts else None
    if spent is None:
        spent = Spend()
        scan_spend(index, table, pace_bins(spent, now))
    pace = spent.usd / (PACE / timedelta(hours=1))
    runs_out = None
    if total is not None and total > 0 and pace > 0:
        runs_out = now + timedelta(hours=total / pace)
    first: Share | None = None
    if runs_out is not None:
        due = [s for s in shares if s.resets_at is not None and now < s.resets_at < runs_out]
        first = min(due, key=lambda s: s.resets_at or now, default=None)
    return Combined(shares, total, pace, runs_out, first)


def analyze_accounts(
    index: Index,
    table: PriceTable,
    samples: Sequence[LimitSample],
    known: Known,
    now: datetime,
    label: Callable[[str], str] = project_label,
) -> tuple[list[AccountView], Combined]:
    """Each account's windows (current account first) and the combined capacity."""
    tagged = attribute(samples, known)
    accounts = accounts_of(tagged, known)
    owners = Owners(tagged, known) if len(accounts) > 1 else None
    # every account's windows and the combined pace in one read of the requests
    preps: list[tuple[Account, list[LimitSample], dict[LimitWindow, Prepared]]] = []
    types = agent_types(index)
    for acct in accounts:
        own = [s for s in tagged if s.account == acct.id]
        keep: Callable[[Request], bool] | None = None
        inferred: Callable[[Request], bool] | None = None
        if owners is not None:
            o, aid = owners, acct.id
            keep = lambda r, o=o, aid=aid: o(r)[0] == aid  # noqa: E731
            inferred = lambda r, o=o: o(r)[1]  # noqa: E731
        prep = {
            w: prepare(own, w, now, label, keep=keep, inferred=inferred, agent_types=types)
            for w in LimitWindow
        }
        preps.append((acct, own, prep))
    spent = Spend()
    # one group per account: its windows' bins take the same requests
    groups: list[Group] = [
        ([b for p in prep.values() for b in p.group[0]], next(iter(prep.values())).group[1])
        for _, _, prep in preps
    ]
    scan_groups(index, table, [*groups, (pace_bins(spent, now), None)])
    out = [
        AccountView(acct, {w: p.finish() for w, p in prep.items()}, own)
        for acct, own, prep in preps
    ]
    return out, combined(index, table, out, now, spent)


def pace_bins(spent: Spend, now: datetime) -> list[Bins]:
    """The bins of the combined pace: all spend over the last `PACE`."""
    return [Bins([(now - PACE, now, spent)])]


def find_account(accounts: Sequence[AccountView], key: str) -> AccountView | None:
    """The account whose email (case-insensitive) or id is `key`."""
    k = key.strip().lower()
    for a in accounts:
        if (a.account.email or "").lower() == k or a.account.id == k:
            return a
    return None
