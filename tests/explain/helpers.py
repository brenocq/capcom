"""Synthetic sessions for the explain tests: a fake Index, round prices, and a
builder that places requests and events by minute."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from capcom.cost.model import price
from capcom.explain.data import SessionCache
from capcom.explain.rules import USAGE_FIELDS, Pricer, SpikeView, candidates
from capcom.types import (
    AgentInfo,
    AttrValue,
    Batch,
    CauseKind,
    CostState,
    Event,
    EventKind,
    FileState,
    LogFile,
    LongContextPrice,
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
LONG = "claude-sonnet-4-5"  # priced higher, the whole request, above 200k tokens
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
        LONG: Price(
            LONG,
            input=3,
            output=15,
            cache_write_5m=3.75,
            cache_write_1h=6,
            cache_read=0.3,
            long_context=LongContextPrice(
                200_000, input=6, output=22.5, cache_write_5m=7.5, cache_write_1h=12, cache_read=0.6
            ),
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
            request_id=f"req_011C{i:020d}",
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

    def api(self, r: Request, seconds_before: float = 5.0, **attrs: AttrValue) -> Event:
        """The `api-request` record that sent `r`, logged `seconds_before` its reply. Its
        request_id is the record's own uuid, as in the real logs."""
        attrs.setdefault("model", r.model)
        attrs.setdefault("query_source", "agent:custom" if r.agent_id else "repl_main_thread")
        minutes = (r.timestamp - T0).total_seconds() / 60 - seconds_before / 60
        return self.event(
            EventKind.API_REQUEST,
            minutes,
            session=r.session_id,
            agent=r.agent_id,
            request_id=str(uuid.uuid4()),
            **attrs,
        )

    def call(
        self,
        minutes: float,
        tool: str,
        target: str | None,
        result_tokens: int,
        agent: str | None = None,
        **usage: int,
    ) -> Request:
        """A request making one tool call, and the call's result a few seconds later."""
        r = self.req(minutes, agent=agent, tools=[(tool, target)], **usage)
        self.event(
            EventKind.TOOL_RESULT,
            minutes + 0.05,
            agent=agent,
            tool_use_id=r.tool_calls[0].tool_use_id,
            tool=tool,
            target=target,
            size_tokens=result_tokens,
        )
        return r

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


def check_claims(w: World, s: Spike) -> None:
    """No token of a request is claimed twice, so the raw excesses of all causes add
    up to at most what the spike's requests cost."""
    view = SpikeView(s, SessionCache(w.index, [s]).members(s), Pricer(TABLE))
    found = candidates(view) if view.members else []
    for r in view.requests:
        for f in USAGE_FIELDS:
            taken = sum(getattr(u, f) for _, u in view.ledger.claims.get(r.message_id, []))
            assert 0 <= taken <= getattr(r.usage, f), (r.message_id, f)
    assert sum(c.excess for c in found) <= view.cost + 1e-12
    # every dollar a cause states comes from its own ledger claims: a rule that prices
    # tokens without claiming them (or claims under another cause) fails here
    rewrites = {(rw.request.message_id, rw.reason) for rw in view.rewrites}
    by_id = {r.message_id: r for r in view.requests}
    claimed: dict[CauseKind, float] = {}
    for mid, claims in view.ledger.claims.items():
        r = by_id[mid]
        for cause, u in claims:
            v = (
                view.pricer.rewrite_value(r, u)
                if (mid, cause) in rewrites
                else view.pricer.value(r, u)
            )
            claimed[cause] = claimed.get(cause, 0.0) + v
    stated: dict[CauseKind, float] = {}
    for c in found:
        stated[c.cause] = stated.get(c.cause, 0.0) + c.excess
    for cause, x in stated.items():
        assert abs(x - claimed.get(cause, 0.0)) <= 1e-9 * max(1.0, x), (cause, x, claimed)
