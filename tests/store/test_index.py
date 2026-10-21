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


def test_cost_state_timestamp_roundtrip(ix: SqliteIndex) -> None:
    put(ix, A, CostState("s1", 10, 1.0, timestamp=at(7.25)))
    put(ix, C, CostState("s2", 10, 1.0))
    got = {c.session_id: c.timestamp for c in ix.last_cost_states()}
    assert got == {"s1": at(7.25), "s2": None}
    assert got["s1"] is not None and got["s1"].utcoffset() is not None
    with pytest.raises(ValueError, match="timezone-aware"):
        put(ix, A, CostState("s1", 20, 1.0, timestamp=at(1).replace(tzinfo=None)))


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
    put(ix, A, req("m0", 0), offset=5)
    # The Request is written, then the Event breaks a NOT NULL constraint: roll back.
    broken = Event(EventKind.TURN, None, None, at(1), "k1")  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError):
        ix.apply(A, Batch([req("m1", 1), broken], 10), FileState(A.path, 1, 10, 1, 10))
    assert [r.message_id for r in ix.requests()] == ["m0"]
    assert list(ix.events()) == []
    state = ix.file_state(A.path)
    assert state is not None and state.offset == 5
    put(ix, A, req("m1", 1), offset=10)  # the connection is usable afterwards
    assert len(list(ix.requests())) == 2


def test_session_fields_merge_by_their_own_sequence(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    with open_index(path) as ix:
        put(ix, A, SessionInfo("s1", "p", title="from A", cwd="/a"))
        put(ix, B, SessionInfo("s1", "p", title="from B"))
        # A later write from A without a title must not bring A's old title back.
        put(ix, A, SessionInfo("s1", "p", version="2.1.306"))
        (s1,) = ix.sessions()
        assert (s1.title, s1.cwd, s1.version) == ("from B", "/a", "2.1.306")
    with open_index(path) as ix:  # the sequence is stored, not taken from the clock
        put(ix, A, SessionInfo("s1", "p", title="from A again"))
        (s1,) = ix.sessions()
        assert s1.title == "from A again"


def test_agent_fields_merge_by_their_own_sequence(ix: SqliteIndex) -> None:
    put(ix, A, AgentInfo("a1", "s1", agent_type="scout", finished=False))
    put(ix, B, AgentInfo("a1", "s1", finished=True))
    put(ix, A, AgentInfo("a1", "s1", description="look"))
    assert list(ix.agents()) == [AgentInfo("a1", "s1", "scout", "look", finished=True)]


def test_last_cost_state_prefers_the_main_log(ix: SqliteIndex) -> None:
    put(ix, A, CostState("s1", 100, 1.0), CostState("s1", 200, 2.0))
    put(ix, B, CostState("s1", 99_999, 0.5))  # a subagent file: larger offsets, not later
    put(ix, B, CostState("s9", 10, 0.1), CostState("s9", 20, 0.2))  # no main log for s9
    got = {c.session_id: c for c in ix.last_cost_states()}
    assert (got["s1"].offset, got["s1"].total_cost_usd) == (200, 2.0)
    assert got["s9"].total_cost_usd == 0.2


def test_tool_calls_union_and_estimated_output(ix: SqliteIndex) -> None:
    t1, t2, t3 = (
        ToolCall("tu1", "Read", "/a"),
        ToolCall("tu2", "Bash", "ls"),
        ToolCall("tu3", "Grep"),
    )
    put(ix, A, req("m1", 1, out=900, output_estimated=True, tool_calls=(t1, t2)))
    # A later scan sees the rest of the reply: fewer tool calls, a smaller estimate.
    put(ix, A, req("m1", 1, out=300, output_estimated=True, tool_calls=(t2, t3)))
    (r,) = ix.requests()
    assert r.tool_calls == (t1, t2, t3)
    assert (r.usage.output, r.output_estimated) == (900, True)
    put(ix, A, req("m1", 1, out=120, tool_calls=()))  # a logged, final count wins as is
    (r,) = ix.requests()
    assert (r.usage.output, r.output_estimated, r.tool_calls) == (120, False, (t1, t2, t3))
    # The same within one batch.
    put(
        ix,
        C,
        req("n1", 2, out=50, output_estimated=True, tool_calls=(t1,)),
        req("n1", 2, out=10, output_estimated=True, tool_calls=(t2,)),
    )
    n1 = next(r for r in ix.requests() if r.message_id == "n1")
    assert (n1.usage.output, n1.tool_calls) == (50, (t1, t2))


def test_naive_datetimes_are_refused(ix: SqliteIndex) -> None:
    naive = at(1).replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        ix.requests(since=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        ix.events(until=naive)


def test_index_inside_log_folders_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "openclaw"))
    for root in ("claude", "openclaw"):
        path = tmp_path / root / "cache" / "index.sqlite"
        with pytest.raises(ValueError, match="log folder"):
            open_index(path)
        with pytest.raises(ValueError, match="log folder"):
            open_index(path, readonly=True)
        assert not (tmp_path / root).exists()


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
