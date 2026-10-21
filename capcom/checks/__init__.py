"""capcom.checks: setup warnings drawn from real usage (contract: capcom.types.ChecksModule).

`run_checks` makes one pass over the window (facts.py) and runs every check in
rules.py on what it collected; each check's docstring gives its saving formula.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from capcom.checks.agents import find_all_definitions
from capcom.checks.facts import collect
from capcom.checks.rules import all_findings
from capcom.types import Finding, Index, PriceTable


def default_agent_dirs(cwds: Sequence[str], claude_root: Path | None = None) -> list[Path]:
    """Where agent definitions live: each project's .claude/agents, then the user's
    (`claude_root`, default from capcom's config: $CLAUDE_CONFIG_DIR or ~/.claude)."""
    if claude_root is None:
        from capcom.config import load_config

        claude_root = load_config().claude_root
    return [*(Path(c) / ".claude" / "agents" for c in cwds), claude_root / "agents"]


def run_checks(
    index: Index,
    table: PriceTable,
    since: datetime,
    until: datetime,
    *,
    agent_dirs: Sequence[Path] | None = None,
    claude_root: Path | None = None,
    openclaw_root: Path | None = None,
) -> list[Finding]:
    """Findings over [since, until), ranked by saving_usd_per_week, largest first.

    `agent_dirs` are the folders whose `*.md` agent definitions are read (their
    `tools:` line only); None reads each project's .claude/agents and
    `<claude_root>/agents`. Projects under `openclaw_root` (default from capcom's
    config) are OpenClaw workspaces, whose tools never go in a pasted `tools:` line.
    """
    facts = collect(index, table, since, until)
    if claude_root is None or openclaw_root is None:
        from capcom.config import load_config

        config = load_config()
        claude_root = claude_root or config.claude_root
        openclaw_root = openclaw_root or config.openclaw_root
    if agent_dirs is None:
        cwds = sorted({c for p in facts.project_cwd for c in facts.project_cwd[p]})
        agent_dirs = default_agent_dirs(cwds, claude_root)
    user_dir = claude_root / "agents"
    return all_findings(facts, find_all_definitions(agent_dirs), openclaw_root, user_dir)


__all__ = ["run_checks"]
