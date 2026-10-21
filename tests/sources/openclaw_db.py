"""A synthetic OpenClaw agent database: the `transcript_events` table as OpenClaw
creates it (session_id, seq, event_json or event_zstd, created_at epoch ms), filled
with made-up events. No real log text."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_json TEXT,
  created_at INTEGER NOT NULL,
  event_zstd BLOB,
  event_utf8_bytes INTEGER,
  navigation_json TEXT,
  PRIMARY KEY (session_id, seq)
) STRICT;
"""


def db_path(openclaw_root: Path, agent: str = "main") -> Path:
    return openclaw_root / "agents" / agent / "agent" / "openclaw-agent.sqlite"


def create_db(path: Path, wal: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def reply(
    event_id: str,
    *,
    provider: str = "google",
    model: str = "gemini-3.8-flash",
    api: str = "google-generative-ai",
    input: int = 100,
    output: int = 20,
    cache_read: int = 0,
    cache_write: int = 0,
    total: float | None = 0.001,
    stop: str = "stop",
) -> dict[str, Any]:
    """An assistant event with usage, shaped like OpenClaw's."""
    usage: dict[str, Any] = {
        "input": input,
        "output": output,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": input + output + cache_read + cache_write,
    }
    if total is not None:
        usage["cost"] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": total}
    return {
        "id": event_id,
        "type": "message",
        "parentId": None,
        "timestamp": "2026-10-01T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "provider": provider,
            "model": model,
            "api": api,
            "stopReason": stop,
            "content": [{"type": "text", "text": "synthetic reply"}],
            "usage": usage,
        },
    }


def user_event(event_id: str) -> dict[str, Any]:
    return {"id": event_id, "type": "message", "message": {"role": "user", "content": "hi"}}


def add(
    path: Path,
    session: str,
    seq: int,
    event: dict[str, Any] | str,
    created: datetime,
    compress: Callable[[bytes], bytes] | None = None,
    rowid: int | None = None,
) -> None:
    """Insert one event, as JSON text or (with `compress`) as a compressed blob."""
    text = event if isinstance(event, str) else json.dumps(event)
    con = sqlite3.connect(path)
    cols = "session_id, seq, created_at, event_json, event_zstd, event_utf8_bytes"
    vals: list[Any]
    if compress is None:
        vals = [session, seq, ms(created), text, None, None]
    else:
        data = text.encode()
        vals = [session, seq, ms(created), None, compress(data), len(data)]
    if rowid is not None:
        cols = "rowid, " + cols
        vals = [rowid, *vals]
    con.execute(
        f"INSERT INTO transcript_events ({cols}) VALUES ({', '.join('?' * len(vals))})", vals
    )
    con.commit()
    con.close()
