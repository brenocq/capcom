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
