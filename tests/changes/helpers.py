"""Synthetic days of requests for the changes tests: a session per day with plain
requests and a number of cache-rewrite episodes (a long tool wait, or an idle
pause), at local times so the weeks line up with the applied date."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from random import Random

from capcom.changes.measure import local_midnight
from capcom.types import ChangeMark, Event, EventKind, Request
from tests.checks.helpers import SortedIndex
from tests.cost.fakes import req

A = date(2026, 9, 30)  # the applied date of most tests
PROJECT = "-home-user-proj"
_n = iter(range(1, 1_000_000))


def mark_of(kind: str, scope: str = PROJECT, on: date = A) -> ChangeMark:
    return ChangeMark(1, kind, scope, on, "a synthetic finding")


def day(
    d: date,
    n: int,
    episodes: int,
    kind: str = "tool",
    project: str = PROJECT,
    agent: str | None = None,
) -> tuple[list[Request], list[Event]]:
    """`n` plain requests on day `d`, then `episodes` rewrites of `kind`: "tool" (a
    Bash call whose result comes back after 20 minutes) or "idle" (a 30-minute
    pause with no call); each episode adds two requests."""
    sid = f"s{d.isoformat()}-{project}"
    t = local_midnight(d) + timedelta(hours=10)
    reqs: list[Request] = []
    evs: list[Event] = []
    for _ in range(n):
        reqs.append(
            req(session=sid, agent=agent, when=t, project=project, cache_read=50_000, output=100)
        )
        t += timedelta(minutes=1)
    for _ in range(episodes):
        tools = ("Bash",) if kind == "tool" else ()
        first = req(
            session=sid,
            agent=agent,
            when=t,
            project=project,
            tools=tools,
            cache_read=50_000,
            output=100,
        )
        reqs.append(first)
        if kind == "tool":
            evs.append(
                Event(
                    EventKind.TOOL_RESULT,
                    sid,
                    agent,
                    t + timedelta(minutes=20),
                    f"ev{next(_n)}",
                    {"tool_use_id": first.tool_calls[0].tool_use_id, "tool": "Bash"},
                )
            )
        t += timedelta(minutes=21 if kind == "tool" else 30)
        reqs.append(
            req(
                session=sid, agent=agent, when=t, project=project, cache_write_5m=50_000, output=100
            )
        )
        t += timedelta(minutes=1)
    return reqs, evs


def build(
    days: Mapping[date, tuple[int, int]], kind: str = "tool", agent: str | None = None
) -> SortedIndex:
    """An index of `days` (date -> (plain requests, episodes)), on the main thread
    or in subagent `agent`."""
    reqs: list[Request] = []
    evs: list[Event] = []
    for d, (n, e) in sorted(days.items()):
        r, v = day(d, n, e, kind, agent=agent)
        reqs += r
        evs += v
    return SortedIndex(reqs=reqs, evs=evs)


def span(start: date, end: date, n: int, episodes: int) -> dict[date, tuple[int, int]]:
    """Every day in [start, end) with the same numbers."""
    return {start + timedelta(days=i): (n, episodes) for i in range((end - start).days)}


def before(n: int = 40, episodes: int = 3) -> dict[date, tuple[int, int]]:
    return span(A - timedelta(days=28), A, n, episodes)


def after(n: int, episodes: int, days: int = 15) -> dict[date, tuple[int, int]]:
    return span(A + timedelta(days=1), A + timedelta(days=days), n, episodes)


def now_after(days: int = 15) -> datetime:
    return local_midnight(A + timedelta(days=days)) + timedelta(hours=12)


def overnight(d: date, project: str = PROJECT) -> list[Request]:
    """An idle pause across midnight: a request at 23:50 on day `d`, and the next
    at 00:20, rewriting the cache, in the same session."""
    sid = f"night{d.isoformat()}-{project}"
    t = local_midnight(d + timedelta(days=1))
    return [
        req(session=sid, when=t - timedelta(minutes=10), project=project, cache_read=50_000),
        req(session=sid, when=t + timedelta(minutes=20), project=project, cache_write_5m=50_000),
    ]


def poisson(rnd: Random, lam: float) -> int:
    """A Poisson draw (Knuth), for noisy synthetic days."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= limit:
            return k
        k += 1
