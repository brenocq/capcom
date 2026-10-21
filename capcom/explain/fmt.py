"""Labels for explanations: numbers, times, tool names and paths only, never text
from the logs."""

from __future__ import annotations

from datetime import datetime, timedelta

from capcom.types import CauseKind, Confidence

CAUSE_LABELS: dict[CauseKind, str] = {
    CauseKind.IDLE_REWRITE: "Idle past cache lifetime",
    CauseKind.TOOL_WAIT_EXPIRY: "Cache expired during a tool call",
    CauseKind.LATE_RESUME_EXPIRY: "Subagent resumed late",
    CauseKind.COMPACTION: "Compaction",
    CauseKind.MODEL_SWITCH: "Model switch",
    CauseKind.TOOL_SET_CHANGE: "Tool or prompt change",
    CauseKind.LARGE_TOOL_RESULT: "Large tool result",
    CauseKind.CONTEXT_GROWTH: "Context growth",
    CauseKind.FAN_OUT: "Subagent fan-out",
    CauseKind.LONG_REPORT: "Long subagent report",
    CauseKind.THINKING_HEAVY: "Thinking-heavy turns",
    CauseKind.POLLING: "Polling and repetition",
    CauseKind.PARALLEL_SUBAGENTS: "Parallel subagents' work",
    CauseKind.UNEXPLAINED: "No rule fits",
}

CONFIDENCE_LABELS: dict[Confidence, str] = {
    Confidence.CLEAR: "clear cause",
    Confidence.LIKELY: "likely cause",
    Confidence.UNEXPLAINED: "unexplained",
}

_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def count_word(n: int, capital: bool = False) -> str:
    """'Eight' for 8, '22' for 22."""
    word = _WORDS[n] if 0 <= n < len(_WORDS) else str(n)
    return word.capitalize() if capital else word


def exact(n: int) -> str:
    return f"{n:,}"


def approx(n: float) -> str:
    """Tokens, rounded: 912, 41k, 180k, 1.2M, 2.41B."""
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def usd(x: float) -> str:
    return f"${x:,.2f}"


def clock(t: datetime) -> str:
    """Local time of day, seconds included."""
    return t.astimezone().strftime("%H:%M:%S")


def hm(t: datetime) -> str:
    return t.astimezone().strftime("%H:%M")


def day_hm(t: datetime) -> str:
    """'Oct 20, 14:30', local time."""
    local = t.astimezone()
    return f"{local:%b} {local.day}, {local:%H:%M}"


def duration(d: timedelta) -> str:
    """'40 s', '74 min', '6 h 10', '2 days'."""
    s = d.total_seconds()
    if s < 60:
        return f"{s:.0f} s"
    m = round(s / 60)
    if m < 120:
        return f"{m} min"
    h, rest = divmod(m, 60)
    if h < 48:
        return f"{h} h {rest:02d}" if rest else f"{h} h"
    return f"{round(h / 24)} days"


def lifetime(d: timedelta) -> str:
    s = int(d.total_seconds())
    if s % 3600 == 0:
        return f"{s // 3600} h"
    if s % 60 == 0:
        return f"{s // 60} minutes"
    return f"{s} s"


def tool_label(tool: str | None, target: str | None) -> str:
    """'Read of build/figures.json', 'Bash (pytest)', 'WebFetch of example.com'."""
    name = tool or "A tool call"
    if not target:
        return name
    if name in ("Read", "Edit", "Write", "NotebookEdit", "WebFetch", "Grep", "Glob"):
        return f"{name} of {target}"
    return f"{name} ({target})"
