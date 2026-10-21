"""Synthetic sessions for the checks tests: a fake index with events in time order,
and builders for events and runs of requests."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from capcom.cost.prices import load_prices
from capcom.types import AgentInfo, AttrValue, Event, EventKind, Finding, Request, SessionInfo
from tests.cost.fakes import T0, FakeIndex, at, req

SINCE = T0
UNTIL = T0 + timedelta(days=7)  # one week: per-week figures equal window totals
PROJECT = "-home-user-proj"
CWD = "/home/user/proj"
READ_RATE = 0.20 / 1e6  # claude-opus-5-5 cache read, USD per token
TABLE = load_prices()
_k = iter(range(1, 1_000_000))


@dataclass
class SortedIndex(FakeIndex):
    """FakeIndex whose events come in time order, as the store returns them."""

    def events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
        kinds: frozenset[EventKind] | None = None,
    ) -> Iterator[Event]:
        evs = super().events(since, until, session_id, kinds)
        yield from sorted(evs, key=lambda e: e.timestamp)


def ev(
    kind: EventKind,
    minutes: float,
    session: str = "s1",
    agent: str | None = None,
    **attrs: AttrValue,
) -> Event:
    return Event(kind, session, agent, at(minutes), f"ev{next(_k)}", attrs)


def run(
    n: int,
    start: float = 1.0,
    step: float = 1.0,
    session: str = "s1",
    agent: str | None = None,
    project: str = PROJECT,
    calls: dict[int, tuple[str, ...]] | None = None,
    model: str = "claude-opus-5-5",
    cache_read: int = 50_000,
    **usage: int,
) -> list[Request]:
    """`n` requests, `step` minutes apart from minute `start`, each reading
    `cache_read` tokens from the cache; `calls[i]` are request i's tool names."""
    out = []
    for i in range(n):
        r = req(
            session=session,
            agent=agent,
            project=project,
            model=model,
            when=at(start + i * step),
            tools=(calls or {}).get(i, ()),
            cache_read=cache_read,
            **usage,
        )
        out.append(replace(r, cwd=CWD) if agent is None else r)
    return out


def agent(
    agent_id: str,
    kind: str,
    session: str = "s1",
    tool_use_id: str | None = None,
    is_built_in: bool | None = None,
) -> AgentInfo:
    return AgentInfo(
        agent_id, session, agent_type=kind, tool_use_id=tool_use_id, is_built_in=is_built_in
    )


def index(
    reqs: Sequence[Request], evs: Sequence[Event] = (), agents: Sequence[AgentInfo] = ()
) -> SortedIndex:
    """The index of `reqs`, plus an empty request a day before the window so the
    logs start before it (the window's pace is then exactly one week)."""
    early = req(session="early", project="-elsewhere", when=SINCE - timedelta(days=1))
    return SortedIndex(reqs=[early, *reqs], evs=list(evs), agent_infos=list(agents))


def kinds(findings: list[Finding]) -> list[str]:
    return [f.kind for f in findings]


def only(findings: list[Finding], kind: str) -> Finding:
    found = [f for f in findings if f.kind == kind]
    assert len(found) == 1, kinds(findings)
    return found[0]


def began(idx: SortedIndex) -> SortedIndex:
    """`idx` with every session's first_seen at its first request: sessions that
    began in the window, whose first context the checks may take as their start."""
    first: dict[str, Request] = {}
    for r in sorted(idx.reqs, key=lambda r: r.timestamp):
        first.setdefault(r.session_id, r)
    idx.infos = [
        SessionInfo(sid, r.project_dir, first_seen=r.timestamp, last_seen=UNTIL)
        for sid, r in first.items()
    ]
    return idx
