"""The persistent index: an SQLite database of the normalized records.

One database file (`config.index_path`, WAL mode). Every row remembers the log file
it came from, so `forget_file` can drop a file's rows before it is read again from 0.
The index outlives the logs: a file Claude Code deleted keeps its rows.

Tables (schema version in `schema_version`):

- files: path -> inode, size, mtime_ns, pos (the FileState of each log)
- requests: one row per message_id (the latest record wins), tool calls as JSON
- events: one row per (file, key), attrs as JSON
- session_parts, agent_parts: what each file says about a session or agent, merged
  per field when read (`sessions()`, `agents()`), so forgetting a file drops only
  its contribution
- cost_states: one row per (file, pos); the last per session has the highest pos

Times are stored as integer microseconds since the Unix epoch, UTC.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from capcom.types import (
    AgentInfo,
    AttrValue,
    Batch,
    CostState,
    Event,
    EventKind,
    FileState,
    LogFile,
    ModelUsage,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)

SCHEMA_VERSION = 1

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    pos INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    message_id TEXT PRIMARY KEY,
    file TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    project_dir TEXT NOT NULL,
    ts INTEGER NOT NULL,
    model TEXT NOT NULL,
    input INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL,
    output INTEGER NOT NULL,
    thinking INTEGER NOT NULL,
    web_searches INTEGER NOT NULL,
    web_fetches INTEGER NOT NULL,
    request_id TEXT,
    cwd TEXT,
    version TEXT,
    effort TEXT,
    speed TEXT,
    service_tier TEXT,
    inference_geo TEXT,
    stop_reason TEXT,
    is_api_error INTEGER NOT NULL,
    output_estimated INTEGER NOT NULL,
    tool_calls TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts);
CREATE INDEX IF NOT EXISTS requests_session_ts ON requests (session_id, ts);
CREATE INDEX IF NOT EXISTS requests_file ON requests (file);

CREATE TABLE IF NOT EXISTS events (
    file TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    ts INTEGER NOT NULL,
    attrs TEXT,
    PRIMARY KEY (file, key)
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_session_ts ON events (session_id, ts);

CREATE TABLE IF NOT EXISTS session_parts (
    file TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    project_dir TEXT NOT NULL,
    cwd TEXT,
    title TEXT,
    version TEXT,
    entrypoint TEXT,
    git_branch TEXT,
    first_seen INTEGER,
    last_seen INTEGER,
    PRIMARY KEY (file, session_id)
);

CREATE TABLE IF NOT EXISTS agent_parts (
    file TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    agent_type TEXT,
    description TEXT,
    tool_use_id TEXT,
    spawn_depth INTEGER,
    is_built_in INTEGER,
    finished INTEGER,
    PRIMARY KEY (file, agent_id)
);

CREATE TABLE IF NOT EXISTS cost_states (
    file TEXT NOT NULL,
    pos INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    total_cost_usd REAL NOT NULL,
    models TEXT,
    start_time_ms INTEGER,
    has_unknown_model_cost INTEGER,
    PRIMARY KEY (file, pos)
);
CREATE INDEX IF NOT EXISTS cost_states_session ON cost_states (session_id, pos);
"""

_REQUEST_COLS = (
    "message_id, file, session_id, agent_id, project_dir, ts, model, input, cache_read, "
    "cache_write_5m, cache_write_1h, output, thinking, web_searches, web_fetches, "
    "request_id, cwd, version, effort, speed, service_tier, inference_geo, stop_reason, "
    "is_api_error, output_estimated, tool_calls"
)
_INSERT_REQUEST = (
    f"INSERT OR REPLACE INTO requests ({_REQUEST_COLS}) VALUES ({', '.join('?' * 26)})"
)
_SELECT_REQUEST = f"SELECT {_REQUEST_COLS.replace('file, ', '')} FROM requests"

_INSERT_EVENT = (
    "INSERT OR REPLACE INTO events (file, key, kind, session_id, agent_id, ts, attrs) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# Merge within one file: later non-None values win; first_seen min, last_seen max.
_UPSERT_SESSION = """
INSERT INTO session_parts (file, session_id, seq, project_dir, cwd, title, version,
    entrypoint, git_branch, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (file, session_id) DO UPDATE SET
    seq = excluded.seq,
    project_dir = excluded.project_dir,
    cwd = COALESCE(excluded.cwd, cwd),
    title = COALESCE(excluded.title, title),
    version = COALESCE(excluded.version, version),
    entrypoint = COALESCE(excluded.entrypoint, entrypoint),
    git_branch = COALESCE(excluded.git_branch, git_branch),
    first_seen = MIN(COALESCE(excluded.first_seen, first_seen),
                     COALESCE(first_seen, excluded.first_seen)),
    last_seen = MAX(COALESCE(excluded.last_seen, last_seen),
                    COALESCE(last_seen, excluded.last_seen))
"""

_UPSERT_AGENT = """
INSERT INTO agent_parts (file, agent_id, seq, session_id, agent_type, description,
    tool_use_id, spawn_depth, is_built_in, finished)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (file, agent_id) DO UPDATE SET
    seq = excluded.seq,
    session_id = excluded.session_id,
    agent_type = COALESCE(excluded.agent_type, agent_type),
    description = COALESCE(excluded.description, description),
    tool_use_id = COALESCE(excluded.tool_use_id, tool_use_id),
    spawn_depth = COALESCE(excluded.spawn_depth, spawn_depth),
    is_built_in = COALESCE(excluded.is_built_in, is_built_in),
    finished = COALESCE(excluded.finished, finished)
"""

_INSERT_COST = (
    "INSERT OR REPLACE INTO cost_states (file, pos, session_id, total_cost_usd, models, "
    "start_time_ms, has_unknown_model_cost) VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_MODEL_FIELDS = tuple(f.name for f in fields(ModelUsage))


def _us(t: datetime) -> int:
    return (t - _EPOCH) // _US


def _us_opt(t: datetime | None) -> int | None:
    return None if t is None else (t - _EPOCH) // _US


def _dt(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def _dt_opt(us: int | None) -> datetime | None:
    return None if us is None else _EPOCH + timedelta(microseconds=us)


def _bool_opt(v: int | None) -> bool | None:
    return None if v is None else bool(v)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _attr(v: Any) -> AttrValue:
    return tuple(v) if isinstance(v, list) else v


def _range(
    since: datetime | None, until: datetime | None, session_id: str | None
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        args.append(_us(since))
    if until is not None:
        where.append("ts < ?")
        args.append(_us(until))
    if session_id is not None:
        where.append("session_id = ?")
        args.append(session_id)
    return where, args


def _sql(base: str, where: list[str], order: str) -> str:
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return f"{base}{clause} ORDER BY {order}"


class SqliteIndex:
    """`capcom.types.Index` on an SQLite database. Use `open_index` to make one."""

    def __init__(self, con: sqlite3.Connection, path: Path, readonly: bool) -> None:
        self._con = con
        self.path = path
        self.readonly = readonly
        self._seq = 0

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> SqliteIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        cur.execute("COMMIT")

    def _next_seq(self) -> int:
        # Orders the per-file session and agent parts: the latest write wins a field.
        self._seq = max(self._seq + 1, time.time_ns())
        return self._seq

    # ---------------------------------------------------------------- writes

    def file_state(self, path: Path) -> FileState | None:
        row = self._con.execute(
            "SELECT inode, size, mtime_ns, pos FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        return None if row is None else FileState(path, *row)

    def apply(self, log: LogFile, batch: Batch, state: FileState) -> None:
        """Store a batch and the file's new state in one transaction."""
        file = str(log.path)
        reqs: list[tuple[Any, ...]] = []
        evs: list[tuple[Any, ...]] = []
        sess: list[tuple[Any, ...]] = []
        agents: list[tuple[Any, ...]] = []
        costs: list[tuple[Any, ...]] = []
        seq = self._next_seq()
        for r in batch.records:
            if isinstance(r, Request):
                u = r.usage
                tools = (
                    _dumps([[c.tool_use_id, c.name, c.target] for c in r.tool_calls])
                    if r.tool_calls
                    else None
                )
                reqs.append(
                    (
                        r.message_id,
                        file,
                        r.session_id,
                        r.agent_id,
                        r.project_dir,
                        _us(r.timestamp),
                        r.model,
                        u.input,
                        u.cache_read,
                        u.cache_write_5m,
                        u.cache_write_1h,
                        u.output,
                        u.thinking,
                        u.web_searches,
                        u.web_fetches,
                        r.request_id,
                        r.cwd,
                        r.version,
                        r.effort,
                        r.speed,
                        r.service_tier,
                        r.inference_geo,
                        r.stop_reason,
                        int(r.is_api_error),
                        int(r.output_estimated),
                        tools,
                    )
                )
            elif isinstance(r, Event):
                attrs = _dumps(dict(r.attrs)) if r.attrs else None
                evs.append(
                    (file, r.key, str(r.kind), r.session_id, r.agent_id, _us(r.timestamp), attrs)
                )
            elif isinstance(r, SessionInfo):
                sess.append(
                    (
                        file,
                        r.session_id,
                        seq,
                        r.project_dir,
                        r.cwd,
                        r.title,
                        r.version,
                        r.entrypoint,
                        r.git_branch,
                        _us_opt(r.first_seen),
                        _us_opt(r.last_seen),
                    )
                )
            elif isinstance(r, AgentInfo):
                agents.append(
                    (
                        file,
                        r.agent_id,
                        seq,
                        r.session_id,
                        r.agent_type,
                        r.description,
                        r.tool_use_id,
                        r.spawn_depth,
                        None if r.is_built_in is None else int(r.is_built_in),
                        None if r.finished is None else int(r.finished),
                    )
                )
            elif isinstance(r, CostState):
                models = (
                    _dumps({m: [getattr(v, f) for f in _MODEL_FIELDS] for m, v in r.models.items()})
                    if r.models
                    else None
                )
                costs.append(
                    (
                        file,
                        r.offset,
                        r.session_id,
                        r.total_cost_usd,
                        models,
                        r.start_time_ms,
                        None if r.has_unknown_model_cost is None else int(r.has_unknown_model_cost),
                    )
                )
            else:
                raise TypeError(f"not a capcom record: {type(r).__name__}")
        with self._tx() as cur:
            if reqs:
                cur.executemany(_INSERT_REQUEST, reqs)
            if evs:
                cur.executemany(_INSERT_EVENT, evs)
            if sess:
                cur.executemany(_UPSERT_SESSION, sess)
            if agents:
                cur.executemany(_UPSERT_AGENT, agents)
            if costs:
                cur.executemany(_INSERT_COST, costs)
            cur.execute(
                "INSERT OR REPLACE INTO files (path, inode, size, mtime_ns, pos) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(state.path), state.inode, state.size, state.mtime_ns, state.offset),
            )

    def forget_file(self, path: Path) -> None:
        """Drop every row that came from `path`, and its FileState."""
        file = str(path)
        with self._tx() as cur:
            for table in ("requests", "events", "session_parts", "agent_parts", "cost_states"):
                cur.execute(f"DELETE FROM {table} WHERE file = ?", (file,))
            cur.execute("DELETE FROM files WHERE path = ?", (file,))

    # ---------------------------------------------------------------- reads

    def requests(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
    ) -> Iterator[Request]:
        """Requests ordered by timestamp, `since` inclusive, `until` exclusive."""
        where, args = _range(since, until, session_id)
        sql = _sql(_SELECT_REQUEST, where, "ts, message_id")
        for row in self._con.execute(sql, args):
            tools = (
                tuple(ToolCall(t[0], t[1], t[2]) for t in json.loads(row[24])) if row[24] else ()
            )
            yield Request(
                message_id=row[0],
                session_id=row[1],
                agent_id=row[2],
                project_dir=row[3],
                timestamp=_dt(row[4]),
                model=row[5],
                usage=Usage(row[6], row[7], row[8], row[9], row[10], row[11], row[12], row[13]),
                request_id=row[14],
                cwd=row[15],
                version=row[16],
                effort=row[17],
                speed=row[18],
                service_tier=row[19],
                inference_geo=row[20],
                stop_reason=row[21],
                is_api_error=bool(row[22]),
                output_estimated=bool(row[23]),
                tool_calls=tools,
            )

    def events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
        kinds: frozenset[EventKind] | None = None,
    ) -> Iterator[Event]:
        """Events ordered by timestamp, `since` inclusive, `until` exclusive."""
        where, args = _range(since, until, session_id)
        if kinds is not None:
            if not kinds:
                return
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args.extend(sorted(str(k) for k in kinds))
        base = "SELECT kind, session_id, agent_id, ts, key, attrs FROM events"
        for kind, session, agent, ts, key, attrs in self._con.execute(
            _sql(base, where, "ts, file, key"), args
        ):
            loaded: Mapping[str, AttrValue] = (
                {k: _attr(v) for k, v in json.loads(attrs).items()} if attrs else {}
            )
            yield Event(EventKind(kind), session, agent, _dt(ts), key, loaded)

    def sessions(self) -> Iterator[SessionInfo]:
        """One merged SessionInfo per session, ordered by first_seen."""
        merged: dict[str, list[Any]] = {}
        for row in self._con.execute(
            "SELECT session_id, project_dir, cwd, title, version, entrypoint, git_branch, "
            "first_seen, last_seen FROM session_parts ORDER BY seq"
        ):
            cur = merged.get(row[0])
            if cur is None:
                merged[row[0]] = list(row)
                continue
            cur[1] = row[1]
            for i in range(2, 7):
                if row[i] is not None:
                    cur[i] = row[i]
            if row[7] is not None:
                cur[7] = row[7] if cur[7] is None else min(cur[7], row[7])
            if row[8] is not None:
                cur[8] = row[8] if cur[8] is None else max(cur[8], row[8])
        out = sorted(merged.values(), key=lambda m: (m[7] is None, m[7] or 0, m[0]))
        for m in out:
            yield SessionInfo(
                m[0], m[1], m[2], m[3], m[4], m[5], m[6], _dt_opt(m[7]), _dt_opt(m[8])
            )

    def agents(self, session_id: str | None = None) -> Iterator[AgentInfo]:
        """One merged AgentInfo per agent, ordered by agent_id."""
        sql = (
            "SELECT agent_id, session_id, agent_type, description, tool_use_id, spawn_depth, "
            "is_built_in, finished FROM agent_parts"
        )
        args: tuple[str, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            args = (session_id,)
        merged: dict[str, list[Any]] = {}
        for row in self._con.execute(sql + " ORDER BY seq", args):
            cur = merged.get(row[0])
            if cur is None:
                merged[row[0]] = list(row)
                continue
            cur[1] = row[1]
            for i in range(2, 8):
                if row[i] is not None:
                    cur[i] = row[i]
        for key in sorted(merged):
            m = merged[key]
            yield AgentInfo(m[0], m[1], m[2], m[3], m[4], m[5], _bool_opt(m[6]), _bool_opt(m[7]))

    def last_cost_states(self) -> Iterator[CostState]:
        """The cost-state with the highest offset of each session that has one."""
        # SQLite returns the bare columns of the row holding MAX(pos).
        for session, pos, total, models, start_ms, unknown, _ in self._con.execute(
            "SELECT session_id, pos, total_cost_usd, models, start_time_ms, "
            "has_unknown_model_cost, MAX(pos) FROM cost_states GROUP BY session_id "
            "ORDER BY session_id"
        ):
            loaded = (
                {
                    m: ModelUsage(**dict(zip(_MODEL_FIELDS, v, strict=False)))
                    for m, v in json.loads(models).items()
                }
                if models
                else {}
            )
            yield CostState(session, pos, total, loaded, start_ms, _bool_opt(unknown))


def open_index(path: Path, readonly: bool = False) -> SqliteIndex:
    """Open the index at `path` (normally `config.index_path`).

    Writable mode creates the folder and the schema. Readonly mode opens the database
    with `mode=ro` and raises FileNotFoundError when it does not exist yet.
    """
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(f"no capcom index at {path}: run `capcom scan` first")
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA busy_timeout = 10000")
    try:
        if not readonly:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.executescript(_SCHEMA)
            if con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                con.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
        version = row[0] if row else None
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"capcom index {path} has schema version {version}, this capcom reads "
                f"{SCHEMA_VERSION}"
            )
    except BaseException:
        con.close()
        raise
    return SqliteIndex(con, path, readonly)
