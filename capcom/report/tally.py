"""Token and dollar totals for the website, one pass over the requests.

The prices come from capcom.cost; this module only adds them up per group, the way
`capcom top` does (placeholder models skipped, web tools counted with output,
estimated output flagged with its share).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from capcom.cost.model import price
from capcom.cost.prices import is_placeholder, normalize
from capcom.cost.top import TEMPORARY, project_label
from capcom.timeline.groups import MAIN_THREAD, OPENCLAW, is_temporary
from capcom.types import AgentInfo, CostBreakdown, PriceTable, Request, SessionInfo

# A gap between two requests of a session at least this long is a pause; shorter
# gaps count as active time.
PAUSE_SECONDS = 5 * 60

# Context-size buckets for "Context size when you paid" (upper bounds, tokens).
CONTEXT_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("<50k", 50_000),
    ("50-100k", 100_000),
    ("100-200k", 200_000),
    ("200-400k", 400_000),
    ("400-600k", 600_000),
    (">600k", None),
)


def ms(t: datetime) -> int:
    """Epoch milliseconds, the time format of every series the API returns."""
    return int(t.timestamp() * 1000)


def iso(t: datetime | None, tz: tzinfo | None = None) -> str | None:
    return None if t is None else t.astimezone(tz).isoformat(timespec="seconds")


@dataclass(slots=True)
class Tally:
    """Totals of a group of requests."""

    requests: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    usd_input: float = 0.0
    usd_cache_read: float = 0.0
    usd_cache_write: float = 0.0
    usd_output: float = 0.0  # output and web tools
    estimated_requests: int = 0  # requests whose output count is an estimate
    estimated_output: int = 0  # output tokens that are estimates
    inexact_price: int = 0

    @property
    def usd(self) -> float:
        return self.usd_input + self.usd_cache_read + self.usd_cache_write + self.usd_output

    @property
    def tokens(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output

    def add(self, req: Request, cost: CostBreakdown) -> None:
        u = req.usage
        self.requests += 1
        self.input += u.input
        self.cache_read += u.cache_read
        self.cache_write += u.cache_write
        self.output += u.output
        self.usd_input += cost.input
        self.usd_cache_read += cost.cache_read
        self.usd_cache_write += cost.cache_write
        self.usd_output += cost.output + cost.web
        if req.output_estimated:
            self.estimated_requests += 1
            self.estimated_output += u.output
        self.inexact_price += not cost.exact_price

    def to_json(self) -> dict[str, Any]:
        share = self.estimated_output / self.output if self.output else 0.0
        return {
            "requests": self.requests,
            "usd": round(self.usd, 6),
            "tokens": self.tokens,
            "categories": {
                "cache_read": {"tokens": self.cache_read, "usd": round(self.usd_cache_read, 6)},
                "cache_write": {"tokens": self.cache_write, "usd": round(self.usd_cache_write, 6)},
                "output": {"tokens": self.output, "usd": round(self.usd_output, 6)},
                "input": {"tokens": self.input, "usd": round(self.usd_input, 6)},
            },
            "estimated_output": {
                "requests": self.estimated_requests,
                "tokens": self.estimated_output,
                "share": round(share, 4),
            },
            "inexact_price_requests": self.inexact_price,
        }


@dataclass(slots=True)
class Group:
    key: str
    label: str
    tally: Tally = field(default_factory=Tally)
    members: set[str] = field(default_factory=set)  # sessions or agents counted in it

    def add(self, req: Request, cost: CostBreakdown, member: str) -> None:
        self.tally.add(req, cost)
        self.members.add(member)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "count": len(self.members)}
        out.update(self.tally.to_json())
        return out


@dataclass(slots=True)
class SessionRow:
    """One session's totals in a window, for the sessions tables."""

    session_id: str
    project_dir: str
    tally: Tally = field(default_factory=Tally)
    agents: set[str] = field(default_factory=set)
    peak_context: int = 0
    first: datetime | None = None
    last: datetime | None = None
    active_seconds: float = 0.0


def project_key(project_dir: str, openclaw_dir: str = "") -> str:
    """The project group of a folder: "openclaw" for the OpenClaw state dir (in folder
    form), the shared temporary key for any folder under /tmp ("temporary (<project
    folder>)" when the scratchpad names the project it was made for, else
    "(temporary)"; capcom.timeline.groups.is_temporary decides what is temporary),
    else the folder itself."""
    if openclaw_dir and (project_dir == openclaw_dir or project_dir.startswith(openclaw_dir + "-")):
        return OPENCLAW
    if is_temporary(project_dir):
        label = project_label(project_dir)
        return label if label.startswith("temporary (") else TEMPORARY
    return project_dir


def project_names(sessions: Iterable[SessionInfo]) -> dict[str, str]:
    """A display name per project folder: the last part of a cwd seen in it (the
    folder name alone loses the slashes). Temporary folders get none: they are
    named after the project they were made for (`project_display`)."""
    names: dict[str, str] = {}
    for s in sessions:
        if s.project_dir in names or is_temporary(s.project_dir):
            continue
        if s.cwd:
            names[s.project_dir] = s.cwd.rstrip("/").rsplit("/", 1)[-1] or s.cwd
    return names


def project_display(project_dir: str, names: Mapping[str, str], openclaw_dir: str = "") -> str:
    """The label of a folder's project group: "temporary (<project>)" for scratch
    folders, with the project named as everywhere else, "(temporary)", "openclaw",
    or the project's name."""
    key = project_key(project_dir, openclaw_dir)
    if key.startswith("temporary ("):
        inner = key[len("temporary (") : -1]
        return f"temporary ({names.get(inner) or inner})"
    if key in (TEMPORARY, OPENCLAW):
        return key
    return names.get(project_dir) or project_dir


class Window:
    """Totals of the requests in one window by project, agent type, model and
    session, and the context buckets; `add` each request once, in time order."""

    def __init__(
        self,
        table: PriceTable,
        infos: Mapping[str, SessionInfo],
        agents: Mapping[str, AgentInfo],
        names: Mapping[str, str],
        openclaw_dir: str = "",
    ) -> None:
        self.table = table
        self.openclaw_dir = openclaw_dir
        self.infos = infos
        self.agents = agents
        self.names = names
        self.total = Tally()
        self.by_project: dict[str, Group] = {}
        self.by_agent: dict[str, Group] = {}
        self.by_model: dict[str, Group] = {}
        self.sessions: dict[str, SessionRow] = {}
        self.subagents = Group("subagents", "subagents")
        self.agent_calls: dict[str, int] = {}  # tool calls per subagent
        self.context = [[0, 0.0] for _ in CONTEXT_BUCKETS]

    def add(self, req: Request) -> CostBreakdown | None:
        if is_placeholder(req.model):
            return None  # not a model call (the rule `capcom top` uses)
        cost = price(req, self.table)
        self.total.add(req, cost)
        pkey = project_key(req.project_dir, self.openclaw_dir)
        label = project_display(req.project_dir, self.names, self.openclaw_dir)
        self._group(self.by_project, pkey, label).add(req, cost, req.session_id)
        if req.agent_id is None:
            akey, member = MAIN_THREAD, req.session_id
        else:
            info = self.agents.get(req.agent_id)
            akey = (info.agent_type if info else None) or "(unknown agent)"
            member = req.agent_id
            self.subagents.tally.add(req, cost)
            self.subagents.members.add(req.agent_id)
            calls = self.agent_calls.get(req.agent_id, 0)
            self.agent_calls[req.agent_id] = calls + len(req.tool_calls)
        self._group(self.by_agent, akey, akey).add(req, cost, member)
        model = normalize(req.model)
        self._group(self.by_model, model, model).add(req, cost, req.session_id)
        self._session(req, cost)
        ctx = req.usage.context
        for i, (_, upper) in enumerate(CONTEXT_BUCKETS):
            if upper is None or ctx < upper:
                self.context[i][0] += 1
                self.context[i][1] += cost.total
                break
        return cost

    def _group(self, groups: dict[str, Group], key: str, label: str) -> Group:
        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(key, label)
        return g

    def _session(self, req: Request, cost: CostBreakdown) -> None:
        row = self.sessions.get(req.session_id)
        if row is None:
            row = self.sessions[req.session_id] = SessionRow(req.session_id, req.project_dir)
        row.tally.add(req, cost)
        if req.agent_id is not None:
            row.agents.add(req.agent_id)
        row.peak_context = max(row.peak_context, req.usage.context)
        if row.last is not None:
            gap = (req.timestamp - row.last).total_seconds()
            if 0 < gap < PAUSE_SECONDS:
                row.active_seconds += gap
        if row.first is None:
            row.first = req.timestamp
        if row.last is None or req.timestamp > row.last:
            row.last = req.timestamp

    def groups_json(self, groups: Mapping[str, Group]) -> list[dict[str, Any]]:
        ordered = sorted(groups.values(), key=lambda g: (-g.tally.usd, g.key))
        return [g.to_json() for g in ordered]

    def context_json(self) -> list[dict[str, Any]]:
        return [
            {"label": label, "requests": n, "usd": round(usd, 6)}
            for (label, _), (n, usd) in zip(CONTEXT_BUCKETS, self.context, strict=True)
        ]

    def session_rows(self, tz: tzinfo | None) -> list[dict[str, Any]]:
        rows = sorted(self.sessions.values(), key=lambda r: (-r.tally.usd, r.session_id))
        return [
            session_json(r, self.infos.get(r.session_id), self.names, tz, self.openclaw_dir)
            for r in rows
        ]


def session_json(
    row: SessionRow,
    info: SessionInfo | None,
    names: Mapping[str, str],
    tz: tzinfo | None,
    openclaw_dir: str = "",
) -> dict[str, Any]:
    t = row.tally
    tokens = t.tokens
    return {
        "session_id": row.session_id,
        "title": info.title if info else None,
        "project": project_display(row.project_dir, names, openclaw_dir),
        "project_dir": row.project_dir,
        "temporary": is_temporary(row.project_dir),
        "git_branch": info.git_branch if info else None,
        "subagents": len(row.agents),
        "active_seconds": round(row.active_seconds),
        "first": iso(row.first, tz),
        "last": iso(row.last, tz),
        "peak_context": row.peak_context,
        "cache_read_share": round(t.cache_read / tokens, 4) if tokens else 0.0,
        "usd": round(t.usd, 6),
        "requests": t.requests,
        "estimated_output": t.estimated_requests > 0,
    }
