"""What changed: daily level shifts and the sessions and agent types behind them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline import regime_changes
from capcom.timeline.regimes import find_changes
from capcom.types import AgentInfo, SessionInfo
from tests.cost.fakes import FakeIndex, req

TABLE = load_prices()
OC = Path("/home/user/.openclaw")
START = datetime(2026, 10, 1, tzinfo=UTC)
PER_DOLLAR = round(1e6 / price(req(output=1_000_000), TABLE).total)  # output tokens a dollar


def day(n: int, hour: int = 12) -> datetime:
    return START + timedelta(days=n, hours=hour)


def spend(index: FakeIndex, n: int, usd: float, session: str, agent: str | None = None) -> None:
    project = f"-home-user-{session}"
    r = req(
        session=session, when=day(n), agent=agent, project=project, output=int(usd * PER_DOLLAR)
    )
    index.reqs.append(r)


def story() -> FakeIndex:
    """Quiet days at about $2, then from day 15 (Oct 16) two sessions resume: one
    main thread at $20 a day, one with coder subagents at $12 and its lead at $3."""
    ix = FakeIndex()
    ix.infos = [
        SessionInfo("old", "-home-user-old", cwd="/home/user/old", title="Notes"),
        SessionInfo("imp", "-home-user-imp", cwd="/home/user/imp", title="Improved"),
        SessionInfo("toy", "-home-user-toy", cwd="/home/user/toy", title="Toy brain"),
    ]
    ix.agent_infos = [AgentInfo("c1", "toy", agent_type="coder")]
    for n in range(21):
        spend(ix, n, 2.0 + 0.1 * (n % 3), "old")
    for n in range(15, 21):
        spend(ix, n, 20.0, "imp")
        spend(ix, n, 3.0, "toy")
        spend(ix, n, 12.0, "toy", agent="c1")
    return ix


def test_a_level_shift_names_the_day_and_who_rose() -> None:
    ix = story()
    out = regime_changes(ix, TABLE, START, START + timedelta(days=21), tz=UTC, openclaw_root=OC)
    assert [c.day for c in out] == [date(2026, 10, 16)]
    c = out[0]
    assert c.before_usd == pytest.approx(2.1, abs=0.15)
    assert c.after_usd == pytest.approx(37.1, abs=0.2)
    assert c.ratio > 10
    assert [s.key for s in c.sessions] == ["imp", "toy"]
    assert c.sessions[0].label == "Improved" and c.sessions[0].project == "imp"
    assert c.sessions[0].share == pytest.approx(20 / 35, abs=0.01)
    assert sum(s.share for s in c.sessions) == pytest.approx(1.0)
    assert [a.key for a in c.agent_types] == ["main", "coder"]
    assert c.agent_types[1].rise_usd == pytest.approx(12.0, abs=0.01)


def test_steady_or_small_spend_has_no_change() -> None:
    ix = FakeIndex()
    for n in range(21):
        spend(ix, n, 2.0, "s1")
    for n in range(15, 21):
        spend(ix, n, 4.0, "s1")  # 3x, but only $4 a day more
    assert (
        regime_changes(ix, TABLE, START, START + timedelta(days=21), tz=UTC, openclaw_root=OC) == []
    )


def test_a_short_window_has_no_change() -> None:
    ix = story()
    since = START + timedelta(days=12)
    assert (
        regime_changes(ix, TABLE, since, since + timedelta(days=9), tz=UTC, openclaw_root=OC) == []
    )


def test_one_change_per_run_and_a_second_shift_later() -> None:
    daily = [1.0] * 10 + [10.0] * 10 + [100.0] * 10
    got = [k for k, _, _ in find_changes(daily)]
    assert got == [10, 20]


def test_a_single_spiky_day_is_not_a_shift() -> None:
    daily = [2.0] * 10 + [80.0] + [2.0] * 9
    assert find_changes(daily) == []


def test_costs_are_the_cost_model_s() -> None:
    r = req(output=PER_DOLLAR)
    assert price(r, TABLE).total == pytest.approx(1.0)
