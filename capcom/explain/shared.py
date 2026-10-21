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
    gap: timedelta, lifetime: timedelta, tool_ran: timedelta | None, subagent: bool
) -> str | None:
    """Who waited through a gap of `gap` between two requests of a thread whose cache
    lives `lifetime`: TOOL_WAIT_EXPIRY when the previous request's tool call ran past
    the lifetime (`tool_ran`: from the request to the call's last result), else
    LATE_RESUME_EXPIRY on a subagent (it had finished and its lead resumed it late),
    else IDLE_EXPIRY. None when the gap is within the lifetime: no expiry."""
    if gap <= lifetime:
        return None
    if tool_ran is not None and tool_ran > lifetime:
        return TOOL_WAIT_EXPIRY
    return LATE_RESUME_EXPIRY if subagent else IDLE_EXPIRY
