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
        "note": "About $0.67 of API-equivalent spend per 1% of your 5-hour window, from 2 rises (rough).",
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
        "runs_out_early": "2026-10-21T13:10:00+00:00",
        "runs_out_late": None,
        "rough": True,
        "readings": 2,
        "rate_factor": 1.8,
        "bucket_minutes": 15,
        "buckets": 12,
        "fan": [[T0, 42, 42, 42], [T0 + H, 55, 50, 70], [T0 + 2 * H, 70, 60, 104]],
    },
    "reason": None,
    "history": [[T0 - 3 * H, 0], [T0 - H, 30], [T0 - 60e3, 40]],
    "start": "2026-10-21T09:00:00+00:00",
    "start_known": True,
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
    assert "42%" in text and "At the reset, 70%; 80% range 60% to the limit." in text
    assert "runs out around" in text and "Not recording" not in text
    fills = [p for p in got["paths"] if "fan-fill" in str(p.get("style", ""))]
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


@needs_node
def test_card_says_rough_and_the_run_out_range() -> None:
    got = node(
        f"""
const m = await import('./limits.js');
const w = {json.dumps(WINDOW)};
const lim = {{ available: true, recording: true, recording_since: new Date({T0} - 2 * {H}).toISOString() }};
const el = m.limitCard('5-hour window', w, lim);
const fc = {{ ...w.forecast, runs_out_late: '2026-10-21T13:58:00+00:00' }};
const at = (iso) => iso.slice(11, 16);
console.log(JSON.stringify({{ text: texts(el), range: m.runsOut(fc, at), low: m.runsOut({{ runs_out_early: fc.runs_out_early }}, at) }}));
"""
    )
    text = got["text"]
    assert "Rough: 2 readings so far, so the range is wide; it narrows as readings come." in text
    assert "; at a slower pace it lasts until the reset." in text and "as early as" in text
    assert (
        "About $0.67 of API-equivalent spend per 1% of your 5-hour window, from 2 rises (rough)."
        in text
    )
    assert "Fit:" not in text and "intervals" not in text
    assert got["range"] == "At this pace it runs out between 13:10 and 13:58, most likely 13:50."
    assert got["low"] == "It may run out before the reset, as early as 13:10 if the pace picks up."


@needs_node
def test_readings_are_dots_and_the_unseen_start_is_dashed() -> None:
    got = node(
        f"""
const m = await import('./limits.js');
const segs = [{{ points: [[{T0}, 0], [{T0 + 3 * H}, 30], [{T0 + 4 * H}, 45]], resets_at: null, reset: false, start_known: true }}];
const el = m.limitChart({{ segments: segs, fan: [], now: {T0 + 4 * H}, x0: {T0}, x1: {T0 + 5 * H}, since: {T0 + 2 * H} }});
const nodes = walk(el, []);
console.log(JSON.stringify({{
  paths: paths(el),
  dots: nodes.filter((n) => n.className === 'rdot').length,
  labels: nodes.filter((n) => n.className === 'gaplab').map((n) => n.textContent),
}}));
"""
    )
    assert got["dots"] == 2  # the two readings, not the window's start
    assert got["labels"] == ["before recording"]
    unseen = [p for p in got["paths"] if p.get("stroke-dasharray") == "4 4"]
    assert len(unseen) == 1 and unseen[0]["style"] == "stroke:var(--muted)"
    solid = [p for p in got["paths"] if p.get("style") == "stroke:var(--text)"]
    assert len(solid) == 1 and solid[0]["stroke-width"] == "2"
    assert solid[0]["d"].count("L") == 1  # from the first reading on


@needs_node
def test_day_tabs_follow_the_recorded_history() -> None:
    got = node(
        """
globalThis.location = { search: '' };
const m = await import('./limits.js');
const now = 1_792_000_000_000, day = 864e5;
const short = m.dayTabs(14, now - 2 * day, now);
const tabs = m.dayTabs(14, now - 10 * day, now);
const buttons = tabs.children.map((b) => ({ text: b.children[0].data, off: b.attrs['aria-disabled'] || null, title: b.attrs.title || null }));
console.log(JSON.stringify({ short, buttons }));
"""
    )
    assert got["short"] is None  # a shorter history than the shortest range: no tabs
    offs = {b["text"]: b["off"] for b in got["buttons"]}
    assert offs == {"7 days": None, "14 days": None, "30 days": "true", "90 days": "true"}
    assert "Recording began 10 days ago" in got["buttons"][2]["title"]


@needs_node
def test_past_the_limit_the_card_says_when_not_how_far() -> None:
    w = {
        **WINDOW,
        "forecast": {
            **WINDOW["forecast"],
            "median": 366,
            "low": 297,
            "high": 454,
            "fan": [[T0, 42, 42, 42], [T0 + 2 * H, 366, 297, 454]],
        },
    }
    got = node(
        f"""
const m = await import('./limits.js');
const el = m.limitCard('Weekly limit', {json.dumps(w)}, {{ available: true, recording: true }});
console.log(JSON.stringify({{ text: texts(el), labels: walk(el, []).filter((n) => n.className === 'ylab').map((n) => n.textContent) }}));
"""
    )
    assert "At this pace it reaches the limit before the reset." in got["text"]
    assert "366" not in got["text"] and "454" not in got["text"]
    assert got["labels"] == ["0%", "50%", "100%"]  # the readings keep the height
