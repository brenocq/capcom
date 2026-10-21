"""The `name:` and `tools:` lines of agent definition files, read-only.

An agent definition is a Markdown file with YAML frontmatter, in
`~/.claude/agents/*.md` or `<project>/.claude/agents/*.md`; the agent type is the
frontmatter's `name:` (the file's stem when it has none). Only `name:` and `tools:`
are read (`tools:` as an inline list, `tools: Read, Grep`, or a block list of
`- Read` lines); nothing else in the file is kept, and nothing is written.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

MAX_FRONTMATTER_LINES = 80
MAX_LINE_CHARS = 4096  # a longer line is cut here, and the rest of it skipped
MAX_SKIP_CHARS = 1 << 20  # stop reading a file whose line runs on past this


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_type: str
    path: Path
    tools: tuple[str, ...] | None  # None: no `tools:` entry (the agent gets every tool)


def _split(value: str) -> list[str]:
    value = value.strip().strip("[]")
    return [p.strip().strip("'\"") for p in value.split(",") if p.strip().strip("'\"")]


def _lines(fh: TextIO) -> Iterator[str]:
    """The file's lines, each cut at MAX_LINE_CHARS (the rest of a long line is
    skipped, not read as a line of its own); ends early on a runaway line."""
    while True:
        line = fh.readline(MAX_LINE_CHARS)
        if not line:
            return
        if len(line) == MAX_LINE_CHARS and not line.endswith("\n"):
            skipped = 0
            while True:
                rest = fh.readline(MAX_LINE_CHARS)
                skipped += len(rest)
                if not rest or rest.endswith("\n"):
                    break
                if skipped > MAX_SKIP_CHARS:
                    return
        yield line


def read_frontmatter(path: Path) -> tuple[str | None, tuple[str, ...] | None]:
    """(name, tools) from a definition's frontmatter: the `name:` value, or None;
    the tools it lists, or None when it lists none (or every tool, `*`). Both None
    when the file cannot be read. Reads at most the frontmatter."""
    name: str | None = None
    tools: list[str] | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = _lines(fh)
            first = next(lines, "")
            if first.strip() != "---":
                return None, None
            in_block = False
            for _, line in zip(range(MAX_FRONTMATTER_LINES), lines, strict=False):
                if line.strip() == "---":
                    break
                if in_block:
                    stripped = line.strip()
                    if stripped.startswith("- "):
                        assert tools is not None
                        tools.extend(_split(stripped[2:]))
                        continue
                    in_block = False
                if line.startswith("tools:"):
                    tools = _split(line[len("tools:") :])
                    in_block = not tools
                elif line.startswith("name:"):
                    name = line[len("name:") :].strip().strip("'\"") or None
    except OSError:
        return None, None
    if not tools or tools == ["*"]:
        return name, None
    return name, tuple(tools)


def read_tools(path: Path) -> tuple[str, ...] | None:
    """The tools a definition file lists, or None when it lists none (or cannot be
    read). Reads at most the frontmatter."""
    return read_frontmatter(path)[1]


def find_definitions(dirs: Iterable[Path]) -> dict[str, AgentDefinition]:
    """Agent type -> definition, from the `*.md` files directly in `dirs`, keyed by
    the frontmatter's `name:` (the file's stem without one). A type defined in
    several folders keeps the first one found (list project folders before the user
    folder)."""
    out: dict[str, AgentDefinition] = {}
    for folder in dirs:
        try:
            files = sorted(p for p in folder.glob("*.md") if p.is_file())
        except OSError:
            continue
        for path in files:
            name, tools = read_frontmatter(path)
            kind = name or path.stem
            if kind not in out:
                out[kind] = AgentDefinition(kind, path, tools)
    return out
