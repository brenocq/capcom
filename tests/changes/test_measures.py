"""Each kind's measure on hand-made facts of one week: what it counts, over what
base, and the dollar value of a counted unit."""

from __future__ import annotations

from datetime import timedelta

import pytest

from capcom.changes.measures import MEASURES, Tally
from capcom.checks.facts import (
    Account,
    Compaction,
    Facts,
    LongContext,
    ModelMix,
    Rewrite,
    Thread,
)
from capcom.types import AgentInfo
from tests.checks.helpers import SortedIndex
from tests.cost.fakes import T0

P = "-home-user-proj"
EMPTY = SortedIndex()


def _facts(*threads: Thread) -> Facts:
    f = Facts(since=T0, until=T0 + timedelta(days=7))
    for t in threads:
        f.threads[(t.session_id, t.agent_id)] = t
        f.session_project.setdefault(t.session_id, t.project)
    return f


def _thread(sid: str, requests: int, agent: str | None = None, project: str = P) -> Thread:
    return Thread(sid, agent, project=project, requests=requests, tool_calls=requests)


def _run(
    kind: str,
    facts: Facts,
    scope: str = P,
    flagged: dict[tuple[str, str | None], int] | None = None,
    errors: dict[tuple[str, str | None], int] | None = None,
) -> tuple[Tally, float | None]:
    m = MEASURES[kind]
    t = m.tally(facts, scope, flagged or {}, errors or {})
    return t, m.value(t)


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("subagent_cache_expiry", "tool_waits"),
        ("tool_wait_expiry", "tool_waits"),
        ("subagent_resumed_late", "resumes"),
        ("idle_cache_rewrites", "idle"),
        ("model_switches", "switches"),
    ],
)
def test_rewrites_per_thousand_requests_of_the_project(kind: str, field: str) -> None:
    f = _facts(_thread("s1", 1500), _thread("s2", 500, "a1"), _thread("s3", 9000, project="-x"))
    getattr(f, field)[P] = Rewrite(count=6, excess_usd=3.0, tokens=600_000)
    getattr(f, field)["-x"] = Rewrite(count=50, excess_usd=9.0)
    t, value = _run(kind, f)
    assert value == pytest.approx(6 / 2000 * 1000)
    assert t.usd / t.count == pytest.approx(0.5)  # one rewrite's excess
    _, everywhere = _run(kind, f, scope="all")
    assert everywhere == pytest.approx(56 / 11000 * 1000)


def test_turns_past_the_line_leave_out_heartbeats() -> None:
    f = _facts(_thread("s1", 1000), _thread("s2", 1000))
    f.long_context[P] = LongContext(
        per_thread={("s1", None): [2.0, 1, 40, 1.0], ("s2", None): [0.0, 0, 0, 0.0]},
        max_context=300_000,
    )
    t, value = _run("long_running_agents", f)
    assert value == pytest.approx(40 / 2000 * 1000)
    assert t.usd == pytest.approx(2.0)
    assert t.maxes["max_context"] == 300_000


def test_late_compactions_count_those_started_past_the_line_in_the_week() -> None:
    f = _facts(_thread("s1", 500))
    f.compactions[("s1", None)] = [
        Compaction(("s1", None), T0 + timedelta(hours=1), "k1", 450_000, excess_usd=1.5, turns=9),
        Compaction(("s1", None), T0 + timedelta(hours=2), "k2", 200_000),
        Compaction(("s1", None), T0 - timedelta(hours=2), "k0", 500_000),  # before the week
    ]
    t, value = _run("late_compactions", f)
    assert value == pytest.approx(1 / 500 * 1000)
    assert t.sums["compactions"] == 2
    assert t.usd == pytest.approx(1.5)


def test_a_heartbeat_is_measured_by_the_median_context_at_a_run_start() -> None:
    th = _thread("s1", 300)
    th.runs = [[60_000, 1e-6], [120_000, 2e-6], [180_000, 1e-6]]
    th.hist = {6: [1, 0, 0]}
    f = _facts(th, _thread("s2", 100, "a1"))
    t, value = _run("heartbeat_growing_session", f)
    assert value == 120_000
    assert t.base == 3  # runs, not requests
    assert t.usd / t.count == pytest.approx((0.06 + 0.24 + 0.18) / 360_000)


def test_never_ended_is_the_share_of_requests_in_sessions_open_past_a_week() -> None:
    old, new = _thread("s1", 300), _thread("s2", 700)
    sub = _thread("s1", 100, "a1")
    f = _facts(old, new, sub)
    f.session_span = {"s1": timedelta(days=12), "s2": timedelta(hours=3)}
    t, value = _run("session_never_ended", f)
    assert value == pytest.approx(400 / 1100 * 100)
    assert t.sums["sessions"] == 1


def test_model_mix_is_dollars_per_request_with_quality_signals() -> None:
    a1, a2 = _thread("s1", 30, "a1"), _thread("s1", 10, "a2")
    f = _facts(_thread("s1", 100), a1, a2)
    f.agents = {
        "a1": AgentInfo("a1", "s1", agent_type="fast-worker"),
        "a2": AgentInfo("a2", "s1", agent_type="fast-worker"),
    }
    f.model_mix[("fast-worker", P)] = ModelMix(
        cost=8.0, by_model={"claude-opus-5-5": 6.0, "claude-sonnet-5": 2.0}, top_cost=6.0
    )
    m = MEASURES["model_mix"]
    t, value = _run(
        "model_mix", f, scope="fast-worker", flagged={("s1", "a1"): 5}, errors={("s1", "a1"): 2}
    )
    assert value == pytest.approx(8.0 / 40)
    assert t.usd / t.count == 1.0
    rows = {label: fn(t, 1.0) for label, fn in m.quality}
    assert rows["Requests per spawn"] == "20.0"
    assert rows["Tool errors per 100 tool calls"] == "5.0"  # 2 errors in 40 calls
    assert {label: fn(t, 1.0) for label, fn in m.rows}["Share on Opus"] == "75%"


@pytest.mark.parametrize(
    ("kind", "key", "other"),
    [
        ("skills_unused", ("skill", "pdf"), ("memory", "instructions", "CLAUDE.md")),
        ("mcp_servers_unused", ("tool", "deferred", "mcp__db__query"), ("skill", "pdf")),
        ("mcp_servers_unused", ("mcp", "db"), ("mcp", "claude.ai Mail")),
        ("claudeai_connectors_unused", ("mcp", "claude.ai Mail"), ("mcp", "db")),
        ("large_memory", ("memory", "instructions", "CLAUDE.md"), ("tool", "shape", "Read")),
        ("images_in_tool_results", ("image", "ev1", "Read"), ("skill", "pdf")),
    ],
)
def test_items_carried_are_tokens_per_request_of_the_project(
    kind: str, key: tuple[str, ...], other: tuple[str, ...]
) -> None:
    f = _facts(_thread("s1", 100), _thread("s2", 300, project="-x"))
    f.accounts[("s1", None, key)] = Account(usd=0.4, requests=100, size=2_000)
    f.accounts[("s1", None, other)] = Account(usd=9.0, requests=100, size=9_000)
    f.accounts[("s2", None, key)] = Account(usd=9.0, requests=300, size=9_000)
    t, value = _run(kind, f)
    assert value == pytest.approx(2_000)
    assert t.usd / t.count == pytest.approx(0.4 / 200_000)  # what a carried token cost


def test_an_agents_tools_are_counted_in_its_own_threads() -> None:
    f = _facts(_thread("s1", 100), _thread("s1", 50, "a1"), _thread("s1", 50, "a2"))
    f.agents = {
        "a1": AgentInfo("a1", "s1", agent_type="reviewer"),
        "a2": AgentInfo("a2", "s1", agent_type="other"),
    }
    for aid in (None, "a1", "a2"):
        f.accounts[("s1", aid, ("tool", "shape", "Read"))] = Account(usd=0.1, requests=50, size=600)
    t, value = _run("agent_tools_unused", f, scope="reviewer")
    assert value == pytest.approx(600)
    assert t.requests == 50


def test_long_reports_count_the_spawns_of_that_type_in_their_sessions() -> None:
    f = _facts(_thread("s1", 100), _thread("s1", 20, "a1"), _thread("s2", 400))
    f.agents = {"a1": AgentInfo("a1", "s1", agent_type="researcher")}
    f.spawn_types = {"tu1": "researcher", "tu2": "other"}
    f.accounts[("s1", None, ("report", "ev1", "tu1"))] = Account(usd=0.3, requests=60, size=3_000)
    f.accounts[("s1", None, ("report", "ev2", "tu2"))] = Account(usd=0.3, requests=60, size=3_000)
    t, value = _run("long_subagent_reports", f, scope="researcher")
    assert t.base == 120  # the requests of s1, where a researcher ran
    assert value == pytest.approx(3_000 * 60 / 120)


def test_an_empty_week_has_no_value() -> None:
    _, value = _run("skills_unused", _facts())
    assert value is None
