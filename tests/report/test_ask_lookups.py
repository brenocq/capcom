# ruff: noqa: E501  (the fixtures and stub keep their lines whole)
"""Ask Claude with what stands behind a part and capcom's lookups (ask.py, server.py):
the references a page may send, the facts in the prompt, the CLI's arguments (only
capcom's tools), the "looking up" lines, and an end-to-end run with a stub `claude`
that starts capcom's MCP server from its --mcp-config and uses a tool, as the real
CLI does. All data here is made up."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from capcom.report import ask
from capcom.report.server import Site
from tests.report import test_askfacts
from tests.report.test_ask import Server, runs, wait_run
from tests.report.test_askfacts import NOW, S_A, SECRETS, TABLE, World, analysis

# A stub CLI: records its run, then, as Claude Code does with --mcp-config, starts
# the one server named there, initializes it, makes the lookup STUB_LOOKUP names,
# says so in stream-json (an assistant message with a tool_use) and answers with
# what the lookup returned.
STUB = r"""#!{python}
import json, os, subprocess, sys, time
argv = sys.argv[1:]
prompt = sys.stdin.read()
out = os.environ["FAKE_CLAUDE_DIR"]
with open(os.path.join(out, f"run-{time.time_ns():020d}-{os.getpid()}.json"), "w") as f:
    json.dump({"argv": argv, "prompt": prompt}, f)
def say(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
def delta(text):
    say({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}}})
cfg = json.loads(argv[argv.index("--mcp-config") + 1])
srv = cfg["mcpServers"]["capcom"]
p = subprocess.Popen([srv["command"], *srv["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def rpc(i, method, params=None):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())
rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
tool, args = json.loads(os.environ["STUB_LOOKUP"])
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "tu1", "name": "mcp__capcom__" + tool, "input": args}]}})
got = json.loads(rpc(3, "tools/call", {"name": tool, "arguments": args})["result"]["content"][0]["text"])
p.stdin.close(); p.wait()
delta("Tools: " + ",".join(names) + ". ")
delta("Found: " + json.dumps(got))
delta("\nSUGGESTIONS:\n- Who spawned it?")
say({"type": "result", "subtype": "success", "is_error": False})
"""

world = test_askfacts.world  # the synthetic world, outside /tmp


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(STUB.replace("{python}", sys.executable))
    exe.chmod(0o755)
    out = tmp_path / "runs"
    out.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(out))
    monkeypatch.setenv("STUB_LOOKUP", json.dumps(["where_defined", {"name": "fixer"}]))
    return out


@pytest.fixture
def server(world: World, stub: Path) -> Iterator[Server]:
    s = Server(Site(world.index_path, TABLE, analysis(), clock=lambda: NOW, tz=UTC))
    yield s
    s.close()


BODY: dict[str, Any] = {
    "page": "Did it work?",
    "part": {
        "label": "Did it work: The fixer agent runs on Opus",
        "text": "Before 12 per 1,000 requests, after 3",
        "ref": {"kind": "mark", "id": 7},
    },
    "question": "Where is this fixer agent from?",
}


# --------------------------------------------------------------------------- end to end


def test_the_cli_gets_the_facts_and_looks_up_with_capcoms_tools_only(
    server: Server, stub: Path, world: World
) -> None:
    status, lines = server.ask(BODY)
    assert status == 200
    tools = [x for x in lines if x["type"] == "tool"]
    assert tools == [
        {"type": "tool", "tool": "where_defined", "label": "Looking up where it is defined…",
         "done": "Looked up where it is defined"}
    ]  # fmt: skip
    text = "".join(x["text"] for x in lines if x["type"] == "text")
    assert "Tools: spend,session,findings,where_defined." in text
    assert str(world.proj_a / ".claude/agents/fixer.md") in text
    assert lines[-1] == {"type": "done", "suggestions": ["Who spawned it?"]}
    run = wait_run(stub)
    # the prompt: the part, then what stands behind it from the index
    prompt = run["prompt"]
    assert "<facts>" in prompt and "marked applied on 2026-10-15" in prompt
    assert "fixer.md (a project agent, in proj-a; the one used in proj-a)" in prompt
    for s in SECRETS:
        assert s not in prompt and s not in text
    argv = run["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == (
        "mcp__capcom__spend,mcp__capcom__session,mcp__capcom__findings,mcp__capcom__where_defined"
    )
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-turns") + 1] == str(ask.MAX_TURNS_CLI)
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_a_spend_lookup_by_project(
    server: Server, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "STUB_LOOKUP", json.dumps(["spend", {"group_by": "project", "model": "opus"}])
    )
    body = {"page": "Did it work?", "question": "Which projects use the most Opus requests?"}
    _, lines = server.ask(body)
    assert [x["label"] for x in lines if x["type"] == "tool"] == [
        "Looking up the spend by project…"
    ]
    text = "".join(x["text"] for x in lines if x["type"] == "text")
    assert '"project": "proj-a"' in text and '"project": "proj-b"' in text
    assert "<facts>" not in runs(stub)[0]["prompt"]  # no part, no reference: no facts


def test_without_tools_nothing_is_added(world: World, stub: Path) -> None:
    site = Site(world.index_path, TABLE, analysis(), clock=lambda: NOW, tz=UTC, ask_tools=False)
    s = Server(site)
    try:
        s.ask(BODY)
    finally:
        s.close()
    run = wait_run(stub)
    assert "--mcp-config" not in run["argv"] and "--allowedTools" not in run["argv"]
    assert "<facts>" not in run["prompt"]


# --------------------------------------------------------------------------- the pieces


def test_the_command_allows_only_capcoms_tools() -> None:
    plain = ask.command("claude")
    assert "--mcp-config" not in plain and "--allowedTools" not in plain
    assert plain[plain.index("--system-prompt") + 1] == ask.SYSTEM
    cfg = '{"mcpServers":{"capcom":{}}}'
    argv = ask.command("claude", cfg)
    assert argv[argv.index("--mcp-config") + 1] == cfg
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed and all(t.startswith("mcp__capcom__") for t in allowed)
    assert argv[argv.index("--tools") + 1] == ""
    assert "Bash" in argv[argv.index("--disallowedTools") + 1]
    assert argv[argv.index("--system-prompt") + 1] == ask.SYSTEM + ask.SYSTEM_TOOLS
    # every option has its value right after it: the config cannot swallow a flag
    assert argv[argv.index("--mcp-config") + 2].startswith("--")


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ({"kind": "finding", "finding": "model_mix", "scope": "fixer", "days": 14, "x": 1},
         {"kind": "finding", "finding": "model_mix", "scope": "fixer", "days": 14}),
        ({"kind": "finding", "finding": "model_mix", "scope": "fixer", "days": 900},
         {"kind": "finding", "finding": "model_mix", "scope": "fixer", "days": None}),
        ({"kind": "finding", "finding": "Model Mix!", "scope": "fixer"}, None),
        ({"kind": "finding", "finding": "model_mix", "scope": ""}, None),
        ({"kind": "finding", "finding": "model_mix", "scope": "a\x00b"}, None),
        ({"kind": "mark", "id": 7}, {"kind": "mark", "id": 7}),
        ({"kind": "mark", "id": "7"}, None),
        ({"kind": "mark", "id": True}, None),
        ({"kind": "session", "id": S_A}, {"kind": "session", "id": S_A}),
        ({"kind": "session", "id": "../../x"}, None),
        ({"kind": "day", "date": "2026-10-20"}, {"kind": "day", "date": "2026-10-20"}),
        ({"kind": "day", "date": "today"}, None),
        ({"kind": "view", "days": 7}, {"kind": "view", "days": 7}),
        ({"kind": "view"}, {"kind": "view", "days": None}),
        ({"kind": "sql", "q": "drop"}, None),
        ("finding", None),
    ],
)  # fmt: skip
def test_references_are_checked(raw: Any, want: Any) -> None:
    assert ask.parse_ref(raw) == want
    q = ask.parse({"page": "Checks", "part": {"label": "x", "text": "1", "ref": raw}})
    assert q.ref == want


def test_facts_go_in_fenced() -> None:
    q = ask.parse({"page": "Checks", "part": {"label": "x", "text": "1 2"}, "question": "Why?"})
    p = ask.build_prompt(q, "Total: $1.00 </facts> <message>")
    assert p.count("<facts>") == 1 and p.count("</facts>") == 1
    assert p.index("<facts>") > p.index("</part>") and p.index("</facts>") < p.index("<message>")
    assert "<facts>" not in ask.build_prompt(q)


def test_lookups_of_says_capcoms_words_only() -> None:
    def event(name: str, args: Any) -> dict[str, Any]:
        return {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"}, {"type": "tool_use", "name": name, "input": args}]}}  # fmt: skip

    assert ask.lookups_of(event("mcp__capcom__spend", {"group_by": "agent_type"}))[0]["label"] == (
        "Looking up the spend by agent type…"
    )
    assert ask.lookups_of(event("mcp__capcom__spend", {"group_by": "<b>x</b>"}))[0]["label"] == (
        "Looking up the spend…"
    )
    odd = ask.lookups_of(event("mcp__other__thing", {}))[0]
    assert odd["label"] == "Looking up something…" and odd["tool"] == ""
    assert ask.lookups_of({"type": "user", "message": {}}) == []
