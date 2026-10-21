"""The usage windows as JSON for the website (`/api/limits` and the Overview's cards)
and as text for `capcom limits`. Times in JSON are ISO 8601 in the given zone, except
chart points, which are epoch milliseconds."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from capcom.cost.top import project_label
from capcom.limits.model import (
    NOT_ENOUGH,
    Forecast,
    WindowView,
    analyze_all,
    load_samples,
    samples_path,
)
from capcom.types import Index, LimitSample, LimitWindow, PriceTable

INSTALL = "capcom limits --install"
NOT_RECORDING = "Not recording yet: run `capcom limits --install` to set it up."
TOP_PROJECTS = 6
OTHER = "(other)"


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _iso(t: datetime | None, tz: tzinfo | None) -> str | None:
    return None if t is None else t.astimezone(tz).isoformat(timespec="seconds")


def _r(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)


def _projects(rows: Sequence[tuple[str, float, float | None]]) -> list[dict[str, Any]]:
    out = [{"project": k, "usd": _r(u, 4), "percent": _r(p)} for k, u, p in rows[:TOP_PROJECTS]]
    rest = rows[TOP_PROJECTS:]
    if rest:
        pcts = [p for _, _, p in rest]
        out.append(
            {
                "project": OTHER,
                "usd": _r(sum(u for _, u, _ in rest), 4),
                "percent": None if None in pcts else _r(sum(p or 0.0 for p in pcts)),
            }
        )
    return out


def window_json(
    v: WindowView, now: datetime, tz: tzinfo | None, since: datetime | None, full: bool
) -> dict[str, Any]:
    reading = v.reading
    f = v.fit
    fc = v.forecast
    last = v.segments[-1] if v.segments else None
    out: dict[str, Any] = {
        "window": v.window.value,
        "label": v.label,
        "samples": v.samples,
        "current": v.current,
        "reading": None
        if reading is None
        else {
            "percent": reading.used_percentage,
            "time": _iso(reading.time, tz),
            "resets_at": _iso(last.resets_at if last else reading.resets_at, tz),
            "source": reading.source,
        },
        "fit": None
        if f is None
        else {
            "method": f.method,
            "note": f.note,
            "percent_per_usd": _r(f.per_usd, 6),
            "usd_per_percent": _r(f.usd_per_percent, 4),
            "intervals": f.intervals,
            "readings": f.readings,
            "rough": f.rough,
            "rate_factor": _r(f.rate_factor, 3),
            "mean_abs_error": _r(f.mean_abs_error),
            "r2": _r(f.r2, 3),
            "weights": [
                {"model": m, "category": c, "percent_per_mtok": _r(w, 6)}
                for (m, c), w in sorted(f.weights.items(), key=lambda kv: -kv[1])
                if w > 0
            ],
        },
        "forecast": None
        if fc is None
        else {
            "reading": fc.reading,
            "now": _r(fc.now),
            "median": _r(fc.median),
            "low": _r(fc.low),
            "high": _r(fc.high),
            "resets_at": _iso(fc.resets_at, tz),
            "runs_out_at": _iso(fc.runs_out_at, tz),
            "runs_out_early": _iso(fc.runs_out_early, tz),
            "runs_out_late": _iso(fc.runs_out_late, tz),
            "rough": fc.rough,
            "readings": fc.readings,
            "rate_factor": _r(fc.factor, 3),
            "line": fc.line,
            "bucket_minutes": int(fc.bucket.total_seconds() // 60),
            "buckets": fc.buckets,
            "fan": [[_ms(t), _r(m), _r(lo), _r(hi)] for t, m, lo, hi in fc.fan],
        },
        "reason": v.reason,
        # the current window's recorded line, for the card's chart
        "history": [] if last is None or not v.current else [[_ms(t), p] for t, p in last.points()],
        "start": None if last is None or not v.current else _iso(last.start, tz),
        "start_known": last is not None and v.current and last.start_known,
    }
    if full:
        by_seg = {id(sv.segment): sv for sv in v.shown}
        segs = []
        for seg in reversed(v.segments):
            if since is not None and seg.end < since:
                break
            sv = by_seg.get(id(seg))
            segs.append(
                {
                    "start": _iso(seg.start, tz),
                    "start_known": seg.start_known,
                    "resets_at": _iso(seg.resets_at, tz),
                    "peak": seg.last.used_percentage,
                    "hit_limit": any(s.source == "rate_limit_error" for s in seg.samples),
                    "points": [[_ms(t), p] for t, p in seg.points()],
                    "reset": seg.resets_at is not None and seg.resets_at <= now,
                    "usd": None if sv is None else _r(sv.usd, 4),
                    "by_project": None if sv is None else _projects(sv.by_project),
                }
            )
        out["segments"] = segs
    return out


def _since(samples: Sequence[LimitSample]) -> datetime | None:
    """The first status line sample (seeded rate-limit errors can be much older), or
    the first sample when there is none."""
    first = next((s for s in samples if s.source == "statusline"), None)
    return first.time if first is not None else samples[0].time if samples else None


def limits_json(
    index: Index,
    table: PriceTable,
    state_dir: Path | None,
    now: datetime,
    tz: tzinfo | None = None,
    days: int | None = None,
    full: bool = True,
    label: Callable[[str], str] = project_label,
) -> dict[str, Any]:
    samples = load_samples(samples_path(state_dir)) if state_dir is not None else []
    recording = any(s.source == "statusline" for s in samples)
    base: dict[str, Any] = {
        "available": bool(samples),
        "recording": recording,
        "message": None if recording else NOT_RECORDING,
        "install": INSTALL,
        "samples": len(samples),
        "last_sample": _iso(samples[-1].time, tz) if samples else None,
        "recording_since": _iso(_since(samples), tz),
    }
    if not samples:
        base["windows"] = {w.value: None for w in LimitWindow}
        return base
    since = now - timedelta(days=days) if days else None
    views = analyze_all(index, table, samples, now, label)
    base["windows"] = {w.value: window_json(views[w], now, tz, since, full) for w in LimitWindow}
    return base


# --------------------------------------------------------------------------- text


def _local(t: datetime | None) -> str:
    return "?" if t is None else t.astimezone().strftime("%a %H:%M")


def _span(d: timedelta) -> str:
    m = max(int(d.total_seconds() // 60), 0)
    if m >= 48 * 60:
        return f"{m // 1440} d {m % 1440 // 60} h"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _at_reset(fc: Forecast) -> str:
    """The value at the reset; past 100 % only that it reaches the limit (usage stops
    there)."""
    if fc.low >= 100:
        return "at this pace it reaches the limit before the reset"
    med = "100% (the limit)" if fc.median >= 100 else f"{fc.median:.0f}%"
    high = "the limit" if fc.high >= 100 else f"{fc.high:.0f}%"
    return f"at the reset {med} (80% range {fc.low:.0f}% to {high})"


def rough_text(readings: int) -> str:
    n = f"{readings} reading{'' if readings == 1 else 's'}"
    return f"Rough: {n} so far, so the range is wide; it narrows as readings come."


def runs_out_text(fc: Forecast, when: Callable[[datetime], str]) -> str | None:
    """When the forecast runs out, with its range, in words (None when even the high
    end of the range lasts until the reset)."""
    mid, early, late = fc.runs_out_at, fc.runs_out_early, fc.runs_out_late
    if mid is not None and late is not None and early is not None and early < late:
        return (
            f"At this pace it runs out between {when(early)} and {when(late)}, "
            f"most likely {when(mid)}."
        )
    if mid is not None and late is not None:
        return f"At this pace it runs out around {when(mid)}."
    if mid is not None:
        first = f", as early as {when(early)}" if early is not None and early < mid else ""
        return (
            f"At this pace it runs out around {when(mid)}{first}; "
            "at a slower pace it lasts until the reset."
        )
    if early is not None:
        return f"It may run out before the reset, as early as {when(early)} if the pace picks up."
    return None


def format_views(
    views: Mapping[LimitWindow, WindowView], samples: Sequence[LimitSample], now: datetime
) -> str:
    lines: list[str] = []
    for w in LimitWindow:
        v = views[w]
        head = v.label[0].upper() + v.label[1:]
        r = v.reading
        if r is None:
            lines.append(f"{head}: no samples.")
            lines.append("")
            continue
        last = v.segments[-1]
        if v.current and last.resets_at is not None:
            lines.append(
                f"{head}: {r.used_percentage:.0f}% at {_local(r.time)}; resets "
                f"{_local(last.resets_at)} (in {_span(last.resets_at - now)})"
            )
        else:
            lines.append(
                f"{head}: last reading {r.used_percentage:.0f}% at {_local(r.time)}; "
                f"that window reset {_local(last.resets_at)}"
            )
        fc = v.forecast
        if fc is not None:
            lines.append(f"  now about {fc.now:.0f}%; {_at_reset(fc)}")
            if fc.rough:
                lines.append("  " + rough_text(fc.readings))
            run = runs_out_text(fc, _local)
            if run:
                lines.append("  " + run)
        else:
            lines.append(f"  no forecast: {v.reason or NOT_ENOUGH}")
        f = v.fit
        if f is not None:
            lines.append(f"  {f.note} ({v.samples} samples recorded)")
            if f.method == "per_type":
                lines.append("  what 1% costs, by model and token type:")
                top = sorted(f.weights.items(), key=lambda kv: -kv[1])[:6]
                for (m, c), wt in top:
                    if wt > 0:
                        tok = 1e6 / wt  # the weight is percent per million tokens
                        lines.append(f"    {m} {c.replace('_', ' ')}: {tok:,.0f} tokens")
        else:
            lines.append(f"  fit: {NOT_ENOUGH} ({v.samples} so far)")
        if v.shown and v.current:
            sv = v.shown[-1]
            parts = []
            for k, u, p in sv.by_project[:5]:
                parts.append(f"{k} ${u:,.2f}" + ("" if p is None else f" ({p:.1f}%)"))
            if parts:
                lines.append("  this window by project: " + ", ".join(parts))
        lines.append("")
    if not any(s.source == "statusline" for s in samples):
        lines.append(NOT_RECORDING)
    return "\n".join(lines).rstrip()
