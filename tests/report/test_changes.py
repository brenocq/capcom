"""Did it work?: the API shapes, the marks on Checks, and the one write (POST
/api/changes) with each of its refusals, on the real capcom.changes over the synthetic
index (marks, day cache and agent definitions in tmp). Stand-ins only for the error
paths."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import capcom.changes
from capcom.changes.cli import DAY_CACHE
from capcom.report import didit
from capcom.report import server as server_mod
from capcom.report.server import Site
from capcom.types import ChangeMark, ChangeResult, Finding, Verdict
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve

TOOL_WAIT = "subagent_cache_expiry"
MODEL = "model_mix"


def checks_fn(*args: Any) -> list[Finding]:
    return [
        Finding(TOOL_WAIT, "Caches expired during long tool calls", "all", "s", 140.0, "a"),
        Finding(MODEL, "Subagents on Opus", "scout", "s", 300.0, "a", change="model: sonnet"),
    ]


@dataclass
class Fixture:
    client: Client
    site: Site
    checks_runs: list[bool]

    def marks(self) -> list[ChangeMark]:
        return capcom.changes.load(self.site.changes_path)

    @property
    def port(self) -> int:
        return int(self.client.base.rsplit(":", 1)[1])

    def post(
        self,
        body: Any,
        *,
        token: str | None = "default",
        origin: str | None = "default",
        referer: str | None = None,
        ctype: str = "application/json",
        raw: bytes | None = None,
        path: str = "/api/changes",
    ) -> tuple[int, Any]:
        data = raw if raw is not None else json.dumps(body).encode()
        headers = {"Content-Type": ctype, "Content-Length": str(len(data))}
        if token is not None:
            headers["X-Capcom-Token"] = self.site.token if token == "default" else token
        if origin is not None:
            headers["Origin"] = f"http://127.0.0.1:{self.port}" if origin == "default" else origin
        if referer is not None:
            headers["Referer"] = referer
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            out = resp.read()
            ctype_out = resp.getheader("Content-Type") or ""
            parsed = json.loads(out) if ctype_out.startswith("application/json") else out
            return resp.status, parsed
        finally:
            conn.close()

    def mark(self, **kw: Any) -> tuple[int, Any]:
        body = {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "2026-10-21"}
        return self.post(body | kw)


def _make(
    index_path: Path, tmp_path: Path, read_only: bool = False, timeout: float = 30.0
) -> Iterator[Fixture]:
    checks_runs: list[bool] = []  # one per Checks pass: was the write lock held?
    sites: list[Site] = []

    def recording_checks(*args: Any) -> list[Finding]:
        checks_runs.append(sites[0].write_lock.locked())
        return checks_fn()

    (tmp_path / "agents").mkdir()
    analysis = replace(FAKE, checks_fn=recording_checks)  # capcom.changes is the real one
    site = Site(
        index_path, TABLE, analysis, clock=lambda: NOW, tz=UTC,
        changes_path=tmp_path / "changes.jsonl", read_only=read_only, request_timeout=timeout,
        day_cache=tmp_path / "days.sqlite", agent_dirs=[tmp_path / "agents"],
    )  # fmt: skip
    sites.append(site)
    for client in serve(site):
        yield Fixture(client, site, checks_runs)


def definition(fx: Fixture, model: str, changed: datetime) -> Path:
    """A synthetic definition of agent type "scout", modified at `changed`."""
    path = Path(fx.site.agent_dirs[0]) / "scout.md"  # type: ignore[index]
    path.write_text(f"---\nname: scout\nmodel: {model}\n---\nMap the code.\n")
    os.utime(path, (changed.timestamp(), changed.timestamp()))
    return path


@pytest.fixture
def fx(index_path: Path, tmp_path: Path) -> Iterator[Fixture]:
    yield from _make(index_path, tmp_path)


@pytest.fixture
def ro(index_path: Path, tmp_path: Path) -> Iterator[Fixture]:
    yield from _make(index_path, tmp_path, read_only=True)


# --------------------------------------------------------------------------- reads


def test_empty_summary(fx: Fixture) -> None:
    status, d, _ = fx.client.get("/api/changes")
    assert status == 200
    assert d["marks"] == 0 and d["changes"] == []
    assert d["counts"] == {v: 0 for v in ("working", "no_effect", "worse", "too_early", "no_data")}
    assert d["with_verdict"]["measured_usd_per_week"] is None
    assert d["path"] == str(fx.site.changes_path)


def test_page_carries_the_token(fx: Fixture) -> None:
    for path in ("/changes", "/changes/3", "/checks"):
        status, body, _ = fx.client.get(path)
        assert status == 200
        found = re.search(rb'<meta name="capcom-token" content="([^"]+)">', body)
        assert found and found.group(1).decode() == fx.site.token
        assert b"capcom-read-only" not in body
    assert fx.client.get("/changes/3/x")[0] == 404


def test_tokens_differ_per_launch(index_path: Path) -> None:
    assert Site(index_path, TABLE, FAKE).token != Site(index_path, TABLE, FAKE).token


def test_mark_then_read_everywhere(fx: Fixture) -> None:
    _, before, _ = fx.client.get("/api/checks")  # cached before the mark
    assert all(r["mark"] is None for r in before["findings"])
    status, d = fx.mark(note="Added the rule <b>to</b> scout.md", applied_on="2026-10-12")
    assert status == 200, d
    m = d["mark"]
    assert m["id"] == 1 and m["finding_id"] == f"{TOOL_WAIT}@all"
    assert m["title"] == "Caches expired during long tool calls"  # from the finding
    assert m["expected_usd_per_week"] == 140.0 and m["note"] == "Added the rule <b>to</b> scout.md"
    assert m["scope_label"] == "every project" and m["source"] == "user"
    # the cache key holds changes.jsonl: Checks shows the mark at once
    _, after, _ = fx.client.get("/api/checks")
    row = next(r for r in after["findings"] if r["kind"] == TOOL_WAIT)
    assert row["finding_id"] == f"{TOOL_WAIT}@all" and row["mark"]["id"] == 1
    assert row["measure"] == capcom.changes.measure_name(TOOL_WAIT) is not None
    _, s, _ = fx.client.get("/api/changes")
    # the synthetic index holds 10 days of a few requests: too little for a verdict
    assert s["marks"] == 1 and s["counts"]["no_data"] == 1
    assert s["with_verdict"] == {
        "changes": 0, "measured_usd_per_week": None, "expected_usd_per_week": None,
        "share": None,
    }  # fmt: skip
    r = s["changes"][0]
    assert r["verdict"] == "no_data" and r["reason"].startswith("Can't tell")
    assert r["measure"] == capcom.changes.measure_name(TOOL_WAIT)
    assert r["days_since"] == 9 and "weeks" not in r
    assert r["window"]["before_to"] == "2026-10-11" and r["window"]["after_from"] == "2026-10-13"
    _, one, _ = fx.client.get("/api/changes/1")
    assert {k: v for k, v in one.items() if k != "weeks"} == r
    weeks = one["weeks"]
    assert weeks and {w["after"] for w in weeks} == {False, True}
    assert set(weeks[0]) == {"week_start", "value", "requests", "after"}
    assert r["window"]["before_from"] == weeks[0]["week_start"]
    assert fx.client.get("/api/status")[1]["changes"] == 1
    assert fx.client.get("/api/changes/2")[0] == 404
    assert fx.client.get("/api/changes/abc")[0] == 404


def test_a_kind_without_a_measure(fx: Fixture) -> None:
    capcom.changes.mark("nope", "all", date(2026, 10, 14), "No measure", path=fx.site.changes_path)
    r = fx.client.get("/api/changes/1")[1]
    assert (r["verdict"], r["measure"], r["weeks"]) == ("no_data", "(no measure)", [])
    assert r["window"]["before_from"] is None


def test_measures_keep_days_in_the_day_cache(fx: Fixture) -> None:
    assert server_mod.DAY_CACHE == DAY_CACHE  # serve keeps the file `capcom changes` keeps
    fx.mark(applied_on="2026-10-12")
    fx.mark(kind=MODEL, scope="scout", applied_on="2026-10-20")
    assert fx.site.day_cache is not None and not fx.site.day_cache.exists()
    _, s, _ = fx.client.get("/api/changes")
    assert [c["kind"] for c in s["changes"]] == [MODEL, TOOL_WAIT]  # the latest date first
    assert fx.site.day_cache.exists()


def mark_of(id: int, verdict: Verdict, measured: float | None, expected: float | None,
            due: date | None = None) -> ChangeResult:  # fmt: skip
    m = ChangeMark(id, "k", "all", date(2026, 10, 1), "t", "suggested", "", expected, NOW, "user")
    return ChangeResult(m, "x", None, None, None, (), 0.0, 0.0, measured, verdict, due, "synthetic")


def test_tally_sums_the_changes_with_a_verdict() -> None:
    results = [
        mark_of(1, Verdict.WORKING, 124.0, 140.0),
        mark_of(2, Verdict.WORSE, -20.0, 60.0),
        mark_of(3, Verdict.NO_EFFECT, None, 50.0),  # a verdict, no measured saving
        mark_of(4, Verdict.TOO_EARLY, None, 999.0, due=date(2026, 10, 30)),
        mark_of(5, Verdict.TOO_EARLY, None, None, due=date(2026, 10, 28)),
        mark_of(6, Verdict.NO_DATA, None, 999.0),
    ]
    t = didit.tally(results)
    assert t["counts"] == {"working": 1, "no_effect": 1, "worse": 1, "too_early": 2, "no_data": 1}
    assert t["with_verdict"] == {
        "changes": 3, "measured_usd_per_week": 104.0, "expected_usd_per_week": 250.0,
        "share": round(104 / 250, 4),
    }  # fmt: skip
    assert t["next_verdict"] == "2026-10-28"
    empty = didit.tally([mark_of(1, Verdict.WORKING, 10.0, None)])
    assert empty["with_verdict"]["share"] is None
    assert didit.tally([])["with_verdict"]["measured_usd_per_week"] is None


def test_noticed_skips_marked_findings(fx: Fixture) -> None:
    definition(fx, "sonnet", NOW - timedelta(hours=2))
    _, d, _ = fx.client.get("/api/changes/noticed?days=14")
    assert [n["finding_id"] for n in d["noticed"]] == [f"{MODEL}@scout"]
    n = d["noticed"][0]
    assert n["path"].endswith("scout.md") and n["detail"] == "model: sonnet"
    assert n["saving_usd_per_week"] == 300.0 and n["since"].startswith("2026-10-21T13:00")
    assert fx.mark(kind=MODEL, scope="scout", source="noticed")[0] == 200
    assert fx.client.get("/api/changes/noticed?days=14")[1]["noticed"] == []
    assert fx.client.get("/api/changes/noticed?days=9")[0] == 400


def test_noticed_keeps_to_the_window(fx: Fixture) -> None:
    definition(fx, "sonnet", datetime(2026, 10, 14, 12, tzinfo=UTC))  # before the 7 days
    assert fx.client.get("/api/changes/noticed?days=7")[1]["noticed"] == []
    assert len(fx.client.get("/api/changes/noticed?days=14")[1]["noticed"]) == 1
    definition(fx, "opus", NOW)  # not the suggested line (days=30: a reply not cached yet)
    assert fx.client.get("/api/changes/noticed?days=30")[1]["noticed"] == []


def test_edit_and_unmark(fx: Fixture) -> None:
    fx.mark()
    status, d = fx.post({"action": "edit", "id": 1, "note": "  later words "})
    assert status == 200 and d["mark"]["note"] == "later words"
    status, d = fx.post({"action": "edit", "id": 1, "applied_on": "2026-10-19"})
    assert status == 200 and d["mark"]["applied_on"] == "2026-10-19"
    assert fx.post({"action": "unmark", "id": 1}) == (200, {"unmarked": 1})
    assert fx.client.get("/api/changes")[1]["marks"] == 0
    assert fx.post({"action": "unmark", "id": 1})[0] == 404


# --------------------------------------------------------------------------- refusals


def test_refuses_without_the_token(fx: Fixture) -> None:
    assert fx.mark()[0] == 200
    body = {"action": "unmark", "id": 1}
    assert fx.post(body, token=None)[1]["error"] == "bad_token"
    assert fx.post(body, token="x" * 43)[0] == 403
    assert fx.post(body, token="")[0] == 403
    assert len(fx.marks()) == 1


def test_refuses_other_origins(fx: Fixture) -> None:
    body = {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "2026-10-21"}
    for origin in ("http://evil.example", f"http://127.0.0.1:{fx.port + 1}", "null"):
        status, d = fx.post(body, origin=origin)
        assert (status, d["error"]) == (403, "cross_origin"), origin
    # no Origin: the Referer decides, and neither is refused
    assert fx.post(body, origin=None)[0] == 403
    assert fx.post(body, origin=None, referer="http://evil.example/checks")[0] == 403
    status, _ = fx.post(body, origin=None, referer=f"http://localhost:{fx.port}/checks")
    assert status == 200
    assert len(fx.marks()) == 1


def test_refuses_a_foreign_host(fx: Fixture) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", fx.port, timeout=10)
    conn.request("POST", "/api/changes", body=b"{}", headers={"Host": "evil.example"})
    assert conn.getresponse().status == 403
    conn.close()


def test_refuses_wrong_type_and_size(fx: Fixture) -> None:
    body = {"action": "unmark", "id": 1}
    assert fx.post(body, ctype="text/plain")[0] == 415
    assert fx.post(body, ctype="application/x-www-form-urlencoded")[0] == 415
    assert fx.post(None, raw=b'{"action": "mark", "note": "' + b"x" * 5000 + b'"}')[0] == 413
    assert fx.post(None, raw=b"{not json")[1]["error"] == "bad_json"
    assert fx.post(body, ctype="application/json; charset=utf-8")[0] == 404  # passed the guard


OK = {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "2026-10-21"}


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"action": "delete", "id": 1},
        {"action": "mark", "kind": TOOL_WAIT, "scope": "all"},
        OK | {"x": 1},
        {"action": "mark", "kind": "nope", "scope": "all", "applied_on": "2026-10-21"},
        {"action": "mark", "kind": TOOL_WAIT, "scope": "beta", "applied_on": "2026-10-21"},
        {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "2026-10-22"},
        {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "2025-01-01"},
        {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "21/10/2026"},
        {"action": "mark", "kind": TOOL_WAIT, "scope": "all", "applied_on": "2026-02-30"},
        OK | {"what": "x"},
        OK | {"note": "a" * 501},
        OK | {"note": "a\nb"},
        OK | {"note": 3},
        OK | {"days": 9},
        OK | {"source": "x"},
        {"action": "mark", "kind": 1, "scope": "all", "applied_on": "2026-10-21"},
        {"action": "edit", "id": 1},
        {"action": "edit", "id": "1", "note": "x"},
        {"action": "edit", "id": True, "note": "x"},
    ],
)  # fmt: skip
def test_refuses_a_bad_schema(fx: Fixture, body: Any) -> None:
    assert fx.mark(kind=MODEL, scope="scout")[0] == 200  # mark 1 exists: ids are real
    status, d = fx.post(body)
    assert status == 400, d
    assert len(fx.marks()) == 1


def test_one_mark_per_finding(fx: Fixture) -> None:
    assert fx.mark()[0] == 200
    status, d = fx.mark()
    assert (status, d["error"]) == (409, "already_marked")


def test_other_paths_stay_get_only(fx: Fixture) -> None:
    assert fx.post({}, path="/api/checks")[0] == 405
    assert fx.post({}, path="/checks")[0] == 405
    assert fx.client.request("PUT", "/api/changes") == 405
    assert fx.client.request("DELETE", "/api/changes") == 405


def test_read_only_turns_the_write_off(ro: Fixture) -> None:
    status, _ = ro.mark()
    assert status == 405
    assert ro.marks() == []
    body = ro.client.get("/checks")[1]
    assert b'<meta name="capcom-read-only" content="1">' in body
    assert ro.client.get("/api/changes")[0] == 200  # reads still work


# --------------------------------------------------------------------------- review fixes


def test_a_mark_keeps_unrelated_replies_cached(fx: Fixture) -> None:
    cache = fx.site.cache
    assert cache is not None
    fx.client.get("/api/sessions?days=7")
    fx.mark()
    hits = cache.hits
    fx.client.get("/api/sessions?days=7")
    assert cache.hits == hits + 1


def test_the_write_lock_never_waits_on_checks(fx: Fixture) -> None:
    assert fx.mark()[0] == 200
    assert fx.checks_runs and not any(fx.checks_runs)
    # with Checks of that window cached, a mark runs no Checks pass at all
    fx.client.get("/api/checks?days=14")
    runs = len(fx.checks_runs)
    assert fx.mark(kind=MODEL, scope="scout")[0] == 200
    assert len(fx.checks_runs) == runs


class StaleChanges:
    """capcom.changes, but its next read misses the newest mark: as when `capcom mark`
    writes between the website's read of the marks and its own write."""

    def __init__(self) -> None:
        self.stale = False

    def __getattr__(self, name: str) -> Any:
        return getattr(capcom.changes, name)

    def load(self, path: Path | None = None) -> list[ChangeMark]:
        marks = capcom.changes.load(path)
        if self.stale:
            self.stale = False
            return marks[:-1]
        return marks


def test_the_backends_duplicate_refusal_is_409(fx: Fixture) -> None:
    stale = StaleChanges()
    fx.site.analysis = replace(fx.site.analysis, changes_mod=stale)
    capcom.changes.mark(TOOL_WAIT, "all", NOW.date(), "t", path=fx.site.changes_path)
    stale.stale = True
    status, d = fx.mark()
    assert (status, d["error"]) == (409, "already_marked")
    assert len(fx.marks()) == 1


class RefusingChanges:
    """A backend that refuses its marks path (as capcom.changes does under ~/.claude)."""

    def store_path(self) -> Path:
        raise ValueError("refusing to write marks there")

    def load(self, path: Path | None = None) -> list[ChangeMark]:
        raise ValueError("refusing to read marks there")


def test_a_refused_marks_path_leaves_checks_working(index_path: Path, tmp_path: Path) -> None:
    analysis = replace(FAKE, checks_fn=checks_fn, changes_mod=RefusingChanges())
    for client in serve(Site(index_path, TABLE, analysis, clock=lambda: NOW, tz=UTC)):
        status, d, _ = client.get("/api/checks")
        assert status == 200
        assert all(r["mark"] is None for r in d["findings"])


def test_a_slow_body_cannot_hold_a_thread(index_path: Path, tmp_path: Path) -> None:
    for fx in _make(index_path, tmp_path, timeout=0.5):
        head = (
            "POST /api/changes HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Origin: http://127.0.0.1:{fx.port}\r\nX-Capcom-Token: {fx.site.token}\r\n"
            "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", fx.port), timeout=5) as sock:
            sock.sendall(head.encode() + b'{"action"')  # 9 of the 100 bytes, then nothing
            started = time.monotonic()
            while sock.recv(1024):
                pass  # the server closes the connection
            assert time.monotonic() - started < 3
        assert fx.marks() == []
