"""The agent timeline: steps, phases and per-step cost of one thread, from synthetic
Claude Code logs scanned by the real source, and the /api/agent reply."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from capcom.config import Config
from capcom.report import agent as thread
from capcom.report.server import Site, background_scan
from tests.fixtures.generate import T0, Reply, SessionWriter, at
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve

CWD = "/home/user/proj"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache" / "capcom", tmp_path / "st"
    )


def client_for(config: Config) -> Iterator[Client]:
    config.projects_dir.mkdir(parents=True, exist_ok=True)
    background_scan(config)
    yield from serve(Site(config.index_path, TABLE, FAKE, clock=lambda: NOW))


def step(
    w: SessionWriter, t: float, tools: list[tuple[str, dict[str, Any]]], **kw: Any
) -> list[str]:
    w.assistant(Reply(at(t), tools=tools, **kw))
    return list(w.last_tool_ids)


def builder_session(root: Path) -> tuple[str, str]:
    """A main thread that spawns one builder, which reads, edits, tests and commits
    without a to-do list."""
    main = SessionWriter(root, CWD, "sess-a")
    main.user_prompt(at(0))
    (spawn,) = step(main, 5, [("Agent", {"subagent_type": "builder", "prompt": "x"})])
    sub = main.subagent("builder", "agent-timeline", tool_use_id=spawn)
    sub.user_prompt(at(5), "task")
    (r1,) = step(sub, 20, [("Read", {"file_path": "/p/a.py"})], cache_read=10_000)
    sub.tool_result(r1, at(22))
    (e1,) = step(sub, 30, [("Edit", {"file_path": "/p/a.py"})], cache_read=12_000)
    sub.tool_result(e1, at(31))
    (b1,) = step(sub, 40, [("Bash", {"command": "cd /p && uv run pytest -q"})], cache_read=13_000)
    sub.tool_result(b1, at(400))
    (b2,) = step(
        sub, 410, [("Bash", {"command": "git commit -m 'secret words'"})], cache_read=90_000
    )
    sub.tool_result(b2, at(412))
    step(sub, 420, [], cache_read=91_000, text="done")
    main.tool_result(spawn, at(425))
    step(main, 430, [])
    return "sess-a", sub.agent_id or ""


def test_command_labels_keep_only_the_program_and_plain_words() -> None:
    assert thread.command_label("cd /p && uv run pytest -q tests/") == "uv run pytest"
    assert thread.command_label("git commit -m 'fix: secret'") == "git commit"
    assert thread.command_label("FOO=1 timeout 60 python3 -m pytest") == "python3 … pytest"
    assert thread.command_label("cat <<'EOF' > x\npytest\nEOF") == "cat"
    assert thread.command_kind("cd /p && uv run pytest")[0] == "test"
    assert thread.command_kind("cargo build --release")[0] == "test"
    assert thread.command_kind("cargo run")[0] == "other"
    assert thread.command_kind("grep -rn foo . | head")[0] == "files"
    assert thread.command_kind("node shot.mjs --dark")[0] == "shot"
    assert thread.command_kind("npx playwright test")[0] == "test"


def test_steps_and_costs_of_a_subagent_without_a_todo_list(config: Config) -> None:
    sid, aid = builder_session(config.claude_root)
    for c in client_for(config):
        status, d, _ = c.get(f"/api/agent/{sid}?agent={aid}")
    assert status == 200, d
    st = d["steps"]
    kinds = list(zip(st["kind"], st["short"], strict=True))
    assert kinds == [
        ("think", "thinking"),
        ("files", "a.py"),
        ("think", "thinking"),
        ("files", "a.py"),
        ("think", "thinking"),
        ("test", "pytest"),
        ("think", "thinking"),
        ("other", "git commit"),
        ("think", "thinking"),
    ]
    ms0 = int(T0.timestamp() * 1000)
    assert st["start"][0] == ms0 + 5000  # from the spawn
    assert (st["start"][5], st["end"][5]) == (ms0 + 40_000, ms0 + 400_000)
    assert "secret" not in str(d)
    # a tool step carries the request that read its result; the first request its thinking
    usd = d["requests"]["usd"]
    assert st["usd"][0] == pytest.approx(usd[0])
    assert st["usd"][5] == pytest.approx(usd[3])
    assert st["next"][5] == 3
    assert st["usd"][7] == pytest.approx(usd[4])
    assert st["usd"][2] == 0 and st["usd"][8] == 0
    # a thinking step whose request was counted on a tool step is marked, not shown as $0.00
    assert [i for i, m in enumerate(st["moved"]) if m] == [2, 4, 6, 8]
    assert sum(st["usd"]) == pytest.approx(sum(usd))
    t = d["thread"]
    assert t["name"] == "agent-timeline" and t["agent_type"] == "builder"
    assert t["spawned"] == ms0 + 5000 and t["spawned_by"]["agent_id"] == "main"
    assert t["alive_seconds"] == pytest.approx(415)
    assert t["tool_seconds"] == pytest.approx(2 + 1 + 360 + 2)
    assert t["model_seconds"] == pytest.approx(415 - 365)
    assert d["neighbors"]["prev"]["agent_id"] == "main" and d["neighbors"]["next"] is None
    # no to-do list: the phases are guessed from what each stretch touched
    assert not d["todo"]
    names = [p["name"] for p in d["phases"]]
    assert all(p["guessed"] for p in d["phases"])
    assert any("a.py" in n for n in names) and any("pytest" in n for n in names)
    kinds_row = {k["key"]: k for k in d["kinds"]}
    # the thinking row would read $0.00 (its requests are costed on tool steps): a dash
    assert kinds_row["think"]["moved"] is True and kinds_row["test"]["moved"] is False
    assert kinds_row["test"]["seconds"] == pytest.approx(360)


def test_the_session_page_lists_each_agent_life(config: Config) -> None:
    sid, aid = builder_session(config.claude_root)
    for c in client_for(config):
        status, d, _ = c.get(f"/api/session/{sid}")
        missing, _, _ = c.get(f"/api/agent/{sid}?agent=nope")
        main_status, main, _ = c.get(f"/api/agent/{sid}?agent=main")
    assert status == 200 and missing == 404 and main_status == 200
    (a,) = d["agents"]
    assert a["agent_id"] == aid
    assert a["alive_seconds"] == pytest.approx(415)
    assert a["tool_seconds"] == pytest.approx(365)
    assert main["thread"]["is_main"] and main["neighbors"]["next"]["agent_id"] == aid
    # the Agent call waits for the subagent: one step of the main thread
    assert "Agent builder" in main["steps"]["label"]


def task_result(w: SessionWriter, tid: str, t: float, task_id: str) -> None:
    rec = w._common(at(t))
    rec.update(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tid, "content": "Task created"}],
            },
            "toolUseResult": {"task": {"id": task_id, "subject": "x"}},
        }
    )
    w.write(rec)


def test_phases_follow_the_task_list_of_the_main_thread(config: Config) -> None:
    main = SessionWriter(config.claude_root, CWD, "sess-b")
    main.user_prompt(at(0))
    ids = step(
        main,
        10,
        [
            ("TaskCreate", {"subject": "Write tests", "activeForm": "Writing tests"}),
            ("TaskCreate", {"subject": "Fix the bug", "activeForm": "Fixing the bug"}),
        ],
    )
    task_result(main, ids[0], 11, "7")
    task_result(main, ids[1], 11, "8")
    (u,) = step(main, 20, [("TaskUpdate", {"taskId": "7", "status": "in_progress"})])
    main.tool_result(u, at(21))
    (r,) = step(main, 30, [("Read", {"file_path": "/p/test_a.py"})])
    main.tool_result(r, at(90))
    u, u2 = step(
        main,
        100,
        [
            ("TaskUpdate", {"taskId": "7", "status": "completed"}),
            ("TaskUpdate", {"taskId": "8", "status": "in_progress"}),
        ],
    )
    main.tool_result(u, at(101))
    main.tool_result(u2, at(101))
    (b,) = step(main, 110, [("Bash", {"command": "make test"})])
    main.tool_result(b, at(300))
    step(main, 310, [])
    main.user_prompt(at(2000))  # the user came back half an hour later
    step(main, 2010, [])
    for c in client_for(config):
        status, d, _ = c.get("/api/agent/sess-b?agent=main")
    assert status == 200, d
    assert d["todo"]
    named = [(p["name"], p["guessed"]) for p in d["phases"]]
    assert ("Writing tests", False) in named and ("Fixing the bug", False) in named
    fix = next(p for p in d["phases"] if p["name"] == "Fixing the bug")
    assert fix["kinds"]["test"] == pytest.approx(190)
    ms0 = int(T0.timestamp() * 1000)
    assert d["idle"] == [[ms0 + 310_000, ms0 + 2_000_000]]
    assert d["thread"]["idle_seconds"] == pytest.approx(1690)


def test_todo_write_snapshots_open_and_close_phases() -> None:
    t = [T0 + timedelta(seconds=s) for s in range(0, 100, 10)]
    ev = [
        thread.TodoEvent(
            "a",
            t[0],
            "snapshot",
            items=[("x", "Doing x", "in_progress"), ("y", "Doing y", "pending")],
        ),
        thread.TodoEvent(
            "b",
            t[3],
            "snapshot",
            items=[("x", "Doing x", "completed"), ("y", "Doing y", "in_progress")],
        ),
        thread.TodoEvent("c", t[5], "snapshot", items=[("x", "Doing x", "completed")]),
    ]
    got = thread.todo_intervals(ev, {"b": t[4]})
    assert got == [(t[0], t[4], "Doing x", "x"), (t[4], t[5], "Doing y", "y")]


def test_the_extras_skip_a_partial_last_line_and_other_threads(tmp_path: Path) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text(
        '{"type":"user","timestamp":"2026-10-20T09:00:00Z","message":{"content":"hi"}}\n'
        '{"type":"user","isSidechain":true,"agentId":"z","timestamp":"2026-10-20T09:01:00Z",'
        '"message":{"content":"sub"}}\n'
        '{"type":"user","timestamp":"2026-10-20T09:02:00Z","message":{"content":"hal',
        encoding="utf-8",
    )
    got = thread.read_extras([log, tmp_path / "gone.jsonl"], None)
    assert got.prompts == [T0]
