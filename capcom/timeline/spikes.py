"""Spike detection (docs/design.md, "Spikes and slope changes"), per agent thread.

A thread is one session's main thread (agent_id None) or one of its subagents. Two
kinds, both from ratios against the thread's own recent past:

- SINGLE_REQUEST: a request costing more than `single_ratio` times the median of
  the thread's previous `history` requests and of its next `window_turns`, and
  more than `single_floor_usd` above the former, which is the baseline.
  Typically a cache rewrite of a big context.
- SLOPE_CHANGE: the cost per turn over the next `window_turns` requests
  is at least `slope_ratio` times the rate over the `baseline_turns` before them,
  and the window spends `slope_floor_usd` more than the old rate would have.
  Consecutive starts that qualify merge into one spike, spanning from the start
  with the highest ratio (where the rate changed) to the end of the last window.
  With `per_minute`, the rate is per active minute instead: the gap before each
  request, capped at `idle_cap` (an idle hour is not an hour of work) and at least
  `min_gap`. Outliers (single requests far above both sides, whatever their
  dollar size) count at their baseline in these windows, so one expensive request
  does not also read as a slope change.

Spikes are reported when they start in [since, until); history up to `lookback`
before `since` feeds the baselines.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import accumulate, pairwise
from statistics import median

from capcom.cost.model import price
from capcom.cost.prices import is_placeholder
from capcom.types import Index, PriceTable, Spike, SpikeKind

TICK = timedelta(microseconds=1)  # a single request's span: [t, t + TICK)


@dataclass(frozen=True, slots=True)
class SpikeParams:
    history: int = 20
    min_history: int = 5
    single_ratio: float = 5.0
    single_floor_usd: float = 1.0
    baseline_turns: int = 20
    window_turns: int = 10
    slope_ratio: float = 1.5
    slope_floor_usd: float = 1.0
    idle_cap: timedelta = timedelta(minutes=5)
    min_gap: timedelta = timedelta(seconds=1)
    lookback: timedelta = timedelta(hours=12)
    per_minute: bool = False  # slope rate per active minute (default: per turn)


DEFAULT = SpikeParams()


@dataclass(frozen=True, slots=True)
class Turn:
    """One request of a thread, reduced to what detection needs."""

    timestamp: datetime
    message_id: str
    usd: float


def outliers(turns: list[Turn], p: SpikeParams = DEFAULT) -> dict[int, float]:
    """Turns far above both the median of the thread's previous `history` turns and
    the median of its next `window_turns` (a request that starts a lasting new
    level is a slope change, not an outlier), mapped to the previous median."""
    out: dict[int, float] = {}
    for i, t in enumerate(turns):
        hist = [x.usd for x in turns[max(0, i - p.history) : i]]
        if len(hist) < p.min_history:
            continue
        med = median(hist)
        if t.usd <= p.single_ratio * med:
            continue
        after = [x.usd for x in turns[i + 1 : i + 1 + p.window_turns]]
        if after and t.usd <= p.single_ratio * median(after):
            continue
        out[i] = med
    return out


def single_spikes(
    session_id: str,
    agent_id: str | None,
    turns: list[Turn],
    found: dict[int, float],
    p: SpikeParams = DEFAULT,
) -> list[Spike]:
    """The outliers in `found` whose excess is above the dollar floor."""
    return [
        Spike(
            SpikeKind.SINGLE_REQUEST,
            start=turns[i].timestamp,
            end=turns[i].timestamp + TICK,
            cost_usd=turns[i].usd,
            baseline_usd=med,
            session_id=session_id,
            agent_id=agent_id,
            message_ids=(turns[i].message_id,),
        )
        for i, med in sorted(found.items())
        if turns[i].usd - med > p.single_floor_usd
    ]


def _active_minutes(turns: list[Turn], p: SpikeParams) -> list[float]:
    """The capped gap before each turn, in minutes; the first turn gets the median of
    the others (its gap is unknown)."""
    gaps = [
        min(max(b.timestamp - a.timestamp, p.min_gap), p.idle_cap).total_seconds() / 60
        for a, b in pairwise(turns)
    ]
    first = median(gaps) if gaps else p.min_gap.total_seconds() / 60
    return [first, *gaps]


def slope_spikes(
    session_id: str,
    agent_id: str | None,
    turns: list[Turn],
    found: dict[int, float],
    p: SpikeParams = DEFAULT,
) -> list[Spike]:
    b, w = p.baseline_turns, p.window_turns
    n = len(turns)
    if n < b + w:
        return []
    cost = [found.get(i, t.usd) for i, t in enumerate(turns)]
    c = [0.0, *accumulate(cost)]
    units = _active_minutes(turns, p) if p.per_minute else [1.0] * n
    d = [0.0, *accumulate(units)]

    def rate(lo: int, hi: int) -> float:
        return (c[hi] - c[lo]) / (d[hi] - d[lo])

    # runs of consecutive qualifying starts: (best start, last start, baseline rate);
    # the best start (highest window/baseline ratio) is where the rate changed
    runs: list[tuple[int, int, float]] = []
    best_ratio = 0.0
    for i in range(b, n - w + 1):
        base = rate(i - b, i)
        win = rate(i, i + w)
        excess = (c[i + w] - c[i]) - base * (d[i + w] - d[i])
        if not (win >= p.slope_ratio * base and excess >= p.slope_floor_usd):
            continue
        ratio = win / base if base > 0 else float("inf")
        if runs and runs[-1][1] == i - 1:
            lo, _, lo_base = runs[-1]
            if ratio > best_ratio:
                lo, lo_base, best_ratio = i, base, ratio
            runs[-1] = (lo, i, lo_base)
        else:
            runs.append((i, i, base))
            best_ratio = ratio
    out: list[Spike] = []
    for lo, last, base in runs:
        hi = last + w  # exclusive turn index
        span = turns[lo:hi]
        out.append(
            Spike(
                SpikeKind.SLOPE_CHANGE,
                start=span[0].timestamp,
                end=span[-1].timestamp + TICK,
                cost_usd=sum(t.usd for t in span),
                baseline_usd=base * (d[hi] - d[lo]),
                session_id=session_id,
                agent_id=agent_id,
                message_ids=tuple(t.message_id for t in span),
            )
        )
    return out


def thread_spikes(
    session_id: str, agent_id: str | None, turns: list[Turn], p: SpikeParams = DEFAULT
) -> list[Spike]:
    found = outliers(turns, p)
    singles = single_spikes(session_id, agent_id, turns, found, p)
    return singles + slope_spikes(session_id, agent_id, turns, found, p)


def find_spikes(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    session_id: str | None = None,
    params: SpikeParams = DEFAULT,
) -> list[Spike]:
    """Spikes starting in [since, until), per agent thread, in time order."""
    threads: dict[tuple[str, str | None], list[Turn]] = defaultdict(list)
    for req in index.requests(since=since - params.lookback, until=until, session_id=session_id):
        if is_placeholder(req.model):
            continue
        usd = price(req, table).total
        threads[(req.session_id, req.agent_id)].append(Turn(req.timestamp, req.message_id, usd))
    out: list[Spike] = []
    for (sid, aid), turns in threads.items():
        out.extend(s for s in thread_spikes(sid, aid, turns, params) if since <= s.start < until)
    out.sort(key=lambda s: (s.start, s.session_id or "", s.agent_id or "", s.kind))
    return out
