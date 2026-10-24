"""static/session.js, run by node: how wide the session timeline's plot is, and where
the pause pills go on it (never over each other)."""

from __future__ import annotations

import json
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(body: str) -> object:
    script = f"const m = await import('./session.js');\n{body}\n"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_the_plot_grows_with_active_time_between_a_floor_and_a_cap() -> None:
    got = _run(
        "console.log(JSON.stringify({ tl: m.TL, w: [[20, 0], [600, 0], [1200, 0],"
        " [1200, 10], [10000, 60], [1e6, 0]].map(([a, g]) => m.plotWidth(a, g)) }))"
    )
    assert isinstance(got, dict)
    tl, w = got["tl"], got["w"]
    short, eight_h, day_long, with_gaps, week, huge = w
    assert short == tl["min"]  # a short session fits the card
    assert eight_h <= 900  # an 8-hour session still fits a wide card
    assert day_long > eight_h and with_gaps > day_long  # more active time, more width
    assert week > 10_000  # a week of active time scrolls for real
    assert huge == tl["max"]


PILLS = """
const pills = [
  { mid: 0.50, minutes: 40, full: '40 min', short: '40m' },
  { mid: 0.51, minutes: 90, full: '1 h 30', short: '2 h' },
  { mid: 0.52, minutes: 31, full: '31 min', short: '31m' },
  { mid: 0.995, minutes: 35, full: '35 min', short: '35m' },
  { mid: 0.0, minutes: 600, full: '10 h', short: '10 h' },
];
const tw = (t) => t.length * 6.5;
const width = W;
const at = m.placePills(pills, width, tw);
console.log(JSON.stringify(at.map((a, i) => a && { ...a, w: tw(a.text) + 16, i })));
"""


@pytest.mark.parametrize("width", [300, 900, 4000])
def test_pause_pills_never_overlap_and_stay_inside(width: int) -> None:
    got = _run(PILLS.replace("W;", f"{width};"))
    assert isinstance(got, list)
    shown = [p for p in got if p]
    assert shown, "at least the longest pause gets a pill"
    assert any(p["i"] == 4 for p in shown)  # the longest is placed first
    spans = sorted((p["left"], p["left"] + p["w"]) for p in shown)
    for (_, b), (c, _) in pairwise(spans):
        assert c >= b, spans
    assert all(a >= 0 and b <= width + 1e-6 for a, b in spans)


def test_a_crowded_pill_is_shortened_before_it_is_dropped() -> None:
    got = _run(
        "console.log(JSON.stringify(m.placePills(["
        "{ mid: 0.5, minutes: 90, full: '1 h 30', short: '2 h' },"
        "{ mid: 0.5 + 55 / 1000, minutes: 45, full: '45 min', short: '45m' },"
        "{ mid: 0.5 + 20 / 1000, minutes: 31, full: '31 min', short: '31m' }],"
        " 1000, (t) => t.length * 6)))"
    )
    assert isinstance(got, list)
    assert got[0] == {"text": "1 h 30", "left": 500 - (36 + 16) / 2}
    assert got[1] is not None and got[1]["text"] == "45m"  # its whole label did not fit
    assert got[2] is None  # no room left at all


def test_a_plot_slightly_wider_than_its_room_is_squeezed_in_a_longer_one_scrolls() -> None:
    got = _run(
        "const three_h = m.plotWidth(180, 3), week = m.plotWidth(10000, 60);"
        "console.log(JSON.stringify([m.plotFloor(three_h, 205), m.plotFloor(three_h, 120),"
        " m.plotFloor(week, 880), m.plotFloor(500, 880), three_h, week]))"
    )
    assert isinstance(got, list)
    phone, tiny, wide_week, wide_short, three_h, week = got
    assert phone == 0  # a three-hour session fits a phone's plot without scrolling
    assert tiny == three_h  # far too narrow a room: the plot keeps its width and scrolls
    assert wide_week == week
    assert wide_short == 0
