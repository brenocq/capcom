"""The serve's background scan feeding "Live now": synthetic Claude Code logs, read by
the real source through `background_scan`, shown by a served /api/live. Each test
scans by hand where the server's `Scanner` would every few seconds."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.config import Config
from capcom.report.server import ApiCache, Scanner, Site, background_scan
from capcom.store.index import open_index
from tests.fixtures.generate import Reply, SessionWriter
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve

CWD = "/home/user/live"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache" / "capcom", tmp_path / "st"
    )


@pytest.fixture
def client(config: Config) -> Iterator[Client]:
    config.projects_dir.mkdir(parents=True)
    background_scan(config)  # the index exists before the server reads it
    yield from serve(Site(config.index_path, TABLE, FAKE, clock=lambda: NOW))


def live_ids(client: Client) -> list[str]:
    status, data, _ = client.get("/api/live")
    assert status == 200
    return sorted(s["session_id"] for s in data["sessions"])


def outputs(config: Config) -> list[tuple[str, int]]:
    with open_index(config.index_path, readonly=True) as ix:
        return sorted((r.session_id, r.usage.output) for r in ix.requests())


def reply(minutes_ago: float, output: int = 300, records: int = 1) -> Reply:
    return Reply(NOW - timedelta(minutes=minutes_ago), output=output, records=records)


def test_a_new_session_shows_after_one_scan(config: Config, client: Client) -> None:
    assert live_ids(client) == []
    s = SessionWriter(config.claude_root, cwd=CWD, session_id="fresh")
    s.assistant(reply(1))
    s.close()
    background_scan(config)
    assert live_ids(client) == ["fresh"]


def test_an_appended_reply_is_read_on_the_next_scan(config: Config, client: Client) -> None:
    s = SessionWriter(config.claude_root, cwd=CWD, session_id="old")
    s.assistant(reply(60))  # outside the live window
    background_scan(config)
    assert live_ids(client) == []
    s.assistant(reply(1, output=700))
    s.close()
    background_scan(config)
    assert live_ids(client) == ["old"]
    assert outputs(config) == [("old", 300), ("old", 700)]


def test_a_reply_streamed_across_two_scans_counts_once(config: Config, client: Client) -> None:
    # Claude Code writes one reply as several records with the same message.id; a
    # scan between two of them must leave one row, from the last record.
    whole = SessionWriter(config.claude_root.parent / "draft", cwd=CWD, session_id="split")
    whole.assistant(reply(1, output=900, records=3))
    whole.close()
    first, *rest = whole.path.read_text(encoding="utf-8").splitlines(keepends=True)
    log = config.projects_dir / whole.path.parent.name / whole.path.name
    log.parent.mkdir(parents=True)
    log.write_text(first, encoding="utf-8")
    background_scan(config)
    assert outputs(config) == [("split", 4)]  # the stream-start count, for now
    with log.open("a", encoding="utf-8") as f:
        f.writelines(rest)
    background_scan(config)
    assert outputs(config) == [("split", 900)]
    assert live_ids(client) == ["split"]


def test_a_partial_line_waits_until_it_is_complete(config: Config, client: Client) -> None:
    s = SessionWriter(config.claude_root, cwd=CWD, session_id="typing")
    s.assistant(reply(2))
    s.close()
    line = s.path.read_text(encoding="utf-8")
    record = json.loads(line)
    record["message"]["id"] = "msg_late"
    record["message"]["usage"]["output_tokens"] = 55
    tail = json.dumps(record, separators=(",", ":")) + "\n"
    with s.path.open("a", encoding="utf-8") as f:
        f.write(tail[:40])  # a write in progress
    background_scan(config)
    assert outputs(config) == [("typing", 300)]
    background_scan(config)  # nothing changed: nothing read again
    assert outputs(config) == [("typing", 300)]
    with s.path.open("a", encoding="utf-8") as f:
        f.write(tail[40:])
    background_scan(config)
    assert outputs(config) == [("typing", 55), ("typing", 300)]


def test_scanner_rests_longer_after_a_slow_scan() -> None:
    scanner = Scanner(lambda: None, interval=3.0, rest=4.0)
    assert scanner.pause(0.005) == 3.0  # a scan with nothing new: the interval
    assert scanner.pause(2.0) == 8.0  # a catch-up: at most a fifth of a core


def test_held_generation_moves_at_most_once_per_hold(config: Config, client: Client) -> None:
    cache = ApiCache(hold=60.0)
    path = config.index_path

    def write(session: str) -> None:
        s = SessionWriter(config.claude_root, cwd=CWD, session_id=session)
        s.assistant(reply(1))
        s.close()
        background_scan(config)

    first = cache.current(path, now=1000.0)
    write("a")
    assert cache.current(path, now=1030.0) == first  # held: it moved at 1000
    second = cache.current(path, now=1060.0)
    assert second != first  # the hold is over: the newest generation
    write("b")
    assert cache.current(path, now=1100.0) == second
    write("c")
    third = cache.current(path, now=1120.0)
    assert third != second
    assert cache.current(path, now=1300.0) == third  # nothing new: no move
    write("d")
    assert cache.current(path, now=1301.0) != third  # after a quiet hold: at once
    assert live_ids(client) == ["a", "b", "c", "d"]  # Live now reads past any hold


def test_version_moves_only_when_a_scan_writes(config: Config, client: Client) -> None:
    def index_version() -> str:
        status, data, _ = client.get("/api/version")
        assert status == 200
        return str(data["index"])

    before = index_version()
    background_scan(config)  # nothing new
    assert index_version() == before
    s = SessionWriter(config.claude_root, cwd=CWD, session_id="v")
    s.assistant(reply(1))
    s.close()
    background_scan(config)
    after = index_version()
    assert after != before
    background_scan(config)
    assert index_version() == after
