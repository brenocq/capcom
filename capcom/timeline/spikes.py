"""Spike detection (docs/design.md, "Spikes and slope changes"), per agent thread
and per session.

A thread is one session's main thread (agent_id None) or one of its subagents. Two
kinds, both from ratios against the thread's own recent past:

- SINGLE_REQUEST: a request costing more than `single_ratio` times the median of
  the thread's previous `history` requests and of its next `window_turns`, and
  more than `single_floor_usd` above the former, which is the baseline.
  Typically a cache rewrite of a big context.
- SLOPE_CHANGE: the cost per turn over the next `window_turns` requests
  is at least `slope_ratio` times the rate over the `baseline_turns` before them,
  and the window spends `slope_floor_usd` more than the old rate would have.
  Qualifying starts merge into one spike, spanning from the start with the
  largest window excess (where the rate changed) to the end of the last window.
  With `per_minute`, the rate is per active minute instead: the gap before each
  request, capped at `idle_cap` (an idle hour is not an hour of work) and at least
  `min_gap`. Outliers (single requests far above both sides, whatever their
  dollar size) count at their baseline in these windows, so one expensive request
  does not also read as a slope change.

Spikes are reported when they start in [since, until); history up to `lookback`
before `since` feeds the baselines, and requests up to `lookahead` after `until`
the look-aheads. A slope change also needs most turns of its window to be up (the
median turn's rate at `slope_ratio` times the baseline). Slope spans never overlap
or share requests: a qualifying start inside the previous span extends it, and
the span starts at the start with the largest window excess.

Session scope (session_spikes) catches what no thread can show: subagent fan-out,
where each new subagent has no history of its own. All threads of a session are
summed per `session_bucket` of wall time (not per turn: threads interleave, and a
burst of subagents raises the spend per minute, not per request), and the same
ratio of windows runs on that series. Two minutes smooths the interleaving of
threads while a burst of subagents running for ten minutes still fills a window.
A session span must spend in at least `session_threads` threads; the thread spikes
inside it are counted against it, so it is dropped when they explain most of its
excess, and otherwise their excess is added to its baseline.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    lookahead: timedelta = timedelta(hours=12)  # read past `until` for look-aheads
    per_minute: bool = False  # slope rate per active minute (default: per turn)
    # Session scope: all threads of a session summed per bucket of wall time.
    session_bucket: timedelta = timedelta(minutes=2)
    session_baseline: int = 10  # buckets
    session_window: int = 5  # buckets
    session_min_baseline: int = 3  # buckets: a short session's baseline is all it has
    session_ratio: float = 2.0
    session_floor_usd: float = 3.0  # excess left after the thread spikes inside
    session_explained: float = 0.8  # drop a span this much explained by thread spikes
    session_threads: int = 2  # threads that must spend in the span


DEFAULT = SpikeParams()


@dataclass(frozen=True, slots=True)
class Turn:
    """One request of a thread, reduced to what detection needs."""

    timestamp: datetime
    message_id: str
    usd: float
    agent_id: str | None = None


def outliers(turns: list[Turn], p: SpikeParams = DEFAULT) -> dict[int, tuple[float, bool]]:
    """Turns far above both the median of the thread's previous `history` turns and
    the median of its next `window_turns` (a request that starts a lasting new
    level is a slope change, not an outlier), mapped to (the previous median, whether
    the look-ahead was complete). A turn with fewer next turns is judged on those it
    has; only one with a complete look-ahead can be a single-request spike."""
    out: dict[int, tuple[float, bool]] = {}
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
        out[i] = (med, len(after) == p.window_turns)
    return out


def single_spikes(
    session_id: str,
    agent_id: str | None,
    turns: list[Turn],
    found: dict[int, tuple[float, bool]],
    p: SpikeParams = DEFAULT,
) -> list[Spike]:
    """The outliers in `found` with a complete look-ahead whose excess is above the
    dollar floor."""
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
        for i, (med, complete) in sorted(found.items())
        if complete and turns[i].usd - med > p.single_floor_usd
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


def rises(
    cost: list[float],
    units: list[float],
    b: int,
    w: int,
    ratio: float,
    floor: float,
    min_b: int | None = None,
) -> list[tuple[int, int, float]]:
    """Spans [lo, hi) where the rate (cost per unit) over the next `w` steps is at
    least `ratio` times the rate over the `b` before them (or the `min_b` or more
    there are), the window spends `floor`
    more than the old rate would have, and most steps of the window are up (the
    median step's rate at `ratio` times the baseline). Qualifying starts whose
    windows overlap merge into one span, from the start with the largest window
    excess (where the rate changed) to the end of the last window; each span comes
    with the baseline rate at its start."""
    n = len(cost)
    first = b if min_b is None else min_b
    if n < first + w:
        return []
    c = [0.0, *accumulate(cost)]
    d = [0.0, *accumulate(units)]
    per_unit = [x / u for x, u in zip(cost, units, strict=True)]

    def rate(lo: int, hi: int) -> float:
        return (c[hi] - c[lo]) / (d[hi] - d[lo])

    # Runs of qualifying starts: [best start, last start, baseline rate at the best
    # start, best excess].
    runs: list[list[float]] = []
    for i in range(first, n - w + 1):
        base = rate(max(0, i - b), i)
        win = rate(i, i + w)
        excess = (c[i + w] - c[i]) - base * (d[i + w] - d[i])
        stays_up = median(per_unit[i : i + w]) >= ratio * base  # most steps rose
        if not (win >= ratio * base and excess >= floor and stays_up):
            continue
        if runs and i < runs[-1][1] + w:  # inside the previous span: same spike
            run = runs[-1]
            run[1] = i
            if excess > run[3]:
                run[0], run[2], run[3] = i, base, excess
        else:
            runs.append([i, i, base, excess])
    return [(int(lo), int(last) + w, base) for lo, last, base, _ in runs]


def slope_spikes(
    session_id: str,
    agent_id: str | None,
    turns: list[Turn],
    found: dict[int, tuple[float, bool]],
    singles: Sequence[Spike] = (),
    p: SpikeParams = DEFAULT,
) -> list[Spike]:
    """Slope changes of one thread. The excess of the `singles` inside a span is
    added to its baseline, so each dollar of excess is counted once."""
    b, w = p.baseline_turns, p.window_turns
    n = len(turns)
    cost = [found[i][0] if i in found else t.usd for i, t in enumerate(turns)]
    units = _active_minutes(turns, p) if p.per_minute else [1.0] * n
    d = [0.0, *accumulate(units)]
    runs = rises(cost, units, b, w, p.slope_ratio, p.slope_floor_usd)
    single_excess = {s.message_ids[0]: s.excess_usd for s in singles}
    out: list[Spike] = []
    for lo, hi, base in runs:
        span = turns[lo:hi]
        counted = sum(single_excess.get(t.message_id, 0.0) for t in span)
        out.append(
            Spike(
                SpikeKind.SLOPE_CHANGE,
                start=span[0].timestamp,
                end=span[-1].timestamp + TICK,
                cost_usd=sum(t.usd for t in span),
                baseline_usd=base * (d[hi] - d[lo]) + counted,
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
    return singles + slope_spikes(session_id, agent_id, turns, found, singles, p)


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class Bucket:
    """One step of a session's series: the requests of all its threads in one
    bucket of wall time (positions in the session's time-ordered turns)."""

    cost: float = 0.0
    lo: int = 0  # first turn
    hi: int = 0  # past the last turn (lo == hi: an empty bucket inside a short pause)


def session_buckets(turns: list[Turn], p: SpikeParams = DEFAULT) -> list[Bucket]:
    """The session's spend per `session_bucket` of wall time, aligned to the epoch.
    Empty buckets count while the session pauses for up to `idle_cap` (threads that
    wait on each other); a longer pause is cut out, as an idle hour is not an hour
    of work."""
    out: list[Bucket] = []
    idle = int(p.idle_cap / p.session_bucket)
    last: int | None = None
    for i, t in enumerate(turns):
        k = (t.timestamp - EPOCH) // p.session_bucket
        if k != last:
            if last is not None:
                out.extend(Bucket(0.0, i, i) for _ in range(min(k - last - 1, idle)))
            out.append(Bucket(0.0, i, i))
            last = k
        out[-1].cost += t.usd
        out[-1].hi = i + 1
    return out


def session_spikes(
    session_id: str, turns: list[Turn], threads: Sequence[Spike], p: SpikeParams = DEFAULT
) -> list[Spike]:
    """Slope changes of a whole session: its threads' spend summed per bucket of
    wall time, so a burst of new subagents (which have no history of their own)
    shows. `turns` are all the session's requests in time order, `threads` its
    thread spikes. A span is dropped when it spends in fewer than `session_threads`
    threads or when the thread spikes inside it explain `session_explained` of its
    excess; otherwise their excess (the share of each inside the span) is added to
    the baseline, so each dollar of excess is counted once."""
    buckets = session_buckets(turns, p)
    cost = [x.cost for x in buckets]
    units = [1.0] * len(buckets)
    b, w = p.session_baseline, p.session_window
    where = {t.message_id: i for i, t in enumerate(turns)}
    out: list[Spike] = []
    for lo, hi, base in rises(
        cost, units, b, w, p.session_ratio, p.session_floor_usd, p.session_min_baseline
    ):
        a, z = buckets[lo].lo, buckets[hi - 1].hi
        span = turns[a:z]
        if not span or len({t.agent_id for t in span}) < p.session_threads:
            continue
        spent = sum(t.usd for t in span)
        old = base * (hi - lo)
        explained = 0.0
        for s in threads:
            inside = [turns[j].usd for m in s.message_ids if a <= (j := where[m]) < z]
            if inside and s.cost_usd > 0:
                explained += s.excess_usd * sum(inside) / s.cost_usd
        raw = spent - old
        if explained >= p.session_explained * raw or raw - explained < p.session_floor_usd:
            continue
        out.append(
            Spike(
                SpikeKind.SLOPE_CHANGE,
                start=span[0].timestamp,
                end=span[-1].timestamp + TICK,
                cost_usd=spent,
                baseline_usd=old + explained,
                session_id=session_id,
                agent_id=None,
                message_ids=tuple(t.message_id for t in span),
                scope="session",
            )
        )
    return out


def find_spikes(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    session_id: str | None = None,
    params: SpikeParams = DEFAULT,
) -> list[Spike]:
    """Spikes starting in [since, until), per agent thread and per session (scope
    "session", see session_spikes), in time order."""
    sessions: dict[str, list[Turn]] = defaultdict(list)
    threads: dict[tuple[str, str | None], list[Turn]] = defaultdict(list)
    lo, hi = since - params.lookback, until + params.lookahead
    for req in index.requests(since=lo, until=hi, session_id=session_id):
        if is_placeholder(req.model):
            continue
        usd = price(req, table).total
        turn = Turn(req.timestamp, req.message_id, usd, req.agent_id)
        sessions[req.session_id].append(turn)
        threads[(req.session_id, req.agent_id)].append(turn)
    found: dict[str, list[Spike]] = defaultdict(list)
    for (sid, aid), turns in threads.items():
        found[sid].extend(thread_spikes(sid, aid, turns, params))
    for sid, turns in sessions.items():
        found[sid].extend(session_spikes(sid, turns, found[sid], params))
    out = [s for spikes in found.values() for s in spikes if since <= s.start < until]
    out.sort(key=lambda s: (s.start, s.session_id or "", s.scope, s.agent_id or "", s.kind))
    return out
