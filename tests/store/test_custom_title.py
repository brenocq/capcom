"""A session the user renamed (`/rename`, `claude -n`): its custom-title wins over
Claude Code's ai-title in either order, across batches and incremental scans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capcom.config import Config
from capcom.sources.claude_code import ClaudeCodeSource
from capcom.store.index import open_index
from capcom.store.scan import run_scan


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / ".claude",
        tmp_path / ".openclaw",
        tmp_path / "cache" / "capcom",
        tmp_path / "state",
    )


def _log(config: Config, sid: str = "s1") -> Path:
    path = config.projects_dir / "-home-u-p" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(path: Path, *recs: dict[str, Any]) -> None:
    with path.open("a") as f:
        for r in recs:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _user(n: int, sid: str = "s1") -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"u{n}",
        "sessionId": sid,
        "timestamp": f"2026-10-20T09:00:{n:02d}Z",
        "cwd": "/home/u/p",
        "message": {"role": "user", "content": "hi"},
    }


def _ai(title: str, sid: str = "s1") -> dict[str, Any]:
    return {"type": "ai-title", "aiTitle": title, "sessionId": sid}


def _custom(title: str, sid: str = "s1") -> dict[str, Any]:
    return {"type": "custom-title", "customTitle": title, "sessionId": sid}


def _titles(config: Config) -> dict[str, tuple[str | None, str | None]]:
    run_scan(config, quiet=True, source=ClaudeCodeSource())
    with open_index(config.index_path, readonly=True) as ix:
        return {s.session_id: (s.title, s.custom_title) for s in ix.sessions()}


def test_custom_title_only(config: Config) -> None:
    _append(_log(config), _user(1), _custom("my name"))
    assert _titles(config) == {"s1": ("my name", "my name")}


def test_ai_title_then_custom(config: Config) -> None:
    _append(_log(config), _user(1), _ai("generated"), _custom("my name"))
    assert _titles(config) == {"s1": ("my name", "my name")}


def test_custom_then_ai_title(config: Config) -> None:
    # Claude Code appends both again and again, in this order too.
    _append(_log(config), _user(1), _custom("my name"), _ai("generated"), _user(2))
    assert _titles(config) == {"s1": ("my name", "my name")}


def test_renamed_twice(config: Config) -> None:
    _append(_log(config), _user(1), _custom("first"), _ai("generated"), _custom("second"))
    assert _titles(config) == {"s1": ("second", "second")}


def test_across_incremental_scans(config: Config) -> None:
    log = _log(config)
    _append(log, _user(1), _ai("generated"))
    assert _titles(config) == {"s1": ("generated", None)}
    _append(log, _custom("mine"), _user(2))
    assert _titles(config) == {"s1": ("mine", "mine")}
    _append(log, _ai("generated again"), _user(3))  # a later ai-title does not win
    assert _titles(config) == {"s1": ("mine", "mine")}
    _append(log, _custom("renamed"))  # a later custom title does
    assert _titles(config) == {"s1": ("renamed", "renamed")}
    _append(log, _custom(""))  # cleared: back to Claude Code's
    assert _titles(config) == {"s1": ("generated again", None)}


def test_other_sessions_keep_their_ai_title(config: Config) -> None:
    _append(_log(config), _user(1), _custom("mine"))
    _append(_log(config, "s2"), _user(1, "s2"), _ai("generated", "s2"))
    assert _titles(config) == {"s1": ("mine", "mine"), "s2": ("generated", None)}
