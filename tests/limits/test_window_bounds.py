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
const seg = (a, b, peak, reset, known = true) => ({{ start: iso(a), resets_at: iso(b), reset, start_known: known,
  points: [[a, 0], [a + (b - a) / 2, peak]] }});
const H = {H};
// three windows back to back, an idle gap, then a window still open (now: 22 h)
const segs = [seg({X0}, {X0} + 5 * H, 60, true), seg({X0} + 5 * H, {X0} + 10 * H, 90, true),
  seg({X0} + 10 * H, {X0} + 15 * H, 40, true), seg({X0} + 18 * H, {X0} + 23 * H, 20, false)];
const NOW = {X0} + 22 * H;
"""


def test_every_start_and_reset_is_found_once() -> None:
    got = _run(
        SEGS
        + f"console.log(JSON.stringify(m.windowBounds(segs, {X0}, {X0} + 23 * H, false, NOW)));"
    )
    # the first start (the chart's left edge), two resets that start the next window
    # (one line each), a reset, the start after the idle gap, the coming reset (the
    # right edge)
    assert [b["kind"] for b in got] == ["start", "reset", "reset", "reset", "start", "next"]
    assert [round(b["f"] * 23) for b in got] == [0, 5, 10, 15, 18, 23]
    assert [b["label"] for b in got] == [_hm(X0 + k * H) for k in (0, 5, 10, 15, 18, 23)]
    assert [b.get("starts", False) for b in got] == [False, True, True, False, False, False]
    assert all(b["exact"] for b in got)
    # the coming reset is placed first, then the open window's start, then newest first
    assert [b["rank"] for b in got] == [5, 4, 3, 2, 1, 0]
    assert got[-1]["title"] == f"Resets {_hm(X0 + 23 * H)}"


def test_a_start_seen_only_as_a_first_reading_is_inferred() -> None:
    got = _run(
        SEGS
        + f"""
segs[3] = seg({X0} + 18 * H + H / 2, {X0} + 23 * H, 20, false, false);
segs[3].resets_at = null;
const b = m.windowBounds(segs, {X0}, {X0} + 23 * H, false, NOW);
console.log(JSON.stringify(b.filter((x) => !x.exact)));
"""
    )
    assert len(got) == 1
    assert got[0]["label"] == "~" + _hm(X0 + 18.5 * H)
    assert "first reading" in got[0]["title"]


def test_weekly_labels_name_the_day() -> None:
    day = 24 * H
    got = _run(
        f"""
const seg = (a, b, reset) => ({{ start: iso(a), resets_at: iso(b), reset, start_known: true, points: [[a, 0], [a + {day}, 30]] }});
const segs = [seg({X0}, {X0} + 7 * {day}, true), seg({X0} + 7 * {day}, {X0} + 14 * {day}, false)];
console.log(JSON.stringify(m.windowBounds(segs, {X0}, {X0} + 14 * {day}, true, {X0} + 10 * {day})));
"""
    )
    assert [b["kind"] for b in got] == ["start", "reset", "next"]
    assert got[1]["label"].endswith(_hm(X0 + 7 * day)) and len(got[1]["label"]) > 5


def test_labels_find_room_beside_their_line_or_a_row_up() -> None:
    got = _run(
        """
const it = (x, rank) => ({ x, w: 40, rank });
console.log(JSON.stringify({
  apart: m.placeLabels([it(50, 0), it(150, 1)], 300),
  close: m.placeLabels([it(100, 1), it(110, 0)], 300),
  stack: m.placeLabels([it(100, 0), it(160, 1), it(130, 2)], 300),
  edges: m.placeLabels([it(0, 0), it(300, 1)], 300),
  now: m.placeLabels([it(95, 0)], 300, [[100, 120]]),
  crowd: m.placeLabels([it(100, 2), it(105, 0), it(110, 1), it(95, 3)], 300),
  // "now" near the right edge, then the coming reset at the edge (weighted)
  coming: m.placeLabels([{ x: 290, w: 24, rank: -1 }, { x: 300, w: 32, rank: 0, weight: 5 }, it(275, 1)], 300),
}));
"""
    )
    rows = {k: [p["row"] for p in v] for k, v in got.items()}
    assert rows["apart"] == [0, 0] and not any(p["flag"] for p in got["apart"])
    # two lines 10 px apart: each label beside its own line, away from the other
    assert rows["close"] == [0, 0]
    assert got["close"][0] == {"row": 0, "left": 57, "flag": True}
    assert got["close"][1] == {"row": 0, "left": 113, "flag": True}
    # a label with no room beside its line goes up a row, its line between the others
    assert rows["stack"] == [0, 0, 1]
    # labels at the plot's edges stay inside it
    assert [p["left"] for p in got["edges"]] == [0, 260]
    # the "now" label keeps its place: the label goes beside its line, away from it
    assert got["now"][0]["row"] == 0 and got["now"][0]["left"] + 40 <= 100
    # four lines in 15 px: two labels; the others are left out (the chart counts
    # them and shows them on hover), never drawn over a line or each other
    assert sorted(rows["crowd"]) == [-1, -1, 0, 0]
    assert rows["crowd"][1] == 0  # the first in priority is always placed
    # "now" steps left of its line, off the coming reset's, so that one finds room a row up
    assert got["coming"][0] == {"row": 0, "left": 263, "flag": True}
    assert rows["coming"][1] == 1


def test_the_chart_draws_a_line_and_a_label_per_boundary() -> None:
    got = _run(
        SEGS
        + f"""
segs[3].start_known = false;
const el = m.limitChart({{ segments: segs, fan: [], now: NOW, x0: {X0}, x1: {X0} + 23 * H, height: 160, weekly: true, kind: 'five_hour' }});
const nodes = walk(el, []);
console.log(JSON.stringify({{
  lines: nodes.filter((n) => n.className === 'winbound').map((n) => n.attrs.style),
  labels: nodes.filter((n) => n.className === 'winlab').map((n) => [n.textContent, /visibility:hidden/.test(n.attrs.style)]),
}}));
"""
    )
    assert len(got["lines"]) == 6
    assert ["dashed" in s for s in got["lines"]] == [False] * 4 + [True, False]
    texts = [_hm(X0 + k * H) for k in (0, 5, 10, 15)] + ["~" + _hm(X0 + 18 * H), _hm(X0 + 23 * H)]
    assert got["labels"] == [[t, False] for t in texts]


def test_the_page_chart_spans_the_range_and_reaches_the_coming_reset() -> None:
    got = _run(
        SEGS
        + f"""
const day = 24 * H;
console.log(JSON.stringify({{
  today: m.chartStart(segs, NOW, {X0} + 12 * H),
  empty: m.chartStart(segs, {X0} + 3 * day, {X0} + 2 * day),
  week: m.chartStart(segs, {X0} + 3 * day, {X0} + 2 * day, true),
  end: m.chartEnd(segs, null, NOW),
  past: m.chartEnd(segs.slice(0, 3), null, NOW),
}}));
"""
    )
    # from the start of the oldest window that ends inside the range
    assert got["today"] == X0 + 10 * H
    # no window in the range: the range's start, or for the weekly chart its newest window
    assert got["empty"] == X0 + 48 * H
    assert got["week"] == X0 + 18 * H
    # the x-axis reaches the open window's reset, past now; without one it ends now
    assert got["end"] == X0 + 23 * H
    assert got["past"] == X0 + 22 * H


def _hm(ms: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M")
