"""The shared tooltip of lib.js, run by node in a small fake DOM with events."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

# Elements keep attributes, children, classes and listeners; the tooltip element
# reports a fixed size; the viewport is 800 x 600.
FAKE_DOM = """
class Node {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.style = {}; this.listeners = {};
    this.hidden = false; this.textContent = '';
    const cls = new Set();
    this.classList = {
      add: (...c) => c.forEach((x) => cls.add(x)),
      has: (c) => cls.has(c), remove: (c) => cls.delete(c),
    };
  }
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === 'class') v.split(' ').forEach((c) => this.classList.add(c));
  }
  set className(v) { v.split(' ').forEach((c) => this.classList.add(c)); }
  append(...cs) { this.children.push(...cs); }
  // like the DOM: anything that is not a node becomes text (a stray null shows as "null")
  replaceChildren(...cs) {
    this.children = cs.map((c) => {
      if (c instanceof Node) return c;
      const t = new Node('#text');
      t.textContent = String(c);
      return t;
    });
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type, ev = {}) { (this.listeners[type] || []).forEach((fn) => fn({ target: this, ...ev })); }
  contains(n) { return n === this; }
  getBoundingClientRect() {
    return this.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
  }
  get offsetWidth() { return 160; }
  get offsetHeight() { return 60; }
}
globalThis.Node = Node;
const tipEl = new Node('div');
tipEl.hidden = true;
const docListeners = {};
globalThis.document = {
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t),
  createTextNode: (t) => { const n = new Node('#text'); n.textContent = t; return n; },
  getElementById: (id) => (id === 'tip' ? tipEl : null),
  addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
};
globalThis.window = { innerWidth: 800, innerHeight: 600, addEventListener() {} };
const text = (n) => (n.tag === '#text' ? n.textContent : (n.children || []).map(text).join(' '));
const fireDoc = (type, ev) => (docListeners[type] || []).forEach((fn) => fn(ev));
const m = await import('./lib.js');
"""


def _run(body: str) -> Any:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", FAKE_DOM + body],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_a_mark_gets_its_text_as_its_name_and_joins_the_tab_order() -> None:
    got = _run("""
const el = m.tipOn(new Node('span'), 'Cache reads', [
  { label: 'Tokens', value: '1.2M', color: 'red' }, { label: 'Share', value: '52%' },
  { note: '3 sessions' }]);
el.fire('focus');
console.log(JSON.stringify({
  attrs: el.attrs, mk: el.classList.has('mk'), shown: !tipEl.hidden, tip: text(tipEl) }));
""")
    assert got["attrs"]["tabindex"] == "0"
    assert got["attrs"]["aria-label"] == "Cache reads: Tokens 1.2M; Share 52%; 3 sessions"
    assert got["mk"] and got["shown"]
    for part in ("Cache reads", "Tokens", "1.2M", "52%", "3 sessions"):
        assert part in got["tip"]
    assert "null" not in got["tip"] and "undefined" not in got["tip"]


@needs_node
def test_a_native_control_keeps_its_own_name_and_a_hover_only_mark_stays_out() -> None:
    got = _run("""
const a = m.tipOn(new Node('a'), 'Oct 20: $5', [],
  { native: true, aria: 'Oct 20: $5, open the day' });
const cell = m.tipOn(new Node('span'), 'Mon 09:00', [{ label: 'Spent', value: '$1' }],
  { focus: false });
console.log(JSON.stringify({ a: a.attrs, cell: cell.attrs }));
""")
    assert got["a"] == {"aria-label": "Oct 20: $5, open the day"}
    assert got["cell"] == {}


@needs_node
def test_focus_anchors_above_the_mark_and_escape_closes() -> None:
    got = _run("""
const el = m.tipOn(new Node('span'), 'A', [{ label: 'x', value: '1' }]);
el.rect = { left: 300, top: 200, right: 340, bottom: 210, width: 40, height: 10 };
el.fire('focus');
const at = { left: tipEl.style.left, top: tipEl.style.top, shown: !tipEl.hidden };
fireDoc('keydown', { key: 'Escape' });
console.log(JSON.stringify({ at, after: !tipEl.hidden }));
""")
    # centered on the mark (320 - 160 / 2), 8 px above it (200 - 60 - 8)
    assert got["at"] == {"left": "240px", "top": "132px", "shown": True}
    assert got["after"] is False


@needs_node
def test_a_tap_shows_it_and_a_tap_elsewhere_hides_it() -> None:
    got = _run("""
const el = m.tipOn(new Node('span'), 'A', [{ label: 'x', value: '1' }]);
el.rect = { left: 10, top: 300, right: 30, bottom: 310, width: 20, height: 10 };
el.fire('pointermove', { pointerType: 'touch', clientX: 15, clientY: 305 });
const beforeTap = !tipEl.hidden;
el.fire('pointerdown', { pointerType: 'touch' });
const tapped = !tipEl.hidden;
fireDoc('pointerdown', { target: el });
const sameMark = !tipEl.hidden;
fireDoc('pointerdown', { target: new Node('div') });
console.log(JSON.stringify({ beforeTap, tapped, sameMark, after: !tipEl.hidden }));
""")
    assert got == {"beforeTap": False, "tapped": True, "sameMark": True, "after": False}


@needs_node
def test_the_pointer_tooltip_flips_at_the_edges_and_stays_inside() -> None:
    got = _run("""
console.log(JSON.stringify([
  m.placeTip(100, 100, 160, 60, 800, 600),
  m.placeTip(700, 100, 160, 60, 800, 600),
  m.placeTip(100, 580, 160, 60, 800, 600),
  m.placeTip(5, 5, 900, 60, 800, 600),
  m.placeTipAt({ left: 780, top: 20, right: 790, bottom: 30, width: 10, height: 10 },
    160, 60, 800, 600),
]));
""")
    assert got[0] == {"left": 114, "top": 114, "flipX": False, "flipY": False}
    # past the right edge: to the pointer's left
    assert got[1]["flipX"] and got[1]["left"] == 700 - 160 - 14
    # past the bottom: above the pointer
    assert got[2]["flipY"] and got[2]["top"] == 580 - 60 - 14
    # wider than the viewport: pinned to the left margin
    assert got[3]["left"] == 4
    # anchored near the top right: below the mark, clamped inside the right edge
    assert got[4]["flipY"] and got[4]["top"] == 38 and got[4]["left"] == 800 - 160 - 4


@needs_node
def test_a_segmented_bar_makes_one_mark_per_part_and_skips_empty_ones() -> None:
    got = _run("""
const bar = m.segBar([
  { color: 'red', share: 0.7, title: 'Reads', rows: [{ label: 'Share', value: '70%' }] },
  { color: 'blue', share: 0, title: 'Input', rows: [] },
  { color: 'green', share: 0.3, title: 'Writes', rows: [{ label: 'Share', value: '30%' }] },
], { aria: 'Tokens by kind' });
console.log(JSON.stringify({
  role: bar.attrs.role, name: bar.attrs['aria-label'], hl: bar.classList.has('hl'),
  parts: bar.children.map((c) => c.attrs['aria-label']) }));
""")
    assert got["role"] == "group" and got["name"] == "Tokens by kind" and got["hl"]
    assert got["parts"] == ["Reads: Share 70%", "Writes: Share 30%"]
