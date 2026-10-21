"""Synthetic logs written by tests/fixtures/generate.py, scanned by the real Claude Code
adapter into a real index, then explained: the rules read the events as the parser
writes them."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from capcom.config import Config
from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.explain import explain
from capcom.store.index import SqliteIndex, open_index
from capcom.store.scan import run_scan
from capcom.types import CauseKind, Confidence, Request, Spike, SpikeKind
from tests.fixtures.generate import Reply, SessionWriter, at


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache" / "capcom")


@pytest.fixture
def index(config: Config) -> Iterator[SqliteIndex]:
    s = SessionWriter(config.claude_root, cwd="/home/user/proj", session_id="e2e-1")
    s.user_prompt(at(0))
    s.api_request(at(1), "claude-opus-5-5", ttl="1h")
    s.assistant(
        Reply(
            at(1),
            cache_write_1h=150_000,
            output=50,
            tools=[("Read", {"file_path": "/home/user/proj/big.json"})],
        )
    )
    s.tool_result(s.last_tool_ids[0], at(2), chars=400_000)
    s.api_request(at(3), "claude-opus-5-5", ttl="1h")
    s.assistant(Reply(at(3), cache_read=150_000, cache_write_1h=100_000, output=50))
    for k in range(4):
        s.api_request(at(10 + k), "claude-opus-5-5", ttl="1h")
        s.assistant(Reply(at(10 + k), cache_read=250_000, output=50))
    s.user_prompt(at(5000))
    s.api_request(at(5000), "claude-opus-5-5", ttl="1h")
    s.assistant(Reply(at(5000), cache_write_1h=250_100, output=50))
    s.close()
    run_scan(config, quiet=True)
    with open_index(config.index_path, readonly=True) as idx:
        yield idx


def _spike(reqs: list[Request], kind: SpikeKind) -> Spike:
    table = load_prices()
    return Spike(
        kind,
        reqs[0].timestamp,
        reqs[-1].timestamp + (at(1) - at(0)),
        sum(price(r, table).total for r in reqs),
        0.0,
        "e2e-1",
        message_ids=tuple(r.message_id for r in reqs),
    )


def test_parsed_logs_explain(index: SqliteIndex) -> None:
    reqs = list(index.requests(session_id="e2e-1"))
    assert len(reqs) == 7
    table = load_prices()
    big = _spike(reqs[1:6], SpikeKind.SLOPE_CHANGE)
    idle = _spike(reqs[6:], SpikeKind.SINGLE_REQUEST)
    out = explain(index, table, [big, idle])
    first = out[0]
    assert first.spike is big and first.cause is CauseKind.LARGE_TOOL_RESULT
    assert any(
        "Read of /home/user/proj/big.json returned about 100k" in e.label for e in first.evidence
    )
    mine = [e for e in out if e.spike is idle]
    last = mine[0]
    assert last.cause is CauseKind.IDLE_REWRITE
    assert last.confidence is Confidence.CLEAR
    assert any("cacheControl ttl 1h" in e.label for e in last.evidence)
    # the output and the new tokens are no rule's: they show as the unexplained rest
    assert [e.cause for e in mine[1:]] == [CauseKind.UNEXPLAINED]
    assert sum(e.excess_usd for e in mine) == pytest.approx(idle.excess_usd)
