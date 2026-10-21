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
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from capcom.cost.model import price
from capcom.cost.prices import canonical, is_placeholder
from capcom.types import Calibration, CostState, EventKind, Index, PriceTable

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


def check_session(index: Index, cs: CostState, table: PriceTable) -> Calibration:
    """Price the session's requests (main and subagents) in the cost-state's window
    and compare them with it, in total and per model (keyed by the table's id)."""
    since, until = window(cs)
    per_model: dict[str, float] = defaultdict(float)
    logged_input: dict[str, int] = defaultdict(int)
    reasons: set[str] = set()
    estimated = 0
    for req in index.requests(since=since, until=until, session_id=cs.session_id):
        if is_placeholder(req.model):
            continue
        key = canonical(table, req.model)
        per_model[key] += price(req, table).total
        logged_input[key] += req.usage.input
        estimated += req.output_estimated
        for call in req.tool_calls:
            if call.name in SIDE_CALL_TOOLS:
                reasons.add(call.name)
    events = index.events(
        since=since, until=until, session_id=cs.session_id, kinds=SIDE_CALL_EVENTS
    )
    for ev in events:
        reasons.add(ev.kind.value)
    cc_models: dict[str, float] = defaultdict(float)
    cc_input: dict[str, int] = defaultdict(int)
    for model, mu in cs.models.items():
        key = canonical(table, model)
        cc_models[key] += mu.cost_usd
        cc_input[key] += mu.input
    for model, tokens in cc_input.items():
        # Only models the window logs; one it lacks is "model only in cost-state".
        logged = logged_input.get(model, 0)
        excess = tokens - logged
        if model in per_model and excess > max(MISSING_CALLS_TOKENS, MISSING_CALLS_SHARE * logged):
            reasons.add(MISSING_CALLS)
    if index.shared_replies(cs.session_id, since=since, until=until):
        reasons.add(SHARED_REPLIES)
    floor = max(IGNORED_SIDE_MODEL_USD, IGNORED_SIDE_MODEL_SHARE * cs.total_cost_usd)
    for model, usd in cc_models.items():
        if usd >= floor and per_model.get(model, 0.0) == 0.0:
            reasons.add("model only in cost-state")
    if since is None:
        reasons.add("no startTime")
    if cs.total_cost_usd < MIN_COMPARABLE_USD:
        reasons.add("below $0.10")
    if estimated:
        reasons.add("estimated output")
    keys = sorted(set(per_model) | set(cc_models))
    return Calibration(
        session_id=cs.session_id,
        capcom_usd=sum(per_model.values()),
        claude_code_usd=cs.total_cost_usd,
        per_model={m: (per_model.get(m, 0.0), cc_models.get(m, 0.0)) for m in keys},
        comparable=not reasons,
        reasons=tuple(sorted(reasons)),
    )


def calibrate(index: Index, table: PriceTable) -> list[Calibration]:
    """One Calibration per session with a cost-state, against its last one."""
    return [check_session(index, cs, table) for cs in index.last_cost_states()]
