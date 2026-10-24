"""The usage windows as JSON for the website (`/api/limits` and the Overview's cards)
and as text for `capcom limits`. Times in JSON are ISO 8601 in the given zone, except
chart points, which are epoch milliseconds."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from capcom.cost.top import project_label
from capcom.limits.accounts import (
    AccountView,
    Combined,
    analyze_accounts,
    load_known,
)
from capcom.limits.detail import window_path
from capcom.limits.model import (
    MERGED,
    MIN_SHARE,
    NOT_ENOUGH,
    Fit,
    Forecast,
    Row,
    Split,
    WindowView,
    load_samples,
    samples_path,
)
from capcom.types import Index, LimitSample, LimitWindow, PriceTable

INSTALL = "capcom limits --install"
NOT_RECORDING = "Not recording yet: run `capcom limits --install` to set it up."
TOP_MEMBERS = 6  # a segment's split lists these, then one "(other)" for the rest
OTHER = "(other)"


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _iso(t: datetime | None, tz: tzinfo | None) -> str | None:
    return None if t is None else t.astimezone(tz).isoformat(timespec="seconds")


def _r(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)


def _rows(rows: Sequence[Row]) -> list[dict[str, Any]]:
    out = [{"key": k, "usd": _r(u, 4), "percent": _r(p)} for k, u, p in rows[:TOP_MEMBERS]]
    rest = rows[TOP_MEMBERS:]
    if rest:
        pcts = [p for _, _, p in rest]
        out.append(
            {
                "key": OTHER,
                "usd": _r(sum(u for _, u, _ in rest), 4),
                "percent": None if None in pcts else _r(sum(p or 0.0 for p in pcts)),
            }
        )
    return out


CATEGORY_WORDS = {
    "input": "input",
    "cache_write": "cache writes",
    "cache_read": "cache reads",
    "output": "output",
}
ROUGH = 2.0  # a range whose high end is more than this times its low end is rough


def _model_word(model: str) -> str:
    return model.removeprefix("claude-")


def _range(w: float, spread: float | None) -> tuple[float, float] | None:
    """A weight's 80 % range, or None when it reaches zero (or is unknown)."""
    if spread is None or w - spread <= 0:
        return None
    return w - spread, w + spread


def costs_json(f: Fit) -> dict[str, Any] | None:
    """What 1 % of the window costs, by model and token type (per_type fits): the
    tokens of each kind that make 1 % and their API-equivalent dollars, each with its
    80 % range ("rough" when the high end is more than twice the low end). A kind is
    left out when its range reaches zero (the readings cannot tell its cost from
    zero); the kinds with too small a share of the spend to measure alone are one row,
    in dollars."""
    if f.method != "per_type":
        return None
    rows: list[dict[str, Any]] = []
    left_out: list[str] = []
    for (m, c), w in sorted(f.weights.items(), key=lambda kv: kv[1]):
        if w <= 0:
            continue
        label = f"{_model_word(m)} {CATEGORY_WORDS.get(c, c)}"
        rng = _range(w, f.spread.get((m, c)))
        if rng is None:
            left_out.append(label)
            continue
        tokens = f.behind.get((m, c), 0.0)
        per_token = f.usd_behind.get((m, c), 0.0) / tokens if tokens else None
        tok = 1e6 / w
        rough = rng[1] > ROUGH * rng[0]
        row: dict[str, Any] = {
            "model": m,
            "category": c,
            "label": label,
            "tokens": round(tok),
            "tokens_low": round(1e6 / rng[1]),
            "tokens_high": round(1e6 / rng[0]),
            "usd": None if per_token is None else _r(tok * per_token, 4),
            "usd_low": None if per_token is None else _r(1e6 / rng[1] * per_token, 4),
            "usd_high": None if per_token is None else _r(1e6 / rng[0] * per_token, 4),
            "rough": rough,
        }
        rows.append(row)
    merged: dict[str, Any] | None = None
    if f.merged:
        labels = [f"{_model_word(m)} {CATEGORY_WORDS.get(c, c)}" for m, c in f.merged]
        rng = _range(f.merged_per_usd, f.spread.get(MERGED))
        share = sum(f.usd_behind.get(k, 0.0) for k in f.merged)
        total = sum(f.usd_behind.values())
        merged = {
            "kinds": labels,
            "share": _r(share / total, 4) if total else None,
            "usd": None if rng is None else _r(1 / f.merged_per_usd, 4),
            "usd_low": None if rng is None else _r(1 / rng[1], 4),
            "usd_high": None if rng is None else _r(1 / rng[0], 4),
            "rough": rng is not None and rng[1] > ROUGH * rng[0],
        }
    return {
        "rows": rows,
        "merged": merged,
        "left_out": left_out,
        "min_share": MIN_SHARE,
        "rough_ratio": ROUGH,
    }


def window_json(
    v: WindowView,
    now: datetime,
    tz: tzinfo | None,
    since: datetime | None,
    full: bool,
    account: str | None = None,
    split: Split = "project",
) -> dict[str, Any]:
    """One window of one account (`account`: its id, for the links to each window's
    own page), each segment's spend split by `split`."""
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
            "costs": costs_json(f),
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
        segs: list[dict[str, Any]] = []
        for seg in reversed(v.segments):
            # the weekly chart keeps at least its newest week, so a short range never
            # leaves it empty
            keep = not segs and v.window is LimitWindow.SEVEN_DAY
            if since is not None and seg.end < since and not keep:
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
                    # of usd, from sessions no reading names, put on the account by time
                    "inferred_usd": None if sv is None else _r(sv.inferred_usd, 4),
                    "by": None if sv is None else _rows(sv.by[split]),
                }
                | ({} if account is None else {"path": window_path(account, seg, tz)})
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
    split: Split = "project",
    since: datetime | None = None,
) -> dict[str, Any]:
    """`since` bounds the windows listed (those that end after it); without it, `days`
    back from `now`."""
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
    if since is None and days:
        since = now - timedelta(days=days)
    accounts, comb = analyze_accounts(index, table, samples, load_known(state_dir), now, label)
    out = [
        {
            "id": a.account.id,
            "email": a.account.email,
            "label": a.account.label,
            "current": a.account.current,
            "samples": len(a.samples),
            "windows": {
                w.value: window_json(a.views[w], now, tz, since, full, a.account.id, split)
                for w in LimitWindow
            },
        }
        for a in accounts
    ]
    base["accounts"] = out
    # the current account's windows, as before accounts were recorded
    base["windows"] = out[0]["windows"]
    base["combined"] = combined_json(comb, tz) if len(accounts) > 1 else None
    return base


def combined_json(c: Combined, tz: tzinfo | None) -> dict[str, Any]:
    return {
        "usd_left": _r(c.usd_left, 2),
        "pace_usd_per_hour": _r(c.pace_usd_per_hour, 4),
        "pace_hours": 24,
        "runs_out_at": _iso(c.runs_out_at, tz),
        "reset_first": None
        if c.reset_first is None
        else {"id": c.reset_first.account.id, "resets_at": _iso(c.reset_first.resets_at, tz)},
        "accounts": [
            {
                "id": s.account.id,
                "label": s.account.label,
                "percent": _r(s.percent),
                "usd_per_percent": _r(s.usd_per_percent, 4),
                "usd_left": _r(s.usd_left, 2),
                "resets_at": _iso(s.resets_at, tz),
            }
            for s in c.shares
        ],
    }


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


def _money(x: float) -> str:
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def format_combined(c: Combined) -> str:
    """The combined line of `capcom limits`: dollars left before every account's
    weekly limit, and when all of it is spent at the recent pace."""
    parts = [f"{s.account.label} {_money(s.usd_left)}" for s in c.shares if s.usd_left is not None]
    if c.usd_left is None:
        return "All accounts: no fit of the weekly limit yet, so no dollars left to add up."
    out = (
        f"All accounts: about {_money(c.usd_left)} of API-equivalent spend left before "
        f"the weekly limits ({', '.join(parts)})"
    )
    if c.missing:
        out += f"; no fit yet for {', '.join(a.label for a in c.missing)}"
    if c.usd_left <= 0:
        out += ". Every account with a fit is at its weekly limit"
    elif c.runs_out_at is not None:
        out += (
            f". At the pace of the last 24 hours ({_money(c.pace_usd_per_hour)} an hour) "
            f"all of it is spent around {_local(c.runs_out_at)}"
        )
        if c.reset_first is not None:
            out += (
                f", but {c.reset_first.account.label}'s week resets first, "
                f"{_local(c.reset_first.resets_at)}"
            )
    return out + "."


def format_accounts(
    accounts: Sequence[AccountView],
    comb: Combined | None,
    samples: Sequence[LimitSample],
    now: datetime,
) -> str:
    """`capcom limits` for each account (with a heading when there are several) and
    the combined line."""
    if len(accounts) == 1:
        return format_views(accounts[0].views, samples, now)
    blocks: list[str] = []
    for a in accounts:
        head = a.account.label + (" (current)" if a.account.current else "")
        blocks.append(f"== {head}\n" + format_views(a.views, a.samples, now, hint=False))
    if comb is not None:
        blocks.append(format_combined(comb))
    if not any(s.source == "statusline" for s in samples):
        blocks.append(NOT_RECORDING)
    return "\n\n".join(blocks)


def format_views(
    views: Mapping[LimitWindow, WindowView],
    samples: Sequence[LimitSample],
    now: datetime,
    hint: bool = True,
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
            costs = costs_json(f)
            if costs is not None and (costs["rows"] or costs["merged"]):
                lines.append("  what 1% costs, by model and token type:")
                for row in costs["rows"]:
                    tok = f"{row['tokens']:,.0f} tokens"
                    if row["rough"]:
                        tok = (
                            f"{row['tokens_low']:,.0f} to {row['tokens_high']:,.0f} tokens (rough)"
                        )
                    usd = "" if row["usd"] is None else f", {_money(row['usd'])}"
                    lines.append(f"    {row['label']}: {tok}{usd}")
                mg = costs["merged"]
                if mg is not None and mg["usd"] is not None:
                    lines.append(
                        f"    the {len(mg['kinds'])} small kinds together: "
                        f"{_money(mg['usd'])}{' (rough)' if mg['rough'] else ''}"
                    )
                if costs["left_out"]:
                    lines.append("    too little data to tell: " + ", ".join(costs["left_out"]))
        else:
            lines.append(f"  fit: {NOT_ENOUGH} ({v.samples} so far)")
        if v.shown and v.current:
            sv = v.shown[-1]
            parts = []
            for k, u, p in sv.by["project"][:5]:
                parts.append(f"{k} ${u:,.2f}" + ("" if p is None else f" ({p:.1f}%)"))
            if parts:
                lines.append("  this window by project: " + ", ".join(parts))
            if sv.inferred_usd >= 0.005:
                lines.append(
                    f"  of which {_money(sv.inferred_usd)} from sessions with no reading, "
                    "put on this account by time"
                )
        lines.append("")
    if hint and not any(s.source == "statusline" for s in samples):
        lines.append(NOT_RECORDING)
    return "\n".join(lines).rstrip()
