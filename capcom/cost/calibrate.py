"""capcom's totals against Claude Code's own `cost-state` (docs/design.md, "Cost model").

What cost-state counts (measured on 2,241 sessions, Claude Code 2.1.27x-2.1.306):

- Every model call of the *current process*, main thread and subagents alike, from
  `startTime` (the process start) to the moment the record was written. A resumed
  session that does not restore the old totals starts again from zero, so only
  requests in [startTime, the cost-state's timestamp] compare.
- Model calls the transcript never shows: away summaries, compactions, WebFetch's
  page summarizer and WebSearch's own call (large uncached input: this is why its
  inputTokens can be 100-1,000x the logged input), and a small fixed Haiku call per
  process. The auto-mode classifier (`serverClassifierRequest`) is server-side and
  not in cost-state.
- Its own price table, which can lag the pricing page (Sonnet 5.5 cache reads were
  $0.20 per MTok up to 2026-10-07: prices.json's `previous`).

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
# Index.requests' `until` is exclusive and log times have millisecond resolution.
_TICK = timedelta(milliseconds=1)


def window(cs: CostState) -> tuple[datetime | None, datetime | None]:
    """[since, until) of the requests a cost-state covers."""
    since = None
    if cs.start_time_ms is not None:
        since = datetime.fromtimestamp(cs.start_time_ms / 1000, UTC)
    until = cs.timestamp + _TICK if cs.timestamp is not None else None
    return since, until


def check_session(index: Index, cs: CostState, table: PriceTable) -> Calibration:
    """Price the session's requests (main and subagents) in the cost-state's window
    and compare them with it, in total and per model (keyed by the table's id)."""
    since, until = window(cs)
    per_model: dict[str, float] = defaultdict(float)
    reasons: set[str] = set()
    estimated = 0
    for req in index.requests(since=since, until=until, session_id=cs.session_id):
        if is_placeholder(req.model):
            continue
        per_model[canonical(table, req.model)] += price(req, table).total
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
    for model, mu in cs.models.items():
        cc_models[canonical(table, model)] += mu.cost_usd
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
