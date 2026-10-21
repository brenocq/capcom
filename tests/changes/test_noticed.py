"""Agent definitions that now carry a finding's suggested line."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capcom.changes import noticed
from capcom.cost.prices import load_prices
from capcom.types import ChangeMark, Finding
from tests.checks.helpers import SortedIndex

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
TABLE = load_prices()


def _def(folder: Path, name: str, lines: list[str], when: datetime = NOW) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.md"
    p.write_text("\n".join(["---", f"name: {name}", *lines, "---", "Synthetic body."]) + "\n")
    os.utime(p, (when.timestamp(), when.timestamp()))
    return p


def _finding(kind: str, scope: str, change: str) -> Finding:
    return Finding(kind, f"About {scope}", scope, "s", 3.0, "a", change=change)


def test_a_model_line_now_set_to_the_suggested_model_is_noticed(tmp_path: Path) -> None:
    d = tmp_path / "agents"
    path = _def(d, "worker", ["model: sonnet"], NOW - timedelta(days=2))
    _def(d, "other", ["model: opus"], NOW - timedelta(days=2))
    found = noticed(
        SortedIndex(),
        TABLE,
        [
            _finding("model_mix", "worker", "model: sonnet"),
            _finding("model_mix", "other", "model: sonnet"),
        ],
        NOW,
        agent_dirs=[d],
        marks=[],
    )
    assert [(n.kind, n.scope, n.path, n.detail) for n in found] == [
        ("model_mix", "worker", path, "model: sonnet")
    ]
    assert found[0].since == NOW - timedelta(days=2)


def test_a_tools_line_matching_the_suggestion_is_noticed_in_any_order(tmp_path: Path) -> None:
    d = tmp_path / "agents"
    _def(d, "reviewer", ["tools: Grep, Read"])
    change = "# a restriction: ...\ntools: Read, Grep"
    found = noticed(
        SortedIndex(),
        TABLE,
        [_finding("agent_tools_unused", "reviewer", change)],
        NOW,
        agent_dirs=[d],
        marks=[],
    )
    assert [n.detail for n in found] == ["tools: Grep, Read"]
    _def(d, "reviewer", ["tools: Grep, Read, Bash"])
    assert not noticed(
        SortedIndex(),
        TABLE,
        [_finding("agent_tools_unused", "reviewer", change)],
        NOW,
        agent_dirs=[d],
        marks=[],
    )


def test_a_file_unchanged_since_the_window_began_or_already_marked_is_not_noticed(
    tmp_path: Path,
) -> None:
    d = tmp_path / "agents"
    _def(d, "worker", ["model: sonnet"], NOW - timedelta(days=30))
    f = _finding("model_mix", "worker", "model: sonnet")
    assert not noticed(SortedIndex(), TABLE, [f], NOW, agent_dirs=[d], marks=[])
    _def(d, "worker", ["model: sonnet"], NOW - timedelta(days=1))
    assert noticed(SortedIndex(), TABLE, [f], NOW, agent_dirs=[d], marks=[])
    m = ChangeMark(4, "model_mix", "worker", NOW.date(), "t")
    assert not noticed(SortedIndex(), TABLE, [f], NOW, agent_dirs=[d], marks=[m])


def test_other_kinds_are_never_noticed(tmp_path: Path) -> None:
    d = tmp_path / "agents"
    _def(d, "worker", ["model: sonnet"])
    f = _finding("skills_unused", "worker", "model: sonnet")
    assert not noticed(SortedIndex(), TABLE, [f], NOW, agent_dirs=[d], marks=[])


def test_a_bom_and_an_indented_model_line_are_read(tmp_path: Path) -> None:
    d = tmp_path / "agents"
    d.mkdir()
    p = d / "worker.md"
    p.write_bytes("﻿---\nname: worker\n  model: sonnet  \n---\nBody.\n".encode())
    os.utime(p, (NOW.timestamp(), NOW.timestamp()))
    f = _finding("model_mix", "worker", "model: sonnet")
    found = noticed(SortedIndex(), TABLE, [f], NOW, agent_dirs=[d], marks=[])
    assert [n.detail for n in found] == ["model: sonnet"]
