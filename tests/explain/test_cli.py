"""`capcom spikes` on a fake index with a fake spike detector."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from capcom.config import Config
from capcom.explain.cli import run_spikes
from capcom.types import Index, PriceTable, SessionInfo, Spike
from tests.explain.helpers import TABLE, World, at, spike


def _world() -> tuple[World, list[Spike]]:
    w = World()
    w.index.infos.append(SessionInfo("s1", "-home-user-proj"))
    w.req(0, cache_write_1h=200_000)
    w.req(2, cache_read=200_000)
    idle = w.req(80, cache_write_1h=205_000)
    polls = [
        w.call(100 + k, "Bash", "sleep", 5_000, cache_read=80_000 + 5_000 * k) for k in range(6)
    ]
    plain = w.req(200, cache_read=1_000, output=300)
    return w, [spike([idle]), spike(polls), spike([plain], baseline=0.001)]


def _config(tmp_path: Path) -> Config:
    return Config(tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache")


def test_spikes_view(tmp_path: Path) -> None:
    w, spikes = _world()
    seen: list[tuple[datetime, datetime]] = []

    def find(index: Index, table: PriceTable, since: datetime, until: datetime) -> list[Spike]:
        seen.append((since, until))
        return spikes

    out = io.StringIO()
    code = run_spikes(
        _config(tmp_path),
        days=14,
        limit=2,
        index=w.index,
        out=out,
        now=at(300),
        find_spikes=find,
        table=TABLE,
    )
    text = out.getvalue()
    assert code == 0
    assert seen and seen[0][1] == at(300) and seen[0][0] < at(300)
    assert "3 spikes; 2 matched to a cause in the logs" in text
    # ranked by excess: the idle rewrite first
    assert text.index("Cache rebuilt after 78 min idle") < text.index("The same Bash call")
    assert "Idle past cache lifetime: $1.90 (clear cause)" in text
    assert "What to change:" in text
    assert "Reading them would have cost $1.90 less." in text
    assert "(1 more spikes; raise --limit to see them)" in text
    summary = text[text.index("Excess by cause") :]
    assert "Idle past cache lifetime" in summary and "Unexplained" in summary
    assert "-home-user-proj" in text


def test_no_spikes(tmp_path: Path) -> None:
    w, _ = _world()
    out = io.StringIO()
    code = run_spikes(
        _config(tmp_path),
        days=0,
        limit=5,
        index=w.index,
        out=out,
        now=at(300),
        find_spikes=lambda *a: [],
        table=TABLE,
    )
    assert code == 0
    assert "capcom spikes, all time" in out.getvalue()
    assert "no spikes in this window" in out.getvalue()


def test_timeline_stub_is_reported(tmp_path: Path) -> None:
    w, _ = _world()

    def stub(*args: object) -> list[Spike]:
        raise NotImplementedError

    assert (
        run_spikes(
            _config(tmp_path),
            14,
            5,
            index=w.index,
            out=io.StringIO(),
            find_spikes=stub,
            table=TABLE,
        )
        == 1
    )


def test_missing_index(tmp_path: Path) -> None:
    assert run_spikes(_config(tmp_path), 14, 5, out=io.StringIO(), table=TABLE) == 1
