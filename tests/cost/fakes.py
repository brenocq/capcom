"""A fake Index (the store protocol) and request builders, all synthetic."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capcom.types import (
    AgentInfo,
    Batch,
    CostState,
    Event,
    EventKind,
    FileState,
    LogFile,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
_n = iter(range(1, 1_000_000))


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def req(
    session: str = "s1",
    model: str = "claude-opus-5-5",
    when: datetime = T0,
    agent: str | None = None,
    project: str = "-home-user-proj",
    tools: tuple[str, ...] = (),
    estimated: bool = False,
    speed: str | None = None,
    geo: str | None = None,
    **usage: int,
) -> Request:
    i = next(_n)
    return Request(
        message_id=f"msg_{i}",
        session_id=session,
        agent_id=agent,
        project_dir=project,
        timestamp=when,
        model=model,
        usage=Usage(**usage),
        speed=speed,
        inference_geo=geo,
        output_estimated=estimated,
        tool_calls=tuple(ToolCall(f"tu_{i}_{k}", name) for k, name in enumerate(tools)),
    )


@dataclass
class FakeIndex:
    reqs: list[Request] = field(default_factory=list)
    evs: list[Event] = field(default_factory=list)
    infos: list[SessionInfo] = field(default_factory=list)
    agent_infos: list[AgentInfo] = field(default_factory=list)
    costs: list[CostState] = field(default_factory=list)
    # Times of the replies each session lost to another session's log (none by default).
    shared: dict[str, list[datetime]] = field(default_factory=dict)

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
        for e in self.evs:
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
        yield from self.costs

    def shared_replies(
        self, session_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        return sum(
            1
            for t in self.shared.get(session_id, [])
            if (since is None or t >= since) and (until is None or t < until)
        )
