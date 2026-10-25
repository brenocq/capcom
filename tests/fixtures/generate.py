"""Synthetic Claude Code logs for tests.

Writes a projects folder in the shape Claude Code 2.1.30x uses (field names as
measured in docs/data-sources.md), with made-up text only. Nothing here is copied
from real logs.

    root = tmp_path / ".claude"
    s = SessionWriter(root, cwd="/home/user/proj", session_id="s1")
    s.user_prompt(at(0))
    mid = s.assistant(Reply(at(1), model="claude-opus-5-5", cache_read=50_000, output=300,
                            tools=[("Read", {"file_path": "/home/user/proj/a.py"})]))
    s.tool_result(s.last_tool_ids[0], at(2), chars=8_000)
    sub = s.subagent("scout", "find the config", tool_use_id=s.last_tool_ids[0])
    sub.assistant(Reply(at(3), model="claude-haiku-4-5-20251001", output=4,
                        stream_start_output=True, tools=[("Bash", {"command": "ls -la"})]))
    s.cost_state(total_usd=1.23, models={"claude-opus-5-5": {...}})
    s.close()

Each writer appends lines to its file as you call it; `partial_line()` leaves a line
without its newline, as a live session being written does.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

T0 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
_ids = itertools.count(1)


def at(seconds: float) -> datetime:
    """A time `seconds` after T0."""
    return T0 + timedelta(seconds=seconds)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uid(prefix: str) -> str:
    return f"{prefix}{next(_ids):012x}"


def project_dir_name(cwd: str) -> str:
    """Claude Code's folder name for a working directory: '/' and '.' become '-'."""
    return cwd.replace("/", "-").replace(".", "-")


@dataclass
class Reply:
    """One model reply. `records` is how many times Claude Code writes it while
    streaming (one record per content block, the same message.id). With
    `stream_start_output`, every record keeps the stream-start output count
    (`output`, e.g. 4) although the content is longer, as subagent logs often do."""

    time: datetime
    model: str = "claude-opus-5-5"
    input: int = 3
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 100
    thinking: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    text: str = ""
    thinking_text: str = ""
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    records: int = 1
    stream_start_output: bool = False
    speed: str = "standard"
    effort: str | None = "high"
    api_error: bool = False
    error: str | None = None  # with api_error: the record's `error` label, e.g. "rate_limit"
    error_status: int | None = None  # with api_error: `apiErrorStatus`, e.g. 429


class SessionWriter:
    """Writes one session log (or, from `subagent()`, one subagent log)."""

    def __init__(
        self,
        root: Path,
        cwd: str,
        session_id: str,
        version: str = "2.1.304",
        *,
        _agent_id: str | None = None,
        _path: Path | None = None,
    ) -> None:
        self.root = root
        self.cwd = cwd
        self.session_id = session_id
        self.version = version
        self.agent_id = _agent_id
        self.project = root / "projects" / project_dir_name(cwd)
        self.path = _path or self.project / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("a", encoding="utf-8")
        self._parent: str | None = None
        self.last_tool_ids: list[str] = []
        self.message_ids: list[str] = []

    # -- plumbing

    def _common(self, t: datetime) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "parentUuid": self._parent,
            "isSidechain": self.agent_id is not None,
        }
        if self.agent_id is not None:
            rec["agentId"] = self.agent_id
        rec["uuid"] = _uid("u-")
        rec["timestamp"] = _iso(t)
        rec.update(
            {
                "userType": "external",
                "entrypoint": "cli",
                "cwd": self.cwd,
                "sessionId": self.session_id,
                "version": self.version,
                "gitBranch": "main",
            }
        )
        self._parent = rec["uuid"]
        return rec

    def write(self, record: dict[str, Any]) -> None:
        """Append any record as one line (compact JSON, like Claude Code)."""
        self._f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._f.flush()

    def raw(self, line: str) -> None:
        """Append a raw line (e.g. invalid JSON)."""
        self._f.write(line + "\n")
        self._f.flush()

    def partial_line(self, record: dict[str, Any] | None = None) -> None:
        """Append a record without its trailing newline: a write in progress."""
        rec = record or {"type": "assistant", "message": {"id": "msg_partial"}}
        self._f.write(json.dumps(rec, separators=(",", ":"))[:-5])
        self._f.flush()

    def close(self) -> None:
        self._f.close()

    # -- records

    def user_prompt(self, t: datetime, text: str = "synthetic prompt") -> None:
        rec = self._common(t)
        rec.update(
            {"type": "user", "promptId": _uid("p-"), "message": {"role": "user", "content": text}}
        )
        self.write(rec)

    def assistant(self, reply: Reply) -> str:
        """Write a reply (possibly as several streaming records); returns its id."""
        mid = _uid("msg_")
        self.message_ids.append(mid)
        blocks: list[dict[str, Any]] = []
        if reply.thinking_text or reply.thinking:
            blocks.append({"type": "thinking", "thinking": reply.thinking_text, "signature": "sig"})
        if reply.text:
            blocks.append({"type": "text", "text": reply.text})
        self.last_tool_ids = []
        for name, tool_input in reply.tools:
            tid = _uid("toolu_")
            self.last_tool_ids.append(tid)
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": tool_input})
        if not blocks:
            blocks.append({"type": "text", "text": "synthetic reply"})
        n = max(1, reply.records)
        request_id = _uid("req_")
        for i in range(n):
            last = i == n - 1
            out = reply.output if (last or reply.stream_start_output) else min(reply.output, 4)
            usage: dict[str, Any] = {
                "input_tokens": reply.input,
                "cache_creation_input_tokens": reply.cache_write_5m + reply.cache_write_1h,
                "cache_read_input_tokens": reply.cache_read,
                "output_tokens": out,
                "server_tool_use": {
                    "web_search_requests": reply.web_searches,
                    "web_fetch_requests": reply.web_fetches,
                },
                "service_tier": "standard",
                "cache_creation": {
                    "ephemeral_1h_input_tokens": reply.cache_write_1h,
                    "ephemeral_5m_input_tokens": reply.cache_write_5m,
                },
                "iterations": [],
                "speed": reply.speed,
            }
            if reply.thinking and last:
                usage["output_tokens_details"] = {"thinking_tokens": reply.thinking}
            rec = self._common(reply.time)
            rec["type"] = "assistant"
            rec["requestId"] = request_id
            rec["message"] = {
                "model": reply.model,
                "id": mid,
                "type": "message",
                "role": "assistant",
                "content": [blocks[min(i, len(blocks) - 1)]] if n > 1 else blocks,
                "stop_reason": ("tool_use" if reply.tools else "end_turn") if last else None,
                "stop_sequence": None,
                "usage": usage,
            }
            if reply.effort:
                rec["effort"] = reply.effort
            if reply.api_error:
                rec["isApiErrorMessage"] = True
                if reply.error is not None:
                    rec["error"] = reply.error
                if reply.error_status is not None:
                    rec["apiErrorStatus"] = reply.error_status
            self.write(rec)
        return mid

    def tool_result(
        self,
        tool_use_id: str,
        t: datetime,
        chars: int = 1000,
        is_image: bool = False,
        is_error: bool = False,
        text: str | None = None,
    ) -> None:
        """A tool's result: `chars` of filler, or `text` (the caller keeps it `chars`
        long when the size matters) for pages that show a result's own words."""
        if is_image:
            content: Any = [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "A" * chars},
                }
            ]
        else:
            content = "x" * chars if text is None else text
        rec = self._common(t)
        rec.update(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": content,
                            "is_error": is_error,
                        }
                    ],
                },
                "toolUseResult": {
                    "stdout": "x" * min(chars, 50) if text is None else text,
                    "stderr": "",
                    "interrupted": False,
                },
                "sourceToolAssistantUUID": self._parent,
            }
        )
        self.write(rec)

    def attachment(self, t: datetime, payload: dict[str, Any]) -> None:
        """An attachment record; `payload` is the `attachment` object (with `type`)."""
        rec = self._common(t)
        rec.update({"type": "attachment", "attachment": payload})
        self.write(rec)

    def skill_listing(self, t: datetime, names: list[str]) -> None:
        self.attachment(
            t,
            {
                "type": "skill_listing",
                "content": "".join(f"- {n}: synthetic skill\n" for n in names),
                "skillCount": len(names),
                "isInitial": True,
                "names": names,
            },
        )

    def deferred_tools_delta(self, t: datetime, added: list[str], removed: list[str] = []) -> None:  # noqa: B006
        self.attachment(
            t,
            {
                "type": "deferred_tools_delta",
                "addedNames": added,
                "addedLines": [f"{n}: synthetic" for n in added],
                "removedNames": removed,
            },
        )

    def model_attachment(self, t: datetime, model: str) -> None:
        self.attachment(
            t, {"type": "model", "identity": {"modelId": model, "marketingName": model}, "text": ""}
        )

    def instructions(self, t: datetime, files: dict[str, int]) -> None:
        """CLAUDE.md-like files loaded into the prompt: path -> size in chars."""
        self.attachment(
            t,
            {
                "type": "instructions",
                "files": [
                    {"path": p, "type": "Project", "content": "y" * n} for p, n in files.items()
                ],
            },
        )

    def nested_memory(self, t: datetime, path: str, chars: int) -> None:
        self.attachment(
            t,
            {
                "type": "nested_memory",
                "path": path,
                "content": {"path": path, "type": "Project", "content": "m" * chars},
                "displayPath": path,
            },
        )

    def invoked_skills(self, t: datetime, skills: dict[str, int]) -> None:
        """Skills whose text was loaded: name -> size in chars."""
        self.attachment(
            t,
            {
                "type": "invoked_skills",
                "skills": [
                    {"name": n, "path": f"/skills/{n}/SKILL.md", "content": "s" * c}
                    for n, c in skills.items()
                ],
            },
        )

    def mcp_instructions_delta(
        self,
        t: datetime,
        added: dict[str, int],
        removed: list[str] = [],  # noqa: B006
    ) -> None:
        """Connector instructions: server name -> size in chars of its block."""
        self.attachment(
            t,
            {
                "type": "mcp_instructions_delta",
                "addedNames": list(added),
                "addedBlocks": ["i" * c for c in added.values()],
                "removedNames": removed,
            },
        )

    def agent_listing_delta(self, t: datetime, added: list[str], removed: list[str] = []) -> None:  # noqa: B006
        self.attachment(
            t,
            {
                "type": "agent_listing_delta",
                "addedTypes": added,
                "addedLines": [f"- {a}: synthetic agent" for a in added],
                "removedTypes": removed,
                "isInitial": True,
            },
        )

    def deferred_tools_record(self, t: datetime, names: list[str]) -> None:
        self.attachment(
            t,
            {
                "type": "deferred_tools_record",
                "entries": [
                    {
                        "name": n,
                        "description": "synthetic tool",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                    for n in names
                ],
            },
        )

    def api_error_system(self, t: datetime, code: str = "ECONNRESET") -> None:
        """A `system/api_error` record (a failed call Claude Code retries)."""
        rec = self._common(t)
        rec.update(
            {
                "type": "system",
                "subtype": "api_error",
                "level": "error",
                "error": {
                    "message": "Connection error.",
                    "connection": {"code": code, "message": "synthetic", "isSSLError": False},
                    "isNetworkDown": True,
                    "rateLimits": None,
                },
                "retryInMs": 500,
                "retryAttempt": 1,
                "maxRetries": 10,
            }
        )
        self.write(rec)

    def compact(self, t: datetime, pre: int, post: int, trigger: str = "auto") -> None:
        rec = self._common(t)
        rec.update(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "content": "Conversation compacted",
                "compactMetadata": {
                    "trigger": trigger,
                    "preTokens": pre,
                    "postTokens": post,
                    "durationMs": 30_000,
                },
            }
        )
        self.write(rec)

    def away(self, t: datetime) -> None:
        rec = self._common(t)
        rec.update({"type": "system", "subtype": "away_summary", "content": "synthetic summary"})
        self.write(rec)

    def turn_duration(self, t: datetime, ms: int) -> None:
        rec = self._common(t)
        rec.update({"type": "system", "subtype": "turn_duration", "durationMs": ms})
        self.write(rec)

    def api_request_shape(self, t: datetime, tools: list[str], shape_hash: str = "h1") -> None:
        self.write(
            {
                "type": "api-request-shape",
                "sessionId": self.session_id,
                "shapeHash": shape_hash,
                "timestamp": _iso(t),
                "version": self.version,
                "shape": {
                    "system": [{"type": "text", "text": "synthetic system prompt"}],
                    "tools": [
                        {"name": n, "description": "synthetic", "input_schema": {"type": "object"}}
                        for n in tools
                    ],
                },
            }
        )

    def api_request(self, t: datetime, model: str, shape_hash: str = "h1", ttl: str = "1h") -> None:
        self.write(
            {
                "type": "api-request",
                "sessionId": self.session_id,
                "id": _uid("req_"),
                "timestamp": _iso(t),
                "version": self.version,
                "querySource": "repl_main_thread" if self.agent_id is None else "agent:custom",
                "shapeHash": shape_hash,
                "params": {"model": model, "betas": [], "max_tokens": 32000},
                "cacheControl": [
                    {"path": ["system", "0"], "value": {"type": "ephemeral", "ttl": ttl}}
                ],
            }
        )

    def ai_title(self, title: str) -> None:
        self.write({"type": "ai-title", "aiTitle": title, "sessionId": self.session_id})

    def cost_state(self, total_usd: float, models: dict[str, dict[str, Any]]) -> None:
        """`models`: model id -> {inputTokens, outputTokens, cacheReadInputTokens,
        cacheCreationInputTokens, thinkingTokens, webSearchRequests, costUSD}."""
        self.write(
            {
                "type": "cost-state",
                "sessionId": self.session_id,
                "totalCostUSD": total_usd,
                "totalAPIDuration": 1000,
                "totalToolDuration": 100,
                "totalLinesAdded": 0,
                "totalLinesRemoved": 0,
                "totalDuration": 5000,
                "startTime": int(T0.timestamp() * 1000),
                "modelUsage": models,
                "hasUnknownModelCost": False,
            }
        )

    def unknown_record(self) -> None:
        """A record type capcom has never seen (must be skipped, not an error)."""
        self.write(
            {"type": "future-record-type", "sessionId": self.session_id, "payload": {"a": 1}}
        )

    def subagent(
        self,
        agent_type: str,
        description: str,
        tool_use_id: str | None = None,
        agent_id: str | None = None,
        meta: bool = True,
    ) -> SessionWriter:
        """A subagent of this session: its log and (unless meta=False) its meta.json."""
        aid = agent_id or f"a{next(_ids):016x}"
        folder = self.project / self.session_id / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        if meta:
            (folder / f"agent-{aid}.meta.json").write_text(
                json.dumps(
                    {
                        "agentType": agent_type,
                        "description": description,
                        "toolUseId": tool_use_id,
                        "spawnDepth": 1,
                        "isBuiltIn": False,
                    }
                ),
                encoding="utf-8",
            )
        return SessionWriter(
            self.root,
            self.cwd,
            self.session_id,
            self.version,
            _agent_id=aid,
            _path=folder / f"agent-{aid}.jsonl",
        )


def build_sample(root: Path) -> dict[str, Any]:
    """A small but complete projects folder used by several tests. Returns the facts
    the tests check against (ids, expected token totals)."""
    s = SessionWriter(
        root, cwd="/home/user/proj", session_id="11111111-aaaa-bbbb-cccc-000000000001"
    )
    s.skill_listing(at(0), ["alpha", "beta"])
    s.deferred_tools_delta(at(0), ["mcp__srv__one", "mcp__srv__two"])
    s.instructions(at(0), {"/home/user/proj/CLAUDE.md": 4000})
    s.api_request_shape(at(0), ["Read", "Bash", "Agent", "mcp__srv__one"])
    s.user_prompt(at(1))
    s.api_request(at(2), "claude-opus-5-5")
    m1 = s.assistant(
        Reply(
            at(2),
            cache_write_1h=20_000,
            output=250,
            thinking=120,
            thinking_text="t" * 400,
            text="synthetic answer",
            tools=[("Read", {"file_path": "/home/user/proj/big.json"})],
            records=3,
        )
    )
    read_id = s.last_tool_ids[0]
    s.tool_result(read_id, at(3), chars=40_000)
    m2 = s.assistant(
        Reply(
            at(4),
            cache_read=20_000,
            cache_write_1h=10_000,
            output=80,
            tools=[
                ("Agent", {"subagent_type": "scout", "description": "look around", "prompt": "p"})
            ],
        )
    )
    agent_call = s.last_tool_ids[0]
    sub = s.subagent("scout", "look around", tool_use_id=agent_call)
    sub.assistant(
        Reply(
            at(5),
            model="claude-haiku-4-5-20251001",
            cache_write_5m=12_000,
            output=4,
            stream_start_output=True,
            thinking_text="h" * 2000,
            tools=[("Bash", {"command": "ls -la /home/user/proj"})],
            records=2,
        )
    )
    sub.close()
    s.tool_result(agent_call, at(20), chars=6_000)
    s.away(at(4000))
    s.compact(at(4100), pre=180_000, post=9_000)
    s.model_attachment(at(4101), "claude-sonnet-5-5")
    m3 = s.assistant(Reply(at(4102), model="claude-sonnet-5-5", cache_write_1h=9_000, output=40))
    s.unknown_record()
    s.raw("{not json")
    s.ai_title("Synthetic session")
    s.cost_state(
        1.5,
        {
            "claude-opus-5-5": {
                "inputTokens": 6,
                "outputTokens": 330,
                "cacheReadInputTokens": 20_000,
                "cacheCreationInputTokens": 30_000,
                "thinkingTokens": 120,
                "webSearchRequests": 0,
                "costUSD": 1.2,
            }
        },
    )
    s.partial_line()
    s.close()
    return {
        "session_id": s.session_id,
        "project_dir": project_dir_name("/home/user/proj"),
        "main_message_ids": [m1, m2, m3],
        "subagent_id": sub.agent_id,
        "subagent_message_ids": sub.message_ids,
        "read_tool_id": read_id,
        "agent_tool_id": agent_call,
    }
