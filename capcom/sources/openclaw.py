"""The OpenClaw adapter: the calls OpenClaw makes to model providers itself.

OpenClaw keeps every agent's transcript in one SQLite database per agent,
`<openclaw_root>/agents/<agent>/agent/openclaw-agent.sqlite`, table
`transcript_events` (docs/data-sources.md, "OpenClaw's direct calls"). Each row is
one event, as JSON in `event_json` or zstd-compressed in `event_zstd`; a model reply
carries `message.usage` (input, output, cacheRead, cacheWrite, cost.total).

Only the direct calls are read: a reply whose provider is `claude-cli` went through
Claude Code, whose own logs already count it. Each direct reply becomes
one Request in the project `DIRECT_PROJECT`, apart from OpenClaw's Claude Code
folders, with its message id prefixed "oc:" and its session id "openclaw:".

The database changes in place while OpenClaw runs, so the position read so far is
a rowid, not a byte offset (`LogKind.OPENCLAW_DB`): `parse` reads the rows above
`start` in rowid order, in batches, each batch ending at its last rowid. The table
has no INTEGER PRIMARY KEY (its key is (session_id, seq)), so a VACUUM may renumber
rowids: `row_key` gives the key at a rowid, which the store checks at its cursor.

The file is opened read-only and nothing is ever created next to it: `mode=ro` while
OpenClaw has it open (its -wal and -shm files exist), and `immutable=1` when a WAL
database lacks them (OpenClaw is not running), since `mode=ro` would create them.
Compressed rows need Python 3.14's `compression.zstd`; without it they are counted in
`compressed_unread` and as skipped, and `unread_from` gives the lowest such rowid so
the store can read them again once a Python that has it runs the scan.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capcom.types import Batch, LogFile, LogKind, Record, Request, SessionInfo, Usage

DIRECT_PROJECT = "openclaw (direct)"  # the project of every direct call
SESSION_PREFIX = "openclaw:"
MESSAGE_PREFIX = "oc:"
DB_NAME = "openclaw-agent.sqlite"
TABLE = "transcript_events"
# Replies that went through Claude Code (already in its logs). Only the provider
# tells: other providers run through a CLI of their own too (api "cli").
VIA_CLAUDE_CODE_PROVIDERS = frozenset({"claude-cli"})
# Entries OpenClaw writes itself (provider "openclaw", e.g. a delivery mirror): no call.
NOT_A_CALL_PROVIDERS = frozenset({"openclaw"})
MAX_BATCH_ROWS = 1_000
_SHORT = 200
_USAGE_KEY = '"usage"'


def _load_zstd() -> Callable[[bytes], bytes] | None:
    try:
        module = importlib.import_module("compression.zstd")
    except ImportError:
        return None
    fn: Callable[[bytes], bytes] = module.decompress
    return fn


_DECOMPRESS = _load_zstd()


def zstd_supported() -> bool:
    """True when this Python can read OpenClaw's compressed rows (3.14 and later)."""
    return _DECOMPRESS is not None


def _str(value: object, limit: int = _SHORT) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _epoch_ms(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: object) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _epoch_ms(value)
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def is_direct(message: Mapping[str, Any]) -> bool:
    """False for a reply that went through Claude Code (already in its logs) and for
    the entries OpenClaw writes itself."""
    provider = message.get("provider")
    return provider not in VIA_CLAUDE_CODE_PROVIDERS and provider not in NOT_A_CALL_PROVIDERS


def event_request(event: Mapping[str, Any], session_id: str, created_ms: object) -> Request | None:
    """The Request of one transcript event, or None when it is not a direct model
    reply (no usage, no id, or a reply that went through Claude Code)."""
    message = _dict(event.get("message"))
    raw = message.get("usage")
    if not isinstance(raw, dict) or not is_direct(message):
        return None
    event_id = _str(event.get("id"))
    if event_id is None:
        return None
    timestamp = (
        _epoch_ms(created_ms) or _iso(event.get("timestamp")) or _iso(message.get("timestamp"))
    )
    if timestamp is None:
        return None
    cost = _dict(raw.get("cost"))
    stop = _str(message.get("stopReason"))
    return Request(
        message_id=MESSAGE_PREFIX + event_id,
        session_id=SESSION_PREFIX + session_id,
        agent_id=None,
        project_dir=DIRECT_PROJECT,
        timestamp=timestamp,
        model=_str(message.get("model")) or "unknown",
        # OpenClaw's cacheWrite has no lifetime: taken as 5-minute writes (the
        # default on Anthropic's API; Gemini prices both lifetimes alike).
        usage=Usage(
            input=_int(raw.get("input")),
            cache_read=_int(raw.get("cacheRead")),
            cache_write_5m=_int(raw.get("cacheWrite")),
            output=_int(raw.get("output")),
        ),
        stop_reason=stop,
        is_api_error=stop == "error",
        reported_cost_usd=_float(cost.get("total")),
    )


def _is_wal(path: Path) -> bool:
    """True when the database header says WAL mode (write and read versions 2)."""
    try:
        with path.open("rb") as f:
            header = f.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[18] == 2 and header[19] == 2


def _connect(path: Path) -> sqlite3.Connection:
    """Open the database read-only, creating no file next to it. A WAL database
    without its -wal or -shm file is closed (`mode=ro` would create them): it is opened
    `immutable`. Otherwise OpenClaw may be writing it, so `mode=ro` without it."""
    path = path.resolve()
    mode = "ro"
    if _is_wal(path):
        sidecars = (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
        if not all(p.exists() for p in sidecars):
            mode = "ro&immutable=1"
    con = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def _columns(con: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in con.execute(f"PRAGMA table_info({TABLE})")}


def _select(cols: set[str]) -> str | None:
    """The batch query for the table's columns (None when it lacks the essentials).
    Rows whose JSON never mentions "usage" come back without it, so the big events
    (tool results, user turns) are not copied out of SQLite."""
    if not {"session_id", "event_json"} <= cols:
        return None
    created = "created_at" if "created_at" in cols else "NULL"
    zstd = "CASE WHEN event_json IS NULL THEN event_zstd END" if "event_zstd" in cols else "NULL"
    return (
        f"SELECT rowid, session_id, {created}, "
        f"CASE WHEN instr(event_json, '{_USAGE_KEY}') > 0 THEN event_json END, {zstd} "
        f"FROM {TABLE} WHERE rowid > ? ORDER BY rowid LIMIT ?"
    )


class _BatchBuilder:
    def __init__(self) -> None:
        self.records: list[Record] = []
        self.sessions: dict[str, tuple[datetime, datetime]] = {}
        self.rows = 0
        self.skipped = 0
        self.first: datetime | None = None

    def add(self, req: Request) -> None:
        self.records.append(req)
        if self.first is None:
            self.first = req.timestamp
        span = self.sessions.get(req.session_id)
        t = req.timestamp
        self.sessions[req.session_id] = (
            (t, t) if span is None else (min(span[0], t), max(span[1], t))
        )

    def batch(self, end: int) -> Batch:
        infos: list[Record] = [
            SessionInfo(
                session_id=sid,
                project_dir=DIRECT_PROJECT,
                entrypoint="openclaw",
                first_seen=first,
                last_seen=last,
            )
            for sid, (first, last) in self.sessions.items()
        ]
        return Batch(
            [*self.records, *infos],
            end,
            lines=self.rows,
            skipped=self.skipped,
            first_timestamp=self.first,
        )


class OpenClawSource:
    """`capcom.types.Source` for OpenClaw's own databases (its direct provider calls)."""

    def __init__(self, batch_rows: int = MAX_BATCH_ROWS) -> None:
        self.batch_rows = batch_rows
        # Diagnostics: compressed rows this Python could not read (no compression.zstd).
        self.compressed_unread = 0
        # The lowest such rowid of the last parse of each database.
        self._unread_from: dict[Path, int] = {}

    def discover(self, root: Path) -> Iterator[LogFile]:
        """`agents/<agent>/agent/openclaw-agent.sqlite` under `root` (the OpenClaw state
        dir), by agent name."""
        try:
            agents = sorted(os.scandir(root / "agents"), key=lambda e: e.name)
        except OSError:
            return
        for entry in agents:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            path = Path(entry.path) / "agent" / DB_NAME
            if path.is_file():
                yield LogFile(
                    path=path,
                    kind=LogKind.OPENCLAW_DB,
                    project_dir=DIRECT_PROJECT,
                    session_id=entry.name,
                )

    def last_row(self, log: LogFile) -> int | None:
        """The highest rowid of the events table now (0 when empty), or None when the
        database or the table cannot be read."""
        try:
            con = _connect(log.path)
        except sqlite3.Error:
            return None
        try:
            row = con.execute(f"SELECT MAX(rowid) FROM {TABLE}").fetchone()
            return int(row[0] or 0)
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def row_key(self, log: LogFile, rowid: int) -> str | None:
        """The table key (session_id and seq) of the row at `rowid`, or None when there
        is no such row or the database cannot be read."""
        try:
            con = _connect(log.path)
        except sqlite3.Error:
            return None
        try:
            seq = "seq" if "seq" in _columns(con) else "NULL"
            row = con.execute(
                f"SELECT session_id, {seq} FROM {TABLE} WHERE rowid = ?", (rowid,)
            ).fetchone()
            return None if row is None else f"{row[0]}\x1f{row[1]}"
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def reads_compressed(self) -> bool:
        """True when this Python can read the compressed rows."""
        return _DECOMPRESS is not None

    def unread_from(self, log: LogFile) -> int | None:
        """The lowest rowid of a compressed row the last parse of `log` left unread."""
        return self._unread_from.get(log.path)

    def parse(self, log: LogFile, start: int) -> Iterator[Batch]:
        """The direct replies in the rows above rowid `start`, in batches of at most
        `batch_rows` rows; each batch's end_offset is its last rowid. Yields nothing
        when there is no new row or the database cannot be read; a read error stops
        the stream after the batches already yielded."""
        self._unread_from.pop(log.path, None)
        try:
            con = _connect(log.path)
        except sqlite3.Error:
            return
        try:
            sql = _select(_columns(con))
            if sql is None:
                return
            cursor = start
            while True:
                b = _BatchBuilder()
                last = cursor
                # One short query per batch: no read snapshot stays open on the live
                # database while the index stores the batch.
                for rowid, session, created, text, blob in con.execute(
                    sql, (cursor, self.batch_rows)
                ):
                    last = int(rowid)
                    b.rows += 1
                    if not self._row(b, session, created, text, blob):
                        self._unread_from.setdefault(log.path, last)
                if b.rows == 0:
                    return
                yield b.batch(last)
                if b.rows < self.batch_rows:
                    return
                cursor = last
        except sqlite3.Error:
            return
        finally:
            con.close()

    def _row(
        self, b: _BatchBuilder, session: object, created: object, text: object, blob: object
    ) -> bool:
        """Read one row into `b`; False for a compressed row this Python cannot read."""
        if not isinstance(session, str) or not session:
            return True
        data: str | bytes
        if isinstance(text, str):
            data = text
        elif isinstance(blob, bytes):
            if _DECOMPRESS is None:
                self.compressed_unread += 1
                b.skipped += 1
                return False
            try:
                data = _DECOMPRESS(blob)
            except Exception:  # a corrupt frame: skip the row, never the scan
                b.skipped += 1
                return True
            if _USAGE_KEY.encode() not in data:
                return True
        else:
            return True  # no usage in this event
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            b.skipped += 1
            return True
        if not isinstance(event, dict):
            b.skipped += 1
            return True
        req = event_request(event, session, created)
        if req is not None:
            b.add(req)
        return True
