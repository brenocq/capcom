"""Ask Roger's lookups: a small MCP server over capcom's index, for one question.

`ask.command` gives the headless `claude` CLI this server, and only it, through
`--mcp-config` (with `--strict-mcp-config`, every built-in tool off and only these
tools allowed). The CLI starts it as `python -I -m capcom.report.asktools --index
...` and talks JSON-RPC to it over stdin and stdout, one message per line; it ends
with the CLI, which capcom kills by its process group when the question is done.

The tools are read-only and answer from `askfacts`: spend and request counts
grouped by project, model, agent type, day or session over a range; one session's
splits; the Checks findings; where an agent, skill or command is defined. They
return aggregates, labels, ids and file paths only, never a session's title or any
text from the logs. At most `MAX_CALLS` calls per question: past that each call
says to answer with what is known.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import IO, Any

from capcom.report.askfacts import GROUPS, MAX_DAYS, Facts, FactsError

NAME = "capcom"
MAX_CALLS = 4  # lookups per question
MAX_RESULT = 6000  # characters of one tool's answer
PROTOCOL = "2025-06-18"

_DAYS = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_DAYS,
    "description": "The last N local days, today included (default 14).",
}
_FILTERS: dict[str, Any] = {
    "project": {
        "type": "string",
        "description": (
            "Only this project: its name as capcom shows it (such as 'capcom'), or its folder."
        ),
    },
    "model": {
        "type": "string",
        "description": (
            "Only models whose id holds this, such as 'opus', 'sonnet', 'haiku' or a full id."
        ),
    },
    "agent_type": {
        "type": "string",
        "description": "Only this agent type; 'main' is the main conversation thread.",
    },
    "session_id": {
        "type": "string",
        "description": "Only this session (its id, or its first 8 characters).",
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "spend",
        "description": (
            "API-equivalent dollars, request counts and session counts from capcom's index, "
            "grouped by project, model, agent_type, day or session, over the last N days or a "
            "date range, optionally filtered. Use it for questions such as which projects use "
            "the most Opus requests, or which sessions an agent type ran in."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": list(GROUPS)},
                "days": _DAYS,
                "since": {
                    "type": "string",
                    "description": "First local day, YYYY-MM-DD (instead of days).",
                },
                "until": {"type": "string", "description": "Last local day, YYYY-MM-DD, included."},
                **_FILTERS,
            },
            "required": ["group_by"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session",
        "description": (
            "One session by id: its project and folder, first and last request, total, and "
            "spend by model, agent type and day, and the subagents it spawned. Session titles "
            "are never given."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _FILTERS["session_id"]},
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "findings",
        "description": (
            "capcom's Checks findings over the last N days: kind, scope (a project, an agent "
            "type or all), title and the estimated saving per week, largest first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"days": _DAYS},
            "additionalProperties": False,
        },
    },
    {
        "name": "where_defined",
        "description": (
            "Where an agent type, a skill or a slash command is defined: the file paths in a "
            "project's .claude folder, the user's ~/.claude or an installed plugin; for an "
            "agent, also the projects whose sessions spawned it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The agent type, skill or command name."},
                "kind": {"type": "string", "enum": ["agent", "skill", "command"]},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)


def allowed() -> str:
    """The `--allowedTools` value: these tools, as the CLI names them."""
    return ",".join(f"mcp__{NAME}__{t}" for t in TOOL_NAMES)


class Server:
    """The JSON-RPC side: one request in, one reply out, `facts` built on first use."""

    def __init__(self, facts: Callable[[], Facts], max_calls: int = MAX_CALLS) -> None:
        self._make = facts
        self._facts: Facts | None = None
        self.calls = 0
        self.max_calls = max_calls

    def facts(self) -> Facts:
        if self._facts is None:
            self._facts = self._make()
        return self._facts

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """The reply to one message, or None for a notification."""
        if not isinstance(msg, dict):
            return _error(None, -32600, "Invalid request")
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            return None  # a notification (initialized, cancelled): nothing to say
        raw = msg.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(
                mid,
                {
                    "protocolVersion": asked if isinstance(asked, str) else PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": NAME, "version": "1"},
                },
            )
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            return _result(mid, self.call(params.get("name"), params.get("arguments")))
        return _error(mid, -32601, "Method not found")

    def call(self, name: Any, args: Any) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            return _content({"error": "No such tool."}, error=True)
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _content({"error": "The arguments must be an object."}, error=True)
        self.calls += 1
        if self.calls > self.max_calls:
            return _content(
                {"error": f"That is {self.max_calls} lookups already: answer with what you know."},
                error=True,
            )
        try:
            facts = self.facts()
            fn: Callable[[Mapping[str, Any]], dict[str, Any]] = getattr(facts, name)
            return _content(fn(args))
        except FactsError as err:
            return _content({"error": str(err)}, error=True)
        except Exception:  # an index being rewritten, a bug: say it, never a traceback
            return _content({"error": "capcom could not look that up."}, error=True)


def _result(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _content(payload: Any, error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(text) > MAX_RESULT:
        text = text[: MAX_RESULT - 20] + '..."(cut)"'
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["isError"] = True
    return out


def serve(server: Server, stdin: IO[str], stdout: IO[str]) -> None:
    """Answer each line of `stdin` until it closes."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=False) + "\n")
            stdout.flush()


# --------------------------------------------------------------------------- the process


def arguments(
    index_path: Path,
    now: datetime,
    tz: tzinfo | None,
    changes_path: Path | None = None,
) -> list[str]:
    """The server's command-line arguments after `-m capcom.report.asktools`."""
    out = ["--index", str(index_path), "--now", now.astimezone(UTC).isoformat()]
    zone = getattr(tz, "key", None) or ("UTC" if tz is UTC else None)
    if zone:
        out += ["--tz", zone]
    if changes_path is not None:
        out += ["--changes", str(changes_path)]
    return out


def mcp_config(args: list[str]) -> str:
    """The `--mcp-config` JSON: this server only, run by this Python, isolated (-I)."""
    server = {"type": "stdio", "command": sys.executable, "args": ["-I", "-m", __name__, *args]}
    return json.dumps({"mcpServers": {NAME: server}}, separators=(",", ":"))


def _context(ns: argparse.Namespace) -> Facts:
    from zoneinfo import ZoneInfo

    from capcom.cost.prices import load_prices
    from capcom.report.analysis import default_analysis
    from capcom.report.api import Context
    from capcom.store.index import open_index

    tz = None if not ns.tz else (UTC if ns.tz == "UTC" else ZoneInfo(ns.tz))
    now = datetime.fromisoformat(ns.now) if ns.now else datetime.now(UTC)
    ctx = Context(
        index=open_index(Path(ns.index), readonly=True),
        table=load_prices(),
        analysis=default_analysis(),
        now=now,
        tz=tz,
        index_path=Path(ns.index),
        changes_path=Path(ns.changes) if ns.changes else None,
    )
    return Facts(ctx)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m capcom.report.asktools")
    p.add_argument("--index", required=True)
    p.add_argument("--now")
    p.add_argument("--tz")
    p.add_argument("--changes")
    ns = p.parse_args(argv)
    serve(Server(lambda: _context(ns)), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
