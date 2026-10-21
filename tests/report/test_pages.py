"""The Spikes and Checks pages' endpoints, spike causes on the other pages, the
scanning flag, the thinning cap and the quiet background scan. Synthetic data
and fake analysis modules only."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import capcom.timeline
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
    k = (win["spike_start"] - win["start"]) // (win["step_s"] * 1000)
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
    start = NOW - timedelta(hours=3)
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
    # usual pace counts the main thread and the scout around it too
    main_and_scout = [r for r in reqs if r.agent_id != "a2" and r.timestamp >= start]
    assert main_and_scout and sum(win["usual"]) > 0


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
    assert data["total"] == {"usd_per_week": 46.0, "findings": 4, "all": False}
    assert rows[0]["overlaps"] == ["long_running_agents"]
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
    assert data["total"] == {"usd_per_week": 7.5, "findings": 2, "all": True}
    assert data["overlapping"] is False


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
