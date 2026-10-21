"""Schemas 2 -> 3 -> 4 -> 5: cost_states gains its times (ts, then next_ts) and the
index its shared_replies, in place, and the files with cost-states and every file of
their sessions are read again."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from capcom.config import Config
from capcom.store import index as index_module
from capcom.store.index import SCHEMA_VERSION, open_index
from capcom.store.scan import run_scan
from tests.fixtures.generate import at
from tests.store.fake_source import FakeSource, write_lines

# The cost_states table as schema version 2 created it (no ts column).
_V2_COST_STATES = """CREATE TABLE IF NOT EXISTS cost_states (
    file TEXT NOT NULL,
    pos INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    main INTEGER NOT NULL,
    total_cost_usd REAL NOT NULL,
    models TEXT,
    start_time_ms INTEGER,
    has_unknown_model_cost INTEGER,
    PRIMARY KEY (file, pos)
);"""


def _old_schema(version: int) -> str:
    """The schema as version 2 or 3 created it: version 3 added cost_states.ts,
    version 4 cost_states.next_ts and the shared_replies table."""
    schema = index_module._SCHEMA.replace(index_module._SHARED, "")
    start = schema.index("CREATE TABLE IF NOT EXISTS cost_states (")
    end = schema.index(");", start) + 2
    table = _V2_COST_STATES
    if version == 3:
        table = table.replace("    PRIMARY KEY", "    ts INTEGER,\n    PRIMARY KEY")
    return schema[:start] + table + schema[end:]


def _v2_schema() -> str:
    return _old_schema(2)


def _file_row(con: sqlite3.Connection, path: Path, pos: int) -> None:
    st = path.stat()
    con.execute(
        "INSERT INTO files (path, inode, size, mtime_ns, pos, head_len) VALUES (?, ?, ?, ?, ?, 0)",
        (str(path), st.st_ino, st.st_size, st.st_mtime_ns, pos),
    )


def _request_row(con: sqlite3.Connection, mid: str, file: Path, out: int) -> None:
    con.execute(
        "INSERT INTO requests (message_id, file, session_id, project_dir, ts, model, input, "
        "cache_read, cache_write_5m, cache_write_1h, output, thinking, web_searches, "
        "web_fetches, is_api_error, output_estimated) "
        "VALUES (?, ?, 's1', '-home-u-p', 0, 'claude-opus-5-5', 3, 0, 0, 0, ?, 0, 0, 0, 0, 0)",
        (mid, str(file), out),
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache" / "capcom")


def test_v2_index_is_migrated_in_place_and_reread(config: Config) -> None:
    project = config.projects_dir / "-home-u-p"
    main = project / "s1.jsonl"
    sub = project / "s1" / "subagents" / "agent-a1.jsonl"
    write_lines(main, {"t": "req", "id": "m1", "ts": 1, "out": 5})
    cost_pos = main.stat().st_size
    write_lines(main, {"t": "cost", "usd": 0.5, "ts": 2})
    write_lines(sub, {"t": "req", "id": "m2", "ts": 3, "out": 7})
    gone = project / "s0.jsonl"  # deleted by Claude Code since: its rows are history

    # The index as schema version 2 left it after a full scan.
    config.index_path.parent.mkdir(parents=True)
    con = sqlite3.connect(config.index_path)
    con.executescript(_v2_schema())
    con.execute("INSERT INTO schema_version (version) VALUES (2)")
    _file_row(con, main, main.stat().st_size)
    _file_row(con, sub, sub.stat().st_size)
    con.execute(
        "INSERT INTO files (path, inode, size, mtime_ns, pos) VALUES (?, 1, 90, 1, 90)",
        (str(gone),),
    )
    _request_row(con, "m1", main, 5)
    _request_row(con, "m2", sub, 7)
    con.executemany(
        "INSERT INTO cost_states (file, pos, session_id, main, total_cost_usd) "
        "VALUES (?, ?, ?, 1, ?)",
        [(str(main), cost_pos, "s1", 0.5), (str(gone), 40, "s0", 0.25)],
    )
    con.commit()
    con.close()

    with pytest.raises(RuntimeError, match="capcom scan"):
        open_index(config.index_path, readonly=True)  # readonly never migrates

    with open_index(config.index_path) as ix:
        assert ix.file_state(main).offset == 0  # type: ignore[union-attr]  # queued
        assert ix.file_state(gone).offset == 0  # type: ignore[union-attr]
        # A subagent log of a session with a cost-state: queued too (its replies).
        assert ix.file_state(sub).offset == 0  # type: ignore[union-attr]
        got = {c.session_id: c for c in ix.last_cost_states()}
        assert {s: (c.total_cost_usd, c.timestamp) for s, c in got.items()} == {
            "s0": (0.25, None),
            "s1": (0.5, None),
        }
        assert [r.message_id for r in ix.requests()] == ["m1", "m2"]
    con = sqlite3.connect(config.index_path)
    assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    con.close()

    source = FakeSource()
    stats = run_scan(config, quiet=True, source=source)
    # Only the queued files, from 0, without a reset.
    assert sorted(source.calls) == sorted([(main, 0), (sub, 0)])
    assert stats.files_reset == 0
    with open_index(config.index_path, readonly=True) as ix:
        got = {c.session_id: c for c in ix.last_cost_states()}
        assert got["s1"].offset == cost_pos
        assert got["s1"].timestamp == at(2)
        assert got["s0"].timestamp is None  # its log is gone: the row is kept as it was
        assert sorted((r.message_id, r.usage.output) for r in ix.requests()) == [
            ("m1", 5),
            ("m2", 7),
        ]
        assert ix.file_state(main).offset == main.stat().st_size  # type: ignore[union-attr]
    con = sqlite3.connect(config.index_path)
    assert con.execute("SELECT COUNT(*) FROM cost_states").fetchone() == (2,)
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone() == (2,)
    con.close()

    source = FakeSource()
    run_scan(config, quiet=True, source=source)
    assert source.calls == []  # read once: unchanged files are skipped again


def test_v3_index_is_migrated_in_place_and_reread(config: Config) -> None:
    main = config.projects_dir / "-home-u-p" / "s1.jsonl"
    fork = config.projects_dir / "-tmp-x" / "s3.jsonl"  # a copy holding reply m1
    write_lines(main, {"t": "req", "id": "m1", "ts": 1, "out": 5})
    cost_pos = main.stat().st_size
    write_lines(main, {"t": "cost", "usd": 0.5, "ts": 1}, {"t": "req", "id": "m2", "ts": 3})
    write_lines(fork, {"t": "req", "id": "m1", "ts": 1, "out": 5})

    # The index as schema version 3 left it: m1's row is the fork's (written last).
    config.index_path.parent.mkdir(parents=True)
    con = sqlite3.connect(config.index_path)
    con.executescript(_old_schema(3))
    assert "next_ts" not in _old_schema(3) and "shared_replies" not in _old_schema(3)
    con.execute("INSERT INTO schema_version (version) VALUES (3)")
    _file_row(con, main, main.stat().st_size)
    _file_row(con, fork, fork.stat().st_size)
    _request_row(con, "m2", main, 0)
    _request_row(con, "m1", fork, 5)
    con.execute("UPDATE requests SET session_id = 's3' WHERE message_id = 'm1'")
    con.execute(
        "INSERT INTO cost_states (file, pos, session_id, main, total_cost_usd, ts) "
        "VALUES (?, ?, 's1', 1, 0.5, 0)",
        (str(main), cost_pos),
    )
    con.commit()
    con.close()

    with pytest.raises(RuntimeError, match="capcom scan"):
        open_index(config.index_path, readonly=True)
    with open_index(config.index_path) as ix:
        assert ix.file_state(main).offset == 0  # type: ignore[union-attr]  # queued
        assert ix.file_state(fork).offset == fork.stat().st_size  # type: ignore[union-attr]
        assert next(ix.last_cost_states()).next_timestamp is None
    con = sqlite3.connect(config.index_path)
    assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    assert SCHEMA_VERSION == 5
    con.close()

    source = FakeSource(batch_lines=2)  # the cost-state ends a batch
    run_scan(config, quiet=True, source=source)
    assert source.calls == [(main, 0)]
    with open_index(config.index_path, readonly=True) as ix:
        (cs,) = ix.last_cost_states()
        assert (cs.offset, cs.timestamp, cs.next_timestamp) == (cost_pos, at(1), at(3))
        # The reread replaced the fork's row of m1: both logs hold it, and the fork,
        # which lost the row, is the one missing it.
        assert (ix.shared_replies("s1"), ix.shared_replies("s3")) == (0, 1)
        assert sorted(r.message_id for r in ix.requests()) == ["m1", "m2"]


def test_v2_index_migrates_through_v3(config: Config) -> None:
    config.index_path.parent.mkdir(parents=True)
    con = sqlite3.connect(config.index_path)
    con.executescript(_old_schema(2))
    con.execute("INSERT INTO schema_version (version) VALUES (2)")
    con.commit()
    con.close()
    with open_index(config.index_path) as ix:
        assert list(ix.last_cost_states()) == []
        assert ix.shared_replies("s1") == 0
    con = sqlite3.connect(config.index_path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(cost_states)")]
    assert cols[-2:] == ["ts", "next_ts"]
    assert con.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    con.close()


def test_v4_index_rereads_every_file_of_sessions_with_cost_states(config: Config) -> None:
    project = config.projects_dir / "-home-u-p"
    main = project / "s1.jsonl"
    sub = project / "s1" / "subagents" / "agent-a1.jsonl"
    fork = config.projects_dir / "-tmp-x" / "s3.jsonl"  # a copy holding reply m5
    write_lines(main, {"t": "req", "id": "m1", "ts": 1, "out": 5})
    cost_pos = main.stat().st_size
    write_lines(main, {"t": "cost", "usd": 0.5, "ts": 1}, {"t": "req", "id": "m2", "ts": 3})
    write_lines(sub, {"t": "req", "id": "m5", "ts": 2, "out": 5}, {"t": "req", "id": "m6", "ts": 2})
    write_lines(fork, {"t": "req", "id": "m5", "ts": 2, "out": 5})

    # The index as version 4 left it: m5's row is the fork's, and the share through
    # the subagent log was never seen (3 -> 4 reread only files with cost-states).
    config.index_path.parent.mkdir(parents=True)
    con = sqlite3.connect(config.index_path)
    con.executescript(index_module._SCHEMA)
    con.execute("INSERT INTO schema_version (version) VALUES (4)")
    for f in (main, sub, fork):
        _file_row(con, f, f.stat().st_size)
    _request_row(con, "m1", main, 5)
    _request_row(con, "m2", main, 0)
    _request_row(con, "m5", fork, 5)
    _request_row(con, "m6", sub, 0)
    con.execute("UPDATE requests SET session_id = 's3' WHERE message_id = 'm5'")
    con.execute(
        "INSERT INTO cost_states (file, pos, session_id, main, total_cost_usd, ts, next_ts) "
        "VALUES (?, ?, 's1', 1, 0.5, 0, 0)",
        (str(main), cost_pos),
    )
    con.commit()
    con.close()

    with open_index(config.index_path) as ix:
        assert ix.file_state(main).offset == 0  # type: ignore[union-attr]
        assert ix.file_state(sub).offset == 0  # type: ignore[union-attr]
        assert ix.file_state(fork).offset == fork.stat().st_size  # type: ignore[union-attr]
        assert ix.shared_replies("s1") == ix.shared_replies("s3") == 0

    source = FakeSource(batch_lines=1)
    run_scan(config, quiet=True, source=source)
    assert sorted(source.calls) == sorted([(main, 0), (sub, 0)])
    with open_index(config.index_path, readonly=True) as ix:
        (cs,) = ix.last_cost_states()
        assert (cs.timestamp, cs.next_timestamp) == (at(1), at(3))  # filled again
        # The subagent log took m5's row back from the fork: s3 now lacks it.
        assert (ix.shared_replies("s1"), ix.shared_replies("s3")) == (0, 1)
