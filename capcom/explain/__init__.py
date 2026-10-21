"""capcom.explain: spikes matched to causes (contract: capcom.types.ExplainModule).

For each spike, the rules in `rules.py` (design.md, "Causes (explain)") look at its
requests and at the events of the same session and agent shortly before and during
it, and say how many of the spike's excess dollars each cause accounts for. The
best-supported causes come first (events before context growth, clear before
likely, then by dollars) and take
their share of the spike's excess until it is used up, so the shares never add up
to more than the spike's excess. A cause worth less than a tenth of the spike does
not explain it; when no cause is left the spike is UNEXPLAINED, naming what was
checked and the closest candidate. When the causes found cover less than half the
spike, the rest is reported as UNEXPLAINED too.
"""

from __future__ import annotations

from collections.abc import Sequence

from capcom.explain import fmt
from capcom.explain.data import SessionCache
from capcom.explain.fmt import CAUSE_LABELS, CONFIDENCE_LABELS
from capcom.explain.rules import Candidate, Pricer, SpikeView, candidates
from capcom.types import (
    CauseKind,
    Confidence,
    Evidence,
    Explanation,
    Index,
    PriceTable,
    Spike,
    SpikeKind,
)

__all__ = ["CAUSE_LABELS", "CONFIDENCE_LABELS", "explain"]

KEEP_SHARE = 0.10  # a cause must account for this share of the spike's excess
REMAINDER_SHARE = 0.50  # an uncovered rest above this share is reported as unexplained
_RANK = {Confidence.CLEAR: 0, Confidence.LIKELY: 1, Confidence.UNEXPLAINED: 2}
_REWRITE_CAUSES = frozenset(
    {
        CauseKind.IDLE_REWRITE,
        CauseKind.COMPACTION,
        CauseKind.MODEL_SWITCH,
        CauseKind.TOOL_SET_CHANGE,
    }
)
_NOTHING_TO_CHANGE = (
    "Nothing to change yet. If this pattern comes back, it is a candidate for a new rule."
)


def explain(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
    """One or more explanations per spike, in the spikes' order; UNEXPLAINED when no
    rule fits."""
    cache = SessionCache(index, spikes)
    pricer = Pricer(table)
    order = sorted(range(len(spikes)), key=lambda i: (spikes[i].session_id or "", spikes[i].start))
    results: dict[int, list[Explanation]] = {}
    for i in order:
        spike = spikes[i]
        view = SpikeView(spike, cache.members(spike), pricer)
        results[i] = _explain_one(view)
        cache.done(spike)
    return [e for i in range(len(spikes)) for e in results[i]]


def _explain_one(view: SpikeView) -> list[Explanation]:
    spike = view.spike
    total = spike.excess_usd
    found = candidates(view) if view.members else []
    kept = [c for c in found if c.excess > 0 and c.excess >= KEEP_SHARE * total]
    if not kept:
        return [_unexplained(view, found)]
    # event causes first (clear before likely, then by dollars); context growth, the
    # cause without a single event, takes what they leave
    kept.sort(key=lambda c: (c.cause is CauseKind.CONTEXT_GROWTH, _RANK[c.confidence], -c.excess))
    out: list[Explanation] = []
    left = total
    for c in kept:
        share = min(c.excess, left)
        if share <= 0 and out:
            continue
        left -= share
        out.append(
            Explanation(spike, c.cause, c.confidence, share, c.title, tuple(c.evidence), c.advice)
        )
    if total > 0 and left > REMAINDER_SHARE * total:
        out.append(
            Explanation(
                spike,
                CauseKind.UNEXPLAINED,
                Confidence.UNEXPLAINED,
                left,
                "The rest of the spike: no rule fits",
                (
                    Evidence(
                        f"The causes above account for {fmt.usd(total - left)} of "
                        f"{fmt.usd(total)} above the usual pace",
                        session_id=spike.session_id,
                        agent_id=spike.agent_id,
                        timestamp=spike.start,
                    ),
                ),
                _NOTHING_TO_CHANGE,
            )
        )
    return out


def _unexplained(view: SpikeView, found: list[Candidate]) -> Explanation:
    spike = view.spike

    def ev(label: str) -> Evidence:
        return Evidence(
            label, session_id=spike.session_id, agent_id=spike.agent_id, timestamp=spike.start
        )

    if spike.kind is SpikeKind.SLOPE_CHANGE:
        title = f"Spend rose for {fmt.duration(spike.end - spike.start)}"
    else:
        title = f"One request cost {fmt.usd(spike.cost_usd)}"
    evidence: list[Evidence] = []
    if not view.members:
        evidence.append(ev("No request of the spike is in the index"))
    unexplained_rewrites = [rw for rw in view.rewrites if rw.reason is CauseKind.UNEXPLAINED]
    evidence.extend(e for rw in unexplained_rewrites[:3] for e in rw.evidence)
    if view.members and not any(c.cause in _REWRITE_CAUSES for c in found):
        evidence.append(ev("No pause, compaction, model switch or tool change in the window"))
    if view.members and not any(c.cause is CauseKind.LARGE_TOOL_RESULT for c in found):
        evidence.append(ev("No tool result above 20k tokens"))
    closest = max((c for c in found if c.excess > 0), key=lambda c: c.excess, default=None)
    if closest is not None:
        evidence.append(ev(f"Closest: {closest.title}, about {fmt.usd(closest.excess)}"))
    return Explanation(
        spike,
        CauseKind.UNEXPLAINED,
        Confidence.UNEXPLAINED,
        spike.excess_usd,
        title,
        tuple(evidence),
        _NOTHING_TO_CHANGE,
    )
