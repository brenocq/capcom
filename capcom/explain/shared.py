"""Names and thresholds that explain shares with checks, so that one event has one
name and one context size one meaning in every finding.

Context sizes: one family. LONG_CONTEXT is where a thread counts as long and the
advice says to hand off or compact; LATE_COMPACTION is where a compaction ran late.
The floor of the context-growth rule derives from LONG_CONTEXT and is never quoted
in a title or advice.

Cache expiry: a cache rewritten after a gap past its lifetime is named by who was
waiting (`expiry_kind`); a gap within the lifetime is never an expiry.
"""

from __future__ import annotations

from datetime import timedelta

from capcom.types import CauseKind

LONG_CONTEXT = 150_000  # past this a thread is long: hand off, /clear or compact
LATE_COMPACTION = 400_000  # a compaction from above this ran late
GROWTH_FLOOR = LONG_CONTEXT * 2 // 3  # context growth is only looked at from here
# a fresh agent that continues the work restarts here (its files and state back)
RESTART_CONTEXT = LONG_CONTEXT * 2 // 3
CONTEXT_STEPS = (LONG_CONTEXT, LATE_COMPACTION)  # the sizes a title or evidence names

TOOL_WAIT_EXPIRY = "tool_wait_expiry"  # the agent waited on its own tool call
LATE_RESUME_EXPIRY = "late_resume_expiry"  # a finished subagent waited for its lead
IDLE_EXPIRY = "idle_expiry"  # the user was away (main thread, no tool call pending)

EXPIRY_SUB_KINDS: dict[CauseKind, str] = {
    CauseKind.TOOL_WAIT_EXPIRY: TOOL_WAIT_EXPIRY,
    CauseKind.LATE_RESUME_EXPIRY: LATE_RESUME_EXPIRY,
    CauseKind.IDLE_REWRITE: IDLE_EXPIRY,
}
EXPIRY_CAUSES: dict[str, CauseKind] = {v: k for k, v in EXPIRY_SUB_KINDS.items()}


def expiry_kind(
    gap: timedelta,
    lifetime: timedelta,
    wait: timedelta | None,
    after_wait: timedelta | None,
    subagent: bool,
    resumed: bool,
) -> str | None:
    """Who waited through a gap of `gap` between two requests of a thread whose cache
    lives `lifetime`. None when the gap is within the lifetime: no expiry.

    `wait` is how long the agent waited on its own work: from the previous request
    to its tool call's last result (user-interaction tools such as AskUserQuestion
    excluded), or, on a main thread, to the last request of the session's other
    threads (background work); `after_wait` runs from there to the next request.
    TOOL_WAIT_EXPIRY when that wait ran past the lifetime and was the longer of the
    two; else LATE_RESUME_EXPIRY on a subagent that had ended its turn and that a
    message from its lead resumed (`resumed`); else IDLE_EXPIRY (someone was away)."""
    if gap <= lifetime:
        return None
    if wait is not None and wait > lifetime:
        user_after = after_wait is not None and after_wait > lifetime and after_wait >= wait
        if not user_after:
            return TOOL_WAIT_EXPIRY
    if subagent and resumed:
        return LATE_RESUME_EXPIRY
    return IDLE_EXPIRY
