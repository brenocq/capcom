"""The `tools:` line of agent definition files, read-only.

An agent definition is a Markdown file with YAML frontmatter, in
`~/.claude/agents/*.md` or `<project>/.claude/agents/*.md`; the agent type is the
file's stem. Only the frontmatter's `tools:` entry is read (an inline list,
`tools: Read, Grep`, or a block list of `- Read` lines); nothing else in the file is
kept, and nothing is written.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MAX_FRONTMATTER_LINES = 80


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_type: str
    path: Path
    tools: tuple[str, ...] | None  # None: no `tools:` entry (the agent gets every tool)


def _split(value: str) -> list[str]:
    value = value.strip().strip("[]")
    return [p.strip().strip("'\"") for p in value.split(",") if p.strip().strip("'\"")]


def read_tools(path: Path) -> tuple[str, ...] | None:
    """The tools a definition file lists, or None when it lists none (or cannot be
    read). Reads at most the frontmatter."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return None
            tools: list[str] | None = None
            in_block = False
            for _ in range(MAX_FRONTMATTER_LINES):
                line = fh.readline()
                if not line or line.strip() == "---":
                    break
                if in_block:
                    stripped = line.strip()
                    if stripped.startswith("- "):
                        assert tools is not None
                        tools.extend(_split(stripped[2:]))
                        continue
                    in_block = False
                if line.startswith("tools:"):
                    rest = line[len("tools:") :]
                    tools = _split(rest)
                    in_block = not tools
    except OSError:
        return None
    if not tools or tools == ["*"]:
        return None
    return tuple(tools)


def find_definitions(dirs: Iterable[Path]) -> dict[str, AgentDefinition]:
    """Agent type -> definition, from the `*.md` files directly in `dirs`. A type
    defined in several folders keeps the first one found (list project folders
    before the user folder)."""
    out: dict[str, AgentDefinition] = {}
    for folder in dirs:
        try:
            files = sorted(p for p in folder.glob("*.md") if p.is_file())
        except OSError:
            continue
        for path in files:
            if path.stem in out:
                continue
            out[path.stem] = AgentDefinition(path.stem, path, read_tools(path))
    return out
