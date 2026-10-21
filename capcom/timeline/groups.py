"""Which chart series a request's spend belongs to (capcom.types.GroupBy).

Shared by the timeline series and the report, so a project, model or agent gets the
same key and label everywhere:

- CATEGORY: input, cache_read, cache_write, output, web (from the CostBreakdown);
- PROJECT: the project folder, with every folder under /tmp grouped as "temporary"
  and every folder under the OpenClaw state dir grouped as "openclaw";
- MODEL: the canonical price-table id (`<synthetic>` placeholders are left out);
- AGENT: "main" for main threads, else the subagent's agent type: from its
  meta.json, else from the parent's Agent call it names (its SPAWN event), else
  "unknown";
- SESSION: the session id, labeled with its title or the id's first 8 characters.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from capcom.config import load_config
from capcom.cost.prices import canonical, is_placeholder
from capcom.types import CostBreakdown, EventKind, GroupBy, Index, PriceTable, Request

TEMPORARY = "temporary"
OPENCLAW = "openclaw"
MAIN_THREAD = "main"
UNKNOWN_AGENT = "unknown"

# (key, label) of the category members, in the order a stacked chart draws them
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("input", "Input"),
    ("cache_read", "Cache read"),
    ("cache_write", "Cache write"),
    ("output", "Output"),
    ("web", "Web"),
)

_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


def folder_name(path: str | Path) -> str:
    """Claude Code's projects/ folder name for a working directory: every character
    that is not a letter or a digit becomes '-'."""
    return _NOT_ALNUM.sub("-", str(path))


def _under(project_dir: str, prefix: str) -> bool:
    return project_dir == prefix or project_dir.startswith(prefix + "-")


def is_temporary(project_dir: str) -> bool:
    """A folder under /tmp (scratch sessions, forked or copied sessions)."""
    return _under(project_dir, "-tmp")


@dataclass(frozen=True, slots=True)
class Grouper:
    """Group keys and labels for requests, with the lookups each grouping needs.

    `titles` maps session ids to titles, `agent_types` maps (session_id, agent_id) to
    agent types, `openclaw_dir` is the OpenClaw state dir in folder form."""

    table: PriceTable
    titles: Mapping[str, str]
    agent_types: Mapping[tuple[str, str], str]
    openclaw_dir: str

    @classmethod
    def from_index(
        cls, index: Index, table: PriceTable, openclaw_root: Path | None = None
    ) -> Grouper:
        if openclaw_root is None:
            openclaw_root = load_config().openclaw_root
        titles = {s.session_id: s.title for s in index.sessions() if s.title}
        agent_types: dict[tuple[str, str], str] = {}
        untyped: dict[tuple[str, str], tuple[str, str]] = {}  # tool_use_id -> agent
        for a in index.agents():
            if a.agent_type:
                agent_types[(a.session_id, a.agent_id)] = a.agent_type
            elif a.tool_use_id:
                untyped[(a.session_id, a.tool_use_id)] = (a.session_id, a.agent_id)
        if untyped:
            # no type in the meta.json: the parent's Agent call names the type it spawned
            for e in index.events(kinds=frozenset({EventKind.SPAWN})):
                agent = untyped.get((e.session_id, str(e.attrs.get("tool_use_id"))))
                kind = e.attrs.get("agent_type")
                if agent is not None and isinstance(kind, str) and kind:
                    agent_types[agent] = kind
        return cls(table, titles, agent_types, folder_name(openclaw_root))

    def project(self, project_dir: str) -> tuple[str, str]:
        # OpenClaw first: its state dir may itself sit under /tmp
        if self.openclaw_dir and _under(project_dir, self.openclaw_dir):
            return OPENCLAW, OPENCLAW
        if is_temporary(project_dir):
            return TEMPORARY, TEMPORARY
        return project_dir, project_dir

    def model(self, model: str) -> tuple[str, str]:
        key = canonical(self.table, model)
        return key, key

    def agent(self, session_id: str, agent_id: str | None) -> tuple[str, str]:
        if agent_id is None:
            return MAIN_THREAD, MAIN_THREAD
        kind = self.agent_types.get((session_id, agent_id)) or UNKNOWN_AGENT
        return kind, kind

    def session(self, session_id: str) -> tuple[str, str]:
        return session_id, self.titles.get(session_id) or session_id[:8]

    def members(
        self, req: Request, by: GroupBy, cost: CostBreakdown
    ) -> list[tuple[str, str, float]]:
        """(key, label, usd) of each member `req`'s spend goes to: one member, or
        one per category. Empty for placeholder replies (`<synthetic>`)."""
        if is_placeholder(req.model):
            return []
        if by is GroupBy.CATEGORY:
            parts = (cost.input, cost.cache_read, cost.cache_write, cost.output, cost.web)
            return [(k, label, v) for (k, label), v in zip(CATEGORIES, parts, strict=True)]
        if by is GroupBy.PROJECT:
            key, label = self.project(req.project_dir)
        elif by is GroupBy.MODEL:
            key, label = self.model(req.model)
        elif by is GroupBy.AGENT:
            key, label = self.agent(req.session_id, req.agent_id)
        elif by is GroupBy.SESSION:
            key, label = self.session(req.session_id)
        else:  # pragma: no cover - GroupBy is closed
            raise ValueError(f"unknown grouping {by!r}")
        return [(key, label, cost.total)]
