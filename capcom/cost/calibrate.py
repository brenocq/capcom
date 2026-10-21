"""capcom's totals against Claude Code's own `cost-state` (docs/design.md, "Cost model").

What cost-state counts (measured on 2,241 sessions, Claude Code 2.1.27x-2.1.306):

- Every model call of the *current process*, main thread and subagents alike, from
  `startTime` (the process start) to the moment the record was written. A resumed
  session that does not restore the old totals starts again from zero, so only
  requests in [startTime, next_timestamp) compare: the window ends at the first
  timestamped record after the cost-state line, exclusive, or is open when none
  follows (the records just before the line can be out of order, and background
  subagents keep spending after the main log's last record).
- Model calls the transcript never shows: away summaries, compactions, WebFetch's
  page summarizer and WebSearch's own call (large uncached input: this is why its
  inputTokens can be 100-1,000x the logged input), and a small fixed Haiku call per
  process. The auto-mode classifier (`serverClassifierRequest`) is server-side and
  not in cost-state.
- Its own price table, which can lag the pricing page (Sonnet 5.5 cache reads were
  $0.20 per MTok up to 2026-10-07: prices.json's `previous`).

Two more causes make a window's logs incomplete: a reply also in another session's
log (a forked or copied session; the index keeps one row per message.id, so one of
the two sessions lost it), and calls the transcript lacks altogether, seen as
cost-state's uncached input for a model far above the logged one.

So a session's spread is meaningful only when its window has none of those side
calls: `Calibration.comparable` says so, and `reasons` says why not.

Per process step. A resumed process restores the totals, `startTime` and
`totalDuration` included, and writes a new cost-state when it exits; so the growth
from one record to the next is what the process that wrote the later one spent, over
its wall-clock span (the growth of `totalDuration`). Each step is compared from that
many milliseconds before the record (less a minute of slack) to the first record
after it; a record with a new `startTime` starts again from its process start. The
logged spend in no step was made by a process that wrote no cost-state (killed,
crashed, or still running): `outside_usd`, never a mismatch.

What the log cannot show. Where capcom estimated output, cost-state's output tokens
above capcom's are priced at the model's output rate: the true total is at least
capcom's and up to that much more (`output_above`). Every other token excess of
cost-state over the log (cache reads, uncached input, cache writes, output where
nothing is estimated) is priced at the window's own rate per model: calls Claude
Code counted that the log does not record (`unlogged`), and every token count the
log has more of is priced the same way (`logged_more`). Both are inferred from token
counts, not from calls seen. The rest is unexplained.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from capcom.cost.model import price
from capcom.cost.prices import canonical, is_placeholder, lookup, price_at
from capcom.types import Calibration, CostState, EventKind, Index, ModelUsage, PriceTable

SIDE_CALL_TOOLS = frozenset({"WebFetch", "WebSearch"})
SIDE_CALL_EVENTS = frozenset({EventKind.AWAY, EventKind.COMPACTION})
MIN_COMPARABLE_USD = 0.10  # below this a fixed per-process side call is above 2 %
TARGET_SPREAD = 0.02
# A model only cost-state knows is ignored below this share of the session or this
# many dollars (the small Haiku call every process makes).
IGNORED_SIDE_MODEL_SHARE = 0.01
IGNORED_SIDE_MODEL_USD = 0.01
# Calls the transcript lacks: cost-state's uncached input for a model above the
# logged uncached input by more than this many tokens and this share of the logged
# input. On the milestone 1 data (2,239 sessions), 1,419 of the 1,426 otherwise
# comparable sessions match the log's uncached input exactly. The token floor sits
# above the small Haiku call every process makes (about 950 uncached tokens); the
# share keeps a large session missing one big call (a 100k-token summarizer) from
# hiding behind a ratio.
MISSING_CALLS_TOKENS = 1_000
MISSING_CALLS_SHARE = 0.02

# A step starts this long before its record's time less its duration: the process
# can write its first reply a moment after it starts.
STEP_SLACK = timedelta(minutes=1)
# Token categories cost-state and the log both count, as Calibration.unlogged keys.
CATEGORIES = ("cache_read", "input", "output", "cache_write")

SHARED_REPLIES = "shares replies with another session"
MISSING_CALLS = "calls not in the log"


def window(cs: CostState) -> tuple[datetime | None, datetime | None]:
    """[since, until) of the requests a cost-state covers: from the process start
    to the first timestamped record after the cost-state line (exclusive); None
    for an end that is not known. The end is never before the cost-state's own time
    plus 1 ms: a next record out of order must not cut replies logged before it."""
    since = None
    if cs.start_time_ms is not None:
        since = datetime.fromtimestamp(cs.start_time_ms / 1000, UTC)
    until = cs.next_timestamp
    if until is not None and cs.timestamp is not None:
        until = max(until, cs.timestamp + timedelta(milliseconds=1))
    return since, until


@dataclass(frozen=True, slots=True)
class Step:
    """What one process added to cost-state's totals, and when: [since, until)."""

    since: datetime | None  # None: no start known, from the beginning of the log
    until: datetime | None  # None: open (no record after the cost-state yet)
    usd: float
    models: dict[str, ModelUsage] = field(default_factory=dict)

    def holds(self, t: datetime) -> bool:
        return (self.since is None or self.since <= t) and (self.until is None or t < self.until)


_TOKENS = ("input", "output", "cache_read", "cache_write")


def _fits(base: CostState, c: CostState) -> bool:
    """Whether `c`'s totals can have grown from `base`'s (a process that restored
    them): no cost and no token count of any model smaller, no shorter duration,
    and a process start (its time less the duration's growth) not before `base`
    was written. A part not known on either side does not rule `base` out."""
    if c.total_cost_usd < base.total_cost_usd:
        return False
    for model, b in base.models.items():
        mu = c.models.get(model, ModelUsage())
        if any(getattr(mu, k) < getattr(b, k) for k in _TOKENS):
            return False
    if c.total_duration_ms is None or base.total_duration_ms is None:
        return True
    grown = c.total_duration_ms - base.total_duration_ms
    if grown < 0:
        return False
    if c.timestamp is None or base.timestamp is None:
        return True
    return c.timestamp - timedelta(milliseconds=grown) + STEP_SLACK >= base.timestamp


def _minus(c: CostState, base: CostState | None) -> dict[str, ModelUsage]:
    if base is None:
        return dict(c.models)
    out = {}
    for model, mu in c.models.items():
        b = base.models.get(model, ModelUsage())
        out[model] = ModelUsage(
            input=max(0, mu.input - b.input),
            output=max(0, mu.output - b.output),
            thinking=max(0, mu.thinking - b.thinking),
            cache_read=max(0, mu.cache_read - b.cache_read),
            cache_write=max(0, mu.cache_write - b.cache_write),
            web_searches=max(0, mu.web_searches - b.web_searches),
            cost_usd=max(0.0, mu.cost_usd - b.cost_usd),
        )
    return out


def steps(states: Sequence[CostState]) -> list[Step]:
    """The process steps of one file's cost-states, in file order.

    A record with the same `startTime` as the one before it restored earlier totals:
    its step is its growth over the latest earlier record of that start it fits
    (`_fits`: concurrent processes can restore an older one), from its time less the
    growth of `totalDuration` but not before that record was written. When no
    earlier record fits, it grows from the latest one with no higher cost, from the
    first record after that one. A record that adds nothing is the same exit written
    twice and adds no step. A record with a new `startTime`, or one costing less than
    every earlier record of its start, starts from its process start."""
    out: list[Step] = []
    chain: list[CostState] = []
    for c in states:
        until = window(c)[1]
        same = chain and c.start_time_ms is not None and c.start_time_ms == chain[-1].start_time_ms
        base = fits = None
        if same:
            base = fits = next((b for b in reversed(chain) if _fits(b, c)), None)
            if base is None:
                base = next(
                    (b for b in reversed(chain) if b.total_cost_usd <= c.total_cost_usd), None
                )
        if base is None:
            chain = [c]
            out.append(Step(window(c)[0], until, c.total_cost_usd, dict(c.models)))
            continue
        chain.append(c)
        grown = c.total_cost_usd - base.total_cost_usd
        dur = (
            None
            if fits is None or c.total_duration_ms is None or base.total_duration_ms is None
            else c.total_duration_ms - base.total_duration_ms
        )
        if grown == 0 and (dur in (0, None) or fits is None):
            continue
        since: datetime | None
        if dur is not None and c.timestamp is not None:
            # totalDuration includes the idle time before the exit: never start before
            # the record this one grew from was written.
            since = c.timestamp - timedelta(milliseconds=dur) - STEP_SLACK
            if base.timestamp is not None:
                since = max(since, base.timestamp)
        else:
            since = window(base)[1] or base.timestamp
        out.append(Step(since, until, grown, _minus(c, base)))
    return out


def session_steps(index: Index, cs: CostState) -> list[Step]:
    """The steps of `cs`'s session up to `cs` (its last cost-state, normally)."""
    states_fn = getattr(index, "cost_states", None)  # optional: not in the Index protocol
    found = states_fn(cs.session_id) if states_fn else ()
    earlier = [c for c in found if c.offset < cs.offset]
    return steps([*earlier, cs])


@dataclass
class _Logged:
    """One model's logged tokens and dollars per category inside the steps."""

    usd: float = 0.0
    estimated: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))
    cost: dict[str, float] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0.0))


def _rates(
    table: PriceTable, model: str, logged: _Logged | None, when: datetime | None
) -> dict[str, float]:
    """USD per token per category: the window's own average where it logged some
    (long context, fast mode and older prices included), else the table's."""
    p = price_at(table, lookup(table, model)[0], when)
    base = {
        "cache_read": p.cache_read,
        "input": p.input,
        "output": p.output,
        "cache_write": p.cache_write_5m,
    }
    out = {}
    for cat in CATEGORIES:
        if logged is not None and logged.tokens[cat] > 0:
            out[cat] = logged.cost[cat] / logged.tokens[cat]
        else:
            out[cat] = base[cat] / 1_000_000
    return out


def check_session(index: Index, cs: CostState, table: PriceTable) -> Calibration:
    """Price the session's requests (main and subagents) in the cost-state's process
    steps and compare them with what those steps added, in total and per model
    (keyed by the table's id)."""
    st = session_steps(index, cs)
    logged: dict[str, _Logged] = defaultdict(_Logged)
    reasons: set[str] = set()
    outside = 0.0
    for req in index.requests(session_id=cs.session_id):
        if is_placeholder(req.model):
            continue
        cost = price(req, table)
        if not any(s.holds(req.timestamp) for s in st):
            outside += cost.total
            continue
        lg = logged[canonical(table, req.model)]
        lg.usd += cost.total
        lg.estimated += req.output_estimated
        u = req.usage
        for cat, n, usd_ in (
            ("cache_read", u.cache_read, cost.cache_read),
            ("input", u.input, cost.input),
            ("output", u.output, cost.output),
            ("cache_write", u.cache_write, cost.cache_write),
        ):
            lg.tokens[cat] += n
            lg.cost[cat] += usd_
        for call in req.tool_calls:
            if call.name in SIDE_CALL_TOOLS:
                reasons.add(call.name)
    for ev in index.events(session_id=cs.session_id, kinds=SIDE_CALL_EVENTS):
        if any(s.holds(ev.timestamp) for s in st):
            reasons.add(ev.kind.value)
    cc: dict[str, _Logged] = defaultdict(_Logged)
    for step in st:
        for model, mu in step.models.items():
            c = cc[canonical(table, model)]
            c.usd += mu.cost_usd
            for cat, n in (
                ("cache_read", mu.cache_read),
                ("input", mu.input),
                ("output", mu.output),
                ("cache_write", mu.cache_write),
            ):
                c.tokens[cat] += n
    for model, c in cc.items():
        # Only models the window logs; one it lacks is "model only in cost-state".
        lg_input = logged[model].tokens["input"] if model in logged else 0
        excess = c.tokens["input"] - lg_input
        if model in logged and excess > max(MISSING_CALLS_TOKENS, MISSING_CALLS_SHARE * lg_input):
            reasons.add(MISSING_CALLS)
    if any(index.shared_replies(cs.session_id, since=s.since, until=s.until) for s in st):
        reasons.add(SHARED_REPLIES)
    cc_total = sum(s.usd for s in st)
    floor = max(IGNORED_SIDE_MODEL_USD, IGNORED_SIDE_MODEL_SHARE * cc_total)
    for model, c in cc.items():
        if c.usd >= floor and (model not in logged or logged[model].usd == 0.0):
            reasons.add("model only in cost-state")
    if any(s.since is None for s in st):
        reasons.add("no startTime")
    if cc_total < MIN_COMPARABLE_USD:
        reasons.add("below $0.10")
    if any(lg.estimated for lg in logged.values()):
        reasons.add("estimated output")
    above: dict[str, float] = {}
    unlogged: dict[str, dict[str, float]] = {}
    more: dict[str, dict[str, float]] = {}
    for model in sorted(set(cc) | set(logged)):
        got = logged.get(model)
        mine = cc.get(model)
        rate = _rates(table, model, got, cs.timestamp)
        parts, less = {}, {}
        for cat in CATEGORIES:
            extra = (mine.tokens[cat] if mine else 0) - (got.tokens[cat] if got else 0)
            if extra < 0:
                less[cat] = -extra * rate[cat]
            elif extra == 0:
                continue
            elif cat == "output" and got is not None and got.estimated:
                above[model] = extra * rate[cat]
            else:
                parts[cat] = extra * rate[cat]
        if parts:
            unlogged[model] = parts
        if less:
            more[model] = less
    keys = sorted(set(logged) | set(cc))
    return Calibration(
        session_id=cs.session_id,
        capcom_usd=sum(lg.usd for lg in logged.values()),
        claude_code_usd=cc_total,
        per_model={
            m: (logged[m].usd if m in logged else 0.0, cc[m].usd if m in cc else 0.0) for m in keys
        },
        comparable=not reasons,
        reasons=tuple(sorted(reasons)),
        outside_usd=outside,
        output_above=above,
        unlogged=unlogged,
        logged_more=more,
    )


def calibrate(index: Index, table: PriceTable) -> list[Calibration]:
    """One Calibration per session with a cost-state, against its last one."""
    return [check_session(index, cs, table) for cs in index.last_cost_states()]
