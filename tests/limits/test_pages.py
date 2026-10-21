# ruff: noqa: E501  (the scripts run by node keep their lines whole)
"""The Overview's limit cards and the limits chart, drawn by node in a minimal fake DOM
on synthetic payloads (the shapes /api/limits returns)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

FAKE_DOM = """
class Node {
  constructor(tag, data) { this.tag = tag; this.attrs = {}; this.children = []; this.data = data; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...cs) { this.children.push(...cs); }
  addEventListener() {}
}
globalThis.Node = Node;
globalThis.document = {
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t),
  createTextNode: (t) => new Node('#text', t),
};
const walk = (n, out) => { out.push(n); (n.children || []).forEach((c) => walk(c, out)); return out; };
const texts = (el) => walk(el, []).map((n) => n.data ?? n.textContent).filter(Boolean).join(' | ');
const paths = (el) => walk(el, []).filter((n) => n.tag === 'path').map((n) => n.attrs);
"""

H = 3600e3
T0 = 1_792_000_000_000  # epoch ms
WINDOW = {
    "window": "five_hour",
    "label": "5-hour window",
    "samples": 5,
    "current": True,
    "reading": {
        "percent": 40,
        "time": "2026-10-21T12:00:00+00:00",
        "resets_at": "2026-10-21T14:00:00+00:00",
        "source": "statusline",
    },
    "fit": {
        "method": "per_usd",
        "note": "one rate per dollar of API prices, from 4 intervals: too few samples",
        "percent_per_usd": 1.5,
        "intervals": 4,
        "mean_abs_error": 0.8,
        "r2": None,
        "weights": [],
    },
    "forecast": {
        "reading": 40,
        "now": 42,
        "median": 70,
        "low": 60,
        "high": 104,
        "resets_at": "2026-10-21T14:00:00+00:00",
        "runs_out_at": "2026-10-21T13:50:00+00:00",
        "bucket_minutes": 15,
        "buckets": 12,
        "fan": [[T0, 42, 42, 42], [T0 + H, 55, 50, 70], [T0 + 2 * H, 70, 60, 104]],
    },
    "reason": None,
    "history": [[T0 - 3 * H, 0], [T0 - H, 30], [T0 - 60e3, 40]],
    "start": "2026-10-21T09:00:00+00:00",
}


def node(script: str) -> Any:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", FAKE_DOM + script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_card_empty_state() -> None:
    got = node(
        """
const m = await import('./limits.js');
const el = m.limitCard('5-hour window', null, { available: false, install: 'capcom limits --install' });
console.log(JSON.stringify(texts(el)));
"""
    )
    assert "No data yet" in got and "Not recording yet" in got
    assert "capcom limits --install" in got and "milestone" not in got


@needs_node
def test_card_with_a_forecast_draws_the_fan() -> None:
    got = node(
        f"""
const m = await import('./limits.js');
const w = {json.dumps(WINDOW)};
const el = m.limitCard('5-hour window', w, {{ available: true, recording: true }});
console.log(JSON.stringify({{ text: texts(el), paths: paths(el) }}));
"""
    )
    text = got["text"]
    assert "42%" in text and "At the reset, 70%" in text and "60% to 104%" in text
    assert "runs out around" in text and "Not recording" not in text
    fills = [p for p in got["paths"] if str(p.get("fill", "")).startswith("url(#fan")]
    assert len(fills) == 1  # the 80 % band
    dashed = [p for p in got["paths"] if p.get("stroke-dasharray") == "6 4"]
    assert len(dashed) == 1  # the median


@needs_node
def test_chart_drops_to_zero_at_each_reset() -> None:
    got = node(
        f"""
const m = await import('./limits.js');
const segs = [
  {{ points: [[{T0}, 0], [{T0 + H}, 50]], resets_at: new Date({T0 + 5 * H}).toISOString(), reset: true }},
  {{ points: [[{T0 + 6 * H}, 0], [{T0 + 7 * H}, 20]], resets_at: null, reset: false }},
];
const el = m.limitChart({{ segments: segs, fan: [], now: {T0 + 7 * H}, x0: {T0}, x1: {T0 + 8 * H} }});
console.log(JSON.stringify(paths(el)));
"""
    )
    drops = [p for p in got if p.get("stroke-dasharray") == "3 3"]
    assert len(drops) == 1  # one reset in view
    lines = [p for p in got if p.get("style") == "stroke:var(--text)"]
    assert len(lines) == 2
