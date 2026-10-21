"""The fixes from the simulated users' round: the "what changed" story, the checks
roll-up, spike causes and times, calibration of resumed sessions, and small API
bugs. All data here is made up."""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import capcom.timeline
from capcom.config import load_config
from capcom.report import api
from capcom.report.analysis import Analysis
from capcom.report.calibration import run_calibrate
from capcom.report.story import regime_json, regime_line, story_line
from capcom.store.index import open_index
from capcom.timeline.regimes import REST, Contributor, RegimeChange
from capcom.types import (
    AgentInfo,
    CauseKind,
    Confidence,
    CostState,
    Evidence,
    Explanation,
    Finding,
    Index,
    ModelUsage,
    PriceTable,
    SessionInfo,
    Spike,
)
from tests.report.conftest import (
    ALPHA,
    BETA,
    FAKE,
    NOW,
    TABLE,
    Client,
    _log,
    _put,
    build_index,
    real_spikes,
    req,
    usd,
)
from tests.report.test_review import _client

GAMMA = "-home-user-gamma"


def _ctx(path: Path, analysis: Analysis = FAKE) -> api.Context:
    return api.Context(open_index(path, readonly=True), TABLE, analysis, NOW, tz=UTC)


@pytest.fixture(scope="module")
def regime(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A quiet session at $2 a day for two weeks, then from 7 days ago a large
    alpha session ($20 a day on its main thread, $30 in a coder subagent) and a beta
    one at $10 a day."""
    path = tmp_path_factory.mktemp("regime") / "index.sqlite"
    ix = open_index(path)
    at = NOW.replace(hour=10)
    quiet = [req("quiet", at - timedelta(days=d), BETA, output=100_000) for d in range(20, 7, -1)]
    big = [
        req("big00000x", at - timedelta(days=d), ALPHA, output=1_000_000) for d in range(7, 0, -1)
    ]
    coder = [
        req("big00000x", at - timedelta(days=d), ALPHA, "c1", output=1_500_000)
        for d in range(7, 0, -1)
    ]
    mid = [req("mid00000y", at - timedelta(days=d), BETA, output=500_000) for d in range(7, 0, -1)]
    _put(ix, _log(BETA, "quiet"), [SessionInfo("quiet", BETA, cwd="/home/user/beta"), *quiet])
    big_info = SessionInfo("big00000x", ALPHA, cwd="/home/user/alpha", title="Big work")
    _put(ix, _log(ALPHA, "big00000x"), [big_info, *big])
    _put(ix, _log(ALPHA, "big00000x", "c1"), [AgentInfo("c1", "big00000x", "coder"), *coder])
    _put(ix, _log(BETA, "mid00000y"), [SessionInfo("mid00000y", BETA, cwd="/home/user/beta"), *mid])
    ix.close()
    return path


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_index(tmp_path_factory.mktemp("plain") / "index.sqlite")


# --------------------------------------------------------------------------- story


def test_overview_regime(regime: Path) -> None:
    got = api.overview(_ctx(regime))["regime"]
    assert got is not None
    assert got["day"] == (NOW - timedelta(days=7)).date().isoformat()
    assert got["before_usd_per_day"] == pytest.approx(2.0)
    assert got["after_usd_per_day"] == pytest.approx(60.0)
    assert got["ratio"] == pytest.approx(30.0)
    first = got["sessions"][0]
    assert first["key"] == "big00000x" and first["project"] == "alpha"
    assert first["title"] == "Big work" and first["share"] == pytest.approx(50 / 58, abs=1e-3)
    assert got["sessions"][-1]["rest"] is True  # the quiet session's fall
    assert sum(s["rise_usd"] for s in got["sessions"]) == pytest.approx(58.0)
    assert got["agent_types"][0]["key"] == "coder"
    assert got["text"].startswith("Spend rose \u00d730.0 from Oct 14 ($2.00 to $60 a day): ")
    assert "alpha big00000 (86%)" in got["text"] and "coder subagents 52%" in got["text"]


def test_no_regime_without_a_change(plain: Path) -> None:
    assert api.overview(_ctx(plain))["regime"] is None


def test_spend_regime_follows_its_window(regime: Path) -> None:
    ctx = _ctx(regime)
    assert api.spend(ctx, {"days": "14"})["regime"]["day"] == "2026-10-14"
    # the 7-day window starts after the change: nothing changed inside it
    assert api.spend(ctx, {"days": "7"})["regime"] is None


def test_regime_unavailable_is_reported(regime: Path) -> None:
    def stub(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    analysis = Analysis(
        capcom.timeline.cumulative,
        capcom.timeline.per_step,
        real_spikes,
        FAKE.explain_fn,
        FAKE.checks_fn,
        regimes_fn=stub,
    )
    out = api.overview(_ctx(regime, analysis))
    assert out["regime"] is None and "timeline" in out["unavailable"]


def test_story_line_for_the_terminal(regime: Path) -> None:
    ix = open_index(regime, readonly=True)
    line = story_line(load_config(), 14, index=ix, now=NOW, tz=UTC)
    assert line == api.overview(_ctx(regime))["regime"]["text"]
    assert story_line(load_config(), 7, index=ix, now=NOW, tz=UTC) is None
    assert story_line(load_config(), 0, index=ix, now=NOW, tz=UTC) == line


def test_regime_line_wording() -> None:
    change = RegimeChange(
        date(2026, 10, 16),
        10.0,
        119.0,
        5,
        (
            Contributor("d3f68339aaaa", "A title", 56.7, 0.52, "improved"),
            Contributor("bcbbaac4bbbb", "Another", 38.2, 0.35, "toy-brain"),
            Contributor("ffff0000cccc", "Small", 3.3, 0.03, "x"),
            Contributor(REST, "Other", 10.8, 0.10),
        ),
        (Contributor("coder", "coder", 57.8, 0.53), Contributor("main", "main", 51.2, 0.47)),
    )
    assert regime_line(change) == (
        "Spend rose \u00d711.9 from Oct 16 ($10 to $119 a day): improved d3f68339 (52%) "
        "and toy-brain bcbbaac4 (35%); coder subagents 53%."
    )
    assert regime_json(change)["sessions"][3]["key"] is None  # the rest has no link
    assert regime_json(None) is None


# --------------------------------------------------------------------------- small bugs


def test_group_agent_type_is_agent(plain: Path) -> None:
    ctx = _ctx(plain)
    a = api.spend(ctx, {"days": "14", "group": "agent_type"})
    b = api.spend(ctx, {"days": "14", "group": "agent"})
    assert a["window"]["group"] == "agent" and a["cumulative"] == b["cumulative"]
    with pytest.raises(api.ApiError):
        api.spend(ctx, {"days": "14", "group": "nope"})


def test_session_counts_line_up(plain: Path) -> None:
    ctx = _ctx(plain)
    spend = api.spend(ctx, {"days": "14"})
    listed = api.sessions(ctx, {"days": "14"})
    assert spend["sessions_count"] + spend["temporary_sessions"] == listed["total"]
    assert listed["temporary"] == spend["temporary_sessions"] == 1


def test_session_times_are_iso_and_main_has_no_spawns(plain: Path) -> None:
    data = api.session(_ctx(plain), "s1")
    main = next(r for r in data["agent_types"] if r["agent_type"] == "main")
    assert main["spawns"] is None and main["median_tool_calls"] is None
    assert main["tool_calls"] == 3
    scout = next(r for r in data["agent_types"] if r["agent_type"] == "scout")
    assert scout["spawns"] == 1 and scout["median_tool_calls"] == 1
    for p in data["pauses"]:
        assert datetime.fromisoformat(p["start"]) < datetime.fromisoformat(p["end"])
    assert all(isinstance(c["t"], str) for c in data["compactions"])
    assert isinstance(data["main"]["first"], str) and isinstance(data["agents"][0]["last"], str)
    assert all(isinstance(t, int) for t in data["requests"]["t"])  # columns stay ms


# --------------------------------------------------------------------------- checks


def _findings(*args: Any) -> list[Finding]:
    return [
        Finding("long_running_agents", "Long agent", ALPHA, "s", 30.0, "a"),
        Finding("long_running_agents", "Long agent", BETA, "s", 10.0, "a"),
        Finding(
            "late_compactions", "Late", ALPHA, "s", 8.0, "a",
            metrics={"overlaps": ["long_running_agents"]},
        ),
        Finding("model_mix", "Sonnet", "coder", "s", 12.0, "a", metrics={"judgement_call": True}),
        Finding("skills_unused", "Skills", "all", "s", 0.05, "a"),
    ]  # fmt: skip


def _checks_analysis() -> Analysis:
    return Analysis(
        capcom.timeline.cumulative,
        capcom.timeline.per_step,
        real_spikes,
        FAKE.explain_fn,
        _findings,
    )


def test_checks_rollup_and_small(plain: Path) -> None:
    data = api.checks(_ctx(plain, _checks_analysis()), {"days": "14"})
    kinds = [r["kind"] for r in data["rollups"]]
    assert kinds == ["long_running_agents", "late_compactions", "model_mix"]  # judgement last
    long = data["rollups"][0]
    assert long["saving_usd_per_week"] == 40.0 and long["scope_noun"] == "project"
    assert [(s["scope_label"], s["share"]) for s in long["scopes"]] == [
        ("alpha", 0.75),
        ("beta", 0.25),
    ]
    assert long["findings"] == [1, 2] and long["counted"] == "all"
    late = data["rollups"][1]
    assert late["overlaps"] == ["long_running_agents"] and late["counted"] == "none"
    assert data["rollups"][2]["judgement_call"] is True
    # the finding under $0.10/week is a count, not a row
    assert all(f["kind"] != "skills_unused" for f in data["findings"])
    assert data["small"] == {"findings": 1, "usd_per_week": 0.05, "below_usd_per_week": 0.10}
    assert data["total"]["usd_per_week"] == 40.0
    assert data["total"]["judgement_calls"]["usd_per_week"] == 12.0
    assert "Judgement calls" in data["footer"] and "under $0.10/week" in data["footer"]


# --------------------------------------------------------------------------- spikes


def _two_causes(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
    out = []
    for i, sp in enumerate(spikes):
        cause = CauseKind.TOOL_WAIT_EXPIRY if i % 2 == 0 else CauseKind.PARALLEL_SUBAGENTS
        ev = (Evidence("A gap", sp.session_id, sp.agent_id, None),)
        out.append(Explanation(sp, cause, Confidence.LIKELY, sp.excess_usd, "T", evidence=ev))
    return out


@pytest.fixture(scope="module")
def spikes_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Three sessions, each with one large cache write among cheap turns."""
    path = tmp_path_factory.mktemp("spk") / "index.sqlite"
    ix = open_index(path)
    for k, (sid, size) in enumerate((("sa", 400_000), ("sb", 900_000), ("sc", 600_000))):
        start = NOW - timedelta(hours=10 - 3 * k)
        reqs = [req(sid, start + timedelta(minutes=i), ALPHA, output=100) for i in range(30)]
        reqs.append(req(sid, start + timedelta(minutes=30), ALPHA, cache_write_1h=size))
        reqs += [req(sid, start + timedelta(minutes=31 + i), ALPHA, output=100) for i in range(12)]
        _put(ix, _log(ALPHA, sid), [SessionInfo(sid, ALPHA, cwd="/home/user/alpha"), *reqs])
    ix.close()
    return path


def test_spikes_times_causes_and_largest(spikes_index: Path) -> None:
    analysis = Analysis(
        capcom.timeline.cumulative,
        capcom.timeline.per_step,
        real_spikes,
        _two_causes,
        FAKE.checks_fn,
    )
    ctx = _ctx(spikes_index, analysis)
    data = api.spikes(ctx, {"days": "1"})
    assert data["count"] >= 2
    items = data["spikes"]
    assert all(isinstance(it["start"], str) and isinstance(it["end"], str) for it in items)
    for it in items:  # the detail window is ISO too
        win = it["window"]
        assert datetime.fromisoformat(win["start"]) <= datetime.fromisoformat(win["spike_start"])
    big = max(items, key=lambda it: it["excess_usd"])
    assert data["largest"] == big["key"]
    colors = {c["cause"]: c["color"] for c in data["causes"]}
    assert colors["tool_wait_expiry"] == "var(--s1)" and colors["parallel_subagents"] == "var(--s2)"
    labels = {c["cause"]: c["label"] for c in data["causes"]}
    assert labels["parallel_subagents"] != labels["tool_wait_expiry"]
    one = api.spikes(ctx, {"days": "1", "cause": "parallel_subagents"})
    assert 0 < one["count"] < data["count"] and one["cause"] == "parallel_subagents"
    assert all(it["cause"] == "parallel_subagents" for it in one["spikes"])
    assert len(one["causes"]) == len(data["causes"])  # the chips stay
    with pytest.raises(api.ApiError):
        api.spikes(ctx, {"days": "1", "cause": "nope"})
    marks = api.spend(ctx, {"days": "7"})["spikes"]["marks"]
    assert all(isinstance(m["t"], str) for m in marks)


# --------------------------------------------------------------------------- calibration


@pytest.fixture(scope="module")
def resumed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A session run in two processes; the last cost-state counts the second only."""
    path = tmp_path_factory.mktemp("resumed") / "index.sqlite"
    ix = open_index(path)
    first = [req("rs", NOW - timedelta(days=2, hours=h), GAMMA, output=50_000) for h in (3, 2)]
    second = [req("rs", NOW - timedelta(hours=h), GAMMA, output=100_000) for h in (3, 2, 1)]
    p1 = int((NOW - timedelta(days=2, hours=4)).timestamp() * 1000)
    p2 = int((NOW - timedelta(hours=4)).timestamp() * 1000)
    m = {"claude-opus-5-5": ModelUsage(cost_usd=usd(second))}
    cs1 = CostState("rs", 50, usd(first), m, p1, None, first[-1].timestamp, second[0].timestamp)
    cs2 = CostState("rs", 99, usd(second), m, p2, None, second[-1].timestamp, None)
    info = SessionInfo("rs", GAMMA, cwd="/home/user/gamma", title="Two runs")
    _put(ix, _log(GAMMA, "rs"), [info, *first, cs1, *second, cs2])
    ix.close()
    return path


def test_session_page_explains_a_resumed_session(resumed: Path) -> None:
    data = api.session(_ctx(resumed), "rs")
    cal = data["calibration"]
    assert cal["part"]["process"] == 2 and cal["part"]["processes"] == 2
    assert cal["part"]["whole_session"] is False
    assert "process 2 of 2" in cal["part"]["text"]
    assert cal["session_usd"] == pytest.approx(data["totals"]["usd"])
    assert cal["capcom_usd"] < cal["session_usd"]
    assert "process 2 of 2" in cal["note"] and "whole session" in cal["note"]


def test_whole_session_has_no_note(plain: Path) -> None:
    cal = api.session(_ctx(plain), "s1")["calibration"]
    assert cal["part"]["whole_session"] is True and cal["note"] is None


def test_calibrate_cli_rows_and_filters(resumed: Path, plain: Path) -> None:
    out = io.StringIO()
    ix = open_index(resumed, readonly=True)
    assert run_calibrate(load_config(), 20, index=ix, out=out, now=NOW, tz=UTC) == 0
    text = out.getvalue()
    head = text.splitlines()[0].split()
    assert head[:3] == ["session", "project", "title"]
    row = text.splitlines()[2]
    assert row.startswith("rs") and "gamma" in row and "process 2 of 2" in row
    assert "1 of these were resumed" in text
    out = io.StringIO()
    run_calibrate(load_config(), 20, project="nope", index=ix, out=out, now=NOW, tz=UTC)
    assert "no session" in out.getvalue()
    out = io.StringIO()
    late = NOW + timedelta(days=30)
    run_calibrate(load_config(), 20, days=7, index=ix, out=out, now=late, tz=UTC)
    assert "no session" in out.getvalue()
    out = io.StringIO()
    run_calibrate(load_config(), 20, project="gamma", index=ix, out=out, now=NOW, tz=UTC)
    assert "process 2 of 2" in out.getvalue()


def test_requests_after_the_last_cost_state(tmp_path: Path) -> None:
    """A session that ran on in a process that has not written a cost-state yet: the
    compared part says so, and --days counts the session's own last record."""
    path = tmp_path / "index.sqlite"
    ix = open_index(path)
    early = [req("on", NOW - timedelta(days=12, hours=h), GAMMA, output=50_000) for h in (3, 2)]
    late = [req("on", NOW - timedelta(hours=1), GAMMA, output=80_000)]
    start = int((NOW - timedelta(days=12, hours=4)).timestamp() * 1000)
    m = {"claude-opus-5-5": ModelUsage(cost_usd=usd(early))}
    cs = CostState("on", 50, usd(early), m, start, None, early[-1].timestamp, late[0].timestamp)
    _put(
        ix,
        _log(GAMMA, "on"),
        [
            SessionInfo("on", GAMMA, cwd="/home/user/gamma", last_seen=late[0].timestamp),
            *early,
            cs,
            *late,
        ],
    )
    ix.close()
    cal = api.session(_ctx(path), "on")["calibration"]
    assert cal["part"]["whole_session"] is False and cal["part"]["process"] == 1
    assert cal["part"]["text"].endswith("one process, later requests not in it")
    assert cal["note"] is not None and "one process" in cal["note"]
    out = io.StringIO()
    run_calibrate(
        load_config(), 20, days=7, index=open_index(path, readonly=True), out=out, now=NOW, tz=UTC
    )
    assert "later requests not in it" in out.getvalue()


@pytest.fixture
def cause_client(spikes_index: Path) -> Iterator[Client]:
    analysis = Analysis(
        capcom.timeline.cumulative,
        capcom.timeline.per_step,
        real_spikes,
        _two_causes,
        FAKE.checks_fn,
    )
    yield from _client(spikes_index, analysis)


def test_cause_filter_is_in_the_cache_key(cause_client: Client) -> None:
    """Two causes through the server, one after the other: two different replies."""
    _, a, _ = cause_client.get("/api/spikes?days=1&cause=tool_wait_expiry")
    _, b, _ = cause_client.get("/api/spikes?days=1&cause=parallel_subagents")
    assert a["cause"] == "tool_wait_expiry" and b["cause"] == "parallel_subagents"
    assert {s["key"] for s in a["spikes"]} != {s["key"] for s in b["spikes"]}
