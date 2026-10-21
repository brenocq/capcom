from __future__ import annotations

import zlib
from pathlib import Path

import pytest

import capcom.sources.openclaw as oc
from capcom.sources.openclaw import (
    DIRECT_PROJECT,
    OpenClawSource,
    zstd_supported,
)
from capcom.types import Batch, LogFile, LogKind, Request, SessionInfo, Source, Usage
from tests.fixtures.generate import at
from tests.sources.openclaw_db import add, create_db, db_path, reply, user_event


def _requests(batches: list[Batch]) -> list[Request]:
    return [r for b in batches for r in b.records if isinstance(r, Request)]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / ".openclaw"


@pytest.fixture
def db(root: Path) -> Path:
    path = db_path(root)
    create_db(path)
    return path


def _log(source: OpenClawSource, root: Path) -> LogFile:
    (log,) = source.discover(root)
    return log


def test_satisfies_protocol() -> None:
    source: Source = OpenClawSource()
    assert source is not None


def test_discover_finds_each_agent_database(root: Path) -> None:
    create_db(db_path(root, "main"))
    create_db(db_path(root, "helper"))
    (root / "agents" / "empty" / "agent").mkdir(parents=True)
    (root / "agents" / "stray.sqlite").write_text("")
    logs = list(OpenClawSource().discover(root))
    assert [log.session_id for log in logs] == ["helper", "main"]
    assert all(log.kind == LogKind.OPENCLAW_DB for log in logs)
    assert all(log.project_dir == DIRECT_PROJECT for log in logs)
    assert list(OpenClawSource().discover(root / "missing")) == []


def test_direct_replies_become_requests(root: Path, db: Path) -> None:
    add(db, "s1", 1, user_event("u1"), at(0))
    add(db, "s1", 2, reply("e1", input=1000, output=50, cache_read=4000, cache_write=300), at(1))
    add(db, "s1", 3, reply("e2", provider="anthropic", model="claude-opus-4-7"), at(2))
    add(db, "s2", 1, reply("e3", stop="error", total=None), at(3))
    source = OpenClawSource()
    batches = list(source.parse(_log(source, root), 0))
    assert len(batches) == 1 and batches[0].end_offset == 4 and batches[0].lines == 4
    e1, e2, e3 = _requests(batches)
    assert e1.message_id == "oc:e1"
    assert e1.session_id == "openclaw:s1"
    assert e1.agent_id is None
    assert e1.project_dir == DIRECT_PROJECT
    assert e1.timestamp == at(1)  # created_at, not the event's own field
    assert e1.model == "gemini-3.8-flash"
    assert e1.usage == Usage(input=1000, cache_read=4000, cache_write_5m=300, output=50)
    assert e1.reported_cost_usd == pytest.approx(0.001)
    assert e2.model == "claude-opus-4-7"
    assert e3.is_api_error and e3.stop_reason == "error" and e3.reported_cost_usd is None
    sessions = {r.session_id: r for r in batches[0].records if isinstance(r, SessionInfo)}
    assert sessions["openclaw:s1"].first_seen == at(1)
    assert sessions["openclaw:s1"].last_seen == at(2)
    assert sessions["openclaw:s1"].project_dir == DIRECT_PROJECT
    assert batches[0].first_timestamp == at(1)


def test_calls_through_claude_code_and_openclaw_entries_are_skipped(root: Path, db: Path) -> None:
    add(db, "s1", 1, reply("c1", provider="claude-cli", api="cli", model="claude-opus-5-5"), at(0))
    add(db, "s1", 2, reply("m1", provider="openclaw", api="openclaw-transcript"), at(1))
    # another provider's own CLI is not Claude Code: kept
    add(db, "s1", 3, reply("g1", provider="google", api="cli"), at(2))
    source = OpenClawSource()
    (batch,) = source.parse(_log(source, root), 0)
    assert [r.message_id for r in _requests([batch])] == ["oc:g1"]
    assert batch.skipped == 0


def test_bad_rows_are_skipped(root: Path, db: Path) -> None:
    add(db, "s1", 1, '{"usage": not json', at(0))
    add(db, "s1", 2, '["usage"]', at(1))
    no_id = reply("x")
    del no_id["id"]
    add(db, "s1", 3, no_id, at(2))
    add(db, "s1", 4, reply("ok"), at(3))
    source = OpenClawSource()
    (batch,) = source.parse(_log(source, root), 0)
    assert [r.message_id for r in _requests([batch])] == ["oc:ok"]
    assert batch.skipped == 2 and batch.lines == 4


def test_incremental_reads_by_rowid_in_batches(root: Path, db: Path) -> None:
    for i in range(5):
        add(db, "s1", i, reply(f"e{i}"), at(i))
    source = OpenClawSource(batch_rows=2)
    log = _log(source, root)
    batches = list(source.parse(log, 0))
    assert [b.end_offset for b in batches] == [2, 4, 5]
    assert len(_requests(batches)) == 5
    assert source.last_row(log) == 5
    # from the cursor: only the rows above it
    later = list(source.parse(log, 4))
    assert [r.message_id for r in _requests(later)] == ["oc:e4"]
    assert list(source.parse(log, 5)) == []  # nothing new: no batch


def test_a_live_wal_database_that_grows_in_place(root: Path) -> None:
    path = db_path(root)
    create_db(path, wal=True)
    add(path, "s1", 1, reply("e1"), at(0))
    source = OpenClawSource()
    log = _log(source, root)
    (first,) = source.parse(log, 0)
    add(path, "s1", 2, reply("e2"), at(1))
    add(path, "s2", 1, reply("e3"), at(2))
    (second,) = source.parse(log, first.end_offset)
    assert [r.message_id for r in _requests([second])] == ["oc:e2", "oc:e3"]
    assert second.end_offset == 3


def test_parse_never_writes_to_the_database(root: Path, db: Path) -> None:
    add(db, "s1", 1, reply("e1"), at(0))
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    db.chmod(0o444)
    source = OpenClawSource()
    log = _log(source, root)
    assert len(_requests(list(source.parse(log, 0)))) == 1
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_unreadable_database_yields_nothing(root: Path) -> None:
    path = db_path(root)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database at all, just bytes" * 10)
    source = OpenClawSource()
    log = _log(source, root)
    assert list(source.parse(log, 0)) == []
    assert source.last_row(log) is None


def test_compressed_rows_without_zstd_are_counted_unread(
    root: Path, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oc, "_DECOMPRESS", None)
    add(db, "s1", 1, reply("z1"), at(0), compress=zlib.compress)
    add(db, "s1", 2, reply("e1"), at(1))
    source = OpenClawSource()
    (batch,) = source.parse(_log(source, root), 0)
    assert [r.message_id for r in _requests([batch])] == ["oc:e1"]
    assert batch.skipped == 1 and source.compressed_unread == 1
    assert batch.end_offset == 2  # the cursor moves past them


def test_compressed_rows_are_decoded(root: Path, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # zlib stands in for zstd so the decoding path runs on every Python
    monkeypatch.setattr(oc, "_DECOMPRESS", zlib.decompress)
    add(db, "s1", 1, reply("z1", model="gemini-2.5-flash"), at(0), compress=zlib.compress)
    add(db, "s1", 2, user_event("u1"), at(1), compress=zlib.compress)
    add(db, "s1", 3, "x", at(2), compress=lambda b: b"corrupt")
    source = OpenClawSource()
    (batch,) = source.parse(_log(source, root), 0)
    (req,) = _requests([batch])
    assert (req.message_id, req.model) == ("oc:z1", "gemini-2.5-flash")
    assert batch.skipped == 1 and source.compressed_unread == 0


@pytest.mark.skipif(not zstd_supported(), reason="needs Python 3.14's compression.zstd")
def test_real_zstd_rows(root: Path, db: Path) -> None:  # pragma: no cover - Python 3.14
    import importlib

    zstd = importlib.import_module("compression.zstd")
    add(db, "s1", 1, reply("z1"), at(0), compress=zstd.compress)
    source = OpenClawSource()
    (batch,) = source.parse(_log(source, root), 0)
    assert [r.message_id for r in _requests([batch])] == ["oc:z1"]


def test_direct_project_is_its_own_group(tmp_path: Path) -> None:
    from capcom.cost.prices import load_prices
    from capcom.timeline.groups import Grouper, ProjectNames

    root = tmp_path / ".openclaw"
    names = ProjectNames.build(
        [(DIRECT_PROJECT, None), ("-x-.openclaw", None)], openclaw_root=root, home=tmp_path
    )
    assert names.label(DIRECT_PROJECT) == DIRECT_PROJECT
    assert names.key(DIRECT_PROJECT) == DIRECT_PROJECT
    grouper = Grouper(load_prices(), {}, {}, oc_folder(root))
    assert grouper.project(DIRECT_PROJECT) == (DIRECT_PROJECT, DIRECT_PROJECT)
    assert grouper.project(oc_folder(root))[0] == "openclaw"


def oc_folder(root: Path) -> str:
    from capcom.timeline.groups import folder_name

    return folder_name(root)
