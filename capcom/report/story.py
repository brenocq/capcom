"""The headline "what changed" story: the latest day in a window where the daily
spend moved to a new level (`capcom.timeline.regime_changes`), with the sessions and
agent types behind the rise, as JSON for the website and as one line for the
terminal (`capcom top`, `capcom spikes`).

A change is looked for on the days of the window; its before level may use the week
before the window starts (regime_changes needs BEFORE_DAYS whole days before a
change), so the search runs from that week on. Only whole local days count: today
so far is never a day of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from capcom.config import Config
from capcom.timeline.regimes import AFTER_DAYS, BEFORE_DAYS, REST, Contributor, RegimeChange
from capcom.types import Index, PriceTable

MAIN = "main"
LINE_SESSIONS = 3  # sessions named on the one-liner, at most
LINE_MIN_SHARE = 0.10  # a session under this share of the rise is left off the line

RegimeFn = Callable[..., list[RegimeChange]]


def _midnight(d: date, tz: tzinfo | None) -> datetime:
    if tz is None:
        return datetime.combine(d, time()).astimezone()
    return datetime.combine(d, time(), tzinfo=tz)


def latest_change(
    index: Index,
    table: PriceTable,
    first_day: date,
    now: datetime,
    tz: tzinfo | None = None,
    openclaw_root: Path | None = None,
    fn: RegimeFn | None = None,
) -> RegimeChange | None:
    """The latest regime change dated on or after `first_day` (a local date), up to
    today's local midnight; None when the spend kept its level."""
    if fn is None:
        from capcom.timeline.regimes import regime_changes

        fn = regime_changes
    # a lead of a before week and an after week: a change just before the window is
    # found on its own day (and left out), not again on the window's first day
    since = _midnight(first_day - timedelta(days=BEFORE_DAYS + AFTER_DAYS), tz)
    until = _midnight(now.astimezone(tz).date(), tz)
    if until <= since:
        return None
    found = [c for c in fn(index, table, since, until, tz, openclaw_root) if c.day >= first_day]
    return found[-1] if found else None


def _contributor(c: Contributor, session: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": None if c.key == REST else c.key,
        "label": c.label,
        "rise_usd": round(c.rise_usd, 6),
        "share": round(c.share, 4),
        "rest": c.key == REST,
    }
    if session:
        out["project"] = c.project or None
        out["title"] = None if c.key == REST else c.label
    return out


def regime_json(change: RegimeChange | None) -> dict[str, Any] | None:
    """A change for the API (API.md, "Regime"), with the one-liner as `text`."""
    if change is None:
        return None
    ratio = change.ratio
    named = line_sessions(change)
    return {
        "day": change.day.isoformat(),
        "before_usd_per_day": round(change.before_usd, 6),
        "after_usd_per_day": round(change.after_usd, 6),
        "after_days": change.after_days,
        "ratio": None if ratio == float("inf") else round(ratio, 4),
        "rise_usd_per_day": round(change.rise_usd, 6),
        "sessions": [_contributor(c, True) | {"in_text": c in named} for c in change.sessions],
        "agent_types": [_contributor(c, False) for c in change.agent_types],
        "text": regime_line(change),
    }


def _pct(share: float) -> str:
    return f"{share * 100:.0f}%"


def _money(x: float) -> str:
    return f"${x:,.0f}" if x >= 10 else f"${x:,.2f}"


def agent_phrase(key: str) -> str:
    """'coder subagents', or 'main threads' for the main threads."""
    return "main threads" if key == MAIN else f"{key} subagents"


def line_sessions(change: RegimeChange) -> list[Contributor]:
    """The sessions the one-liner names: the largest, then those with at least
    LINE_MIN_SHARE of the rise, at most LINE_SESSIONS."""
    named = [c for c in change.sessions if c.key != REST]
    return [c for i, c in enumerate(named[:LINE_SESSIONS]) if i == 0 or c.share >= LINE_MIN_SHARE]


def regime_line(change: RegimeChange) -> str:
    """'Spend rose x11.9 from Oct 16 ($9 to $107 a day): improved d3f68339 (52%) and
    toy-brain bcbbaac4 (35%); coder subagents 53%.'"""
    ratio = change.ratio
    times = "from nothing" if ratio == float("inf") else f"\u00d7{ratio:.1f}"
    day = f"{change.day:%b} {change.day.day}"
    head = (
        f"Spend rose {times} from {day} "
        f"({_money(change.before_usd)} to {_money(change.after_usd)} a day)"
    )
    who = [
        f"{c.project + ' ' if c.project else ''}{c.key[:8]} ({_pct(c.share)})"
        for c in line_sessions(change)
    ]
    parts = []
    if who:
        parts.append(who[0] if len(who) == 1 else ", ".join(who[:-1]) + " and " + who[-1])
    agents = [c for c in change.agent_types if c.key != REST]
    if agents:
        parts.append(f"{agent_phrase(agents[0].key)} {_pct(agents[0].share)}")
    return head + (": " + "; ".join(parts) if parts else "") + "."


def story_line(
    config: Config,
    days: int,
    index: Index | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> str | None:
    """The one-liner for the terminal: the latest change in the last `days` local
    days (0: all time), None when there is none or the index cannot be read (the
    command itself then says why)."""
    import contextlib
    import sqlite3

    from capcom.cost.prices import load_prices

    now = now or datetime.now(UTC)
    opened = None
    if index is None:
        from capcom.store.index import open_index

        try:
            index = opened = open_index(config.index_path, readonly=True)
        except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error):
            return None
    try:
        today = now.astimezone(tz).date()
        if days > 0:
            first = today - timedelta(days=days - 1)
        else:
            reqs = iter(index.requests())
            head = next(reqs, None)
            getattr(reqs, "close", lambda: None)()
            if head is None:
                return None
            first = head.timestamp.astimezone(tz).date() + timedelta(days=BEFORE_DAYS)
        change = latest_change(index, load_prices(), first, now, tz, config.openclaw_root)
        return None if change is None else regime_line(change)
    finally:
        if opened is not None:
            with contextlib.suppress(Exception):
                getattr(opened, "close", lambda: None)()
