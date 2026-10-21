"""What explain reads from the index for one spike: the sessions it touches, split
into threads (the main thread and each subagent), with their events.

A session is loaded whole (its requests, events and agents), once per `explain`
call, and dropped when no later spike needs it.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from capcom.types import AgentInfo, Event, EventKind, Index, Request, Spike

FIVE_MINUTES = timedelta(minutes=5)
ONE_HOUR = timedelta(hours=1)
# How far from a request an `api-request` record may be and still describe it.
API_REQUEST_REACH = timedelta(minutes=2)


def ttl_delta(ttl: str | None) -> timedelta | None:
    """'5m' -> 5 minutes, '1h' -> 1 hour; None for anything else."""
    if not ttl:
        return None
    unit, num = ttl[-1], ttl[:-1]
    if not num.isdigit():
        return None
    n = int(num)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "s":
        return timedelta(seconds=n)
    return None


@dataclass
class Thread:
    """The requests and events of one agent (agent_id None: the main thread)."""

    session_id: str
    agent_id: str | None
    requests: list[Request] = field(default_factory=list)  # time order, no API errors
    events: list[Event] = field(default_factory=list)  # time order
    _pos: dict[str, int] = field(default_factory=dict)
    _req_times: list[datetime] = field(default_factory=list)
    _ev_times: list[datetime] = field(default_factory=list)

    def finish(self) -> None:
        self.requests.sort(key=lambda r: r.timestamp)
        self.events.sort(key=lambda e: e.timestamp)
        self._pos = {r.message_id: i for i, r in enumerate(self.requests)}
        self._req_times = [r.timestamp for r in self.requests]
        self._ev_times = [e.timestamp for e in self.events]

    def position(self, r: Request) -> int | None:
        return self._pos.get(r.message_id)

    def previous(self, r: Request) -> Request | None:
        """The request of this thread before `r`."""
        i = self.position(r)
        if i is None:
            i = bisect.bisect_left(self._req_times, r.timestamp)
        return self.requests[i - 1] if i > 0 else None

    def requests_between(self, t0: datetime, t1: datetime) -> list[Request]:
        """Requests with t0 <= time < t1."""
        lo = bisect.bisect_left(self._req_times, t0)
        hi = bisect.bisect_left(self._req_times, t1)
        return self.requests[lo:hi]

    def events_between(
        self, t0: datetime, t1: datetime, kinds: Iterable[EventKind] | None = None
    ) -> list[Event]:
        """Events with t0 < time <= t1 (the events that precede a request at t1)."""
        lo = bisect.bisect_right(self._ev_times, t0)
        hi = bisect.bisect_right(self._ev_times, t1)
        wanted = None if kinds is None else frozenset(kinds)
        return [e for e in self.events[lo:hi] if wanted is None or e.kind in wanted]

    def nearest_event(self, kind: EventKind, t: datetime, reach: timedelta) -> Event | None:
        """The event of `kind` closest to `t`, at most `reach` away (earlier wins a tie)."""
        best: Event | None = None
        best_gap: timedelta | None = None
        for e in self.events_between(t - reach - timedelta(microseconds=1), t + reach, (kind,)):
            gap = abs(e.timestamp - t)
            if best_gap is None or gap < best_gap:
                best, best_gap = e, gap
        return best


@dataclass
class SessionData:
    session_id: str
    project_dir: str = ""
    threads: dict[str | None, Thread] = field(default_factory=dict)
    agents: dict[str, AgentInfo] = field(default_factory=dict)
    agent_by_tool_use: dict[str, str] = field(default_factory=dict)
    main_writes_1h: bool = False  # the main thread wrote 1-hour cache entries

    def thread(self, agent_id: str | None) -> Thread:
        t = self.threads.get(agent_id)
        if t is None:
            t = self.threads[agent_id] = Thread(self.session_id, agent_id)
        return t

    def agent_type(self, agent_id: str | None) -> str | None:
        if agent_id is None:
            return None
        info = self.agents.get(agent_id)
        return info.agent_type if info else None

    def cache_lifetime(self, thread: Thread, r: Request) -> tuple[timedelta, str | None]:
        """How long the cache written by `r` lives, and the `cache_ttl` that says so
        (None when it is a fallback: 5 minutes, or 1 hour on a main thread whose
        session shows 1-hour writes)."""
        ev = None
        if r.request_id:
            for e in thread.events_between(
                r.timestamp - 2 * API_REQUEST_REACH,
                r.timestamp + API_REQUEST_REACH,
                (EventKind.API_REQUEST,),
            ):
                if e.attrs.get("request_id") == r.request_id:
                    ev = e
        if ev is None:
            ev = thread.nearest_event(EventKind.API_REQUEST, r.timestamp, API_REQUEST_REACH)
        ttl = ev.attrs.get("cache_ttl") if ev is not None else None
        delta = ttl_delta(ttl) if isinstance(ttl, str) else None
        if delta is not None:
            return delta, str(ttl)
        if thread.agent_id is None and self.main_writes_1h:
            return ONE_HOUR, None
        return FIVE_MINUTES, None


def load_session(index: Index, session_id: str) -> SessionData:
    data = SessionData(session_id)
    for r in index.requests(session_id=session_id):
        data.project_dir = data.project_dir or r.project_dir
        if r.agent_id is None and r.usage.cache_write_1h > 0:
            data.main_writes_1h = True
        if r.is_api_error or r.usage.context == 0:
            continue  # an error or a synthetic reply: no prompt, nothing cached
        data.thread(r.agent_id).requests.append(r)
    for e in index.events(session_id=session_id):
        data.thread(e.agent_id).events.append(e)
    for a in index.agents(session_id):
        data.agents[a.agent_id] = a
        if a.tool_use_id:
            data.agent_by_tool_use[a.tool_use_id] = a.agent_id
    for t in data.threads.values():
        t.finish()
    return data


@dataclass
class Member:
    """The requests of one thread that make up (part of) a spike."""

    session: SessionData
    thread: Thread
    requests: list[Request]


class SessionCache:
    """Sessions loaded for the spikes of one `explain` call."""

    def __init__(self, index: Index, spikes: Sequence[Spike]) -> None:
        self.index = index
        self._loaded: dict[str, SessionData] = {}
        self._uses: dict[str, int] = {}
        for s in spikes:
            if s.session_id is not None:
                self._uses[s.session_id] = self._uses.get(s.session_id, 0) + 1

    def get(self, session_id: str) -> SessionData:
        data = self._loaded.get(session_id)
        if data is None:
            data = self._loaded[session_id] = load_session(self.index, session_id)
        return data

    def done(self, spike: Spike) -> None:
        """Forget sessions no later spike names."""
        if spike.session_id is None:
            self._loaded = {k: v for k, v in self._loaded.items() if self._uses.get(k, 0) > 0}
            return
        n = self._uses.get(spike.session_id, 0) - 1
        self._uses[spike.session_id] = n
        if n <= 0:
            self._loaded.pop(spike.session_id, None)

    def members(self, spike: Spike) -> list[Member]:
        """The spike's requests, grouped by session and thread."""
        wanted = set(spike.message_ids)
        if spike.session_id is not None:
            session_ids = [spike.session_id]
        else:
            session_ids = sorted(
                {
                    r.session_id
                    for r in self.index.requests(since=spike.start, until=spike.end)
                    if not wanted or r.message_id in wanted
                }
            )
        out: list[Member] = []
        for sid in session_ids:
            data = self.get(sid)
            for agent_id, thread in data.threads.items():
                if spike.agent_id is not None and agent_id != spike.agent_id:
                    continue
                if wanted:
                    reqs = [r for r in thread.requests if r.message_id in wanted]
                else:
                    reqs = thread.requests_between(spike.start, spike.end)
                if reqs:
                    out.append(Member(data, thread, reqs))
        out.sort(key=lambda m: (m.requests[0].timestamp, m.thread.agent_id or ""))
        return out
