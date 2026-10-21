import io

import pytest

from capcom.config import load_config
from capcom.cost.prices import load_prices
from capcom.cost.top import TEMPORARY, Row, aggregate, format_top, run_calibrate, run_top
from capcom.types import AgentInfo, CostState, ModelUsage, SessionInfo

from .fakes import T0, FakeIndex, at, req

T = load_prices()
M = 1_000_000


def sample() -> FakeIndex:
    return FakeIndex(
        reqs=[
            req("s1", when=at(0), project="-home-user-a", input=M, cache_read=M, output=M),
            req(
                "s1",
                when=at(1),
                project="-home-user-a",
                agent="ag1",
                model="claude-haiku-5-5",
                output=M,
            ),
            req("s1", when=at(2), project="-home-user-a", agent="ag2", output=1000, estimated=True),
            req("s2", when=at(60 * 30), project="-home-user-b", model="claude-opus-5-6", output=M),
            req("s3", when=at(5), project="-tmp-claude-xyz", model="<synthetic>", output=5),
            req("s4", when=at(6), project="-tmp-other", model="claude-sonnet-5-5", input=M),
        ],
        infos=[SessionInfo("s1", "-home-user-a", title="fix the parser")],
        agent_infos=[AgentInfo("ag1", "s1", agent_type="scout")],
        costs=[CostState("s1", 0, 25.0, {"claude-opus-5-5": ModelUsage(cost_usd=24.5)}, None)],
    )


def rows_by(by: str, idx: FakeIndex | None = None) -> dict[str, Row]:
    idx = idx or sample()
    titles = {s.session_id: s.title for s in idx.infos if s.title}
    types = {a.agent_id: a.agent_type for a in idx.agent_infos if a.agent_type}
    return {r.label: r for r in aggregate(idx.requests(), by, T, titles, types)}


def test_by_project_groups_temporary_folders() -> None:
    rows = rows_by("project")
    assert set(rows) == {"-home-user-a", "-home-user-b", TEMPORARY}
    tmp = rows[TEMPORARY]
    assert tmp.requests == 2
    a = rows["-home-user-a"]
    assert a.requests == 3
    assert a.usd_input == pytest.approx(4)
    assert a.usd_cache_read == pytest.approx(0.2)
    assert a.total == pytest.approx(4 + 0.2 + 20 + 0.5 + 0.02)
    assert a.flags == "~"
    assert rows["-home-user-b"].flags == "?"


def test_by_session_uses_titles() -> None:
    rows = rows_by("session")
    assert "fix the parser" in rows and "s2" in rows


def test_by_agent_type() -> None:
    rows = rows_by("agent")
    assert set(rows) == {"main", "scout", "(unknown agent)"}
    assert rows["main"].requests == 4


def test_by_model_and_day() -> None:
    assert set(rows_by("model")) == {
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "claude-opus-5-6",
        "<synthetic>",
        "claude-sonnet-5-5",
    }
    days = list(rows_by("day"))
    assert days == ["2026-10-21", "2026-10-20"]  # newest first


def test_rows_are_sorted_by_total() -> None:
    rows = aggregate(sample().requests(), "project", T)
    totals = [r.total for r in rows]
    assert totals == sorted(totals, reverse=True)


def test_unknown_grouping() -> None:
    with pytest.raises(ValueError):
        aggregate(sample().requests(), "color", T)


def test_format_top_limits_and_totals() -> None:
    text = format_top(aggregate(sample().requests(), "model", T), "model", limit=2)
    assert "all (5 models)" in text
    assert "3 more rows" in text
    assert "~ output estimated" in text and "? priced as the closest" in text


def test_run_top_prints_a_table() -> None:
    out = io.StringIO()
    cfg = load_config({"HOME": "/nonexistent"})
    assert run_top(cfg, "agent", days=0, limit=10, index=sample(), out=out) == 0
    text = out.getvalue()
    assert "scout" in text and "main" in text and "$" in text


def test_run_top_window() -> None:
    out = io.StringIO()
    cfg = load_config({"HOME": "/nonexistent"})
    now = T0.replace(day=21, hour=12)
    run_top(cfg, "session", days=1, limit=10, index=sample(), out=out, now=now)
    assert "s2" in out.getvalue() and "fix the parser" not in out.getvalue()


def test_run_top_without_an_index() -> None:
    cfg = load_config({"HOME": "/nonexistent", "XDG_CACHE_HOME": "/nonexistent/cache"})
    assert run_top(cfg, "project", days=14, limit=5) == 1


def test_run_calibrate() -> None:
    out = io.StringIO()
    cfg = load_config({"HOME": "/nonexistent"})
    assert run_calibrate(cfg, limit=5, index=sample(), out=out) == 0
    text = out.getvalue()
    assert "fix the parser" in text
    assert "comparable" in text and "all: 1 sessions" in text
