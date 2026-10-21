"""The setup checks (docs/design.md, "Setup checks"), each a function of the facts.

Every saving is a weekly figure at the window's pace:

    saving_usd_per_week = usd_in_window / weeks_in_window

and the usd of an item carried in prompts is, per request that carried it, its
size in tokens times that request's price per prompt token (facts.py), which is
"size x requests x the right price" with cache reads and rewrites priced as they
were billed. Each check's docstring says what it counts.

Some savings overlap (an unused connector tool is also part of a long context); a
check never counts the same tokens twice within itself, but the findings are not
meant to be added up.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from capcom.checks.agents import AgentDefinition
from capcom.checks.facts import (
    EVIDENCE_PER_FINDING,
    LATE_COMPACTION,
    LONG_CONTEXT,
    LONG_REPORT_TOKENS,
    Account,
    Compaction,
    Facts,
    ItemKey,
    LongContext,
    Rewrite,
    Thread,
)
from capcom.cost.top import TEMPORARY, project_label
from capcom.types import AttrValue, Evidence, Finding

MEMORY_BUDGET_TOKENS = 2_000  # one always-loaded file of about 150 lines
REPORT_TARGET_TOKENS = 500  # a report of about 25 lines
IMAGE_SAVING_SHARE = 0.5  # cropped and downscaled images: half the tokens
SUBAGENT_MAX_CALLS = 2
MIN_AGENTS = 2  # spawns of a type before judging it
MIN_SAVING_USD_PER_WEEK = 0.01
BUILT_IN_AGENTS = frozenset(
    {"general-purpose", "Explore", "Plan", "statusline-setup", "claude-code-guide"}
)
CONNECTOR_TOOL_PREFIX = "mcp__claude_ai_"


def _k(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _settings_change(env_or_keys: Mapping[str, object]) -> str:
    return json.dumps(env_or_keys, indent=2)


def _where(facts: Facts, project: str, rel: str) -> str:
    cwd = facts.cwd_of(project)
    return f"{cwd}/{rel}" if cwd and project != TEMPORARY else f"<project>/{rel}"


def _group(project: str) -> str:
    return project_label(project)


@dataclass(slots=True)
class _Sum:
    usd: float = 0.0
    requests: int = 0
    size: int = 0
    sessions: set[str] = field(default_factory=set)

    def add(self, sid: str, acc: Account) -> None:
        self.usd += acc.usd
        self.requests += acc.requests
        self.size = max(self.size, acc.size)
        self.sessions.add(sid)


def _accounts(
    facts: Facts, match: Callable[[ItemKey], bool]
) -> Iterable[tuple[str, str | None, ItemKey, Account]]:
    for (sid, aid, key), acc in facts.accounts.items():
        if match(key):
            yield sid, aid, key, acc


def _server(key: ItemKey) -> str | None:
    """The MCP server an item belongs to (tool names `mcp__<server>__<tool>`, or the
    server named by an instructions item)."""
    if key[0] == "tool" and key[2].startswith("mcp__"):
        parts = key[2].split("__")
        return parts[1] if len(parts) > 2 else None
    if key[0] == "mcp":
        name = key[1]
        if name.startswith("claude.ai "):
            return "claude_ai_" + name[len("claude.ai ") :].replace(" ", "_")
        return name
    return None


def _is_connector(key: ItemKey) -> bool:
    server = _server(key)
    return server is not None and server.startswith("claude_ai_")


def _called_server(name: str) -> str | None:
    parts = name.split("__")
    return parts[1] if name.startswith("mcp__") and len(parts) > 2 else None


def _finding(
    facts: Facts,
    usd: float,
    *,
    kind: str,
    title: str,
    scope: str,
    summary: str,
    advice: str,
    change: str | None = None,
    evidence: tuple[Evidence, ...] = (),
    metrics: Mapping[str, AttrValue] | None = None,
) -> Finding | None:
    """A finding saving `usd` over the window, or None below the weekly floor."""
    saving = facts.per_week(usd)
    if saving < MIN_SAVING_USD_PER_WEEK:
        return None
    return Finding(
        kind=kind,
        title=title,
        scope=scope,
        summary=summary,
        saving_usd_per_week=saving,
        advice=advice,
        change=change,
        evidence=evidence,
        metrics=dict(metrics or {}),
    )


# --------------------------------------------------------------------------- connectors


def claudeai_connectors_unused(facts: Facts) -> list[Finding]:
    """claude.ai connectors (`mcp__claude_ai_*` tools and their instructions) in the
    sessions of a project that never call one. Saving: the connector items' size x
    the requests of those sessions (main threads and subagents) x each request's
    prompt price."""
    listed: dict[str, dict[str, _Sum]] = defaultdict(dict)  # project -> session -> sum
    servers: dict[str, set[str]] = defaultdict(set)
    for sid, _aid, key, acc in _accounts(facts, _is_connector):
        project = _group(facts.project_of(sid))
        listed[project].setdefault(sid, _Sum()).add(sid, acc)
        servers[project].add(_server(key) or "?")
    out: list[Finding] = []
    for project, sessions in listed.items():
        using = {
            sid
            for sid in sessions
            if any(n.startswith(CONNECTOR_TOOL_PREFIX) for n in facts.calls_by_session.get(sid, ()))
        }
        unused = {sid: s for sid, s in sessions.items() if sid not in using}
        if not unused or len(using) > len(unused):
            continue
        usd = sum(s.usd for s in unused.values())
        reqs = sum(s.requests for s in unused.values())
        tokens = _connector_tokens(facts, set(unused))
        names = sorted(n.removeprefix("claude_ai_").replace("_", " ") for n in servers[project])
        top = sorted(unused.items(), key=lambda kv: -kv[1].usd)[:EVIDENCE_PER_FINDING]
        summary = (
            f"{len(names)} claude.ai connector{'s' if len(names) != 1 else ''} "
            f"({', '.join(names)}) rode in {_k(reqs)} requests of {len(unused)} "
            f"session{'s' if len(unused) != 1 else ''} that never called them, "
            f"about {_k(tokens)} tokens in each prompt."
        )
        if using:
            summary += f" {len(using)} other session(s) in this project did call them."
        f = _finding(
            facts,
            usd,
            kind="claudeai_connectors_unused",
            title="claude.ai connectors in sessions that never use them",
            scope=project,
            summary=summary,
            advice=(
                "Turn the claude.ai connectors off for this project; they stay "
                "available in claude.ai and in the projects that use them. "
                f"In {_where(facts, project, '.claude/settings.json')}:"
            ),
            change=_settings_change({"env": {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}}),
            evidence=tuple(
                Evidence(
                    f"session {sid[:8]}: {s.requests} requests carried them, {_usd(s.usd)}",
                    session_id=sid,
                )
                for sid, s in top
            ),
            metrics={
                "connectors": len(names),
                "sessions_listing": len(sessions),
                "sessions_using": len(using),
                "tokens_per_request": tokens,
                "requests": reqs,
            },
        )
        if f:
            out.append(f)
    return out


def _connector_tokens(facts: Facts, sessions: set[str]) -> int:
    """The largest per-thread total size of connector items in `sessions`."""
    per_thread: dict[tuple[str, str | None], int] = defaultdict(int)
    for sid, aid, _key, acc in _accounts(facts, _is_connector):
        if sid in sessions:
            per_thread[(sid, aid)] += acc.size
    return max(per_thread.values(), default=0)


# --------------------------------------------------------------------------- MCP servers


def tools_unused(facts: Facts) -> list[Finding]:
    """MCP servers (other than claude.ai connectors) whose tools ride in a
    project's prompts while none of them is called there. Saving: the server's
    listed tools and instructions x the requests that carried them x each request's
    prompt price. Built-in tools are counted in the metrics only: a main thread
    cannot drop them."""
    loaded: dict[str, set[str]] = defaultdict(set)
    per_server: dict[str, dict[str, _Sum]] = defaultdict(dict)
    for sid, _aid, key, acc in _accounts(facts, lambda k: k[0] in ("tool", "mcp")):
        project = _group(facts.project_of(sid))
        if key[0] == "tool":
            loaded[project].add(key[2])
        server = _server(key)
        if server is None or server.startswith("claude_ai_"):
            continue
        per_server[project].setdefault(server, _Sum()).add(sid, acc)
    called: dict[str, set[str]] = defaultdict(set)
    for sid, session_calls in facts.calls_by_session.items():
        called[_group(facts.project_of(sid))].update(session_calls)
    out: list[Finding] = []
    for project, servers in per_server.items():
        used = {s for n in called[project] if (s := _called_server(n))}
        unused = {s: v for s, v in servers.items() if s not in used}
        if not unused:
            continue
        usd = sum(v.usd for v in unused.values())
        reqs = max(v.requests for v in unused.values())
        n_tools = sum(1 for n in loaded[project] if (_called_server(n) or "") in unused)
        listed_names = loaded[project]
        called_loaded = called[project] & listed_names
        names = sorted(unused)
        f = _finding(
            facts,
            usd,
            kind="mcp_servers_unused",
            title="MCP servers loaded but never called",
            scope=project,
            summary=(
                f"{len(names)} MCP server{'s' if len(names) != 1 else ''} "
                f"({', '.join(names)}) put {n_tools} tools and their instructions into "
                f"up to {_k(reqs)} requests; none of their tools was called in this project."
            ),
            advice=(
                "Turn these servers off for this project. For a server from the project's "
                ".mcp.json, the change below does it (in "
                f"{_where(facts, project, '.claude/settings.local.json')}); for a user "
                "server, add it with `claude mcp add --scope project` only in the "
                "projects that use it."
            ),
            change=_settings_change({"disabledMcpjsonServers": names}),
            evidence=tuple(
                Evidence(
                    f"{s}: in {len(v.sessions)} sessions, {_k(v.requests)} requests, {_usd(v.usd)}"
                )
                for s, v in sorted(unused.items(), key=lambda kv: -kv[1].usd)[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "tools_loaded": len(listed_names),
                "tools_called": len(called_loaded),
                "servers_unused": len(names),
                "servers_loaded": len(servers),
            },
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- skills


def _skill_used(name: str, invoked: set[str]) -> bool:
    short = name.split(":")[-1]
    return name in invoked or short in invoked or any(i.split(":")[-1] == short for i in invoked)


def skills_unused(facts: Facts) -> list[Finding]:
    """Skills in a project's skill listing that were never invoked there (neither by
    the Skill tool nor by a slash command). Saving: each unused skill's share of the
    listing x the requests that carried it x each request's prompt price."""
    listed: dict[str, dict[str, _Sum]] = defaultdict(dict)
    for sid, _aid, key, acc in _accounts(facts, lambda k: k[0] == "skill"):
        listed[_group(facts.project_of(sid))].setdefault(key[1], _Sum()).add(sid, acc)
    invoked: dict[str, set[str]] = defaultdict(set)
    for sid, session_skills in facts.skills_by_session.items():
        invoked[_group(facts.project_of(sid))].update(session_skills)
    out: list[Finding] = []
    for project, skills in listed.items():
        unused = {n: s for n, s in skills.items() if not _skill_used(n, invoked[project])}
        if not unused:
            continue
        usd = sum(s.usd for s in unused.values())
        tokens = sum(s.size for s in unused.values())
        names = sorted(unused, key=lambda n: -unused[n].usd)
        shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
        f = _finding(
            facts,
            usd,
            kind="skills_unused",
            title="Skills listed but never invoked",
            scope=project,
            summary=(
                f"{len(unused)} of {len(skills)} listed skills were never invoked here "
                f"({shown}); their listing is about {_k(tokens)} tokens in every request."
            ),
            advice=(
                "Keep a skill out of the listing where it is not used: add the line below "
                "to its SKILL.md frontmatter (it stays callable as a slash command), move "
                "it from ~/.claude/skills into the projects that use it, or disable the "
                "plugin that brings it."
            ),
            change="disable-model-invocation: true",
            evidence=tuple(
                Evidence(f"skill {n}: {_k(unused[n].size)} tokens, {_usd(unused[n].usd)}")
                for n in names[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "skills_listed": len(skills),
                "skills_invoked": len(skills) - len(unused),
                "tokens_unused": tokens,
            },
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- agents


def _agent_threads(facts: Facts) -> dict[str, list[Thread]]:
    out: dict[str, list[Thread]] = defaultdict(list)
    for t in facts.threads.values():
        if t.agent_id is not None and t.requests:
            out[facts.agent_type(t.agent_id) or "(unknown agent)"].append(t)
    return out


def agent_tools_unused(facts: Facts, definitions: Mapping[str, AgentDefinition]) -> list[Finding]:
    """A custom agent type whose spawns never call some of the tools it is given
    (its definition's `tools:` line, or the tools listed in its requests when the
    definition gives it every tool). Saving: the unused tools' listed sizes x the
    agent's requests x each request's prompt price; for tools no listing sized, the
    median tool size seen in request shapes."""
    out: list[Finding] = []
    for kind, threads in _agent_threads(facts).items():
        if kind in BUILT_IN_AGENTS or kind.startswith("(") or len(threads) < MIN_AGENTS:
            continue
        infos = [facts.agents.get(t.agent_id or "") for t in threads]
        if any(i is not None and i.is_built_in for i in infos):
            continue
        keys = {(t.session_id, t.agent_id) for t in threads}
        called: set[str] = set()
        for k in keys:
            called |= facts.calls_by_thread.get(k, set())
        sized: dict[str, _Sum] = {}
        for sid, aid, key, acc in _accounts(facts, lambda k: k[0] == "tool"):
            if (sid, aid) in keys:
                sized.setdefault(key[2], _Sum()).add(sid, acc)
        definition = definitions.get(kind)
        given = set(definition.tools) if definition and definition.tools else set(sized)
        unused = sorted(given - called)
        if not unused or not called:
            continue
        rate_sum = sum(t.rate_sum for t in threads)
        estimate = facts.tool_token_estimate()
        usd = sum(sized[n].usd if n in sized else estimate * rate_sum for n in unused)
        keep = sorted(called & given) if definition and definition.tools else sorted(called)
        path = str(definition.path) if definition else f".claude/agents/{kind}.md"
        f = _finding(
            facts,
            usd,
            kind="agent_tools_unused",
            title=f"The {kind} agent carries tools it never uses",
            scope=kind,
            summary=(
                f"Across {len(threads)} {kind} spawns it called {len(called & given)} of "
                f"{len(given)} tools it is given; the other {len(unused)} rode in "
                f"each of its {sum(t.requests for t in threads):,} requests."
            ),
            advice=f"In {path}, list only the tools it uses:",
            change=f"tools: {', '.join(keep)}",
            evidence=tuple(Evidence(f"never called: {n}") for n in unused[:EVIDENCE_PER_FINDING]),
            metrics={
                "spawns": len(threads),
                "tools_given": len(given),
                "tools_called": len(called & given),
                "requests": sum(t.requests for t in threads),
            },
        )
        if f:
            out.append(f)
    return out


def one_command_subagents(facts: Facts) -> list[Finding]:
    """Subagents that made at most two tool calls: the spawn writes a whole prompt
    prefix to the cache for work the parent could have done in a turn or two.
    Saving per subagent: its own cost minus max(calls, 1) x the average request cost
    of its session's main thread (what doing it in the parent would cost)."""
    parent_avg: dict[str, float] = {}
    for t in facts.threads.values():
        if t.agent_id is None and t.requests:
            parent_avg[t.session_id] = t.cost / t.requests
    out: list[Finding] = []
    for kind, threads in _agent_threads(facts).items():
        small = [t for t in threads if t.tool_calls <= SUBAGENT_MAX_CALLS]
        if len(small) < MIN_AGENTS:
            continue
        savings = [
            (max(0.0, t.cost - max(t.tool_calls, 1) * parent_avg.get(t.session_id, 0.0)), t)
            for t in small
        ]
        usd = sum(s for s, _ in savings)
        top = sorted(savings, key=lambda p: -p[0])[:EVIDENCE_PER_FINDING]
        f = _finding(
            facts,
            usd,
            kind="one_command_subagents",
            title="Subagents for one-command tasks",
            scope=kind,
            summary=(
                f"{len(small)} of {len(threads)} {kind} subagents made "
                f"{SUBAGENT_MAX_CALLS} tool calls or fewer; together they cost "
                f"{_usd(sum(t.cost for t in small))}, each writing its own prompt prefix."
            ),
            advice=(
                "Run single commands and single-file lookups in the parent; spawn a "
                "subagent only for work that needs several tool calls. A line for the "
                "main agent's instructions (CLAUDE.md or AGENTS.md):"
            ),
            change=(
                "Run single commands and single-file lookups yourself. Spawn a subagent "
                "only for work that needs several tool calls."
            ),
            evidence=tuple(
                Evidence(
                    f"{kind} {t.agent_id or ''}: {t.tool_calls} tool calls, "
                    f"{t.requests} requests, {_usd(t.cost)}",
                    session_id=t.session_id,
                    agent_id=t.agent_id,
                    timestamp=t.first,
                )
                for _, t in top
            ),
            metrics={
                "subagents": len(threads),
                "one_command": len(small),
                "cost_usd": round(sum(t.cost for t in small), 4),
            },
        )
        if f:
            out.append(f)
    return out


def long_subagent_reports(facts: Facts) -> list[Finding]:
    """Agent results above LONG_REPORT_TOKENS, reread by the parent on every later
    turn until a compaction. Saving: (report tokens - REPORT_TARGET_TOKENS) x the
    parent's later requests x each request's prompt price."""
    per_type: dict[str, list[tuple[str, str | None, ItemKey, Account]]] = defaultdict(list)
    for sid, aid, key, acc in _accounts(facts, lambda k: k[0] == "report"):
        per_type[facts.spawn_types.get(key[2], "(unknown agent)")].append((sid, aid, key, acc))
    out: list[Finding] = []
    for kind, reports in per_type.items():
        usd = sum(a.usd * max(0, a.size - REPORT_TARGET_TOKENS) / a.size for *_, a in reports)
        sizes = sorted(a.size for *_, a in reports)
        top = sorted(reports, key=lambda r: -r[3].usd)[:EVIDENCE_PER_FINDING]
        f = _finding(
            facts,
            usd,
            kind="long_subagent_reports",
            title="Long subagent reports",
            scope=kind,
            summary=(
                f"{len(reports)} {kind} report{'s' if len(reports) != 1 else ''} above "
                f"{_k(LONG_REPORT_TOKENS)} tokens (median {_k(sizes[len(sizes) // 2])}, "
                f"largest {_k(sizes[-1])}), each reread by the parent on every later turn."
            ),
            advice=(
                "Ask for a short report in the agent's definition or in the spawn prompt; "
                "the details can go to a file the parent reads only if it needs them:"
            ),
            change=(
                "End with a report of at most 25 lines; put details in a file and give its path."
            ),
            evidence=tuple(
                Evidence(
                    f"report of {_k(a.size)} tokens reread by {a.requests} later requests, "
                    f"{_usd(a.usd)}",
                    session_id=sid,
                    agent_id=aid,
                    event_key=key[1],
                )
                for sid, aid, key, a in top
            ),
            metrics={"reports": len(reports), "median_tokens": sizes[len(sizes) // 2]},
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- memory


def large_memory(facts: Facts) -> list[Finding]:
    """Always-loaded instructions and memory files (CLAUDE.md and the files it
    imports, nested memory) above MEMORY_BUDGET_TOKENS per file. Saving: the part
    above the budget x the requests that carried it x each request's prompt price."""
    per_project: dict[str, dict[str, _Sum]] = defaultdict(dict)
    for sid, _aid, key, acc in _accounts(facts, lambda k: k[0] == "memory"):
        per_project[_group(facts.project_of(sid))].setdefault(key[2], _Sum()).add(sid, acc)
    out: list[Finding] = []
    for project, labels in per_project.items():
        over: list[tuple[str, _Sum, int]] = []
        for label, s in labels.items():
            budget = MEMORY_BUDGET_TOKENS * max(1, len(label.split("|")))
            if s.size > budget:
                over.append((label, s, budget))
        if not over:
            continue
        usd = sum(s.usd * (s.size - b) / s.size for _, s, b in over)
        total = sum(s.size for s in labels.values())
        paths = sorted({p for label, _, _ in over for p in label.split("|")})
        f = _finding(
            facts,
            usd,
            kind="large_memory",
            title="Large always-loaded instructions",
            scope=project,
            summary=(
                f"About {_k(total)} tokens of instructions and memory ride in every "
                f"request here; {len(paths)} file(s) are above the "
                f"{_k(MEMORY_BUDGET_TOKENS)}-token (about 150-line) budget."
            ),
            advice=(
                "Move detail into docs/ files that are read when needed, and keep the "
                "always-loaded files short (about 150 lines each)."
            ),
            evidence=tuple(
                Evidence(
                    f"{label.replace('|', ', ')}: {_k(s.size)} tokens, "
                    f"{_k(s.requests)} requests, {_usd(s.usd)}"
                )
                for label, s, _ in sorted(over, key=lambda o: -o[1].usd)[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "tokens": total,
                "budget_tokens": MEMORY_BUDGET_TOKENS,
                "files_over": len(paths),
            },
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- context


def long_running_agents(facts: Facts) -> list[Finding]:
    """Threads whose prompts grew past LONG_CONTEXT. Saving: each such turn's
    (context - LONG_CONTEXT) x its prompt price; the part above LATE_COMPACTION
    before a late compaction is left to `late_compactions`."""
    out: list[Finding] = []
    grouped: dict[str, list[tuple[str, LongContext]]] = defaultdict(list)
    for project, lc in facts.long_context.items():
        grouped[_group(project)].append((project, lc))
    for project, items in grouped.items():
        lcs = [lc for _, lc in items]
        usd = sum(lc.excess_usd for lc in lcs)
        threads: set[tuple[str, str | None]] = set().union(*(lc.threads for lc in lcs))
        turns = sum(lc.turns for lc in lcs)
        biggest = max(lc.max_context for lc in lcs)
        evs = sorted(
            (e for lc in lcs for e in lc.evidence.values()),
            key=lambda p: -p[0],
        )
        subagents = sum(1 for _, aid in threads if aid is not None)
        f = _finding(
            facts,
            usd,
            kind="long_running_agents",
            title="Long-running agents",
            scope=project,
            summary=(
                f"{len(threads)} thread(s) ({subagents} subagents) ran {turns:,} turns past "
                f"{_k(LONG_CONTEXT)} tokens, up to {_k(biggest)}; every such turn rereads "
                "the whole context."
            ),
            advice=(
                "One task per agent: hand off with a short note and start fresh, or "
                f"compact by hand, when the context passes {_k(LONG_CONTEXT)}."
            ),
            evidence=tuple(e for _, e in evs[:EVIDENCE_PER_FINDING]),
            metrics={
                "threads": len(threads),
                "turns": turns,
                "max_context": biggest,
                "threshold": LONG_CONTEXT,
            },
        )
        if f:
            out.append(f)
    return out


def late_compactions(facts: Facts) -> list[Finding]:
    """Compactions that started above LATE_COMPACTION tokens. Saving: the turns
    before each one, (context - LATE_COMPACTION) x their prompt price."""
    per_project: dict[str, list[Compaction]] = defaultdict(list)
    for (sid, _aid), comps in facts.compactions.items():
        for c in comps:
            if c.pre_tokens > LATE_COMPACTION and c.timestamp >= facts.since:
                per_project[_group(facts.project_of(sid))].append(c)
    out: list[Finding] = []
    for project, found in per_project.items():
        comps = sorted(found, key=lambda c: -c.pre_tokens)
        usd = sum(c.excess_usd for c in comps)
        pres = [c.pre_tokens for c in comps]
        f = _finding(
            facts,
            usd,
            kind="late_compactions",
            title="Compactions that ran too late",
            scope=project,
            summary=(
                f"{len(comps)} compaction(s) started above {_k(LATE_COMPACTION)} tokens "
                f"({', '.join(_k(p) for p in pres[:5])}); the turns before them reread "
                "everything above that."
            ),
            advice=(
                f"Compact by hand when the context passes {_k(LATE_COMPACTION)}, or end the "
                "task with a handoff note and start a fresh session."
            ),
            evidence=tuple(
                Evidence(
                    f"compaction at {_k(c.pre_tokens)} tokens after {c.turns} turns above "
                    f"{_k(LATE_COMPACTION)}",
                    session_id=c.thread[0],
                    agent_id=c.thread[1],
                    timestamp=c.timestamp,
                    event_key=c.key,
                )
                for c in comps[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "compactions": len(comps),
                "max_pre_tokens": max(pres),
                "threshold": LATE_COMPACTION,
            },
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- cache


def idle_cache_rewrites(facts: Facts) -> list[Finding]:
    """Requests that rewrote at least half their prompt to the cache after their
    thread had been idle past the cache lifetime (5 minutes, or 1 hour when the
    request wrote with the 1-hour lifetime). Saving: each rewrite's cost above
    reading the same tokens from the cache."""
    return _rewrites(
        facts,
        facts.idle,
        kind="idle_cache_rewrites",
        title="Cache rewrites after idling",
        what="after a pause longer than the cache lifetime",
        advice=(
            "Group work so sessions don't sit idle past the cache lifetime between "
            "turns, and check the 1-hour cache lifetime is in use for long pauses."
        ),
    )


def model_switches(facts: Facts) -> list[Finding]:
    """Requests that rewrote at least half their prompt because the thread's model
    changed. Saving: each rewrite's cost above reading the same tokens."""
    return _rewrites(
        facts,
        facts.switches,
        kind="model_switches",
        title="Model switches mid-session",
        what="right after a model switch",
        advice=(
            "Switch models between sessions, not inside one: a new model starts with a cold cache."
        ),
    )


def _rewrites(
    facts: Facts, data: Mapping[str, Rewrite], kind: str, title: str, what: str, advice: str
) -> list[Finding]:
    grouped: dict[str, list[Rewrite]] = defaultdict(list)
    for project, rw in data.items():
        grouped[_group(project)].append(rw)
    out: list[Finding] = []
    for project, rws in grouped.items():
        count = sum(r.count for r in rws)
        usd = sum(r.excess_usd for r in rws)
        tokens = sum(r.tokens for r in rws)
        days: set[str] = set().union(*(r.days for r in rws))
        evs = sorted((e for r in rws for e in r.evidence), key=lambda p: -p[0])
        f = _finding(
            facts,
            usd,
            kind=kind,
            title=title,
            scope=project,
            summary=(
                f"{count} request(s) on {len(days)} day(s) rewrote {_k(tokens)} tokens to the "
                f"cache {what}, {_usd(usd)} above reading them."
            ),
            advice=advice,
            evidence=tuple(e for _, e in evs[:EVIDENCE_PER_FINDING]),
            metrics={"rewrites": count, "days": len(days), "tokens": tokens},
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- images


def images_in_tool_results(facts: Facts) -> list[Finding]:
    """Images and screenshots in tool results, reread by every later turn until a
    compaction. Saving: IMAGE_SAVING_SHARE of what they cost (size x later requests x
    prompt price), for images cropped and downscaled to half the tokens."""
    per_project: dict[str, list[tuple[str, ItemKey, Account]]] = defaultdict(list)
    for sid, _aid, key, acc in _accounts(facts, lambda k: k[0] == "image"):
        per_project[_group(facts.project_of(sid))].append((sid, key, acc))
    out: list[Finding] = []
    for project, images in per_project.items():
        usd = sum(a.usd for *_, a in images) * IMAGE_SAVING_SHARE
        tokens = sum(a.size for *_, a in images)
        by_tool: dict[str, int] = defaultdict(int)
        for _, key, _a in images:
            by_tool[key[2]] += 1
        tools = ", ".join(f"{t} ({n})" for t, n in sorted(by_tool.items(), key=lambda kv: -kv[1]))
        top = sorted(images, key=lambda i: -i[2].usd)[:EVIDENCE_PER_FINDING]
        f = _finding(
            facts,
            usd,
            kind="images_in_tool_results",
            title="Screenshots and images in tool results",
            scope=project,
            summary=(
                f"{len(images)} image(s) from {tools}, about {_k(tokens)} tokens, each "
                "reread by every later turn until a compaction."
            ),
            advice=(
                "Crop and downscale screenshots before reading them, and take them at "
                "checkpoints only."
            ),
            evidence=tuple(
                Evidence(
                    f"{key[2]} image of {_k(a.size)} tokens, reread by {a.requests} requests",
                    session_id=sid,
                    event_key=key[1],
                )
                for sid, key, a in top
            ),
            metrics={"images": len(images), "tokens": tokens},
        )
        if f:
            out.append(f)
    return out


def all_findings(facts: Facts, definitions: Mapping[str, AgentDefinition]) -> list[Finding]:
    found: list[Finding] = []
    for check in (
        claudeai_connectors_unused,
        tools_unused,
        skills_unused,
        large_memory,
        one_command_subagents,
        long_subagent_reports,
        long_running_agents,
        late_compactions,
        idle_cache_rewrites,
        model_switches,
        images_in_tool_results,
    ):
        found.extend(check(facts))
    found.extend(agent_tools_unused(facts, definitions))
    found.sort(key=lambda f: (-f.saving_usd_per_week, f.kind, f.scope))
    return found
