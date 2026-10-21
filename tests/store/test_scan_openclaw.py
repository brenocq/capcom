"""Scanning OpenClaw's database: a rowid cursor instead of byte offsets."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

import capcom.sources.openclaw as oc
from capcom.config import Config
from capcom.sources.openclaw import DIRECT_PROJECT
from capcom.store import index as index_module
from capcom.store.index import open_index
from capcom.store.scan import run_scan
from tests.fixtures.generate import at
from tests.sources.openclaw_db import add, create_db, db_path, reply
from tests.store.fake_source import write_lines


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / ".claude",
        tmp_path / ".openclaw",
        tmp_path / "cache" / "capcom",
        tmp_path / "state",
    )


def _ids(config: Config) -> list[str]:
    with open_index(config.index_path, readonly=True) as ix:
        return sorted(r.message_id for r in ix.requests())


def test_both_sources_are_scanned_and_the_cursor_is_a_rowid(config: Config) -> None:
    config.projects_dir.mkdir(parents=True)
    db = db_path(config.openclaw_root)
    create_db(db, wal=True)
    add(db, "s1", 1, reply("e1", total=0.25), at(0))
    add(db, "s1", 2, reply("c1", provider="claude-cli", api="cli"), at(1))

    stats = run_scan(config, quiet=True)
    assert stats.files_seen == 1 and stats.files_read == 1 and stats.lines == 2
    assert _ids(config) == ["oc:e1"]
    with open_index(config.index_path, readonly=True) as ix:
        assert ix.file_state(db).offset == 2  # type: ignore[union-attr]
        (req,) = ix.requests()
        assert req.project_dir == DIRECT_PROJECT and req.reported_cost_usd == 0.25
        (sess,) = ix.sessions()
        assert sess.session_id == "openclaw:s1" and sess.project_dir == DIRECT_PROJECT

    # nothing new: the database is opened, nothing is read
    again = run_scan(config, quiet=True)
    assert again.files_read == 0 and again.lines == 0

    # it grows in place (in WAL mode its size need not change): only new rows are read
    add(db, "s1", 3, reply("e2"), at(2))
    grown = run_scan(config, quiet=True)
    assert grown.lines == 1 and grown.files_reset == 0
    assert _ids(config) == ["oc:e1", "oc:e2"]


def test_a_rebuilt_table_is_read_again_and_keeps_history(config: Config) -> None:
    db = db_path(config.openclaw_root)
    create_db(db)
    for i in range(4):
        add(db, "s1", i, reply(f"e{i}"), at(i))
    run_scan(config, quiet=True)
    assert len(_ids(config)) == 4

    # OpenClaw pruned and rebuilt the table: its highest rowid is now below the cursor
    con = sqlite3.connect(db)
    con.execute("DELETE FROM transcript_events")
    con.commit()
    con.close()
    add(db, "s1", 9, reply("e1"), at(1), rowid=1)  # kept, renumbered
    add(db, "s2", 1, reply("n1"), at(5), rowid=2)  # new
    stats = run_scan(config, quiet=True)
    assert stats.files_reset == 1 and stats.lines == 2
    assert _ids(config) == ["oc:e0", "oc:e1", "oc:e2", "oc:e3", "oc:n1"]
    with open_index(config.index_path, readonly=True) as ix:
        assert ix.file_state(db).offset == 2  # type: ignore[union-attr]


def test_reused_rowids_at_the_cursor_mean_a_reread(config: Config) -> None:
    db = db_path(config.openclaw_root)
    create_db(db)
    for i in range(4):
        add(db, "s1", i, reply(f"e{i}"), at(i))
    run_scan(config, quiet=True)

    # the top rows go and new ones take their rowids: the highest rowid is unchanged
    con = sqlite3.connect(db)
    con.execute("DELETE FROM transcript_events WHERE rowid > 2")
    con.commit()
    con.close()
    add(db, "s2", 1, reply("n1"), at(5), rowid=3)
    add(db, "s2", 2, reply("n2"), at(6), rowid=4)
    stats = run_scan(config, quiet=True)
    assert stats.files_reset == 1 and stats.lines == 4
    assert _ids(config) == ["oc:e0", "oc:e1", "oc:e2", "oc:e3", "oc:n1", "oc:n2"]
    again = run_scan(config, quiet=True)
    assert again.files_reset == 0 and again.lines == 0


def test_compressed_rows_are_read_once_zstd_is_there(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = db_path(config.openclaw_root)
    create_db(db)
    monkeypatch.setattr(oc, "_DECOMPRESS", None)
    add(db, "s1", 1, reply("e0"), at(0))
    add(db, "s1", 2, reply("z1"), at(1), compress=zlib.compress)
    add(db, "s1", 3, reply("e1"), at(2))
    run_scan(config, quiet=True)
    assert _ids(config) == ["oc:e0", "oc:e1"]
    add(db, "s1", 4, reply("z2"), at(3), compress=zlib.compress)
    run_scan(config, quiet=True)
    assert _ids(config) == ["oc:e0", "oc:e1"]

    # a Python that can decompress them (zlib stands in for zstd): read from the first
    monkeypatch.setattr(oc, "_DECOMPRESS", zlib.decompress)
    stats = run_scan(config, quiet=True)
    assert stats.lines == 3 and stats.files_reset == 0
    assert _ids(config) == ["oc:e0", "oc:e1", "oc:z1", "oc:z2"]
    assert run_scan(config, quiet=True).lines == 0


def test_claude_code_logs_still_scan_alongside(config: Config) -> None:
    project = config.projects_dir / "-home-u-p"
    project.mkdir(parents=True)
    write_lines(project / "s1.jsonl", {"t": "junk"})
    db = db_path(config.openclaw_root)
    create_db(db)
    add(db, "s1", 1, reply("e1"), at(0))
    stats = run_scan(config, quiet=True)
    assert stats.files_seen == 2 and stats.files_failed == 0


def test_index_v5_gains_the_reported_cost_column(config: Config) -> None:
    config.index_path.parent.mkdir(parents=True)
    con = sqlite3.connect(config.index_path)
    v5 = index_module._SCHEMA.replace(",\n    reported_cost REAL", "")
    assert v5 != index_module._SCHEMA
    con.executescript(v5)
    con.execute("INSERT INTO schema_version (version) VALUES (5)")
    con.commit()
    con.close()
    with open_index(config.index_path) as ix:
        assert list(ix.requests()) == []
    con = sqlite3.connect(config.index_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
    version = con.execute("SELECT version FROM schema_version").fetchall()
    con.close()
    assert "reported_cost" in cols and version == [(index_module.SCHEMA_VERSION,)]
