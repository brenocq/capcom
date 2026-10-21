"""SqliteIndex: upserts, merges, queries, forget_file and readonly mode."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from capcom.store.index import SCHEMA_VERSION, SqliteIndex, open_index
from capcom.types import (
    AgentInfo,
    Batch,
    CostState,
    Event,
    EventKind,
    FileState,
    Index,
    LogFile,
    LogKind,
    ModelUsage,
    Record,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)
from tests.fixtures.generate import at

A = LogFile(Path("/logs/p/s1.jsonl"), LogKind.SESSION, "p", "s1")
B = LogFile(Path("/logs/p/s1/subagents/agent-a1.jsonl"), LogKind.SUBAGENT, "p", "s1", "a1")
C = LogFile(Path("/logs/p/s2.jsonl"), LogKind.SESSION, "p", "s2")


def req(mid: str, t: float, session: str = "s1", out: int = 10, **kw: object) -> Request:
    return Request(
        message_id=mid,
        session_id=session,
        agent_id=None,
        project_dir="p",
        timestamp=at(t),
        model="claude-opus-5-5",
        usage=Usage(output=out),
        **kw,  # type: ignore[arg-type]
    )


def ev(
    key: str, t: float, kind: EventKind = EventKind.TURN, session: str = "s1", **attrs: object
) -> Event:
    return Event(kind, session, None, at(t), key, attrs)  # type: ignore[arg-type]


def put(ix: SqliteIndex, log: LogFile, *records: Record, offset: int = 0) -> None:
    ix.apply(log, Batch(list(records), offset), FileState(log.path, 1, offset, 1, offset))


@pytest.fixture
def ix(tmp_path: Path) -> SqliteIndex:
    return open_index(tmp_path / "cache" / "index.sqlite")


def test_satisfies_protocol(ix: SqliteIndex) -> None:
    index: Index = ix
    assert index.file_state(A.path) is None


def test_schema_version_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "dir" / "index.sqlite"
    open_index(path).close()
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    con.execute("UPDATE schema_version SET version = 999")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="schema"):
        open_index(path)


def test_file_state_roundtrip(ix: SqliteIndex) -> None:
    state = FileState(A.path, inode=42, size=1000, mtime_ns=123, offset=900)
    ix.apply(A, Batch([], 900), state)
    assert ix.file_state(A.path) == state
    ix.apply(A, Batch([], 1000), FileState(A.path, 42, 1100, 124, 1000))
    assert ix.file_state(A.path) == FileState(A.path, 42, 1100, 124, 1000)


def test_request_roundtrip(ix: SqliteIndex) -> None:
    r = Request(
        message_id="m1",
        session_id="s1",
        agent_id="a1",
        project_dir="p",
        timestamp=at(1.5),
        model="claude-opus-5-5",
        usage=Usage(1, 2, 3, 4, 5, 6, 7, 8),
        request_id="r1",
        cwd="/home/u/p",
        version="2.1.306",
        effort="high",
        speed="fast",
        service_tier="standard",
        inference_geo="us",
        stop_reason="tool_use",
        is_api_error=True,
        output_estimated=True,
        tool_calls=(ToolCall("tu1", "Read", "/a.py"), ToolCall("tu2", "WebSearch")),
    )
    put(ix, B, r)
    assert list(ix.requests()) == [r]
    plain = req("m2", 2)
    put(ix, A, plain)
    assert list(ix.requests())[1] == plain


def test_request_upsert_by_message_id(ix: SqliteIndex) -> None:
    # Streaming records of one reply, split across two batches and two files.
    put(ix, A, req("m1", 1, out=4), req("m1", 1, out=50))
    put(ix, A, req("m1", 1, out=300), req("m2", 2))
    assert [(r.message_id, r.usage.output) for r in ix.requests()] == [("m1", 300), ("m2", 10)]
    put(ix, B, req("m1", 1, out=301))
    assert [r.usage.output for r in ix.requests()] == [301, 10]


def test_time_range_and_session_filter(ix: SqliteIndex) -> None:
    put(ix, A, req("m3", 30), req("m1", 10), req("m2", 20))
    put(ix, C, req("n1", 15, session="s2"))
    ids = [r.message_id for r in ix.requests()]
    assert ids == ["m1", "n1", "m2", "m3"]
    assert [r.message_id for r in ix.requests(since=at(15), until=at(30))] == ["n1", "m2"]
    assert [r.message_id for r in ix.requests(until=at(10))] == []
    assert [r.message_id for r in ix.requests(session_id="s2")] == ["n1"]
    assert [r.message_id for r in ix.requests(since=at(20), session_id="s1")] == ["m2", "m3"]


def test_events_replace_and_query(ix: SqliteIndex) -> None:
    put(ix, A, ev("k1", 1, EventKind.COMPACTION, pre_tokens=100, trigger="auto"))
    put(ix, A, ev("k1", 1, EventKind.COMPACTION, pre_tokens=200, trigger="auto"))
    put(ix, B, ev("k1", 2, EventKind.SPAWN, agent_type="scout"))  # same key, other file
    put(ix, A, ev("k2", 3, EventKind.TOOL_SET, names=("Read", "Bash"), size_tokens=5, f=1.5))
    put(ix, C, ev("k1", 4, session="s2", none_attr=None, flag=True))
    got = list(ix.events())
    assert [(e.key, e.kind) for e in got] == [
        ("k1", EventKind.COMPACTION),
        ("k1", EventKind.SPAWN),
        ("k2", EventKind.TOOL_SET),
        ("k1", EventKind.TURN),
    ]
    assert got[0].attrs == {"pre_tokens": 200, "trigger": "auto"}
    assert got[2].attrs == {"names": ("Read", "Bash"), "size_tokens": 5, "f": 1.5}
    assert got[3].attrs == {"none_attr": None, "flag": True}
    assert got[0].timestamp == at(1)
    kinds = frozenset({EventKind.SPAWN, EventKind.TOOL_SET})
    assert [e.key for e in ix.events(kinds=kinds)] == ["k1", "k2"]
    assert [e.timestamp for e in ix.events(since=at(2), until=at(4))] == [at(2), at(3)]
    assert [e.session_id for e in ix.events(session_id="s2")] == ["s2"]


def test_session_merge(ix: SqliteIndex) -> None:
    put(ix, A, SessionInfo("s1", "p", cwd="/p", first_seen=at(10), last_seen=at(20)))
    put(ix, A, SessionInfo("s1", "p", title="first title", first_seen=at(30), last_seen=at(40)))
    put(ix, B, SessionInfo("s1", "p", version="2.1.300", first_seen=at(5), last_seen=at(35)))
    put(ix, A, SessionInfo("s1", "p", title="second title", version=None))
    put(ix, C, SessionInfo("s2", "p", entrypoint="cli", git_branch="main"))
    by_id = {s.session_id: s for s in ix.sessions()}
    assert by_id["s1"] == SessionInfo(
        "s1",
        "p",
        cwd="/p",
        title="second title",
        version="2.1.300",
        first_seen=at(5),
        last_seen=at(40),
    )
    assert by_id["s2"] == SessionInfo("s2", "p", entrypoint="cli", git_branch="main")
    ix.forget_file(B.path)
    s1 = next(s for s in ix.sessions() if s.session_id == "s1")
    assert (s1.first_seen, s1.version) == (at(10), None)


def test_agent_merge(ix: SqliteIndex) -> None:
    put(ix, B, AgentInfo("a1", "s1", agent_type="scout", description="look", finished=False))
    put(ix, B, AgentInfo("a1", "s1", spawn_depth=1, finished=True))
    put(ix, C, AgentInfo("a2", "s2", agent_type="coder", is_built_in=False, tool_use_id="tu9"))
    assert list(ix.agents(session_id="s1")) == [
        AgentInfo("a1", "s1", "scout", "look", None, 1, None, True)
    ]
    assert [a.agent_id for a in ix.agents()] == ["a1", "a2"]


def test_last_cost_states(ix: SqliteIndex) -> None:
    models = {"claude-opus-5-5": ModelUsage(input=3, output=4, cache_read=5, cost_usd=0.5)}
    put(
        ix,
        A,
        CostState("s1", 500, 1.0),
        CostState("s1", 100, 0.2),
        CostState("s1", 9000, 2.5, models, 77, False),
    )
    put(ix, A, CostState("s1", 700, 1.2))
    put(ix, C, CostState("s2", 50, 0.1))
    got = {c.session_id: c for c in ix.last_cost_states()}
    assert got["s1"] == CostState("s1", 9000, 2.5, models, 77, False)
    assert got["s2"].total_cost_usd == 0.1
    put(ix, A, CostState("s1", 9000, 3.0))  # same line read again replaces it
    assert {c.session_id: c.total_cost_usd for c in ix.last_cost_states()}["s1"] == 3.0


def test_forget_file(ix: SqliteIndex) -> None:
    put(ix, A, req("m1", 1), ev("k1", 1), SessionInfo("s1", "p"), CostState("s1", 5, 1.0), offset=9)
    put(ix, B, req("m2", 2), ev("k1", 2), AgentInfo("a1", "s1"), offset=9)
    ix.forget_file(A.path)
    assert ix.file_state(A.path) is None
    assert ix.file_state(B.path) is not None
    assert [r.message_id for r in ix.requests()] == ["m2"]
    assert [e.timestamp for e in ix.events()] == [at(2)]
    assert list(ix.sessions()) == []
    assert list(ix.last_cost_states()) == []
    assert [a.agent_id for a in ix.agents()] == ["a1"]


def test_apply_is_one_transaction(ix: SqliteIndex) -> None:
    bad = Batch([req("m1", 1), "not a record"], 10)  # type: ignore[list-item]
    with pytest.raises(TypeError):
        ix.apply(A, bad, FileState(A.path, 1, 10, 1, 10))
    assert list(ix.requests()) == []
    assert ix.file_state(A.path) is None


def test_readonly(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    with pytest.raises(FileNotFoundError):
        open_index(path, readonly=True)
    assert not path.exists()
    with open_index(path) as w:
        put(w, A, req("m1", 1), offset=5)
    size, mtime = path.stat().st_size, path.stat().st_mtime_ns
    with open_index(path, readonly=True) as ro:
        assert [r.message_id for r in ro.requests()] == ["m1"]
        assert ro.file_state(A.path) is not None
        with pytest.raises(sqlite3.OperationalError):
            put(ro, A, req("m2", 2))
        with pytest.raises(sqlite3.OperationalError):
            ro.forget_file(A.path)
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (size, mtime)


def test_readonly_sees_live_writer(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    w = open_index(path)
    put(w, A, req("m1", 1))
    ro = open_index(path, readonly=True)
    put(w, A, req("m2", 2))
    assert [r.message_id for r in ro.requests()] == ["m1", "m2"]
    ro.close()
    w.close()
