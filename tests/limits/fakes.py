"""A minimal in-memory index for the limits tests (requests and events only). All
data is synthetic."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

from capcom.types import AgentInfo, Event, EventKind, LimitSample, LimitWindow, Request, Usage

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_n = iter(range(1, 10_000_000))


class FakeIndex:
    def __init__(
        self,
        requests: Sequence[Request] = (),
        events: Sequence[Event] = (),
        agents: Sequence[AgentInfo] = (),
    ) -> None:
        self.reqs = sorted(requests, key=lambda r: r.timestamp)
        self.evs = sorted(events, key=lambda e: e.timestamp)
        self.agent_infos = list(agents)

    def agents(self, session_id: str | None = None) -> Iterator[AgentInfo]:
        return iter(self.agent_infos)

    def requests(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
    ) -> Iterator[Request]:
        for r in self.reqs:
            if since is not None and r.timestamp < since:
                continue
            if until is not None and r.timestamp >= until:
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
            if kinds is None or e.kind in kinds:
                yield e

    def sessions(self) -> Iterator[object]:
        return iter(())


def req(
    when: datetime, model: str = "claude-opus-5-5", project: str = "-home-user-alpha", **u: int
) -> Request:
    i = next(_n)
    return Request(
        message_id=f"msg_{i}",
        session_id="s1",
        agent_id=None,
        project_dir=project,
        timestamp=when,
        model=model,
        usage=Usage(**u),
    )


def sample(
    when: datetime,
    pct: float,
    window: LimitWindow = LimitWindow.FIVE_HOUR,
    resets: datetime | None = None,
    source: str = "statusline",
) -> LimitSample:
    return LimitSample(when, window, pct, resets, "s1", source)


def minutes(m: float) -> timedelta:
    return timedelta(minutes=m)
