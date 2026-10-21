"""One pass over the window's requests and events, collecting what the checks need.

The pass merges two time-ordered streams (requests, and the events that set what
rides in every request's prompt) and keeps, per thread (a session's main thread or
one subagent), the items its prompt carries: tool listings, connector instructions,
the skill listing, memory files, images and long subagent reports. Each thread also
keeps a running sum of its requests' prompt rates (USD per prompt token, cache reads
and writes included, at each request's own model and price). An item that rode from
rate sum R0 to R1 cost

    item_usd = size_tokens x (R1 - R0)

which is the item's size times the price each of those requests paid per prompt
token. Nothing here holds a whole log or every request in memory: only the threads,
their open items and per-item totals.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from capcom.cost.model import price, price_usage
from capcom.cost.prices import is_placeholder, normalize
from capcom.types import (
    AgentInfo,
    AttrValue,
    Event,
    EventKind,
    Evidence,
    Index,
    PriceTable,
    Request,
    Usage,
)

# Events before the window still set what its first requests carry.
LOOKBACK = timedelta(days=3)
# The tools' share of an api-request-shape's size (the rest is the system prompt),
# used when the event has no `tools_size_tokens`. Not measured; a round guess.
SHAPE_TOOLS_SHARE = 0.75
DEFAULT_TOOL_TOKENS = 600  # one tool schema, when no listing in the logs gives a size
LONG_CONTEXT = 150_000  # past this a thread is "long-running"
LATE_COMPACTION = 400_000  # a compaction with more pre_tokens than this ran late
LONG_REPORT_TOKENS = 1_500  # an Agent result above this is a long report
REWRITE_SHARE = 0.5  # a request writing this share of its prompt rewrote the cache
MIN_REWRITE_TOKENS = 10_000
CACHE_TTL_5M = timedelta(minutes=5)
CACHE_TTL_1H = timedelta(hours=1)
AGENT_TOOLS = frozenset({"Agent", "Task"})
EVIDENCE_PER_FINDING = 5

ItemKey = tuple[str, ...]
ThreadKey = tuple[str, str | None]  # (session_id, agent_id)

# Item families. ("tool", family, name): a tool in a listing; ("mcp", server):
# an MCP server's instructions; ("skill", name); ("memory", source, paths);
# ("image", event_key, tool); ("report", event_key, tool_use_id).
_SET_FAMILIES = {"api-request-shape": "shape", "deferred_tools_record": "record"}
# Items in the system prompt or tool list survive a compaction; the rest ride in the
# message history and are summarized away (attachments are sent again after it).
_SURVIVES_COMPACTION = {("tool", "shape"), ("tool", "record")}


@dataclass(slots=True)
class _Open:
    size: int
    r0: float
    c0: int


@dataclass(slots=True)
class Account:
    """What one item cost in one thread over the window."""

    usd: float = 0.0
    requests: int = 0
    size: int = 0  # the largest size seen


@dataclass(slots=True)
class Thread:
    session_id: str
    agent_id: str | None
    project: str = ""
    rate_sum: float = 0.0  # sum of the thread's requests' USD per prompt token
    requests: int = 0
    cost: float = 0.0
    tool_calls: int = 0
    max_context: int = 0
    first: datetime | None = None
    last: datetime | None = None
    last_model: str | None = None
    items: dict[ItemKey, _Open] = field(default_factory=dict)


@dataclass(slots=True)
class Rewrite:
    """Cache rewrites of one cause in one project."""

    count: int = 0
    excess_usd: float = 0.0
    tokens: int = 0
    days: set[str] = field(default_factory=set)
    evidence: list[tuple[float, Evidence]] = field(default_factory=list)


@dataclass(slots=True)
class LongContext:
    threads: set[ThreadKey] = field(default_factory=set)
    turns: int = 0
    excess_usd: float = 0.0
    max_context: int = 0
    evidence: dict[ThreadKey, tuple[int, Evidence]] = field(default_factory=dict)


@dataclass(slots=True)
class Compaction:
    thread: ThreadKey
    timestamp: datetime
    key: str
    pre_tokens: int
    excess_usd: float = 0.0  # turns before it above LATE_COMPACTION
    turns: int = 0


@dataclass(slots=True)
class Facts:
    since: datetime
    until: datetime
    first_request: datetime | None = None  # in the window
    logs_start: datetime | None = None  # the first request in the index
    threads: dict[ThreadKey, Thread] = field(default_factory=dict)
    accounts: dict[tuple[str, str | None, ItemKey], Account] = field(default_factory=dict)
    session_project: dict[str, str] = field(default_factory=dict)
    project_cwd: dict[str, dict[str, int]] = field(default_factory=dict)
    calls_by_session: dict[str, set[str]] = field(default_factory=dict)
    calls_by_thread: dict[ThreadKey, set[str]] = field(default_factory=dict)
    skills_by_session: dict[str, set[str]] = field(default_factory=dict)
    agents: dict[str, AgentInfo] = field(default_factory=dict)
    spawn_types: dict[str, str] = field(default_factory=dict)  # tool_use_id -> agent type
    idle: dict[str, Rewrite] = field(default_factory=dict)
    switches: dict[str, Rewrite] = field(default_factory=dict)
    long_context: dict[str, LongContext] = field(default_factory=dict)
    compactions: dict[ThreadKey, list[Compaction]] = field(default_factory=dict)
    shape_tool_tokens: list[float] = field(default_factory=list)

    @property
    def weeks(self) -> float:
        """The window's length in weeks, from its start (or from the first request in
        the whole index, when the logs begin later) to its end; at least one day."""
        start = max(self.since, self.logs_start or self.since)
        span = max(self.until - start, timedelta(days=1))
        return span / timedelta(days=7)

    def per_week(self, usd: float) -> float:
        return usd / self.weeks

    def project_of(self, session_id: str) -> str:
        return self.session_project.get(session_id, "")

    def cwd_of(self, project: str) -> str | None:
        counts = self.project_cwd.get(project)
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    def agent_type(self, agent_id: str | None) -> str | None:
        if agent_id is None:
            return None
        info = self.agents.get(agent_id)
        return info.agent_type if info else None

    def tool_token_estimate(self) -> float:
        """One tool schema's size: the median per-tool size seen in request shapes."""
        if not self.shape_tool_tokens:
            return DEFAULT_TOOL_TOKENS
        values = sorted(self.shape_tool_tokens)
        return values[len(values) // 2]


def prompt_rate(req: Request, table: PriceTable) -> float:
    """USD per prompt token of `req`: its input, cache-write and cache-read cost over
    its prompt size. An item of N tokens in that prompt cost N x this."""
    ctx = req.usage.context
    if ctx <= 0:
        return 0.0
    b = price(req, table)
    return (b.input + b.cache_write + b.cache_read) / ctx


def rewrite_excess(req: Request, table: PriceTable) -> float:
    """What `req`'s cache write cost above reading the same tokens from the cache."""
    b = price(req, table)
    as_read = price_usage(
        req.model,
        Usage(cache_read=req.usage.cache_write),
        table,
        speed=req.speed,
        inference_geo=req.inference_geo,
        when=req.timestamp,
    )
    return max(0.0, b.cache_write - as_read.cache_read)


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple):
        return tuple(v for v in value if isinstance(v, str) and v)
    return ()


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _short(sid: str) -> str:
    return sid[:8]


_INTERESTING = frozenset(
    {
        EventKind.TOOL_SET,
        EventKind.MEMORY,
        EventKind.SKILLS_INVOKED,
        EventKind.TOOL_RESULT,
        EventKind.COMPACTION,
        EventKind.SPAWN,
    }
)


class _Pass:
    def __init__(self, index: Index, table: PriceTable, since: datetime, until: datetime) -> None:
        self.index = index
        self.table = table
        self.f = Facts(since=since, until=until)

    # -- threads and items

    def thread(self, session_id: str, agent_id: str | None) -> Thread:
        key = (session_id, agent_id)
        t = self.f.threads.get(key)
        if t is None:
            t = self.f.threads[key] = Thread(session_id, agent_id)
        return t

    def close(self, t: Thread, key: ItemKey) -> None:
        item = t.items.pop(key, None)
        if item is None or t.requests == item.c0:
            return
        acc_key = (t.session_id, t.agent_id, key)
        acc = self.f.accounts.get(acc_key)
        if acc is None:
            acc = self.f.accounts[acc_key] = Account()
        acc.usd += item.size * (t.rate_sum - item.r0)
        acc.requests += t.requests - item.c0
        acc.size = max(acc.size, item.size)

    def open(self, t: Thread, key: ItemKey, size: int) -> None:
        old = t.items.get(key)
        if old is not None:
            if old.size == size:
                return
            self.close(t, key)
        if size > 0:
            t.items[key] = _Open(size, t.rate_sum, t.requests)

    def replace_family(self, t: Thread, prefix: ItemKey, new: dict[ItemKey, int]) -> None:
        n = len(prefix)
        for key in [k for k in t.items if k[:n] == prefix and k not in new]:
            self.close(t, key)
        for key, size in new.items():
            self.open(t, key, size)

    # -- events

    def event(self, e: Event) -> None:
        t = self.thread(e.session_id, e.agent_id)
        a = e.attrs
        in_window = e.timestamp >= self.f.since
        if e.kind is EventKind.TOOL_SET:
            self.tool_set(t, a)
        elif e.kind is EventKind.MEMORY:
            source = a.get("source")
            paths = _names(a.get("paths"))
            size = _int(a.get("size_tokens"))
            label = "|".join(paths) or "(no path)"
            if source == "instructions":
                new: dict[ItemKey, int] = {("memory", "instructions", label): size}
                self.replace_family(t, ("memory", "instructions"), new)
            else:
                self.open(t, ("memory", "nested", label), size)
        elif e.kind is EventKind.SKILLS_INVOKED and in_window:
            self.f.skills_by_session.setdefault(e.session_id, set()).update(_names(a.get("names")))
        elif e.kind is EventKind.TOOL_RESULT:
            size = _int(a.get("size_tokens"))
            tool = a.get("tool") if isinstance(a.get("tool"), str) else None
            if a.get("is_image") is True:
                self.open(t, ("image", e.key, str(tool or "?")), size)
            elif tool in AGENT_TOOLS and size > LONG_REPORT_TOKENS:
                tid = a.get("tool_use_id")
                self.open(t, ("report", e.key, tid if isinstance(tid, str) else ""), size)
        elif e.kind is EventKind.COMPACTION:
            for key in [k for k in t.items if k[:2] not in _SURVIVES_COMPACTION]:
                self.close(t, key)
        elif e.kind is EventKind.SPAWN:
            tid, kind = a.get("tool_use_id"), a.get("agent_type")
            if isinstance(tid, str) and isinstance(kind, str):
                self.f.spawn_types[tid] = kind

    def tool_set(self, t: Thread, attrs: Mapping[str, AttrValue]) -> None:
        source = attrs.get("source")
        size = _int(attrs.get("size_tokens"))
        names = _names(attrs.get("names"))
        added = _names(attrs.get("added"))
        removed = _names(attrs.get("removed"))
        if source in _SET_FAMILIES:
            family = _SET_FAMILIES[str(source)]
            per = 0
            if names:
                if family == "shape":
                    tools_size = _int(attrs.get("tools_size_tokens"))
                    total = tools_size or size * SHAPE_TOOLS_SHARE
                    per = round(total / len(names))
                    self.f.shape_tool_tokens.append(total / len(names))
                else:
                    per = round(size / len(names))
            self.replace_family(t, ("tool", family), {("tool", family, n): per for n in names})
        elif source == "deferred_tools_delta":
            per = round(size / len(added)) if added else 0
            for n in removed:
                self.close(t, ("tool", "deferred", n))
            for n in added:
                self.open(t, ("tool", "deferred", n), max(per, 1))
        elif source == "mcp_instructions_delta":
            per = round(size / len(added)) if added else 0
            for n in removed:
                self.close(t, ("mcp", n))
            for n in added:
                self.open(t, ("mcp", n), per)
        elif source == "skill_listing":
            per = round(size / len(names)) if names else 0
            skills: dict[ItemKey, int] = {("skill", n): max(per, 1) for n in names}
            if attrs.get("is_initial") is False:
                for key, s in skills.items():
                    self.open(t, key, s)
            else:
                self.replace_family(t, ("skill",), skills)

    # -- requests

    def request(self, r: Request) -> None:
        f = self.f
        if f.first_request is None:
            f.first_request = r.timestamp
        t = self.thread(r.session_id, r.agent_id)
        project = r.project_dir
        t.project = project
        f.session_project.setdefault(r.session_id, project)
        if r.cwd and r.agent_id is None:
            counts = f.project_cwd.setdefault(project, {})
            counts[r.cwd] = counts.get(r.cwd, 0) + 1
        cost = price(r, self.table).total
        u = r.usage
        ctx = u.context
        rate = prompt_rate(r, self.table)
        model = normalize(r.model)
        placeholder = is_placeholder(r.model) or model.startswith("<")

        # cache rewrites: a model switch first, else a pause past the cache lifetime
        rewrote = ctx >= MIN_REWRITE_TOKENS and u.cache_write >= REWRITE_SHARE * ctx
        if t.last is not None and not placeholder and rewrote:
            gap = r.timestamp - t.last
            ttl = CACHE_TTL_1H if u.cache_write_1h > 0 else CACHE_TTL_5M
            if t.last_model is not None and model != t.last_model:
                label = f"{t.last_model} to {model}, {u.cache_write:,} tokens rewritten"
                self.rewrite(f.switches, project, r, label)
            elif gap > ttl:
                minutes = gap.total_seconds() / 60
                label = f"{minutes:.0f} min idle, {u.cache_write:,} tokens rewritten"
                self.rewrite(f.idle, project, r, label)

        # context past the useful size
        if ctx > LONG_CONTEXT and rate > 0:
            late = self.next_late_compaction(t, r.timestamp)
            band = (min(ctx, LATE_COMPACTION) - LONG_CONTEXT) * rate
            above = max(0, ctx - LATE_COMPACTION) * rate
            lc = f.long_context.setdefault(project, LongContext())
            if late is not None:
                late.excess_usd += above
                late.turns += 1 if above > 0 else 0
                above = 0.0
            lc.threads.add((t.session_id, t.agent_id))
            lc.turns += 1
            lc.excess_usd += band + above
            lc.max_context = max(lc.max_context, ctx)
            tk = (t.session_id, t.agent_id)
            if ctx > lc.evidence.get(tk, (0, None))[0]:
                who = (
                    "main thread"
                    if r.agent_id is None
                    else f"subagent {f.agent_type(r.agent_id) or '?'}"
                )
                ev = Evidence(
                    f"{who} of session {_short(r.session_id)} reached {ctx:,} tokens",
                    session_id=r.session_id,
                    agent_id=r.agent_id,
                    timestamp=r.timestamp,
                    message_id=r.message_id,
                )
                lc.evidence[tk] = (ctx, ev)

        for tc in r.tool_calls:
            f.calls_by_session.setdefault(r.session_id, set()).add(tc.name)
            f.calls_by_thread.setdefault((r.session_id, r.agent_id), set()).add(tc.name)
            if tc.name == "Skill" and tc.target:
                f.skills_by_session.setdefault(r.session_id, set()).add(tc.target)

        t.rate_sum += rate
        t.requests += 1
        t.cost += cost
        t.tool_calls += len(r.tool_calls)
        t.max_context = max(t.max_context, ctx)
        t.first = t.first or r.timestamp
        t.last = r.timestamp
        if not placeholder:
            t.last_model = model

    def rewrite(self, into: dict[str, Rewrite], project: str, r: Request, label: str) -> None:
        excess = rewrite_excess(r, self.table)
        rw = into.setdefault(project, Rewrite())
        rw.count += 1
        rw.excess_usd += excess
        rw.tokens += r.usage.cache_write
        rw.days.add(r.timestamp.astimezone().date().isoformat())
        ev = Evidence(
            f"session {_short(r.session_id)}: {label} (${excess:.2f} above a cache read)",
            session_id=r.session_id,
            agent_id=r.agent_id,
            timestamp=r.timestamp,
            message_id=r.message_id,
        )
        rw.evidence.append((excess, ev))
        if len(rw.evidence) > 4 * EVIDENCE_PER_FINDING:
            rw.evidence.sort(key=lambda p: -p[0])
            del rw.evidence[EVIDENCE_PER_FINDING:]

    def next_late_compaction(self, t: Thread, when: datetime) -> Compaction | None:
        comps = self.f.compactions.get((t.session_id, t.agent_id))
        if not comps:
            return None
        i = bisect_right([c.timestamp for c in comps], when)
        if i < len(comps) and comps[i].pre_tokens > LATE_COMPACTION:
            return comps[i]
        return None

    # -- the pass

    def run(self) -> Facts:
        f = self.f
        first = next(iter(self.index.requests()), None)
        f.logs_start = first.timestamp if first else None
        f.agents = {a.agent_id: a for a in self.index.agents()}
        for a in f.agents.values():
            if a.tool_use_id and a.agent_type:
                f.spawn_types.setdefault(a.tool_use_id, a.agent_type)
        for e in self.index.events(f.since, f.until, kinds=frozenset({EventKind.COMPACTION})):
            pre = _int(e.attrs.get("pre_tokens"))
            comp = Compaction((e.session_id, e.agent_id), e.timestamp, e.key, pre)
            f.compactions.setdefault(comp.thread, []).append(comp)
        for comps in f.compactions.values():
            comps.sort(key=lambda c: c.timestamp)

        events = self.index.events(f.since - LOOKBACK, f.until, kinds=_INTERESTING)
        requests = self.index.requests(f.since, f.until)
        for _, _, _, rec in heapq.merge(_tagged(events, 0), _tagged(requests, 1)):
            if isinstance(rec, Request):
                self.request(rec)
            else:
                self.event(rec)
        for t in f.threads.values():
            for key in list(t.items):
                self.close(t, key)
        for s in self.index.sessions():
            f.session_project.setdefault(s.session_id, s.project_dir)
        return f


def _tagged(
    records: Iterable[Event | Request], order: int
) -> Iterator[tuple[datetime, int, int, Event | Request]]:
    # Events sort before requests at the same time: an attachment precedes its reply.
    for seq, rec in enumerate(records):
        yield rec.timestamp, order, seq, rec


def collect(index: Index, table: PriceTable, since: datetime, until: datetime) -> Facts:
    """Every fact the checks use, from one pass over [since, until)."""
    return _Pass(index, table, since, until).run()


def threads_by_project(facts: Facts) -> dict[str, list[Thread]]:
    out: dict[str, list[Thread]] = defaultdict(list)
    for t in facts.threads.values():
        if t.requests:
            out[t.project].append(t)
    return out
