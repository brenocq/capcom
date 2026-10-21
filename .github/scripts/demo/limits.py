"""Made-up usage-limit samples for the synthetic logs, as `capcom record-limits` writes them.

    python .github/scripts/demo/limits.py <work-dir>   (after data.py has scanned it)

The plan's usage windows are not in the logs; Claude Code's status line reports them,
and capcom's recorder keeps one JSONL line per rise. This plays the recorder over the
synthetic weeks: each 5-hour window opens at the hour of the first spend after the
last one reset, the weekly one resets on a fixed hour, and the percentage of each is
what the window has spent so far times a made-up rate (the busiest 5-hour window
peaks near 90 %, the week stands at half with three days to go). The spend comes from capcom
itself (`/api/spend` and `/api/day`), so capcom's fit and forecast, asked for through
`/api/limits`, find the rate again and project the current windows to their resets.
Writes <work-dir>/state/capcom/limits.jsonl (the sandbox's XDG_STATE_HOME).
"""

from __future__ import annotations

import json
import sys
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIVE = timedelta(hours=5)
WEEK = timedelta(days=7)
FIVE_PEAK = 88.0  # the busiest 5-hour window's peak, in percent
WEEK_NOW = 50.0  # where the weekly window stands at the end of the data, 3 days from its reset


def _steps(cum: dict[str, Any]) -> list[tuple[float, float]]:
    """(end of step in epoch s, spend in the step) from a Cumulative's total."""
    out, prev = [], 0.0
    for t, v in zip(cum["times"], cum["total"], strict=True):
        out.append((t / 1000, v - prev))
        prev = v
    return out


def spend(get: Any) -> list[tuple[float, float]]:
    """The spend in steps: an hour over two weeks, 10 minutes over the last two days."""
    week = _steps(get("/api/spend?days=14&group=project")["cumulative"])
    days = sorted({datetime.fromtimestamp(t).astimezone().date() for t, _ in week})[-2:]
    fine: list[tuple[float, float]] = []
    for d in days:
        c = get(f"/api/day/{d.isoformat()}").get("cumulative")
        if c:
            fine += _steps(c)
    cut = min(t for t, _ in fine) - 600 if fine else float("inf")
    return [s for s in week if s[0] <= cut] + fine


def samples(steps: list[tuple[float, float]], now: datetime) -> list[dict[str, Any]]:
    times = [t for t, _ in steps]
    cum, total = [], 0.0
    for _, u in steps:
        total += u
        cum.append(total)

    def at(t: float) -> float:
        i = bisect_right(times, t)
        return cum[i - 1] if i else 0.0

    # the 5-hour windows: (start, reset) in epoch s
    windows: list[tuple[float, float]] = []
    for t, u in steps:
        if u <= 0.005 or (windows and t - 600 < windows[-1][1]):
            continue
        start = datetime.fromtimestamp(t - 600, UTC).replace(minute=0, second=0, microsecond=0)
        windows.append((start.timestamp(), (start + FIVE).timestamp()))
    k5 = FIVE_PEAK / max(at(b) - at(a) for a, b in windows)
    end = now.timestamp()
    week_reset = (now + timedelta(days=3, hours=1)).replace(minute=0, second=0, microsecond=0)
    week_start = (week_reset - WEEK).timestamp()
    k7 = WEEK_NOW / max(at(end) - at(week_start), 0.01)

    out: list[dict[str, Any]] = []
    last: dict[str, tuple[float, float]] = {}

    def add(t: float, window: str, pct: float, reset: float) -> None:
        pct = round(min(pct, 100.0), 1)
        prev = last.get(window)
        if prev and prev[1] == reset and pct < prev[0] + 0.5:
            return
        last[window] = (pct, reset)
        out.append(
            {
                "time": datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z"),
                "window": window,
                "used_percentage": pct,
                "resets_at": datetime.fromtimestamp(reset, UTC).isoformat().replace("+00:00", "Z"),
                "session_id": None,
                "source": "statusline",
            }
        )

    for t in times:
        if t > end:
            break
        for a, b in windows:
            if a < t <= b:
                add(t, "five_hour", (at(t) - at(a)) * k5, b)
        ws = week_start if t > week_start else week_start - WEEK.total_seconds()
        add(t, "seven_day", (at(t) - at(ws)) * k7, ws + WEEK.total_seconds())
    out.sort(key=lambda s: s["time"])
    return out


def write(work: Path, get: Any, now: datetime) -> Path:
    path = work / "state" / "capcom" / "limits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = samples(spend(get), now)
    path.write_text("".join(json.dumps(s, separators=(",", ":")) + "\n" for s in rows))
    return path


def main() -> None:
    import data

    if len(sys.argv) != 2:
        sys.exit(__doc__)
    work = Path(sys.argv[1]).resolve()
    now = datetime.now(UTC)  # the synthetic logs end a few minutes before they were built
    with data.serve(work) as base:
        p = write(work, lambda q: data.get(base, q), now)
        print(p, sum(1 for _ in p.open()))
        print(json.dumps(data.get(base, "/api/limits?days=7"))[:1500])


if __name__ == "__main__":
    main()
