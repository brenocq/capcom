"""Grouping keys and labels (synthetic requests only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline.groups import (
    CATEGORIES,
    OPENCLAW,
    TEMPORARY,
    Grouper,
    folder_name,
    is_temporary,
)
from capcom.types import AgentInfo, GroupBy, Request, SessionInfo
from tests.cost.fakes import FakeIndex, req

TABLE = load_prices()
OPENCLAW_ROOT = Path("/home/user/.openclaw")


def grouper(ix: FakeIndex | None = None) -> Grouper:
    return Grouper.from_index(ix or FakeIndex(), TABLE, OPENCLAW_ROOT)


def key(r: Request, by: GroupBy, g: Grouper | None = None) -> tuple[str, str]:
    g = g or grouper()
    members = g.members(r, by, price(r, TABLE))
    assert len(members) == 1
    return members[0][0], members[0][1]


def test_folder_name_matches_claude_code() -> None:
    assert folder_name("/home/user/.openclaw") == "-home-user--openclaw"
    assert folder_name("/home/user/my_proj.v2") == "-home-user-my-proj-v2"


def test_project_groups_temporary_and_openclaw() -> None:
    assert is_temporary("-tmp-claude-1000-abc")
    assert is_temporary("-tmp")
    assert not is_temporary("-tmpfoo")
    assert not is_temporary("-home-user-tmp")
    assert key(req(project="-tmp-scratch-1"), GroupBy.PROJECT) == (TEMPORARY, TEMPORARY)
    assert key(req(project="-home-user--openclaw"), GroupBy.PROJECT) == (OPENCLAW, OPENCLAW)
    assert key(req(project="-home-user--openclaw-workspace-x"), GroupBy.PROJECT)[0] == OPENCLAW
    assert key(req(project="-home-user--openclawish"), GroupBy.PROJECT)[0] != OPENCLAW
    assert key(req(project="-home-user-proj"), GroupBy.PROJECT) == (
        "-home-user-proj",
        "-home-user-proj",
    )


def test_openclaw_root_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "/srv/claw.state")
    g = Grouper.from_index(FakeIndex(), TABLE)
    assert g.project("-srv-claw-state-agents")[0] == OPENCLAW


def test_model_is_canonical_and_skips_synthetic() -> None:
    assert key(req(model="claude-haiku-4-5", output=10), GroupBy.MODEL)[0] == (
        "claude-haiku-4-5-20251001"
    )
    assert key(req(model="claude-opus-5-5[1m]", output=10), GroupBy.MODEL)[0] == "claude-opus-5-5"
    synthetic = req(model="<synthetic>")
    assert grouper().members(synthetic, GroupBy.MODEL, price(synthetic, TABLE)) == []


def test_agent_main_type_and_unknown() -> None:
    ix = FakeIndex(agent_infos=[AgentInfo("a1", "s1", agent_type="scout"), AgentInfo("a2", "s1")])
    g = grouper(ix)
    assert key(req(agent=None), GroupBy.AGENT, g) == ("main", "main")
    assert key(req(agent="a1"), GroupBy.AGENT, g) == ("scout", "scout")
    assert key(req(agent="a2"), GroupBy.AGENT, g) == ("unknown", "unknown")
    assert key(req(agent="a9"), GroupBy.AGENT, g) == ("unknown", "unknown")
    # agent ids are looked up within their own session
    assert key(req(session="s2", agent="a1"), GroupBy.AGENT, g)[0] == "unknown"


def test_session_label_is_title_or_short_id() -> None:
    sid = "0123456789abcdef"
    ix = FakeIndex(infos=[SessionInfo("titled", "-p", title="Fix the parser")])
    g = grouper(ix)
    assert key(req(session="titled"), GroupBy.SESSION, g) == ("titled", "Fix the parser")
    assert key(req(session=sid), GroupBy.SESSION, g) == (sid, "01234567")


def test_category_splits_the_cost() -> None:
    r = req(input=1000, cache_read=100_000, cache_write_1h=2000, output=500, web_searches=2)
    cost = price(r, TABLE)
    members = grouper().members(r, GroupBy.CATEGORY, cost)
    assert [(k, label) for k, label, _ in members] == list(CATEGORIES)
    got = {k: v for k, _, v in members}
    assert got == {
        "input": cost.input,
        "cache_read": cost.cache_read,
        "cache_write": cost.cache_write,
        "output": cost.output,
        "web": cost.web,
    }
    assert abs(sum(got.values()) - cost.total) < 1e-12
