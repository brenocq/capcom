"""static/sessions.js, run by node with a tiny stand-in DOM: the sessions table's
column headers cycle descending, ascending, then no sort."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# Elements keep their tag, attributes, children and listeners; enough for h().
PRELUDE = """
class Node {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.on = {}; this.text = '';
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { this.on[t] = f; }
  append(...cs) { this.children.push(...cs); }
  set textContent(v) { this.text = v; }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  set className(v) { this.attrs.class = v; }
  all(tag) {
    return [this.tag === tag ? [this] : [], ...this.children.map((c) => c.all(tag))].flat();
  }
}
class Text extends Node { constructor(t) { super('#text'); this.text = t; } }
globalThis.Node = Node;
globalThis.document = {
  createElement: (t) => new Node(t), createElementNS: (_, t) => new Node(t),
  createTextNode: (t) => new Text(t), querySelector: () => null, addEventListener: () => {} };
globalThis.window = { addEventListener: () => {}, dispatchEvent: () => {},
  matchMedia: () => ({ matches: false, addEventListener: () => {} }) };
globalThis.location = { search: '', pathname: '/sessions' };
const m = await import('./sessions.js');
const rows = [{ session_id: 's1abcdefgh', title: 'A', project: 'p', subagents: 1,
  active_seconds: 60, peak_context: 1000, cache_read_share: 0.5,
  last: '2026-10-01T10:00:00Z', usd: 1.5 }];
const heads = (sort, dir, onSort) => m.sessionsTable(rows, sort, onSort, dir).all('th')
  .map((th) => ({ label: th.textContent, sort: th.getAttribute('aria-sort'),
    buttons: th.all('button').length, click: () => th.all('button')[0].on.click() }));
"""


def _run(body: str) -> object:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", f"{PRELUDE}\n{body}\n"],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_next_sort_cycles_descending_ascending_then_none() -> None:
    got = _run(
        "console.log(JSON.stringify([m.nextSort(null, null, 'usd'),"
        "m.nextSort('usd', 'desc', 'usd'),"
        "m.nextSort('usd', 'asc', 'usd'), m.nextSort('usd', 'asc', 'last'),"
        "m.nextSort('last', 'desc', 'usd')]))"
    )
    assert got == [
        {"sort": "usd", "dir": "desc"},
        {"sort": "usd", "dir": "asc"},
        {"sort": None, "dir": None},
        {"sort": "last", "dir": "desc"},
        {"sort": "usd", "dir": "desc"},
    ]


def test_headers_show_the_direction_and_click_to_the_next_state() -> None:
    got = _run(
        "const calls = []; const on = (k, d) => calls.push([k, d]);"
        "const pick = (hs, l) => hs.find((x) => x.label.replace(/[↑↓]$/, '') === l);"
        "const desc = heads('usd', 'desc', on), asc = heads('usd', 'asc', on);"
        "const none = heads(null, 'desc', on);"
        "pick(desc, 'Cost').click(); pick(asc, 'Cost').click(); pick(none, 'Cost').click();"
        "pick(asc, 'Active').click();"
        "const show = (hs) => hs.map((x) => [x.label, x.sort, x.buttons]);"
        "console.log(JSON.stringify({ desc: show(desc), asc: show(asc), none: show(none), calls }))"
    )
    assert isinstance(got, dict)
    assert got["desc"][-1] == ["Cost↓", "descending", 1]
    assert got["asc"][-1] == ["Cost↑", "ascending", 1]
    assert got["none"][-1] == ["Cost", "none", 1]
    # the other sortable headers are "none" without an arrow; the plain ones have no button
    assert ["Active", "none", 1] in got["desc"]
    assert got["desc"][0] == ["Session", None, 0]
    assert ["Prompts", "none", 1] in got["desc"]
    assert ["Cost / active hour", "none", 1] in got["desc"]
    assert got["calls"] == [
        ["usd", "asc"],
        [None, None],
        ["usd", "desc"],
        ["active_seconds", "desc"],
    ]


def test_without_on_sort_the_headers_are_plain_text() -> None:
    got = _run(
        "console.log(JSON.stringify(m.sessionsTable(rows).all('th')"
        ".map((th) => [th.getAttribute('aria-sort'), th.all('button').length])))"
    )
    assert isinstance(got, list)
    assert all(g == [None, 0] for g in got)
