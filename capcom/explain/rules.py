"""The causes of design.md, "Causes (explain)", each as a rule that looks at a spike's
requests and the events before and during them, and says how many dollars it
accounts for.

Every excess is a counterfactual on real requests: the cost of the request as
logged minus its cost with the tokens the cause put there taken out (or, for a
cache rewrite, read instead of written), priced by `capcom.cost.model` at the
request's own model, speed, region and date. A rule returns raw excesses; the
caller caps their sum at the spike's excess.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from capcom.cost.model import price, price_usage
from capcom.cost.prices import canonical, lookup, price_at
from capcom.explain import fmt, shared
from capcom.explain.data import (
    ONE_HOUR,
    Member,
    SessionData,
    Thread,
    api_request_for,
    ttl_delta,
)
from capcom.explain.shared import CONTEXT_STEPS, GROWTH_FLOOR, LATE_COMPACTION, LONG_CONTEXT
from capcom.types import (
    CauseKind,
    Confidence,
    Event,
    EventKind,
    Evidence,
    PriceTable,
    Request,
    Spike,
    ToolCall,
    Usage,
)

# A request "rewrites its context" when it wrote again at least this share of what its
# thread had cached, and at least MIN_REWRITE_TOKENS (`rewritten_tokens`).
REWRITE_SHARE = 0.6
MIN_REWRITE_TOKENS = 10_000
LARGE_RESULT_TOKENS = 20_000  # a tool result this big is "large" (rule 5)
LONG_REPORT_TOKENS = 10_000  # an Agent report this big is "long" (rule 8)
RESULT_LOOKBACK = timedelta(hours=1)  # results that may still weigh on a spike
TRIM_MIN_TOKENS = 20_000  # a prompt that shrank this much, and by TRIM_SHARE, was trimmed
TRIM_SHARE = 0.05
COMPACTION_REACH = timedelta(minutes=10)  # a compaction just after the spike ends it
GROWTH_RATIO = 1.5  # peak context over the hour-before median
GROWTH_CLEAR_RATIO = 2.0
BASELINE_WINDOW = timedelta(hours=1)
FAN_OUT_MIN = 4  # Agent calls in the window that make a fan-out (rule 7)
FAN_OUT_LOOKBACK = timedelta(minutes=10)
FAN_OUT_BURST = timedelta(minutes=5)  # FAN_OUT_MIN spawns within this make a burst
FAN_OUT_START = timedelta(minutes=2)  # an unmatched spawn's agent starts within this
THINKING_SHARE = 0.5  # thinking at least this share of the output (rule 9)
OUTPUT_COST_SHARE = 0.5  # without logged thinking: output at least this share of cost
MIN_HEAVY_OUTPUT = 2_000
POLL_MIN = 5  # the same tool call this many times in a row (rule 10)
POLL_CLEAR = 10
POLL_TOOLS = frozenset({"BashOutput", "TaskOutput"})  # checks on a background task
POLL_COMMANDS = frozenset({"sleep", "watch", "until", "while", "wait", "inotifywait"})
POLL_WORDS = ("wait", "poll")  # e.g. a waitlog.sh script
PARALLEL_MIN = 4  # subagents working at once that make parallel work (rule 11)
PARALLEL_PAD = timedelta(seconds=30)  # a thread counts as active this long after a request
MAX_EVIDENCE = 6

AGENT_TOOLS = frozenset({"Agent", "Task"})
ATTACHMENT_SOURCES = frozenset(
    {"deferred_tools_delta", "mcp_instructions_delta", "agent_listing_delta", "skill_listing"}
)
SHAPE_SOURCE = "api-request-shape"


@dataclass
class Candidate:
    cause: CauseKind
    confidence: Confidence
    excess: float
    title: str
    evidence: list[Evidence] = field(default_factory=list)
    advice: str = ""
    summary: str = ""


# --------------------------------------------------------------------------- pricing


@dataclass(frozen=True, slots=True)
class _Rates:
    """USD per token (per search or fetch) of one request, at its own rate tier."""

    input: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    output: float
    web_searches: float
    web_fetches: float


class Pricer:
    """Request costs, and the value of a part of a request's usage.

    A part is priced at the request's own per-token rates: when the whole request
    is above a long-context threshold, the tokens a cause put there cost the
    long-context rate, and taking them out never re-prices the rest of the request
    at the base rate. The parts of one request add up to its cost."""

    def __init__(self, table: PriceTable) -> None:
        self.table = table
        self._costs: dict[str, float] = {}
        self._rates: dict[str, _Rates] = {}

    def cost(self, r: Request) -> float:
        c = self._costs.get(r.message_id)
        if c is None:
            c = self._costs[r.message_id] = price(r, self.table).total
        return c

    def rates(self, r: Request) -> _Rates:
        found = self._rates.get(r.message_id)
        if found is not None:
            return found
        p = price_at(self.table, lookup(self.table, r.model)[0], r.timestamp)
        lc = p.long_context
        # a probe of n tokens lands in the same tier as the request
        long = lc is not None and r.usage.context > lc.threshold_tokens
        n = lc.threshold_tokens + 1 if lc is not None and long else 1

        def per(u: Usage, part: str, k: int) -> float:
            b = price_usage(r.model, u, self.table, r.speed, r.inference_geo, r.timestamp)
            return float(getattr(b, part)) / k

        found = self._rates[r.message_id] = _Rates(
            input=per(Usage(input=n), "input", n),
            cache_read=per(Usage(cache_read=n), "cache_read", n),
            cache_write_5m=per(Usage(cache_write_5m=n), "cache_write", n),
            cache_write_1h=per(Usage(cache_write_1h=n), "cache_write", n),
            # output is priced in the tier of the prompt it answers
            output=per(Usage(output=n, input=r.usage.context), "output", n),
            web_searches=per(Usage(web_searches=1), "web", 1),
            web_fetches=per(Usage(web_fetches=1), "web", 1),
        )
        return found

    def value(self, r: Request, part: Usage) -> float:
        """What `part` of `r`'s usage cost, at `r`'s own rates."""
        k = self.rates(r)
        return (
            part.input * k.input
            + part.cache_read * k.cache_read
            + part.cache_write_5m * k.cache_write_5m
            + part.cache_write_1h * k.cache_write_1h
            + part.output * k.output
            + part.web_searches * k.web_searches
            + part.web_fetches * k.web_fetches
        )

    def rewrite_value(self, r: Request, writes: Usage) -> float:
        """What writing `writes` cost above reading them from the cache."""
        return max(0.0, self.value(r, writes) - self.value(r, Usage(cache_read=writes.cache_write)))

    def same_model(self, a: str, b: str) -> bool:
        return canonical(self.table, a) == canonical(self.table, b)


USAGE_FIELDS = (
    "input",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "output",
    "thinking",
    "web_searches",
    "web_fetches",
)


def _writes(u: Usage, n: int) -> Usage:
    """`n` of the writes in `u`, 1-hour and 5-minute in proportion."""
    n = min(n, u.cache_write)
    if n <= 0:
        return Usage()
    n1 = min(u.cache_write_1h, round(n * u.cache_write_1h / u.cache_write))
    n5 = min(u.cache_write_5m, n - n1)
    return Usage(cache_write_1h=n1, cache_write_5m=n5)


def prompt_part(u: Usage, n: int, writes_first: bool) -> Usage:
    """`n` prompt tokens of `u`: from the writes then the reads (the request that
    first sent them), or the reads then the writes (later requests), then input."""
    left = max(0, n)
    w = Usage()
    read = 0
    for part in ("writes", "reads") if writes_first else ("reads", "writes"):
        if part == "writes":
            w = _writes(u, left)
            left -= w.cache_write
        else:
            read = min(u.cache_read, left)
            left -= read
    return replace(w, cache_read=read, input=min(u.input, left))


class Ledger:
    """The tokens of each spike request that rules have claimed, so that no token
    (and no dollar) is claimed twice. Each rule claims from what earlier rules left."""

    def __init__(self, pricer: Pricer) -> None:
        self.pricer = pricer
        self._left: dict[str, Usage] = {}
        self.claims: dict[str, list[tuple[CauseKind, Usage]]] = {}

    def left(self, r: Request) -> Usage:
        return self._left.get(r.message_id, r.usage)

    def prompt_claimed(self, r: Request) -> int:
        return r.usage.context - self.left(r).context

    def claim(self, r: Request, part: Usage, cause: CauseKind) -> Usage:
        """Claim `part` of `r` (cut to what is left) and return what was claimed."""
        left = self.left(r)
        got = {f: max(0, min(getattr(part, f), getattr(left, f))) for f in USAGE_FIELDS}
        got["thinking"] = min(got["thinking"], got["output"])
        taken = Usage(**got)
        if any(got.values()):
            rest = {f: getattr(left, f) - got[f] for f in USAGE_FIELDS}
            rest["thinking"] = min(rest["thinking"], rest["output"])
            self._left[r.message_id] = Usage(**rest)
            self.claims.setdefault(r.message_id, []).append((cause, taken))
        return taken

    def take(self, r: Request, part: Usage, cause: CauseKind) -> float:
        """Claim `part` of `r` and return what the claimed tokens cost."""
        return self.pricer.value(r, self.claim(r, part, cause))

    def take_prompt(self, r: Request, n: int, writes_first: bool, cause: CauseKind) -> float:
        return self.take(r, prompt_part(self.left(r), n, writes_first), cause)


# --------------------------------------------------------------------------- the spike


@dataclass
class SpikeView:
    spike: Spike
    members: list[Member]
    pricer: Pricer
    ledger: Ledger = field(init=False)
    rewrites: list[Rewrite] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ledger = Ledger(self.pricer)

    @property
    def requests(self) -> list[Request]:
        return [r for m in self.members for r in m.requests]

    @property
    def cost(self) -> float:
        return sum(self.pricer.cost(r) for r in self.requests)

    @property
    def window_end(self) -> datetime:
        last = max((r.timestamp for r in self.requests), default=self.spike.end)
        return max(self.spike.end, last + timedelta(microseconds=1))


def _ev(label: str, m: Member, r: Request | None = None, e: Event | None = None) -> Evidence:
    t = r.timestamp if r is not None else (e.timestamp if e is not None else None)
    return Evidence(
        label,
        session_id=m.thread.session_id,
        agent_id=m.thread.agent_id,
        timestamp=t,
        message_id=r.message_id if r is not None else None,
        event_key=e.key if e is not None else None,
    )


def _who(m: Member) -> str:
    if m.thread.agent_id is None:
        return "main-thread"
    kind = m.session.agent_type(m.thread.agent_id)
    return f"{kind} subagent" if kind else "subagent"


def _int(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _names(v: object) -> tuple[str, ...]:
    return v if isinstance(v, tuple) else ()


def _list(names: Sequence[str], limit: int = 4) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _cap(evidence: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    out = []
    for e in evidence:
        if e.label not in seen:
            seen.add(e.label)
            out.append(e)
    return out[:MAX_EVIDENCE]


def _confidence(parts: Iterable[tuple[Confidence, float]]) -> Confidence:
    """CLEAR when clear parts carry at least half the excess."""
    clear = total = 0.0
    for conf, x in parts:
        total += x
        if conf is Confidence.CLEAR:
            clear += x
    return Confidence.CLEAR if total > 0 and clear >= total / 2 else Confidence.LIKELY


# --------------------------------------------------------------------------- rules 1-4: rewrites


@dataclass
class Rewrite:
    """A spike request that wrote again a context its thread had cached."""

    member: Member
    request: Request
    previous: Request
    tokens: int
    excess: float
    reason: CauseKind = CauseKind.UNEXPLAINED
    confidence: Confidence = Confidence.LIKELY
    evidence: list[Evidence] = field(default_factory=list)
    gap: timedelta = timedelta(0)
    lifetime: timedelta | None = None
    same_model: bool = True  # the price table prices both requests as one model
    waited_on: str | None = None  # the tool call that ran through the pause, if any
    ttl_change: tuple[str, str] | None = None  # cacheControl ttl before and after
    shape_change: bool = False  # the tool list or prompt (shapeHash) changed too
    trimmed: bool = False  # the prompt shrank with no compaction record (a COMPACTION)


def rewritten_tokens(r: Request, prev: Request | None) -> int:
    """Tokens `r` wrote again that its thread's previous request had sent: the part of
    the previous prompt `r` did not read from the cache, as far as `r` wrote it. 0 when
    that is below MIN_REWRITE_TOKENS or below REWRITE_SHARE of the smaller of the two
    prompts (a compaction shrinks the prompt, new tool output grows it)."""
    if prev is None:
        return 0
    u = r.usage
    lost = prev.usage.context - u.cache_read
    n = min(u.cache_write, lost)
    if n < MIN_REWRITE_TOKENS or n < REWRITE_SHARE * min(prev.usage.context, u.context):
        return 0
    return n


def _shape_hash(thread: Thread, r: Request) -> str | None:
    e = api_request_for(thread, r)
    h = e.attrs.get("shape_hash") if e is not None else None
    return h if isinstance(h, str) and h else None


def _last_shape(thread: Thread, before: datetime) -> Event | None:
    found = None
    for e in thread.events_between(datetime.min.replace(tzinfo=before.tzinfo), before):
        if e.kind is EventKind.TOOL_SET and e.attrs.get("source") == SHAPE_SOURCE:
            found = e
    return found


def _ttl(thread: Thread, r: Request) -> str | None:
    e = api_request_for(thread, r)
    ttl = e.attrs.get("cache_ttl") if e is not None else None
    return ttl if isinstance(ttl, str) and ttl else None


def _tool_wait(rw: Rewrite) -> tuple[Event, timedelta] | None:
    """The tool result the pause before `rw` waited for: the last result of the
    previous request's calls, when it came back past the cache lifetime (the agent
    was running a tool, not idle: the cache expired while it ran)."""
    p, r = rw.previous, rw.request
    ids = {c.tool_use_id for c in p.tool_calls}
    if not ids or rw.lifetime is None:
        return None
    results = [
        e
        for e in rw.member.thread.events_between(p.timestamp, r.timestamp, (EventKind.TOOL_RESULT,))
        if e.attrs.get("tool_use_id") in ids
    ]
    if not results:
        return None
    last = results[-1]
    ran = last.timestamp - p.timestamp
    if ran <= rw.lifetime:
        return None
    return last, ran


def _expired(rw: Rewrite, confidence: Confidence, how: str, wrote: Evidence) -> None:
    """A rewrite after a gap past the cache lifetime, named by who was waiting
    (`shared.expiry_kind`): the agent on its own tool call, a finished subagent on
    its lead, or the user."""
    m, p, r = rw.member, rw.previous, rw.request
    lifetime = rw.lifetime or ONE_HOUR
    wait = _tool_wait(rw)
    sub = shared.expiry_kind(
        rw.gap, lifetime, wait[1] if wait else None, m.thread.agent_id is not None
    )
    rw.reason = shared.EXPIRY_CAUSES.get(sub or "", CauseKind.IDLE_REWRITE)
    rw.confidence = confidence
    rw.evidence = [
        _ev(f"Previous {_who(m)} request at {fmt.clock(p.timestamp)}", m, p),
        _ev(
            f"Gap of {fmt.duration(rw.gap)}; cache lifetime {fmt.lifetime(lifetime)} ({how})",
            m,
            r,
        ),
    ]
    if wait is not None:
        e, ran = wait
        call = next(
            (c for c in p.tool_calls if c.tool_use_id == e.attrs.get("tool_use_id")),
            p.tool_calls[0],
        )
        rw.waited_on = call.name or "a tool"
        rw.evidence.append(
            _ev(f"The {rw.waited_on} call ran for {fmt.duration(ran)} before its result", m, e=e)
        )
    elif rw.reason is CauseKind.LATE_RESUME_EXPIRY:
        rw.evidence.append(
            _ev(
                f"The {_who(m)} had finished its turn at {fmt.clock(p.timestamp)} and was "
                f"resumed {fmt.duration(rw.gap)} later",
                m,
                p,
            )
        )
    rw.evidence.append(wrote)


def _lifetime(sess: SessionData, th: Thread, p: Request) -> tuple[timedelta, str, bool]:
    """(lifetime of the cache `p` left, how capcom knows it, whether it is known)."""
    delta, ttl = sess.cache_lifetime(th, p)
    if ttl is not None:
        return delta, f"cacheControl ttl {ttl}", True
    u = p.usage
    if u.cache_write_1h > 0:
        return ONE_HOUR, "the previous request wrote 1-hour entries", True
    if u.cache_write_5m > 0:
        d = ttl_delta("5m")
        assert d is not None
        return d, "the previous request wrote 5-minute entries", True
    if delta >= ONE_HOUR:
        return delta, "assumed: the session wrote 1-hour entries", False
    return delta, "assumed: no cacheControl record", False


def classify(rw: Rewrite) -> None:
    """Rules 1-4 (and the rewrite half of rule 2): why `rw` rewrote its cache."""
    m, r, p = rw.member, rw.request, rw.previous
    th = m.thread
    evs = th.events_between(p.timestamp, r.timestamp)
    wrote = _ev(
        f"The request at {fmt.clock(r.timestamp)} wrote {fmt.exact(r.usage.cache_write)} "
        "tokens to the cache",
        m,
        r,
    )
    rw.gap = r.timestamp - p.timestamp

    comps = [e for e in evs if e.kind is EventKind.COMPACTION]
    if comps:
        e = comps[-1]
        rw.reason, rw.confidence = CauseKind.COMPACTION, Confidence.CLEAR
        rw.evidence = [*_compaction_lines(m, e), wrote]
        return

    lifetime, how, known = _lifetime(m.session, th, p)
    rw.lifetime = lifetime
    expired = rw.gap > lifetime
    # a pause past a known lifetime rewrites the cache whatever else changed in it
    if expired and (known or rw.gap > ONE_HOUR):
        _expired(rw, Confidence.CLEAR, how, wrote)
        return

    if not rw.same_model:
        models = [e for e in evs if e.kind is EventKind.MODEL]
        rw.reason, rw.confidence = CauseKind.MODEL_SWITCH, Confidence.CLEAR
        rw.evidence = [
            _ev(
                f"message.model changed from {p.model} to {r.model} at {fmt.clock(r.timestamp)}",
                m,
                r,
            ),
        ]
        if models:
            rw.evidence.append(
                _ev(
                    f"A model attachment at {fmt.clock(models[-1].timestamp)} "
                    f"set {models[-1].attrs.get('model') or 'a model'}",
                    m,
                    e=models[-1],
                )
            )
        rw.evidence.append(wrote)
        return

    ttl_prev, ttl_now = _ttl(th, p), _ttl(th, r)
    ttl_changed = bool(ttl_prev and ttl_now and ttl_prev != ttl_now)
    h_prev, h_now = _shape_hash(th, p), _shape_hash(th, r)
    shapes = [
        e for e in evs if e.kind is EventKind.TOOL_SET and e.attrs.get("source") == SHAPE_SOURCE
    ]
    shape_changed = bool(
        (h_prev and h_now and h_prev != h_now)
        or (shapes and h_prev and shapes[-1].attrs.get("shape_hash") not in (None, h_prev))
    )
    if ttl_changed or shape_changed:
        rw.reason, rw.confidence = CauseKind.TOOL_SET_CHANGE, Confidence.CLEAR
        rw.evidence = []
        if ttl_prev and ttl_now and ttl_changed:
            rw.ttl_change = (ttl_prev, ttl_now)
            rw.evidence.append(
                _ev(
                    f"cacheControl ttl changed from {ttl_prev} to {ttl_now} between the "
                    f"requests at {fmt.clock(p.timestamp)} and {fmt.clock(r.timestamp)}",
                    m,
                    r,
                )
            )
        if shape_changed:
            rw.shape_change = True
            rw.evidence.append(
                _ev(
                    f"shapeHash changed between the requests at {fmt.clock(p.timestamp)} and "
                    f"{fmt.clock(r.timestamp)}",
                    m,
                    r,
                )
            )
            if shapes:
                rw.evidence.extend(_shape_diff(m, shapes[-1], p.timestamp))
        rw.evidence.append(wrote)
        return

    if expired:
        _expired(rw, Confidence.LIKELY, how, wrote)
        return
    # a gap within the cache lifetime is never an expiry, whatever else it holds (an
    # away_summary too): the cache was still there
    deltas = [
        e
        for e in evs
        if e.kind is EventKind.TOOL_SET
        and e.attrs.get("source") in ATTACHMENT_SOURCES
        and e.attrs.get("is_initial") is not True
    ]
    if deltas:
        rw.reason, rw.confidence = CauseKind.TOOL_SET_CHANGE, Confidence.LIKELY
        rw.evidence = [_delta_line(m, e) for e in deltas[-3:]] + [wrote]
        return
    # a prompt that shrank with no compact_boundary and none of the causes above (a
    # tool or connector removed also shrinks it): something cut the context early
    # on (Claude Code drops old tool results without logging a compaction), so the
    # cache is rebuilt from the cut on
    drop = p.usage.context - r.usage.context
    if drop >= TRIM_MIN_TOKENS and drop >= TRIM_SHARE * p.usage.context:
        rw.reason, rw.confidence, rw.trimmed = CauseKind.COMPACTION, Confidence.LIKELY, True
        rw.evidence = [
            _ev(
                f"The prompt went from {fmt.approx(p.usage.context)} to "
                f"{fmt.approx(r.usage.context)} tokens between the requests at "
                f"{fmt.clock(p.timestamp)} and {fmt.clock(r.timestamp)}, with no "
                "compact_boundary between them",
                m,
                r,
            ),
            _ev(f"Only the first {fmt.approx(r.usage.cache_read)} were read from the cache", m, r),
            wrote,
        ]
        return
    rw.reason, rw.confidence = CauseKind.UNEXPLAINED, Confidence.UNEXPLAINED
    rw.evidence = [
        _ev(
            f"The request at {fmt.clock(r.timestamp)} wrote {fmt.exact(rw.tokens)} tokens "
            f"to the cache {fmt.duration(rw.gap)} after the previous one, with no pause, "
            "compaction, model or tool change before it",
            m,
            r,
        )
    ]
    if r.usage.context < p.usage.context:
        rw.evidence.append(
            _ev(
                f"The prompt went from {fmt.approx(p.usage.context)} to "
                f"{fmt.approx(r.usage.context)} tokens and only its first "
                f"{fmt.approx(r.usage.cache_read)} were read from the cache: something "
                "early in the context changed",
                m,
                r,
            )
        )


def _compaction_lines(m: Member, e: Event) -> list[Evidence]:
    trig = e.attrs.get("trigger")
    lines = [
        _ev(
            f"compact_boundary at {fmt.clock(e.timestamp)}"
            + (f", trigger {trig}" if isinstance(trig, str) else ""),
            m,
            e=e,
        )
    ]
    pre, post = _int(e.attrs.get("pre_tokens")), _int(e.attrs.get("post_tokens"))
    if pre or post:
        lines.append(_ev(f"preTokens {fmt.exact(pre)}, postTokens {fmt.exact(post)}", m, e=e))
    return lines


def _shape_diff(m: Member, shape: Event, before: datetime) -> list[Evidence]:
    prev = _last_shape(m.thread, before)
    if prev is None:
        return []
    old, new = set(_names(prev.attrs.get("names"))), _names(shape.attrs.get("names"))
    added = [n for n in new if n not in old]
    removed = sorted(old - set(new))
    parts = []
    if added:
        parts.append(f"added {_list(added)}")
    if removed:
        parts.append(f"removed {_list(removed)}")
    if not parts:
        return []
    return [_ev(f"Tool list at {fmt.clock(shape.timestamp)}: " + "; ".join(parts), m, e=shape)]


def _delta_line(m: Member, e: Event) -> Evidence:
    source = str(e.attrs.get("source"))
    added, removed = _names(e.attrs.get("added")), _names(e.attrs.get("removed"))
    if source == "skill_listing":
        n = len(_names(e.attrs.get("names")))
        what = f"{n} skills listed" if n else "skills listed again"
    else:
        bits = []
        if added:
            bits.append(f"added {_list(added)}")
        if removed:
            bits.append(f"removed {_list(removed)}")
        what = "; ".join(bits) or "a change"
    return _ev(f"{source} at {fmt.clock(e.timestamp)} mid-session: {what}", m, e=e)


def find_rewrites(view: SpikeView) -> list[Rewrite]:
    out = []
    for m in view.members:
        for r in m.requests:
            p = m.thread.previous(r)
            n = rewritten_tokens(r, p)
            if p is None or n == 0:
                continue
            rw = Rewrite(m, r, p, n, 0.0)
            rw.same_model = view.pricer.same_model(r.model, p.model)
            classify(rw)
            writes = _writes(view.ledger.left(r), n)
            if rw.reason is CauseKind.UNEXPLAINED:
                rw.excess = view.pricer.rewrite_value(r, writes)  # not a cause: claims nothing
            else:
                rw.excess = view.pricer.rewrite_value(r, view.ledger.claim(r, writes, rw.reason))
            out.append(rw)
    return out


_REWRITE_ADVICE = {
    CauseKind.MODEL_SWITCH: (
        "Switch models between sessions, not inside one. For a cheaper side task, spawn a "
        "subagent on the other model instead."
    ),
    CauseKind.TOOL_SET_CHANGE: (
        "Settle tools, connectors and skills before the session starts: turning one on or "
        "off mid-session rewrites the whole cache."
    ),
    CauseKind.COMPACTION: (
        "A compaction always writes a fresh cache; it costs least when it runs early. "
        "Compact or hand off before the context grows large."
    ),
}
_TRIM_ADVICE = (
    "Trimming the context (old tool results dropped, no compaction logged) rewrites the "
    "cache from the cut on, and costs most on a large context. Compact or hand off "
    "before the context grows large."
)


EXPIRY_CAUSES = frozenset(shared.EXPIRY_SUB_KINDS)


def _pause(life: timedelta) -> str:
    return "an hour" if life >= ONE_HOUR else fmt.lifetime(life)


def _tools_waited(group: Sequence[Rewrite]) -> str:
    """The one tool name the group's waits share, or "tools"."""
    names = {rw.waited_on for rw in group if rw.waited_on}
    return next(iter(names)) if len(names) == 1 else "tools"


def _expiry_text(reason: CauseKind, group: list[Rewrite], top: Rewrite) -> tuple[str, str]:
    """Title and advice of cache expiries, by who waited (`shared.expiry_kind`)."""
    k = len(group)
    life = top.lifetime or ONE_HOUR
    pause = _pause(life)
    if reason is CauseKind.TOOL_WAIT_EXPIRY:
        tool = _tools_waited(group)
        title = (
            f"Cache expired while {top.waited_on or 'a tool'} ran {fmt.duration(top.gap)}"
            if k == 1
            else f"Cache expired {k} times while {tool} ran"
        )
        advice = (
            f"A tool call that runs longer than {pause} comes back to an expired cache "
            "and rewrites the whole context. Run long jobs in the background and check on "
            "them with short calls, or wait in an agent with a small context."
        )
        if any(rw.waited_on in AGENT_TOOLS for rw in group):
            advice += (
                " A foreground Agent call keeps its caller waiting the same way: run long "
                "subagents in the background."
            )
        if any(rw.member.thread.agent_id is None for rw in group):
            advice += (
                " A command waiting on a permission prompt looks the same in the logs: "
                "allow it in the settings, or approve it sooner."
            )
        return title, advice
    if reason is CauseKind.LATE_RESUME_EXPIRY:
        title = (
            f"Subagent resumed after the cache expired ({fmt.duration(top.gap)} later)"
            if k == 1
            else f"Subagents resumed {k} times after the cache expired"
        )
        advice = (
            f"A subagent that finished and is resumed more than {pause} later rewrites its "
            "whole context. For the next step, spawn a fresh agent with a short note of "
            f"what it needs, or reply to the finished one within {pause}."
        )
        return title, advice
    title = (
        f"Cache rebuilt after {fmt.duration(top.gap)} idle"
        if k == 1
        else f"Cache rebuilt {k} times after idling"
    )
    advice = (
        f"A pause longer than {pause} costs one full rewrite of the context. Before a "
        "long break, compact or end the session with a handoff note."
    )
    if any(rw.lifetime is not None and rw.lifetime < ONE_HOUR for rw in group):
        advice += (
            f" This thread cached for {fmt.lifetime(life)}: with the 1-hour cache lifetime, "
            "breaks under an hour would read the cache instead."
        )
    return title, advice


def _expiry_line(rw: Rewrite) -> Evidence:
    """One rewrite of a group of expiries, on one line."""
    m, r = rw.member, rw.request
    if rw.reason is CauseKind.TOOL_WAIT_EXPIRY:
        why = f"while its {rw.waited_on or 'tool'} call ran"
    elif rw.reason is CauseKind.LATE_RESUME_EXPIRY:
        why = "after it had finished"
    else:
        why = "idle"
    life = fmt.lifetime(rw.lifetime or ONE_HOUR)
    return _ev(
        f"{_who(m).capitalize()} at {fmt.clock(r.timestamp)}: {fmt.duration(rw.gap)} {why} "
        f"(lifetime {life}), {fmt.approx(rw.tokens)} tokens written again",
        m,
        r,
    )


def rewrite_candidates(view: SpikeView) -> list[Candidate]:
    by_reason: dict[tuple[CauseKind, bool], list[Rewrite]] = {}
    for rw in view.rewrites:
        if rw.reason is not CauseKind.UNEXPLAINED:
            by_reason.setdefault((rw.reason, rw.trimmed), []).append(rw)
    out = []
    for (reason, trimmed), group in by_reason.items():
        excess = sum(rw.excess for rw in group)
        top = max(group, key=lambda rw: rw.excess)
        k = len(group)
        if reason in EXPIRY_CAUSES:
            title, advice = _expiry_text(reason, group, top)
        elif reason is CauseKind.MODEL_SWITCH:
            title = "Model switch rewrote the cache" + (f" {k} times" if k > 1 else "")
            advice = _REWRITE_ADVICE[reason]
        elif reason is CauseKind.TOOL_SET_CHANGE and all(rw.ttl_change for rw in group):
            a, b = top.ttl_change or ("", "")
            title = f"Cache lifetime changed from {a} to {b}" + (f" {k} times" if k > 1 else "")
            advice = (
                "Entries cached with one lifetime are not read by a request asking for "
                "another, so a change of the cache lifetime mid-session rewrites the whole "
                "context. Keep one lifetime for the session."
            )
            if any(rw.shape_change for rw in group):
                advice += " The tool list or prompt changed too: " + _REWRITE_ADVICE[reason]
        elif reason is CauseKind.TOOL_SET_CHANGE:
            title = "Tool or prompt change rewrote the cache" + (f" {k} times" if k > 1 else "")
            advice = _REWRITE_ADVICE[reason]
        elif trimmed:
            title = "Context trimmed without a compaction record, cache rebuilt" + (
                f" {k} times" if k > 1 else ""
            )
            advice = _TRIM_ADVICE
        else:
            title = "Cache rewritten after a compaction" + (f" {k} times" if k > 1 else "")
            advice = _REWRITE_ADVICE[CauseKind.COMPACTION]
        ordered = sorted(group, key=lambda rw: rw.request.timestamp)
        if reason in EXPIRY_CAUSES and k > 1:
            evidence = [_expiry_line(rw) for rw in ordered]
        else:
            evidence = [e for rw in ordered for e in rw.evidence]
        out.append(
            Candidate(
                reason,
                _confidence((rw.confidence, rw.excess) for rw in group),
                excess,
                title,
                _cap(evidence),
                advice,
                _rewrite_summary(reason, group, excess),
            )
        )
    return out


def _rewrite_summary(reason: CauseKind, group: list[Rewrite], excess: float) -> str:
    top = max(group, key=lambda rw: rw.excess)
    tokens = sum(rw.tokens for rw in group)
    who = _who(top.member)
    if len(group) == 1:
        lead = f"The {who} request at {fmt.hm(top.request.timestamp)}"
    else:
        lead = f"{fmt.count_word(len(group), True)} requests"
    if reason in EXPIRY_CAUSES:
        life = fmt.lifetime(top.lifetime or ONE_HOUR)
        # the longest pause, which need not be the costliest rewrite
        lg = max(group, key=lambda rw: rw.gap)
        if reason is CauseKind.TOOL_WAIT_EXPIRY:
            tool = lg.waited_on or "a tool"
            why = (
                f"came {fmt.duration(top.gap)} after the previous one, once its {tool} call "
                f"had run past the cache lifetime of {life}"
                if len(group) == 1
                else f"came after tool calls ran past the cache lifetime of {life} (the "
                f"longest, {tool}, {fmt.duration(lg.gap)})"
            )
        elif reason is CauseKind.LATE_RESUME_EXPIRY:
            why = (
                f"came when its lead resumed it {fmt.duration(top.gap)} after it had "
                f"finished, past its cache lifetime of {life}"
                if len(group) == 1
                else "came when finished subagents were resumed past their cache lifetime "
                f"(the longest after {fmt.duration(lg.gap)})"
            )
        else:
            why = (
                f"came {fmt.duration(top.gap)} after the previous one, past its cache "
                f"lifetime of {life}"
                if len(group) == 1
                else "came after pauses past the cache lifetime (the longest "
                f"{fmt.duration(lg.gap)})"
            )
    elif reason is CauseKind.COMPACTION and top.trimmed:
        why = (
            f"came after the prompt shrank from {fmt.approx(top.previous.usage.context)} to "
            f"{fmt.approx(top.request.usage.context)} tokens with no compaction record"
            if len(group) == 1
            else "came after the prompt shrank with no compaction record"
        )
    elif reason is CauseKind.COMPACTION:
        why = "followed a compaction"
    elif reason is CauseKind.MODEL_SWITCH:
        why = f"switched from {top.previous.model} to {top.request.model}"
    elif top.ttl_change:
        a, b = top.ttl_change
        why = f"asked for a {b} cache where the previous one asked for {a}"
        if top.shape_change:
            why += ", with a changed tool list or prompt"
    else:
        why = "followed a change of the tool list or the prompt"
    return (
        f"{lead} {why}, so {fmt.approx(tokens)} tokens the cache already held were "
        f"written again. Reading them would have cost {fmt.usd(excess)} less."
    )


# --------------------------------------------------------------- rule 2: late compaction


_LATE_COMPACTION_ADVICE = (
    f"Compact or hand off by about {fmt.approx(LONG_CONTEXT)} tokens, well before "
    f"{fmt.approx(LATE_COMPACTION)}: past that, each turn costs about twice what it did, "
    "and the work rarely needs the oldest half of the context."
)
_GROWTH_ADVICE = (
    f"One task per agent. Hand off, /clear or compact by about {fmt.approx(LONG_CONTEXT)} "
    "tokens: past that, every turn rereads all of it."
)


def late_compaction(view: SpikeView) -> list[Candidate]:
    out = []
    for m in view.members:
        th = m.thread
        first = m.requests[0].timestamp
        lo = min(first, view.spike.start) - timedelta(microseconds=1)
        for e in th.events_between(lo, view.window_end + COMPACTION_REACH, (EventKind.COMPACTION,)):
            before = [r for r in m.requests if r.timestamp <= e.timestamp]
            pre = _int(e.attrs.get("pre_tokens")) or max(
                (r.usage.context for r in before), default=0
            )
            if pre < LATE_COMPACTION:
                continue
            heavy = [r for r in before if r.usage.context > LATE_COMPACTION]
            excess = sum(
                view.ledger.take_prompt(
                    r, r.usage.context - LATE_COMPACTION, False, CauseKind.COMPACTION
                )
                for r in heavy
            )
            if excess <= 0:
                continue
            mean = statistics.fmean(r.usage.context for r in heavy)
            evidence = [
                *_compaction_lines(m, e),
                _ev(
                    f"{len(heavy)} turns above {fmt.approx(LATE_COMPACTION)}, "
                    f"averaging {fmt.approx(mean)}",
                    m,
                    heavy[0],
                ),
            ]
            after = th.requests_between(e.timestamp, e.timestamp + timedelta(days=365))
            if after and after[0].usage.cache_write:
                evidence.append(
                    _ev(
                        f"The next request wrote {fmt.approx(after[0].usage.cache_write)} "
                        "tokens to the cache",
                        m,
                        after[0],
                    )
                )
            out.append(
                Candidate(
                    CauseKind.COMPACTION,
                    Confidence.CLEAR,
                    excess,
                    f"Compaction ran late, at {fmt.approx(pre)} tokens",
                    evidence,
                    _LATE_COMPACTION_ADVICE,
                    f"The {_who(m)} compacted at {fmt.approx(pre)} tokens, after "
                    f"{len(heavy)} turns above {fmt.approx(LATE_COMPACTION)} "
                    f"(about {fmt.approx(mean)} each). The context above "
                    f"{fmt.approx(LATE_COMPACTION)} cost {fmt.usd(excess)} on those "
                    "turns.",
                )
            )
    return out


# --------------------------------------------------------------------------- rules 5 and 8: results


@dataclass
class _Result:
    member: Member
    event: Event
    tokens: int
    excess: float
    writer: Request | None
    rereads: int
    until_compaction: bool


def _results(view: SpikeView, agent: bool) -> list[_Result]:
    out = []
    for m in view.members:
        th = m.thread
        first = m.requests[0].timestamp
        lo = view.spike.start - RESULT_LOOKBACK
        comps = th.events_between(lo, first, (EventKind.COMPACTION,))
        if comps:
            lo = comps[-1].timestamp
        ids = {r.message_id for r in m.requests}
        for e in th.events_between(lo, view.window_end, (EventKind.TOOL_RESULT,)):
            tool = e.attrs.get("tool")
            is_agent = tool in AGENT_TOOLS
            if is_agent != agent or e.attrs.get("is_error") is True:
                continue
            n = _int(e.attrs.get("size_tokens"))
            if n < (LONG_REPORT_TOKENS if agent else LARGE_RESULT_TOKENS):
                continue
            nxt = th.events_between(
                e.timestamp,
                datetime.max.replace(tzinfo=e.timestamp.tzinfo),
                (EventKind.COMPACTION,),
            )
            end = nxt[0].timestamp if nxt else datetime.max.replace(tzinfo=e.timestamp.tzinfo)
            later = [r for r in th.requests_between(e.timestamp, end) if r.timestamp > e.timestamp]
            cause = CauseKind.LONG_REPORT if agent else CauseKind.LARGE_TOOL_RESULT
            excess = 0.0
            for i, r in enumerate(later):
                if r.message_id in ids:
                    excess += view.ledger.take_prompt(r, n, i == 0, cause)
            out.append(
                _Result(
                    m, e, n, excess, later[0] if later else None, max(0, len(later) - 1), bool(nxt)
                )
            )
    return out


def _result_lines(res: _Result, what: str) -> list[Evidence]:
    m = res.member
    lines = [_ev(what, m, e=res.event)]
    if res.writer is not None:
        lines.append(
            _ev(f"Written to the cache at {fmt.clock(res.writer.timestamp)}", m, res.writer)
        )
    tail = "until the compaction" if res.until_compaction else "so far"
    lines.append(_ev(f"Reread by {res.rereads} later requests {tail}", m, e=res.event))
    return lines


def _result_advice(tool: str | None, image: bool) -> str:
    if image:
        return "Crop or downscale screenshots before they enter the conversation."
    if tool == "Bash":
        return (
            "Pipe long command output through tail, grep or a file, and read only the "
            "part you need."
        )
    if tool == "Read":
        return (
            "Read a slice (offset and limit) or grep the file first. Large outputs "
            "belong in a file the agent searches, not in the conversation."
        )
    return "Large outputs belong in a file the agent searches, not in the conversation."


def large_results(view: SpikeView) -> list[Candidate]:
    found = [r for r in _results(view, agent=False) if r.excess > 0]
    if not found:
        return []
    found.sort(key=lambda r: -r.excess)
    top = found[0]
    evidence = []
    for res in found:
        a = res.event.attrs
        tool, target = _str(a.get("tool")), _str(a.get("target"))
        if a.get("is_image") is True:
            what = f"{tool or 'A tool'} returned an image of about {fmt.approx(res.tokens)} tokens"
        else:
            what = (
                f"{fmt.tool_label(tool, target)} returned about {fmt.approx(res.tokens)} "
                "tokens (estimated from its size)"
            )
        evidence.extend(_result_lines(res, what))
    a = top.event.attrs
    title = (
        f"A {fmt.approx(top.tokens)}-token tool result stayed in context"
        if len(found) == 1
        else f"{len(found)} large tool results stayed in context "
        f"({fmt.approx(sum(r.tokens for r in found))} tokens)"
    )
    tool = _str(a.get("tool"))
    excess = sum(r.excess for r in found)
    label = fmt.tool_label(tool, _str(a.get("target")))
    if len(found) == 1:
        summary = (
            f"{label} returned about {fmt.approx(top.tokens)} tokens at "
            f"{fmt.hm(top.event.timestamp)}. Writing it to the cache and rereading it on "
            f"{top.rereads} later requests cost {fmt.usd(excess)}."
        )
    else:
        summary = (
            f"{len(found)} tool results above {fmt.approx(LARGE_RESULT_TOKENS)} tokens, the "
            f"largest from {label}, were written to the cache and reread by later "
            f"requests, for {fmt.usd(excess)}."
        )
    return [
        Candidate(
            CauseKind.LARGE_TOOL_RESULT,
            Confidence.LIKELY,
            excess,
            title,
            _cap(evidence),
            _result_advice(tool, a.get("is_image") is True),
            summary,
        )
    ]


def long_reports(view: SpikeView) -> list[Candidate]:
    found = [r for r in _results(view, agent=True) if r.excess > 0]
    if not found:
        return []
    found.sort(key=lambda r: -r.excess)
    evidence = []
    for res in found:
        tid = res.event.attrs.get("tool_use_id")
        kind = res.event.attrs.get("target")
        if not isinstance(kind, str) and isinstance(tid, str):
            kind = res.member.session.agent_type(res.member.session.agent_by_tool_use.get(tid))
        who = f"{kind} subagent" if isinstance(kind, str) and kind else "A subagent"
        what = f"{who} reported about {fmt.approx(res.tokens)} tokens (estimated from its size)"
        evidence.extend(_result_lines(res, what))
    top = found[0]
    title = (
        f"A {fmt.approx(top.tokens)}-token subagent report stayed in context"
        if len(found) == 1
        else f"{len(found)} long subagent reports stayed in context"
    )
    excess = sum(r.excess for r in found)
    reports = "report" if len(found) == 1 else f"{len(found)} reports"
    return [
        Candidate(
            CauseKind.LONG_REPORT,
            Confidence.LIKELY,
            excess,
            title,
            _cap(evidence),
            "Ask subagents for a short report and to put the details in a file the parent "
            "reads only when it needs them.",
            f"The subagent {reports} ({fmt.approx(sum(r.tokens for r in found))} tokens) "
            f"stayed in the parent's context and were reread by its later requests, for "
            f"{fmt.usd(excess)}.",
        )
    ]


# --------------------------------------------------------------------------- rule 6: growth


def context_growth(view: SpikeView) -> list[Candidate]:
    grown: list[tuple[Member, int, int, float]] = []  # member, ref, peak, excess
    for m in view.members:
        th = m.thread
        ids = {r.message_id for r in m.requests}
        before = [
            r
            for r in th.requests_between(view.spike.start - BASELINE_WINDOW, view.spike.start)
            if r.message_id not in ids
        ]
        ref = (
            int(statistics.median(r.usage.context for r in before))
            if before
            else (m.requests[0].usage.context)
        )
        peak = max(r.usage.context for r in m.requests)
        if peak < GROWTH_FLOOR or peak < GROWTH_RATIO * max(ref, 1):
            continue
        excess = 0.0
        for r in m.requests:
            extra = r.usage.context - ref - view.ledger.prompt_claimed(r)
            if extra > 0:
                excess += view.ledger.take_prompt(r, extra, False, CauseKind.CONTEXT_GROWTH)
        if excess > 0:
            grown.append((m, ref, peak, excess))
    if not grown:
        return []
    excess = sum(g[3] for g in grown)
    reads = sum(r.usage.cache_read for g in grown for r in g[0].requests)
    ratio = max(g[2] / max(g[1], 1) for g in grown)
    evidence: list[Evidence] = []
    if len(grown) == 1:
        m, ref, peak, _ = grown[0]
        title = f"Context grew from {fmt.approx(ref)} to {fmt.approx(peak)} tokens"
        evidence.append(
            _ev(
                f"Context per request rose from {fmt.approx(ref)} to {fmt.approx(peak)}",
                m,
                max(m.requests, key=lambda r: r.usage.context),
            )
        )
        for thr in CONTEXT_STEPS:
            if ref < thr <= peak:
                r = next(r for r in m.requests if r.usage.context >= thr)
                evidence.append(_ev(f"Crossed {fmt.approx(thr)} at {fmt.clock(r.timestamp)}", m, r))
        evidence.append(_ev(f"{fmt.approx(reads)} cache reads over {len(m.requests)} requests", m))
    else:
        peaks = [g[2] for g in grown]
        past = [t for t in CONTEXT_STEPS if all(p >= t for p in peaks)]
        agents = len(grown)
        title = (
            f"{fmt.count_word(agents, True)} agents past {fmt.approx(past[-1])} context"
            if past
            else f"{fmt.count_word(agents, True)} agents' contexts grew"
        )
        starts = [g[0].requests[0].timestamp for g in grown]
        longest = max(g[0].requests[-1].timestamp - g[0].requests[0].timestamp for g in grown)
        m0 = grown[0][0]
        evidence.append(
            _ev(
                f"{agents} agents active from {fmt.hm(min(starts))}, the longest for "
                f"{fmt.duration(longest)}",
                m0,
            )
        )
        evidence.append(
            _ev(
                f"Context per request rose from {fmt.approx(min(g[1] for g in grown))} to "
                f"{fmt.approx(min(peaks))}\u2013{fmt.approx(max(peaks))}",
                m0,
            )
        )
        evidence.append(_ev(f"{fmt.approx(reads)} cache reads in the window", m0))
    if not any(rw.reason is CauseKind.COMPACTION and not rw.trimmed for rw in view.rewrites):
        evidence.append(_ev("No compaction in the window", grown[0][0]))
    conf = Confidence.CLEAR if ratio >= GROWTH_CLEAR_RATIO else Confidence.LIKELY
    turns = sum(len(g[0].requests) for g in grown)
    if len(grown) == 1:
        m, ref, peak, _ = grown[0]
        summary = (
            f"The {_who(m)} context grew from {fmt.approx(ref)} to {fmt.approx(peak)} tokens "
            f"over {turns} requests, and each one reread all of it. The context above the "
            f"hour before cost {fmt.usd(excess)}."
        )
    else:
        summary = (
            f"{len(grown)} agents ran with growing contexts over {turns} requests, each "
            f"rereading all of it. The context above each agent's hour before cost "
            f"{fmt.usd(excess)}."
        )
    return [
        Candidate(
            CauseKind.CONTEXT_GROWTH,
            conf,
            excess,
            title,
            _cap(evidence),
            _GROWTH_ADVICE,
            summary,
        )
    ]


# --------------------------------------------------------------------------- rule 7: fan-out


def _bursts(times: Sequence[datetime]) -> list[int]:
    """The positions of `times` (in order) that fall in a window of FAN_OUT_BURST
    holding at least FAN_OUT_MIN of them."""
    keep: set[int] = set()
    j = 0
    for i, t in enumerate(times):
        while t - times[j] > FAN_OUT_BURST:
            j += 1
        if i - j + 1 >= FAN_OUT_MIN:
            keep.update(range(j, i + 1))
    return sorted(keep)


def burst(spawns: list[tuple[Thread, Event]]) -> list[tuple[Thread, Event]]:
    """The spawns (in time order) that fall in a window of FAN_OUT_BURST holding at
    least FAN_OUT_MIN of them."""
    return [spawns[k] for k in _bursts([e.timestamp for _, e in spawns])]


def _spawned(sess: SessionData, spawns: list[tuple[Thread, Event]]) -> dict[str, Event]:
    """The agents of `spawns`, each with its spawn: by the spawn's tool_use_id, else
    the first agent with no known spawn whose first request follows the spawn within
    FAN_OUT_START."""
    agents: dict[str, Event] = {}
    unmapped: list[Event] = []
    for _, e in spawns:
        tid = e.attrs.get("tool_use_id")
        if isinstance(tid, str) and tid in sess.agent_by_tool_use:
            agents[sess.agent_by_tool_use[tid]] = e
        else:
            unmapped.append(e)
    for e in unmapped:
        starts = sorted(
            (th.requests[0].timestamp, aid)
            for aid, th in sess.threads.items()
            if aid is not None
            and aid not in agents
            and not (aid in sess.agents and sess.agents[aid].tool_use_id)
            and th.requests
            and e.timestamp <= th.requests[0].timestamp <= e.timestamp + FAN_OUT_START
        )
        if starts:
            agents[starts[0][1]] = e
    return agents


# What fan-out adds is each new agent's prefix: the system prompt, tools and task its
# first request writes to a cache (or sends uncached). The agents' later turns are
# their own work, which the parent would have done in its own context, rereading a
# context at least as large: they are left to the other rules (idle rewrites, context
# growth...) and, when none claims them, are not counted as excess of fan-out. The
# prefix's rereads are not charged either, for the same reason.
_LOOKUP_ADVICE = (
    "Most of these agents made two tool calls or fewer: do such lookups in the parent "
    "and batch related questions into one agent."
)
_PREFIX_ADVICE = (
    "Each agent writes its own prefix (system prompt and tools) before its first turn: "
    "give these agents a narrower tool list so the prefix is smaller, and run fewer of "
    "them at once."
)
_WORKFLOW_ADVICE = (
    "A workflow starts its agents in parallel without an Agent call: check how many it "
    "runs at once, and give its agents a narrower tool list so each prefix is smaller."
)


def _claim_prefixes(view: SpikeView, members: Sequence[Member]) -> tuple[float, int, float]:
    """Claim each member agent's prefix (the cache writes and uncached input of its
    thread's first request, when that request is in the spike) as FAN_OUT; return the
    dollars and tokens claimed, and what the members' requests in the spike cost."""
    usd = 0.0
    tokens = 0
    for m in members:
        first = m.thread.requests[0] if m.thread.requests else None
        for r in m.requests:
            if r is not first:
                continue
            left = view.ledger.left(r)
            got = view.ledger.claim(
                r,
                Usage(
                    input=left.input,
                    cache_write_5m=left.cache_write_5m,
                    cache_write_1h=left.cache_write_1h,
                ),
                CauseKind.FAN_OUT,
            )
            usd += view.pricer.value(r, got)
            tokens += got.input + got.cache_write
    spent = sum(view.pricer.cost(r) for m in members for r in m.requests)
    return usd, tokens, spent


def _fan_out_advice(small: int, agents: int, workflow: bool) -> str:
    """Lookups in the parent only when most agents made two tool calls or fewer;
    otherwise the prefix size and how many run at once."""
    if small * 2 > agents:
        return f"{_LOOKUP_ADVICE} {_WORKFLOW_ADVICE}" if workflow else _LOOKUP_ADVICE
    return _WORKFLOW_ADVICE if workflow else _PREFIX_ADVICE


def _fan_out_summary(lead: str, tokens: int, excess: float, spent: float) -> str:
    return (
        f"{lead} Each wrote its own prefix to a cache: {fmt.approx(tokens)} tokens in all, "
        f"which cost {fmt.usd(excess)}. Their requests in the spike cost {fmt.usd(spent)} "
        "in all; the rest is the agents' own work, not counted as fan-out."
    )


def fan_out(view: SpikeView) -> list[Candidate]:
    if view.spike.scope == "session":
        return session_fan_out(view)
    out = []
    sessions = {m.session.session_id: m.session for m in view.members}
    lo = view.spike.start - FAN_OUT_LOOKBACK
    for sess in sessions.values():
        found: list[tuple[Thread, Event]] = []
        for th in sess.threads.values():
            found.extend(
                (th, e) for e in th.events_between(lo, view.window_end, (EventKind.SPAWN,))
            )
        found.sort(key=lambda x: x[1].timestamp)
        spawns = burst(found)
        if not spawns:
            continue
        t_first, t_last = spawns[0][1].timestamp, spawns[-1][1].timestamp
        agents = _spawned(sess, spawns)
        members = [m for m in view.members if m.session is sess and m.thread.agent_id in agents]
        excess, tokens, spent = _claim_prefixes(view, members)
        if excess <= 0:
            continue
        m0 = next((m for m in view.members if m.session is sess), members[0])
        types = Counter(str(e.attrs.get("agent_type") or "unknown type") for _, e in spawns)
        firsts = [
            sess.threads[a].requests[0].usage.cache_write
            for a in agents
            if a in sess.threads and sess.threads[a].requests
        ]
        small = sum(
            1
            for a in agents
            if a in sess.threads and sum(len(r.tool_calls) for r in sess.threads[a].requests) <= 2
        )
        n = len(spawns)
        span = t_last - t_first
        when = "in under a minute" if span < timedelta(minutes=1) else f"in {fmt.duration(span)}"
        evidence = [
            _ev(
                f"{n} Agent tool calls between {fmt.hm(t_first)} and {fmt.hm(t_last)}",
                m0,
                e=spawns[0][1],
            ),
            _ev(", ".join(f"{k} \u00d7{v}" for k, v in types.most_common(4)), m0),
        ]
        if firsts:
            evidence.append(
                _ev(
                    "Each first request wrote about "
                    f"{fmt.approx(statistics.median(firsts))} tokens",
                    m0,
                )
            )
        if agents:
            evidence.append(_ev(f"{small} of them made two tool calls or fewer", m0))
        conf = Confidence.CLEAR  # the prefixes are writes of agents its Agent calls started
        kinds = ", ".join(f"{v} {k}" for k, v in types.most_common(3))
        out.append(
            Candidate(
                CauseKind.FAN_OUT,
                conf,
                excess,
                f"{n} subagents {when}",
                _cap(evidence),
                _fan_out_advice(small, len(agents), False),
                _fan_out_summary(
                    f"{n} Agent calls {when} from {fmt.hm(t_first)} ({kinds}) started "
                    f"{len(members)} subagent{'s' if len(members) != 1 else ''} active in the "
                    "spike.",
                    tokens,
                    excess,
                    spent,
                ),
            )
        )
    return out


# A session spike sums every thread of its session, so a fan-out shows there as new
# threads: subagents whose first request falls in the span, whether an Agent call
# started them or not (a workflow's agents have no Agent call, hence no SPAWN).
WORKFLOW_AGENT_TYPE = "workflow-subagent"  # the meta.json agentType of a workflow's agents
_ORIGIN_LABELS = {
    "agent": "from Agent calls",
    "workflow": "from a workflow, without an Agent call",
    "none": "without an Agent call",
}


def _new_threads(sess: SessionData, spike: Spike) -> list[tuple[datetime, str]]:
    """(first request time, agent id) of the agent threads that start in the spike and
    fall in a burst of FAN_OUT_MIN within FAN_OUT_BURST, in time order."""
    starts = sorted(
        (th.requests[0].timestamp, aid)
        for aid, th in sess.threads.items()
        if aid is not None and th.requests and spike.start <= th.requests[0].timestamp < spike.end
    )
    return [starts[k] for k in _bursts([t for t, _ in starts])]


def _spawns_of(sess: SessionData, lo: datetime, hi: datetime) -> dict[str, Event]:
    """The SPAWN events of the session in (lo, hi], by the agent they started."""
    found = sorted(
        (
            (th, e)
            for th in sess.threads.values()
            for e in th.events_between(lo, hi, (EventKind.SPAWN,))
        ),
        key=lambda x: x[1].timestamp,
    )
    return _spawned(sess, found)


def _origin(sess: SessionData, agent: str, spawns: dict[str, Event]) -> str:
    """How an agent started: "agent" (an Agent call names it, in its meta or by a
    SPAWN), "workflow" (a workflow's agent), "none" (no call found)."""
    info = sess.agents.get(agent)
    if info is not None and info.tool_use_id:
        return "agent"
    if info is not None and info.agent_type == WORKFLOW_AGENT_TYPE:
        return "workflow"
    return "agent" if agent in spawns else "none"


def session_fan_out(view: SpikeView) -> list[Candidate]:
    """Fan-out on a session spike: FAN_OUT_MIN or more new threads within FAN_OUT_BURST.
    It claims the prefixes of those threads (`_claim_prefixes`)."""
    out = []
    spike = view.spike
    sessions = {m.session.session_id: m.session for m in view.members}
    for sess in sessions.values():
        new = _new_threads(sess, spike)
        if not new:
            continue
        agents = [a for _, a in new]
        wanted = set(agents)
        members = [m for m in view.members if m.session is sess and m.thread.agent_id in wanted]
        excess, tokens, spent = _claim_prefixes(view, members)
        if excess <= 0 or not members:
            continue
        t_first, t_last = new[0][0], new[-1][0]
        lo = t_first - FAN_OUT_LOOKBACK
        spawns = _spawns_of(sess, lo, view.window_end)
        types = Counter(
            sess.agent_type(a) or _str(spawns[a].attrs.get("agent_type")) or "unknown type"
            if a in spawns
            else sess.agent_type(a) or "unknown type"
            for a in agents
        )
        origins = Counter(_origin(sess, a, spawns) for a in agents)
        n = len(agents)
        span = t_last - t_first
        when = "in under a minute" if span < timedelta(minutes=1) else f"in {fmt.duration(span)}"
        m0 = members[0]
        first0 = m0.thread.requests[0]
        firsts = [sess.threads[a].requests[0].usage.cache_write for a in agents]
        small = sum(
            1 for a in agents if sum(len(r.tool_calls) for r in sess.threads[a].requests) <= 2
        )
        mix = ", ".join(f"{k} \u00d7{v}" for k, v in types.most_common(4))
        how = "; ".join(
            f"{v} {_ORIGIN_LABELS[k]}" for k, v in sorted(origins.items(), key=lambda kv: -kv[1])
        )
        a, b = fmt.hm(t_first), fmt.hm(t_last)
        between = f"at {a}" if a == b else f"between {a} and {b}"
        evidence = [
            _ev(f"{n} new subagents {when}: {mix}", m0, r=first0),
            _ev(
                f"Started {between}: {how}",
                m0,
                r=first0,
            ),
            _ev(
                f"Each first request wrote about {fmt.approx(statistics.median(firsts))} tokens",
                m0,
            ),
            _ev(f"{small} of them made two tool calls or fewer", m0),
        ]
        # the prefixes are logged writes: CLEAR when most agents came from Agent calls
        # or a workflow, LIKELY when most started without a call capcom can see
        known = origins["agent"] + origins["workflow"]
        conf = Confidence.CLEAR if known * 2 > n else Confidence.LIKELY
        advice = _fan_out_advice(small, n, origins.most_common(1)[0][0] == "workflow")
        kinds = ", ".join(f"{v} {k}" for k, v in types.most_common(3))
        out.append(
            Candidate(
                CauseKind.FAN_OUT,
                conf,
                excess,
                f"{n} new subagents {when}",
                _cap(evidence),
                advice,
                _fan_out_summary(
                    f"{n} subagents ({kinds}) started {when} from {fmt.hm(t_first)}, {how}.",
                    tokens,
                    excess,
                    spent,
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- rule 9: thinking


def thinking_heavy(view: SpikeView) -> list[Candidate]:
    logged: list[tuple[Member, Request, Usage]] = []
    unlogged: list[tuple[Member, Request, Usage]] = []
    for m in view.members:
        outputs = [r.usage.output for r in m.thread.requests]
        typical = int(statistics.median(outputs)) if outputs else 0
        for r in m.requests:
            u = r.usage
            if len(r.tool_calls) > 1:
                continue
            if u.thinking > 0:
                if u.thinking >= THINKING_SHARE * u.output:
                    logged.append((m, r, Usage(output=u.thinking, thinking=u.thinking)))
            elif (
                not r.output_estimated
                and u.output >= MIN_HEAVY_OUTPUT
                and u.output > typical
                and view.pricer.value(r, Usage(output=u.output))
                >= OUTPUT_COST_SHARE * view.pricer.cost(r)
            ):
                unlogged.append((m, r, Usage(output=u.output - typical)))
    chosen = logged or unlogged
    turns = [(m, r, view.ledger.take(r, part, CauseKind.THINKING_HEAVY)) for m, r, part in chosen]
    if not turns:
        return []
    excess = sum(x for *_, x in turns)
    if excess <= 0:
        return []
    m0 = turns[0][0]
    out_tokens = sum(r.usage.output for _, r, _ in turns)
    efforts = Counter(r.effort for _, r, _ in turns if r.effort)
    if logged:
        thinking = sum(r.usage.thinking for _, r, _ in turns)
        title = f"Thinking-heavy turns: {fmt.approx(thinking)} thinking tokens"
        lines = [
            _ev(
                f"{len(turns)} turns with {fmt.approx(thinking)} thinking tokens of "
                f"{fmt.approx(out_tokens)} output",
                m0,
                turns[0][1],
            )
        ]
        conf = Confidence.CLEAR
    else:
        title = f"Output-heavy turns: {fmt.approx(out_tokens)} output tokens"
        lines = [
            _ev(
                f"{len(turns)} turns wrote {fmt.approx(out_tokens)} output tokens; "
                "thinking is not logged separately",
                m0,
                turns[0][1],
            )
        ]
        conf = Confidence.LIKELY
    if efforts:
        lines.append(_ev(f"Effort {efforts.most_common(1)[0][0]} on these turns", m0))
    lines.append(_ev("One tool call or fewer per turn", m0))
    what = (
        f"{fmt.approx(sum(r.usage.thinking for _, r, _ in turns))} thinking tokens"
        if logged
        else f"{fmt.approx(out_tokens)} output tokens"
    )
    effort = f" at effort {efforts.most_common(1)[0][0]}" if efforts else ""
    return [
        Candidate(
            CauseKind.THINKING_HEAVY,
            conf,
            excess,
            title,
            lines,
            "Lower the effort for routine work; keep high effort for hard design or "
            "debugging turns.",
            f"{fmt.count_word(len(turns), True)} turns{effort} with one tool call or fewer "
            f"wrote {what}, for {fmt.usd(excess)} of output.",
        )
    ]


# --------------------------------------------------------------------------- rule 10: polling


def poll_key(c: ToolCall) -> tuple[str, str] | None:
    """What makes a repeat of `c` a real repeat: the same Read path, a sleep, watch or
    wait-like command, or a background-output check. None for every other call: a
    Bash target is only the command's first word, so `cd`, `bash`, `python` and the
    like say nothing about what ran, and a call with no target says nothing at all."""
    if c.name in POLL_TOOLS:
        return c.name, c.target or ""
    if not c.target:
        return None
    if c.name == "Read":
        return c.name, c.target
    if c.name == "Bash":
        word = c.target.rsplit("/", 1)[-1].lower()
        if word in POLL_COMMANDS or any(w in word for w in POLL_WORDS):
            return c.name, c.target
    return None


def _signature(r: Request) -> tuple[tuple[str, str], ...]:
    keys = [poll_key(c) for c in r.tool_calls]
    if not keys or any(k is None for k in keys):
        return ()
    return tuple(sorted({k for k in keys if k is not None}))


def _results_of(th: Thread, r: Request) -> list[tuple[Event, int]]:
    """The tool results of `r`'s calls, with their size in tokens."""
    ids = {c.tool_use_id for c in r.tool_calls}
    nxt = th.requests_between(r.timestamp + timedelta(microseconds=1), r.timestamp + ONE_HOUR)
    end = nxt[0].timestamp if nxt else r.timestamp + ONE_HOUR
    return [
        (e, _int(e.attrs.get("size_tokens")))
        for e in th.events_between(r.timestamp, end, (EventKind.TOOL_RESULT,))
        if e.attrs.get("tool_use_id") in ids
    ]


def _poll_excess(view: SpikeView, m: Member, run: list[Request]) -> tuple[float, int, int]:
    """(dollars, output tokens, result tokens) the repeats of `run` added: their own
    output, and their results as every later spike request of the thread reread them
    (until a compaction)."""
    th = m.thread
    ids = {r.message_id for r in m.requests}
    x = 0.0
    out_tokens = res_tokens = 0
    for q in run[1:]:
        if q.message_id in ids:
            out_tokens += q.usage.output
            x += view.ledger.take(q, Usage(output=q.usage.output), CauseKind.POLLING)
        for e, n in _results_of(th, q):
            if n <= 0:
                continue
            res_tokens += n
            comps = th.events_between(e.timestamp, view.window_end, (EventKind.COMPACTION,))
            end = comps[0].timestamp if comps else view.window_end
            later = [r for r in m.requests if e.timestamp < r.timestamp < end]
            for i, r in enumerate(later):
                x += view.ledger.take_prompt(r, n, i == 0, CauseKind.POLLING)
    return x, out_tokens, res_tokens


def polling(view: SpikeView) -> list[Candidate]:
    runs: list[tuple[Member, list[Request]]] = []
    for m in view.members:
        run: list[Request] = []
        sig: tuple[tuple[str, str], ...] = ()
        for r in m.requests:
            s = _signature(r)
            if any(k[0] == "Read" for k in s):
                # paging one file with offset and limit reads parts of different
                # sizes; only the same result again is a reread
                sizes = sorted(n for _, n in _results_of(m.thread, r))
                s = (*s, ("size_tokens", ",".join(map(str, sizes))))
            if s and s == sig:
                run.append(r)
                continue
            if len(run) >= POLL_MIN and sig:
                runs.append((m, run))
            run, sig = [r], s
        if len(run) >= POLL_MIN and sig:
            runs.append((m, run))
    found = []
    for m, run in runs:
        x, out_tokens, res_tokens = _poll_excess(view, m, run)
        if x > 0:
            found.append((m, run, x, out_tokens, res_tokens))
    if not found:
        return []
    found.sort(key=lambda f: -f[2])
    m, run, _, out_tokens, res_tokens = found[0]
    calls = sum(len(r.tool_calls) for r in run)
    first = run[0].tool_calls[0]
    label = fmt.tool_label(first.name, first.target)
    evidence = [
        _ev(
            f"{calls} {label} calls in a row, {fmt.hm(run[0].timestamp)} to "
            f"{fmt.hm(run[-1].timestamp)}",
            m,
            run[0],
        ),
        _ev(
            f"The repeats wrote {fmt.approx(out_tokens)} output tokens and added "
            f"{fmt.approx(res_tokens)} result tokens that later turns reread",
            m,
            run[-1],
        ),
    ]
    for m2, run2, *_ in found[1:3]:
        c2 = run2[0].tool_calls[0]
        evidence.append(
            _ev(
                f"Also {len(run2)} {fmt.tool_label(c2.name, c2.target)} calls in a row", m2, run2[0]
            )
        )
    longest = max(len(f[1]) for f in found)
    excess = sum(f[2] for f in found)
    others = len(found) - 1
    also = (
        f", and {fmt.count_word(others)} other run{'s' if others > 1 else ''} like it"
        if others
        else ""
    )
    advice = (
        "Read the file once and keep the part you need instead of rereading it."
        if first.name == "Read"
        else "Wait with one blocking command, or a background task and its notification, "
        "instead of a sleep-and-check loop."
    )
    return [
        Candidate(
            CauseKind.POLLING,
            Confidence.CLEAR if longest >= POLL_CLEAR else Confidence.LIKELY,
            excess,
            f"The same {first.name} call {calls} times in a row",
            evidence,
            advice,
            f"The {_who(m)} made {calls} {label} calls in a row from "
            f"{fmt.hm(run[0].timestamp)}{also}. The repeats and the rereads of their results "
            f"cost {fmt.usd(excess)}.",
        )
    ]


# --------------------------------------------------------------------------- all rules

# In claiming order: each rule prices only the tokens the rules before it left (the
# rewrites claim theirs in `find_rewrites`, before any rule), and context growth, the
# cause without a single event, takes the rest.
# --------------------------------------------------------------------------- rule 11: parallel work


def _most_at_once(spans: Sequence[tuple[datetime, datetime]]) -> tuple[int, datetime | None]:
    """The most spans open at one time, and the first time that many were."""
    points = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
    n = best = 0
    when = None
    for t, d in points:
        n += d
        if n > best:
            best, when = n, t
    return best, when


_PARALLEL_ADVICE = (
    "Run fewer subagents at once (three or four), each on a narrow task with a short "
    "brief, and give routine ones a cheaper model: every agent rereads its own context "
    "each turn, so the spend grows with how many run side by side."
)


def parallel_work(view: SpikeView) -> list[Candidate]:
    """Rule 11: on a session spike where PARALLEL_MIN or more subagents work at once,
    their own requests (what the other causes left of them) are the parallel work."""
    if view.spike.scope != "session":
        return []
    subs = [m for m in view.members if m.thread.agent_id is not None]
    if len(subs) < PARALLEL_MIN:
        return []
    spans = [(m.requests[0].timestamp, m.requests[-1].timestamp + PARALLEL_PAD) for m in subs]
    peak, when = _most_at_once(spans)
    if peak < PARALLEL_MIN or when is None:
        return []
    spent = sum(view.pricer.cost(r) for m in subs for r in m.requests)
    excess = sum(
        view.ledger.take(r, view.ledger.left(r), CauseKind.PARALLEL_SUBAGENTS)
        for m in subs
        for r in m.requests
    )
    if excess <= 0:
        return []
    types = Counter(m.session.agent_type(m.thread.agent_id) or "unknown type" for m in subs)
    mix = ", ".join(f"{k} \u00d7{v}" for k, v in types.most_common(4))
    turns = sum(len(m.requests) for m in subs)
    m0 = subs[0]
    evidence = [
        _ev(f"{peak} subagents working at once at {fmt.clock(when)}", m0),
        _ev(f"{len(subs)} subagents in the spike: {mix}", m0),
        _ev(f"{turns} subagent requests in the spike, {fmt.usd(spent)} in all", m0),
    ]
    return [
        Candidate(
            CauseKind.PARALLEL_SUBAGENTS,
            Confidence.LIKELY,
            excess,
            f"Parallel subagents' work, up to {peak} at once",
            evidence,
            _PARALLEL_ADVICE,
            f"{len(subs)} subagents worked in this spike, up to {peak} at once, over "
            f"{turns} requests. Their own work, apart from what the causes above claim, "
            f"cost {fmt.usd(excess)}: the price of running them side by side.",
        )
    ]


RULES: tuple[Callable[[SpikeView], list[Candidate]], ...] = (
    rewrite_candidates,
    late_compaction,
    large_results,
    long_reports,
    fan_out,
    thinking_heavy,
    polling,
    context_growth,
    parallel_work,  # last: the subagents' work no other cause claims
)


def merge(cands: list[Candidate]) -> list[Candidate]:
    """One candidate per cause: excesses add up, the largest part names it. A trim
    (a prompt cut with no compaction record) stays apart from the logged compactions:
    its title, advice and summary say something else."""
    by: dict[tuple[CauseKind, bool], list[Candidate]] = {}
    for c in cands:
        by.setdefault((c.cause, c.advice == _TRIM_ADVICE), []).append(c)
    out = []
    for (cause, _), group in by.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        group.sort(key=lambda c: -c.excess)
        out.append(
            Candidate(
                cause,
                _confidence((c.confidence, c.excess) for c in group),
                sum(c.excess for c in group),
                group[0].title,
                _cap([e for c in group for e in c.evidence]),
                group[0].advice,
                group[0].summary,
            )
        )
    return out


def candidates(view: SpikeView) -> list[Candidate]:
    view.rewrites = find_rewrites(view)
    found: list[Candidate] = []
    for rule in RULES:
        found.extend(rule(view))
    return merge(found)
