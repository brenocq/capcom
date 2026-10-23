# ruff: noqa: E501  (the scripts run by node keep their lines whole)
"""The window boundaries (resets) on the limits charts, drawn by node in a minimal fake
DOM on synthetic windows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import pytest

from tests.limits.test_pages import FAKE_DOM, STATIC

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

H = 3600e3
X0 = 1_792_000_000_000 - 1_792_000_000_000 % int(H)  # on the hour, UTC
ISO = "new Date(t).toISOString()"


def _run(body: str, module: str = "limits.js") -> Any:
    script = FAKE_DOM + f"const m = await import('./{module}');\nconst iso = (t) => {ISO};\n" + body
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "TZ": "UTC"},
    )
    return json.loads(out.stdout)


SEGS = f"""
const seg = (a, b, peak, reset) => ({{ start: iso(a), resets_at: iso(b), reset, start_known: true,
  points: [[a, 0], [a + (b - a) / 2, peak]] }});
const H = {H};
// three windows back to back, an idle gap, then a window still open
const segs = [seg({X0}, {X0} + 5 * H, 60, true), seg({X0} + 5 * H, {X0} + 10 * H, 90, true),
  seg({X0} + 10 * H, {X0} + 15 * H, 40, true), seg({X0} + 18 * H, {X0} + 23 * H, 20, false)];
"""


def test_every_boundary_inside_the_chart_is_found_once() -> None:
    got = _run(
        SEGS + f"console.log(JSON.stringify(m.windowBounds(segs, {X0}, {X0} + 23 * H, false)));"
    )
    assert [b["kind"] for b in got] == ["reset", "reset", "reset", "start"]
    assert [round(b["f"] * 23) for b in got] == [5, 10, 15, 18]
    assert [b["label"] for b in got] == [
        _hm(X0 + 5 * H),
        _hm(X0 + 10 * H),
        _hm(X0 + 15 * H),
        _hm(X0 + 18 * H),
    ]
    assert all(b["labeled"] for b in got)
    # the chart's own edges are no boundaries
    assert all(0 < b["f"] < 1 for b in got)


def test_labels_make_room_on_a_crowded_chart() -> None:
    got = _run(
        SEGS
        + f"console.log(JSON.stringify(m.windowBounds(segs, {X0} - 200 * H, {X0} + 23 * H, false)));"
    )
    assert len(got) == 5  # every line is drawn (the first window's start is inside now)
    assert [b["labeled"] for b in got] == [True, False, False, False, False]  # with room only


def test_the_chart_draws_a_line_per_boundary() -> None:
    got = _run(
        SEGS
        + f"""
const el = m.limitChart({{ segments: segs, fan: [], now: {X0} + 22 * H, x0: {X0}, x1: {X0} + 23 * H, height: 160, weekly: true, kind: 'five_hour' }});
const nodes = walk(el, []);
console.log(JSON.stringify({{
  lines: nodes.filter((n) => n.className === 'winbound').map((n) => n.attrs.style),
  labels: nodes.filter((n) => n.className === 'winlab').map((n) => n.textContent),
}}));
"""
    )
    assert len(got["lines"]) == 4
    assert all("solid" in s for s in got["lines"])
    assert got["labels"] == [_hm(X0 + 5 * H), _hm(X0 + 10 * H), _hm(X0 + 15 * H), _hm(X0 + 18 * H)]


def _hm(ms: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M")
