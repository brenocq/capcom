"""What each finding kind measures, read from the checks' own facts (capcom.checks.facts)
of one week, so a mark counts its thing exactly as the finding did.

A measure tallies, over a week and the mark's scope, a `count` (the thing the
finding is about), a `base` (what it is a rate of: requests, or runs) and `usd`
(what the counted things cost, by the finding's own pricing). Weeks add up, so a
window's rate is its summed count over its summed base, times the measure's
`scale` (1,000 for "per 1,000 requests"). The unit value used for the measured
saving is `usd / count` over the before window:

- cache rewrites (tool wait, idle, late resume, model switch): one rewrite's cost
  above reading the same tokens (`rewrite_excess`, the finding's saving);
- turns past LONG_CONTEXT: the long_running_agents saving per such turn (rereads
  less a simulated handoff), the finding's own formula;
- late compactions: the late_compactions saving per compaction;
- a heartbeat's runs: the count is the tokens each run starts with, priced at
  that run's own read prices (the finding's formula with no fresh floor), so a
  token less at the start of a run saves its rereads through that run;
- sessions never ended: the requests of sessions open past 7 days, valued at the
  session_never_ended saving of those sessions per request;
- model mix: dollars per request already; one dollar is worth one dollar;
- items carried in prompts (skills, MCP tools, connectors, memory, an agent's
  tools, images, long reports): the count is tokens carried (size x requests that
  carried it), valued at what those requests paid per prompt token (`acc.usd`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from statistics import median

from capcom.checks.facts import Account, Facts, ItemKey, Rewrite, Thread, heartbeat
from capcom.checks.rules import _group, _is_connector, _server
from capcom.checks.sessions import NEVER_ENDED_SPAN, _span, band_usd, fresh_context
from capcom.cost.prices import normalize
from capcom.explain.shared import (
    IDLE_EXPIRY,
    LATE_COMPACTION,
    LATE_RESUME_EXPIRY,
    LONG_CONTEXT,
    TOOL_WAIT_EXPIRY,
)
from capcom.types import EventKind, Index


@dataclass(slots=True)
class Tally:
    """One week's numbers (or a window's, summed) for a mark's measure."""

    count: float = 0.0
    base: float = 0.0
    requests: int = 0  # requests in scope
    usd: float = 0.0
    samples: list[float] = field(default_factory=list)  # median measures: one per run
    sums: dict[str, float] = field(default_factory=dict)  # evidence, summed over weeks
    maxes: dict[str, float] = field(default_factory=dict)  # evidence, the largest

    def add(self, key: str, value: float) -> None:
        self.sums[key] = self.sums.get(key, 0.0) + value

    def top(self, key: str, value: float) -> None:
        self.maxes[key] = max(self.maxes.get(key, 0.0), value)

    def merge(self, other: Tally) -> None:
        self.count += other.count
        self.base += other.base
        self.requests += other.requests
        self.usd += other.usd
        self.samples.extend(other.samples)
        for k, v in other.sums.items():
            self.add(k, v)
        for k, v in other.maxes.items():
            self.top(k, v)


ERRORS_ROW = "Tool errors per 100 tool calls"  # shown only when the index flags errors
Row = tuple[str, Callable[[Tally, float], str]]  # (label, value of a tally over n weeks)


@dataclass(frozen=True, slots=True)
class Measure:
    label: str  # e.g. "Tool-wait rewrites per 1,000 requests"
    unit: str  # the value's unit in a sentence: "per 1,000 requests"
    scale: float
    tally: Callable[[Facts, str, Index], Tally]
    rows: tuple[Row, ...] = ()
    median: bool = False  # the value is the median of the samples, not count / base
    fmt: str = "{:,.1f}"
    quality: tuple[Row, ...] = ()  # model changes: signals to judge quality by
    # the work the habit rides on, in words (its count: the tally's "habit" sum, else
    # the requests in scope): near zero before and present after, nothing to compare
    habit: str = "requests in scope"

    def value(self, t: Tally) -> float | None:
        if self.median:
            return float(median(t.samples)) if t.samples else None
        return t.count / t.base * self.scale if t.base else None


# --------------------------------------------------------------------------- scopes


def _in_project(facts: Facts, scope: str) -> Callable[[str], bool]:
    """Whether a session belongs to the project scope (its group label, or "all")."""
    return lambda sid: scope == "all" or _group(facts.project_of(sid)) == scope


def _project_threads(facts: Facts, scope: str) -> list[Thread]:
    return [
        t
        for t in facts.threads.values()
        if t.requests and (scope == "all" or _group(t.project) == scope)
    ]


def _type_threads(facts: Facts, scope: str) -> list[Thread]:
    return [
        t
        for t in facts.threads.values()
        if t.requests and t.agent_id is not None and facts.agent_type(t.agent_id) == scope
    ]


def _requests(threads: Iterable[Thread]) -> int:
    return sum(t.requests for t in threads)


# --------------------------------------------------------------------------- per kind


def _rewrites(
    pick: Callable[[Facts], Mapping[str, Rewrite]], habit: Callable[[Thread], float]
) -> Callable[..., Tally]:
    def tally(facts: Facts, scope: str, index: Index) -> Tally:
        out = Tally()
        threads = _project_threads(facts, scope)
        out.requests = _requests(threads)
        out.base = out.requests
        out.add("habit", sum(habit(t) for t in threads))
        for project, rw in pick(facts).items():
            if scope != "all" and _group(project) != scope:
                continue
            out.count += rw.count
            out.usd += rw.excess_usd
            out.add("tokens", rw.tokens)
            out.top("longest_min", rw.longest.total_seconds() / 60)
            for name, n in rw.causes.items():
                out.add(f"cause:{name}", n)
        return out

    return tally


def _beats(facts: Facts) -> set[tuple[str, str | None]]:
    return {k for k, t in facts.threads.items() if t.agent_id is None and heartbeat(t)}


def _long_running(facts: Facts, scope: str, index: Index) -> Tally:
    out = Tally()
    out.requests = _requests(_project_threads(facts, scope))
    out.base = out.requests
    beats = _beats(facts)
    for project, lc in facts.long_context.items():
        if scope != "all" and _group(project) != scope:
            continue
        for key, acc in lc.per_thread.items():
            if key in beats or not acc[2]:
                continue
            out.count += acc[2]
            out.usd += max(0.0, acc[0])
            out.add("threads", 1)
        out.top("max_context", lc.max_context)
    return out


def _late_compactions(facts: Facts, scope: str, index: Index) -> Tally:
    out = Tally()
    out.requests = _requests(_project_threads(facts, scope))
    out.base = out.requests
    beats = _beats(facts)
    for key, comps in facts.compactions.items():
        if key in beats or not _in_project(facts, scope)(key[0]):
            continue
        for c in comps:
            if c.timestamp >= facts.since:
                out.add("compactions", 1)
            if c.pre_tokens > LATE_COMPACTION and c.timestamp >= facts.since:
                out.count += 1
                out.usd += max(0.0, c.excess_usd)
                out.add("turns_above", c.turns)
                out.top("max_pre", c.pre_tokens)
    return out


def _heartbeat(facts: Facts, scope: str, index: Index) -> Tally:
    """The context each run of the scope's main threads starts with (a run starts
    at a resume after a pause, or at the thread's first request)."""
    out = Tally()
    threads = _project_threads(facts, scope)
    out.requests = _requests(threads)
    for t in threads:
        if t.agent_id is not None:
            continue
        for start, rates in t.runs:
            out.samples.append(start)
            out.count += start
            out.usd += start * rates
            out.base += 1
            out.top("max_start", start)
        out.add("resumed", len(t.resumes))
        if heartbeat(t):
            out.add("beats", 1)
    return out


def _never_ended(facts: Facts, scope: str, index: Index) -> Tally:
    out = Tally()
    threads = _project_threads(facts, scope)
    out.requests = _requests(threads)
    out.base = out.requests
    beats = _beats(facts)
    long_sessions = {
        t.session_id
        for t in threads
        if t.agent_id is None
        and (t.session_id, None) not in beats
        and _span(facts, t) > NEVER_ENDED_SPAN
    }
    overall = fresh_context(facts)
    typical = overall if scope == "all" else fresh_context(facts, scope, default=overall)
    for t in threads:
        if t.session_id not in long_sessions:
            continue
        out.count += t.requests
        if t.agent_id is None:
            out.usd += max(0.0, band_usd(t, typical) - len(t.days) * typical * t.write_rate)
            out.add("sessions", 1)
            out.top("longest_days", _span(facts, t).days)
            out.add("compactions", len(facts.compactions.get((t.session_id, None), [])))
    return out


def _model_mix(facts: Facts, scope: str, index: Index) -> Tally:
    out = Tally()
    threads = _type_threads(facts, scope)
    out.requests = _requests(threads)
    out.base = out.requests
    for (kind, _project), mm in facts.model_mix.items():
        if kind != scope:
            continue
        out.count += mm.cost
        out.usd += mm.cost
        out.add("top_cost", mm.top_cost)
        for name, usd in mm.by_model.items():
            out.add(f"model:{normalize(name)}", usd)
    out.add("spawns", len(threads))
    out.add("tool_calls", sum(t.tool_calls for t in threads))
    return out


def _errors(index: Index, facts: Facts, out: Tally, threads: Iterable[Thread]) -> None:
    """Tool results flagged as errors in `threads` over the facts' window."""
    keys = {(t.session_id, t.agent_id) for t in threads}
    flagged = errors = 0
    for e in index.events(facts.since, facts.until, kinds=frozenset({EventKind.TOOL_RESULT})):
        if (e.session_id, e.agent_id) not in keys or "is_error" not in e.attrs:
            continue
        flagged += 1
        errors += 1 if e.attrs.get("is_error") is True else 0
    out.add("flagged", flagged)
    out.add("errors", errors)


def _model_mix_with_errors(facts: Facts, scope: str, index: Index) -> Tally:
    out = _model_mix(facts, scope, index)
    _errors(index, facts, out, _type_threads(facts, scope))
    return out


def _carried(
    match: Callable[[Facts, str, ItemKey], bool],
    threads: Callable[[Facts, str], list[Thread]],
    by_thread: bool = False,
) -> Callable[..., Tally]:
    """Tokens carried in prompts by the items `match` picks, per request of
    `threads`; `by_thread` matches items in those threads only (an agent type's)."""

    def tally(facts: Facts, scope: str, index: Index) -> Tally:
        out = Tally()
        ts = threads(facts, scope)
        out.requests = _requests(ts)
        out.base = out.requests
        keys = {(t.session_id, t.agent_id) for t in ts} if by_thread else None
        in_scope = _in_project(facts, scope)
        names: set[str] = set()
        for (sid, aid, key), acc in facts.accounts.items():
            if keys is not None and (sid, aid) not in keys:
                continue
            if keys is None and not in_scope(sid):
                continue
            if not match(facts, scope, key):
                continue
            _carry(out, acc)
            names.add(str(key[-1]) if key[0] in ("tool", "skill", "mcp") else str(key[1:]))
        out.add("items", len(names))
        return out

    return tally


def _carry(out: Tally, acc: Account) -> None:
    out.count += acc.size * acc.requests
    out.usd += acc.usd
    out.add("carried_items", 1)
    out.top("largest", acc.size)
    out.add("size_sum", acc.size)


def _report_threads(facts: Facts, scope: str) -> list[Thread]:
    """Every thread of the sessions that ran an agent of type `scope`."""
    sessions = {t.session_id for t in _type_threads(facts, scope)}
    return [t for t in facts.threads.values() if t.requests and t.session_id in sessions]


def _is_report(facts: Facts, scope: str, key: ItemKey) -> bool:
    return key[0] == "report" and facts.spawn_types.get(str(key[2]), "(unknown agent)") == scope


# --------------------------------------------------------------------------- evidence


def _per_week(key: str, fmt: str = "{:,.1f}") -> Callable[[Tally, float], str]:
    return lambda t, w: fmt.format(t.sums.get(key, 0.0) / w) if w else "-"


def _count_per_week(fmt: str = "{:,.1f}") -> Callable[[Tally, float], str]:
    return lambda t, w: fmt.format(t.count / w) if w else "-"


def _largest(key: str, fmt: str = "{:,.0f}") -> Callable[[Tally, float], str]:
    return lambda t, w: fmt.format(t.maxes[key]) if key in t.maxes else "-"


def _tokens(n: float) -> str:
    return f"{n / 1000:,.0f}k" if n >= 10_000 else f"{n:,.0f}"


def _ratio(num: str, den: str, scale: float = 1.0, fmt: str = "{:,.1f}") -> Callable[..., str]:
    def value(t: Tally, w: float) -> str:
        d = t.sums.get(den, 0.0) if den != "count" else t.count
        n = t.sums.get(num, 0.0) if num != "count" else t.count
        return fmt.format(n / d * scale) if d else "-"

    return value


def _top_cause(t: Tally, w: float) -> str:
    causes = {k[6:]: v for k, v in t.sums.items() if k.startswith("cause:")}
    if not causes or not w:
        return "-"
    name, n = max(causes.items(), key=lambda kv: (kv[1], kv[0]))
    return f"{name} {n / w:,.1f}"


def _per_rewrite_tokens(t: Tally, w: float) -> str:
    return _tokens(t.sums.get("tokens", 0.0) / t.count) if t.count else "-"


def _opus_share(t: Tally, w: float) -> str:
    return f"{t.sums.get('top_cost', 0.0) / t.count:.0%}" if t.count else "-"


def _main_model(t: Tally, w: float) -> str:
    models = {k[6:]: v for k, v in t.sums.items() if k.startswith("model:")}
    return max(models.items(), key=lambda kv: kv[1])[0] if models else "-"


def _errors_per_100(t: Tally, w: float) -> str:
    calls = t.sums.get("tool_calls", 0.0)
    return f"{t.sums.get('errors', 0.0) / calls * 100:.1f}" if calls else "-"


def _median_tokens(t: Tally, w: float) -> str:
    return _tokens(median(t.samples)) if t.samples else "-"


_REWRITE_ROWS: tuple[Row, ...] = (
    ("Rewrites per week", _count_per_week()),
    ("Tokens rewritten per rewrite", _per_rewrite_tokens),
)


def _sub_calls(t: Thread) -> float:
    return t.tool_calls if t.agent_id is not None else 0


def _sub_requests(t: Thread) -> float:
    return t.requests if t.agent_id is not None else 0


def _main_requests(t: Thread) -> float:
    return t.requests if t.agent_id is None else 0


def _rewrite_measure(
    label: str,
    pick: Callable[[Facts], Mapping[str, Rewrite]],
    pause: str,
    habit: Callable[[Thread], float],
    habit_label: str,
) -> Measure:
    rows = (*_REWRITE_ROWS, (f"Longest {pause} (min)", _largest("longest_min")))
    if pause == "wait":
        rows = (*rows, ("Top tool waited on, rewrites per week", _top_cause))
    return Measure(
        label, "per 1,000 requests", 1000, _rewrites(pick, habit), rows, habit=habit_label
    )


def _prefix(label: str, match: Callable[[Facts, str, ItemKey], bool], what: str) -> Measure:
    return Measure(
        label,
        "tokens per request",
        1,
        _carried(match, _project_threads),
        (
            (f"{what}, a week's average", _per_week("items", "{:,.0f}")),
            ("Largest item (tokens)", _largest("largest")),
        ),
        fmt="{:,.0f}",
    )


def _is_mcp(facts: Facts, scope: str, key: ItemKey) -> bool:
    server = _server(key)
    return server is not None and not server.startswith("claude_ai_")


TOOL_WAIT = _rewrite_measure(
    "Tool-wait rewrites per 1,000 requests",
    lambda f: f.tool_waits,
    "wait",
    _sub_calls,
    "subagent tool calls",
)
LATE_RESUME = _rewrite_measure(
    "Late-resume rewrites per 1,000 requests",
    lambda f: f.resumes,
    "pause",
    _sub_requests,
    "subagent requests",
)
IDLE = _rewrite_measure(
    "Idle rewrites per 1,000 requests",
    lambda f: f.idle,
    "pause",
    _main_requests,
    "main-thread requests",
)
SWITCH = Measure(
    "Model-switch rewrites per 1,000 requests",
    "per 1,000 requests",
    1000,
    _rewrites(lambda f: f.switches, _main_requests),
    _REWRITE_ROWS,
    habit="main-thread requests",
)

MEASURES: dict[str, Measure] = {
    "subagent_cache_expiry": TOOL_WAIT,
    TOOL_WAIT_EXPIRY: TOOL_WAIT,
    "subagent_resumed_late": LATE_RESUME,
    LATE_RESUME_EXPIRY: LATE_RESUME,
    "idle_cache_rewrites": IDLE,
    IDLE_EXPIRY: IDLE,
    "model_switches": SWITCH,
    "long_running_agents": Measure(
        f"Turns past {LONG_CONTEXT // 1000}k tokens per 1,000 requests",
        "per 1,000 requests",
        1000,
        _long_running,
        (
            (f"Turns past {LONG_CONTEXT // 1000}k per week", _count_per_week()),
            (f"Threads past {LONG_CONTEXT // 1000}k per week", _per_week("threads")),
            ("Largest context (tokens)", _largest("max_context")),
        ),
    ),
    "late_compactions": Measure(
        f"Compactions past {LATE_COMPACTION // 1000}k tokens per 1,000 requests",
        "per 1,000 requests",
        1000,
        _late_compactions,
        (
            ("Late compactions per week", _count_per_week()),
            ("All compactions per week", _per_week("compactions")),
            ("Largest start (tokens)", _largest("max_pre")),
            (
                f"Turns above {LATE_COMPACTION // 1000}k per late one",
                _ratio("turns_above", "count"),
            ),
        ),
    ),
    "heartbeat_growing_session": Measure(
        "Median context at a run's start (tokens)",
        "tokens at a run's start",
        1,
        _heartbeat,
        (
            ("Runs per week", lambda t, w: f"{t.base / w:,.1f}" if w else "-"),
            ("Resumes per week", _per_week("resumed")),
            ("Largest start (tokens)", _largest("max_start")),
        ),
        median=True,
        fmt="{:,.0f}",
    ),
    "session_never_ended": Measure(
        "Share of requests in sessions open over 7 days (%)",
        "% of requests",
        100,
        _never_ended,
        (
            ("Sessions open over 7 days (week average)", _per_week("sessions")),
            ("Longest open (days)", _largest("longest_days")),
            ("Compactions in them per week", _per_week("compactions")),
        ),
    ),
    "model_mix": Measure(
        "Dollars per request of the agent type",
        "dollars per request",
        1,
        _model_mix_with_errors,
        (
            ("Spend per week ($)", _count_per_week("{:,.2f}")),
            ("Share on Opus", _opus_share),
            ("Model with the most spend", _main_model),
        ),
        fmt="{:,.4f}",
        habit="requests of this agent type",
        quality=(
            (
                "Requests per spawn",
                lambda t, w: f"{t.requests / s:,.1f}" if (s := t.sums.get("spawns")) else "-",
            ),
            (ERRORS_ROW, _errors_per_100),
        ),
    ),
    "skills_unused": _prefix(
        "Skill-listing tokens per request", lambda f, s, k: k[0] == "skill", "Skills listed"
    ),
    "mcp_servers_unused": _prefix("MCP tool tokens per request", _is_mcp, "MCP items"),
    "claudeai_connectors_unused": _prefix(
        "Connector tokens per request", lambda f, s, k: _is_connector(k), "Connector items"
    ),
    "large_memory": _prefix(
        "Memory and instructions tokens per request",
        lambda f, s, k: k[0] == "memory",
        "Memory files",
    ),
    "agent_tools_unused": Measure(
        "Tool-listing tokens per request of the agent type",
        "tokens per request",
        1,
        _carried(lambda f, s, k: k[0] == "tool", _type_threads, by_thread=True),
        (
            ("Tools listed, a week's average", _per_week("items", "{:,.0f}")),
            ("Largest tool (tokens)", _largest("largest")),
            ("Requests per week", lambda t, w: f"{t.requests / w:,.0f}" if w else "-"),
        ),
        fmt="{:,.0f}",
    ),
    "images_in_tool_results": Measure(
        "Image tokens carried per request",
        "tokens per request",
        1,
        _carried(lambda f, s, k: k[0] == "image", _project_threads),
        (
            ("Images per week", _per_week("carried_items")),
            ("Tokens per image", _ratio("size_sum", "carried_items", fmt="{:,.0f}")),
            ("Largest image (tokens)", _largest("largest")),
        ),
        fmt="{:,.0f}",
    ),
    "long_subagent_reports": Measure(
        "Report tokens carried per request of the sessions that ran it",
        "tokens per request",
        1,
        _carried(_is_report, _report_threads, by_thread=True),
        (
            ("Long reports per week", _per_week("carried_items")),
            ("Tokens per report", _ratio("size_sum", "carried_items", fmt="{:,.0f}")),
            ("Largest report (tokens)", _largest("largest")),
        ),
        fmt="{:,.0f}",
    ),
}

# kinds counted on an agent type, not a project
AGENT_SCOPED = frozenset({"model_mix", "agent_tools_unused", "long_subagent_reports"})
