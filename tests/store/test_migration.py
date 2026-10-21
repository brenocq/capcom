"""Schema 2 -> 3: cost_states gains its time, in place, and the files are read again."""

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


def _v2_schema() -> str:
    schema = index_module._SCHEMA
    start = schema.index("CREATE TABLE IF NOT EXISTS cost_states (")
    end = schema.index(");", start) + 2
    return schema[:start] + _V2_COST_STATES + schema[end:]


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
        assert ix.file_state(sub).offset == sub.stat().st_size  # type: ignore[union-attr]
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
    assert source.calls == [(main, 0)]  # only the queued file, from 0, without a reset
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
