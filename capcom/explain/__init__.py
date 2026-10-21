"""capcom.explain: spikes matched to causes (contract: capcom.types.ExplainModule).

For each spike, the rules in `rules.py` (design.md, "Causes (explain)") look at its
requests and at the events of the same session and agent shortly before and during
it, and say how many of the spike's excess dollars each cause accounts for. The
best-supported causes come first (events before context growth, clear before
likely, then by dollars) and take their share of the spike's excess until it is
used up, so the shares never add up to more than the spike's excess.

A cause worth less than a tenth of the spike does not explain it, unless it is CLEAR
and worth at least KEEP_CLEAR_USD and KEEP_CLEAR_SHARE of the spike; when no cause
is left the spike is UNEXPLAINED, naming what was checked and the closest candidate.
Whatever excess the causes found leave is reported as UNEXPLAINED too, so a spike's
explanations add up to its excess: only a tiny rest (under TINY_REST_USD and under
TINY_REST_SHARE of the spike) is folded, silently, into the largest cause's share,
so that no line of a few cents shows. A spike is matched when its explanations other
than UNEXPLAINED cover at least KEEP_SHARE of its excess (`matched`).
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
KEEP_CLEAR_USD = 0.50  # ... or be CLEAR and account for at least this many dollars
KEEP_CLEAR_SHARE = 0.03  # ... and at least this share of the spike's excess
TINY_REST_USD = 0.05  # an uncovered rest under both of these is folded into a cause
TINY_REST_SHARE = 0.02
_LAST = {CauseKind.CONTEXT_GROWTH: 1, CauseKind.PARALLEL_SUBAGENTS: 2}
_RANK = {Confidence.CLEAR: 0, Confidence.LIKELY: 1, Confidence.UNEXPLAINED: 2}
_REWRITE_CAUSES = frozenset(
    {
        CauseKind.IDLE_REWRITE,
        CauseKind.TOOL_WAIT_EXPIRY,
        CauseKind.LATE_RESUME_EXPIRY,
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
    kept = [c for c in found if c.excess > 0 and _keeps(c, c.excess, total)]
    if not kept:
        return [_unexplained(view, found)]
    # event causes first (clear before likely, then by dollars); context growth, the
    # cause without a single event, takes what they leave, and the parallel subagents'
    # own work what is left after that
    kept.sort(key=lambda c: (_LAST.get(c.cause, 0), _RANK[c.confidence], -c.excess))
    shares: list[tuple[Candidate, float]] = []
    left = total
    for c in kept:
        share = min(c.excess, left)
        if (share <= 0 or not _keeps(c, share, total)) and shares:
            continue  # what an earlier cause left of it is too small: part of the rest
        left -= share
        shares.append((c, share))
    if 1e-9 < left < TINY_REST_USD and left < TINY_REST_SHARE * total:
        # a tiny rest goes to the largest cause before its summary is written, so the
        # line and its text name the same dollars
        k = max(range(len(shares)), key=lambda i: shares[i][1])
        shares[k] = (shares[k][0], shares[k][1] + left)
        left = 0.0
    out: list[Explanation] = []
    for c, share in shares:
        summary = c.summary
        if share < c.excess - 1e-9:
            # an earlier cause took part of the excess: say what this one keeps
            summary += f" {fmt.usd(share)} of that is above the usual pace and counted here."
        elif share > c.excess + 1e-9:
            summary += f" With the few cents no rule claims, {fmt.usd(share)} is counted here."
        out.append(
            Explanation(
                spike,
                c.cause,
                c.confidence,
                share,
                c.title,
                tuple(c.evidence),
                c.advice,
                summary,
            )
        )
    if left > 1e-9:
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
                f"The causes above account for {fmt.usd(total - left)} of the "
                f"{fmt.usd(total)} above the usual pace; no rule fits the other "
                f"{fmt.usd(left)}."
                + (
                    " It includes the new subagents' own work, apart from their prefixes."
                    if any(e.cause is CauseKind.FAN_OUT for e in out)
                    and not any(e.cause is CauseKind.PARALLEL_SUBAGENTS for e in out)
                    else ""
                ),
            )
        )
    return out


def _keeps(c: Candidate, share: float, total: float) -> bool:
    """A cause explains a spike with at least KEEP_SHARE of its excess, or, when it is
    CLEAR, with at least KEEP_CLEAR_USD and KEEP_CLEAR_SHARE of it; a smaller one folds
    into the unexplained rest."""
    if share >= KEEP_SHARE * total:
        return True
    return (
        c.confidence is Confidence.CLEAR
        and share >= KEEP_CLEAR_USD
        and share >= KEEP_CLEAR_SHARE * total
    )


def matched(spike: Spike, explanations: Sequence[Explanation]) -> bool:
    """A spike is matched when its causes other than UNEXPLAINED cover at least
    KEEP_SHARE of its excess."""
    found = sum(e.excess_usd for e in explanations if e.cause is not CauseKind.UNEXPLAINED)
    return found > 0 and found >= KEEP_SHARE * spike.excess_usd


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
    n = len(view.requests)
    summary = (
        f"{fmt.count_word(n, True)} request{'s' if n != 1 else ''} spent "
        f"{fmt.usd(spike.excess_usd)} above the usual pace and no rule fits"
        + (
            f"; the closest, {CAUSE_LABELS[closest.cause].lower()}, accounts for "
            f"{fmt.usd(closest.excess)}."
            if closest
            else "."
        )
    )
    return Explanation(
        spike,
        CauseKind.UNEXPLAINED,
        Confidence.UNEXPLAINED,
        spike.excess_usd,
        title,
        tuple(evidence),
        _NOTHING_TO_CHANGE,
        summary,
    )
