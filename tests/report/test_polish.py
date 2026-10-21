"""Labels shared with `capcom top`, the pro-rated "today" comparison, the week's
daily average over elapsed days, and the API cache per scan generation."""

from __future__ import annotations

import io
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from capcom.config import Config
from capcom.cost.top import run_top
from capcom.report.server import ApiCache, Site, generation
from capcom.store.index import open_index
from capcom.types import SessionInfo
from tests.report.conftest import FAKE, NOW, TABLE, _log, _put, req, serve, usd

HOME = "/home/user"
OPENCLAW = Path(HOME) / ".openclaw"
TOY = "-home-user-Github-toy-brain"
CLAW = "-home-user--openclaw"
CLAW_WS = "-home-user--openclaw-workspace"
UUID = "0f0e0d0c-0b0a-4908-8706-050403020100"
SCRATCH = f"-tmp-claude-1000-{TOY}-{UUID}-scratchpad"


def _site(path: Path, **kw: Any) -> Site:
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("tz", UTC)
    return Site(path, TABLE, FAKE, openclaw_root=OPENCLAW, **kw)


# --------------------------------------------------------------------------- labels


@pytest.fixture(scope="module")
def projects_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("labels") / "index.sqlite"
    ix = open_index(path)
    when = NOW - timedelta(hours=2)
    folders = [
        (TOY, f"{HOME}/Github/toy-brain/lewm"),  # a session that moved into a subfolder
        (CLAW, str(OPENCLAW)),
        (CLAW_WS, f"{OPENCLAW}/workspace"),
        (SCRATCH, f"/tmp/claude-1000/{TOY}/{UUID}/scratchpad"),
    ]
    for i, (project, cwd) in enumerate(folders):
        sid = f"p{i}"
        reqs = [req(sid, when + timedelta(minutes=i), project, input=1_000 * (i + 1), output=10)]
        _put(ix, _log(project, sid), [SessionInfo(sid, project, cwd=cwd), *reqs])
    ix.close()
    return path


def test_labels_identical_in_cli_and_api(projects_index: Path, tmp_path: Path) -> None:
    config = Config(tmp_path / "claude", OPENCLAW, tmp_path / "cache")
    out = io.StringIO()
    with open_index(projects_index, readonly=True) as ix:
        assert run_top(config, "project", 7, 0, index=ix, out=out, now=NOW) == 0
    text = out.getvalue()
    expected = {"toy-brain", "openclaw", "openclaw (workspace)", "temporary (toy-brain)"}
    for client in serve(_site(projects_index)):
        week = client.get("/api/overview")[1]["week"]
        rows = client.get("/api/sessions?days=7")[1]["sessions"]
    api_labels = {g["label"] for g in week["by_project"]}
    assert api_labels == expected  # the two OpenClaw folders listed apart
    table = text.split("\n\n")[1].splitlines()[2:]  # after the header and the rule
    cli_labels = {line.split("  ")[0].strip() for line in table if not line.startswith("all")}
    assert cli_labels == expected
    # the full folder beside the name in the CLI, on hover on the web
    assert "~/Github/toy-brain" in text or f"{HOME}/Github/toy-brain" in text
    paths = {g["label"]: g["path"] for g in week["by_project"]}
    assert paths["toy-brain"].endswith("/Github/toy-brain")
    assert {r["project"] for r in rows} == expected
    assert all(r["project_path"] for r in rows)


def test_charts_say_openclaw_is_merged(
    projects_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(OPENCLAW))  # the timeline's series read it
    for client in serve(_site(projects_index)):
        data = client.get("/api/spend?days=7&group=project")[1]
    labels = {m["label"] for m in data["cumulative"]["members"]}
    assert "openclaw (all folders)" in labels
    assert "toy-brain" in labels


# --------------------------------------------------------------------------- fair comparisons


def test_today_against_the_same_hour(tmp_path: Path) -> None:
    """Now is 15:00: each previous day counts only its spend before 15:00."""
    path = tmp_path / "index.sqlite"
    ix = open_index(path)
    early, late, today = [], [], []
    for d in range(1, 8):
        day = NOW - timedelta(days=d)
        early.append(req("old", day.replace(hour=8), TOY, input=70_000))
        late.append(req("old", day.replace(hour=20), TOY, input=700_000))
    today.append(req("new", NOW.replace(hour=9), TOY, input=70_000))
    _put(ix, _log(TOY, "old"), [SessionInfo("old", TOY, cwd="/x"), *early, *late])
    _put(ix, _log(TOY, "new"), [SessionInfo("new", TOY, cwd="/x"), *today])
    ix.close()
    for client in serve(_site(path)):
        d = client.get("/api/overview")[1]
    prev = d["previous_7_days"]
    assert prev["same_hour"] == "15:00"
    assert prev["same_hour_average_usd"] == pytest.approx(usd(early) / 7, abs=1e-6)
    assert prev["daily_average_usd"] == pytest.approx(usd(early + late) / 7, abs=1e-6)
    # the week: 6 full days and today's 15 hours
    assert d["week"]["elapsed_days"] == pytest.approx(6 + 15 / 24, abs=1e-3)
    assert d["week"]["full_days"] == 6


# --------------------------------------------------------------------------- cache


def test_cache_answers_repeats_and_a_new_scan_invalidates(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    ix = open_index(path)
    first = [req("c1", NOW - timedelta(hours=1), TOY, input=10_000)]
    _put(ix, _log(TOY, "c1"), [SessionInfo("c1", TOY, cwd="/x"), *first])
    ix.close()
    site = _site(path)
    assert site.cache is not None
    for client in serve(site):
        a = client.get("/api/spend?days=7")[1]
        assert site.cache.misses == 1 and site.cache.hits == 0
        assert client.get("/api/spend?days=7")[1] == a
        assert site.cache.hits == 1
        client.get("/api/spend?days=14")  # other params: their own entry
        assert site.cache.misses == 2
        client.get("/api/status")  # never cached
        assert site.cache.misses == 2
        before = generation(path)
        ix = open_index(path)  # a scan lands
        more = [req("c2", NOW - timedelta(minutes=30), TOY, input=50_000)]
        _put(ix, _log(TOY, "c2"), [SessionInfo("c2", TOY, cwd="/x"), *more])
        ix.close()
        assert generation(path) != before
        b = client.get("/api/spend?days=7")[1]
        assert b["totals"]["usd"] == pytest.approx(usd(first + more), abs=1e-6)
        assert b["totals"]["usd"] > a["totals"]["usd"]
        assert len(site.cache) == 1  # the old generation's entries are gone


def test_cache_is_bounded_and_expires() -> None:
    cache = ApiCache(max_bytes=400, ttl=10.0)
    for i in range(10):
        cache.put(("g", i), 0.0, b"x" * 90)
    assert len(cache) == 4  # least recently used out
    assert cache.get(("g", 0), 1.0) is None
    assert cache.get(("g", 9), 1.0) == b"x" * 90
    assert cache.get(("g", 9), 11.0) is None  # older than the ttl
    cache.put(("g", "big"), 0.0, b"x" * 200)  # over a quarter of the budget: not kept
    assert cache.get(("g", "big"), 0.0) is None


def test_no_cache(projects_index: Path) -> None:
    site = _site(projects_index, cache=None)
    for client in serve(site):
        assert client.get("/api/overview")[0] == 200


# --------------------------------------------------------------------------- short history


def _history(path: Path, reqs: list[Any]) -> None:
    ix = open_index(path)
    _put(ix, _log(TOY, "h"), [SessionInfo("h", TOY, cwd="/x"), *reqs])
    ix.close()


def test_averages_count_only_days_with_history(tmp_path: Path) -> None:
    """Two days of history: the averages divide by the days that have data."""
    path = tmp_path / "index.sqlite"
    old = [req("h", (NOW - timedelta(days=d)).replace(hour=8), TOY, input=70_000) for d in (1, 2)]
    _history(path, [*old, req("h", NOW.replace(hour=9), TOY, input=70_000)])
    for client in serve(_site(path)):
        d = client.get("/api/overview")[1]
    prev = d["previous_7_days"]
    assert prev["days"] == 2
    assert prev["daily_average_usd"] == pytest.approx(usd(old) / 2, abs=1e-6)
    assert prev["same_hour_average_usd"] == pytest.approx(usd(old) / 2, abs=1e-6)
    assert d["week"]["full_days"] == 2 and d["week"]["days"] == 3
    assert d["week"]["elapsed_days"] == pytest.approx(2 + 15 / 24, abs=1e-3)


def test_same_time_comparison_waits_for_a_baseline(tmp_path: Path) -> None:
    """A cent of baseline at 00:30: no comparison; at 15:00 there is one."""
    path = tmp_path / "index.sqlite"
    _history(
        path, [req("h", NOW - timedelta(days=1, hours=15) + timedelta(minutes=10), TOY, input=10)]
    )
    early = NOW.replace(hour=0, minute=30)
    for client in serve(_site(path, clock=lambda: early)):
        prev = client.get("/api/overview")[1]["previous_7_days"]
    assert 0 < prev["same_hour_average_usd"] < 1
    assert prev["comparable"] is False
    for client in serve(_site(path)):
        assert client.get("/api/overview")[1]["previous_7_days"]["comparable"] is True


def test_today_against_the_same_hour_across_daylight_saving(tmp_path: Path) -> None:
    """New York left daylight saving on 2026-11-01: local hours still line up."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 4, 15, 0, tzinfo=ny)
    path = tmp_path / "index.sqlite"
    early, late = [], []
    for d in range(1, 8):
        day = (now - timedelta(days=d)).date()
        early.append(
            req("h", datetime(day.year, day.month, day.day, 8, tzinfo=ny), TOY, input=70_000)
        )
        late.append(
            req("h", datetime(day.year, day.month, day.day, 20, tzinfo=ny), TOY, input=700_000)
        )
    _history(path, sorted([*early, *late], key=lambda r: r.timestamp))
    for client in serve(_site(path, clock=lambda: now, tz=ny)):
        d = client.get("/api/overview")[1]
    prev = d["previous_7_days"]
    assert prev["days"] == 7 and prev["same_hour"] == "15:00"
    assert prev["same_hour_average_usd"] == pytest.approx(usd(early) / 7, abs=1e-6)
    assert prev["daily_average_usd"] == pytest.approx(usd(early + late) / 7, abs=1e-6)
    assert d["week"]["elapsed_days"] == pytest.approx(6 + 15 / 24, abs=1e-3)
