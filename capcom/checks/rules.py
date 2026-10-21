"""The setup checks (docs/design.md, "Setup checks"), each a function of the facts.

Every saving is a weekly figure at the window's pace:

    saving_usd_per_week = usd_in_window / weeks_in_window

and the usd of an item carried in prompts is, per request that carried it, its
size in tokens times that request's price per prompt token (facts.py), which is
"size x requests x the right price" with cache reads and rewrites priced as they
were billed. Each check's docstring says what it counts.

The context and cache findings are disjoint within a project: long contexts and
late compactions price the tokens past their threshold at the cache-read price
(what rereading them cost, split between the two at LATE_COMPACTION), while the
rewrite findings (idle, tool waits, model switches; each request in at most one)
count only a rewrite's premium above that read. Items carried in prompts are
priced at the full prompt price, so they overlap those findings: `overlaps()` names
the kinds a finding may share dollars with, and such findings are not to be added up.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

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
# spawns before offering a tools: line to an agent that inherits every tool: the
# tools it called must be a broad sample before a line removes all the others
MIN_RESTRICT_SPAWNS = 10
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
    """Accounts of several items or threads added up. A request that carried several
    of the items counts once: `requests` sums, per thread, the largest count."""

    usd: float = 0.0
    size: int = 0
    sessions: set[str] = field(default_factory=set)
    per_thread: dict[tuple[str, str | None], int] = field(default_factory=dict)

    def add(self, sid: str, aid: str | None, acc: Account) -> None:
        self.usd += acc.usd
        self.size = max(self.size, acc.size)
        self.sessions.add(sid)
        key = (sid, aid)
        self.per_thread[key] = max(self.per_thread.get(key, 0), acc.requests)

    @property
    def requests(self) -> int:
        return sum(self.per_thread.values())


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
    for sid, aid, key, acc in _accounts(facts, _is_connector):
        project = _group(facts.project_of(sid))
        listed[project].setdefault(sid, _Sum()).add(sid, aid, acc)
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
        tokens = _connector_tokens(facts, unused)
        names = sorted(n.removeprefix("claude_ai_").replace("_", " ") for n in servers[project])
        top = sorted(unused.items(), key=lambda kv: -kv[1].usd)[:EVIDENCE_PER_FINDING]
        summary = (
            f"{len(names)} claude.ai connector{'s' if len(names) != 1 else ''} "
            f"({', '.join(names)}) rode in {_k(reqs)} requests of {len(unused)} "
            f"session{'s' if len(unused) != 1 else ''} that never called them, "
            f"about {_k(tokens)} tokens in each prompt."
        )
        local = _where(facts, project, ".claude/settings.local.json")
        change: str | None = None
        if using:
            summary += f" {len(using)} other session(s) in this project did call them."
            shown = ", ".join(sorted(sid[:8] for sid in using)[:EVIDENCE_PER_FINDING])
            more = ", ..." if len(using) > EVIDENCE_PER_FINDING else ""
            advice = (
                f"Sessions {shown}{more} of this project call the claude.ai connectors, "
                "so turning them off here would break that work. Start the sessions that "
                "do not need them with ENABLE_CLAUDEAI_MCP_SERVERS=false in the "
                "environment, or move the connector work to its own project."
            )
        else:
            advice = (
                "Turn the claude.ai connectors off for this project; they stay "
                "available in claude.ai and in the projects that use them. "
                f"In {local} (yours only, not shared with the repo):"
            )
            change = _settings_change({"env": {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}})
        f = _finding(
            facts,
            usd,
            kind="claudeai_connectors_unused",
            title="claude.ai connectors in sessions that never use them",
            scope=project,
            summary=summary,
            advice=advice,
            change=change,
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


def _connector_tokens(facts: Facts, sessions: Mapping[str, _Sum]) -> int:
    """Connector tokens per request in `sessions`, averaged over their requests."""
    token_requests = sum(
        acc.size * acc.requests
        for sid, _aid, _key, acc in _accounts(facts, _is_connector)
        if sid in sessions
    )
    reqs = sum(s.requests for s in sessions.values())
    return round(token_requests / reqs) if reqs else 0


# --------------------------------------------------------------------------- MCP servers


def tools_unused(facts: Facts) -> list[Finding]:
    """MCP servers (other than claude.ai connectors) whose tools ride in a
    project's prompts while none of them is called there. Saving: the server's
    listed tools and instructions x the requests that carried them x each request's
    prompt price. Built-in tools are counted in the metrics only: a main thread
    cannot drop them."""
    loaded: dict[str, set[str]] = defaultdict(set)
    per_server: dict[str, dict[str, _Sum]] = defaultdict(dict)
    for sid, aid, key, acc in _accounts(facts, lambda k: k[0] in ("tool", "mcp")):
        project = _group(facts.project_of(sid))
        if key[0] == "tool":
            loaded[project].add(key[2])
        server = _server(key)
        if server is None or server.startswith("claude_ai_"):
            continue
        per_server[project].setdefault(server, _Sum()).add(sid, aid, acc)
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
    listing x the requests that carried it x each request's prompt price.

    The change (`disable-model-invocation: true` in SKILL.md) applies wherever the
    skill is listed, so it is offered only for skills no project invoked in the
    window; a skill invoked elsewhere is advised to move to those projects instead.
    A plugin's skill (`<plugin>:<skill>`) never gets the SKILL.md change, since a
    plugin update overwrites the file: the advice is to disable the plugin here."""
    listed: dict[str, dict[str, _Sum]] = defaultdict(dict)
    for sid, aid, key, acc in _accounts(facts, lambda k: k[0] == "skill"):
        listed[_group(facts.project_of(sid))].setdefault(key[1], _Sum()).add(sid, aid, acc)
    invoked: dict[str, set[str]] = defaultdict(set)
    for sid, session_skills in facts.skills_by_session.items():
        invoked[_group(facts.project_of(sid))].update(session_skills)
    out: list[Finding] = []
    for project, skills in listed.items():
        unused = {n: s for n, s in skills.items() if not _skill_used(n, invoked[project])}
        if not unused:
            continue
        elsewhere = {
            n: sorted(p for p, used in invoked.items() if p != project and _skill_used(n, used))
            for n in unused
        }
        nowhere = sorted(n for n in unused if not elsewhere[n] and ":" not in n)
        plugin_skills = sorted(n for n in unused if not elsewhere[n] and ":" in n)
        moved = sorted(n for n in unused if elsewhere[n])
        usd = sum(s.usd for s in unused.values())
        tokens = sum(s.size for s in unused.values())
        names = sorted(unused, key=lambda n: -unused[n].usd)
        shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
        parts: list[str] = []
        if moved:
            where = sorted({p for n in moved for p in elsewhere[n]})
            parts.append(
                f"{_list_names(moved, 4)} {'is' if len(moved) == 1 else 'are'} used in other "
                f"projects ({_list_names(where, 3)}): move {'it' if len(moved) == 1 else 'them'} "
                "from ~/.claude/skills into those projects' .claude/skills (for a plugin's "
                "skill, enable the plugin only there) instead of disabling it."
            )
        if nowhere:
            parts.append(
                f"No project invoked {_list_names(nowhere, 4)}: add the line below to "
                f"{'its' if len(nowhere) == 1 else 'each'} SKILL.md frontmatter to keep it out "
                "of the listing everywhere (it stays callable as a slash command)."
            )
        if plugin_skills:
            plugins = sorted({n.split(":", 1)[0] for n in plugin_skills})
            one = len(plugins) == 1
            parts.append(
                f"{_list_names(plugin_skills, 4)} come{'s' if len(plugin_skills) == 1 else ''} "
                f"from the plugin{'' if one else 's'} {_list_names(plugins, 4)}, whose "
                "SKILL.md files a plugin update overwrites: if this project does not need "
                f"{'it' if one else 'them'}, disable {'it' if one else 'them'} here "
                "(`/plugin`, or `enabledPlugins` in the project's .claude/settings.json)."
            )
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
            advice=" ".join(parts),
            change="disable-model-invocation: true" if nowhere else None,
            evidence=tuple(
                Evidence(
                    f"skill {n}: {_k(unused[n].size)} tokens, {_usd(unused[n].usd)}"
                    + (f"; used in {_list_names(elsewhere[n], 2)}" if elsewhere[n] else "")
                )
                for n in names[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "skills_listed": len(skills),
                "skills_invoked": len(skills) - len(unused),
                "skills_used_elsewhere": len(moved),
                "plugin_skills": len(plugin_skills),
                "tokens_unused": tokens,
            },
        )
        if f:
            out.append(f)
    return out


def _list_names(names: list[str], limit: int = 6) -> str:
    return ", ".join(names[:limit]) + (", ..." if len(names) > limit else "")


# --------------------------------------------------------------------------- agents


def _agent_threads(facts: Facts) -> dict[str, list[Thread]]:
    out: dict[str, list[Thread]] = defaultdict(list)
    for t in facts.threads.values():
        if t.agent_id is not None and t.requests:
            out[facts.agent_type(t.agent_id) or "(unknown agent)"].append(t)
    return out


_PLAIN_TOOL = re.compile(r"^[A-Za-z0-9_-]+$")


def _canon(tool: str) -> str:
    """Task is the Agent tool's older name."""
    return "Agent" if tool == "Task" else tool


def _entry_matches(entry: str, tool: str) -> bool:
    """Whether a `tools:` entry covers a tool name: the same tool (Task and Agent
    alike), or a whole MCP server (`mcp__<server>`) covering its tools."""
    e, t = _canon(entry), _canon(tool)
    if e == t:
        return True
    return e.startswith("mcp__") and len(e.split("__")) == 2 and t.startswith(e + "__")


def _entry_used(entry: str, called: set[str]) -> bool | None:
    """True or False for a plain entry; None for one this check cannot read (a
    permission pattern such as `Bash(git *)`, a wildcard), which is always kept."""
    if not _PLAIN_TOOL.match(entry):
        return None
    return any(_entry_matches(entry, c) for c in called)


def _encoded(path: Path) -> str:
    """A folder as Claude Code names its project folder: every other character a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _is_openclaw(facts: Facts, project: str, root: Path | None) -> bool:
    """Whether `project` is an OpenClaw workspace (its sessions run under the OpenClaw
    folder, whose tools are not Claude Code's)."""
    if root is None:
        return False
    enc = _encoded(root)
    if project == enc or project.startswith(enc + "-"):
        return True
    return any(Path(c).is_relative_to(root) for c in facts.project_cwd.get(project, {}))


def _definition_projects(
    facts: Facts, definition: AgentDefinition, user_dir: Path | None
) -> set[str] | None:
    """The projects a project-level definition (`<cwd>/.claude/agents/x.md`) applies
    to; None for a user-level one (in `user_dir` or ~/.claude/agents, which a
    project whose cwd is the home folder would otherwise claim)."""
    folder = definition.path.parent
    if folder.name != "agents" or folder.parent.name != ".claude":
        return None
    if folder in {user_dir, Path.home() / ".claude" / "agents"}:
        return None
    cwd = folder.parent.parent
    found = {p for p, cwds in facts.project_cwd.items() if any(Path(c) == cwd for c in cwds)}
    return found or None


def agent_tools_unused(
    facts: Facts,
    definitions: Mapping[str, AgentDefinition],
    openclaw_root: Path | None = None,
    user_dir: Path | None = None,
) -> list[Finding]:
    """A custom agent type whose spawns never call some of the tools its definition
    gives it (its `tools:` line, or the tools listed in its requests when the line
    gives it every tool). Saving: the unused tools' listed sizes x the agent's
    requests x each request's prompt price; for tools no listing sized, the median
    tool size seen in request shapes.

    Only the spawns the definition governs in Claude Code projects count: a
    project-level definition's own project, or (user level) every project but the
    OpenClaw workspaces under `openclaw_root`, whose tools are OpenClaw's.

    An agent type with no definition file (a plugin's agent, or one Claude Code
    makes itself, such as its workflow agent) is skipped: there is no file to change.
    With a `tools:` line, the pasted line keeps every entry used anywhere or not
    understood, in the file's order. Without one the agent inherits every tool, and
    a line would restrict it: the line then holds only tools it called that its own
    listings name (never a name from elsewhere), is offered only after
    MIN_RESTRICT_SPAWNS spawns, and says in a comment how many tools it removes."""
    out: list[Finding] = []
    for kind, all_threads in _agent_threads(facts).items():
        if kind in BUILT_IN_AGENTS or kind.startswith("(") or len(all_threads) < MIN_AGENTS:
            continue
        definition = definitions.get(kind)
        if definition is None:
            continue
        infos = [facts.agents.get(t.agent_id or "") for t in all_threads]
        if any(i is not None and i.is_built_in for i in infos):
            continue
        scope = _definition_projects(facts, definition, user_dir)
        threads = [
            t
            for t in all_threads
            if (
                t.project in scope
                if scope is not None
                else not _is_openclaw(facts, t.project, openclaw_root)
            )
        ]
        if len(threads) < MIN_AGENTS:
            continue
        keys = {(t.session_id, t.agent_id) for t in threads}
        called: set[str] = set()
        for k in keys:
            called |= facts.calls_by_thread.get(k, set())
        called_anywhere: set[str] = set()
        for t in all_threads:
            called_anywhere |= facts.calls_by_thread.get((t.session_id, t.agent_id), set())
        if not called:
            continue
        sized: dict[str, _Sum] = {}
        for sid, aid, key, acc in _accounts(facts, lambda k: k[0] == "tool"):
            if (sid, aid) in keys:
                sized.setdefault(key[2], _Sum()).add(sid, aid, acc)
        spawns = len(threads)
        requests = sum(t.requests for t in threads)
        restricts = not definition.tools
        dropped: list[str] = []
        if definition.tools:
            given = list(dict.fromkeys(definition.tools))
            used = {e: _entry_used(e, called_anywhere) for e in given}
            unused = [e for e in given if used[e] is False]
            keep = [e for e in given if used[e] is not False]
        else:
            given = sorted(sized)
            unused = [n for n in given if not any(_entry_matches(n, c) for c in called)]
            keep = sorted(n for n in given if any(_entry_matches(n, c) for c in called))
            dropped = sorted(
                c for c in called_anywhere if not any(_entry_matches(n, c) for n in keep)
            )
        if not unused or not keep:
            continue
        n_used = len(given) - len(unused)
        rate_sum = sum(t.rate_sum for t in threads)
        estimate = facts.tool_token_estimate()
        usd = 0.0
        for e in unused:
            priced = [v.usd for n, v in sized.items() if _entry_matches(e, n)]
            usd += sum(priced) if priced else estimate * rate_sum
        line = f"tools: {', '.join(keep)}"
        if not restricts:
            advice = f"In {definition.path}, list only the tools it uses:"
            change: str | None = line
        else:
            advice = (
                f"{definition.path} has no tools: line, so {kind} inherits every tool "
                f"({len(given)} in its listings). Adding one restricts it to the {len(keep)} "
                "it called: a tool a later task needs would then be missing until added "
                "back."
            )
            if dropped:
                advice += (
                    f" Left out: {_list_names(dropped, 4)}, called only in OpenClaw or "
                    "other projects' sessions or missing from its listings."
                )
            if spawns >= MIN_RESTRICT_SPAWNS:
                advice += " The line, if you accept that:"
                change = (
                    f"# adds a tools: line: the {kind} agent then has only these "
                    f"{len(keep)} tools; today it inherits all {len(given)}\n{line}"
                )
            else:
                advice += (
                    f" With {spawns} spawns that set is a thin sample, so no line is offered yet."
                )
                change = None
        f = _finding(
            facts,
            usd,
            kind="agent_tools_unused",
            title=f"The {kind} agent carries tools it never uses",
            scope=kind,
            summary=(
                f"Across {spawns} {kind} spawns it used {n_used} of the "
                f"{len(given)} tools it is given; the other {len(unused)} rode in "
                f"each of its {requests:,} requests."
            ),
            advice=advice,
            change=change,
            evidence=tuple(Evidence(f"never called: {n}") for n in unused[:EVIDENCE_PER_FINDING]),
            metrics={
                "spawns": spawns,
                "tools_given": len(given),
                "tools_called": n_used,
                "requests": requests,
                "restricts": restricts,
                "tools_left_out": len(dropped),
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


_WORKTREES = "/.worktrees/"
# Branch name prefixes that hold a slash (`feat/x` lands in .worktrees/feat/x/).
_BRANCH_PREFIXES = frozenset(
    {
        "feat",
        "fix",
        "chore",
        "docs",
        "doc",
        "refactor",
        "test",
        "tests",
        "perf",
        "build",
        "ci",
        "style",
        "release",
        "hotfix",
        "bugfix",
        "feature",
        "exp",
        "wip",
        "dev",
    }
)


def _strip_worktree(path: str) -> str:
    """`<repo>/.worktrees/<branch>/<rel>` as `<repo>/<rel>`: a worktree's copy of a
    file is the same file. The branch ends at the first folder holding a `.git`
    entry, or else after one folder (two after a prefix such as `feat/`)."""
    i = path.find(_WORKTREES)
    if i < 0:
        return path
    root, rest = path[:i], path[i + len(_WORKTREES) :]
    parts = rest.split("/")
    if len(parts) < 2:
        return path
    cut = 0
    for n in range(1, len(parts)):
        try:
            if (Path(root, ".worktrees", *parts[:n]) / ".git").exists():
                cut = n
                break
        except OSError:
            break
    if not cut:
        cut = 2 if parts[0] in _BRANCH_PREFIXES and len(parts) > 2 else 1
    return "/".join([root, *parts[cut:]])


def _repo_path(path: str, root: str | None) -> str:
    """A memory file's path, relative to the repo root when it is inside it, with
    worktree copies folded into the main checkout's path."""
    full = _strip_worktree(path)
    if root and full.startswith(root + "/"):
        return full[len(root) + 1 :]
    return full


def large_memory(facts: Facts) -> list[Finding]:
    """Always-loaded instructions and memory files (CLAUDE.md and the files it
    imports, nested memory) above MEMORY_BUDGET_TOKENS per file. Saving: the part
    above the budget x the requests that carried it x each request's prompt price.

    Paths are shown relative to the repo root, and a worktree's copy of a file counts
    as the file itself; one instructions attachment counts once, however many files
    it holds."""
    per_project: dict[str, dict[str, _Sum]] = defaultdict(dict)
    carried: dict[str, _Sum] = defaultdict(_Sum)  # every memory item, per project
    token_requests: dict[str, int] = defaultdict(int)
    for sid, aid, key, acc in _accounts(facts, lambda k: k[0] == "memory"):
        project = _group(facts.project_of(sid))
        per_project[project].setdefault(key[2], _Sum()).add(sid, aid, acc)
        carried[project].add(sid, aid, acc)
        token_requests[project] += acc.size * acc.requests
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
        reqs = carried[project].requests
        total = round(token_requests[project] / reqs) if reqs else 0
        cwd = facts.cwd_of(project)
        root = _strip_worktree(cwd) if cwd else None
        shown: dict[str, list[tuple[_Sum, float]]] = defaultdict(list)
        for label, s, b in over:
            paths = dict.fromkeys(_repo_path(p, root) for p in label.split("|"))
            shown[", ".join(paths)].append((s, s.usd * (s.size - b) / s.size))
        ranked = sorted(shown.items(), key=lambda kv: -sum(u for _, u in kv[1]))
        n = len(shown)
        f = _finding(
            facts,
            usd,
            kind="large_memory",
            title="Large always-loaded instructions",
            scope=project,
            summary=(
                f"Instructions and memory average about {_k(total)} tokens in each of "
                f"{_k(reqs)} requests here; {n} always-loaded attachment{'s' if n != 1 else ''} "
                f"{'are' if n != 1 else 'is'} above the {_k(MEMORY_BUDGET_TOKENS)}-token "
                "(about 150-line) budget per file."
            ),
            advice=(
                "Move detail into docs/ files that are read when needed, and keep the "
                "always-loaded files short (about 150 lines each)."
            ),
            evidence=tuple(
                Evidence(
                    f"{label}: {_k(max(s.size for s, _ in items))} tokens, "
                    f"{_k(sum(s.requests for s, _ in items))} requests, "
                    f"{_usd(sum(s.usd for s, _ in items))}"
                )
                for label, items in ranked[:EVIDENCE_PER_FINDING]
            ),
            metrics={
                "tokens": total,
                "budget_tokens": MEMORY_BUDGET_TOKENS,
                "files_over": n,
            },
        )
        if f:
            out.append(f)
    return out


# --------------------------------------------------------------------------- context


def long_running_agents(facts: Facts) -> list[Finding]:
    """Threads whose prompts grew past LONG_CONTEXT. Saving: each such turn's
    (context - LONG_CONTEXT) x its model's cache-read price, what rereading the
    tokens past the useful size cost (a rewrite's premium on them is left to the
    rewrite findings); the part above LATE_COMPACTION before a late compaction is
    left to `late_compactions`."""
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
    before each one, (context - LATE_COMPACTION) x their cache-read price."""
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


def subagent_cache_expiry(facts: Facts) -> list[Finding]:
    """Subagent requests that rewrote at least half their prompt because a tool call
    ran past the cache lifetime (5 minutes for subagents): the last result of the
    previous request's calls came back after the lifetime, and the rewrite followed
    it within a minute. Saving: each rewrite's cost above reading the same tokens.
    These rewrites are left out of `idle_cache_rewrites`. No setting is known to
    change a subagent's lifetime, so there is no change to paste."""
    out = _rewrites(
        facts,
        facts.tool_waits,
        kind="subagent_cache_expiry",
        title="Subagent caches expired during long tool calls",
        what="after a tool call ran past the cache lifetime",
        advice=(
            "Run long commands (builds, test suites, wait loops) in the background and "
            "poll their output in short steps, or split the work so a subagent doesn't "
            "wait on a long command: a subagent's cache lasts 5 minutes."
        ),
    )
    grouped: dict[str, list[Rewrite]] = defaultdict(list)
    for project, rw in facts.tool_waits.items():
        grouped[_group(project)].append(rw)
    for i, f in enumerate(out):
        rws = grouped[f.scope]
        causes: dict[str, int] = defaultdict(int)
        for rw in rws:
            for name, n in rw.causes.items():
                causes[name] += n
        top = sorted(causes.items(), key=lambda kv: (-kv[1], kv[0]))
        longest = max(rw.longest for rw in rws).total_seconds() / 60
        out[i] = replace(
            f,
            summary=(
                f"{f.summary} The calls waited on: "
                f"{', '.join(f'{n} ({c})' for n, c in top[:4])}; the longest ran "
                f"{longest:.0f} min."
            ),
            metrics={
                **f.metrics,
                "tools": tuple(n for n, _ in top),
                "longest_wait_minutes": round(longest, 1),
            },
        )
    return out


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
    per_project: dict[str, list[tuple[str, str | None, ItemKey, Account]]] = defaultdict(list)
    for sid, aid, key, acc in _accounts(facts, lambda k: k[0] == "image"):
        per_project[_group(facts.project_of(sid))].append((sid, aid, key, acc))
    out: list[Finding] = []
    for project, images in per_project.items():
        usd = sum(a.usd for *_, a in images) * IMAGE_SAVING_SHARE
        tokens = sum(a.size for *_, a in images)
        by_tool: dict[str, int] = defaultdict(int)
        for _sid, _aid, key, _acc in images:
            by_tool[key[2]] += 1
        tools = ", ".join(f"{t} ({n})" for t, n in sorted(by_tool.items(), key=lambda kv: -kv[1]))
        top = sorted(images, key=lambda i: -i[3].usd)[:EVIDENCE_PER_FINDING]
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
                    agent_id=aid,
                    event_key=key[1],
                )
                for sid, aid, key, a in top
            ),
            metrics={"images": len(images), "tokens": tokens},
        )
        if f:
            out.append(f)
    return out


def all_findings(
    facts: Facts,
    definitions: Mapping[str, AgentDefinition],
    openclaw_root: Path | None = None,
    user_dir: Path | None = None,
) -> list[Finding]:
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
        subagent_cache_expiry,
        model_switches,
        images_in_tool_results,
    ):
        found.extend(check(facts))
    found.extend(agent_tools_unused(facts, definitions, openclaw_root, user_dir))
    found.sort(key=lambda f: (-f.saving_usd_per_week, f.kind, f.scope))
    present = {f.kind for f in found}
    return [replace(f, metrics={**f.metrics, "overlaps": overlaps(f.kind, present)}) for f in found]


# Items carried in prompts: their cost includes the cache rewrites and the long
# contexts they are part of, so those findings may count the same dollars. The
# context and rewrite kinds among themselves are disjoint (module docstring).
_PROMPT_ITEMS = (
    "claudeai_connectors_unused",
    "mcp_servers_unused",
    "skills_unused",
    "large_memory",
    "agent_tools_unused",
    "long_subagent_reports",
    "images_in_tool_results",
)
_CONTEXT = (
    "long_running_agents",
    "late_compactions",
    "idle_cache_rewrites",
    "subagent_cache_expiry",
    "model_switches",
)
_OVERLAP_PAIRS: frozenset[frozenset[str]] = frozenset(
    {frozenset({a, b}) for a in _PROMPT_ITEMS for b in _CONTEXT}
    | {frozenset({"agent_tools_unused", b}) for b in _PROMPT_ITEMS[:3]}
    | {frozenset({"one_command_subagents", b}) for b in (*_PROMPT_ITEMS, *_CONTEXT)}
)


def overlaps(kind: str, present: Iterable[str] | None = None) -> tuple[str, ...]:
    """The finding kinds whose savings may count some of the same dollars as
    `kind`'s (limited to `present` when given): such savings are each an "up to"
    and are not to be added up."""
    others = {k for pair in _OVERLAP_PAIRS if kind in pair for k in pair if k != kind}
    if present is not None:
        others &= set(present)
    return tuple(sorted(others))
