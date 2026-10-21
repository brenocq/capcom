"""The cumulative band chart, drawn by node in a minimal fake DOM on synthetic series."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

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


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_week_bars_split_before_and_after_at_the_applied_date() -> None:
    script = (
        FAKE_DOM
        + """
globalThis.document.querySelector = () => null;
const m = await import('./changes.js');
const weeks = ['2026-09-24', '2026-10-01', '2026-10-08', '2026-10-15'].map((d, i) =>
  ({ week_start: d, value: [36, 41, 33, 43][i], requests: 1000, after: false }));
weeks.push({ week_start: '2026-10-23', value: 6.2, requests: 1100, after: true });
weeks.push({ week_start: '2026-10-30', value: null, requests: 0, after: true });
const el = m.weekBars({ measure: 'Rewrites per 1,000 requests', before: 38.2, after: 4.1,
  applied_on: '2026-10-22', weeks });
const all = walk(el, []);
const groups = all.filter((n) => n.tag === 'g' && /^wk /.test(n.attrs.class || ''));
const bars = (g) => g.children.filter((c) => c.attrs.class === 'bar');
const applied = all.find((n) => n.attrs.class === 'applied');
const firstAfter = groups[4].children.find((c) => c.attrs.class === 'hit');
console.log(JSON.stringify({
  classes: groups.map((g) => g.attrs.class),
  bars: groups.map((g) => bars(g).length),
  texts: all.filter((n) => n.tag === 'text').map((n) => n.textContent),
  appliedX: applied.attrs.x1, afterX: firstAfter.attrs.x,
  label: el.attrs['aria-label'],
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
    assert got["classes"] == ["wk before"] * 4 + ["wk after"] * 2
    assert got["bars"] == [1, 1, 1, 1, 1, 0]  # a week without requests has no bar
    assert {"before: 38.2", "after: 4.1", "Applied Oct 22", "none", "Oct 23"} <= set(got["texts"])
    assert abs(float(got["appliedX"]) - float(got["afterX"])) < 0.01  # the line opens week 5
    assert got["label"].endswith("Oct 23 6.2, Oct 30 no requests")
