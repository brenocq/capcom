"""The timeline on a real index built from synthetic logs (tests/fixtures/generate.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from capcom.config import Config
from capcom.cost.prices import load_prices
from capcom.store.index import open_index
from capcom.store.scan import run_scan
from capcom.timeline import cumulative, find_spikes, per_step
from capcom.types import GroupBy, SpikeKind
from tests.fixtures.generate import T0, Reply, SessionWriter, at

TABLE = load_prices()


def build(tmp_path: Path) -> tuple[Config, list[str]]:
    # the OpenClaw dir is only a cwd here (nothing is read from it)
    config = Config(
        tmp_path / ".claude", Path("/home/user/.openclaw"), tmp_path / "cache" / "capcom"
    )
    s = SessionWriter(config.claude_root, cwd="/home/user/proj", session_id="sess-0001-aaaa")
    s.ai_title("Synthetic title")
    ids = []
    for k in range(25):
        ids.append(s.assistant(Reply(at(60 * k), cache_read=250_000, output=10, records=2)))
    ids.append(s.assistant(Reply(at(60 * 25), cache_write_1h=300_000, output=10)))
    sub = s.subagent("scout", "look around")
    sub.assistant(Reply(at(90), model="claude-haiku-4-5-20251001", output=1_000))
    sub.close()
    tmp = SessionWriter(config.claude_root, cwd="/tmp/scratch", session_id="sess-0002-bbbb")
    tmp.assistant(Reply(at(30), output=2_000))
    tmp.close()
    claw = SessionWriter(
        config.claude_root, cwd=str(config.openclaw_root / "workspace"), session_id="sess-3"
    )
    claw.assistant(Reply(at(45), output=3_000))
    claw.close()
    s.close()
    run_scan(config, quiet=True)
    return config, ids


def test_series_and_spikes_from_the_store(tmp_path: Path) -> None:
    config, ids = build(tmp_path)
    since, until = T0, T0 + timedelta(hours=1)
    with open_index(config.index_path, readonly=True) as ix:
        projects = cumulative(
            ix, TABLE, since, until, GroupBy.PROJECT, timedelta(minutes=10), config.openclaw_root
        )
        sessions = per_step(ix, TABLE, since, until, GroupBy.SESSION, timedelta(minutes=10))
        agents = per_step(ix, TABLE, since, until, GroupBy.AGENT, until - since)
        found = find_spikes(ix, TABLE, since, until)
    assert {s.key for s in projects} == {"-home-user-proj", "temporary", "openclaw"}
    assert all(len(s.times) == 6 and s.times[0] == since for s in projects)
    labels = {s.key: s.label for s in sessions}
    assert labels["sess-0001-aaaa"] == "Synthetic title"
    assert labels["sess-0002-bbbb"] == "sess-000"
    assert {s.key for s in agents} == {"main", "scout"}
    assert [(s.kind, s.message_ids) for s in found] == [(SpikeKind.SINGLE_REQUEST, (ids[-1],))]
