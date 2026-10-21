"""The website's edge cases: sorting with zero values, placeholder-only sessions,
errors that leak nothing, temporary folders, daylight-saving days, the index's
update time, spike markers, thinning and calibration labels. Synthetic data only."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from capcom.report import api
from capcom.report.analysis import Analysis
from capcom.report.calibration import REASON_LABELS
from capcom.report.server import STATIC, Scanner, Site
from capcom.report.tally import project_display, project_names
from capcom.store.index import open_index
from capcom.types import SessionInfo
from tests.report.conftest import (
    FAKE,
    NOW,
    TABLE,
    Client,
    _log,
    _put,
    fake_checks,
    fake_explain,
    real_spikes,
    req,
    serve,
    usd,
)

ALPHA = "-home-user-alpha"
UUID = "0f0e0d0c-0b0a-4908-8706-050403020100"
SCRATCH = f"-tmp-claude-1000-{ALPHA}-{UUID}-scratchpad"


def _client(path: Path, analysis: Analysis = FAKE, **kw: Any) -> Iterator[Client]:
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("tz", UTC)
    yield from serve(Site(path, TABLE, analysis, **kw))


# --------------------------------------------------------------------------- sorting


@pytest.fixture(scope="module")
def zero_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Client]:
    """A paid session and a $0 one with no subagents, no active time and no context;
    plus one whose replies are all placeholders."""
    path = tmp_path_factory.mktemp("zero") / "index.sqlite"
    ix = open_index(path)
    paid = [
        req("paid", NOW - timedelta(hours=2), ALPHA, input=1_000, output=500),
        req("paid", NOW - timedelta(hours=2) + timedelta(minutes=1), ALPHA, "a1", output=100),
    ]
    free = [req("free", NOW - timedelta(hours=1), ALPHA)]  # no tokens: $0, context 0
    synth = [req("synth", NOW - timedelta(hours=3), ALPHA, model="<synthetic>", output=10)]
    for sid, reqs in (("paid", paid), ("free", free), ("synth", synth)):
        info = SessionInfo(sid, ALPHA, cwd="/home/user/alpha", first_seen=reqs[0].timestamp)
        _put(ix, _log(ALPHA, sid), [info, *reqs])
    ix.close()
    yield from _client(path)


@pytest.mark.parametrize(
    "sort", ["usd", "last", "peak_context", "active_seconds", "subagents", "requests"]
)
def test_sessions_sort_with_zero_rows(zero_client: Client, sort: str) -> None:
    status, data, _ = zero_client.get(f"/api/sessions?days=0&sort={sort}")
    assert status == 200
    ids = [r["session_id"] for r in data["sessions"]]
    assert set(ids) == {"paid", "free"}  # the placeholder-only session has no row
    values = [r[sort] for r in data["sessions"]]
    assert values == sorted(values, reverse=True)
    if sort != "last":
        assert ids[0] == "paid"


def test_placeholder_only_session(zero_client: Client) -> None:
    status, data, _ = zero_client.get("/api/session/synth")
    assert status == 200
    assert data["active_seconds"] == 0
    assert data["totals"]["requests"] == 0
    assert data["requests"]["t"] == [] and data["thinned"] is None


# --------------------------------------------------------------------------- server errors


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("secret detail /home/user/alpha")


def test_unexpected_error_is_a_plain_500(
    index_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = Analysis(_boom, _boom, _boom, fake_explain, fake_checks)
    for client in _client(index_path, broken):
        status, body, headers = client.get("/api/overview")
        assert status == 500
        assert body == b"internal error"
        assert headers["Content-Type"] == "text/plain"
        assert client.get("/api/status")[0] == 200  # the server keeps answering
    assert "secret detail" in capsys.readouterr().err  # the terminal gets the traceback


@pytest.mark.parametrize("path", ["/static/%00.js", "/static/" + "a" * 300 + ".js"])
def test_bad_static_names(client: Client, path: str) -> None:
    status, body, _ = client.get(path)
    assert status == 404 and body == b"not found"


def test_static_symlink_out_of_the_folder(client: Client, tmp_path: Path) -> None:
    outside = tmp_path / "outside.js"
    outside.write_text("export const leaked = 1;\n")
    link = STATIC / f"test-link-{os.getpid()}.js"
    link.symlink_to(outside)
    try:
        status, body, _ = client.get(f"/static/{link.name}")
    finally:
        link.unlink()
    assert status == 404
    assert b"leaked" not in body


def test_head(client: Client) -> None:
    for path in ("/static/app.js", "/api/status", "/"):
        assert client.request("HEAD", path) == 200
    assert client.request("HEAD", "/nope") == 404


def test_updated_follows_the_wal(index_path: Path, tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    path.write_bytes(index_path.read_bytes())
    wal = tmp_path / "index.sqlite-wal"
    wal.write_bytes(b"")  # an empty WAL is a valid one
    later = time.time() + 3600
    os.utime(wal, (later, later))
    for client in _client(path):
        updated = client.get("/api/status")[1]["index"]["updated"]
    assert datetime.fromisoformat(updated).timestamp() == pytest.approx(later, abs=1)


# --------------------------------------------------------------------------- temporary folders


def test_temporary_labels() -> None:
    sessions = [SessionInfo("a", ALPHA, cwd="/home/user/alpha")]
    names = project_names(sessions, Path("/home/user/.openclaw"))
    assert names.key(SCRATCH) == f"temporary ({ALPHA})"
    assert project_display(SCRATCH, names) == "temporary (alpha)"
    assert project_display("-tmp-x", names) == "(temporary)"
    assert project_display("-tmp", names) == "(temporary)"
    assert project_display("-home-user--openclaw", names) == "openclaw"
    assert project_display("-home-user--openclaw-ws", names) == "openclaw (ws)"
    assert project_display(ALPHA, names) == "alpha"


def test_temporary_sessions_grouped(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    ix = open_index(path)
    main = [req("m", NOW - timedelta(hours=1), ALPHA, output=1_000)]
    scratch = [req("t", NOW - timedelta(hours=2), SCRATCH, output=2_000)]
    _put(ix, _log(ALPHA, "m"), [SessionInfo("m", ALPHA, cwd="/home/user/alpha"), *main])
    cwd = f"/tmp/claude-1000/{ALPHA}/{UUID}/scratchpad"
    _put(ix, _log(SCRATCH, "t"), [SessionInfo("t", SCRATCH, cwd=cwd), *scratch])
    ix.close()
    for client in _client(path):
        spend = client.get("/api/spend?days=7&group=project")[1]
        overview = client.get("/api/overview")[1]
        rows = {r["session_id"]: r for r in client.get("/api/sessions?days=0")[1]["sessions"]}
    assert spend["temporary_sessions"] == 1 and spend["sessions_count"] == 1
    labels = {p["label"] for p in overview["today"]["by_project"]}
    assert labels == {"alpha", "temporary (alpha)"}  # never the cwd's last part
    assert rows["t"]["project"] == "temporary (alpha)" and rows["t"]["temporary"] is True
    members = {m["key"]: m["label"] for m in spend["cumulative"]["members"]}
    assert members == {ALPHA: "alpha", "temporary": "temporary folders"}


# --------------------------------------------------------------------------- daylight saving

BERLIN = ZoneInfo("Europe/Berlin")


def test_midnights_across_dst() -> None:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=BERLIN)  # the day after clocks went back
    ctx = api.Context(None, TABLE, FAKE, now, tz=BERLIN)  # type: ignore[arg-type]
    assert ctx.midnight() == datetime(2026, 10, 26, tzinfo=BERLIN)
    assert ctx.midnight().utcoffset() == timedelta(hours=1)
    six = ctx.midnight(6)
    assert (six.date().isoformat(), six.hour, six.utcoffset()) == (
        "2026-10-20",
        0,
        timedelta(hours=2),
    )


def test_spend_days_across_dst(tmp_path: Path) -> None:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=BERLIN)
    first = [req("d", datetime(2026, 10, 20, 0, 30, tzinfo=BERLIN), ALPHA, output=1_000)]
    today = [req("d", datetime(2026, 10, 26, 0, 30, tzinfo=BERLIN), ALPHA, output=3_000)]
    path = tmp_path / "index.sqlite"
    ix = open_index(path)
    _put(ix, _log(ALPHA, "d"), [SessionInfo("d", ALPHA, cwd="/a"), *first, *today])
    ix.close()
    for client in _client(path, clock=lambda: now, tz=BERLIN):
        data = client.get("/api/spend?days=7")[1]
    days = data["daily"]["days"]
    assert days[0]["date"] == "2026-10-20"
    assert days[-1]["date"] == "2026-10-26"
    assert days[0]["total"] == pytest.approx(usd(first), abs=1e-6)
    assert days[-1]["total"] == pytest.approx(usd(today), abs=1e-6)
    assert data["totals"]["usd"] == pytest.approx(usd(first + today), abs=1e-6)
    assert data["cumulative"]["total"][-1] == pytest.approx(data["totals"]["usd"], abs=1e-6)


# --------------------------------------------------------------------------- spikes, thinning


@pytest.fixture(scope="module")
def spiky_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One main thread: 30 cheap turns a minute apart, one cache rewrite of a large
    context, then 12 cheap turns."""
    path = tmp_path_factory.mktemp("spiky") / "index.sqlite"
    start = NOW - timedelta(hours=3)
    reqs = [req("sp", start + timedelta(minutes=i), ALPHA, output=100) for i in range(30)]
    reqs.append(req("sp", start + timedelta(minutes=30), ALPHA, cache_write_1h=900_000))
    reqs += [req("sp", start + timedelta(minutes=31 + i), ALPHA, output=100) for i in range(12)]
    ix = open_index(path)
    _put(ix, _log(ALPHA, "sp"), [SessionInfo("sp", ALPHA, cwd="/a", title="Spiky"), *reqs])
    ix.close()
    return path


def test_spike_markers(spiky_path: Path) -> None:
    for client in _client(spiky_path):
        spend = client.get("/api/spend?days=7")[1]
        overview = client.get("/api/overview")[1]
    for marks in (spend["spikes"], overview["spikes_today"]):
        assert marks["count"] == 1
        (mark,) = marks["marks"]
        assert mark["n"] == 1 and mark["kind"] == "single_request"
        assert mark["session_id"] == "sp" and mark["title"] == "Spiky"
        assert mark["t"] == (NOW - timedelta(hours=2, minutes=30)).isoformat()
        assert mark["excess_usd"] > 1


def test_session_thinning(spiky_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "MAX_POINTS", 9)
    for client in _client(spiky_path):
        data = client.get("/api/session/sp")[1]
    cols, thinned = data["requests"], data["thinned"]
    assert thinned["requests"] == 43 and thinned["spikes"] == 1
    assert thinned["kept"] == len(cols["t"]) < 43
    assert all(len(v) == len(cols["t"]) for v in cols.values())
    assert cols["t"] == sorted(cols["t"])
    assert max(cols["usd"]) > 1  # the spike stays
    assert data["totals"]["requests"] == 43  # totals are not thinned


def test_thin_keeps_maxima_and_spikes() -> None:
    n = 10_000
    threads = [-1 if i % 10 else i % 3 for i in range(n)]
    usd_col = [(i * 7919 % 1000) / 1000 for i in range(n)]
    context = [(i * 104729 % 50_000) for i in range(n)]
    usd_col[1234] = 50.0
    context[777] = 900_000
    kept = api.thin(threads, usd_col, context, {4321})
    assert len(kept) <= 4_000 + 5 * 4 + 1
    assert kept == sorted(set(kept))
    assert {1234, 777, 4321, 0, n - 1} <= set(kept)
    assert api.thin(threads[:50], usd_col[:50], context[:50], set()) == list(range(50))


def test_calibration_reason_labels(client: Client) -> None:
    cal = client.get("/api/session/s1")[1]["calibration"]
    assert "compaction" in cal["reasons"]
    labels = dict(zip(cal["reasons"], cal["reason_labels"], strict=True))
    assert labels["compaction"] == REASON_LABELS["compaction"]


def test_real_spikes_fn_is_used() -> None:
    assert FAKE.find_spikes_fn is real_spikes


# --------------------------------------------------------------------------- background scan


def test_scanner_never_overlaps_and_server_stays_responsive(index_path: Path) -> None:
    release, started = threading.Event(), threading.Event()
    calls: list[int] = []

    def slow_scan() -> None:
        calls.append(1)
        started.set()
        release.wait(10)

    scanner = Scanner(slow_scan, interval=0.01)
    scanner.start()
    try:
        assert started.wait(5)
        assert scanner.running
        assert scanner.scan_once() is False  # one is running: no second scan
        for client in _client(index_path, scanner=scanner):
            t0 = time.monotonic()
            assert client.get("/api/status")[0] == 200
            assert time.monotonic() - t0 < 5
        assert len(calls) == 1
        release.set()
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2  # the loop scans again after the interval
    finally:
        release.set()
        scanner.stop(timeout=5)
    assert not scanner.running


def test_scanner_survives_a_failing_scan(capsys: pytest.CaptureFixture[str]) -> None:
    def failing() -> None:
        raise OSError("disk gone")

    scanner = Scanner(failing)
    assert scanner.scan_once() is True
    assert scanner.scans == 1 and not scanner.running
    assert "disk gone" in capsys.readouterr().err


def test_no_index_page_while_scanning(tmp_path: Path) -> None:
    release = threading.Event()
    scanner = Scanner(lambda: release.wait(10))
    scanner.start()
    try:
        for client in _client(tmp_path / "missing.sqlite", scanner=scanner):
            deadline = time.monotonic() + 5
            while not scanner.running and time.monotonic() < deadline:
                time.sleep(0.01)
            status, body, _ = client.get("/")
        assert status == 503 and b"reading the logs" in body
    finally:
        release.set()
        scanner.stop(timeout=5)


def test_serve_no_scan_flag() -> None:
    from capcom.cli import build_parser

    assert build_parser().parse_args(["serve", "--no-scan"]).no_scan is True
    assert build_parser().parse_args(["serve"]).no_scan is False
