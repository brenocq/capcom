"""Findings rolled up by kind across their scopes: "Long-running agents: up to
$X/week across 9 projects".

`run_checks` returns one finding per kind and scope (a project, or an agent type);
a roll-up adds up one kind's scopes. Within a kind the scopes count different
dollars (each check splits its requests by project or by agent type), so their sum
counts nothing twice; a scope of "all" covers the others, and the roll-up then
takes the larger of it and their sum. Kinds may still overlap one another
(`metrics["overlaps"]`): roll-ups of different kinds are not to be added up. A
roll-up's `overlaps` are the kinds that kept one of its findings out of the total
when the caller says which (capcom.report.tally.overlap_blockers), else every kind
its findings declare.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from capcom.types import Finding

# A kind's name across scopes (several kinds title each finding by its scope).
KIND_TITLES = {
    "claudeai_connectors_unused": "claude.ai connectors in sessions that never use them",
    "mcp_servers_unused": "MCP servers loaded but never called",
    "skills_unused": "Skills listed but never invoked",
    "large_memory": "Large always-loaded instructions",
    "agent_tools_unused": "Agents carrying tools they never use",
    "one_command_subagents": "Subagents for one-command tasks",
    "long_subagent_reports": "Long subagent reports",
    "long_running_agents": "Long-running agents",
    "late_compactions": "Compactions that ran too late",
    "idle_cache_rewrites": "Cache rewrites after idling",
    "subagent_resumed_late": "Subagents resumed after their cache expired",
    "subagent_cache_expiry": "Cache lifetime and long waits",
    "model_switches": "Model switches mid-session",
    "images_in_tool_results": "Screenshots and images in tool results",
    "session_never_ended": "Sessions that never ended",
    "heartbeat_growing_session": "Heartbeats resuming one growing session",
    "model_mix": "Subagents on Opus that could run on Sonnet",
}
# Kinds scoped by agent type; the others by project.
AGENT_KINDS = frozenset(
    {"agent_tools_unused", "one_command_subagents", "long_subagent_reports", "model_mix"}
)


@dataclass(frozen=True, slots=True)
class ScopeShare:
    scope: str
    saving_usd_per_week: float
    share: float  # of the roll-up's saving, 0 to 1


@dataclass(frozen=True, slots=True)
class Rollup:
    """One kind of finding across its scopes, largest scope first."""

    kind: str
    title: str
    saving_usd_per_week: float
    scopes: tuple[ScopeShare, ...]
    scope_noun: str  # "project" or "agent type"
    overlaps: tuple[str, ...] = ()  # kinds whose dollars it may share

    @property
    def headline(self) -> str:
        """'Long-running agents: up to $160.87/week across 9 projects'."""
        n = len(self.scopes)
        noun = self.scope_noun if n == 1 else self.scope_noun + "s"
        where = f"{n} {noun}"
        if n == 1 and self.scopes[0].scope == "all":
            where = f"all {self.scope_noun}s"
        return f"{self.title}: up to ${self.saving_usd_per_week:,.2f}/week across {where}"


def _overlaps(f: Finding) -> tuple[str, ...]:
    raw = f.metrics.get("overlaps")
    if isinstance(raw, str):
        return (raw,)
    return tuple(raw) if isinstance(raw, tuple | list) else ()


def rollup(
    findings: Iterable[Finding], left_out_by: Sequence[Iterable[str]] | None = None
) -> list[Rollup]:
    """The findings grouped by kind, each with its total weekly saving and its
    scopes' shares; largest first. `left_out_by`, aligned with `findings`, names
    for each the kinds of the larger findings that kept it out of the total: the
    roll-up's `overlaps` are then those kinds only."""
    by_kind: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    titles: dict[str, str] = {}
    overlaps: dict[str, set[str]] = defaultdict(set)
    for i, f in enumerate(findings):
        by_kind[f.kind][f.scope] += f.saving_usd_per_week
        titles.setdefault(f.kind, KIND_TITLES.get(f.kind, f.title))
        overlaps[f.kind].update(_overlaps(f) if left_out_by is None else left_out_by[i])
    out: list[Rollup] = []
    for kind, scopes in by_kind.items():
        whole = scopes.get("all", 0.0)
        total = max(whole, sum(usd for scope, usd in scopes.items() if scope != "all"))
        ranked = sorted(scopes.items(), key=lambda kv: (-kv[1], kv[0]))
        shares = tuple(
            ScopeShare(scope, usd, usd / total if total > 0 else 0.0) for scope, usd in ranked
        )
        out.append(
            Rollup(
                kind=kind,
                title=titles[kind],
                saving_usd_per_week=total,
                scopes=shares,
                scope_noun="agent type" if kind in AGENT_KINDS else "project",
                overlaps=tuple(sorted(overlaps[kind])),
            )
        )
    out.sort(key=lambda r: (-r.saving_usd_per_week, r.kind))
    return out
