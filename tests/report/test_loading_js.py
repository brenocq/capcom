"""static/loading.js, run by node: the number tween and the job-following fetch."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(body: str) -> object:
    script = f"const m = await import('./loading.js');\n{body}\n"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_tween_keeps_the_format() -> None:
    got = _run(
        "console.log(JSON.stringify(["
        "m.tweenStep('$186.53', '$568.80', 0), m.tweenStep('$186.53', '$568.80', 1),"
        "m.tweenStep('$1,189', '$2,400', 0.5), m.tweenStep('14', '172', 0.5),"
        "m.tweenStep('62%', '80%', 0.25), m.tweenStep('$3.5', '12 min', 0.5),"
        "m.tweenStep('Loading', '$1', 0.5)]))"
    )
    assert isinstance(got, list)
    assert got[0] == "$186.53" and got[1] == "$568.80"
    assert got[2].startswith("$2,") and "," in got[2]  # grouped, between the two
    assert 14 < int(got[3]) < 172
    assert got[4].endswith("%") and 62 <= int(got[4][:-1]) <= 80
    assert got[5] is None and got[6] is None  # not numbers alike: swapped, not tweened


def test_ease_out_is_monotonic_and_bounded() -> None:
    got = _run("console.log(JSON.stringify([...Array(11).keys()].map((i) => m.easeOut(i / 10))))")
    assert isinstance(got, list)
    assert got[0] == 0 and got[-1] == 1 and got == sorted(got)
    assert got[3] > 0.3  # ease-out: fast first


def test_get_progressive_follows_the_job() -> None:
    got = _run(
        """
const replies = [
  { status: 202, body: { status: 'computing', job: 'j1', progress: 0.2,
    source: null, partial: null } },
  { status: 202, body: { status: 'computing', job: 'j1', progress: 0.6,
    source: 'chunks', partial: { count: 3 } } },
  { status: 200, body: { count: 5 } },
];
const urls = [], seen = [];
const fetchFn = async (url) => { urls.push(url); const r = replies.shift();
  return { status: r.status, ok: r.status < 300, json: async () => r.body }; };
const out = await m.getProgressive('spikes?days=30', { fetchFn, pollMs: 1,
  onPartial: (p, progress, source) => seen.push([p, progress, source]) });
console.log(JSON.stringify({ out, urls, seen }));
"""
    )
    assert isinstance(got, dict)
    assert got["out"] == {"count": 5}
    assert got["urls"] == [
        "/api/spikes?days=30",
        "/api/spikes?days=30&job=j1",
        "/api/spikes?days=30&job=j1",
    ]
    assert got["seen"] == [[None, 0.2, None], [{"count": 3}, 0.6, "chunks"]]


def test_tween_mixed_moves_every_number_of_a_text() -> None:
    got = _run(
        "console.log(JSON.stringify(["
        "m.tweenMixed('12 of 30 spikes', '40 of 30 spikes', 0.5),"
        "m.tweenMixed('width:40.0%;background:var(--s1)', 'width:80.0%;background:var(--s1)', 1, true),"
        "m.tweenMixed('M0 4.5L12 9', 'M0 8.5L20 9', 0, true),"
        "m.tweenMixed('Oct 21, 14:30', 'Oct 21, 15:05', 0.5),"
        "m.tweenMixed('3 spikes', '3 spikes in 2 projects', 0.5),"
        "m.tweenMixed('$1,189 more', '$2,400 more', 0.5)]))"
    )
    assert isinstance(got, list)
    assert got[0].endswith(" of 30 spikes") and 12 < int(got[0].split()[0]) < 40
    assert got[1] == "width:80.0%;background:var(--s1)"
    assert got[2] == "M0 4.5L12 9"
    assert got[3] is None  # a clock time is swapped, not counted through
    assert got[4] is None  # different words: swapped
    assert got[5].startswith("$2,") and got[5].endswith(" more")
