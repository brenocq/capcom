"""What stands behind a part of a page, for Ask Roger, and the answers of its tools.

Two uses, one vocabulary. `context` turns a part's reference (a finding, a marked
change, a session, a day, a page's range) into a few compact lines that go into
the prompt with the part (ask.py); `asktools` answers the CLI's follow-up lookups
with `spend`, `session`, `findings` and `where_defined` below.

Everything here is aggregates (dollars, requests, sessions, spawns), labels capcom
makes or keys it groups by (project labels and folders, model ids, agent types,
finding kinds, dates), session ids and file paths (project folders, the files that
define an agent, a skill or a command). Never a session's title, a spawn's
description, an evidence row, a note, a tool's input or anything else written in
the logs; a definition file is only found and its frontmatter `name:` read
(capcom.checks.agents), never its body.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from capcom.checks.agents import find_all_definitions
from capcom.cost.model import price
from capcom.cost.prices import canonical, is_placeholder
from capcom.timeline.groups import MAIN_THREAD
from capcom.types import AgentInfo, AttrValue, Finding

if TYPE_CHECKING:
    from capcom.report.api import Context
    from capcom.timeline.groups import ProjectNames
    from capcom.types import SessionInfo

GROUPS = ("project", "model", "agent_type", "day", "session")
MAX_DAYS = 90
DEFAULT_DAYS = 14
MAX_ROWS = 15  # rows of one grouping a tool returns
CONTEXT_ROWS = 4  # rows of one grouping in the prompt's facts
MAX_CONTEXT = 2400  # characters of facts in a prompt, about 600 tokens
MAX_TEXT = 200  # characters of any string argument
AGENT_KINDS = frozenset(
    {"agent_tools_unused", "one_command_subagents", "long_subagent_reports", "model_mix"}
)
_SESSION_ID = re.compile(r"[0-9A-Za-z_-]{4,64}")
_DAY = re.compile(r"\d{4}-\d\d-\d\d")
_NAME = re.compile(r"[\w.:@/+-]{1,120}")


class FactsError(ValueError):
    """An argument the facts cannot take; its message is shown to the CLI."""


# --------------------------------------------------------------------------- filters


@dataclass(frozen=True, slots=True)
class Filter:
    """Which requests count: by project (its label, folder or path), model (a part
    of its id, such as "opus"), agent type ("main": the main threads) or session
    (its id, or the start of it)."""

    project: str | None = None
    model: str | None = None
    agent_type: str | None = None
    session_id: str | None = None

    def said(self) -> str:
        parts = [
            f"project {self.project}" if self.project else "",
            f"model {self.model}" if self.model else "",
            f"agent type {self.agent_type}" if self.agent_type else "",
            f"session {self.session_id}" if self.session_id else "",
        ]
        return ", ".join(p for p in parts if p)


def _text(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FactsError(f"{name} must be a string.")
    value = value.strip()
    if len(value) > MAX_TEXT:
        raise FactsError(f"{name} is longer than {MAX_TEXT} characters.")
    return value or None


def make_filter(args: Mapping[str, Any]) -> Filter:
    sid = _text(args.get("session_id"), "session_id")
    if sid is not None and not _SESSION_ID.fullmatch(sid):
        raise FactsError("session_id is not a session id.")
    return Filter(
        project=_text(args.get("project"), "project"),
        model=_text(args.get("model"), "model"),
        agent_type=_text(args.get("agent_type"), "agent_type"),
        session_id=sid,
    )


def _days(value: Any, default: int = DEFAULT_DAYS) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        raise FactsError("days must be a whole number.")
    if not 1 <= int(value) <= MAX_DAYS:
        raise FactsError(f"days must be between 1 and {MAX_DAYS}.")
    return int(value)


def _date(value: Any, name: str) -> date | None:
    text = _text(value, name)
    if text is None:
        return None
    if not _DAY.fullmatch(text):
        raise FactsError(f"{name} must be a date, YYYY-MM-DD.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise FactsError(f"{name} is not a date.") from None


# --------------------------------------------------------------------------- totals


@dataclass(slots=True)
class Row:
    key: str
    label: str
    usd: float = 0.0
    requests: int = 0
    sessions: set[str] = field(default_factory=set)
    path: str | None = None
    first: datetime | None = None
    last: datetime | None = None

    def add(self, usd: float, session_id: str, when: datetime) -> None:
        self.usd += usd
        self.requests += 1
        self.sessions.add(session_id)
        if self.first is None or when < self.first:
            self.first = when
        if self.last is None or when > self.last:
            self.last = when


@dataclass(slots=True)
class Totals:
    since: datetime
    until: datetime
    total: Row
    groups: dict[str, dict[str, Row]]

    def rows(self, by: str, limit: int = MAX_ROWS) -> list[Row]:
        rows = list(self.groups[by].values())
        if by == "day":
            return sorted(rows, key=lambda r: r.key)[-limit:]
        return sorted(rows, key=lambda r: (-r.usd, r.key))[:limit]


class Facts:
    """The index behind one question: sessions, agents and project names read once."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.infos: dict[str, SessionInfo] = ctx.infos()
        self.names: ProjectNames = ctx.names(self.infos)
        self.agents: dict[str, AgentInfo] = ctx.agents()

    # ------------------------------------------------------------ helpers

    def project(self, project_dir: str) -> tuple[str, str, str]:
        """(key, label, path) of a project folder, as the tables show it."""
        key = self.names.key(project_dir)
        return key, self.names.label(key), self.names.path(key)

    def agent_type(self, agent_id: str | None) -> str:
        if agent_id is None:
            return MAIN_THREAD
        info = self.agents.get(agent_id)
        return (info.agent_type if info else None) or "(unknown agent)"

    def window(
        self, days: int, since: date | None = None, until: date | None = None
    ) -> tuple[datetime, datetime]:
        """[since, until): the last `days` local days up to now, or whole local days."""
        ctx = self.ctx
        if since is None and until is None:
            return ctx.midnight(days - 1), ctx.now
        first = since or (until or ctx.today()) - timedelta(days=days - 1)
        last = until or min(ctx.today(), first + timedelta(days=MAX_DAYS - 1))
        if last < first:
            raise FactsError("until is before since.")
        if (last - first).days >= MAX_DAYS:
            raise FactsError(f"A range is at most {MAX_DAYS} days.")
        return ctx.at_midnight(first), min(ctx.now, ctx.at_midnight(last + timedelta(days=1)))

    def _session_match(self, wanted: str) -> set[str]:
        hits = {s for s in self.infos if s == wanted or s.startswith(wanted)}
        return hits if len(wanted) >= 8 or wanted in self.infos else {wanted} & set(self.infos)

    def _project_match(self, wanted: str) -> set[str]:
        w = wanted.strip().rstrip("/").lower()
        out: set[str] = set()
        for project_dir in {i.project_dir for i in self.infos.values()}:
            key, label, path = self.project(project_dir)
            full = self.names.paths.get(project_dir, "")
            if w in {
                key.lower(),
                label.lower(),
                path.rstrip("/").lower(),
                project_dir.lower(),
                full.rstrip("/").lower(),
            }:
                out.add(project_dir)
        return out

    def totals(self, since: datetime, until: datetime, filt: Filter | None = None) -> Totals:
        """Every request in [since, until) that `filt` lets through, added up by
        project, model, agent type, local day and session."""
        filt = filt or Filter()
        groups: dict[str, dict[str, Row]] = {g: {} for g in GROUPS}
        total = Row("total", "total")
        projects = self._project_match(filt.project) if filt.project else None
        sessions = self._session_match(filt.session_id) if filt.session_id else None
        model = filt.model.lower() if filt.model else None
        agent = filt.agent_type.lower() if filt.agent_type else None
        one = next(iter(sessions)) if sessions is not None and len(sessions) == 1 else None
        if (projects is not None and not projects) or (sessions is not None and not sessions):
            return Totals(since, until, total, groups)
        table = self.ctx.table
        for req in self.ctx.index.requests(since, until, session_id=one):
            if is_placeholder(req.model):
                continue
            if sessions is not None and req.session_id not in sessions:
                continue
            if projects is not None and req.project_dir not in projects:
                continue
            kind = self.agent_type(req.agent_id)
            if agent is not None and kind.lower() != agent:
                continue
            mid = canonical(table, req.model)
            if model is not None and model not in mid.lower() and model not in req.model.lower():
                continue
            usd = price(req, table).total
            when = req.timestamp
            total.add(usd, req.session_id, when)
            key, label, path = self.project(req.project_dir)
            self._row(groups["project"], key, label, path).add(usd, req.session_id, when)
            self._row(groups["model"], mid, mid).add(usd, req.session_id, when)
            self._row(groups["agent_type"], kind, kind).add(usd, req.session_id, when)
            day = self.ctx.local(when).date().isoformat()
            self._row(groups["day"], day, day).add(usd, req.session_id, when)
            self._row(groups["session"], req.session_id, label, path).add(usd, req.session_id, when)
        return Totals(since, until, total, groups)

    @staticmethod
    def _row(rows: dict[str, Row], key: str, label: str, path: str | None = None) -> Row:
        row = rows.get(key)
        if row is None:
            row = rows[key] = Row(key, label, path=path)
        return row

    def when(self, t: datetime | None) -> str | None:
        return None if t is None else self.ctx.local(t).isoformat(timespec="minutes")

    # ------------------------------------------------------------ the tools

    def spend(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Tool `spend`: totals grouped by one key, over a range, with a filter."""
        by = _text(args.get("group_by"), "group_by") or "project"
        if by not in GROUPS:
            raise FactsError(f"group_by is one of {', '.join(GROUPS)}.")
        filt = make_filter(args)
        since, until = self.window(
            _days(args.get("days")),
            _date(args.get("since"), "since"),
            _date(args.get("until"), "until"),
        )
        t = self.totals(since, until, filt)
        limit = MAX_ROWS if by != "day" else MAX_DAYS
        rows = [self._row_json(by, r, t.total.usd) for r in t.rows(by, limit)]
        out: dict[str, Any] = {
            "from": self.when(since),
            "to": self.when(until),
            "filter": filt.said() or "none",
            "total": self._total_json(t.total),
            "group_by": by,
            "rows": rows,
        }
        shown = len(rows)
        if len(t.groups[by]) > shown:
            out["more_rows"] = len(t.groups[by]) - shown
        return out

    def _total_json(self, r: Row) -> dict[str, Any]:
        return {"usd": round(r.usd, 2), "requests": r.requests, "sessions": len(r.sessions)}

    def _row_json(self, by: str, r: Row, total: float) -> dict[str, Any]:
        share = round(100 * r.usd / total, 1) if total else 0.0
        if by == "session":
            info = self.infos.get(r.key)
            return {
                "session_id": r.key,
                "project": r.label,
                "folder": (info.cwd if info and info.cwd else r.path),
                "usd": round(r.usd, 2),
                "share_pct": share,
                "requests": r.requests,
                "first": self.when(r.first),
                "last": self.when(r.last),
            }
        out: dict[str, Any] = {
            by: r.label,
            "usd": round(r.usd, 2),
            "share_pct": share,
            "requests": r.requests,
            "sessions": len(r.sessions),
        }
        if by == "project" and r.path and r.path != r.label:
            out["folder"] = r.path
        return out

    def session(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Tool `session`: one session's project, folder, times and splits."""
        sid = _text(args.get("session_id"), "session_id")
        if sid is None or not _SESSION_ID.fullmatch(sid):
            raise FactsError("session_id is needed: a session id, or its first 8 characters.")
        hits = sorted(self._session_match(sid))
        if not hits:
            return {"found": False, "message": "No session with that id in capcom's index."}
        if len(hits) > 1:
            return {
                "found": False,
                "message": "Several sessions start with that id.",
                "session_ids": hits[:10],
            }
        return self._session_json(hits[0])

    def _session_json(self, sid: str, rows: int = MAX_ROWS) -> dict[str, Any]:
        info = self.infos[sid]
        _, label, path = self.project(info.project_dir)
        start = info.first_seen or self.ctx.now - timedelta(days=MAX_DAYS)
        end = (info.last_seen or self.ctx.now) + timedelta(seconds=1)
        t = self.totals(start, end, Filter(session_id=sid))
        spawns: dict[str, int] = {}
        for a in self.agents.values():
            if a.session_id == sid:
                kind = a.agent_type or "(unknown agent)"
                spawns[kind] = spawns.get(kind, 0) + 1
        return {
            "found": True,
            "session_id": sid,
            "project": label,
            "project_folder": path,
            "folder": info.cwd,
            "entrypoint": info.entrypoint,
            "claude_code_version": info.version,
            "first": self.when(t.total.first or info.first_seen),
            "last": self.when(t.total.last or info.last_seen),
            "total": self._total_json(t.total),
            "by_model": [self._row_json("model", r, t.total.usd) for r in t.rows("model", rows)],
            "by_agent_type": [
                self._row_json("agent_type", r, t.total.usd) for r in t.rows("agent_type", rows)
            ],
            "by_day": [self._row_json("day", r, t.total.usd) for r in t.rows("day", rows)],
            "subagents_spawned": dict(sorted(spawns.items(), key=lambda kv: -kv[1])[:rows]),
        }

    def findings(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Tool `findings`: the Checks findings of the last `days` days, by saving."""
        days = _days(args.get("days"))
        since, until = self.window(days)
        found = self.ctx.analysis.checks(self.ctx.index, self.ctx.table, since, until)
        return {
            "days": days,
            "findings": [self.finding_json(f) for f in found[:MAX_ROWS]],
            **({"more": len(found) - MAX_ROWS} if len(found) > MAX_ROWS else {}),
        }

    def scope_label(self, scope: str) -> str:
        dirs = {i.project_dir for i in self.infos.values()}
        if scope in dirs:
            return self.project(scope)[1]
        return scope

    def finding_json(self, f: Finding) -> dict[str, Any]:
        """A finding as Checks shows it: its kind, scope, title and generated numbers."""
        out: dict[str, Any] = {
            "kind": f.kind,
            "scope": self.scope_label(f.scope),
            "title": f.title,
            "summary": f.summary[:400],  # generated sentences with the numbers
            "saving_usd_per_week": round(f.saving_usd_per_week, 2),
        }
        nums = _numbers(f.metrics)
        if nums:
            out["numbers"] = nums
        return out

    def where_defined(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Tool `where_defined`: the files that define an agent type, a skill or a
        slash command, and the projects whose sessions spawned the agent."""
        name = _text(args.get("name"), "name")
        if name is None or not _NAME.fullmatch(name.lstrip("/")):
            raise FactsError("name is needed: an agent type, a skill or a command name.")
        what = _text(args.get("kind"), "kind") or "agent"
        if what not in ("agent", "skill", "command"):
            raise FactsError("kind is agent, skill or command.")
        name = name.lstrip("/")
        files = definitions(name, what, self.roots(), self.claude_root())
        out: dict[str, Any] = {"name": name, "kind": what, "defined_in": files}
        if len(files) > 1:
            out["precedence"] = PRECEDENCE
        if not files:
            out["message"] = (
                f"No file defining the {what} {name} was found in the projects' .claude "
                "folders, the user's ~/.claude or the installed plugins. It may be built "
                "into Claude Code."
            )
        if what == "agent":
            out["spawned_in"] = self.spawned_in(name, defs=files)
        return out

    def spawned_in(
        self,
        agent_type: str,
        limit: int = MAX_ROWS,
        defs: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """The projects whose sessions spawned subagents of `agent_type`, most first;
        with its definitions `defs`, the one Claude Code uses in each."""
        counts: dict[str, list[Any]] = {}
        folders: dict[str, set[str]] = {}
        for a in self.agents.values():
            if (a.agent_type or "") != agent_type:
                continue
            info = self.infos.get(a.session_id)
            if info is None:
                continue
            key, label, path = self.project(info.project_dir)
            row = counts.setdefault(key, [label, path, 0, set(), key])
            row[2] += 1
            row[3].add(a.session_id)
            got = folders.setdefault(key, set())
            got.add(info.cwd or "")
            got.add(self.names.paths.get(info.project_dir) or "")
        rows = sorted(counts.values(), key=lambda r: (-r[2], r[0]))[:limit]
        out = []
        for r in rows:
            item: dict[str, Any] = {
                "project": r[0], "folder": r[1], "spawns": r[2], "sessions": len(r[3])
            }  # fmt: skip
            if defs:
                item["uses"] = used_in(defs, folders[r[4]])
            out.append(item)
        return out

    def roots(self) -> list[Path]:
        """The folders whose `.claude/` may define agents, skills and commands: the
        sessions' working folders and their projects' folders."""
        found = {i.cwd for i in self.infos.values() if i.cwd}
        found |= {p for p in self.names.paths.values() if p}
        return [Path(p) for p in sorted(found) if p.startswith("/")]

    def claude_root(self) -> Path:
        dirs = self.ctx.agent_dirs
        if dirs:
            return Path(dirs[-1]).parent  # the user's agents folder comes last
        from capcom.config import load_config

        return load_config().claude_root

    # ------------------------------------------------------------ the context

    def context(self, ref: Mapping[str, Any]) -> str:
        """What stands behind a part, as compact lines for the prompt."""
        kind = ref.get("kind")
        if kind == "finding":
            lines = self._finding_lines(
                str(ref["finding"]), str(ref["scope"]), int(ref.get("days") or DEFAULT_DAYS)
            )
        elif kind == "mark":
            lines = self._mark_lines(int(ref["id"]))
        elif kind == "session":
            lines = self._session_lines(str(ref["id"]))
        elif kind == "day":
            day = date.fromisoformat(str(ref["date"]))
            since = self.ctx.at_midnight(day)
            until = min(self.ctx.now, self.ctx.at_midnight(day + timedelta(days=1)))
            lines = [
                f"Behind it, on {day.isoformat()}:",
                *self._split_lines(self.totals(since, until)),
            ]
        else:
            days = int(ref.get("days") or DEFAULT_DAYS)
            since, until = self.window(days)
            lines = [
                f"Behind it, the last {days} days:",
                *self._split_lines(self.totals(since, until)),
            ]
        return clip(lines)

    def _split_lines(
        self, t: Totals, groups: Sequence[str] = ("project", "model", "agent_type", "day")
    ) -> list[str]:
        if not t.total.requests:
            return ["No requests in capcom's index for it in that time."]
        out = [
            f"Total: {_usd(t.total.usd)} in {t.total.requests:,} requests, "
            f"{len(t.total.sessions)} sessions."
        ]
        names = {
            "project": "By project",
            "model": "By model",
            "agent_type": "By agent type ('main' is the main thread)",
            "day": "By day",
        }
        for g in groups:
            rows = t.rows(g, CONTEXT_ROWS if g != "day" else 7)
            if g == "day":
                said = [f"{r.key} {_usd(r.usd)}" for r in rows]
            else:
                said = [
                    f"{r.label}"
                    f"{f' ({r.path})' if g == 'project' and r.path and r.path != r.label else ''} "
                    f"{_usd(r.usd)}, {r.requests:,} requests"
                    for r in rows
                ]
            more = len(t.groups[g]) - len(rows)
            tail = f"; and {more} more" if more > 0 and g != "day" else ""
            out.append(f"{names[g]}: " + "; ".join(said) + tail + ".")
        top = t.rows("session", 3)
        if top:
            out.append(
                "Top sessions (titles are never given): "
                + "; ".join(f"{r.key} in {r.label}, {_usd(r.usd)}" for r in top)
                + "."
            )
        return out

    def _filter_for(self, finding: str, scope: str) -> tuple[Filter, str]:
        """The requests a finding's scope stands for, and how to say it."""
        types = {a.agent_type for a in self.agents.values() if a.agent_type}
        dirs = {i.project_dir for i in self.infos.values()}
        if finding in AGENT_KINDS or (scope in types and scope not in dirs):
            return Filter(agent_type=scope), f"the {scope} agent type's requests"
        if scope in dirs:
            label = self.project(scope)[1]
            return Filter(project=scope), f"the requests in project {label}"
        return Filter(), "every request"

    def _finding_lines(
        self, finding: str, scope: str, days: int, since: datetime | None = None
    ) -> list[str]:
        filt, said = self._filter_for(finding, scope)
        if since is None:
            since, until = self.window(days)
            span = f"the last {days} days"
        else:
            until = self.ctx.now
            span = f"since {self.ctx.local(since).date().isoformat()}"
        lines = [f"Behind this finding ({finding}), {said} over {span}:"]
        lines += self._split_lines(self.totals(since, until, filt))
        if filt.agent_type:
            lines += self._agent_lines(filt.agent_type)
        return lines

    def _agent_lines(self, agent_type: str) -> list[str]:
        out: list[str] = []
        files = definitions(agent_type, "agent", self.roots(), self.claude_root())
        if files:
            out.append(
                f"The {agent_type} agent is defined in: "
                + "; ".join(f"{f['path']} ({_level(f)}; {f['use']})" for f in files[:4])
                + "."
                + (f" {PRECEDENCE}" if len(files) > 1 else "")
            )
        else:
            out.append(f"No file defining the {agent_type} agent was found (it may be built in).")
        spawned = self.spawned_in(agent_type, CONTEXT_ROWS, defs=files)
        if spawned:
            out.append(
                "Spawned in: "
                + "; ".join(
                    f"{r['project']} ({r['folder']}), "
                    f"{r['spawns']} spawns in {r['sessions']} sessions"
                    + (f", using {r['uses']}" if len(files) > 1 and r.get("uses") else "")
                    for r in spawned
                )
                + "."
            )
        return out

    def _mark_lines(self, mark_id: int) -> list[str]:
        marks = self.ctx.analysis.changes().load(self.ctx.changes_path)
        mark = next((m for m in marks if m.id == mark_id), None)
        if mark is None:
            return []
        start = self.ctx.at_midnight(mark.applied_on - timedelta(days=28))
        start = max(start, self.ctx.now - timedelta(days=MAX_DAYS))
        lines = [
            f"The change was marked applied on {mark.applied_on.isoformat()}, for the finding "
            f"{mark.kind} on {self.scope_label(mark.scope)}."
        ]
        return lines + self._finding_lines(mark.kind, mark.scope, DEFAULT_DAYS, since=start)

    def _session_lines(self, sid: str) -> list[str]:
        hits = sorted(self._session_match(sid))
        if len(hits) != 1:
            return []
        s = self._session_json(hits[0], CONTEXT_ROWS)
        tot = s["total"]
        out = [
            f"Behind it, session {s['session_id']} in project {s['project']} "
            f"({s['folder'] or s['project_folder']}), "
            f"from {s['first']} to {s['last']}: {_usd(tot['usd'])} in {tot['requests']:,} requests."
        ]
        for name, key, col in (
            ("By model", "by_model", "model"),
            ("By agent type", "by_agent_type", "agent_type"),
        ):
            if s[key]:
                out.append(
                    f"{name}: "
                    + "; ".join(
                        f"{r[col]} {_usd(r['usd'])}, {r['requests']:,} requests" for r in s[key]
                    )
                    + "."
                )
        if s["subagents_spawned"]:
            out.append(
                "Subagents spawned: "
                + ", ".join(f"{k} {v}" for k, v in s["subagents_spawned"].items())
                + "."
            )
        return out


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _level(f: Mapping[str, Any]) -> str:
    if f["level"] == "project":
        return f"a project {f['what']}, in {f.get('project') or 'a project'}"
    if f["level"] == "plugin":
        return f"from the plugin {f.get('plugin')}"
    return f"a user {f['what']}, in ~/.claude"


def _numbers(metrics: Mapping[str, AttrValue], limit: int = 8) -> dict[str, float]:
    """A finding's numeric metrics only (its lists and strings are left out)."""
    out: dict[str, float] = {}
    for k, v in metrics.items():
        if isinstance(v, bool) or not isinstance(v, int | float):
            continue
        out[k] = round(float(v), 4)
        if len(out) == limit:
            break
    return out


def clip(lines: Iterable[str], limit: int = MAX_CONTEXT) -> str:
    """Lines joined, whole lines only, at most `limit` characters."""
    out: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


# --------------------------------------------------------------------------- definitions


def plugin_dirs(claude_root: Path) -> list[tuple[str, Path]]:
    """(plugin name, its folder) of each installed plugin's cached version:
    plugins/cache/<marketplace>/<plugin>/<version>/."""
    out: list[tuple[str, Path]] = []
    cache = claude_root / "plugins" / "cache"
    try:
        for market in sorted(p for p in cache.iterdir() if p.is_dir()):
            for plugin in sorted(p for p in market.iterdir() if p.is_dir()):
                for version in sorted(p for p in plugin.iterdir() if p.is_dir()):
                    out.append((plugin.name, version))
    except OSError:
        pass
    return out


def _project_of(path: Path, roots: Sequence[Path]) -> str:
    for r in roots:
        if path.is_relative_to(r / ".claude"):
            return r.name
    return ""


def definitions(
    name: str, what: str, roots: Sequence[Path], claude_root: Path
) -> list[dict[str, Any]]:
    """The files that define agent type, skill or command `name`: each project's
    `.claude/`, then the user's, then the plugins' (a "plugin:name" looks only in
    that plugin). Only file names and, for agents, the frontmatter's `name:`."""
    plugin, _, bare = name.rpartition(":")
    found: list[dict[str, Any]] = []
    places: list[tuple[str, Path, str]] = []  # (level, the .claude-like folder, plugin)
    if not plugin:
        places += [("project", r / ".claude", "") for r in roots]
        places.append(("user", claude_root, ""))
    places += [("plugin", d, p) for p, d in plugin_dirs(claude_root) if not plugin or p == plugin]
    seen: set[Path] = set()
    for level, base, plug in places:
        for path in _candidates(base, bare, what):
            try:
                real = path.resolve()
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            row: dict[str, Any] = {"path": str(path), "level": level, "what": what}
            if level == "project":
                row["project"] = _project_of(path, roots)
            if plug:
                row["plugin"] = plug
            found.append(row)
    return _ranked(found)


PRECEDENCE = (
    "Claude Code uses a project's own file first, then the user's ~/.claude, then a plugin's."
)
_ORDER = {"project": 0, "user": 1, "plugin": 2}


def _ranked(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`found` with the one Claude Code uses first (a project's own file overrides
    the user's, which overrides a plugin's), each saying where it is used."""
    found = sorted(found, key=lambda r: _ORDER[r["level"]])  # stable: roots' order kept
    projects = [r.get("project") or "a project" for r in found if r["level"] == "project"]
    user = any(r["level"] == "user" for r in found)
    for r in found:
        if r["level"] == "project":
            r["use"] = f"the one used in {r.get('project') or 'its project'}"
            r["overridden"] = False
        elif r["level"] == "user":
            r["overridden"] = bool(projects)
            r["use"] = (
                f"overridden in {', '.join(projects)} by the project's own file; "
                "used in every other project"
                if projects
                else "the one used in every project"
            )
        else:
            r["overridden"] = user or bool(projects)
            if user:
                r["use"] = "overridden by the user's file"
            elif projects:
                r["use"] = f"overridden in {', '.join(projects)}; used in every other project"
            else:
                r["use"] = "the one used"
    return found


def used_in(defs: Sequence[Mapping[str, Any]], folders: Iterable[str]) -> str | None:
    """The path of the definition Claude Code uses in a project with these folders."""
    dirs = [Path(f) / ".claude" for f in folders if f and f.startswith("/")]
    for r in defs:
        if r["level"] == "project" and any(Path(r["path"]).is_relative_to(d) for d in dirs):
            return str(r["path"])
    for r in defs:
        if r["level"] != "project":
            return str(r["path"])
    return None


def _candidates(base: Path, name: str, what: str) -> list[Path]:
    if what == "agent":
        defs = find_all_definitions([base / "agents"]).get(name, [])
        return [d.path for d in defs]
    if what == "skill":
        path = base / "skills" / name / "SKILL.md"
    else:  # a command; "a:b" is commands/a/b.md
        path = base / "commands" / Path(*name.split(":")).with_suffix(".md")
    try:
        return [path] if path.is_file() else []
    except OSError:
        return []
