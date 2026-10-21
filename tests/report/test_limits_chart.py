"""The limits charts' run-out mark and start, run by node on synthetic forecasts."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"
HOUR = 3600e3
X0 = 1792458000000  # 2026-10-20 01:00 UTC, a Tuesday
X1 = X0 + 168 * HOUR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(expr: str) -> object:
    script = (
        "globalThis.document = { createElement: () => ({}), createElementNS: () => ({}) };\n"
        "const m = await import('./limits.js');\n"
        "const iso = (t) => new Date(t).toISOString();\n"
        f"console.log(JSON.stringify({expr}));\n"
    )
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


def _mark(mid: float | None, early: float | None, late: float | None) -> object:
    fc = {
        "runs_out_at": None if mid is None else f"iso({X0 + mid * HOUR})",
        "runs_out_early": None if early is None else f"iso({X0 + early * HOUR})",
        "runs_out_late": None if late is None else f"iso({X0 + late * HOUR})",
    }
    body = ", ".join(f"{k}: {v if v else 'null'}" for k, v in fc.items())
    return _run(f"m.runoutMark({{{body}}}, {X0}, {X1}, true)")


def test_mark_near_the_right_edge_flips_left_with_its_range() -> None:
    m = _mark(150, 140, 160)
    assert isinstance(m, dict)
    assert m["title"] == "Runs out ~Mon 07:00"
    assert m["sub"] == "between Sun 21:00 and Mon 17:00"
    assert m["side"] == "left"
    assert m["band"][0] < m["f"] < m["band"][1]


def test_mark_early_in_the_window_sits_right() -> None:
    m = _mark(30, None, None)
    assert isinstance(m, dict)
    assert m["side"] == "right" and m["sub"] is None and m["band"] is None


def test_no_mark_when_the_forecast_lasts_until_the_reset() -> None:
    assert _mark(None, 150, None) is None
    assert _mark(200, 150, None) is None  # past the reset


def test_page_chart_starts_at_the_window_start() -> None:
    now = X0 + 60 * HOUR
    pts = f"[[{X0}, 0], [{X0 + 50 * HOUR}, 88]]"
    segs = f"[{{ start: iso({X0}), points: {pts}, resets_at: iso({X1}) }}]"
    assert _run(f"m.chartStart({segs}, {now}, 14)") == X0


FAKE_DOM = """
class Node {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.style = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...cs) { this.children.push(...cs.filter((c) => c != null)); }
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


def test_fan_widens_past_the_limit_and_is_clipped_only_by_the_plot() -> None:
    # a fan from 60 % at hour 100 whose median crosses 100 % near hour 140 and
    # whose upper bound reaches 150 % by the reset: it must keep rising, not flatten
    fan = [[X0 + (100 + 10 * i) * HOUR, 60 + 10 * i, 60 + 5 * i, 60 + 15 * i] for i in range(7)]
    script = (
        FAKE_DOM
        + f"""
const m = await import('./limits.js');
const iso = (t) => new Date(t).toISOString();
const fan = {json.dumps(fan)};
const fc = {{ runs_out_at: iso({X0 + 140 * HOUR}), runs_out_early: iso({X0 + 128 * HOUR}),
  runs_out_late: iso({X0 + 160 * HOUR}) }};
const runout = m.runoutMark(fc, {X0}, {X1}, true);
const pts = [[{X0}, 0], [{X0 + 100 * HOUR}, 60]];
const el = m.limitChart({{ segments: [{{ points: pts, reset: false }}],
  fan, now: fan[0][0], x0: {X0}, x1: {X1}, height: 180, weekly: true, runout }});
const nodes = walk(el, []);
const svg = nodes.find((n) => n.tag === 'svg');
const rect = nodes.find((n) => n.tag === 'rect');
const band = nodes.find((n) => n.tag === 'path' && String(n.attrs.style).includes('fan-fill'));
const median = nodes.find((n) => n.tag === 'path' && n.attrs['stroke-dasharray'] === '6 4');
console.log(JSON.stringify({{ view: svg.attrs.viewBox, rect: rect.attrs, band: band.attrs.d,
  median: median.attrs.d }}));
"""
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "TZ": "UTC"},
    )
    r = json.loads(out.stdout)
    w, h = (float(v) for v in r["view"].split()[2:])
    # the clip is the whole plot rectangle, not the area under 100 %
    assert float(r["rect"]["y"]) == 0 and float(r["rect"]["height"]) == h
    assert float(r["rect"]["width"]) == w

    def ys(d: str) -> list[float]:
        return [float(p.split()[1]) for p in d.replace("M", "L").split("L")[1:]]

    upper = ys(r["band"].rstrip("Z"))[: len(fan)]
    # SVG y grows downward: a rising bound has strictly falling y, past the limit too
    assert all(b < a for a, b in itertools.pairwise(upper))
    ymax = 100 * h / (h - 48)
    y100 = h - 100 / ymax * h
    past = [y for y, p in zip(upper, fan, strict=True) if p[3] > 100]
    assert len(past) >= 3 and all(y < y100 for y in past)
    assert len(set(past)) == len(past)  # never flat along a line
    med = ys(r["median"])
    assert med[-1] < y100  # the median goes on past 100 % too


Pt = tuple[float, float]


def _cross(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    def side(p: Pt, q: Pt, r: Pt) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return side(a, b, c) * side(a, b, d) <= 0 and side(c, d, a) * side(c, d, b) <= 0


def _inside(p: Pt, poly: list[Pt]) -> bool:
    hit = False
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1], strict=True):
        if (ay > p[1]) != (by > p[1]) and p[0] < ax + (p[1] - ay) * (bx - ax) / (by - ay):
            hit = not hit
    return hit


def _meets(box: tuple[float, float, float, float], pts: list[Pt], closed: bool) -> bool:
    a, b, top, bot = box
    corners = [(a, top), (b, top), (b, bot), (a, bot)]
    edges = list(zip(corners, corners[1:] + corners[:1], strict=True))
    segs = list(zip(pts, pts[1:] + (pts[:1] if closed else []), strict=False))
    if any(_cross(p, q, c, d) for p, q in segs for c, d in edges):
        return True
    if any(a <= x <= b and top <= y <= bot for x, y in pts):
        return True
    return closed and any(_inside(c, pts) for c in corners)


@pytest.mark.parametrize(
    ("start", "v0", "rate", "height"),
    [
        (40, 92, 1.0, 210),  # steep, crossing early: label right of the guide
        (40, 92, 1.0, 140),  # the same on the overview card
        (20, 60, 0.35, 210),  # shallow, crossing near the right edge: label flips left
        (20, 60, 0.35, 140),
        (10, 40, 0.75, 210),  # mid-window crossing
    ],
)
def test_runout_label_stays_off_the_fan_and_the_median(
    start: float, v0: float, rate: float, height: int
) -> None:
    hours = [start + 6 * i for i in range(int((168 - start) / 6) + 1)] + [168]
    rise = [(t, rate * (t - start)) for t in hours]
    fan = [[X0 + t * HOUR, v0 + r, v0 + 0.7 * r, v0 + 1.3 * r] for t, r in rise]
    cross = start + (100 - v0) / rate
    f = cross / 168
    h = height + 30
    ymax = 100 * h / (h - 48)
    for w in (560, 900):
        plot = {"x0": X0, "x1": X1, "w": 560, "h": h, "ymax": ymax}
        lab = _run(f"m.runoutLabel({f}, {json.dumps(fan)}, {json.dumps(plot)})")
        assert isinstance(lab, dict)
        assert lab["side"] == ("left" if f > 0.6 else "right")
        # the chart anchors the label at the guide, so on a wider plot it covers
        # less of the forecast: check it where it lands at both widths
        xc = f * w
        a = xc + 10 if lab["side"] == "right" else xc - 10 - 240
        box = (a, a + 240, lab["top"], lab["top"] + 46)
        assert box[0] >= 0 and box[1] <= w and box[3] <= h

        def px(t: float, v: float, w: int = w) -> Pt:
            return ((t - X0) / (X1 - X0) * w, h - v / ymax * h)

        upper = [px(p[0], p[3]) for p in fan]
        lower = [px(p[0], p[2]) for p in reversed(fan)]
        assert not _meets(box, upper + lower, closed=True)
        assert not _meets(box, [px(p[0], p[1]) for p in fan], closed=False)
        assert box[2] > h - 100 / ymax * h  # below the 100 % line
