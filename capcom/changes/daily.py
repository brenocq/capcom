"""Day-by-day tallies from one pass of the checks' facts over a whole window.

The checks' pass (capcom.checks.facts) keeps per-thread state: when a thread last
ran, its open tool calls, the items its prompt carries, its context simulations.
One pass over each day alone would start every thread fresh at local midnight,
losing the rewrites after overnight pauses and starting a fake run with every
day's first request. So the pass runs once over the window (from a day of
lookback before it, for that state) and, at each local midnight, hands the day's
facts to the measures and resets only the day's sums: the rewrite and context
tallies, the accounts of carried items (each open item is split at midnight, its
first part counted in the ending day), the model mix, and each thread's request
counters and runs. Thread state carries on.

Finished days' tallies are kept in a small SQLite file in capcom's cache
(`changes-days.sqlite`): a past day of the logs does not change, so a warm run
only reads the last days again. A day counts as finished one day before the last
activity the index has seen (a scan may still bring the hours before it).
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from capcom.changes.measures import MEASURES, Tally
from capcom.checks.facts import (
    _INTERESTING,
    LOOKBACK,
    Compaction,
    Facts,
    Thread,
    _int,
    _Pass,
    _tagged,
)
from capcom.types import Event, EventKind, Index, PriceTable, Request

CACHE_VERSION = 1  # bump when a measure's counting changes
STATE_LOOKBACK = timedelta(days=1)  # thread state read before the first day measured
FINISHED_MARGIN = timedelta(days=1)

Key = tuple[str, str]  # (kind, scope)
DayTallies = dict[Key, dict[date, Tally]]


def local_midnight(d: date) -> datetime:
    """The start of local day `d`, as an aware datetime."""
    return datetime.combine(d, time()).astimezone()


@dataclass(slots=True)
class _Errors:
    flagged: dict[tuple[str, str | None], int] = field(default_factory=dict)
    errors: dict[tuple[str, str | None], int] = field(default_factory=dict)


class _DayPass(_Pass):
    """The checks' pass, with the tool-result error flags counted per thread."""

    def __init__(self, index: Index, table: PriceTable, since: datetime, until: datetime) -> None:
        super().__init__(index, table, since, until)
        self.errs = _Errors()

    def event(self, e: Event) -> None:
        if (
            e.kind is EventKind.TOOL_RESULT
            and "is_error" in e.attrs
            and e.timestamp >= self.f.since
        ):
            key = (e.session_id, e.agent_id)
            self.errs.flagged[key] = self.errs.flagged.get(key, 0) + 1
            if e.attrs.get("is_error") is True:
                self.errs.errors[key] = self.errs.errors.get(key, 0) + 1
        super().event(e)

    def settle(self) -> list[tuple[Thread, tuple[str, ...], int]]:
        """Close every open item into the accounts (before a day's tally), and
        return them to reopen once the day is reset."""
        reopen: list[tuple[Thread, tuple[str, ...], int]] = []
        for t in self.f.threads.values():
            for key, item in list(t.items.items()):
                self.close(t, key)
                reopen.append((t, key, item.size))
        return reopen

    def rollover(self, reopen: list[tuple[Thread, tuple[str, ...], int]]) -> None:
        """Reset the day's sums, and reopen the settled items from here."""
        f = self.f
        f.accounts = {}
        f.idle, f.tool_waits, f.resumes, f.switches = {}, {}, {}, {}
        f.long_context = {}
        f.model_mix = {}
        for t in f.threads.values():
            t.requests = 0
            t.cost = 0.0
            t.tool_calls = 0
            t.hist = {}
            t.days = set()
            t.runs = []
        for t, key, size in reopen:
            self.open(t, key, size)
        self.errs = _Errors()


def _pass_days(
    index: Index,
    table: PriceTable,
    first: date,
    last: date,
    now: datetime,
    on_day: Callable[[date, Facts, _Errors], None],
) -> None:
    """One pass from STATE_LOOKBACK before day `first` to the end of day `last`
    (at most `now`), calling `on_day` with each day's facts from `first` on."""
    start = local_midnight(first) - STATE_LOOKBACK
    end = min(local_midnight(last + timedelta(days=1)), now)
    p = _DayPass(index, table, start, end)
    f = p.f
    head = next(iter(index.requests()), None)
    f.logs_start = head.timestamp if head else None
    f.agents = {a.agent_id: a for a in index.agents()}
    for s in index.sessions():
        if s.first_seen is not None:
            f.session_first[s.session_id] = s.first_seen
        f.session_project.setdefault(s.session_id, s.project_dir)
        if s.first_seen is not None and s.last_seen is not None:
            f.session_span[s.session_id] = s.last_seen - s.first_seen
    for a in f.agents.values():
        if a.tool_use_id and a.agent_type:
            f.spawn_types.setdefault(a.tool_use_id, a.agent_type)
    for e in index.events(start, end, kinds=frozenset({EventKind.COMPACTION})):
        trigger = e.attrs.get("trigger")
        comp = Compaction(
            (e.session_id, e.agent_id),
            e.timestamp,
            e.key,
            _int(e.attrs.get("pre_tokens")),
            trigger=trigger if isinstance(trigger, str) else None,
            post_tokens=_int(e.attrs.get("post_tokens")),
        )
        f.compactions.setdefault(comp.thread, []).append(comp)
    for comps in f.compactions.values():
        comps.sort(key=lambda c: c.timestamp)

    # day boundaries: the lookback's end (no tally), then each measured day's end
    bounds: list[tuple[datetime, date | None]] = [(local_midnight(first), None)]
    d = first
    while d <= last:
        bounds.append((min(local_midnight(d + timedelta(days=1)), end), d))
        d += timedelta(days=1)
    i = 0

    def close_day(when: datetime | None) -> None:
        nonlocal i
        while i < len(bounds) and (when is None or bounds[i][0] <= when):
            at, day = bounds[i]
            reopen = p.settle()
            if day is not None:
                f.since, f.until = local_midnight(day), at
                on_day(day, f, p.errs)
                f.since, f.until = start, end
            p.rollover(reopen)
            i += 1

    events = index.events(start - LOOKBACK, end, kinds=_INTERESTING)
    requests = index.requests(start, end)
    stream: Iterable[tuple[datetime, int, int, Event | Request]] = heapq.merge(
        _tagged(events, 0), _tagged(requests, 1)
    )
    for when, _, _, rec in stream:
        p.flush(when)  # late-resume candidates decided by now, before the day closes
        close_day(when)
        if isinstance(rec, Request):
            p.request(rec)
        else:
            p.event(rec)
    p.flush(None)
    close_day(None)


# --------------------------------------------------------------------------- cache


class DayCache:
    """Finished days' tallies on disk; every failure is a miss."""

    def __init__(self, path: Path | None, salt: str) -> None:
        self.path = path
        self.salt = salt

    def _key(self, key: Key, day: date) -> str:
        at = local_midnight(day).isoformat()  # the local offset that day: its bounds
        return json.dumps([CACHE_VERSION, self.salt, key[0], key[1], at])

    def get(self, keys: Iterable[tuple[Key, date]]) -> dict[tuple[Key, date], Tally]:
        out: dict[tuple[Key, date], Tally] = {}
        if self.path is None or not self.path.exists():
            return out
        wanted = {self._key(k, d): (k, d) for k, d in keys}
        try:
            with closing(sqlite3.connect(self.path)) as con:
                rows = con.execute("SELECT key, tally FROM days").fetchall()
        except sqlite3.Error:
            return out
        for k, raw in rows:
            if k in wanted:
                with suppress(ValueError, TypeError, KeyError):
                    out[wanted[k]] = _load_tally(raw)
        return out

    def put(self, items: Mapping[tuple[Key, date], Tally]) -> None:
        if self.path is None or not items:
            return
        with suppress(sqlite3.Error, OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as con, con:
                con.execute("CREATE TABLE IF NOT EXISTS days (key TEXT PRIMARY KEY, tally TEXT)")
                con.executemany(
                    "INSERT OR REPLACE INTO days VALUES (?, ?)",
                    [(self._key(k, d), _dump_tally(t)) for (k, d), t in items.items()],
                )


def _dump_tally(t: Tally) -> str:
    return json.dumps(
        {
            "count": t.count,
            "base": t.base,
            "requests": t.requests,
            "usd": t.usd,
            "samples": t.samples,
            "sums": t.sums,
            "maxes": t.maxes,
        }
    )


def _load_tally(raw: str) -> Tally:
    o = json.loads(raw)
    return Tally(
        count=float(o["count"]),
        base=float(o["base"]),
        requests=int(o["requests"]),
        usd=float(o["usd"]),
        samples=[float(x) for x in o["samples"]],
        sums={str(k): float(v) for k, v in o["sums"].items()},
        maxes={str(k): float(v) for k, v in o["maxes"].items()},
    )


def day_tallies(
    index: Index,
    table: PriceTable,
    keys: Iterable[Key],
    first: date,
    now: datetime,
    cache_path: Path | None = None,
) -> DayTallies:
    """Each (kind, scope)'s tally for every local day from `first` to `now`'s day."""
    wanted = sorted({k for k in keys if k[0] in MEASURES})
    out: DayTallies = {k: {} for k in wanted}
    last = now.astimezone().date()
    if not wanted or first > last:
        return out
    last_seen = max((s.last_seen for s in index.sessions() if s.last_seen), default=now)
    finished_before = min(now, last_seen) - FINISHED_MARGIN
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    cache = DayCache(cache_path, str(table.checked))
    hits = cache.get((k, d) for k in wanted for d in days)
    for (k, d), t in hits.items():
        out[k][d] = t
    missing = [d for d in days if any(d not in out[k] for k in wanted)]
    if not missing:
        return out
    fresh: dict[tuple[Key, date], Tally] = {}

    def on_day(day: date, facts: Facts, errs: _Errors) -> None:
        for k in wanted:
            if day in out[k]:
                continue
            t = MEASURES[k[0]].tally(facts, k[1], errs.flagged, errs.errors)
            out[k][day] = t
            if local_midnight(day + timedelta(days=1)) <= finished_before:
                fresh[(k, day)] = t

    _pass_days(index, table, missing[0], last, now, on_day)
    cache.put(fresh)
    return out
