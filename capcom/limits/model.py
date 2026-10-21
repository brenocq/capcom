"""Reading the recorded usage windows, fitting what they cost, forecasting the reset.

A window's samples (`limits.jsonl`) are cut into segments, one per reset: within a
segment the percentage only rises (a lower reading from a session that had not
refreshed yet is noise and is dropped), and it starts from 0 at the window's start
(`resets_at` minus the window's length) when the reset time is known.

The fit: between consecutive points of a segment, the rise in percent against what
the index's requests spent in that interval. With enough intervals (at least
`PER_TYPE_MIN` and three per unknown) it fits a weight per model and token category,
in percent per million tokens, by non-negative least squares; with fewer, a single
percent per dollar of capcom's API-equivalent prices (total rise over total spend),
and says so. Usage from other machines or claude.ai counts toward the limits but is
not in the logs, so it shows up as fit error. The fit's own uncertainty is a factor
on its rate (`rate_factor`): a t-interval on the residuals, pooled with a wide prior
(times or divided by 2.5 from a single rise) so that a few agreeing rises do not
claim more than they know; it narrows as rises come.

The forecast, from the second reading on: the current segment's spend in fixed
buckets (15 minutes for the 5-hour window, an hour for the weekly one), turned into
percent by the fit, resampled in blocks for the time left until the reset, each draw
with its own rate drawn from the fit's uncertainty: the median and an 80 % range
(10th to 90th percentile) of the value at reset, a fan of the same at points along
the way, and when the paths reach 100 % (median, earliest and latest). With few
rises the range also covers the plain straight line of percent against time through
the current window's readings (its slope times or divided by the same factor).
"""

from __future__ import annotations

import bisect
import json
import math
import os
import random
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from capcom.cost.model import price
from capcom.cost.prices import canonical, is_placeholder
from capcom.cost.top import project_label
from capcom.limits.record import LOCK_FILE, SAMPLES_FILE
from capcom.types import EventKind, Index, LimitSample, LimitWindow, PriceTable, Request

LENGTH = {LimitWindow.FIVE_HOUR: timedelta(hours=5), LimitWindow.SEVEN_DAY: timedelta(days=7)}
BUCKET = {LimitWindow.FIVE_HOUR: timedelta(minutes=15), LimitWindow.SEVEN_DAY: timedelta(hours=1)}
LOOKBACK = {LimitWindow.FIVE_HOUR: timedelta(hours=2), LimitWindow.SEVEN_DAY: timedelta(hours=24)}
BLOCK = {LimitWindow.FIVE_HOUR: 4, LimitWindow.SEVEN_DAY: 6}  # buckets per resampled block
LABEL = {LimitWindow.FIVE_HOUR: "5-hour window", LimitWindow.SEVEN_DAY: "weekly limit"}
CATEGORIES = ("input", "cache_write", "cache_read", "output")
SAME_RESET = timedelta(minutes=10)  # reset times closer than this are one reset
BURST = timedelta(seconds=30)  # rises closer than this are one refresh burst
NOISE_DROP = 5.0  # without reset times, a fall of more than this many points is a reset
MIN_INTERVALS = 1  # fewer: no fit
MIN_READINGS = 2  # recorded readings (after dropping stale ones) before a fit
MIN_RISE = 1.0  # percent points the intervals must add up to
FEW_RISES = 6  # fewer: the forecast is rough and also checked against a straight line
PRIOR_FACTOR = 2.5  # the rate's 80 % factor from a single rise
PRIOR_DF = 3  # the prior's weight, in rises, when pooled with the residuals
MAX_FACTOR = 6.0
Z80 = 1.2815515655446004  # the normal's 90th percentile
# the t distribution's 90th percentile by degrees of freedom (1 to 30)
T80 = (
    3.078, 1.886, 1.638, 1.533, 1.476, 1.440, 1.415, 1.397, 1.383, 1.372,
    1.363, 1.356, 1.350, 1.345, 1.341, 1.337, 1.333, 1.330, 1.328, 1.325,
    1.323, 1.321, 1.319, 1.318, 1.316, 1.315, 1.314, 1.313, 1.311, 1.310,
)  # fmt: skip
PER_TYPE_MIN = 12  # intervals before a per-type fit is tried
DRAWS = 400
FAN_POINTS = 24
SOURCES = ("statusline", "rate_limit_error")

Key = tuple[str, str]  # (model, category)
NOT_ENOUGH = "it needs two readings with a rise and some spend between them"


# --------------------------------------------------------------------------- samples


def samples_path(state_dir: Path) -> Path:
    return state_dir / SAMPLES_FILE


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def parse_sample(line: str | bytes) -> LimitSample | None:
    """One JSONL line as a LimitSample, or None when it is not one."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    t = _dt(d.get("time"))
    used = d.get("used_percentage")
    try:
        window = LimitWindow(str(d.get("window")))
    except ValueError:
        return None
    if t is None or isinstance(used, bool) or not isinstance(used, int | float):
        return None
    if not math.isfinite(used):
        return None
    sid, source = d.get("session_id"), d.get("source")
    return LimitSample(
        time=t,
        window=window,
        used_percentage=float(used),
        resets_at=_dt(d.get("resets_at")),
        session_id=sid if isinstance(sid, str) else None,
        source=source if isinstance(source, str) else "statusline",
    )


def load_samples(path: Path) -> list[LimitSample]:
    """Every sample in `path`, in time order; bad lines and a partial last line (a
    recorder still writing) are skipped. A missing file is no samples."""
    out: list[LimitSample] = []
    try:
        with path.open("rb") as f:
            for line in f:
                if not line.endswith(b"\n"):
                    break
                s = parse_sample(line)
                if s is not None:
                    out.append(s)
    except OSError:
        return []
    out.sort(key=lambda s: s.time)
    return out


def sample_json(s: LimitSample) -> str:
    return json.dumps(
        {
            "time": s.time.astimezone(UTC).isoformat(),
            "window": s.window.value,
            "used_percentage": s.used_percentage,
            "resets_at": None if s.resets_at is None else s.resets_at.astimezone(UTC).isoformat(),
            "session_id": s.session_id,
            "source": s.source,
        },
        separators=(",", ":"),
    )


def _same(a: LimitSample, b: LimitSample) -> bool:
    return a.window == b.window and a.source == b.source and a.time == b.time


def append_samples(state_dir: Path, new: Sequence[LimitSample]) -> list[LimitSample]:
    """Append the samples of `new` that the file does not hold yet (same window, time
    and source), under the recorder's lock; return those appended."""
    import fcntl

    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        have = {(s.window, s.source, s.time) for s in load_samples(samples_path(state_dir))}
        fresh = [s for s in new if (s.window, s.source, s.time) not in have]
        fresh = [s for i, s in enumerate(fresh) if not any(_same(s, o) for o in fresh[:i])]
        if fresh:
            data = "".join(sample_json(s) + "\n" for s in fresh).encode()
            fd = os.open(samples_path(state_dir), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        return fresh
    finally:
        os.close(lock)


@dataclass(slots=True)
class Seed:
    samples: list[LimitSample]
    errors: int  # rate-limit errors in the index
    unknown_window: int  # of those, the ones whose record does not say which window


def _epoch(v: Any) -> datetime | None:
    """`v` as a time when it is plausible epoch seconds (0 < v < 1e11), else None."""
    if not isinstance(v, int | float) or isinstance(v, bool) or not 0 < v < 1e11:
        return None
    try:
        return datetime.fromtimestamp(v, UTC)
    except (OverflowError, ValueError, OSError):
        return None


def seed_samples(index: Index) -> Seed:
    """A 100 % sample for each rate-limit error in the index whose event names the
    window it hit (`rate_limit_type` or `window`, and `resets_at` in epoch seconds
    when given); the others are counted and skipped."""
    out: list[LimitSample] = []
    errors = unknown = 0
    for e in index.events(kinds=frozenset({EventKind.API_ERROR})):
        if e.attrs.get("error") != "rate_limit":
            continue
        errors += 1
        name = e.attrs.get("rate_limit_type") or e.attrs.get("window")
        try:
            window = LimitWindow(str(name))
        except ValueError:
            unknown += 1
            continue
        reset_t = _epoch(e.attrs.get("resets_at"))
        out.append(
            LimitSample(e.timestamp, window, 100.0, reset_t, e.session_id, "rate_limit_error")
        )
    out.sort(key=lambda s: s.time)
    return Seed(out, errors, unknown)


# --------------------------------------------------------------------------- segments


@dataclass(slots=True)
class Segment:
    """One window between resets. `points` are (time, percent), rising; the first is
    the window's start at 0 % when the reset time is known."""

    window: LimitWindow
    start: datetime
    resets_at: datetime | None
    samples: list[LimitSample] = field(default_factory=list)
    start_known: bool = False

    @property
    def last(self) -> LimitSample:
        return self.samples[-1]

    @property
    def end(self) -> datetime:
        return self.resets_at or self.last.time

    def points(self) -> list[tuple[datetime, float]]:
        pts = [(self.start, 0.0)] if self.start_known else []
        return pts + [(s.time, s.used_percentage) for s in self.samples]


def _new_reset(cur: Segment, s: LimitSample) -> bool:
    """Whether sample `s` belongs to a window after `cur`'s reset."""
    r = cur.resets_at
    if r is not None:
        moved = s.resets_at is not None and s.resets_at - r >= SAME_RESET
        return moved or s.time >= r
    return s.used_percentage < cur.last.used_percentage - NOISE_DROP


def _stale(cur: Segment | None, s: LimitSample) -> bool:
    """Whether `s` reports a window that is already over: its reset is at or before
    its own time, or before the current segment's reset (an idle session still
    showing the old window)."""
    if s.resets_at is None:
        return False
    if s.resets_at <= s.time:
        return True
    r = cur.resets_at if cur is not None else None
    return r is not None and r - s.resets_at >= SAME_RESET


def _floor(done: Sequence[Segment]) -> datetime | None:
    """The earliest a new segment may start: after the previous one's last reading."""
    return done[-1].last.time if done else None


def segments(samples: Iterable[LimitSample], window: LimitWindow) -> list[Segment]:
    out: list[Segment] = []
    cur: Segment | None = None
    for s in sorted((s for s in samples if s.window == window), key=lambda s: s.time):
        if _stale(cur, s):
            continue
        new = cur is None or _new_reset(cur, s)
        if new:
            start, known = s.time, False
            if s.resets_at is not None and s.resets_at - LENGTH[window] <= s.time:
                start, known = s.resets_at - LENGTH[window], True
            floor = _floor(out)
            if floor is not None and start < floor:  # inconsistent readings: no 0 % start
                start, known = floor, False
            cur = Segment(window, start, s.resets_at, [], known)
            out.append(cur)
        assert cur is not None
        if cur.resets_at is None and s.resets_at is not None:
            cur.resets_at = s.resets_at
            first = cur.samples[0].time if cur.samples else s.time
            begin = s.resets_at - LENGTH[window]
            floor = _floor(out[:-1])
            if begin <= first and (floor is None or begin >= floor):
                cur.start, cur.start_known = begin, True
        # each session reports the limits as of its own last reply, so readings of one
        # window interleave stale values; usage only rises, so keep the running maximum
        if cur.samples and s.used_percentage < cur.last.used_percentage:
            continue  # a stale reading
        if cur.samples and s.used_percentage == cur.last.used_percentage:
            continue  # no rise: the earlier sample already says when it was reached
        if cur.samples and s.time - cur.last.time < BURST:
            # sessions refreshing together, the lower ones stale: one reading, not a
            # rise of several points in a few milliseconds
            cur.samples[-1] = s
            continue
        cur.samples.append(s)
    return out


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime
    rise: float


def intervals(segs: Iterable[Segment]) -> list[Interval]:
    """Consecutive points of each segment, in time order (they never overlap)."""
    out: list[Interval] = []
    for seg in segs:
        pts = seg.points()
        for (t0, p0), (t1, p1) in pairwise(pts):
            if t1 > t0 and p1 >= p0:
                out.append(Interval(t0, t1, p1 - p0))
    out.sort(key=lambda i: i.start)
    return out


# --------------------------------------------------------------------------- spend


@dataclass(slots=True)
class Spend:
    """What some requests spent: API-equivalent USD and tokens per (model, category)."""

    usd: float = 0.0
    tokens: dict[Key, float] = field(default_factory=dict)
    requests: int = 0

    def add(self, r: Request, model: str, usd: float) -> None:
        self.usd += usd
        self.requests += 1
        u = r.usage
        for cat, n in zip(
            CATEGORIES, (u.input, u.cache_write, u.cache_read, u.output), strict=True
        ):
            if n:
                k = (model, cat)
                self.tokens[k] = self.tokens.get(k, 0.0) + n


class Bins:
    """Non-overlapping [start, end) bins, sorted, each with a payload."""

    def __init__(self, items: Sequence[tuple[datetime, datetime, Any]]) -> None:
        self.items = sorted(items, key=lambda x: x[0])
        self.starts = [x[0] for x in self.items]

    def find(self, t: datetime) -> Any:
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and t < self.items[i][1]:
            return self.items[i][2]
        return None


def _merged(ranges: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(r for r in ranges if r[1] > r[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def counted(r: Request) -> bool:
    return not r.is_api_error and not is_placeholder(r.model)


def scan_spend(
    index: Index,
    table: PriceTable,
    bins: Sequence[Bins],
    on_request: Callable[[Request, str, float], None] | None = None,
) -> None:
    """One pass over the requests the bins cover, adding each to every bin holding
    it (a bin's payload is a Spend, or a callable taking the request)."""
    ranges = [(s, e) for b in bins for s, e, _ in b.items]
    for since, until in _merged(ranges):
        for r in index.requests(since=since, until=until):
            if not counted(r):
                continue
            model = canonical(table, r.model)
            usd = price(r, table).total
            for b in bins:
                payload = b.find(r.timestamp)
                if isinstance(payload, Spend):
                    payload.add(r, model, usd)
                elif payload is not None:
                    payload(r, model, usd)
            if on_request is not None:
                on_request(r, model, usd)


# --------------------------------------------------------------------------- fit


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small, well-scaled systems)."""
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-300:
            continue
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for c in range(n - 1, -1, -1):
        if abs(m[c][c]) < 1e-300:
            continue
        x[c] = (m[c][n] - sum(m[c][k] * x[k] for k in range(c + 1, n))) / m[c][c]
    return x


def nnls(rows: Sequence[Sequence[float]], b: Sequence[float], max_iter: int = 200) -> list[float]:
    """Non-negative least squares (Lawson and Hanson): x >= 0 minimizing |Ax - b|.
    Columns are scaled to unit norm first; a tiny ridge keeps the systems solvable."""
    k = len(rows[0]) if rows else 0
    if k == 0:
        return []
    norms = [math.sqrt(sum(r[j] ** 2 for r in rows)) or 1.0 for j in range(k)]
    a = [[r[j] / norms[j] for j in range(k)] for r in rows]
    ata = [[sum(r[i] * r[j] for r in a) for j in range(k)] for i in range(k)]
    atb = [sum(r[i] * y for r, y in zip(a, b, strict=True)) for i in range(k)]
    ridge = 1e-10 * (sum(ata[i][i] for i in range(k)) / k or 1.0)

    def solve(p: list[int]) -> list[float]:
        sub = [[ata[i][j] + (ridge if i == j else 0.0) for j in p] for i in p]
        z = _solve(sub, [atb[i] for i in p])
        out = [0.0] * k
        for i, v in zip(p, z, strict=True):
            out[i] = v
        return out

    x = [0.0] * k
    passive: list[int] = []
    tol = 1e-12 * max(1.0, max(abs(v) for v in atb))
    for _ in range(max_iter):
        w = [atb[i] - sum(ata[i][j] * x[j] for j in range(k)) for i in range(k)]
        free = [i for i in range(k) if i not in passive and w[i] > tol]
        if not free:
            break
        passive.append(max(free, key=lambda i: w[i]))
        for _ in range(max_iter):
            s = solve(passive)
            if all(s[i] > 0 for i in passive):
                x = s
                break
            alpha = min(x[i] / (x[i] - s[i]) for i in passive if s[i] <= 0 and x[i] != s[i])
            alpha = max(0.0, min(1.0, alpha))
            x = [x[i] + alpha * (s[i] - x[i]) for i in range(k)]
            passive = [i for i in passive if x[i] > tol]
            if not passive:
                x = [0.0] * k
                break
    return [max(0.0, x[j]) / norms[j] for j in range(k)]


@dataclass(slots=True)
class Fit:
    """Percent of a window per unit of spend. `method` is "per_type" (a weight per
    model and category, in percent per million tokens) or "per_usd" (one percent per
    dollar of API-equivalent prices, when there are too few samples for more)."""

    window: LimitWindow
    method: str
    intervals: int
    samples: int
    per_usd: float  # percent per dollar (the implied average for per_type)
    weights: dict[Key, float] = field(default_factory=dict)  # percent per million tokens
    mean_abs_error: float = 0.0  # percent points per interval
    r2: float | None = None
    rate_factor: float = PRIOR_FACTOR  # the rate's 80 % range: divided to multiplied by it
    readings: int = 0  # recorded readings the intervals come from

    def percent(self, spend: Spend) -> float:
        if self.method == "per_type":
            return sum(self.weights.get(k, 0.0) * n / 1e6 for k, n in spend.tokens.items())
        return self.per_usd * spend.usd

    @property
    def rough(self) -> bool:
        return self.intervals < FEW_RISES

    @property
    def usd_per_percent(self) -> float | None:
        return 1 / self.per_usd if self.per_usd > 0 else None

    @property
    def note(self) -> str:
        """The fit in plain words, for the cards and `capcom limits`."""
        per = self.usd_per_percent
        dollars = "?" if per is None else f"${per:,.0f}" if per >= 10 else f"${per:,.2f}"
        rises = f"{self.intervals} rise{'' if self.intervals == 1 else 's'}"
        out = f"About {dollars} of API-equivalent spend per 1% of your {LABEL[self.window]}"
        if self.method == "per_type" and sum(1 for w in self.weights.values() if w > 0) > 1:
            out += f", from {rises}, with a separate weight for each model and token type"
        else:
            out += f", from {rises}"
        if self.rough:
            return out + " (rough)."
        return out + f"; readings stray from it by {self.mean_abs_error:.1f} points on average."


def _t80(df: int) -> float:
    return T80[df - 1] if 1 <= df <= len(T80) else Z80


def rate_factor(ys: Sequence[float], preds: Sequence[float], segments: int = 1) -> float:
    """The fit's 80 % range as a factor on its rate: from the residuals of the rises
    (`ys`, percent points) against the fit's `preds`, pooled with a wide prior worth
    `PRIOR_DF` rises (a single rise gives `PRIOR_FACTOR`), plus the whole-point
    rounding of each segment's last reading; a t quantile on the pooled degrees of
    freedom."""
    n, rise = len(ys), sum(ys)
    if n == 0 or rise <= 0:
        return PRIOR_FACTOR
    s0 = math.log(PRIOR_FACTOR) / _t80(PRIOR_DF)  # per rise, relative
    if n >= 2:
        mean = rise / n
        data = sum((p - y) ** 2 for p, y in zip(preds, ys, strict=True)) / (n - 1) / mean**2
        pooled = (PRIOR_DF * s0**2 + (n - 1) * data) / (PRIOR_DF + n - 1)
    else:
        pooled = s0**2
    rounding = 0.5 * math.sqrt(max(segments, 1)) / rise
    half = _t80(PRIOR_DF + n - 1) * math.sqrt(pooled / n + rounding**2)
    return min(math.exp(half), MAX_FACTOR)


def fit(
    window: LimitWindow,
    items: Sequence[tuple[Interval, Spend]],
    samples: int,
    segments: int = 1,
    readings: int = 0,
) -> Fit | None:
    """The fit of `items` (each interval's rise with the spend in it, from `segments`
    windows and `readings` readings); None with too few intervals or no rise and
    spend to learn from."""
    items = [(iv, sp) for iv, sp in items if sp.requests or iv.rise > 0]
    rise = sum(iv.rise for iv, _ in items)
    usd = sum(sp.usd for _, sp in items)
    if len(items) < MIN_INTERVALS or rise < MIN_RISE or usd <= 0:
        return None
    keys = sorted({k for _, sp in items for k in sp.tokens})
    ys = [iv.rise for iv, _ in items]
    per_usd = rise / usd
    result = Fit(window, "per_usd", len(items), samples, per_usd, readings=readings)
    if len(items) >= max(PER_TYPE_MIN, 3 * len(keys)):
        rows = [[sp.tokens.get(k, 0.0) / 1e6 for k in keys] for _, sp in items]
        w = nnls(rows, ys)
        weights = {k: v for k, v in zip(keys, w, strict=True)}
        typed = Fit(window, "per_type", len(items), samples, per_usd, weights, readings=readings)
        typed.per_usd = sum(typed.percent(sp) for _, sp in items) / usd
        result = typed
    pred = [result.percent(sp) for _, sp in items]
    result.mean_abs_error = sum(abs(p - y) for p, y in zip(pred, ys, strict=True)) / len(ys)
    result.rate_factor = rate_factor(ys, pred, segments)
    mean = sum(ys) / len(ys)
    tot = sum((y - mean) ** 2 for y in ys)
    if tot > 0:
        result.r2 = 1 - sum((p - y) ** 2 for p, y in zip(pred, ys, strict=True)) / tot
    return result


# --------------------------------------------------------------------------- forecast


@dataclass(slots=True)
class Line:
    """The plain straight line of percent against time through the current window's
    readings: `percent` at `time`, rising `per_hour`."""

    time: datetime
    percent: float
    per_hour: float

    def at(self, t: datetime, factor: float = 1.0) -> float:
        return self.percent + self.per_hour * factor * (t - self.time) / timedelta(hours=1)

    def reaches_full(self, factor: float = 1.0) -> datetime | None:
        rate = self.per_hour * factor
        if self.percent >= 100:
            return self.time
        if rate <= 0:
            return None
        return self.time + timedelta(hours=(100 - self.percent) / rate)


@dataclass(slots=True)
class Forecast:
    reading: float  # the last recorded percent
    now: float  # the reading plus the fitted percent of what was spent since
    median: float  # at the reset
    low: float  # 10th percentile
    high: float  # 90th percentile
    resets_at: datetime
    runs_out_at: datetime | None  # when the median path reaches 100 %, before the reset
    fan: list[tuple[datetime, float, float, float]]  # (time, median, low, high)
    buckets: int  # buckets of history the velocity comes from
    bucket: timedelta
    runs_out_early: datetime | None = None  # the earliest of the range, before the reset
    runs_out_late: datetime | None = None  # the latest; None: it may last to the reset
    factor: float = 1.0  # the fit's rate factor the draws spread over
    readings: int = 0  # readings behind the fit
    rough: bool = False  # few rises: a wide range
    line: bool = False  # the straight-line check widened the range


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _rank(sorted_values: Sequence[float], q: float) -> float:
    """The nearest-rank quantile (safe with infinite values)."""
    return sorted_values[min(int(q * len(sorted_values)), len(sorted_values) - 1)]


def _earliest(*ts: datetime | None) -> datetime | None:
    return min((t for t in ts if t is not None), default=None)


def forecast(
    window: LimitWindow,
    now_value: float,
    reading: float,
    rates: Sequence[float],
    now: datetime,
    resets_at: datetime,
    seed: int = 7,
    factor: float = 1.0,
    line: Line | None = None,
) -> Forecast:
    """Resample `rates` (percent per bucket, recent history, oldest first) in blocks
    over the time left until `resets_at`, starting from `now_value`. Each draw scales
    the fitted percent (what was spent since the `reading`, and the rates) by its own
    rate, lognormal with an 80 % range of divided to multiplied by `factor`. A `line`
    widens the range to its own (slope divided to multiplied by `factor`)."""
    bucket = BUCKET[window]
    left = max((resets_at - now) / bucket, 0.0)
    steps = math.ceil(left)
    block = BLOCK[window]
    rng = random.Random(seed)
    sigma = math.log(factor) / Z80 if factor > 1 else 0.0
    since = now_value - reading
    marks = sorted({max(1, round(steps * i / FAN_POINTS)) for i in range(1, FAN_POINTS + 1)})
    marks = [m for m in marks if m <= steps]
    at_mark: dict[int, list[float]] = {m: [] for m in marks}
    starts: list[float] = []
    finals: list[float] = []
    crossings: list[float] = []
    n = len(rates)
    for _ in range(DRAWS if (n and steps) or sigma else 1):
        scale = math.exp(rng.gauss(0.0, sigma)) if sigma else 1.0
        total = reading + since * scale
        starts.append(total)
        cross = 0.0 if total >= 100 else math.inf
        i = 0
        while i < steps:
            start = rng.randrange(n) if n else 0
            for j in range(block):
                if i >= steps:
                    break
                v = rates[(start + j) % n] * scale if n else 0.0
                part = min(1.0, left - i)  # the last step may be partial
                before = total
                total += v * part
                i += 1
                if before < 100 <= total and cross == math.inf:
                    cross = i - 1 + ((100 - before) / (total - before) if total > before else 0)
                if i in at_mark:
                    at_mark[i].append(total)
        finals.append(total)
        crossings.append(cross)
    finals.sort()
    crossings.sort()
    starts.sort()

    def when(steps_from_now: float) -> datetime | None:
        return (
            None
            if math.isinf(steps_from_now) or steps_from_now > left
            else now + bucket * steps_from_now
        )

    fan = [(now, now_value, _quantile(starts, 0.1), _quantile(starts, 0.9))]
    for m in marks:
        vals = sorted(at_mark[m])
        t = min(now + bucket * m, resets_at)
        fan.append((t, _quantile(vals, 0.5), _quantile(vals, 0.1), _quantile(vals, 0.9)))
    low, high = _quantile(finals, 0.1), _quantile(finals, 0.9)
    early = when(_rank(crossings, 0.1))
    late = when(_rank(crossings, 0.9))
    widened = False
    if line is not None and line.per_hour > 0:
        k = max(factor, 1.0)
        fan2 = []
        for t, med, lo, hi in fan:
            a, b = line.at(t, 1 / k), line.at(t, k)
            fan2.append((t, med, min(lo, a), max(hi, b)))
        fan = fan2
        a, b = line.at(resets_at, 1 / k), line.at(resets_at, k)
        widened = a < low or b > high
        low, high = min(low, a), max(high, b)
        fast = line.reaches_full(k)
        if fast is not None and fast <= resets_at:
            early = _earliest(early, max(fast, now))
        slow = line.reaches_full(1 / k)
        late = None if late is None or slow is None or slow > resets_at else max(late, slow)
    mid = crossings[len(crossings) // 2]  # infinite when a path never reaches 100 %
    runs_out = when(mid)
    if runs_out is not None:
        early = _earliest(early, runs_out)
    return Forecast(
        reading=reading,
        now=now_value,
        median=_quantile(finals, 0.5),
        low=low,
        high=high,
        resets_at=resets_at,
        runs_out_at=runs_out,
        fan=fan,
        buckets=n,
        bucket=bucket,
        runs_out_early=early,
        runs_out_late=late if runs_out is not None or early is not None else None,
        factor=factor,
        line=widened,
    )


# --------------------------------------------------------------------------- per window


@dataclass(slots=True)
class SegmentView:
    segment: Segment
    by_project: list[tuple[str, float, float | None]]  # (label, usd, percent or None)
    usd: float


@dataclass(slots=True)
class WindowView:
    window: LimitWindow
    samples: int
    segments: list[Segment]
    fit: Fit | None
    forecast: Forecast | None
    reason: str | None  # why there is no forecast
    shown: list[SegmentView]
    reading: LimitSample | None  # the last sample of the window
    current: bool  # the last segment's reset is still ahead

    @property
    def label(self) -> str:
        return LABEL[self.window]


def _line(seg: Segment) -> Line | None:
    """The straight line through a segment's first and last points."""
    pts = seg.points()
    if len(pts) < 2:
        return None
    (t0, p0), (t1, p1) = pts[0], pts[-1]
    hours = (t1 - t0) / timedelta(hours=1)
    if hours <= 0:
        return None
    return Line(t1, p1, (p1 - p0) / hours)


def analyze(
    index: Index,
    table: PriceTable,
    samples: Sequence[LimitSample],
    window: LimitWindow,
    now: datetime,
    label: Callable[[str], str] = project_label,
    shown: int = 8,
) -> WindowView:
    """Everything capcom knows about one window: its segments, the fit, the forecast
    for the current segment, and what spent the last `shown` segments by project."""
    own = [s for s in samples if s.window == window and s.time <= now]
    segs = segments(own, window)
    ivs = intervals(segs)
    iv_spend = [(iv, Spend()) for iv in ivs]
    bins: list[Bins] = [Bins([(iv.start, iv.end, sp) for iv, sp in iv_spend])]

    last = segs[-1] if segs else None
    current = last is not None and last.resets_at is not None and last.resets_at > now
    bucket = BUCKET[window]
    rates_spend: list[Spend] = []
    since_last = Spend()
    if current and last is not None:
        base = min(last.start, now - LOOKBACK[window])
        whole = int((now - base) / bucket)
        start = now - bucket * whole
        rates_spend = [Spend() for _ in range(whole)]
        bins.append(
            Bins(
                [
                    (start + bucket * i, start + bucket * (i + 1), s)
                    for i, s in enumerate(rates_spend)
                ]
            )
        )
        bins.append(Bins([(last.last.time, now, since_last)]))

    views = segs[-shown:]
    per_project: list[dict[str, Spend]] = [{} for _ in views]

    def project_adder(d: dict[str, Spend]) -> Callable[[Request, str, float], None]:
        def add(r: Request, model: str, usd: float) -> None:
            key = label(r.project_dir)
            d.setdefault(key, Spend()).add(r, model, usd)

        return add

    bins.append(
        Bins(
            [
                (seg.start, min(seg.end, now), project_adder(d))
                for seg, d in zip(views, per_project, strict=True)
            ]
        )
    )
    scan_spend(index, table, bins)

    readings = sum(len(seg.samples) for seg in segs)
    result = fit(window, iv_spend, len(own), sum(1 for seg in segs if seg.samples), readings)
    if readings < MIN_READINGS:
        result = None
    reason: str | None = None
    fc: Forecast | None = None
    if result is None:
        reason = NOT_ENOUGH
    elif not current or last is None or last.resets_at is None:
        reason = "no reading in the current window"
    else:
        now_value = last.last.used_percentage + result.percent(since_last)
        rates = [result.percent(s) for s in rates_spend]
        fc = forecast(
            window,
            now_value,
            last.last.used_percentage,
            rates,
            now,
            last.resets_at,
            factor=result.rate_factor,
            line=_line(last) if result.rough else None,
        )
        fc.readings, fc.rough = result.readings, result.rough

    shown_views: list[SegmentView] = []
    for seg, d in zip(views, per_project, strict=True):
        total = sum(s.usd for s in d.values())
        rows = sorted(d.items(), key=lambda kv: -kv[1].usd)
        shown_views.append(
            SegmentView(
                seg,
                [(k, s.usd, None if result is None else result.percent(s)) for k, s in rows],
                total,
            )
        )
    return WindowView(
        window=window,
        samples=len(own),
        segments=segs,
        fit=result,
        forecast=fc,
        reason=reason,
        shown=shown_views,
        reading=segs[-1].last if segs else None,
        current=current,
    )


def analyze_all(
    index: Index,
    table: PriceTable,
    samples: Sequence[LimitSample],
    now: datetime,
    label: Callable[[str], str] = project_label,
) -> dict[LimitWindow, WindowView]:
    return {w: analyze(index, table, samples, w, now, label) for w in LimitWindow}


def model_label(key: Key) -> str:
    model, cat = key
    return f"{model.removeprefix('claude-')} {cat.replace('_', ' ')}"


def top_weights(f: Fit, n: int = 8) -> Iterator[tuple[str, float]]:
    for k, v in sorted(f.weights.items(), key=lambda kv: -kv[1])[:n]:
        if v > 0:
            yield model_label(k), v


def by_key(d: Mapping[Key, float]) -> dict[str, float]:
    return {f"{m}/{c}": v for (m, c), v in d.items()}
