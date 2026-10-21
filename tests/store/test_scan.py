"""The scan loop: incremental reads, resets, idempotence, progress."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from capcom.config import Config
from capcom.store.index import SqliteIndex, open_index
from capcom.store.scan import run_scan
from capcom.types import Batch, LogFile, ScanStats
from tests.store.fake_source import FakeSource, write_lines


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache" / "capcom")


def main_log(config: Config, session: str = "s1", project: str = "-home-u-p") -> Path:
    return config.projects_dir / project / f"{session}.jsonl"


def sub_log(config: Config, agent: str, session: str = "s1", project: str = "-home-u-p") -> Path:
    return config.projects_dir / project / session / "subagents" / f"agent-{agent}.jsonl"


def scan(config: Config, source: FakeSource | None = None) -> ScanStats:
    return run_scan(config, quiet=True, source=source or FakeSource(batch_lines=2))


def index(config: Config) -> SqliteIndex:
    return open_index(config.index_path, readonly=True)


def outputs(config: Config) -> list[tuple[str, int]]:
    with index(config) as ix:
        return [(r.message_id, r.usage.output) for r in ix.requests()]


def test_first_scan(config: Config) -> None:
    write_lines(
        main_log(config),
        {"t": "ses", "title": "t", "first": 0, "last": 5},
        {"t": "req", "id": "m1", "ts": 1, "out": 4},
        {"t": "req", "id": "m1", "ts": 1, "out": 90},  # streaming: the last record wins
        {"t": "ev", "kind": "spawn", "key": "u1", "ts": 2, "attrs": {"agent_type": "scout"}},
        {"t": "mystery"},
        {"t": "cost", "usd": 0.5},
    )
    write_lines(
        sub_log(config, "a1"),
        {"t": "agent", "type": "scout"},
        {"t": "req", "id": "m2", "ts": 3, "out": 7},
    )
    stats = scan(config)
    assert config.index_path.is_file()
    assert (stats.files_seen, stats.files_read, stats.files_reset) == (2, 2, 0)
    assert (stats.lines, stats.records, stats.skipped) == (8, 7, 1)
    assert (
        stats.bytes_read == main_log(config).stat().st_size + sub_log(config, "a1").stat().st_size
    )
    assert outputs(config) == [("m1", 90), ("m2", 7)]
    with index(config) as ix:
        assert [a.agent_type for a in ix.agents()] == ["scout"]
        assert [s.title for s in ix.sessions()] == ["t"]
        assert [c.total_cost_usd for c in ix.last_cost_states()] == [0.5]
        assert len(list(ix.events())) == 1


def test_rescan_is_idempotent_and_skips_unchanged(config: Config) -> None:
    write_lines(main_log(config), *({"t": "req", "id": f"m{i}", "ts": i} for i in range(5)))
    write_lines(sub_log(config, "a1"), {"t": "ev", "ts": 1})
    scan(config)
    before = outputs(config)
    source = FakeSource()
    stats = scan(config, source)
    assert source.calls == []  # unchanged files are not opened
    assert (stats.files_seen, stats.files_read, stats.bytes_read, stats.records) == (2, 0, 0, 0)
    assert outputs(config) == before
    with index(config) as ix:
        assert len(list(ix.events())) == 1


def test_append_reads_only_the_new_part(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1, "out": 5})
    write_lines(main_log(config, "s2"), {"t": "req", "id": "n1", "ts": 0})
    scan(config)
    size = log.stat().st_size
    write_lines(
        log, {"t": "req", "id": "m1", "ts": 1, "out": 50}, {"t": "req", "id": "m2", "ts": 2}
    )
    source = FakeSource()
    stats = scan(config, source)
    assert source.calls == [(log, size)]
    assert (stats.files_read, stats.bytes_read, stats.records) == (1, log.stat().st_size - size, 2)
    assert outputs(config) == [("n1", 0), ("m1", 50), ("m2", 0)]


def test_partial_last_line_completed_later(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1}, partial={"t": "req", "id": "m2", "ts": 2})
    stats = scan(config)
    complete = len(log.read_bytes().split(b"\n")[0]) + 1
    assert stats.bytes_read == complete
    assert outputs(config) == [("m1", 0)]
    with index(config) as ix:
        state = ix.file_state(log)
    assert state is not None and state.offset == complete and state.size == log.stat().st_size

    source = FakeSource()
    scan(config, source)
    assert source.calls == []  # nothing changed: the partial line waits

    with log.open("a") as f:
        f.write("\n")
    write_lines(log, {"t": "req", "id": "m3", "ts": 3})
    source = FakeSource()
    stats = scan(config, source)
    assert source.calls == [(log, complete)]
    assert stats.records == 2
    assert outputs(config) == [("m1", 0), ("m2", 0), ("m3", 0)]


def test_replaced_file_is_read_from_zero(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1}, {"t": "req", "id": "m2", "ts": 2})
    write_lines(log, {"t": "ev", "ts": 1}, {"t": "cost", "usd": 1.0})
    scan(config)
    tmp = log.with_suffix(".new")
    write_lines(tmp, *(dict(t="req", id=f"m{i}", ts=i, out=i) for i in (2, 3, 4, 5, 6)))
    os.replace(tmp, log)  # new inode, larger than the old offset
    source = FakeSource()
    stats = scan(config, source)
    assert source.calls == [(log, 0)]
    assert (stats.files_reset, stats.files_read) == (1, 1)
    assert outputs(config) == [("m2", 2), ("m3", 3), ("m4", 4), ("m5", 5), ("m6", 6)]
    with index(config) as ix:
        assert list(ix.events()) == []
        assert list(ix.last_cost_states()) == []


def test_shrunk_file_is_read_from_zero(config: Config) -> None:
    log = main_log(config)
    write_lines(log, *({"t": "req", "id": f"m{i}", "ts": i} for i in range(6)))
    write_lines(log, {"t": "ev", "key": "e1", "ts": 1})
    scan(config)
    inode = log.stat().st_ino
    with log.open("w") as f:  # same inode, truncated
        f.write("")
    write_lines(log, {"t": "req", "id": "m0", "ts": 0, "out": 1}, {"t": "ev", "key": "e1", "ts": 9})
    assert log.stat().st_ino == inode
    stats = scan(config)
    assert stats.files_reset == 1
    assert outputs(config) == [("m0", 1)]
    with index(config) as ix:
        assert [e.timestamp.second for e in ix.events()] == [9]
    assert scan(config).files_reset == 0


def test_vanished_file_keeps_its_rows(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1})
    scan(config)
    log.unlink()  # Claude Code's cleanup
    stats = scan(config)
    assert stats.files_seen == 0
    assert outputs(config) == [("m1", 0)]


def test_touched_file_without_new_lines(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1})
    scan(config)
    st = log.stat()
    os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    source = FakeSource()
    stats = scan(config, source)
    assert (stats.files_read, stats.files_reset, stats.records) == (0, 0, 0)
    source = FakeSource()
    assert scan(config, source).files_read == 0
    assert source.calls == []
    assert outputs(config) == [("m1", 0)]


def test_progress_on_stderr(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    write_lines(main_log(config), {"t": "req", "id": "m1", "ts": 1})
    run_scan(config, source=FakeSource())
    err = capsys.readouterr().err
    assert "1/1 files" in err and err.endswith("\n")
    run_scan(config, quiet=True, source=FakeSource())
    assert capsys.readouterr().err == ""


def test_missing_projects_dir(config: Config) -> None:
    assert scan(config).files_seen == 0


def test_default_source_is_the_claude_code_adapter(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    fake = types.ModuleType("capcom.sources.claude_code")
    fake.ClaudeCodeSource = FakeSource  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "capcom.sources.claude_code", fake)
    write_lines(main_log(config), {"t": "req", "id": "m1", "ts": 1})
    assert run_scan(config, quiet=True).records == 1


class FailingSource(FakeSource):
    """Raises on the files whose name is in `bad`."""

    def __init__(self, bad: set[str]) -> None:
        super().__init__()
        self.bad = bad

    def parse(self, log: LogFile, start: int) -> Iterator[Batch]:
        if log.path.name in self.bad:
            raise RuntimeError("synthetic parser bug")
        return super().parse(log, start)


def test_failed_file_is_reported_and_the_scan_goes_on(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    write_lines(main_log(config, "s1"), {"t": "req", "id": "m1", "ts": 1})
    write_lines(main_log(config, "s2"), {"t": "req", "id": "n1", "ts": 2})
    stats = run_scan(config, source=FailingSource({"s1.jsonl"}))
    err = capsys.readouterr().err
    assert "failed on" in err and "s1.jsonl" in err and "RuntimeError" in err
    assert "2/2 files" in err and "1 failed" in err
    assert stats.records == 1
    assert outputs(config) == [("n1", 0)]
    assert scan(config).records == 1  # retried on the next scan
    assert outputs(config) == [("m1", 0), ("n1", 0)]


def test_in_place_rewrite_with_the_same_inode_is_read_from_zero(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1}, {"t": "req", "id": "m2", "ts": 2})
    scan(config)
    inode, size = log.stat().st_ino, log.stat().st_size
    with log.open("r+b") as f:  # same inode, same length head, other content
        f.write(log.read_bytes().replace(b'"m1"', b'"x1"').replace(b'"m2"', b'"x2"'))
    write_lines(log, {"t": "req", "id": "x3", "ts": 3})
    assert (log.stat().st_ino, log.stat().st_size > size) == (inode, True)
    stats = scan(config)
    assert (stats.files_reset, stats.records) == (1, 3)
    assert outputs(config) == [("x1", 0), ("x2", 0), ("x3", 0)]
    write_lines(log, {"t": "req", "id": "x4", "ts": 4})  # a plain append is no reset
    assert scan(config).files_reset == 0


def test_short_file_head_grows_without_a_reset(config: Config) -> None:
    log = main_log(config)
    write_lines(log, {"t": "req", "id": "m1", "ts": 1})
    scan(config)
    for i in range(2, 80):  # past the hashed head, a few lines per scan
        write_lines(log, {"t": "req", "id": f"m{i}", "ts": i, "pad": "y" * 100})
        if i % 10 == 0:
            assert scan(config).files_reset == 0
    assert scan(config).files_reset == 0
    assert len(outputs(config)) == 79


def test_meta_change_alone_is_picked_up(config: Config) -> None:
    log = sub_log(config, "a1")
    write_lines(log, {"t": "req", "id": "m1", "ts": 1})
    meta = log.with_name("agent-a1.meta.json")
    meta.write_text(json.dumps({"agentType": "scout", "finished": False}))
    scan(config)
    offset = log.stat().st_size
    meta.write_text(json.dumps({"agentType": "scout", "finished": True}))
    st = meta.stat()
    os.utime(meta, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    source = FakeSource()
    stats = scan(config, source)
    assert source.calls == [(log, offset)]
    assert (stats.files_read, stats.files_reset, stats.records) == (0, 0, 1)
    with index(config) as ix:
        assert [(a.agent_type, a.finished) for a in ix.agents()] == [("scout", True)]
    source = FakeSource()
    scan(config, source)
    assert source.calls == []


def test_run_scan_refuses_an_index_inside_the_logs(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    config = Config(claude, tmp_path / ".openclaw", claude / "capcom-cache")
    with pytest.raises(ValueError, match="log folder"):
        run_scan(config, quiet=True, source=FakeSource())
    assert not claude.exists()
