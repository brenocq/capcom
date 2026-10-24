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
token. Long contexts are priced instead at the cache-read price of the tokens
past the useful size (what rereading them cost), so they never count the cache
write premium a rewrite finding counts on the same request. A rewrite after a
pause past the cache lifetime has one of three causes (expiry_kind): the thread's
last tool call ran past the lifetime (`tool_waits`, any thread), a subagent was
resumed late by its parent (`resumes`), or the main thread waited on the user
(`idle`); the thread's cache lived as long as its last write's lifetime said. The
same stream feeds capcom.checks.lifetimes, which prices the window's cache reads and
writes under three ways to run the cache (`cache_plans`). Main threads also keep a
histogram of their contexts (10k-token buckets with the read price summed per
bucket), so the session checks can price the rereads above any line without
holding the requests. Nothing here holds a whole log or every request in memory:
only the threads, their open items and per-item totals.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median

from capcom.checks.lifetimes import Planner, Plans
from capcom.cost.model import price, price_usage
from capcom.cost.prices import is_placeholder, normalize
from capcom.explain.rules import RESUME_REACH, RESUME_SKEW, RESUME_TOOLS, USER_TOOLS
from capcom.explain.shared import (
    IDLE_EXPIRY,
    LATE_COMPACTION,
    LATE_RESUME_EXPIRY,
    LONG_CONTEXT,
    RESTART_CONTEXT,
    TOOL_WAIT_EXPIRY,
    expiry_kind,
)
from capcom.types import (
    AgentInfo,
    AttrValue,
    Event,
    EventKind,
    Evidence,
    Index,
    PriceTable,
    Request,
    ToolCall,
    Usage,
)

# Events before the window still set what its first requests carry.
LOOKBACK = timedelta(days=3)
# The tools' share of an api-request-shape's size (the rest is the system prompt),
# used when the event has no `tools_size_tokens`. Not measured; a round guess.
SHAPE_TOOLS_SHARE = 0.75
DEFAULT_TOOL_TOKENS = 600  # one tool schema, when no listing in the logs gives a size
LONG_REPORT_TOKENS = 1_500  # an Agent result above this is a long report
REWRITE_SHARE = 0.5  # a request writing this share of its prompt rewrote the cache
MIN_REWRITE_TOKENS = 10_000
CACHE_TTL_5M = timedelta(minutes=5)
CACHE_TTL_1H = timedelta(hours=1)
AGENT_TOOLS = frozenset({"Agent", "Task"})
EVIDENCE_PER_FINDING = 5
# A fresh agent's starting context when its thread's own is unknown (the thread
# began before the window, or its first request in it is already big).
HANDOFF_TOKENS = 40_000
BUCKET_TOKENS = 10_000  # the context histogram's bucket (a multiple divides LONG_CONTEXT)
RESUME_GAP = timedelta(minutes=10)  # a main-thread pause this long starts a new run
# Tool names the agent types called are read this far back at least, so the
# tools: line a check pastes does not depend on a short window.
CALL_LOOKBACK = timedelta(days=30)
TOP_FAMILIES = ("opus",)  # model_mix prices these as the Sonnet of the same version

ItemKey = tuple[str, ...]
ThreadKey = tuple[str, str | None]  # (session_id, agent_id)

# Item families. ("tool", family, name): a tool in a listing; ("mcp", server):
# an MCP server's instructions; ("skill", name); ("memory", source, paths);
# ("image", event_key, tool); ("report", event_key, tool_use_id).
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
    shape_tools: frozenset[str] = frozenset()  # the current shape's loaded tools
    pending: dict[str, ToolCall] = field(default_factory=dict)  # the last request's calls
    result_at: datetime | None = None  # when the last of those calls' results came back
    result_call: ToolCall | None = None
    above: bool = False  # the last request's context was past LONG_CONTEXT
    start: int | None = None  # the context a fresh agent of this thread starts at
    prev_ctx: int | None = None
    sim: float | None = None  # the handed-off agent's context (long_running_agents)
    late_sim: float | None = None  # the earlier-compacted context (late_compactions)
    late_comp: Compaction | None = None
    last_calls: int = 0  # tool calls of the last request
    last_stop: str | None = None
    write_1h: bool = False  # the thread's last cache write used the 1-hour lifetime
    wrote: bool = False  # the thread has written to the cache (write_1h is known)
    away_at: datetime | None = None  # the last away_summary
    # main threads: context bucket -> [requests, sum of read rates, sum of ctx x rate]
    hist: dict[int, list[float]] = field(default_factory=dict)
    resumes: list[tuple[datetime, int]] = field(default_factory=list)  # (time, context)
    days: set[str] = field(default_factory=set)  # main threads: local days with requests
    write_rate: float = 0.0  # main threads: USD per token written, at the latest price
    # main threads: each run (from the first request or a resume): [context at its
    # start, sum of its requests' read rates]
    runs: list[list[float]] = field(default_factory=list)


@dataclass(slots=True)
class Rewrite:
    """Cache rewrites of one cause in one project."""

    count: int = 0
    excess_usd: float = 0.0
    tokens: int = 0
    days: set[str] = field(default_factory=set)
    evidence: list[tuple[float, Evidence]] = field(default_factory=list)
    causes: dict[str, int] = field(default_factory=dict)  # e.g. the tool waited on -> count
    longest: timedelta = timedelta(0)
    confirmed: int = 0  # idle rewrites after an away_summary (the user was away)


@dataclass(slots=True)
class LongContext:
    threads: set[ThreadKey] = field(default_factory=set)
    turns: int = 0
    excess_usd: float = 0.0
    max_context: int = 0
    evidence: dict[ThreadKey, tuple[int, Evidence]] = field(default_factory=dict)
    # per thread: [saving of the simulated handoffs (USD), handoffs, turns past the
    # line, rereads past the line (USD, the old ceiling)]
    per_thread: dict[ThreadKey, list[float]] = field(default_factory=dict)


@dataclass(slots=True)
class Compaction:
    thread: ThreadKey
    timestamp: datetime
    key: str
    pre_tokens: int
    excess_usd: float = 0.0  # turns before it above LATE_COMPACTION
    turns: int = 0
    trigger: str | None = None
    post_tokens: int = 0


@dataclass(slots=True)
class _Expiry:
    """A rewrite after a gap past the cache lifetime, before it is named."""

    r: Request
    project: str
    who: str
    gap: timedelta
    ttl: timedelta
    wait: tuple[timedelta, timedelta, ToolCall | None] | None  # (ran, after, call)
    away: bool
    lo: datetime  # a resuming call must come after this


@dataclass(slots=True)
class ModelMix:
    """One agent type's spend in one project, and its requests on a model priced
    above Sonnet (TOP_FAMILIES) with what they would have cost on Sonnet."""

    cost: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)
    threads: set[ThreadKey] = field(default_factory=set)
    top_cost: float = 0.0
    top_as_sonnet: float = 0.0
    top_requests: int = 0
    top_output: int = 0
    top_calls: int = 0
    top_threads: set[ThreadKey] = field(default_factory=set)


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
    tool_waits: dict[str, Rewrite] = field(default_factory=dict)  # subagents only
    switches: dict[str, Rewrite] = field(default_factory=dict)
    long_context: dict[str, LongContext] = field(default_factory=dict)
    compactions: dict[ThreadKey, list[Compaction]] = field(default_factory=dict)
    shape_tool_tokens: list[float] = field(default_factory=list)
    resumes: dict[str, Rewrite] = field(default_factory=dict)  # subagents resumed late
    # tool names each (agent type, project) called, over CALL_LOOKBACK at least
    type_calls: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    model_mix: dict[tuple[str, str], ModelMix] = field(default_factory=dict)
    session_span: dict[str, timedelta] = field(default_factory=dict)  # first to last seen
    session_first: dict[str, datetime] = field(default_factory=dict)  # first seen
    # the window's cache cost per thread kind under each way to run the cache
    cache_plans: dict[str, Plans] = field(default_factory=dict)

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


def read_rate(req: Request, table: PriceTable) -> float:
    """USD per token of reading `req`'s prompt from the cache, at its model and price."""
    b = price_usage(
        req.model,
        Usage(cache_read=1_000_000),
        table,
        speed=req.speed,
        inference_geo=req.inference_geo,
        when=req.timestamp,
    )
    return b.cache_read / 1_000_000


def tool_label(call: ToolCall) -> str:
    """'Bash (pytest)', 'Read of a/b.py', or the tool's name."""
    if not call.target:
        return call.name
    if call.name in ("Read", "Edit", "Write", "NotebookEdit", "WebFetch", "Grep", "Glob"):
        return f"{call.name} of {call.target}"
    return f"{call.name} ({call.target})"


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
        EventKind.AWAY,
        EventKind.API_REQUEST,  # a request's start and lifetime (capcom.checks.lifetimes)
    }
)


def write_rate(req: Request, table: PriceTable, one_hour: bool) -> float:
    """USD per token of writing to the cache at `req`'s model and price, with the
    1-hour or the 5-minute lifetime."""
    usage = Usage(cache_write_1h=1_000_000) if one_hour else Usage(cache_write_5m=1_000_000)
    b = price_usage(
        req.model,
        usage,
        table,
        speed=req.speed,
        inference_geo=req.inference_geo,
        when=req.timestamp,
    )
    return b.cache_write / 1_000_000


def sonnet_of(model: str) -> str | None:
    """The Sonnet of the same version for a model of TOP_FAMILIES (the table prices
    the closest Sonnet version when that one is missing); None for other models."""
    name = normalize(model)
    for family in TOP_FAMILIES:
        if f"-{family}-" in name or name.endswith(f"-{family}"):
            return name.replace(family, "sonnet", 1)
    return None


class _Pass:
    def __init__(self, index: Index, table: PriceTable, since: datetime, until: datetime) -> None:
        self.index = index
        self.table = table
        self.f = Facts(since=since, until=until)
        self.pending: deque[_Expiry] = deque()
        self.sub_last: dict[str, datetime] = {}  # session -> its last subagent request
        self.resume_calls: dict[str, deque[tuple[datetime, str | None]]] = {}
        self._reads: dict[tuple[str, str | None, str | None, object], float] = {}
        self._writes: dict[tuple[str, str | None, str | None, object, bool], float] = {}
        self.planner = Planner(table, since)

    def read_rate(self, r: Request) -> float:
        key = (r.model, r.speed, r.inference_geo, r.timestamp.date())
        rate = self._reads.get(key)
        if rate is None:
            rate = self._reads[key] = read_rate(r, self.table)
        return rate

    def write_rate(self, r: Request, one_hour: bool) -> float:
        key = (r.model, r.speed, r.inference_geo, r.timestamp.date(), one_hour)
        rate = self._writes.get(key)
        if rate is None:
            rate = self._writes[key] = write_rate(r, self.table, one_hour)
        return rate

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
            call = t.pending.get(str(a.get("tool_use_id")))
            if call is not None and call.name in USER_TOOLS:
                call = None  # its result waits on the user, not on the tool
            if call is not None and (t.result_at is None or e.timestamp >= t.result_at):
                t.result_at, t.result_call = e.timestamp, call
            if a.get("is_image") is True:
                self.open(t, ("image", e.key, str(tool or "?")), size)
            elif tool in AGENT_TOOLS and size > LONG_REPORT_TOKENS:
                tid = a.get("tool_use_id")
                self.open(t, ("report", e.key, tid if isinstance(tid, str) else ""), size)
        elif e.kind is EventKind.AWAY:
            t.away_at = e.timestamp
        elif e.kind is EventKind.COMPACTION:
            t.above = False  # the context starts again below the line
            t.late_sim = t.late_comp = None
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
        if source == "api-request-shape":
            # Deferred tools ride by name only (deferred_tools_delta prices them);
            # the loaded tools share tools_size_tokens, the size of their schemas.
            deferred = set(_names(attrs.get("deferred_names")))
            loaded = [n for n in names if n not in deferred]
            per = 0
            if loaded:
                tools_size = _int(attrs.get("tools_size_tokens"))
                total = tools_size or size * SHAPE_TOOLS_SHARE
                per = round(total / len(loaded))
                self.f.shape_tool_tokens.append(total / len(loaded))
            t.shape_tools = frozenset(loaded)
            for n in loaded:  # a tool now in the shape is no longer counted as a record
                self.close(t, ("tool", "record", n))
            self.replace_family(t, ("tool", "shape"), {("tool", "shape", n): per for n in loaded})
        elif source == "deferred_tools_record":
            per = round(size / len(names)) if names else 0
            fresh: dict[ItemKey, int] = {
                ("tool", "record", n): per for n in names if n not in t.shape_tools
            }
            self.replace_family(t, ("tool", "record"), fresh)
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
            # the lifetime the thread's cache lived: what its last write used (the
            # rewriting request's own, for a thread that never wrote before)
            one_hour = t.write_1h if t.wrote else u.cache_write_1h > 0
            ttl = CACHE_TTL_1H if one_hour else CACHE_TTL_5M
            who = (
                "main thread"
                if r.agent_id is None
                else f"subagent {f.agent_type(r.agent_id) or '?'}"
            )
            if t.last_model is not None and model != t.last_model:
                label = f"{t.last_model} to {model}, {u.cache_write:,} tokens rewritten"
                self.rewrite(f.switches, project, r, label)
            elif gap > ttl:
                self.expiry(t, r, project, who, gap, ttl)

        if u.cache_write > 0:
            t.write_1h, t.wrote = u.cache_write_1h > 0, True
        reread = 0.0 if placeholder else self.read_rate(r)
        if r.agent_id is None and reread > 0:
            self.main_thread(t, r, ctx, reread)
        if not placeholder:
            self.model_mix(r, cost, project)

        # context past the useful size: simulated handoffs and earlier compactions
        if reread > 0:
            self.simulate(t, r, ctx, reread)

        if r.agent_id is not None and r.tool_calls:
            kind = f.agent_type(r.agent_id) or "(unknown agent)"
            f.type_calls.setdefault((kind, project), set()).update(tc.name for tc in r.tool_calls)
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
        t.pending = {tc.tool_use_id: tc for tc in r.tool_calls if tc.tool_use_id}
        t.result_at = t.result_call = None
        t.last_calls, t.last_stop = len(r.tool_calls), r.stop_reason
        if r.agent_id is not None:
            self.sub_last[r.session_id] = r.timestamp
        if any(tc.name in RESUME_TOOLS for tc in r.tool_calls):
            log = self.resume_calls.setdefault(r.session_id, deque())
            log.append((r.timestamp, r.agent_id))
            while log and log[0][0] < r.timestamp - 2 * RESUME_REACH:
                log.popleft()
        if not placeholder:
            t.last_model = model

    def main_thread(self, t: Thread, r: Request, ctx: int, rate: float) -> None:
        """A main-thread request into its thread's context histogram, and a resume
        when it follows a pause of RESUME_GAP or more."""
        b = t.hist.get(ctx // BUCKET_TOKENS)
        if b is None:
            b = t.hist[ctx // BUCKET_TOKENS] = [0.0, 0.0, 0.0]
        b[0] += 1
        b[1] += rate
        b[2] += ctx * rate
        t.days.add(r.timestamp.astimezone().date().isoformat())
        t.write_rate = self.write_rate(r, t.write_1h)
        if t.last is None:
            t.runs.append([ctx, 0.0])
        elif self.resumed_by_user(t, r):
            t.resumes.append((r.timestamp, ctx))
            t.runs.append([ctx, 0.0])
        if t.runs:
            t.runs[-1][1] += rate

    def resumed_by_user(self, t: Thread, r: Request) -> bool:
        """Whether main-thread request `r` starts a new run: a pause of RESUME_GAP or
        more through which nothing ran, as explain names an idle gap (no tool call
        of its own nor subagent of its session working past RESUME_GAP)."""
        assert t.last is not None
        gap = r.timestamp - t.last
        if gap < RESUME_GAP:
            return False
        waits: list[tuple[timedelta, timedelta]] = []
        if t.result_at is not None and t.result_call is not None:
            waits.append((t.result_at - t.last, r.timestamp - t.result_at))
        bg = self.sub_last.get(r.session_id)
        if bg is not None and bg > t.last:
            waits.append((bg - t.last, r.timestamp - bg))
        ran, after = max(waits, default=(None, None), key=lambda w: w[0] or timedelta(0))
        return (
            expiry_kind(gap, RESUME_GAP - timedelta(microseconds=1), ran, after, False, False)
            == IDLE_EXPIRY
        )

    def model_mix(self, r: Request, cost: float, project: str) -> None:
        if r.agent_id is None:
            return
        kind = self.f.agent_type(r.agent_id) or "(unknown agent)"
        mm = self.f.model_mix.get((kind, project))
        if mm is None:
            mm = self.f.model_mix[(kind, project)] = ModelMix()
        model = normalize(r.model)
        tk = (r.session_id, r.agent_id)
        mm.cost += cost
        mm.by_model[model] = mm.by_model.get(model, 0.0) + cost
        mm.threads.add(tk)
        sonnet = sonnet_of(model)
        if sonnet is None:
            return
        b = price_usage(
            sonnet, r.usage, self.table, inference_geo=r.inference_geo, when=r.timestamp
        )
        mm.top_cost += cost
        mm.top_as_sonnet += b.total
        mm.top_requests += 1
        mm.top_output += r.usage.output
        mm.top_calls += len(r.tool_calls)
        mm.top_threads.add(tk)

    def simulate(self, t: Thread, r: Request, ctx: int, rate: float) -> None:
        """Advance thread `t`'s two simulations by request `r` (context `ctx`, read
        price `rate`), the formulas of long_running_agents and late_compactions."""
        f = self.f
        if t.start is None:
            # the thread's own first context when its session began in the window
            # (else this is a mid-life context), at most RESTART_CONTEXT: a fresh
            # agent that continues the work needs its files and state back
            began = f.session_first.get(t.session_id)
            known = began is not None and began >= f.since
            t.start = min(ctx, RESTART_CONTEXT) if known else RESTART_CONTEXT
            if not known:
                # a thread older than the window: its simulated agent handed off
                # before the window (not charged here), and is taken halfway through
                # a cycle, so that where the window starts does not reset it
                t.sim = float(min(ctx, (RESTART_CONTEXT + LONG_CONTEXT) // 2))
        step = ctx - t.prev_ctx if t.prev_ctx is not None else 0
        t.prev_ctx = ctx
        tk = (t.session_id, t.agent_id)
        # a handoff each time the simulated agent passes the line: a fresh one starts
        # at the restart context, written once (5-minute lifetime for a subagent, the
        # thread's own otherwise), and grows by the thread's own increments
        sim = ctx if t.sim is None else max(t.sim + step, min(ctx, t.start))
        cost = sim * rate
        handoff = sim > LONG_CONTEXT
        if handoff:
            sim = t.start
            cost = sim * self.write_rate(r, t.write_1h and t.agent_id is None)
        t.sim = sim
        if ctx > LONG_CONTEXT or handoff:
            lc = f.long_context.setdefault(t.project, LongContext())
            acc = lc.per_thread.setdefault(tk, [0.0, 0, 0, 0.0])
            acc[0] += ctx * rate - cost
            acc[1] += 1 if handoff else 0
            acc[2] += 1 if ctx > LONG_CONTEXT else 0
            acc[3] += max(0, ctx - LONG_CONTEXT) * rate  # the old ceiling, for comparison
            lc.threads.add(tk)
            lc.turns += 1 if ctx > LONG_CONTEXT else 0
            lc.max_context = max(lc.max_context, ctx)
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
        # a late compaction run at LATE_COMPACTION instead: from the turn the context
        # passed it, the thread restarts at the compaction's logged post size (or
        # RESTART_CONTEXT if larger), written once, and regrows the same way
        restart = False
        if t.late_sim is None and ctx > LATE_COMPACTION:
            comp = self.next_late_compaction(t, r.timestamp)
            if comp is not None:
                t.late_comp, restart = comp, True
                t.late_sim = float(max(comp.post_tokens, RESTART_CONTEXT))
        elif t.late_sim is not None and t.late_comp is not None:
            q = max(t.late_comp.post_tokens, RESTART_CONTEXT)
            t.late_sim = max(t.late_sim + step, min(ctx, q))
            if t.late_sim > LATE_COMPACTION:
                t.late_sim, restart = float(q), True
        if t.late_sim is not None and t.late_comp is not None:
            one_hour = t.write_1h and t.agent_id is None
            cost = t.late_sim * (self.write_rate(r, one_hour) if restart else rate)
            t.late_comp.excess_usd += ctx * rate - cost
            t.late_comp.turns += 1

    def expiry(
        self, t: Thread, r: Request, project: str, who: str, gap: timedelta, ttl: timedelta
    ) -> None:
        """Name a rewrite after a gap past the cache lifetime with explain's
        `expiry_kind`, from the same inputs: the thread's own tool wait (user tools
        left out) or, on a main thread, its subagents' work through the gap; and
        whether a finished subagent was resumed by another thread's SendMessage or
        Agent call (up to RESUME_REACH before the request or RESUME_SKEW after, so a
        candidate waits in `pending` until the stream passes that)."""
        assert t.last is not None
        waits: list[tuple[timedelta, timedelta, ToolCall | None]] = []
        if t.result_at is not None and t.result_call is not None:
            waits.append((t.result_at - t.last, r.timestamp - t.result_at, t.result_call))
        bg = self.sub_last.get(r.session_id)
        if r.agent_id is None and bg is not None and bg > t.last:
            waits.append((bg - t.last, r.timestamp - bg, None))
        wait = max(waits, key=lambda w: w[0], default=None)
        away = t.away_at is not None and t.away_at >= t.last
        e = _Expiry(r, project, who, gap, ttl, wait, away, max(t.last, r.timestamp - RESUME_REACH))
        candidate = (
            r.agent_id is not None and not t.last_calls and t.last_stop in (None, "end_turn")
        )
        if not candidate:
            self.record(e, resumed=False)
        elif self.resumed(e):
            self.record(e, resumed=True)
        else:
            self.pending.append(e)

    def resumed(self, e: _Expiry) -> bool:
        hi = e.r.timestamp + RESUME_SKEW
        return any(
            e.lo < when <= hi and aid != e.r.agent_id
            for when, aid in self.resume_calls.get(e.r.session_id, ())
        )

    def flush(self, now: datetime | None) -> None:
        """Decide the pending late-resume candidates whose look-ahead has passed."""
        while self.pending and (now is None or self.pending[0].r.timestamp + RESUME_SKEW < now):
            e = self.pending.popleft()
            self.record(e, resumed=self.resumed(e))

    def record(self, e: _Expiry, resumed: bool) -> None:
        f, r, u = self.f, e.r, e.r.usage
        ran, after, call = e.wait if e.wait is not None else (None, None, None)
        cause = expiry_kind(e.gap, e.ttl, ran, after, r.agent_id is not None, resumed)
        minutes = e.gap.total_seconds() / 60
        if cause == TOOL_WAIT_EXPIRY and ran is not None:
            what = tool_label(call) if call is not None else "its subagents' work"
            label = (
                f"{e.who} waited {ran.total_seconds() / 60:.0f} min on {what}, "
                f"{u.cache_write:,} tokens rewritten"
            )
            rw = self.rewrite(f.tool_waits, e.project, r, label)
            name = call.name if call is not None else "background agents"
            rw.causes[name] = rw.causes.get(name, 0) + 1
            rw.longest = max(rw.longest, ran)
        elif cause == LATE_RESUME_EXPIRY:
            label = (
                f"{e.who} resumed {minutes:.0f} min after its last turn, "
                f"{u.cache_write:,} tokens rewritten"
            )
            rw = self.rewrite(f.resumes, e.project, r, label)
            rw.longest = max(rw.longest, e.gap)
        elif cause == IDLE_EXPIRY:
            note = ", after an away_summary" if e.away else ""
            label = f"{minutes:.0f} min idle{note}, {u.cache_write:,} tokens rewritten"
            rw = self.rewrite(f.idle, e.project, r, label)
            rw.confirmed += 1 if e.away else 0
            rw.longest = max(rw.longest, e.gap)

    def rewrite(self, into: dict[str, Rewrite], project: str, r: Request, label: str) -> Rewrite:
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
        return rw

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
        for s in self.index.sessions():
            if s.first_seen is not None:
                f.session_first[s.session_id] = s.first_seen
        for a in f.agents.values():
            if a.tool_use_id and a.agent_type:
                f.spawn_types.setdefault(a.tool_use_id, a.agent_type)
        for e in self.index.events(f.since, f.until, kinds=frozenset({EventKind.COMPACTION})):
            pre = _int(e.attrs.get("pre_tokens"))
            trigger = e.attrs.get("trigger")
            post = _int(e.attrs.get("post_tokens"))
            comp = Compaction(
                (e.session_id, e.agent_id),
                e.timestamp,
                e.key,
                pre,
                trigger=trigger if isinstance(trigger, str) else None,
                post_tokens=post,
            )
            f.compactions.setdefault(comp.thread, []).append(comp)
        for comps in f.compactions.values():
            comps.sort(key=lambda c: c.timestamp)

        early = min(f.since, f.until - CALL_LOOKBACK)
        for r in self.index.requests(early, f.since) if early < f.since else ():
            if r.agent_id is not None and r.tool_calls:
                kind = f.agent_type(r.agent_id) or "(unknown agent)"
                calls = f.type_calls.setdefault((kind, r.project_dir), set())
                calls.update(tc.name for tc in r.tool_calls)
        events = self.index.events(f.since - LOOKBACK, f.until, kinds=_INTERESTING)
        requests = self.index.requests(f.since, f.until)
        for when, _, _, rec in heapq.merge(_tagged(events, 0), _tagged(requests, 1)):
            self.flush(when)
            if isinstance(rec, Request):
                self.request(rec)
                self.planner.request(rec)
            else:
                self.event(rec)
                self.planner.event(rec)
        self.flush(None)
        f.cache_plans = self.planner.plans
        for t in f.threads.values():
            for key in list(t.items):
                self.close(t, key)
        for s in self.index.sessions():
            f.session_project.setdefault(s.session_id, s.project_dir)
            if s.first_seen is not None and s.last_seen is not None:
                f.session_span[s.session_id] = s.last_seen - s.first_seen
        return f


def _tagged(
    records: Iterable[Event | Request], order: int
) -> Iterator[tuple[datetime, int, int, Event | Request]]:
    # Events sort before requests at the same time: an attachment precedes its reply.
    for seq, rec in enumerate(records):
        yield rec.timestamp, order, seq, rec


# resumes (idle pauses of RESUME_GAP or more) per day over the thread's active span,
# a rate so that detection does not depend on the window's length
HEARTBEAT_PER_DAY = 6
HEARTBEAT_MIN_RESUMES = 10  # and this many, to judge the cadence
HEARTBEAT_STEADY = 0.7  # this share of the intervals between resumes ...
HEARTBEAT_TOLERANCE = 0.25  # ... within this much of their median
HEARTBEAT_GROWTH = 1.5  # the median resumed context over the smallest one ...
HEARTBEAT_GROWTH_TOKENS = 20_000  # ... and at least this much more


def heartbeat(t: Thread) -> tuple[timedelta, int, int] | None:
    """(the interval, the smallest context at a resume, the median one) when main
    thread `t` was resumed on a steady cadence (HEARTBEAT_PER_DAY or more over its
    active span in the window, HEARTBEAT_MIN_RESUMES at least, most of the
    intervals within HEARTBEAT_TOLERANCE of their median) and its runs carry
    the history of the earlier ones: the median resumed context is HEARTBEAT_GROWTH
    times the smallest (what a fresh run carries) or more. Compactions may cut it
    back on the way: the median, not the last runs, is what the runs carried."""
    if len(t.resumes) < HEARTBEAT_MIN_RESUMES or t.first is None or t.last is None:
        return None
    days = max((t.last - t.first) / timedelta(days=1), 1 / 24)
    if len(t.resumes) / days < HEARTBEAT_PER_DAY:
        return None
    times = [when for when, _ in t.resumes]
    gaps = [(b - a).total_seconds() for a, b in pairwise(times)]
    mid = median(gaps)
    if mid <= 0:
        return None
    steady = sum(1 for g in gaps if abs(g - mid) <= HEARTBEAT_TOLERANCE * mid)
    if steady < HEARTBEAT_STEADY * len(gaps):
        return None
    smallest = min(c for _, c in t.resumes)
    typical = int(median(c for _, c in t.resumes))
    if typical < smallest * HEARTBEAT_GROWTH or typical - smallest < HEARTBEAT_GROWTH_TOKENS:
        return None
    return timedelta(seconds=mid), smallest, typical


def collect(index: Index, table: PriceTable, since: datetime, until: datetime) -> Facts:
    """Every fact the checks use, from one pass over [since, until)."""
    return _Pass(index, table, since, until).run()


def threads_by_project(facts: Facts) -> dict[str, list[Thread]]:
    out: dict[str, list[Thread]] = defaultdict(list)
    for t in facts.threads.values():
        if t.requests:
            out[t.project].append(t)
    return out
