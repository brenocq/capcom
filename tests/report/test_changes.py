"""Did it work?: the API shapes, the marks on Checks, and the one write (POST
/api/changes) with each of its refusals. capcom.changes is a stand-in here."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from capcom.report.server import Site
from capcom.types import Finding
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve
from tests.report.fake_changes import FakeChanges


def checks_fn(*args: Any) -> list[Finding]:
    return [
        Finding("tool_wait", "Caches expired during long tool calls", "all", "s", 140.0, "a"),
        Finding("model", "Subagents on Opus", "coder", "s", 300.0, "a"),
    ]


@dataclass
class Fixture:
    client: Client
    site: Site
    fake: FakeChanges

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
        body = {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "2026-10-21"}
        return self.post(body | kw)


def _make(index_path: Path, tmp_path: Path, read_only: bool = False) -> Iterator[Fixture]:
    fake = FakeChanges(tmp_path / "changes.jsonl", noticed_kind="model")
    analysis = replace(FAKE, checks_fn=checks_fn, changes_mod=fake)
    site = Site(
        index_path, TABLE, analysis, clock=lambda: NOW, tz=UTC,
        changes_path=fake.path, read_only=read_only,
    )  # fmt: skip
    for client in serve(site):
        yield Fixture(client, site, fake)


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
    assert d["path"] == str(fx.fake.path)


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
    status, d = fx.mark(note="Added the rule <b>to</b> coder.md", applied_on="2026-10-12")
    assert status == 200, d
    m = d["mark"]
    assert m["id"] == 1 and m["finding_id"] == "tool_wait@all"
    assert m["title"] == "Caches expired during long tool calls"  # from the finding
    assert m["expected_usd_per_week"] == 140.0 and m["note"] == "Added the rule <b>to</b> coder.md"
    # the cache key holds changes.jsonl: Checks shows the mark at once
    _, after, _ = fx.client.get("/api/checks")
    row = next(r for r in after["findings"] if r["kind"] == "tool_wait")
    assert row["finding_id"] == "tool_wait@all" and row["mark"]["id"] == 1
    _, s, _ = fx.client.get("/api/changes")
    assert s["marks"] == 1 and s["counts"]["working"] == 1
    assert s["with_verdict"] == {
        "changes": 1, "measured_usd_per_week": 124.0, "expected_usd_per_week": 140.0,
        "share": round(124 / 140, 4),
    }  # fmt: skip
    r = s["changes"][0]
    assert r["verdict"] == "working" and r["days_since"] == 9 and "weeks" not in r
    assert r["window"] == {
        "before_from": "2026-09-14", "before_to": "2026-10-11", "after_from": "2026-10-13",
    }  # fmt: skip
    _, one, _ = fx.client.get("/api/changes/1")
    assert [w["after"] for w in one["weeks"]] == [False] * 4 + [True, True]
    first = {"week_start": "2026-09-14", "value": 36.0, "requests": 1000, "after": False}
    assert one["weeks"][0] == first
    assert one["evidence"] == [{"label": "Long waits per week", "before": "101", "after": "3"}]
    assert fx.client.get("/api/status")[1]["changes"] == 1
    assert fx.client.get("/api/changes/2")[0] == 404
    assert fx.client.get("/api/changes/abc")[0] == 404


def test_too_early_and_the_next_verdict(fx: Fixture) -> None:
    assert fx.mark(applied_on="2026-10-20")[0] == 200
    _, s, _ = fx.client.get("/api/changes")
    assert s["counts"]["too_early"] == 1 and s["with_verdict"]["changes"] == 0
    assert s["next_verdict"] == "2026-10-28"
    assert s["changes"][0]["verdict_due"] == "2026-10-28"


def test_noticed_skips_marked_findings(fx: Fixture) -> None:
    _, d, _ = fx.client.get("/api/changes/noticed?days=14")
    assert [n["finding_id"] for n in d["noticed"]] == ["model@coder"]
    n = d["noticed"][0]
    assert n["path"].endswith("coder.md") and n["detail"] == "model: sonnet"
    assert n["saving_usd_per_week"] == 300.0
    assert fx.mark(kind="model", scope="coder", source="noticed")[0] == 200
    assert fx.client.get("/api/changes/noticed?days=14")[1]["noticed"] == []
    assert fx.client.get("/api/changes/noticed?days=9")[0] == 400


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
    assert fx.fake.calls == ["mark"]


def test_refuses_other_origins(fx: Fixture) -> None:
    body = {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "2026-10-21"}
    for origin in ("http://evil.example", f"http://127.0.0.1:{fx.port + 1}", "null"):
        status, d = fx.post(body, origin=origin)
        assert (status, d["error"]) == (403, "cross_origin"), origin
    # no Origin: the Referer decides, and neither is refused
    assert fx.post(body, origin=None)[0] == 403
    assert fx.post(body, origin=None, referer="http://evil.example/checks")[0] == 403
    status, _ = fx.post(body, origin=None, referer=f"http://localhost:{fx.port}/checks")
    assert status == 200
    assert fx.fake.calls == ["mark"]


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


OK = {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "2026-10-21"}


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"action": "delete", "id": 1},
        {"action": "mark", "kind": "tool_wait", "scope": "all"},
        OK | {"x": 1},
        {"action": "mark", "kind": "nope", "scope": "all", "applied_on": "2026-10-21"},
        {"action": "mark", "kind": "tool_wait", "scope": "beta", "applied_on": "2026-10-21"},
        {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "2026-10-22"},
        {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "2025-01-01"},
        {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "21/10/2026"},
        {"action": "mark", "kind": "tool_wait", "scope": "all", "applied_on": "2026-02-30"},
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
    status, d = fx.post(body)
    assert status in (400, 404), d
    assert fx.fake.calls == []


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
    assert ro.fake.calls == []
    body = ro.client.get("/checks")[1]
    assert b'<meta name="capcom-read-only" content="1">' in body
    assert ro.client.get("/api/changes")[0] == 200  # reads still work
