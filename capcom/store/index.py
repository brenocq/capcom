"""The persistent index: an SQLite database of the normalized records.

One database file (`config.index_path`, WAL mode). Every row remembers the log file
it came from, so `forget_file` can drop a file's rows before it is read again from 0.
The index outlives the logs: a file Claude Code deleted keeps its rows.

Tables (schema version in `schema_version`):

- files: path -> inode, size, mtime_ns, pos (the FileState of each log; for OpenClaw's
  database, pos is the last rowid read), and what the scan checks besides: a hash of
  the file's head, the meta.json's mtime
- requests: one row per message_id (the latest record wins, but tool calls are the
  union of every record's by tool_use_id, and an estimated output never shrinks),
  tool calls as JSON
- events: one row per (file, key), attrs as JSON
- session_parts, agent_parts: what each file says about a session or agent, each
  field with the sequence number of the write that set it (from `counters`), merged
  per field when read, so forgetting a file drops only its contribution
- cost_states: one row per (file, pos), flagged when it comes from its session's
  main log; the last per session is the main log's highest pos. `next_ts` is the time
  of the first timestamped record after the line: the source fills it within a parse
  call, and `apply` fills what is still missing from the next batch's first_timestamp
- shared_replies: (session_id, message_id) of each reply found in the logs of two
  sessions (a forked or copied session). `requests` keeps one row per message_id, so
  the session whose row was replaced lost that request; found when a write replaces a
  row of another session, and kept as history (forget_file leaves it)

Version 2 lacked cost_states.ts, version 3 cost_states.next_ts and shared_replies,
version 4 filled next_ts from the earliest time of the next batch, and version 5 lacked
requests.reported_cost (OpenClaw's own cost estimate), version 6
cost_states.total_duration_ms, version 7 session_parts.custom_title and color: a writable open
migrates them in place (the index keeps history the logs may have lost); up to 5 it
queues the files with cost-states, and every file of their sessions, for a reread,
5 -> 6 only adds the column, 6 -> 7 queues the files with cost-states alone, and 7 -> 8
the session logs that hold a custom-title or agent-color record, and 8 -> 9 (prompt
events) every Claude Code main session log.

Times are stored as integer microseconds since the Unix epoch, UTC.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from capcom.config import load_config
from capcom.types import (
    AgentInfo,
    AttrValue,
    Batch,
    CostState,
    Event,
    EventKind,
    FileState,
    LogFile,
    LogKind,
    ModelUsage,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)

SCHEMA_VERSION = 9

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)
_IN_CHUNK = 500  # ids per `IN (...)` query
_SESSION, _OUTPUT, _ESTIMATED = 2, 11, 24  # positions in a request row

_SESSION_FIELDS = (
    "project_dir",
    "cwd",
    "title",
    "version",
    "entrypoint",
    "git_branch",
    "custom_title",
    "color",
)
_AGENT_FIELDS = (
    "session_id",
    "agent_type",
    "description",
    "tool_use_id",
    "spawn_depth",
    "is_built_in",
    "finished",
)


def _seq_cols(names: Sequence[str], typ: str = "") -> str:
    return ",\n    ".join(f"{n} {typ}, {n}_seq INTEGER".replace("  ", " ") for n in names)


_SHARED = """
CREATE TABLE IF NOT EXISTS shared_replies (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (session_id, message_id)
) WITHOUT ROWID;
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO counters (name, value) VALUES ('seq', 0);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    pos INTEGER NOT NULL,
    head_len INTEGER,
    head_hash TEXT,
    meta_mtime_ns INTEGER
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
    tool_calls TEXT,
    reported_cost REAL
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
    {_seq_cols(_SESSION_FIELDS)},
    first_seen INTEGER,
    last_seen INTEGER,
    PRIMARY KEY (file, session_id)
);

CREATE TABLE IF NOT EXISTS agent_parts (
    file TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    {_seq_cols(_AGENT_FIELDS)},
    PRIMARY KEY (file, agent_id)
);

CREATE TABLE IF NOT EXISTS cost_states (
    file TEXT NOT NULL,
    pos INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    main INTEGER NOT NULL,
    total_cost_usd REAL NOT NULL,
    models TEXT,
    start_time_ms INTEGER,
    has_unknown_model_cost INTEGER,
    ts INTEGER,
    next_ts INTEGER,
    total_duration_ms INTEGER,
    PRIMARY KEY (file, pos)
);
CREATE INDEX IF NOT EXISTS cost_states_session ON cost_states (session_id, main, pos);
{_SHARED}"""

_REQUEST_COLS = (
    "message_id, file, session_id, agent_id, project_dir, ts, model, input, cache_read, "
    "cache_write_5m, cache_write_1h, output, thinking, web_searches, web_fetches, "
    "request_id, cwd, version, effort, speed, service_tier, inference_geo, stop_reason, "
    "is_api_error, output_estimated, tool_calls, reported_cost"
)
_INSERT_REQUEST = (
    f"INSERT OR REPLACE INTO requests ({_REQUEST_COLS}) VALUES ({', '.join('?' * 27)})"
)
_SELECT_REQUEST = f"SELECT {_REQUEST_COLS.replace('file, ', '')} FROM requests"

_INSERT_EVENT = (
    "INSERT OR REPLACE INTO events (file, key, kind, session_id, agent_id, ts, attrs) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _upsert_part(table: str, key: str, names: Sequence[str], extra: Sequence[str] = ()) -> str:
    """Merge within one file: a field (and its seq) changes only when the new value is
    non-None; `first_seen` keeps the minimum and `last_seen` the maximum."""
    cols = ["file", key, *(c for n in names for c in (n, f"{n}_seq")), *extra]
    sets = []
    for n in names:
        sets.append(f"{n} = COALESCE(excluded.{n}, {n})")
        sets.append(
            f"{n}_seq = CASE WHEN excluded.{n} IS NULL THEN {n}_seq ELSE excluded.{n}_seq END"
        )
    if "first_seen" in extra:
        sets.append(
            "first_seen = MIN(COALESCE(excluded.first_seen, first_seen), "
            "COALESCE(first_seen, excluded.first_seen))"
        )
    if "last_seen" in extra:
        sets.append(
            "last_seen = MAX(COALESCE(excluded.last_seen, last_seen), "
            "COALESCE(last_seen, excluded.last_seen))"
        )
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT (file, {key}) DO UPDATE SET {', '.join(sets)}"
    )


_UPSERT_SESSION = _upsert_part(
    "session_parts", "session_id", _SESSION_FIELDS, ("first_seen", "last_seen")
)
_UPSERT_AGENT = _upsert_part("agent_parts", "agent_id", _AGENT_FIELDS)

_INSERT_COST = (
    "INSERT OR REPLACE INTO cost_states (file, pos, session_id, main, total_cost_usd, models, "
    "start_time_ms, has_unknown_model_cost, ts, next_ts, total_duration_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_UPSERT_FILE = (
    "INSERT INTO files (path, inode, size, mtime_ns, pos) VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (path) DO UPDATE SET inode = excluded.inode, size = excluded.size, "
    "mtime_ns = excluded.mtime_ns, pos = excluded.pos"
)

_MODEL_FIELDS = tuple(f.name for f in fields(ModelUsage))


@dataclass(frozen=True, slots=True)
class FileExtra:
    """What the scan remembers about a log besides its FileState: a hash of its first
    `head_len` bytes (an in-place rewrite changes it) and its meta.json's mtime."""

    head_len: int = 0
    head_hash: str | None = None
    meta_mtime_ns: int | None = None


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


def _int_opt(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _attr(v: Any) -> AttrValue:
    return tuple(v) if isinstance(v, list) else v


def _union(old: list[list[Any]], new: list[list[Any]]) -> list[list[Any]]:
    """Tool calls of both records by tool_use_id: earlier ones first, newer values."""
    if not old:
        return new
    if not new:
        return old
    out = list(old)
    where = {c[0]: i for i, c in enumerate(out)}
    for c in new:
        i = where.get(c[0])
        if i is None:
            where[c[0]] = len(out)
            out.append(c)
        else:
            out[i] = c
    return out


def _aware(name: str, t: datetime) -> int:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime (capcom times are UTC)")
    return _us(t)


def _range(
    since: datetime | None, until: datetime | None, session_id: str | None
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        args.append(_aware("since", since))
    if until is not None:
        where.append("ts < ?")
        args.append(_aware("until", until))
    if session_id is not None:
        where.append("session_id = ?")
        args.append(session_id)
    return where, args


def _sql(base: str, where: list[str], order: str) -> str:
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return f"{base}{clause} ORDER BY {order}"


def _merge_parts(
    rows: Iterable[Sequence[Any]], n_fields: int
) -> dict[str, tuple[list[Any], list[int]]]:
    """Fold per-file part rows (key, f1, f1_seq, f2, f2_seq, ..., rest...) into one
    value per field: the non-None value with the highest seq."""
    merged: dict[str, tuple[list[Any], list[int]]] = {}
    for row in rows:
        values = [row[1 + 2 * i] for i in range(n_fields)]
        seqs = [row[2 + 2 * i] or 0 if values[i] is not None else -1 for i in range(n_fields)]
        rest = list(row[1 + 2 * n_fields :])
        cur = merged.get(row[0])
        if cur is None:
            merged[row[0]] = ([*values, *rest], seqs)
            continue
        vals, best = cur
        for i in range(n_fields):
            if values[i] is not None and seqs[i] > best[i]:
                vals[i] = values[i]
                best[i] = seqs[i]
        for j, v in enumerate(rest):
            vals[n_fields + j] = v if vals[n_fields + j] is None else vals[n_fields + j]
            if v is not None and j == 0:  # first_seen: the minimum
                vals[n_fields] = min(vals[n_fields], v)
            if v is not None and j == 1:  # last_seen: the maximum
                vals[n_fields + 1] = max(vals[n_fields + 1], v)
    return merged


def is_under(path: Path, roots: Iterable[Path]) -> bool:
    p = path.expanduser().resolve()
    return any(p.is_relative_to(r.expanduser().resolve()) for r in roots)


def check_index_path(path: Path, roots: Iterable[Path]) -> None:
    """Refuse an index location inside a log folder: capcom never writes there."""
    roots = list(roots)
    if is_under(path, roots):
        raise ValueError(
            f"refusing to keep the capcom index at {path}: it is inside a log folder "
            f"({', '.join(map(str, roots))}), which capcom only reads"
        )


class SqliteIndex:
    """`capcom.types.Index` on an SQLite database. Use `open_index` to make one."""

    def __init__(self, con: sqlite3.Connection, path: Path, readonly: bool) -> None:
        self._con = con
        self.path = path
        self.readonly = readonly

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

    # ---------------------------------------------------------------- files

    def file_state(self, path: Path) -> FileState | None:
        row = self._con.execute(
            "SELECT inode, size, mtime_ns, pos FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        return None if row is None else FileState(path, *row)

    def file_extra(self, path: Path) -> FileExtra | None:
        row = self._con.execute(
            "SELECT head_len, head_hash, meta_mtime_ns FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        return None if row is None else FileExtra(row[0] or 0, row[1], row[2])

    def set_file_extra(self, path: Path, extra: FileExtra) -> None:
        """Record the head hash and meta mtime of a file the index already has."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE files SET head_len = ?, head_hash = ?, meta_mtime_ns = ? WHERE path = ?",
                (extra.head_len, extra.head_hash, extra.meta_mtime_ns, str(path)),
            )

    # ---------------------------------------------------------------- writes

    def apply(self, log: LogFile, batch: Batch, state: FileState) -> None:
        """Store a batch and the file's new state in one transaction."""
        file = str(log.path)
        reqs: dict[str, tuple[list[Any], list[list[Any]], float | None]] = {}
        evs: list[tuple[Any, ...]] = []
        sess: list[SessionInfo] = []
        agents: list[AgentInfo] = []
        costs: list[tuple[Any, ...]] = []
        for r in batch.records:
            if isinstance(r, Request):
                u = r.usage
                row = [
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
                ]
                tools = [[c.tool_use_id, c.name, c.target] for c in r.tool_calls]
                prev = reqs.pop(r.message_id, None)
                if prev is not None:
                    tools = _union(prev[1], tools)
                    if r.output_estimated:
                        row[_OUTPUT] = max(row[_OUTPUT], prev[0][_OUTPUT])
                reqs[r.message_id] = (row, tools, r.reported_cost_usd)
            elif isinstance(r, Event):
                attrs = _dumps(dict(r.attrs)) if r.attrs else None
                evs.append(
                    (file, r.key, str(r.kind), r.session_id, r.agent_id, _us(r.timestamp), attrs)
                )
            elif isinstance(r, SessionInfo):
                sess.append(r)
            elif isinstance(r, AgentInfo):
                agents.append(r)
            elif isinstance(r, CostState):
                models = (
                    _dumps({m: [getattr(v, f) for f in _MODEL_FIELDS] for m, v in r.models.items()})
                    if r.models
                    else None
                )
                main = int(log.kind == LogKind.SESSION and log.session_id == r.session_id)
                costs.append(
                    (
                        file,
                        r.offset,
                        r.session_id,
                        main,
                        r.total_cost_usd,
                        models,
                        r.start_time_ms,
                        _int_opt(r.has_unknown_model_cost),
                        None if r.timestamp is None else _aware("timestamp", r.timestamp),
                        None
                        if r.next_timestamp is None
                        else _aware("next_timestamp", r.next_timestamp),
                        r.total_duration_ms,
                    )
                )
            else:
                raise TypeError(f"not a capcom record: {type(r).__name__}")
        with self._tx() as cur:
            if batch.first_timestamp is not None:
                # This file's cost-states before this batch that no record followed yet:
                # the batch's first timestamped record, in file order, is the next one.
                row = cur.execute("SELECT pos FROM files WHERE path = ?", (file,)).fetchone()
                if row is not None and row[0] > 0:
                    cur.execute(
                        "UPDATE cost_states SET next_ts = ? "
                        "WHERE file = ? AND next_ts IS NULL AND pos < ?",
                        (_aware("first_timestamp", batch.first_timestamp), file, row[0]),
                    )
            if reqs:
                self._write_requests(cur, reqs)
            if evs:
                cur.executemany(_INSERT_EVENT, evs)
            if sess or agents:
                seq = cur.execute(
                    "UPDATE counters SET value = value + 1 WHERE name = 'seq' RETURNING value"
                ).fetchone()[0]
                if sess:
                    cur.executemany(_UPSERT_SESSION, [_session_row(file, s, seq) for s in sess])
                if agents:
                    cur.executemany(_UPSERT_AGENT, [_agent_row(file, a, seq) for a in agents])
            if costs:
                cur.executemany(_INSERT_COST, costs)
            cur.execute(
                _UPSERT_FILE,
                (str(state.path), state.inode, state.size, state.mtime_ns, state.offset),
            )

    @staticmethod
    def _write_requests(
        cur: sqlite3.Cursor, reqs: dict[str, tuple[list[Any], list[list[Any]], float | None]]
    ) -> None:
        # Records of one reply can arrive in different scans, a later one with fewer
        # tool calls or (when estimated) a smaller output than before: keep the union of
        # the tool calls, and the larger output when the new one is an estimate.
        # A row of another session replaced here: both sessions hold this reply.
        ids = list(reqs)
        old: dict[str, tuple[int, list[list[Any]], str]] = {}
        for i in range(0, len(ids), _IN_CHUNK):
            chunk = ids[i : i + _IN_CHUNK]
            for mid, output, tools, session in cur.execute(
                "SELECT message_id, output, tool_calls, session_id FROM requests "
                f"WHERE message_id IN ({', '.join('?' * len(chunk))})",
                chunk,
            ):
                old[mid] = (output, json.loads(tools) if tools else [], session)
        rows = []
        shared: list[tuple[str, str]] = []
        for mid, (row, tools, reported) in reqs.items():
            prev = old.get(mid)
            if prev is not None:
                tools = _union(prev[1], tools)
                if row[_ESTIMATED]:
                    row[_OUTPUT] = max(row[_OUTPUT], prev[0])
                if prev[2] != row[_SESSION]:
                    shared += [(prev[2], mid), (row[_SESSION], mid)]
            rows.append((*row, _dumps(tools) if tools else None, reported))
        cur.executemany(_INSERT_REQUEST, rows)
        if shared:
            cur.executemany(
                "INSERT OR IGNORE INTO shared_replies (session_id, message_id) VALUES (?, ?)",
                shared,
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
        """Requests ordered by timestamp, `since` inclusive, `until` exclusive. Naive
        datetimes raise ValueError."""
        where, args = _range(since, until, session_id)
        return self._requests(_sql(_SELECT_REQUEST, where, "ts, message_id"), args)

    def _requests(self, sql: str, args: list[Any]) -> Iterator[Request]:
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
                reported_cost_usd=row[25],
            )

    def events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
        kinds: frozenset[EventKind] | None = None,
    ) -> Iterator[Event]:
        """Events ordered by timestamp, `since` inclusive, `until` exclusive. Naive
        datetimes raise ValueError."""
        where, args = _range(since, until, session_id)
        if kinds is not None:
            if not kinds:
                return iter(())
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args.extend(sorted(str(k) for k in kinds))
        base = "SELECT kind, session_id, agent_id, ts, key, attrs FROM events"
        return self._events(_sql(base, where, "ts, file, key"), args)

    def _events(self, sql: str, args: list[Any]) -> Iterator[Event]:
        for kind, session, agent, ts, key, attrs in self._con.execute(sql, args):
            loaded: Mapping[str, AttrValue] = (
                {k: _attr(v) for k, v in json.loads(attrs).items()} if attrs else {}
            )
            yield Event(EventKind(kind), session, agent, _dt(ts), key, loaded)

    def sessions(self) -> Iterator[SessionInfo]:
        """One merged SessionInfo per session, ordered by first_seen."""
        cols = ", ".join(f"{n}, {n}_seq" for n in _SESSION_FIELDS)
        rows = self._con.execute(
            f"SELECT session_id, {cols}, first_seen, last_seen FROM session_parts"
        )
        n = len(_SESSION_FIELDS)
        merged = _merge_parts(rows, n)
        out = sorted(merged.items(), key=lambda kv: (kv[1][0][n] is None, kv[1][0][n] or 0, kv[0]))
        for sid, (v, _) in out:
            f = dict(zip(_SESSION_FIELDS, v, strict=False))
            # A name the user gave wins over Claude Code's; a cleared one ("") falls
            # back to it. A reset color ("") is none.
            custom = f["custom_title"] or None
            yield SessionInfo(
                sid,
                f["project_dir"],
                f["cwd"],
                custom or f["title"],
                f["version"],
                f["entrypoint"],
                f["git_branch"],
                _dt_opt(v[n]),
                _dt_opt(v[n + 1]),
                custom,
                f["color"] or None,
            )

    def agents(self, session_id: str | None = None) -> Iterator[AgentInfo]:
        """One merged AgentInfo per agent, ordered by agent_id."""
        cols = ", ".join(f"{n}, {n}_seq" for n in _AGENT_FIELDS)
        rows = self._con.execute(f"SELECT agent_id, {cols} FROM agent_parts")
        merged = _merge_parts(rows, len(_AGENT_FIELDS))
        for aid in sorted(merged):
            v = merged[aid][0]
            if session_id is not None and v[0] != session_id:
                continue
            yield AgentInfo(aid, v[0], v[1], v[2], v[3], v[4], _bool_opt(v[5]), _bool_opt(v[6]))

    def activity_days(self) -> Iterator[tuple[str, str | None, str, int, datetime]]:
        """(project_dir, agent_id, model, day, first) for each day each combination
        made a request: the day as whole days since the Unix epoch (UTC), `first` its
        first request that day. The website's colors read it to order projects, agent
        types and models by when each was first seen and active (one pass over the
        requests, some 30 ms for 100k)."""
        for project, agent, model, day, first in self._con.execute(
            "SELECT project_dir, agent_id, model, ts / 86400000000 AS day, MIN(ts) "
            "FROM requests GROUP BY project_dir, agent_id, model, day"
        ):
            yield project, agent, model, int(day), _dt(first)

    def last_cost_states(self) -> Iterator[CostState]:
        """The last cost-state of each session: the highest offset in the session's
        main log, or in its other files when the main log has none."""
        for row in self._con.execute(
            f"SELECT {_COST_COLS} FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY "
            "session_id ORDER BY main DESC, pos DESC, file) AS rank FROM cost_states) "
            "WHERE rank = 1 ORDER BY session_id"
        ):
            yield _cost_state(row)

    def cost_states(self, session_id: str) -> Iterator[CostState]:
        """Every cost-state of the file that holds the session's last one (its main
        log, or another file when the main log has none), in file order."""
        row = self._con.execute(
            "SELECT file FROM cost_states WHERE session_id = ? "
            "ORDER BY main DESC, pos DESC, file LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        for r in self._con.execute(
            f"SELECT {_COST_COLS} FROM cost_states WHERE session_id = ? AND file = ? ORDER BY pos",
            (session_id, row[0]),
        ):
            yield _cost_state(r)

    def process_starts(self, session_id: str | None = None) -> dict[str, list[int]]:
        """The distinct process start times (cost-state `startTime`, epoch ms) of
        each session, oldest first: one per Claude Code process that wrote a
        cost-state (a resumed session has several)."""
        sql = "SELECT DISTINCT session_id, start_time_ms FROM cost_states "
        sql += "WHERE start_time_ms IS NOT NULL"
        args: list[Any] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            args.append(session_id)
        out: dict[str, list[int]] = {}
        for sid, start in self._con.execute(sql + " ORDER BY session_id, start_time_ms", args):
            out.setdefault(sid, []).append(int(start))
        return out

    def shared_replies(
        self, session_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        """How many replies of `session_id` with a time in [since, until) are also in
        another session's log and whose request row that other session holds, so this
        session is missing them (the session that kept the row counts none)."""
        where, args = _range(since, until, None)
        where = [f"r.{w}" for w in where]
        sql = (
            "SELECT COUNT(*) FROM shared_replies s JOIN requests r USING (message_id) "
            f"WHERE {' AND '.join(['s.session_id = ?', 'r.session_id != ?', *where])}"
        )
        return int(self._con.execute(sql, [session_id, session_id, *args]).fetchone()[0])


_COST_COLS = (
    "session_id, pos, total_cost_usd, models, start_time_ms, has_unknown_model_cost, ts, "
    "next_ts, total_duration_ms"
)


def _cost_state(row: tuple[Any, ...]) -> CostState:
    session, pos, total, models, start_ms, unknown, ts, next_ts, duration = row
    loaded = (
        {
            m: ModelUsage(**dict(zip(_MODEL_FIELDS, v, strict=False)))
            for m, v in json.loads(models).items()
        }
        if models
        else {}
    )
    return CostState(
        session,
        pos,
        total,
        loaded,
        start_ms,
        _bool_opt(unknown),
        _dt_opt(ts),
        _dt_opt(next_ts),
        duration,
    )


def _session_row(file: str, s: SessionInfo, seq: int) -> tuple[Any, ...]:
    vals = (
        s.project_dir,
        s.cwd,
        s.title,
        s.version,
        s.entrypoint,
        s.git_branch,
        s.custom_title,
        s.color,
    )
    pairs = [x for v in vals for x in (v, None if v is None else seq)]
    return (file, s.session_id, *pairs, _us_opt(s.first_seen), _us_opt(s.last_seen))


def _agent_row(file: str, a: AgentInfo, seq: int) -> tuple[Any, ...]:
    vals = (
        a.session_id,
        a.agent_type,
        a.description,
        a.tool_use_id,
        a.spawn_depth,
        _int_opt(a.is_built_in),
        _int_opt(a.finished),
    )
    pairs = [x for v in vals for x in (v, None if v is None else seq)]
    return (file, a.agent_id, *pairs)


def _migrate_2_to_3(con: sqlite3.Connection) -> None:
    """Add cost_states.ts and set the offset of every file with cost-states to 0, so
    the next scan reads them again (idempotent: rows upsert by their keys) and fills
    the time in. One transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2:
            con.execute("ALTER TABLE cost_states ADD COLUMN ts INTEGER")
            con.execute("UPDATE files SET pos = 0 WHERE path IN (SELECT file FROM cost_states)")
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (3)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _migrate_3_to_4(con: sqlite3.Connection) -> None:
    """Add cost_states.next_ts and shared_replies, and queue every file with
    cost-states for a reread (as 2 -> 3 does): the reread fills next_ts and, as it
    replaces request rows another session wrote last, finds the shared replies of
    every session that has a cost-state. One transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3:
            con.execute("ALTER TABLE cost_states ADD COLUMN next_ts INTEGER")
            con.execute(_SHARED)
            _queue_cost_sessions(con)
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (4)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _queue_cost_sessions(con: sqlite3.Connection) -> None:
    """Set to 0 the offset of every file with cost-states and of every file holding
    requests of a session with a cost-state (its subagent logs too), so the next scan
    rereads them: rows upsert by their keys, next_ts is filled again, and a reply
    another session's row holds is found as the reread replaces it."""
    con.execute("UPDATE files SET pos = 0 WHERE path IN (SELECT file FROM cost_states)")
    con.execute(
        "UPDATE files SET pos = 0 WHERE path IN (SELECT file FROM requests "
        "WHERE session_id IN (SELECT session_id FROM cost_states))"
    )


def _migrate_4_to_5(con: sqlite3.Connection) -> None:
    """No new column: version 4 filled next_ts from the earliest time of the file's
    next batch (not the first in file order) and, when migrated from 3, missed the
    replies shared through subagent logs. Queue the same files as 3 -> 4 for a
    reread. One transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 4:
            _queue_cost_sessions(con)
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (5)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _migrate_5_to_6(con: sqlite3.Connection) -> None:
    """Add requests.reported_cost (the cost a source reported itself, OpenClaw's
    estimate for its direct calls). No reread: Claude Code's rows have none, and the
    OpenClaw database was not read before this version. One transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5:
            cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
            if "reported_cost" not in cols:
                con.execute("ALTER TABLE requests ADD COLUMN reported_cost REAL")
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (6)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _migrate_6_to_7(con: sqlite3.Connection) -> None:
    """Add cost_states.total_duration_ms (cost-state's totalDuration, for comparing
    per process step) and queue the files that hold cost-states for a reread, so the
    column is filled. Only those: no other row changes, so the subagent logs and
    other files of their sessions stay as they are. One transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 6:
            cols = {r[1] for r in con.execute("PRAGMA table_info(cost_states)")}
            if "total_duration_ms" not in cols:
                con.execute("ALTER TABLE cost_states ADD COLUMN total_duration_ms INTEGER")
            con.execute("UPDATE files SET pos = 0 WHERE path IN (SELECT file FROM cost_states)")
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (7)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


# The records schema 8 added (a session's name and color), as Claude Code writes them.
_NAME_MARKERS = (b'"type":"custom-title"', b'"type":"agent-color"')


def _holds(path: str, markers: Sequence[bytes], chunk: int = 1 << 20) -> bool:
    """Whether the file at `path` contains any of `markers` (False when it cannot be
    read). Streams it read-only, a chunk at a time."""
    keep = max(len(m) for m in markers) - 1
    tail = b""
    try:
        with open(path, "rb") as f:
            while block := f.read(chunk):
                seam = tail + block[:keep]
                if any(m in block or m in seam for m in markers):
                    return True
                tail = block[-keep:]
    except OSError:
        return False
    return False


def _migrate_7_to_8(con: sqlite3.Connection) -> None:
    """Add session_parts.custom_title (the name the user gave a session, which wins
    over its ai-title) and session_parts.color, and queue for a reread the Claude Code
    session logs that hold a custom-title or agent-color record, found by a byte search
    of each main log (subagent logs never hold one) before the transaction. Only
    those: every other row stays. One transaction."""
    if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] != 7:
        return
    paths = [r[0] for r in con.execute("SELECT path FROM files")]
    queued = [
        p
        for p in paths
        if p.endswith(".jsonl") and Path(p).parent.name != "subagents" and _holds(p, _NAME_MARKERS)
    ]
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 7:
            cols = {r[1] for r in con.execute("PRAGMA table_info(session_parts)")}
            for name in ("custom_title", "color"):
                if name not in cols:
                    con.execute(f"ALTER TABLE session_parts ADD COLUMN {name}")
                    con.execute(f"ALTER TABLE session_parts ADD COLUMN {name}_seq INTEGER")
            con.executemany("UPDATE files SET pos = 0 WHERE path = ?", [(p,) for p in queued])
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (8)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _migrate_8_to_9(con: sqlite3.Connection) -> None:
    """No new table or column: version 9 adds the prompt events (a message the user
    sent), so every Claude Code main session log (`projects/<dir>/<id>.jsonl`, the
    only place prompts are written) is queued for a reread. Events are keyed by file
    and record, so a reread adds the new ones and rewrites the rest in place; logs
    Claude Code has since deleted keep their rows, without prompts. One transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8:
            paths = [r[0] for r in con.execute("SELECT path FROM files")]
            queued = [
                p
                for p in paths
                if p.endswith(".jsonl") and Path(p).parent.parent.name == "projects"
            ]
            con.executemany("UPDATE files SET pos = 0 WHERE path = ?", [(p,) for p in queued])
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (9)")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def open_index(path: Path, readonly: bool = False) -> SqliteIndex:
    """Open the index at `path` (normally `config.index_path`).

    Writable mode creates the folder and the schema. Readonly mode opens the database
    with `mode=ro` and raises FileNotFoundError when it does not exist yet. A path
    inside the Claude Code or OpenClaw folders raises ValueError.
    """
    config = load_config()
    check_index_path(path, (config.claude_root, config.openclaw_root))
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
            con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if row[0] is None:
                con.executescript(_SCHEMA)
                con.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                if row[0] == 2:
                    _migrate_2_to_3(con)
                if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3:
                    _migrate_3_to_4(con)
                if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 4:
                    _migrate_4_to_5(con)
                if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5:
                    _migrate_5_to_6(con)
                if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 6:
                    _migrate_6_to_7(con)
                if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 7:
                    _migrate_7_to_8(con)
                if con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8:
                    _migrate_8_to_9(con)
        row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
        version = row[0] if row else None
        if version != SCHEMA_VERSION:
            # older versions were migrated above: a newer index needs a newer capcom
            newer = isinstance(version, int) and version > SCHEMA_VERSION
            fix = (
                f"a newer capcom wrote it; update this one ({sys.argv[0]})"
                if newer
                else "run `capcom scan` to update it"
            )
            raise RuntimeError(
                f"capcom index {path} has schema version {version}, this capcom reads "
                f"{SCHEMA_VERSION}: {fix}"
            )
    except BaseException:
        con.close()
        raise
    return SqliteIndex(con, path, readonly)
