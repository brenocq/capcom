"""The Spikes and Checks pages' endpoints, spike causes on the other pages, the
scanning flag, the thinning cap and the quiet background scan. Synthetic data
and fake analysis modules only."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import capcom.timeline
from capcom.explain import fmt
from capcom.report import api
from capcom.report.analysis import Analysis
from capcom.report.server import FailuresOnly, Scanner
from capcom.store.index import open_index
from capcom.types import (
    CauseKind,
    Confidence,
    Evidence,
    Explanation,
    Finding,
    Index,
    PriceTable,
    SessionInfo,
    Spike,
    SpikeKind,
)
from tests.report.conftest import (
    ALPHA,
    BETA,
    NOW,
    _log,
    _put,
    fake_checks,
    not_yet,
    real_spikes,
    req,
    usd,
)
from tests.report.test_review import _client


def explain_all(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
    """Two causes per spike: most of it a cache rebuild, a small rest unexplained."""
    out = []
    for sp in spikes:
        out.append(
            Explanation(
                sp,
                CauseKind.IDLE_REWRITE,
                Confidence.CLEAR,
                sp.excess_usd * 0.8,
                "Cache rebuilt after 74 minutes idle",
                evidence=(Evidence("Gap of 74 min", sp.session_id, sp.agent_id, sp.start),),
                advice="Compact before a long break.",
                summary="The next turn rewrote it all.",
            )
        )
        out.append(
            Explanation(sp, CauseKind.UNEXPLAINED, Confidence.UNEXPLAINED, sp.excess_usd * 0.1, "?")
        )
    return out


def _analysis(explain: Any = explain_all, checks: Any = fake_checks) -> Analysis:
    return Analysis(
        capcom.timeline.cumulative, capcom.timeline.per_step, real_spikes, explain, checks
    )


@pytest.fixture(scope="module")
def spiky(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One main thread of cheap turns with one cache rewrite of a large context, a
    scout subagent with one tool call, a coder with five, and a late compaction."""
    from capcom.types import AgentInfo, Event, EventKind

    path = tmp_path_factory.mktemp("pages") / "index.sqlite"
    start = NOW - timedelta(hours=3)
    reqs = [req("sp", start + timedelta(minutes=i), ALPHA, output=100) for i in range(30)]
    reqs.append(req("sp", start + timedelta(minutes=30), ALPHA, cache_write_1h=900_000))
    reqs += [req("sp", start + timedelta(minutes=31 + i), ALPHA, output=100) for i in range(12)]
    scout = [req("sp", start + timedelta(minutes=5), ALPHA, "a1", tools=["Grep"], output=10)]
    coder = [
        req("sp", start + timedelta(minutes=6 + i), ALPHA, "a2", tools=["Bash"], output=10)
        for i in range(5)
    ]
    comp = Event(
        EventKind.COMPACTION, "sp", None, start + timedelta(minutes=40), "c1",
        {"trigger": "auto", "pre_tokens": 650_000, "post_tokens": 30_000},
    )  # fmt: skip
    ix = open_index(path)
    info = SessionInfo("sp", ALPHA, cwd="/a", title="Spiky")
    _put(ix, _log(ALPHA, "sp"), [info, *reqs, comp])
    _put(ix, _log(ALPHA, "sp", "a1"), [AgentInfo("a1", "sp", agent_type="scout"), *scout])
    _put(ix, _log(ALPHA, "sp", "a2"), [AgentInfo("a2", "sp", agent_type="coder"), *coder])
    ix.close()
    return path


# --------------------------------------------------------------------------- spikes


def test_spikes_with_causes(spiky: Path) -> None:
    for client in _client(spiky, _analysis()):
        status, data, _ = client.get("/api/spikes?days=7")
    assert status == 200
    assert data["count"] == 1 and data["explained"] == 1
    (item,) = data["spikes"]
    assert item["n"] == 1 and item["kind"] == "single_request" and item["scope"] == "thread"
    assert item["title"] == "Spiky" and item["session_id"] == "sp" and item["agent_id"] is None
    assert item["cause"] == "idle_rewrite" and item["cause_label"] == "Idle past cache lifetime"
    assert item["headline"] == "Cache rebuilt after 74 minutes idle"
    first = item["explanations"][0]
    assert first["confidence_label"] == "clear cause" and first["summary"]
    assert first["evidence"][0]["label"] == "Gap of 74 min"
    causes = [c["cause"] for c in data["causes"]]
    assert causes == ["idle_rewrite", "unexplained"]  # unexplained last
    assert data["causes"][0]["excess_usd"] == pytest.approx(0.8 * data["excess_usd"], abs=1e-5)
    # the window: extra adds up to the excess, and lies in the spike's bar only
    win = item["window"]
    assert len(win["usual"]) == len(win["extra"]) <= api.WINDOW_BARS + 1
    assert sum(win["extra"]) == pytest.approx(item["excess_usd"], abs=1e-4)
    gap = datetime.fromisoformat(win["spike_start"]) - datetime.fromisoformat(win["start"])
    k = int(gap.total_seconds()) // win["step_s"]
    assert [i for i, x in enumerate(win["extra"]) if x > 0] == [k]
    assert sum(win["usual"]) > 0  # the cheap turns around it


def test_spikes_unavailable_until_explain_lands(spiky: Path) -> None:
    for client in _client(spiky, _analysis(explain=not_yet)):
        status, data, _ = client.get("/api/spikes")
        overview = client.get("/api/overview")[1]
    assert status == 501 and data["module"] == "explain" and data["milestone"] == 3
    # the markers stay, without causes, and say why
    assert "explain" in overview["unavailable"]
    marks = overview["spikes_today"]
    assert marks["explained"] is False and marks["marks"][0]["headline"] is None


def test_markers_and_session_carry_the_cause(spiky: Path) -> None:
    for client in _client(spiky, _analysis()):
        spend = client.get("/api/spend?days=7")[1]
        overview = client.get("/api/overview")[1]
        session = client.get("/api/session/sp")[1]
        keys = {s["key"] for s in client.get("/api/spikes?days=7")[1]["spikes"]}
    for marks in (spend["spikes"], overview["spikes_today"]):
        (mark,) = marks["marks"]
        assert marks["explained"] is True and mark["key"] in keys
        assert mark["cause_label"] == "Idle past cache lifetime"
        assert mark["headline"] == "Cache rebuilt after 74 minutes idle"
    assert overview["unavailable"] == []
    sp = session["spikes"]
    assert sp["count"] == 1 and sp["explained"] is True
    assert sp["items"][0]["key"] in keys
    assert sp["items"][0]["explanations"][0]["advice"] == "Compact before a long break."


def test_session_spikes_without_explain(spiky: Path) -> None:
    for client in _client(spiky, _analysis(explain=not_yet)):
        sp = client.get("/api/session/sp")[1]["spikes"]
    assert sp["explained"] is False and sp["items"][0]["explanations"] is None
    assert sp["items"][0]["kind"] == "single_request"


def test_session_scope_spike_window_counts_every_thread(spiky: Path) -> None:
    with open_index(spiky, readonly=True) as ix:
        reqs = list(ix.requests(session_id="sp"))
    coder = [r for r in reqs if r.agent_id == "a2"]
    ids = tuple(r.message_id for r in coder)
    cost = usd(coder)
    sp = Spike(
        SpikeKind.SLOPE_CHANGE, coder[0].timestamp, coder[-1].timestamp,
        cost_usd=cost, baseline_usd=cost / 4, session_id="sp", message_ids=ids, scope="session",
    )  # fmt: skip

    def found(*args: Any) -> list[Spike]:
        return [sp]

    analysis = Analysis(
        capcom.timeline.cumulative, capcom.timeline.per_step, found, explain_all, fake_checks
    )
    for client in _client(spiky, analysis):
        (item,) = client.get("/api/spikes")[1]["spikes"]
    assert item["scope"] == "session"
    win = item["window"]
    assert sum(win["extra"]) == pytest.approx(0.75 * cost, rel=1e-3)
    # usual pace: the spike at its baseline, plus the main thread and the scout
    # inside the window
    lo = datetime.fromisoformat(win["start"]).timestamp() * 1000
    hi = lo + len(win["usual"]) * win["step_s"] * 1000
    main_and_scout = [
        r for r in reqs if r.agent_id != "a2" and lo <= r.timestamp.timestamp() * 1000 < hi
    ]
    assert main_and_scout
    assert sum(win["usual"]) == pytest.approx(cost / 4 + usd(main_and_scout), rel=1e-3)


def test_linked_spike_is_listed_beyond_the_largest(spiky: Path) -> None:
    """`?spike=<key>` lists that spike even when 40 larger ones push it out."""
    with open_index(spiky, readonly=True) as ix:
        main = [r for r in ix.requests(session_id="sp") if r.agent_id is None]
    many = [
        Spike(
            SpikeKind.SINGLE_REQUEST,
            r.timestamp,
            r.timestamp,
            cost_usd=1.0 + i,
            baseline_usd=0.5,
            session_id="sp",
            message_ids=(r.message_id,),
        )
        for i, r in enumerate(main[: api.MAX_SPIKES + 2])
    ]
    small = many[0]  # the smallest excess
    analysis = Analysis(
        capcom.timeline.cumulative, capcom.timeline.per_step, lambda *a: many, explain_all,
        fake_checks,
    )  # fmt: skip
    key = api.spike_key(small)
    for client in _client(spiky, analysis):
        plain = client.get("/api/spikes")[1]["spikes"]
        linked = client.get(f"/api/spikes?spike={key}")[1]["spikes"]
    assert len(plain) == api.MAX_SPIKES and key not in {s["key"] for s in plain}
    assert len(linked) == api.MAX_SPIKES + 1 and key in {s["key"] for s in linked}


def test_cause_labels_match_the_cli(spiky: Path) -> None:
    for kind, label in fmt.CAUSE_LABELS.items():
        assert api.CAUSE_LABELS[kind.value] == label
    for conf, label in fmt.CONFIDENCE_LABELS.items():
        assert api.CONFIDENCE_LABELS[conf.value] == label


# --------------------------------------------------------------------------- checks


def finding(kind: str, usd: float, scope: str = "all", **metrics: Any) -> Finding:
    return Finding(
        kind, f"Title {kind}", scope, "summary", usd, "advice", change='{"a": 1}',
        evidence=(Evidence("one fact", "sp", None, NOW),), metrics=metrics,
    )  # fmt: skip


FINDINGS = [
    finding("late_compactions", 30.0, ALPHA, threshold=400_000, overlaps=("long_running_agents",)),
    finding(
        "long_running_agents",
        20.0,
        max_context=650_000,
        threshold=200_000,
        overlaps=("late_compactions",),
    ),
    finding("one_command_subagents", 10.0, "scout", subagents=1, one_command=1, overlaps=()),
    finding("mcp_servers_unused", 5.0, tools_loaded=12, tools_called=3, overlaps=()),
    finding("model_switches", 1.0, rewrites=2),
]


def test_checks_page_data(spiky: Path) -> None:
    for client in _client(spiky, _analysis(checks=lambda *a: FINDINGS)):
        status, data, _ = client.get("/api/checks?days=7")
    assert status == 200
    rows = data["findings"]
    assert [r["n"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["saving_usd_per_week"] for r in rows] == [30.0, 20.0, 10.0, 5.0, 1.0]
    assert data["overlapping"] is True
    # never sum overlapping kinds: the second one shares dollars with the first
    assert [r["counted"] for r in rows] == [True, False, True, True, True]
    assert data["total"] == {
        "usd_per_week": 46.0,
        "findings": 4,
        "of": 5,
        "all": False,
        "text": "up to $46.00/week across 4 findings that don't overlap"
        " (of 5; overlapping savings are counted once)",
    }
    assert rows[0]["overlaps"] == ["long_running_agents"]
    # cwd "/a" does not explain the folder -home-user-alpha: the folder name, as in `capcom top`
    assert rows[0]["scope_label"] == ALPHA
    assert rows[2]["scope_label"] == "scout"  # an agent type stays as it is
    assert rows[0]["change"] == '{"a": 1}' and rows[0]["evidence"][0]["label"] == "one fact"
    visual = {r["kind"]: r["visual"] for r in rows}
    assert visual["late_compactions"] == {
        "type": "dots", "label": "Context size when each compaction started",
        "values": [650_000], "threshold": 400_000,
    }  # fmt: skip
    hist = {b["label"]: b["count"] for b in visual["one_command_subagents"]["buckets"]}
    assert hist["1"] == 1 and sum(hist.values()) == 1  # the scout only, not the coder
    assert visual["mcp_servers_unused"]["type"] == "squares"
    assert (
        visual["mcp_servers_unused"]["total"] == 12 and visual["mcp_servers_unused"]["filled"] == 3
    )
    assert visual["long_running_agents"]["type"] == "gauge"
    assert visual["model_switches"] is None


def test_checks_empty_and_unavailable(spiky: Path) -> None:
    for client in _client(spiky, _analysis(checks=lambda *a: [])):
        data = client.get("/api/checks")[1]
    assert data["findings"] == [] and data["total"] is None and data["overlapping"] is False
    for client in _client(spiky, _analysis(checks=not_yet)):
        status, data, _ = client.get("/api/checks")
    assert status == 501 and data["milestone"] == 4


def test_checks_total_when_nothing_overlaps(spiky: Path) -> None:
    found = [finding("mcp_servers_unused", 5.0), finding("model_switches", 2.5)]
    for client in _client(spiky, _analysis(checks=lambda *a: found)):
        data = client.get("/api/checks")[1]
    assert data["total"] == {
        "usd_per_week": 7.5,
        "findings": 2,
        "of": 2,
        "all": True,
        "text": "up to $7.50/week across 2 findings that don't overlap",
    }
    assert data["overlapping"] is False


def test_overlap_needs_the_same_project(tmp_path: Path) -> None:
    """Idle rewrites in project A still count when only project B has long-running
    agents; in the same project, or against "all", they overlap."""
    ix = open_index(tmp_path / "index.sqlite")
    for proj, sid in ((ALPHA, "sa"), (BETA, "sb")):
        info = SessionInfo(sid, proj, cwd="/" + sid, title=sid)
        _put(ix, _log(proj, sid), [info, req(sid, NOW - timedelta(hours=1), proj, output=10)])
    ix.close()
    cases = [
        (BETA, [True, True]),
        (ALPHA, [True, False]),
        ("all", [True, False]),
    ]
    for scope, want in cases:
        found = [
            finding("long_running_agents", 20.0, scope, overlaps=("idle_rewrites",)),
            finding("idle_rewrites", 10.0, ALPHA, overlaps=("long_running_agents",)),
        ]
        for client in _client(tmp_path / "index.sqlite", _analysis(checks=lambda *a, f=found: f)):
            data = client.get("/api/checks")[1]
        assert [r["counted"] for r in data["findings"]] == want, scope


# --------------------------------------------------------------------------- scanning


def test_status_says_when_the_first_scan_runs(spiky: Path, tmp_path: Path) -> None:
    release = threading.Event()
    scanner = Scanner(lambda: release.wait(10), interval=60)
    scanner.start()
    try:
        for client in _client(spiky, scanner=scanner):
            assert client.get("/api/status")[1]["scanning"] is True
            release.set()
            deadline = time.monotonic() + 5
            while scanner.scans < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.get("/api/status")[1]["scanning"] is False
    finally:
        release.set()
        scanner.stop(timeout=5)
    for client in _client(spiky):
        assert client.get("/api/status")[1]["scanning"] is False  # no scanner at all


def test_no_index_api_while_scanning(tmp_path: Path) -> None:
    release = threading.Event()
    scanner = Scanner(lambda: release.wait(10))
    scanner.start()
    try:
        for client in _client(tmp_path / "missing.sqlite", scanner=scanner):
            status, data, _ = client.get("/api/overview")
    finally:
        release.set()
        scanner.stop(timeout=5)
    assert status == 503 and data["error"] == "no_index" and data["scanning"] is True
    assert "reading the logs" in data["message"]
    for client in _client(tmp_path / "missing.sqlite"):
        status, data, _ = client.get("/api/overview")
    assert data["scanning"] is False and "capcom scan" in data["message"]


def test_background_scan_prints_only_failures() -> None:
    out = io.StringIO()
    stream = FailuresOnly(out)
    stream.write("capcom scan: 10/20 files, 1.0 MB, 5 records")
    stream.write("\ncapcom scan: failed on /logs/x.jsonl: ValueError: bad\n")
    stream.write("capcom scan: 20/20 files, 2.0 MB, 9 records, 1 failed\n")
    assert out.getvalue() == "capcom serve: failed on /logs/x.jsonl: ValueError: bad\n"


# --------------------------------------------------------------------------- thinning


def test_thin_is_capped_whatever_the_threads() -> None:
    n, threads_n = 100_000, 30_000
    threads = [-1 if i % 4 == 0 else i % threads_n for i in range(n)]
    usd_col = [(i * 7919 % 1000) / 1000 for i in range(n)]
    context = [(i * 104729 % 50_000) for i in range(n)]
    usd_col[54_321] = 99.0
    kept = api.thin(threads, usd_col, context, set())
    assert len(kept) <= 4_000 + 10
    assert kept == sorted(set(kept)) and {0, n - 1, 54_321} <= set(kept)
    main = [i for i in kept if threads[i] == -1]
    assert len(main) > 800  # the main thread keeps its own runs
    # many spike requests are thinned too, with a finer run
    spikes = set(range(0, n, 3))
    kept = api.thin(threads, usd_col, context, spikes)
    assert len(kept) <= 4_000 + 10
    assert len(spikes & set(kept)) >= 1_500


def test_session_wide_spike_does_not_break_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session-scope spike of 2,000 requests keeps only its ends in thinning."""
    monkeypatch.setattr(api, "MAX_POINTS", 500)
    start = NOW - timedelta(hours=10)
    reqs = [
        req(
            "big",
            start + timedelta(seconds=10 * i),
            ALPHA,
            f"a{i % 50}" if i % 3 else None,
            output=100 + i % 7,
        )
        for i in range(3_000)
    ]
    ix = open_index(tmp_path / "index.sqlite")
    _put(ix, _log(ALPHA, "big"), [SessionInfo("big", ALPHA, cwd="/a", title="Big"), *reqs])
    ix.close()
    wide = reqs[500:2_500]
    sp = Spike(
        SpikeKind.SLOPE_CHANGE, wide[0].timestamp, wide[-1].timestamp, cost_usd=5.0,
        baseline_usd=1.0, session_id="big", message_ids=tuple(r.message_id for r in wide),
        scope="session",
    )  # fmt: skip
    assert api._thin_keep_ids([sp]) == {wide[0].message_id, wide[-1].message_id}

    def found(*args: Any) -> list[Spike]:
        return [sp]

    analysis = Analysis(
        capcom.timeline.cumulative, capcom.timeline.per_step, found, explain_all, fake_checks
    )
    for client in _client(tmp_path / "index.sqlite", analysis):
        data = client.get("/api/session/big")[1]
        marks = client.get("/api/spend?days=7")[1]["spikes"]["marks"]
    assert data["thinned"]["spikes"] == 2
    assert data["thinned"]["kept"] <= 500 + 10
    assert data["spikes"]["items"][0]["scope"] == "session"
    assert marks[0]["scope"] == "session" and marks[0]["agent_id"] is None


@pytest.fixture(scope="module")
def two_projects(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One session in alpha and one in beta, each with a few cheap turns."""
    path = tmp_path_factory.mktemp("projects") / "index.sqlite"
    start = NOW - timedelta(hours=2)
    ix = open_index(path)
    for sid, project, cwd in (("pa", ALPHA, "/home/user/alpha"), ("pb", BETA, "/home/user/beta")):
        reqs = [req(sid, start + timedelta(minutes=i), project, output=100) for i in range(6)]
        _put(ix, _log(project, sid), [SessionInfo(sid, project, cwd=cwd, title=sid), *reqs])
    ix.close()
    return path


def _project_spikes(index: Index, table: PriceTable, *rest: Any) -> list[Spike]:
    reqs = list(index.requests())
    out = []
    for sid, extra in (("pa", 3.0), ("pa", 1.0), ("pb", 2.0)):
        r = next(
            r
            for r in reqs
            if r.session_id == sid and not any(r.message_id in s.message_ids for s in out)
        )
        out.append(
            Spike(
                SpikeKind.SINGLE_REQUEST, r.timestamp, r.timestamp, cost_usd=extra + 0.5,
                baseline_usd=0.5, session_id=sid, message_ids=(r.message_id,),
            )
        )  # fmt: skip
    return out


def test_spikes_by_project_and_filter(two_projects: Path) -> None:
    """Every spike names its project; `by_project` adds their excess per project,
    largest first; `?project=` keeps one project's spikes, causes and totals."""
    analysis = Analysis(
        capcom.timeline.cumulative, capcom.timeline.per_step, _project_spikes, explain_all,
        fake_checks,
    )  # fmt: skip
    for client in _client(two_projects, analysis):
        _, full, _ = client.get("/api/spikes?days=7")
        _, alpha, _ = client.get("/api/spikes?days=7&project=alpha")
        _, by_path, _ = client.get("/api/spikes?days=7&project=/home/user/beta")
        bad, err, _ = client.get("/api/spikes?days=7&project=gamma")
    assert {(it["session_id"], it["project"], it["project_path"]) for it in full["spikes"]} == {
        ("pa", "alpha", "/home/user/alpha"),
        ("pb", "beta", "/home/user/beta"),
    }
    rows = full["by_project"]
    assert [(r["project"], r["spikes"]) for r in rows] == [("alpha", 2), ("beta", 1)]
    assert rows[0]["project_path"] == "/home/user/alpha"
    assert rows[0]["excess_usd"] == pytest.approx(4.0)
    assert sum(r["excess_usd"] for r in rows) == pytest.approx(full["excess_usd"])
    assert full["project"] is None and full["count"] == 3

    assert alpha["project"] == "alpha" and alpha["project_path"] == "/home/user/alpha"
    assert alpha["count"] == 2 and alpha["excess_usd"] == pytest.approx(4.0)
    assert {it["project"] for it in alpha["spikes"]} == {"alpha"}
    idle = {c["cause"]: c["excess_usd"] for c in alpha["causes"]}["idle_rewrite"]
    assert idle == pytest.approx(0.8 * 4.0)
    assert alpha["by_project"] == full["by_project"]  # the bars to switch project stay

    assert by_path["project"] == "beta" and by_path["count"] == 1
    assert by_path["excess_usd"] == pytest.approx(2.0)
    assert bad == 400 and err["error"] == "bad_query"


def test_spike_markers_name_their_project(spiky: Path) -> None:
    for client in _client(spiky, _analysis()):
        _, data, _ = client.get("/api/spend?days=7")
    (mark,) = data["spikes"]["marks"]
    assert mark["project"] == ALPHA and mark["title"] == "Spiky"


def _explain_split(named: float, rest: float) -> Any:
    """A spike `named` parts a cache rebuild and `rest` parts unexplained."""

    def explain(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
        out = []
        for sp in spikes:
            if named:
                out.append(
                    Explanation(
                        sp, CauseKind.IDLE_REWRITE, Confidence.CLEAR, sp.excess_usd * named, "Idle"
                    )
                )
            out.append(
                Explanation(
                    sp,
                    CauseKind.UNEXPLAINED,
                    Confidence.UNEXPLAINED,
                    sp.excess_usd * rest,
                    "The rest of the spike: no rule fits",
                )
            )
        return out

    return explain


@pytest.mark.parametrize(
    ("named", "rest", "headline", "cause", "mostly"),
    [
        (0.3, 0.7, "Idle", "idle_rewrite", True),  # a larger rest never heads the row
        (0.6, 0.4, "Idle", "idle_rewrite", False),
        (0.0, 1.0, "No rule fits", "unexplained", True),
    ],
)
def test_headline_is_the_largest_named_cause(
    spiky: Path, named: float, rest: float, headline: str, cause: str, mostly: bool
) -> None:
    for client in _client(spiky, _analysis(explain=_explain_split(named, rest))):
        (item,) = client.get("/api/spikes?days=7")[1]["spikes"]
        (mark,) = client.get("/api/overview")[1]["spikes_today"]["marks"]
    for row in (item, mark):
        assert row["headline"] == headline and row["cause"] == cause
        assert row["mostly_unexplained"] is mostly
        assert row["unexplained_usd"] == pytest.approx(rest * row["excess_usd"], abs=1e-5)
