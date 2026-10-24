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


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_day_bars_put_an_amount_on_every_day_with_spend() -> None:
    got = _node("""
const m = await import('./charts.js');
const totals = [0, 12.5, 1234, 80, 3.2, 640, 41];
const days = totals.map((t, i) => ({ date: new Date(2026, 9, 16 + i),
  iso: `2026-10-${16 + i}`, values: [t], total: t }));
const el = m.dayBars(days, [{ label: 'a', color: 'red' }], days[6].iso);
const [bars] = el.children;
console.log(JSON.stringify({
  mode: bars.className,
  amts: bars.children.map((c) => {
    const a = c.children.find((k) => k && /amt/.test(k.className || ''));
    return a ? [a.textContent, a.className] : null;
  }),
  f: bars.children.map((c) => c.attrs.style),
}));
""")
    # a week fits level labels until the browser measures the bars
    assert got["mode"] == "days hl flat"
    assert got["amts"][0] is None  # a day without spend has no amount
    assert [a[0] for a in got["amts"][1:]] == ["$13", "$1.2k", "$80", "$3.2", "$640", "$41"]
    # the highest day and today are the ones a phone keeps
    keys = [i for i, a in enumerate(got["amts"]) if a and "key" in a[1]]
    assert keys == [2, 6]
    assert got["f"][2] == "--f:1.0000" and got["f"][0] == "--f:0.0000"


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


COLORS = {
    "general": 2,
    "palette": [["#111111", "#eeeeee"], ["#222222", "#dddddd"], ["#C8601A", "#F0904A"]],
    "project": {"-home-x-capcom": 2, "capcom": 2, "-home-x-cv": 0, "cv": 0},
    "agent": {"main": 1, "scout": 0},
    "model": {"claude-opus-5-5": 1},
}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_colors_follow_the_entity_not_its_rank() -> None:
    got = _node(
        """
document.head = new Node('head');
document.getElementById = (id) => document.head.children.find((c) => c.id === id) || null;
const m = await import('./lib.js');
const before = m.entityColor('capcom', 'project');
m.useColors(COLORS_JSON, false);
// two views with different keys: the day's chart (folder keys) and its bars (labels)
const chart = m.entityColors(['-home-x-capcom', '-home-x-cv', '(other)'], 'project');
const bars = m.entityColors(['cv', 'improved', 'capcom'], 'project');
const style = document.getElementById('entity-colors');
const [chartC, barsC] = [Object.fromEntries(chart), Object.fromEntries(bars)];
console.log(JSON.stringify({ before, chart: chartC, bars: barsC,
  agent: m.entityColor('main', 'agent'), anyKind: m.entityColor('claude-opus-5-5'),
  unknown: [m.entityColor('improved', 'project'), m.entityColor('improved', 'project')],
  css: style ? style.textContent : null }));
""".replace("COLORS_JSON", json.dumps(COLORS))
    )
    assert got["before"].startswith("var(--s")  # no assignment yet: a stable palette slot
    assert got["chart"]["-home-x-capcom"] == got["bars"]["capcom"] == "var(--ec2)"
    assert got["chart"]["-home-x-cv"] == got["bars"]["cv"] == "var(--ec0)"
    assert got["chart"]["(other)"] == "var(--gray)"
    assert got["agent"] == "var(--ec1)" and got["anyKind"] == "var(--ec1)"
    # a key the server does not know: the same general slot every time
    assert got["unknown"][0] == got["unknown"][1] in ("var(--ec0)", "var(--ec1)")
    # every palette color is defined for both themes
    light, dark = got["css"].split("\n")
    assert light.startswith(':root,[data-theme="light"]') and dark.startswith('[data-theme="dark"]')
    for i, (lo, hi) in enumerate(COLORS["palette"]):
        assert f"--ec{i}:{lo}" in light and f"--ec{i}:{hi}" in dark


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


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_verdict_labels() -> None:
    script = (
        FAKE_DOM
        + """
globalThis.document.querySelector = () => null;
const m = await import('./marks.js');
const labels = Object.entries(m.VERDICTS).map(([k, v]) => [k, v.label]);
console.log(JSON.stringify(Object.fromEntries(labels)));
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
    assert json.loads(out.stdout) == {
        "working": "Working",
        "no_effect": "No clear effect",
        "worse": "Worse",
        "too_early": "Too early",
        "no_data": "Can\u2019t tell",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_share_bar_inside_a_control_is_one_image_without_tab_stops() -> None:
    got = _node("""
const m = await import('./lib.js');
const parts = [{ color: 'red', share: 0.7, title: 'a', rows: [] },
  { color: 'blue', share: 0.3, title: 'b', rows: [] },
  { color: 'x', share: 0, title: 'c', rows: [] }];
const own = m.segBar(parts, { aria: 'own' });
const inner = m.segBar(parts, { focus: false, aria: 'inner' });
const tabs = (b) => b.children.map((c) => c.attrs.tabindex || null);
console.log(JSON.stringify({ own: [own.attrs.role, tabs(own)],
  inner: [inner.attrs.role, tabs(inner)] }));
""")
    # a part with no share is left out; on its own each part is a tab stop
    assert got["own"] == ["group", ["0", "0"]]
    assert got["inner"] == ["img", [None, None]]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_long_label_is_shortened_with_its_full_text_on_hover() -> None:
    deep = "/home/user/build/area/sub-one/sub-two/sub-three/sub-four/kittex"
    got = _node(f"""
const m = await import('./lib.js');
const deep = {json.dumps(deep)};
const spans = (el) => walk(el, []).filter((n) => (n.attrs.class || n.className) === 'lbl');
const leg = m.legend([{{ label: deep, color: 'red', value: '$1' }},
  {{ label: 'kittex', color: 'blue', value: '$2' }}]);
const bars = m.barList([{{ key: 'a', label: deep, usd: 3 }}], (r) => r.usd);
const chip = m.projectChip({{ project: deep, project_path: deep }});
const pick = (n) => ({{ text: n.textContent, title: n.attrs.title || null }});
console.log(JSON.stringify({{ legend: spans(leg).map(pick), bars: spans(bars).map(pick),
  chip: pick(chip), plain: m.clipLabel('short') }}));
""")
    short = "/home/user/build/area/sub-one/su…/kittex"  # 40 characters
    assert len(short) == 40
    assert got["legend"] == [{"text": short, "title": deep}, {"text": "kittex", "title": None}]
    assert got["bars"] == [{"text": short, "title": deep}]
    assert got["chip"] == {"text": short, "title": f"Project {deep}"}
    assert got["plain"] == "short"
