# ruff: noqa: E501  (the fixtures and stub keep their lines whole)
"""Ask Roger's lookups as an MCP server (capcom/report/asktools.py): the JSON-RPC
it speaks, each tool's answer, the cap on calls, and the real process over a
synthetic index, as the CLI starts it. All data here is made up."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from capcom.report import asktools
from capcom.report.api import Context
from capcom.report.askfacts import Facts
from capcom.store.index import open_index
from tests.report import test_askfacts
from tests.report.test_askfacts import NOW, S_A, TABLE, World, analysis, no_secrets

world = test_askfacts.world  # the synthetic world, outside /tmp


@pytest.fixture
def server(world: World) -> asktools.Server:
    def make() -> Facts:
        ix = open_index(world.index_path, readonly=True)
        return Facts(Context(ix, TABLE, analysis(), NOW, tz=UTC, index_path=world.index_path))

    return asktools.Server(make)


def call(server: asktools.Server, name: str, args: Any = None) -> tuple[Any, bool]:
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    assert reply is not None
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


def test_initialize_list_and_notifications(server: asktools.Server) -> None:
    init = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    assert init is not None
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed is not None
    names = [t["name"] for t in listed["result"]["tools"]]
    assert names == ["spend", "session", "findings", "where_defined"]
    for t in listed["result"]["tools"]:
        assert (
            t["inputSchema"]["type"] == "object"
            and t["inputSchema"]["additionalProperties"] is False
        )
    unknown = server.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert unknown is not None and unknown["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {},
    }


def test_each_tool_answers_with_aggregates_ids_and_paths(
    server: asktools.Server, world: World
) -> None:
    spend, err = call(server, "spend", {"group_by": "project", "model": "opus"})
    assert not err and {r["project"] for r in spend["rows"]} == {"proj-a", "proj-b"}
    session, err = call(server, "session", {"session_id": S_A})
    assert not err and session["project"] == "proj-a"
    found, err = call(server, "findings", {"days": 7})
    assert not err and found["findings"][0]["kind"] == "model_mix"
    where, err = call(server, "where_defined", {"name": "fixer", "kind": "agent"})
    assert not err and where["defined_in"][0]["path"] == str(
        world.proj_a / ".claude/agents/fixer.md"
    )
    no_secrets([spend, session, found, where])


def test_bad_arguments_and_unknown_tools_are_errors_not_crashes(server: asktools.Server) -> None:
    out, err = call(server, "spend", {"group_by": "nope"})
    assert err and "group_by" in out["error"]
    out, err = call(server, "Bash", {"command": "ls"})
    assert err and out["error"] == "No such tool."
    out, err = call(server, "spend", ["not", "an", "object"])
    assert err


def test_calls_are_capped_per_question(server: asktools.Server) -> None:
    for _ in range(asktools.MAX_CALLS):
        _, err = call(server, "spend", {"group_by": "model"})
        assert not err
    out, err = call(server, "spend", {"group_by": "model"})
    assert err and "answer with what you know" in out["error"]


def test_a_broken_index_says_so_without_a_traceback(tmp_path: Path) -> None:
    def make() -> Facts:
        raise RuntimeError("/secret/path/in/a/traceback")

    out, err = call(asktools.Server(make), "spend", {"group_by": "model"})
    assert err and out == {"error": "capcom could not look that up."}


def test_long_answers_are_cut() -> None:
    out = asktools._content({"rows": ["x" * 100] * 200})
    assert len(out["content"][0]["text"]) <= asktools.MAX_RESULT


def test_mcp_config_names_only_this_server(world: World) -> None:
    args = asktools.arguments(world.index_path, NOW, UTC, world.changes)
    cfg = json.loads(asktools.mcp_config(args))
    assert list(cfg["mcpServers"]) == ["capcom"]
    srv = cfg["mcpServers"]["capcom"]
    assert srv["type"] == "stdio" and srv["command"] == sys.executable
    assert srv["args"][:3] == ["-I", "-m", "capcom.report.asktools"]
    assert srv["args"][3:] == [
        "--index", str(world.index_path), "--now", NOW.isoformat(), "--tz", "UTC", "--changes", str(world.changes),
    ]  # fmt: skip
    assert asktools.allowed() == (
        "mcp__capcom__spend,mcp__capcom__session,mcp__capcom__findings,mcp__capcom__where_defined"
    )


def test_the_process_as_the_cli_starts_it(world: World) -> None:
    """`python -I -m capcom.report.asktools` over stdio, on the synthetic index, with
    the real analysis modules: initialize, list, a lookup, then stdin closes."""
    cfg = json.loads(asktools.mcp_config(asktools.arguments(world.index_path, NOW, UTC)))
    srv = cfg["mcpServers"]["capcom"]
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "spend", "arguments": {"group_by": "agent_type"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "where_defined", "arguments": {"name": "fixer"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "findings", "arguments": {}}},
    ]  # fmt: skip
    env = os.environ | {"CLAUDE_CONFIG_DIR": str(world.claude_root), "HOME": str(world.home)}
    done = subprocess.run(
        [srv["command"], *srv["args"]],
        input="".join(json.dumps(m) + "\n" for m in msgs) + "not json\n",
        capture_output=True, text=True, timeout=60, env=env, cwd=world.home, check=True,
    )  # fmt: skip
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, 4, 5, None]
    assert replies[-1]["error"]["code"] == -32700
    spend = json.loads(replies[2]["result"]["content"][0]["text"])
    assert {r["agent_type"] for r in spend["rows"]} == {"main", "fixer"}
    where = json.loads(replies[3]["result"]["content"][0]["text"])
    assert where["defined_in"][0]["path"].endswith("proj-a/.claude/agents/fixer.md")
    assert "error" not in replies[4] and "result" in replies[4]  # the real checks ran
    no_secrets(done.stdout)
