"""capcom's totals against Claude Code's own `cost-state` (docs/design.md, "Cost model").

What cost-state counts (measured on 2,241 sessions, Claude Code 2.1.27x-2.1.306):

- Every model call of the *current process*, main thread and subagents alike, since
  `startTime` (the process start). A resumed session that does not restore the old
  totals starts again from zero, so only requests from `startTime` on compare.
- Model calls the transcript never shows: away summaries, compactions, WebFetch's
  page summarizer and WebSearch's own call (large uncached input: this is why its
  inputTokens can be 100-1,000x the logged input), and a small fixed Haiku call per
  process. The auto-mode classifier (`serverClassifierRequest`) is server-side and
  not in cost-state.
- Its own price table, which can lag the pricing page (Sonnet 5.5 cache reads were
  $0.20 per MTok in sessions up to 2026-10-07, $0.10 from 2026-10-18).

So a session's spread is meaningful only when its window has none of those side
calls: `SessionCheck.comparable` says so, and `reasons` says why not.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from capcom.cost.model import price
from capcom.cost.prices import normalize
from capcom.types import Calibration, CostState, EventKind, Index, PriceTable

SIDE_CALL_TOOLS = frozenset({"WebFetch", "WebSearch"})
SIDE_CALL_EVENTS = frozenset({EventKind.AWAY, EventKind.COMPACTION})
MIN_COMPARABLE_USD = 0.10  # below this a fixed per-process side call is above 2 %
TARGET_SPREAD = 0.02


@dataclass(frozen=True, slots=True)
class SessionCheck:
    """A calibration and whether it is comparable (no side calls in its window)."""

    calibration: Calibration
    comparable: bool
    reasons: tuple[str, ...] = ()
    requests: int = 0
    estimated_output: int = 0  # requests whose output count is an estimate
    inexact_price: int = 0  # requests priced as their closest family


def _window_start(cs: CostState) -> datetime | None:
    if cs.start_time_ms is None:
        return None
    return datetime.fromtimestamp(cs.start_time_ms / 1000, UTC)


def check_session(index: Index, cs: CostState, table: PriceTable) -> SessionCheck:
    """Price the session's requests (main and subagents) from the cost-state's
    `startTime` on and compare them with the cost-state, in total and per model."""
    since = _window_start(cs)
    per_model: dict[str, float] = defaultdict(float)
    reasons: set[str] = set()
    n = est = inexact = 0
    for req in index.requests(since=since, session_id=cs.session_id):
        cost = price(req, table)
        per_model[normalize(req.model)] += cost.total
        n += 1
        est += req.output_estimated
        inexact += not cost.exact_price
        for call in req.tool_calls:
            if call.name in SIDE_CALL_TOOLS:
                reasons.add(call.name)
    for ev in index.events(since=since, session_id=cs.session_id, kinds=SIDE_CALL_EVENTS):
        reasons.add(ev.kind.value)
    cc_models: dict[str, float] = defaultdict(float)
    for model, mu in cs.models.items():
        cc_models[normalize(model)] += mu.cost_usd
    for model, usd in cc_models.items():
        if usd > 0 and per_model.get(model, 0.0) == 0.0:
            reasons.add("model only in cost-state")
    if cs.total_cost_usd < MIN_COMPARABLE_USD:
        reasons.add("below $0.10")
    if est:
        reasons.add("estimated output")
    keys = sorted(set(per_model) | set(cc_models))
    calibration = Calibration(
        session_id=cs.session_id,
        capcom_usd=sum(per_model.values()),
        claude_code_usd=cs.total_cost_usd,
        per_model={m: (per_model.get(m, 0.0), cc_models.get(m, 0.0)) for m in keys},
    )
    return SessionCheck(
        calibration=calibration,
        comparable=not reasons,
        reasons=tuple(sorted(reasons)),
        requests=n,
        estimated_output=est,
        inexact_price=inexact,
    )


def check_sessions(index: Index, table: PriceTable) -> list[SessionCheck]:
    return [check_session(index, cs, table) for cs in index.last_cost_states()]


def calibrate(index: Index, table: PriceTable) -> list[Calibration]:
    """One Calibration per session with a cost-state (see `check_sessions` for the
    comparable flag)."""
    return [c.calibration for c in check_sessions(index, table)]
