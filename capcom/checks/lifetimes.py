"""What the window's cache reads and writes would have cost under three ways to run
the prompt cache, from the requests and events the facts pass streams by.

- A, 5 minutes: every request writes with the 5-minute lifetime; a gap longer than
  that between two requests of a thread rewrites what the next one reuses.
- B, 1 hour: every request writes with the 1-hour lifetime (twice the input price
  instead of 1.25 times); a gap of up to an hour keeps the cache.
- C, 5 minutes with pings: as A, but through a wait the agent can ping through (its
  previous reply called a tool other than one that waits on the user) it checks in
  every PING_EVERY, each check reading the cache, writing PING_WRITE_TOKENS and
  replying PING_OUTPUT_TOKENS, for up to MAX_PINGS checks (an hour). A wait after
  the agent ended its turn (the user's next message on a main thread, a lead's
  resume on a subagent) cannot be pinged and costs what it costs under A.

Per request, the reusable part of its prompt is what the previous request of the
thread carried (all of it when the prompt grew, what was read when it shrank), none
after a compaction, a model change or a broken prefix (a request read under a tenth
of it within the lifetime its thread used). A part read that no warm prefix explains
(a system prompt or a fork's parent another thread keeps warm) is a read in every
scenario. Gaps run from one request's start to the next one's: a request starts at
the last tool result or `api_request` record after the previous reply, else at its
own reply. Uncached input and output are the same in all three and left out.

Checked against the recorded cost on 30 days of real logs: the model of what
happened (each request warm when its gap was within the lifetime its thread used)
came within 1% of the recorded cache cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from capcom.cost.model import price_usage
from capcom.cost.prices import is_placeholder, lookup, price_at
from capcom.explain.rules import USER_TOOLS
from capcom.types import Event, EventKind, PriceTable, Request, Usage

FIVE_MIN = timedelta(minutes=5)
ONE_HOUR = timedelta(hours=1)
PING_EVERY = timedelta(minutes=4)
PING_WRITE_TOKENS = 300  # a check's own turn, written to the cache
PING_OUTPUT_TOKENS = 50
MAX_PINGS = 15  # an hour of checks; past that the 5-minute cache is let go
BROKEN_READ = 0.1  # a warm request reading under this share of its prefix broke it
SHARED_READ = 0.9  # reading under this share of the prefix: the read came from elsewhere

MAIN, SUBAGENT = "main", "subagent"


@dataclass(slots=True)
class Plans:
    """One thread kind's cache cost (USD) in the window under each way to run it."""

    five_min: float = 0.0  # A
    one_hour: float = 0.0  # B
    pinged: float = 0.0  # C: pings through the waits the agent could ping through
    modeled: float = 0.0  # what happened, by the same model (each thread's own lifetime)
    recorded: float = 0.0  # what the logs record: reads and writes at their prices
    requests: int = 0
    pings: int = 0  # checks C adds
    # gaps over 5 minutes that kept the prefix: [pingable, not pingable]
    gaps: list[int] = field(default_factory=lambda: [0, 0])
    long_gaps: list[int] = field(default_factory=lambda: [0, 0])  # the same, past an hour
    written_1h: int = 0  # tokens written with each lifetime
    written_5m: int = 0
    latest_1h: bool | None = None  # the lifetime of the window's last write
    latest_at: datetime | None = None

    @property
    def current(self) -> str:
        """The lifetime in use: "1h" or "5m", from the window's last write."""
        return "1h" if self.latest_1h else "5m"

    def cost(self, plan: str) -> float:
        return {"5m": self.five_min, "1h": self.one_hour, "ping": self.pinged}[plan]

    def cheapest(self) -> str:
        """The cheapest of "5m", "1h" and "ping"; the current lifetime on a tie."""
        order = [self.current, *(p for p in ("5m", "1h", "ping") if p != self.current)]
        return min(order, key=self.cost)


@dataclass(slots=True)
class _State:
    reply: datetime | None = None  # the last request's reply
    start: datetime | None = None  # when the last request started
    prefix: int = 0  # the cached part of the last request's prompt
    model: str | None = None
    pingable: bool = False  # the last request called a tool the agent waits on
    one_hour: bool = False  # the lifetime the thread wrote with last
    mark: datetime | None = None  # the latest start marker since the last reply
    mark_1h: bool | None = None  # the lifetime an `api_request` record named
    compacted: bool = False  # a compaction since the last reply


_Rates = tuple[float, float, float, float]  # USD per token: read, 5m write, 1h write, output


class Planner:
    """Fed the facts pass's events and requests in time order; `plans` holds the
    window's totals per thread kind (MAIN, SUBAGENT)."""

    def __init__(self, table: PriceTable, since: datetime) -> None:
        self.table = table
        self.since = since
        self.plans: dict[str, Plans] = {}
        self._threads: dict[tuple[str, str | None], _State] = {}
        self._rates: dict[tuple[object, ...], _Rates] = {}

    def event(self, e: Event) -> None:
        if e.kind not in (EventKind.TOOL_RESULT, EventKind.API_REQUEST, EventKind.COMPACTION):
            return
        s = self._threads.get((e.session_id, e.agent_id))
        if s is None:
            s = self._threads[(e.session_id, e.agent_id)] = _State()
        if s.reply is not None and e.timestamp <= s.reply:
            return
        if e.kind is EventKind.COMPACTION:
            s.compacted = True
            return
        s.mark = e.timestamp
        ttl = e.attrs.get("cache_ttl")
        if isinstance(ttl, str) and ttl in ("1h", "5m"):
            s.mark_1h = ttl == "1h"

    def request(self, r: Request) -> None:
        if r.is_api_error or is_placeholder(r.model) or r.model == "unknown":
            return
        key = (r.session_id, r.agent_id)
        s = self._threads.get(key)
        if s is None:
            s = self._threads[key] = _State()
        u = r.usage
        size = u.cache_read + u.cache_write
        read, w5, w1, out = self.rates(r, u.context)
        start = s.mark or r.timestamp
        gap = None if s.start is None else start - s.start  # None: the thread's first
        prev = s.prefix
        # the lifetime this request wrote with: its own split, else what its
        # `api_request` record named, else the thread's last one
        if u.cache_write > 0:
            one_hour = u.cache_write_1h > 0
        else:
            one_hour = s.one_hour if s.mark_1h is None else s.mark_1h

        broken = gap is None or s.compacted or r.model != s.model
        reuse = 0 if broken else prev if size >= prev else min(u.cache_read, size)
        lived = ONE_HOUR if s.one_hour else FIVE_MIN
        warm_gap = gap is not None and gap <= lived
        if not broken and warm_gap and reuse and u.cache_read < BROKEN_READ * reuse:
            reuse = 0
        shared = min(u.cache_read, size) if not reuse or u.cache_read < SHARED_READ * reuse else 0

        def cold(write: float) -> float:
            return shared * read + (size - shared) * write

        def warm(write: float) -> float:
            kept = max(reuse, shared)
            return kept * read + (size - kept) * write

        pings = 0
        if gap is None or not reuse:
            a, b, c, modeled = cold(w5), cold(w1), cold(w5), cold(w1 if one_hour else w5)
        else:
            a = warm(w5) if gap <= FIVE_MIN else cold(w5)
            b = warm(w1) if gap <= ONE_HOUR else cold(w1)
            c = a
            if gap > FIVE_MIN and s.pingable:
                pings = min(int(gap / PING_EVERY), MAX_PINGS)
                each = prev * read + PING_WRITE_TOKENS * w5 + PING_OUTPUT_TOKENS * out
                c = pings * each + (warm(w5) if gap <= ONE_HOUR else cold(w5))
            write = w1 if one_hour else w5
            modeled = warm(write) if gap <= lived else cold(write)

        if r.timestamp >= self.since:
            kind = MAIN if r.agent_id is None else SUBAGENT
            p = self.plans.get(kind)
            if p is None:
                p = self.plans[kind] = Plans()
            p.five_min += a
            p.one_hour += b
            p.pinged += c
            p.modeled += modeled
            p.recorded += u.cache_read * read + u.cache_write_5m * w5 + u.cache_write_1h * w1
            p.requests += 1
            p.pings += pings
            if gap is not None and gap > FIVE_MIN and reuse:
                (p.long_gaps if gap > ONE_HOUR else p.gaps)[0 if s.pingable else 1] += 1
            p.written_1h += u.cache_write_1h
            p.written_5m += u.cache_write_5m
            if u.cache_write > 0 and (p.latest_at is None or r.timestamp >= p.latest_at):
                p.latest_1h, p.latest_at = u.cache_write_1h > 0, r.timestamp

        s.reply, s.start, s.prefix, s.model = r.timestamp, start, size, r.model
        s.pingable = any(tc.name not in USER_TOOLS for tc in r.tool_calls)
        s.one_hour = one_hour
        s.mark, s.mark_1h, s.compacted = None, None, False

    def rates(self, r: Request, context: int) -> _Rates:
        """USD per token of reading, writing (5 minutes, 1 hour) and output at `r`'s
        model, speed, region and date, in the price tier of a `context`-token prompt."""
        p = price_at(self.table, lookup(self.table, r.model)[0], r.timestamp)
        lc = p.long_context
        tier = lc is not None and context > lc.threshold_tokens
        key = (r.model, r.speed, r.inference_geo, r.timestamp.date(), tier)
        rates = self._rates.get(key)
        if rates is None:
            n = max(context, 1)

            def per_token(usage: Usage, part: str) -> float:
                b = price_usage(
                    r.model,
                    usage,
                    self.table,
                    speed=r.speed,
                    inference_geo=r.inference_geo,
                    when=r.timestamp,
                )
                return float(getattr(b, part)) / n

            rates = self._rates[key] = (
                per_token(Usage(cache_read=n), "cache_read"),
                per_token(Usage(cache_write_5m=n), "cache_write"),
                per_token(Usage(cache_write_1h=n), "cache_write"),
                per_token(Usage(cache_read=n, output=n), "output"),
            )
        return rates
