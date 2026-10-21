"""Changes capcom can see on its own: an agent definition whose frontmatter now says
what a finding suggested (a `model:` line for model_mix, a `tools:` line for
agent_tools_unused), changed after the finding's window began. Read-only: the same
definition files the checks read (capcom.checks.default_agent_dirs), frontmatter
only. The user confirms one with `capcom mark`, source "noticed"."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capcom.changes.store import load
from capcom.checks.agents import (
    MAX_FRONTMATTER_LINES,
    AgentDefinition,
    _lines,
    _split,
    find_all_definitions,
)
from capcom.types import ChangeMark, Finding, Index, NoticedChange, PriceTable

DEFAULT_WINDOW = timedelta(days=14)  # `capcom checks`' default window
KINDS = frozenset({"model_mix", "agent_tools_unused"})


def read_model(path: Path) -> str | None:
    """The `model:` value of a definition's frontmatter, or None. Reads at most the
    frontmatter."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = _lines(fh)
            if next(lines, "").strip() != "---":
                return None
            for _, line in zip(range(MAX_FRONTMATTER_LINES), lines, strict=False):
                if line.strip() == "---":
                    return None
                if line.startswith("model:"):
                    return line[len("model:") :].strip().strip("'\"") or None
    except OSError:
        return None
    return None


def _suggested(change: str | None, key: str) -> str | None:
    """The value of the `key:` line of a finding's change text."""
    for line in (change or "").splitlines():
        if line.startswith(f"{key}:"):
            return line[len(key) + 1 :].strip()
    return None


def _matches(finding: Finding, d: AgentDefinition) -> str | None:
    """The detail line when definition `d` now holds the finding's suggested line."""
    if finding.kind == "model_mix":
        want = _suggested(finding.change, "model") or "sonnet"
        have = read_model(d.path)
        if have is not None and want.lower() in have.lower():
            return f"model: {have}"
        return None
    want_tools = _suggested(finding.change, "tools")
    if want_tools is None or d.tools is None:
        return None
    if set(_split(want_tools)) == set(d.tools):
        return f"tools: {', '.join(d.tools)}"
    return None


def _default_dirs(index: Index) -> list[Path]:
    from capcom.checks import default_agent_dirs

    cwds = sorted({s.cwd for s in index.sessions() if s.cwd})
    return default_agent_dirs(cwds)


def noticed(
    index: Index,
    table: PriceTable,
    findings: Sequence[Finding],
    now: datetime,
    *,
    since: datetime | None = None,
    agent_dirs: Sequence[Path] | None = None,
    marks: Sequence[ChangeMark] | None = None,
    path: Path | None = None,
) -> list[NoticedChange]:
    """Definitions changed since `since` (default: the checks' 14-day window before
    `now`) that now carry a finding's suggested line; never a finding with an open
    mark (`marks`, default: the marks file at `path`)."""
    since = since or now - DEFAULT_WINDOW
    marked = {(m.kind, m.scope) for m in (load(path) if marks is None else marks)}
    wanted = [f for f in findings if f.kind in KINDS and (f.kind, f.scope) not in marked]
    if not wanted:
        return []
    defs = find_all_definitions(_default_dirs(index) if agent_dirs is None else agent_dirs)
    out: list[NoticedChange] = []
    seen: set[tuple[str, str]] = set()
    for f in wanted:
        for d in defs.get(f.scope, []):
            try:
                mtime = datetime.fromtimestamp(d.path.stat().st_mtime, UTC)
            except OSError:
                continue
            if mtime < since or (f.kind, f.scope) in seen:
                continue
            detail = _matches(f, d)
            if detail is None:
                continue
            seen.add((f.kind, f.scope))
            out.append(NoticedChange(f.kind, f.scope, f.title, d.path, detail, mtime))
    return out
