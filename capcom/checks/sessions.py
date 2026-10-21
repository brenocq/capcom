"""Checks on how sessions are run over days: sessions that never end, heartbeats that
resume one growing session, and the model mix of each agent type.

The two session checks price the rereads of a main thread's contexts in a band,
from its histogram (facts.py: per 10k-token bucket, the requests and the sums of
their read rates and of context x read rate):

    band_usd(lo, hi) = sum over requests of (min(context, hi) - lo)+ x read price

with `hi` = LONG_CONTEXT for a session that never ended (it overlaps the
long-running and late-compaction findings, other ways to the same rereads), and no
upper end for a heartbeat, whose sessions those two findings leave out. A session
that is a heartbeat is not also a session that never ended.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

from capcom.checks.agents import AgentDefinition
from capcom.checks.facts import (
    BUCKET_TOKENS,
    EVIDENCE_PER_FINDING,
    Facts,
    ModelMix,
    Thread,
    ThreadKey,
    _short,
    heartbeat,
)
from capcom.checks.rules import (
    BUILT_IN_AGENTS,
    _finding,
    _group,
    _is_openclaw,
    _k,
    _net,
    _usd,
)
from capcom.explain.shared import LONG_CONTEXT
from capcom.types import Evidence, Finding

NEVER_ENDED_SPAN = timedelta(days=7)  # a session active longer than this never ended
NEVER_ENDED_COMPACTIONS = 3  # ... with this many compactions in the window
NEVER_ENDED_AUTO = 2  # ... or this many automatic ones (the context hit its top)
FRESH_SPAN = timedelta(days=1)  # sessions this short give the typical fresh context
FRESH_CONTEXT_DEFAULT = 50_000  # when the window has no such session
MIX_MIN_SPAWNS = 5  # spawns of a type before judging its model
MIX_TOP_SHARE = 0.5  # share of the type's spend on Opus (or Fable)
MIX_SHORT_OUTPUT = 1_000  # mean output tokens per request on that model, at most
MIX_TOOL_CALLS = 0.7  # mean tool calls per request on that model, at least


def band_usd(t: Thread, lo: int, hi: int | None = LONG_CONTEXT) -> float:
    """What thread `t`'s requests paid to reread their context between `lo` and `hi`
    tokens (both multiples of BUCKET_TOKENS, which makes the sum exact; None: no
    upper end)."""
    usd = 0.0
    for b, (_, rate, ctx_rate) in t.hist.items():
        start = b * BUCKET_TOKENS
        if hi is not None and start >= hi:
            usd += (hi - lo) * rate
        elif start >= lo:
            usd += ctx_rate - lo * rate
    return max(0.0, usd)


def _ceil_bucket(tokens: float) -> int:
    return int(-(-tokens // BUCKET_TOKENS) * BUCKET_TOKENS)


def _span(facts: Facts, t: Thread) -> timedelta:
    seen = t.last - t.first if t.last and t.first else timedelta(0)
    return max(facts.session_span.get(t.session_id, timedelta(0)), seen)


def _main_threads(facts: Facts) -> list[Thread]:
    return [t for t in facts.threads.values() if t.agent_id is None and t.hist]


def fresh_context(facts: Facts, project: str | None = None, default: int | None = None) -> int:
    """A fresh session's typical context: the median context of the main-thread
    requests of sessions active under FRESH_SPAN (in `project`'s group when given),
    rounded up to a bucket; at most LONG_CONTEXT. Without such sessions, `default`
    (FRESH_CONTEXT_DEFAULT when None)."""
    counts: dict[int, float] = defaultdict(float)
    for t in _main_threads(facts):
        if project is not None and _group(t.project) != project:
            continue
        if _span(facts, t) < FRESH_SPAN:
            for b, (n, _, _) in t.hist.items():
                counts[b] += n
    total = sum(counts.values())
    if not total:
        return FRESH_CONTEXT_DEFAULT if default is None else default
    seen = 0.0
    for b in sorted(counts):
        seen += counts[b]
        if seen >= total / 2:
            return min((b + 1) * BUCKET_TOKENS, LONG_CONTEXT)
    return FRESH_CONTEXT_DEFAULT  # not reached


def heartbeat_growing_session(facts: Facts) -> list[Finding]:
    """A main thread resumed on a regular cadence (a heartbeat, a scheduled job)
    whose context keeps growing: each run rereads the history of all the runs
    before it. Saving, over the thread's requests in the window:

        band_usd(fresh run, no upper end)

    every reread above what a fresh run carries: the smaller of the smallest
    context at a resume and the project's fresh-session context (fresh_context;
    the window may start when the session is already big), rounded up to a bucket.
    A fresh run needs no handoff. These sessions are left out of
    `long_running_agents` and `late_compactions`, so their dollars count once."""
    per_project: dict[str, list[tuple[Thread, timedelta, int, int, float]]] = defaultdict(list)
    for t in _main_threads(facts):
        beat = heartbeat(t)
        if beat is None:
            continue
        group = _group(t.project)
        fresh = min(beat[1], fresh_context(facts, group, default=beat[1]))
        usd = band_usd(t, _ceil_bucket(fresh), None)
        per_project[group].append((t, beat[0], fresh, beat[2], usd))
    out: list[Finding] = []
    for project, found in per_project.items():
        found.sort(key=lambda x: -x[4])
        usd = sum(x[4] for x in found)
        t, every, first, last, _ = found[0]
        minutes = every.total_seconds() / 60
        f = _finding(
            facts,
            usd,
            kind="heartbeat_growing_session",
            title="A heartbeat resumes one growing session",
            scope=project,
            summary=(
                f"{len(found)} session(s) were resumed on a steady cadence (session "
                f"{_short(t.session_id)}: {len(t.resumes)} times, about every "
                f"{minutes:.0f} min); its runs carried about {_k(last)} tokens where a "
                f"fresh run carries {_k(first)}: every run rereads the earlier runs. "
                f"Rereading what a fresh run would not carry cost {_usd(usd)} (counted "
                "here, not in Long-running agents or late compactions)."
            ),
            advice=(
                "Start each run fresh: a new session per run, or /clear between runs, "
                "keeping what a run must remember in a file it reads at the start."
            ),
            evidence=tuple(
                Evidence(
                    f"session {_short(x[0].session_id)}: {len(x[0].resumes)} resumes about "
                    f"every {x[1].total_seconds() / 60:.0f} min, context {_k(x[3])} at the "
                    f"median resume, {_k(x[2])} for a fresh run",
                    session_id=x[0].session_id,
                    timestamp=x[0].resumes[-1][0],
                )
                for x in found[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "sessions": len(found),
                "resumes": len(t.resumes),
                "interval_minutes": round(minutes, 1),
                "fresh_context": first,
                "median_context": last,
            },
        )
        if f:
            out.append(f)
    return out


def session_never_ended(facts: Facts) -> list[Finding]:
    """A main thread active over more than NEVER_ENDED_SPAN with at least
    NEVER_ENDED_COMPACTIONS compactions in the window, or NEVER_ENDED_AUTO automatic
    ones (each one ran when the context reached the top of the model's window).
    Heartbeat sessions are left to `heartbeat_growing_session`. Saving, per session:

        band_usd(fresh_context, LONG_CONTEXT) - active days x HANDOFF_TOKENS x write price

    floored at zero: the rereads above what a fresh session typically carries (the
    part past LONG_CONTEXT is in `long_running_agents`), less a fresh start
    with a handoff note on each day it was used."""
    typical = fresh_context(facts)
    per_project: dict[str, list[tuple[Thread, int, int, float]]] = defaultdict(list)
    for t in _main_threads(facts):
        if _span(facts, t) <= NEVER_ENDED_SPAN or heartbeat(t) is not None:
            continue
        found_comps = facts.compactions.get((t.session_id, None), [])
        auto = sum(1 for c in found_comps if c.trigger == "auto")
        if len(found_comps) < NEVER_ENDED_COMPACTIONS and auto < NEVER_ENDED_AUTO:
            continue
        usd = _net(band_usd(t, typical), len(t.days) * t.handoff_usd)
        per_project[_group(t.project)].append((t, len(found_comps), auto, usd))
    out: list[Finding] = []
    for project, found in per_project.items():
        found.sort(key=lambda x: -x[3])
        usd = sum(x[3] for x in found)
        longest = max(_span(facts, x[0]) for x in found)
        comps = sum(x[1] for x in found)
        f = _finding(
            facts,
            usd,
            kind="session_never_ended",
            title="Sessions that never ended",
            scope=project,
            summary=(
                f"{len(found)} session(s) stayed open for up to {longest.days} days, with "
                f"{comps} compaction(s) in this window; each turn rereads a context a "
                f"fresh session would not carry (a fresh one here typically holds about "
                f"{_k(typical)} tokens). The saving counts the rereads between that and "
                f"{_k(LONG_CONTEXT)}, net of a handoff on each day of use."
            ),
            advice=(
                "End the session with a handoff note (what is done, what is next, the "
                "files that matter) and start a fresh one for the next task or day."
            ),
            evidence=tuple(
                Evidence(
                    f"session {_short(x[0].session_id)}: open {_span(facts, x[0]).days} days, "
                    f"{x[1]} compactions ({x[2]} automatic), up to {_k(x[0].max_context)} "
                    "tokens",
                    session_id=x[0].session_id,
                    timestamp=x[0].last,
                )
                for x in found[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "sessions": len(found),
                "longest_days": longest.days,
                "compactions": comps,
                "fresh_context": typical,
            },
        )
        if f:
            out.append(f)
    return out


def model_mix(
    facts: Facts,
    definitions: Mapping[str, AgentDefinition | Sequence[AgentDefinition]],
    openclaw_root: Path | None = None,
) -> list[Finding]:
    """A subagent type that spends most of its cost (MIX_TOP_SHARE) on Opus or
    Fable, in short turns (at most MIX_SHORT_OUTPUT output tokens per request on
    average) heavy on tools (at least MIX_TOOL_CALLS calls per request), over
    MIX_MIN_SPAWNS spawns or more in Claude Code projects (OpenClaw workspaces left
    out). Saving: what those requests cost less the same usage priced on the Sonnet
    of the same version. Whether Sonnet does the work as well is a judgement call;
    the `model: sonnet` line is offered only when a definition file exists."""
    per_type: dict[str, list[ModelMix]] = defaultdict(list)
    for (kind, project), mm in facts.model_mix.items():
        if kind.startswith("(") or _is_openclaw(facts, project, openclaw_root):
            continue
        per_type[kind].append(mm)
    out: list[Finding] = []
    for kind, mms in per_type.items():
        threads: set[ThreadKey] = set().union(*(m.threads for m in mms))
        cost = sum(m.cost for m in mms)
        top = sum(m.top_cost for m in mms)
        requests = sum(m.top_requests for m in mms)
        if len(threads) < MIX_MIN_SPAWNS or cost <= 0 or not requests:
            continue
        share = top / cost
        output = sum(m.top_output for m in mms) / requests
        calls = sum(m.top_calls for m in mms) / requests
        if share < MIX_TOP_SHARE or output > MIX_SHORT_OUTPUT or calls < MIX_TOOL_CALLS:
            continue
        by_model: dict[str, float] = defaultdict(float)
        for m in mms:
            for name, usd in m.by_model.items():
                by_model[name] += usd
        main = max(by_model.items(), key=lambda kv: kv[1])[0]
        usd = top - sum(m.top_as_sonnet for m in mms)
        defs = definitions.get(kind)
        definition = defs if isinstance(defs, AgentDefinition) else (defs[0] if defs else None)
        judgement = (
            "Whether Sonnet does this agent's work as well is a judgement call on quality: "
            "try it on a few tasks before keeping it."
        )
        if definition is not None and kind not in BUILT_IN_AGENTS:
            advice = (
                f"Run {kind} on Sonnet: in the frontmatter of {definition.path} (between "
                "its --- lines), set its model (replace a model: line if it has one). " + judgement
            )
            change: str | None = "model: sonnet"
        else:
            advice = (
                f"Run {kind} on Sonnet when you spawn it; no definition file for it was "
                "found, so there is no line to paste. " + judgement
            )
            change = None
        f = _finding(
            facts,
            usd,
            kind="model_mix",
            title=f"The {kind} agent runs on {main.split('-')[1].capitalize()}"
            if main.count("-") >= 1
            else f"The {kind} agent runs on {main}",
            scope=kind,
            summary=(
                f"{share:.0%} of the {_usd(cost)} its {len(threads)} spawns spent went to "
                f"{main} or another model priced above Sonnet, in short turns ({output:,.0f} "
                f"output tokens and {calls:.1f} tool calls per request); on Sonnet the "
                f"same {requests:,} requests would have cost {_usd(top - usd)}."
            ),
            advice=advice,
            change=change,
            metrics={
                "spawns": len(threads),
                "top_share": round(share, 4),
                "model": main,
                "output_per_request": round(output, 1),
                "tool_calls_per_request": round(calls, 2),
                "requests": requests,
                # its saving shrinks every other one on these requests: keep it out
                # of a total of savings
                "judgement_call": True,
            },
        )
        if f:
            out.append(f)
    return out


def session_findings(
    facts: Facts,
    definitions: Mapping[str, AgentDefinition | Sequence[AgentDefinition]],
    openclaw_root: Path | None = None,
) -> list[Finding]:
    return [
        *session_never_ended(facts),
        *heartbeat_growing_session(facts),
        *model_mix(facts, definitions, openclaw_root),
    ]
