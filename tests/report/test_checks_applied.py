"""Checks with findings marked as applied: they leave the "would save" total and the
overlap count, each kind says how many of its findings are applied, and the kinds
fully applied come after the open ones. On the real capcom.changes over the
synthetic index, with synthetic findings."""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from capcom.report.server import Site
from capcom.types import Finding
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve

WAIT = "subagent_cache_expiry"
MODEL = "model_mix"
CONNECTORS = "claudeai_connectors_unused"


def findings(*args: Any) -> list[Finding]:
    # WAIT may share dollars with MODEL: while MODEL@scout counts, WAIT is left out
    return [
        Finding(MODEL, "Subagents on Opus", "scout", "s", 300.0, "a"),
        Finding(WAIT, "Caches expired during long tool calls", "all", "s", 140.0, "a",
                metrics={"overlaps": (MODEL,)}),
        Finding(CONNECTORS, "Connectors never used", "all", "s", 90.0, "a"),
        Finding(MODEL, "Subagents on Opus", "fixer", "s", 40.0, "a"),
    ]  # fmt: skip


@dataclass
class Fx:
    client: Client
    site: Site

    def mark(self, kind: str, scope: str) -> None:
        body = {"action": "mark", "kind": kind, "scope": scope, "applied_on": "2026-10-21"}
        data = json.dumps(body).encode()
        port = int(self.client.base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/api/changes", body=data, headers={
                "Content-Type": "application/json", "Content-Length": str(len(data)),
                "X-Capcom-Token": self.site.token, "Origin": f"http://127.0.0.1:{port}",
            })  # fmt: skip
            resp = conn.getresponse()
            assert resp.status == 200, resp.read()
            resp.read()
        finally:
            conn.close()

    def checks(self) -> dict[str, Any]:
        status, d, _ = self.client.get("/api/checks")
        assert status == 200
        return d  # type: ignore[no-any-return]


@pytest.fixture
def fx(index_path: Path, tmp_path: Path) -> Iterator[Fx]:
    (tmp_path / "agents").mkdir()
    site = Site(
        index_path, TABLE, replace(FAKE, checks_fn=findings), clock=lambda: NOW,
        changes_path=tmp_path / "changes.jsonl", day_cache=tmp_path / "days.sqlite",
        agent_dirs=[tmp_path / "agents"],
    )  # fmt: skip
    for client in serve(site):
        yield Fx(client, site)


def rollup(d: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(r for r in d["rollups"] if r["kind"] == kind)


def test_nothing_applied_keeps_the_page_as_it_was(fx: Fx) -> None:
    d = fx.checks()
    assert d["applied"] is None
    assert all(r["applied"] is False for r in d["findings"])
    assert [r["kind"] for r in d["rollups"]] == [MODEL, WAIT, CONNECTORS]
    assert all(r["applied"] == 0 for r in d["rollups"])
    assert rollup(d, MODEL)["open_saving_usd_per_week"] == rollup(d, MODEL)["saving_usd_per_week"]
    # MODEL (340) and CONNECTORS (90); WAIT shares dollars with MODEL@scout
    assert d["total"]["usd_per_week"] == 430.0 and d["total"]["findings"] == 3
    assert rollup(d, WAIT)["counted"] == "none"
    assert "marked as applied" not in d["footer"]


def test_a_partly_applied_kind_says_so_and_saves_only_the_rest(fx: Fx) -> None:
    fx.mark(MODEL, "scout")
    d = fx.checks()
    r = rollup(d, MODEL)
    assert r["applied"] == 1 and len(r["findings"]) == 2
    assert r["saving_usd_per_week"] == 340.0 and r["open_saving_usd_per_week"] == 40.0
    scout = next(f for f in d["findings"] if f["scope"] == "scout")
    assert scout["applied"] is True and scout["counted"] is False and scout["mark"]["id"] == 1
    # with MODEL@scout done, WAIT no longer overlaps a counted finding: it counts again
    wait = next(f for f in d["findings"] if f["kind"] == WAIT)
    assert wait["counted"] is True and wait["overlapped_by"] == []
    assert rollup(d, WAIT)["counted"] == "all" and rollup(d, WAIT)["overlaps"] == []
    assert d["total"]["usd_per_week"] == 270.0  # 140 + 90 + 40
    assert d["applied"] == {"findings": 1, "usd_per_week": 300.0}
    assert "1 finding marked as applied is left out of the total" in d["footer"]
    # ordered by what each kind still would save
    assert [x["kind"] for x in d["rollups"]] == [WAIT, CONNECTORS, MODEL]


def test_a_fully_applied_kind_comes_after_the_open_ones(fx: Fx) -> None:
    fx.mark(MODEL, "scout")
    fx.mark(MODEL, "fixer")
    d = fx.checks()
    r = rollup(d, MODEL)
    assert r["applied"] == 2 and r["open_saving_usd_per_week"] == 0.0
    assert [x["kind"] for x in d["rollups"]] == [WAIT, CONNECTORS, MODEL]
    assert d["applied"] == {"findings": 2, "usd_per_week": 340.0}
    assert d["total"]["usd_per_week"] == 230.0 and d["total"]["of"] == 2


def test_everything_applied_leaves_no_total(fx: Fx) -> None:
    for kind, scope in ((MODEL, "scout"), (MODEL, "fixer"), (WAIT, "all"), (CONNECTORS, "all")):
        fx.mark(kind, scope)
    d = fx.checks()
    assert d["total"] is None and d["applied"]["findings"] == 4
    assert d["overlapping"] is False
    assert all(r["applied"] == len(r["findings"]) for r in d["rollups"])
    assert "4 findings marked as applied are left out of the total" in d["footer"]


# --------------------------------------------------------------------------- the chips

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"
STUBS = """
globalThis.document = { querySelector: () => null };
globalThis.window = { dispatchEvent: () => {} };
const m = await import('./marks.js');
"""


def _node(body: str) -> Any:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", f"{STUBS}\n{body}\n"],
        cwd=STATIC, capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_marked_finding_says_where_it_stands_and_links_to_its_change() -> None:
    got = _node("""
    const mark = { id: 7, applied_on: '2026-10-12' };
    console.log(JSON.stringify({
      early: m.markStatus(mark, undefined),
      working: m.markStatus(mark, { verdict: 'working' }),
      worse: m.markStatus(mark, { verdict: 'worse' }, 'scout'),
      none: m.markStatus(mark, { verdict: 'no_data' }),
      odd: m.markStatus(mark, { verdict: 'from a later version' }),
      href: m.changeHref(mark),
      kinds: [m.kindState([{}, {}]), m.kindState([{ mark }, {}]), m.kindState([{ mark }])],
    }));
    """)
    assert got["early"] == {"verdict": "too_early", "text": "Applied Oct 12 · too early"}
    assert got["working"] == {"verdict": "working", "text": "Applied Oct 12 · working"}
    assert got["worse"] == {"verdict": "worse", "text": "scout: applied Oct 12 · worse"}
    assert got["none"]["text"] == "Applied Oct 12 · can\u2019t tell"
    assert got["odd"]["verdict"] == "too_early"
    assert got["href"] == "/changes/7"
    assert got["kinds"] == ["none", "some", "all"]
