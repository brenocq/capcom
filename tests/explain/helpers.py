"""Synthetic sessions for the explain tests: a fake Index, round prices, and a
builder that places requests and events by minute."""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from capcom.cost.model import price
from capcom.types import (
    AgentInfo,
    AttrValue,
    Batch,
    CostState,
    Event,
    EventKind,
    FileState,
    LogFile,
    Price,
    PriceTable,
    Request,
    SessionInfo,
    Spike,
    SpikeKind,
    ToolCall,
    Usage,
)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
M = 1_000_000

# USD per million tokens, round on purpose
TABLE = PriceTable(
    checked=date(2026, 10, 21),
    sources=("synthetic",),
    models={
        OPUS: Price(
            OPUS, input=5, output=25, cache_write_5m=6.25, cache_write_1h=10, cache_read=0.5
        ),
        SONNET: Price(
            SONNET, input=3, output=15, cache_write_5m=3.75, cache_write_1h=6, cache_read=0.3
        ),
    },
)
_ids = itertools.count(1)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@dataclass
class FakeIndex:
    reqs: list[Request] = field(default_factory=list)
    evs: list[Event] = field(default_factory=list)
    agent_infos: list[AgentInfo] = field(default_factory=list)
    infos: list[SessionInfo] = field(default_factory=list)

    def file_state(self, path: Path) -> FileState | None:
        return None

    def apply(self, log: LogFile, batch: Batch, state: FileState) -> None:
        raise NotImplementedError

    def forget_file(self, path: Path) -> None:
        raise NotImplementedError

    def requests(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
    ) -> Iterator[Request]:
        for r in sorted(self.reqs, key=lambda r: r.timestamp):
            if since is not None and r.timestamp < since:
                continue
            if until is not None and r.timestamp >= until:
                continue
            if session_id is not None and r.session_id != session_id:
                continue
            yield r

    def events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
        kinds: frozenset[EventKind] | None = None,
    ) -> Iterator[Event]:
        for e in sorted(self.evs, key=lambda e: e.timestamp):
            if since is not None and e.timestamp < since:
                continue
            if until is not None and e.timestamp >= until:
                continue
            if session_id is not None and e.session_id != session_id:
                continue
            if kinds is not None and e.kind not in kinds:
                continue
            yield e

    def sessions(self) -> Iterator[SessionInfo]:
        yield from self.infos

    def agents(self, session_id: str | None = None) -> Iterator[AgentInfo]:
        yield from (a for a in self.agent_infos if session_id in (None, a.session_id))

    def last_cost_states(self) -> Iterator[CostState]:
        yield from ()

    def shared_replies(
        self, session_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        return 0


class World:
    """One fake index; `req` and `event` add to it."""

    def __init__(self) -> None:
        self.index = FakeIndex()

    def req(
        self,
        minutes: float,
        session: str = "s1",
        agent: str | None = None,
        model: str = OPUS,
        tools: Sequence[tuple[str, str | None]] = (),
        effort: str | None = None,
        **usage: int,
    ) -> Request:
        i = next(_ids)
        usage.setdefault("output", 100)
        r = Request(
            message_id=f"msg_{i}",
            session_id=session,
            agent_id=agent,
            project_dir="-home-user-proj",
            timestamp=at(minutes),
            model=model,
            usage=Usage(**usage),
            request_id=f"req_{i}",
            effort=effort,
            tool_calls=tuple(
                ToolCall(f"toolu_{i}_{k}", name, target) for k, (name, target) in enumerate(tools)
            ),
        )
        self.index.reqs.append(r)
        return r

    def event(
        self,
        kind: EventKind,
        minutes: float,
        session: str = "s1",
        agent: str | None = None,
        **attrs: AttrValue,
    ) -> Event:
        e = Event(kind, session, agent, at(minutes), f"ev_{next(_ids)}", attrs)
        self.index.evs.append(e)
        return e

    def agent(self, agent_id: str, agent_type: str, tool_use_id: str, session: str = "s1") -> None:
        self.index.agent_infos.append(
            AgentInfo(agent_id, session, agent_type=agent_type, tool_use_id=tool_use_id)
        )


def cost(r: Request) -> float:
    return price(r, TABLE).total


def spike(
    reqs: Sequence[Request],
    baseline: float = 0.0,
    kind: SpikeKind = SpikeKind.SINGLE_REQUEST,
    session: str | None = "s1",
    agent: str | None = None,
) -> Spike:
    reqs = sorted(reqs, key=lambda r: r.timestamp)
    return Spike(
        kind=kind,
        start=reqs[0].timestamp,
        end=reqs[-1].timestamp + timedelta(seconds=1),
        cost_usd=sum(cost(r) for r in reqs),
        baseline_usd=baseline,
        session_id=session,
        agent_id=agent,
        message_ids=tuple(r.message_id for r in reqs),
    )
