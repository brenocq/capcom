"""The cumulative band chart, drawn by node in a minimal fake DOM on synthetic series."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

# A tiny stand-in for the DOM: elements keep their tag, attributes and children.
FAKE_DOM = """
class Node {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.style = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...cs) { this.children.push(...cs); }
  addEventListener() {}
}
globalThis.Node = Node;
globalThis.document = {
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t),
  createTextNode: (t) => new Node('#text'),
};
const walk = (n, out) => {
  out.push(n);
  (n.children || []).forEach((c) => walk(c, out));
  return out;
};
"""


def _strokes() -> list[dict[str, object]]:
    script = (
        FAKE_DOM
        + """
const m = await import('./charts.js');
const el = m.bandChart({
  times: [1, 2], start: 0, end: 2,
  members: [
    { label: 'a', values: [4, 6], color: 'red' },
    { label: 'b', values: [1, 3], color: 'blue' },
  ],
  total: [5, 9],
});
const paths = walk(el, []).filter((n) => n.tag === 'path' && n.attrs.fill === 'none');
console.log(JSON.stringify(paths.map((p) => ({ style: p.attrs.style, d: p.attrs.d }))));
"""
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result: list[dict[str, object]] = json.loads(out.stdout)
    return result


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_day_bars_link_each_day_and_keep_the_last_label_clear() -> None:
    script = (
        FAKE_DOM
        + """
const m = await import('./charts.js');
const pad = (n) => String(n).padStart(2, '0');
const days = [];
for (let i = 0; i < 90; i++) {
  const d = new Date(2026, 6, 24 + i);
  const iso = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  days.push({ date: d, iso, values: [i], total: i });
}
const el = m.dayBars(days, [{ label: 'a', color: 'red' }], days[89].iso);
const [bars, labs] = el.children;
console.log(JSON.stringify({
  links: bars.children.map((c) => [c.tag, c.attrs.href]),
  labels: labs.children.map((c) => c.textContent || ''),
}));
"""
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    got = json.loads(out.stdout)
    assert got["links"][0] == ["a", "/day/2026-07-24"]
    assert got["links"][89] == ["a", "/day/2026-10-21"]
    # every 7th day is labeled, but not Oct 16, five days before today's label
    assert got["labels"][77] == "Oct 9" and got["labels"][84] == ""
    assert got["labels"][89] == "Oct 21"


def _ys(d: object) -> list[float]:
    assert isinstance(d, str)
    return [float(pt.split(" ")[1]) for pt in d.lstrip("M").split("L")]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_band_chart_draws_the_total_and_each_series_from_zero() -> None:
    lines = {str(p["style"]): _ys(p["d"]) for p in _strokes()}
    # the total is its own line in the text color, and the highest one
    assert "stroke:var(--text)" in lines
    total = lines.pop("stroke:var(--text)")
    a, b = lines["stroke:red"], lines["stroke:blue"]
    # every line starts at zero (the bottom of the plot)
    assert total[0] == a[0] == b[0]
    # not stacked: the smaller series stays below the larger one, and the total
    # (5 then 9) lies above both
    assert all(t < x < y for t, x, y in zip(total[1:], a[1:], b[1:], strict=True))


def _node(body: str) -> Any:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", FAKE_DOM + body],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_end_label_keeps_clear_of_the_markers_and_the_now_label() -> None:
    got = _node("""
const m = await import('./charts.js');
const box = { left: -44, top: -24, right: 804, bottom: 304 };
const size = { w: 40, h: 16 };
const pt = { x: 780, y: 20 };
// nothing in the way: above the end, to its left
const free = m.placeEndLabel(pt, size, [], box);
// a spike marker just above-left of the end and the "now" label above it
const marker = { left: 730, top: -8, right: 756, bottom: 18 };
const now = { left: 770, top: -22, right: 795, bottom: -8 };
const busy = m.placeEndLabel(pt, size, [marker, now], box);
const x = pt.x + busy.dx, y = pt.y + busy.dy;
const r = { left: x, top: y, right: x + 40, bottom: y + 16 };
const hit = [marker, now].some((o) =>
  r.left < o.right && r.right > o.left && r.top < o.bottom && r.bottom > o.top);
const inside = r.left >= box.left && r.right <= box.right
  && r.top >= box.top && r.bottom <= box.bottom;
// everything blocked: the least overlapping place, still inside
const wall = { left: 0, top: -24, right: 800, bottom: 300 };
const stuck = m.placeEndLabel(pt, size, [wall], box);
console.log(JSON.stringify({ free, hit, inside, stuck }));
""")
    assert got["free"] == {"dx": -46, "dy": -22}
    assert not got["hit"] and got["inside"]
    assert set(got["stuck"]) == {"dx", "dy"}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_colors_follow_the_entity_not_its_rank() -> None:
    got = _node("""
const m = await import('./lib.js');
const a = m.entityColors(['capcom', 'improved', 'toy-brain', '(other)']);
const b = m.entityColors(['toy-brain', '(other)', 'improved', 'capcom']);
const many = m.entityColors(['a', 'b', 'c', 'd', 'e', 'f', 'g']);
console.log(JSON.stringify({ a: [...a], b: Object.fromEntries(b),
  distinct: new Set([...a.values()]).size, many: [...many.values()] }));
""")
    a = dict(got["a"])
    assert a == got["b"]
    assert a["(other)"] == "var(--gray)"
    assert got["distinct"] == 4
    # six palette colors for the first six keys, the seventh shares the gray
    assert len(set(got["many"][:6])) == 6 and got["many"][6] == "var(--gray)"
