"""Progressive replies: slow API replies as jobs (202 with progress and the reply so
far, then the normal 200), deduplicated, dropped when the index changes, timed out;
and the warmer that computes the pages' usual views ahead of the clicks."""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import capcom.timeline
from capcom.report import api
from capcom.report.analysis import Analysis
from capcom.report.jobs import Cancelled, Job, Jobs, Result
from capcom.report.server import ApiCache, Site, Warmer, cache_key
from capcom.types import (
    CauseKind,
    Confidence,
    Explanation,
    Index,
    PriceTable,
    Spike,
    SpikeKind,
)
from tests.report.conftest import FAKE, NOW, TABLE, Client, fake_checks, serve

SESSIONS = ("s1", "s2", "s3", "s4")


def _spikes(index: Index, table: PriceTable, since: datetime, until: datetime, sid: Any) -> Any:
    """Twelve synthetic spikes, three per session, inside the window."""
    out = []
    for i in range(12):
        start = NOW - timedelta(hours=2 + i)
        out.append(
            Spike(
                SpikeKind.SINGLE_REQUEST,
                start,
                start + timedelta(minutes=5),
                cost_usd=2.0 + i,
                baseline_usd=1.0,
                session_id=SESSIONS[i % 4],
            )
        )
    return [sp for sp in out if since <= sp.start < until]


class Gate:
    """An explain function that waits for a token per call, so a test can step a
    job through its chunks."""

    def __init__(self) -> None:
        self.tokens = threading.Semaphore(0)
        self.calls = 0

    def __call__(self, index: Index, table: PriceTable, spikes: Sequence[Spike]) -> Any:
        self.calls += 1
        assert self.tokens.acquire(timeout=10)
        return [
            Explanation(sp, CauseKind.IDLE_REWRITE, Confidence.CLEAR, sp.excess_usd, "Idle")
            for sp in spikes
        ]


def _site(index_path: Path, gate: Gate, **kw: Any) -> Site:
    analysis = Analysis(
        capcom.timeline.cumulative, capcom.timeline.per_step, _spikes, gate, fake_checks
    )
    return Site(index_path, TABLE, analysis, clock=lambda: NOW, tz=UTC, **kw)


@pytest.fixture
def small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "EXPLAIN_BATCH", 1)  # one session per chunk: four chunks


def _poll(client: Client, path: str, job: str) -> tuple[int, Any]:
    status, body, _ = client.get(f"{path}&job={job}")
    return status, body


def test_slow_reply_is_202_then_200(index_path: Path, small_batches: None) -> None:
    gate = Gate()
    site = _site(index_path, gate, patience=0.05)
    for client in serve(site):
        status, body, _ = client.get("/api/spikes?days=1")
        assert status == 202
        assert body["status"] == "computing" and 0 <= body["progress"] <= 1
        job = body["job"]
        seen, partials = [body["progress"]], []
        for _ in range(4):
            gate.tokens.release()
            time.sleep(0.1)
            status, body = _poll(client, "/api/spikes?days=1", job)
            if status == 200:
                break
            assert status == 202 and body["job"] == job
            seen.append(body["progress"])
            if body["partial"] is not None:
                assert body["source"] == "chunks"
                partials.append(body["partial"])
        while status == 202:
            gate.tokens.release()
            status, body = _poll(client, "/api/spikes?days=1", job)
        assert status == 200
        final = body
    assert seen == sorted(seen) and seen[-1] < 1
    assert partials, "the job published the reply so far"
    listed = [len(p["spikes"]) for p in partials]
    assert listed == sorted(listed) and listed[-1] <= len(final["spikes"])
    for p in partials:  # the same shape as the final reply, the same whole-window totals
        assert set(p) == set(final)
        assert p["count"] == final["count"] and p["excess_usd"] == final["excess_usd"]
        assert p["explained"] <= final["explained"]
    # the same reply as computing it in the request
    plain = _site(index_path, Gate(), jobs=None)
    plain.analysis.explain_fn.tokens.release(99)  # type: ignore[attr-defined]
    for client in serve(plain):
        assert client.get("/api/spikes?days=1")[1] == final
    assert gate.calls == 4


def test_fast_reply_is_a_plain_200(index_path: Path) -> None:
    gate = Gate()
    gate.tokens.release(99)
    for client in serve(_site(index_path, gate, patience=5)):
        status, body, _ = client.get("/api/spikes?days=1")
        assert status == 200 and "spikes" in body
        assert client.get("/api/spikes?days=1")[0] == 200  # and from the cache next


def test_requests_for_one_view_share_a_job(index_path: Path) -> None:
    gate = Gate()
    for client in serve(_site(index_path, gate, patience=0.05)):
        first = client.get("/api/spikes?days=1")[1]["job"]
        second = client.get("/api/spikes?days=1")[1]["job"]
        other = client.get("/api/spikes?days=7")[1]["job"]
        assert first == second != other
        gate.tokens.release(99)
    assert gate.calls == 2  # one explain per view (one chunk each)


def test_a_new_index_generation_drops_the_job(
    index_path: Path, tmp_path: Path, small_batches: None
) -> None:
    path = tmp_path / "index.sqlite"
    shutil.copy(index_path, path)
    gate = Gate()
    site = _site(path, gate, patience=0.05)
    for client in serve(site):
        job = client.get("/api/spikes?days=1")[1]["job"]
        gate.tokens.release()
        time.sleep(0.1)
        with sqlite3.connect(path) as conn:  # a scan wrote
            conn.execute("CREATE TABLE IF NOT EXISTS scratch (x)")
            conn.execute("INSERT INTO scratch VALUES (1)")
        status, body = _poll(client, "/api/spikes?days=1", job)
        assert status == 202 and body["job"] != job  # a new job, for the new index
        gate.tokens.release(99)
        deadline = time.monotonic() + 10
        while status == 202 and time.monotonic() < deadline:
            status, body = _poll(client, "/api/spikes?days=1", body["job"])
        assert status == 200
    assert site.jobs is not None and len(site.jobs) == 0


def test_a_job_past_its_time_limit_is_a_504(index_path: Path, small_batches: None) -> None:
    gate = Gate()
    site = _site(index_path, gate, patience=0.05, jobs=Jobs(timeout=0.3))
    for client in serve(site):
        job = client.get("/api/spikes?days=1")[1]["job"]
        time.sleep(0.4)
        status, body = _poll(client, "/api/spikes?days=1", job)
        assert status == 504 and body["error"] == "timeout"
        gate.tokens.release(99)


def test_stale_reply_stands_in_while_the_new_one_computes(index_path: Path, tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    shutil.copy(index_path, path)
    gate = Gate()
    gate.tokens.release()
    for client in serve(_site(path, gate, patience=2)):
        old = client.get("/api/spikes?days=1")[1]
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS scratch (x)")
            conn.execute("INSERT INTO scratch VALUES (1)")
        status, body, _ = client.get("/api/spikes?days=1&job=x")
        assert status == 202 and body["source"] == "stale" and body["partial"] == old
        gate.tokens.release(99)


# --------------------------------------------------------------------------- Jobs


def _stepper(steps: int, pause: float, log: list[str], name: str) -> Any:
    def work(job: Job) -> Result:
        for i in range(steps):
            job.step(i / steps)
            log.append(name)
            time.sleep(pause)
        return Result(200, b"{}")

    return work


def test_background_job_yields_to_a_page_job() -> None:
    jobs = Jobs()
    log: list[str] = []
    bg = jobs.submit("warm", "g", _stepper(20, 0.01, log, "bg"), background=True)
    time.sleep(0.05)
    page = jobs.submit("page", "g", _stepper(5, 0.02, log, "page"))
    assert page.done.wait(5) and bg.done.wait(5)
    first, last = log.index("page"), len(log) - 1 - log[::-1].index("page")
    # once the page's job started, the background one passed no step until it ended
    # (one chunk it was already in may finish)
    assert log[first + 1 : last].count("bg") <= 1
    assert bg.result is not None and page.result is not None


def test_background_job_holding_a_needed_slot_is_cancelled() -> None:
    jobs = Jobs(max_running=1)
    bg = jobs.submit("warm", "g", _stepper(100, 0.01, [], "bg"), background=True)
    time.sleep(0.05)
    page = jobs.submit("page", "g", _stepper(2, 0.01, [], "page"))
    assert page.done.wait(5) and page.result is not None
    assert bg.done.wait(5) and bg.cancelled == "cancelled"


def test_set_gen_cancels_running_jobs() -> None:
    jobs = Jobs()
    jobs.set_gen("g1")
    job = jobs.submit("k", "g1", _stepper(200, 0.01, [], "x"))
    time.sleep(0.05)
    jobs.set_gen("g2")
    assert job.done.wait(5) and job.cancelled == "stale" and job.result is None
    assert jobs.find("k") is None


def test_progress_is_monotonic_and_estimated_between_steps() -> None:
    now = [0.0]
    jobs = Jobs(clock=lambda: now[0])
    job = Job(jobs, "k", "g", background=False)
    job.started, job.expected = 0.0, 10.0
    now[0] = 5.0
    p1 = job.progress()
    assert 0.4 < p1 < 0.5  # half the expected time: 0.9 * 0.5
    job._progress = 0.2  # a step that reports less than the estimate
    assert job.progress() >= p1
    with pytest.raises(Cancelled):
        now[0] = 1000.0
        job.step(0.5)
    assert job.cancelled == "timeout"


# --------------------------------------------------------------------------- warmer


def test_warmer_fills_the_cache_and_skips_when_warm(index_path: Path) -> None:
    gate = Gate()
    gate.tokens.release(99)
    site = _site(index_path, gate)
    views = {"spikes": [{"days": "14"}, {"days": "7"}], "limits": [{"days": "14"}]}
    warmer = Warmer(site, views=views, rest=0)
    warmer.saw("spikes", {"days": "7"})
    assert warmer.warm_round()
    assert warmer.computed == 3
    assert site.cache is not None
    for name, query in [("spikes", {"days": "7"}), ("limits", {"days": "14"})]:
        key = cache_key(site, name, [name], query, NOW)
        assert site.cache.get(key, NOW.timestamp()) is not None
    assert warmer.warm_round() and warmer.computed == 3  # nothing changed: nothing to do


def test_warmer_warms_the_views_opened_lately_then_the_defaults() -> None:
    site = Site(Path("/nonexistent"), TABLE, FAKE)
    views = {"a": [{"days": "14"}], "b": [{"days": "14"}, {"days": "7"}, {"days": "30"}]}
    warmer = Warmer(site, views=views, recent=60)
    assert warmer.order() == [("a", {"days": "14"}), ("b", {"days": "14"})]  # no other tabs
    warmer.saw("b", {"days": "30"})
    warmer.saw("a", {"days": "7"})
    warmer.saw("session", {})  # not a warmed page
    assert warmer.order() == [
        ("a", {"days": "7"}),
        ("b", {"days": "30"}),
        ("a", {"days": "14"}),
        ("b", {"days": "14"}),
    ]
    warmer.saw("b", {"days": "30"})  # opened again: newest first
    assert warmer.order()[0] == ("b", {"days": "30"})


def test_warmer_forgets_views_not_opened_lately() -> None:
    site = Site(Path("/nonexistent"), TABLE, FAKE)
    warmer = Warmer(site, views={"a": [{"days": "14"}]}, recent=60)
    warmer.saw("a", {"days": "30"})
    tag = ("a", (("days", "30"),))
    warmer._opened[tag] -= 61  # opened a minute and a second ago
    assert warmer.order() == [("a", {"days": "14"})]


def test_cache_keeps_a_stale_stand_in_per_view() -> None:
    cache = ApiCache(max_bytes=1000)
    cache.put(("g1", "d", "spikes", (), ()), 0, b"old")
    cache.put(("g2", "d", "spikes", (), ()), 0, b"new")
    assert cache.stale(("g3", "d2", "spikes", (), ())) == b"new"  # the newest one
    assert cache.get(("g1", "d", "spikes", (), ()), 0) is None
    assert cache.stale(("g2", "d", "spikes", (), ())) == b"old"  # retired, but kept
    assert cache.stale(("g2", "d", "sessions", (), ())) is None


def test_usage_samples_turn_only_the_limits_reply_stale(index_path: Path, tmp_path: Path) -> None:
    from capcom.limits.model import samples_path

    site = _site(index_path, Gate(), state_dir=tmp_path)
    samples = samples_path(tmp_path)
    samples.write_text("{}\n")
    names = ("spikes", "spend", "limits", "overview")
    before = {n: cache_key(site, n, [n], {"days": "14"}, NOW) for n in names}
    samples.write_text("{}\n{}\n")  # the recorder adds a sample
    after = {n: cache_key(site, n, [n], {"days": "14"}, NOW) for n in names}
    assert after["spikes"] == before["spikes"] and after["spend"] == before["spend"]
    assert after["limits"] != before["limits"] and after["overview"] != before["overview"]
    assert after["limits"][0] == after["spikes"][0]  # one generation for every page


# --------------------------------------------------------------------------- priority


def test_background_work_waits_while_a_page_request_is_in_flight() -> None:
    jobs = Jobs(quiet=0.1)
    log: list[str] = []
    jobs.user_begin()
    bg = jobs.submit("warm", "g", _stepper(5, 0.01, log, "bg"), background=True)
    time.sleep(0.2)
    assert log == [] and not bg.done.is_set()  # not even started while the page waits
    jobs.user_end()
    assert jobs.must_yield(bg)  # the quiet time after the last request
    assert bg.done.wait(5) and bg.result is not None and log == ["bg"] * 5
    assert not jobs.must_yield(bg)


def test_a_page_job_never_yields() -> None:
    jobs = Jobs()
    jobs.user_begin()
    page = jobs.submit("page", "g", _stepper(3, 0.01, [], "page"))
    assert page.done.wait(5) and page.result is not None
    jobs.user_end()


def test_a_prefetch_runs_low_and_the_click_lifts_it(index_path: Path, small_batches: None) -> None:
    gate = Gate()
    site = _site(index_path, gate, patience=0.05)
    low = {"X-Capcom-Priority": "low"}
    for client in serve(site):
        status, body, _ = client.get("/api/spikes?days=1", headers=low)
        assert status == 202
        assert site.jobs is not None
        job = site.jobs.by_id(body["job"])
        assert job is not None and job.low
        _poll_low = client.get(f"/api/spikes?days=1&job={body['job']}", headers=low)
        assert _poll_low[0] == 202 and job.low  # its own polls do not lift it
        status, body2, _ = client.get("/api/spikes?days=1")  # the click
        assert body2["job"] == body["job"] and not job.low  # the same job, lifted
        gate.tokens.release(99)
        assert job.done.wait(5) and job.result is not None


def test_a_poll_waits_for_news(index_path: Path, small_batches: None) -> None:
    gate = Gate()
    site = _site(index_path, gate, patience=0.05, poll_wait=0.4)
    for client in serve(site):
        status, body, _ = client.get("/api/spikes?days=1")
        assert status == 202
        t0 = time.monotonic()
        status, _, _ = client.get(f"/api/spikes?days=1&job={body['job']}")
        assert status == 202 and time.monotonic() - t0 >= 0.35  # nothing new: waited
        threading.Timer(0.1, lambda: gate.tokens.release(99)).start()
        t0 = time.monotonic()
        while status == 202:
            status, _, _ = client.get(f"/api/spikes?days=1&job={body['job']}")
        assert status == 200 and time.monotonic() - t0 < 1.5


# --------------------------------------------------------------------------- worker


def test_the_worker_process_computes_the_same_reply_and_pauses(index_path: Path) -> None:
    from capcom.report.analysis import default_analysis
    from capcom.report.server import _work, compute
    from capcom.report.worker import Worker

    site = Site(index_path, TABLE, default_analysis(), clock=lambda: NOW, tz=UTC)
    site.jobs = Jobs(quiet=0.05)
    site.worker = Worker(site)
    try:
        query = {"days": "14"}
        key = cache_key(site, "sessions", ["sessions"], query, NOW)
        here = compute(site, "sessions", ["sessions"], dict(query), NOW)
        site.jobs.user_begin()  # a page's request in flight: the child must not run
        work = _work(site, "sessions", ["sessions"], query, NOW, key, background=True)
        job = site.jobs.submit(key, key[0], work, "sessions", background=True)
        assert not job.done.wait(0.5)
        site.jobs.user_end()
        assert job.done.wait(30) and job.result is not None
        assert job.result.status == 200 and job.result.body == here.body
        assert site.worker.started == 1
        assert site.cache is not None and site.cache.get(key, NOW.timestamp()) == here.body
    finally:
        site.worker.close()


def test_the_worker_stops_its_child_while_a_page_needs_the_cpu(index_path: Path) -> None:
    from capcom.report.analysis import default_analysis
    from capcom.report.server import _work
    from capcom.report.worker import Worker

    site = Site(index_path, TABLE, default_analysis(), clock=lambda: NOW, tz=UTC)
    site.jobs = Jobs(quiet=0.05)
    site.worker = Worker(site)
    try:
        query = {"days": "30"}
        key = cache_key(site, "overview", ["overview"], query, NOW)
        work = _work(site, "overview", ["overview"], query, NOW, key, background=True)
        job = site.jobs.submit(key, key[0], work, "overview", background=True)
        deadline = time.monotonic() + 10
        while site.worker._proc is None and time.monotonic() < deadline:
            time.sleep(0.01)  # the child starts
        site.jobs.user_begin()
        busy = not job.done.is_set()
        time.sleep(0.3)
        stopped = site.worker.stopped
        site.jobs.user_end()
        assert job.done.wait(30) and job.result is not None and job.result.status == 200
        if busy:  # the child was still computing when the page came: it was stopped
            assert stopped >= 1 and job.paused > 0
    finally:
        site.worker.close()


# --------------------------------------------------------------------------- live pages


def test_version_says_which_part_changed(index_path: Path, tmp_path: Path) -> None:
    from capcom.limits.model import samples_path

    site = _site(index_path, Gate(), state_dir=tmp_path)
    for client in serve(site):
        status, v1, _ = client.get("/api/version")
        assert status == 200 and set(v1) == {"index", "samples", "marks", "updated"}
        assert client.get("/api/version")[1] == v1  # nothing changed
        samples = samples_path(tmp_path)
        samples.parent.mkdir(parents=True, exist_ok=True)
        samples.write_text("{}\n")
        v2 = client.get("/api/version")[1]
        assert v2["samples"] != v1["samples"]
        assert v2["index"] == v1["index"] and v2["marks"] == v1["marks"]
    assert site.jobs is not None and len(site.jobs) == 0  # never a job
