"""A small JSON-lines source for the store's tests.

The store only needs something that satisfies `capcom.types.Source`; this one reads a
made-up line format (one JSON object per line, `"t"` saying which record it is), so
the store can be tested without the Claude Code parser. The layout on disk matches
Claude Code's: `<root>/<project>/<session>.jsonl` and
`<root>/<project>/<session>/subagents/agent-<id>.jsonl`.

Line shapes (every field but `t` optional):

    {"t": "req", "id": "m1", "s": "s1", "ts": 12.5, "out": 100, "tools": [["tu1", "Read", "/a"]]}
    {"t": "ev", "kind": "compaction", "key": "u1", "s": "s1", "ts": 3, "attrs": {...}}
    {"t": "ses", "s": "s1", "title": "x", "first": 0, "last": 9}
    {"t": "agent", "id": "a1", "s": "s1", "type": "scout"}
    {"t": "cost", "s": "s1", "usd": 1.5, "models": {"m": {"input": 3, "cost_usd": 1.5}}, "ts": 4,
     "next": 5}

`ts`, `first` and `last` are seconds after `tests.fixtures.generate.T0`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from capcom.types import (
    AgentInfo,
    Batch,
    CostState,
    Event,
    EventKind,
    LogFile,
    LogKind,
    ModelUsage,
    Record,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)
from tests.fixtures.generate import T0


def _t(v: Any) -> Any:
    return None if v is None else T0 + timedelta(seconds=float(v))


def to_record(d: dict[str, Any], log: LogFile, pos: int) -> Record | None:
    t = d.get("t")
    s = d.get("s", log.session_id)
    if t == "req":
        return Request(
            message_id=d["id"],
            session_id=s,
            agent_id=d.get("a", log.agent_id),
            project_dir=log.project_dir,
            timestamp=_t(d.get("ts", 0)),
            model=d.get("model", "claude-opus-5-5"),
            usage=Usage(
                input=d.get("in", 3),
                cache_read=d.get("cr", 0),
                cache_write_5m=d.get("cw5", 0),
                cache_write_1h=d.get("cw1", 0),
                output=d.get("out", 0),
            ),
            request_id=d.get("rid"),
            stop_reason=d.get("stop"),
            tool_calls=tuple(ToolCall(*x) for x in d.get("tools", ())),
        )
    if t == "ev":
        return Event(
            kind=EventKind(d.get("kind", "turn")),
            session_id=s,
            agent_id=d.get("a", log.agent_id),
            timestamp=_t(d.get("ts", 0)),
            key=d.get("key", str(pos)),
            attrs={
                k: tuple(v) if isinstance(v, list) else v for k, v in d.get("attrs", {}).items()
            },
        )
    if t == "ses":
        return SessionInfo(
            session_id=s,
            project_dir=log.project_dir,
            cwd=d.get("cwd"),
            title=d.get("title"),
            version=d.get("version"),
            first_seen=_t(d.get("first")),
            last_seen=_t(d.get("last")),
        )
    if t == "agent":
        return AgentInfo(
            agent_id=d.get("id", log.agent_id or ""),
            session_id=s,
            agent_type=d.get("type"),
            description=d.get("desc"),
            finished=d.get("finished"),
        )
    if t == "cost":
        return CostState(
            session_id=s,
            offset=pos,
            total_cost_usd=d.get("usd", 0.0),
            models={k: ModelUsage(**v) for k, v in d.get("models", {}).items()},
            timestamp=_t(d.get("ts")),
            next_timestamp=_t(d.get("next")),
            total_duration_ms=d.get("dur"),
        )
    return None


class FakeSource:
    """Satisfies `capcom.types.Source`. Records every `parse` call in `calls`."""

    def __init__(self, batch_lines: int = 1000) -> None:
        self.batch_lines = batch_lines
        self.calls: list[tuple[Path, int]] = []

    def discover(self, root: Path) -> Iterator[LogFile]:
        if not root.is_dir():
            return
        for project in sorted(p for p in root.iterdir() if p.is_dir()):
            for path in sorted(project.glob("*.jsonl")):
                yield LogFile(path, LogKind.SESSION, project.name, path.stem)
            for path in sorted(project.glob("*/subagents/agent-*.jsonl")):
                session = path.parent.parent.name
                agent = path.stem.removeprefix("agent-")
                meta = path.with_name(f"agent-{agent}.meta.json")
                yield LogFile(path, LogKind.SUBAGENT, project.name, session, agent, meta)

    def parse(self, log: LogFile, start: int) -> Iterator[Batch]:
        self.calls.append((log.path, start))
        with log.path.open("rb") as f:
            f.seek(start)
            pos = start
            batch = Batch(records=[], end_offset=start)
            if log.meta_path is not None and log.meta_path.is_file():
                # Like the Claude Code adapter: the meta is read again on every call.
                meta = json.loads(log.meta_path.read_text())
                batch.records.append(
                    AgentInfo(
                        agent_id=log.agent_id or "",
                        session_id=log.session_id,
                        agent_type=meta.get("agentType"),
                        description=meta.get("description"),
                        finished=meta.get("finished"),
                    )
                )
            for line in f:
                if not line.endswith(b"\n"):
                    break  # partial last line: read it next time
                line_pos = pos
                pos += len(line)
                batch.lines += 1
                batch.end_offset = pos
                try:
                    rec = to_record(json.loads(line), log, line_pos)
                except (ValueError, KeyError, TypeError):
                    rec = None
                if rec is None:
                    batch.skipped += 1
                else:
                    batch.records.append(rec)
                    if batch.first_timestamp is None and isinstance(rec, (Request, Event)):
                        batch.first_timestamp = rec.timestamp  # first in file order
                if batch.lines >= self.batch_lines:
                    yield batch
                    batch = Batch(records=[], end_offset=pos)
            if batch.lines or batch.records:
                yield batch


def write_lines(
    path: Path, *records: dict[str, Any], partial: dict[str, Any] | None = None
) -> None:
    """Append records to `path` as JSON lines; `partial` is written without a newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        if partial is not None:
            f.write(json.dumps(partial))
