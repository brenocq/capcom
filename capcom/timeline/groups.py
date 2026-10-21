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

`ProjectNames` is the one display rule for project folders in tables (the website
and `capcom top`): the repo folder's own name, "openclaw" or "openclaw (<sub>)" for
the OpenClaw folders, listed apart, and "temporary (<project>)" or "(temporary)"
for folders under /tmp.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from capcom.config import load_config
from capcom.cost.prices import canonical, is_placeholder
from capcom.types import (
    CostBreakdown,
    EventKind,
    GroupBy,
    Index,
    PriceTable,
    Request,
    SessionInfo,
)

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

TEMPORARY_LABEL = "(temporary)"  # the table label of scratch folders naming no project
OPENCLAW_ALL = "openclaw (all folders)"  # the chart series that merges them

_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
# A folder under /tmp that names the project it was made for: Claude Code's
# scratchpads, /tmp/claude-<uid>/<project folder>/<session id>/..., hold the
# project's own folder name (itself starting with "-") before a session id.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TEMP_OF_PROJECT = re.compile(rf"^-tmp-.+?-(-[^-].*?)-{_UUID}(?:-|$)")


def folder_name(path: str | Path) -> str:
    """Claude Code's projects/ folder name for a working directory: every character
    that is not a letter or a digit becomes '-'."""
    return _NOT_ALNUM.sub("-", str(path))


def _under(project_dir: str, prefix: str) -> bool:
    return project_dir == prefix or project_dir.startswith(prefix + "-")


def is_temporary(project_dir: str) -> bool:
    """A folder under /tmp (scratch sessions, forked or copied sessions)."""
    return _under(project_dir, "-tmp")


def temporary_project(project_dir: str) -> str | None:
    """The project folder a scratch folder under /tmp was made for, if its name holds
    one (Claude Code's scratchpads do)."""
    if not project_dir.startswith("-tmp-"):
        return None
    m = _TEMP_OF_PROJECT.match(project_dir)
    return m.group(1) if m else None


def _home_path(path: str, home: str) -> str:
    """`path` with the home directory written as "~"."""
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        return "~" + path[len(home.rstrip("/")) :]
    return path


@dataclass(frozen=True, slots=True)
class ProjectNames:
    """Display names of project folders, the same in every table.

    A folder's real path is the cwd of one of its sessions, or the ancestor of that
    cwd whose folder form is the project folder (a session that moved into a
    subfolder still names its repo). The label is that path's last part; two
    folders with the same last part are told apart by their path. OpenClaw folders
    are "openclaw" (the state dir) and "openclaw (<path under it>)"; folders under
    /tmp are "temporary (<project label>)" when they name the project they were made
    for, else "(temporary)". A folder no cwd explains keeps its folder name."""

    paths: Mapping[str, str]  # project folder -> its path, from the cwds seen
    openclaw_dir: str = ""  # the OpenClaw state dir, in folder form
    openclaw_root: str = ""
    home: str = ""
    labels: Mapping[str, str] | None = None  # filled by `build`

    @classmethod
    def build(
        cls,
        sessions: Iterable[SessionInfo] | Iterable[tuple[str, str | None]],
        openclaw_root: Path | None = None,
        home: Path | None = None,
    ) -> ProjectNames:
        if openclaw_root is None:
            openclaw_root = load_config().openclaw_root
        home_s = str(home if home is not None else Path.home())
        claw = folder_name(openclaw_root)
        paths: dict[str, str] = {}
        dirs: set[str] = set()
        for s in sessions:
            project_dir, cwd = (s.project_dir, s.cwd) if isinstance(s, SessionInfo) else s
            dirs.add(project_dir)
            if project_dir in paths or not cwd:
                continue
            if is_temporary(project_dir) and not _under(project_dir, claw):
                continue
            p = Path(cwd)
            for cand in (p, *p.parents):
                if folder_name(cand) == project_dir:
                    paths[project_dir] = str(cand)
                    break
        base = cls(paths, claw, str(openclaw_root), home_s)
        # every folder's label, OpenClaw ones and folders no cwd explains included,
        # so a repo named "openclaw" and the OpenClaw state dir are told apart
        labels = {
            d: base._own_label(d)
            for d in dirs
            if base.is_openclaw(d) or not is_temporary(d) or d in paths
        }
        seen: dict[str, list[str]] = {}
        for d, label in labels.items():
            seen.setdefault(label, []).append(d)
        for same in seen.values():
            if len(same) > 1:  # same last part: tell them apart by the path
                for d in same:
                    labels[d] = base._full_path(d)
        return cls(paths, base.openclaw_dir, base.openclaw_root, home_s, labels)

    def is_openclaw(self, project_dir: str) -> bool:
        return bool(self.openclaw_dir) and _under(project_dir, self.openclaw_dir)

    def key(self, project_dir: str) -> str:
        """The table row of a folder: the folder itself, or one shared key per
        project for scratch folders ("temporary (<project folder>)", "(temporary)")."""
        if self.is_openclaw(project_dir) or not is_temporary(project_dir):
            return project_dir
        inner = temporary_project(project_dir)
        return f"temporary ({inner})" if inner else TEMPORARY_LABEL

    def _own_label(self, project_dir: str) -> str:
        path = self.paths.get(project_dir)
        if self.is_openclaw(project_dir):
            if project_dir == self.openclaw_dir:
                return OPENCLAW
            sub = project_dir[len(self.openclaw_dir) + 1 :]
            root = self.openclaw_root.rstrip("/")
            if path and path.startswith(root + "/"):
                sub = path[len(root) + 1 :]
            return f"{OPENCLAW} ({sub})"
        if path:
            if path.rstrip("/") == self.home.rstrip("/"):
                return "~"  # the home folder itself
            return path.rstrip("/").rsplit("/", 1)[-1] or path
        return project_dir

    def _full_path(self, project_dir: str) -> str:
        """The folder's path ("~/..."), the OpenClaw root for its state dir, else
        the folder name."""
        known = self.paths.get(project_dir)
        if not known and self.openclaw_dir and project_dir == self.openclaw_dir:
            known = self.openclaw_root
        return _home_path(known, self.home) if known else project_dir

    def label(self, project_dir: str) -> str:
        """The display name of a folder, or of a table key `key` returned."""
        if project_dir.startswith("temporary (") and project_dir.endswith(")"):
            return f"temporary ({self.label(project_dir[len('temporary (') : -1])})"
        if project_dir == TEMPORARY_LABEL:
            return TEMPORARY_LABEL
        if is_temporary(project_dir) and not self.is_openclaw(project_dir):
            return self.label(self.key(project_dir))
        if self.labels is not None and project_dir in self.labels:
            return self.labels[project_dir]
        return self._own_label(project_dir)

    def path(self, project_dir: str) -> str:
        """The folder's full path for a tooltip or a side column ("~" for home), or
        the folder name when no cwd explains it; scratch keys name their project's."""
        if project_dir.startswith("temporary (") and project_dir.endswith(")"):
            return "/tmp/... for " + self.path(project_dir[len("temporary (") : -1])
        if project_dir == TEMPORARY_LABEL:
            return "/tmp"
        if is_temporary(project_dir) and not self.is_openclaw(project_dir):
            return self.path(self.key(project_dir))
        return self._full_path(project_dir)


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
