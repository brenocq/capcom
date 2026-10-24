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
    for row in (0, 1):
        spans = sorted((p["left"], p["left"] + p["w"]) for p in shown if p["row"] == row)
        for (_, b), (c, _) in pairwise(spans):
            assert c >= b, spans
    spans = [(p["left"], p["left"] + p["w"]) for p in shown]
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
    assert got[0] == {"text": "1 h 30", "left": 500 - (36 + 16) / 2, "row": 0}
    # its whole label did not fit on the first line, its short one did
    assert got[1] is not None and got[1]["text"] == "45m" and got[1]["row"] == 0
    assert got[2] is not None and got[2]["row"] == 1  # no room left: the second line


def test_two_long_pauses_a_few_pixels_apart_both_show_their_length() -> None:
    got = _run(
        "const pills = [{ mid: 0.400, minutes: 131, full: '2 h 11', short: '2 h' },"
        " { mid: 0.404, minutes: 95, full: '1 h 35', short: '2 h' }];"
        "const tw = (t) => t.length * 6;"
        "console.log(JSON.stringify([1000, 16000].map((w) => m.placePills(pills, w, tw))))"
    )
    assert isinstance(got, list)
    near, zoomed = got
    assert [p["row"] for p in near] == [0, 1]  # the shorter pause goes under it
    assert [p["text"] for p in near] == ["2 h 11", "1 h 35"]
    assert [p["row"] for p in zoomed] == [0, 0]  # zoomed in, both fit on one line


def test_a_pill_is_dropped_only_when_neither_line_has_room() -> None:
    got = _run(
        "const pills = [0, 1, 2].map((k) => ({ mid: 0.5 + k / 1000, minutes: 90 - k,"
        " full: `${k} h 30`, short: `${k} h` }));"
        "console.log(JSON.stringify(m.placePills(pills, 1000, (t) => t.length * 6)))"
    )
    assert isinstance(got, list)
    assert [p and p["row"] for p in got] == [0, 1, None]


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


MIN = 60_000


def _rate(t: list[float], usd: list[float], start: float, end: float, gaps: list[dict]) -> list:
    got = _run(
        f"console.log(JSON.stringify(m.spendRate({json.dumps(t)}, {json.dumps(usd)},"
        f" {start}, {end}, {json.dumps(gaps)})))"
    )
    assert isinstance(got, list)
    return got


def _area(run: dict) -> float:
    """Dollars under one run's curve ($/h over ms)."""
    pts = list(zip(run["t"], run["rate"], strict=True))
    return sum((b - a) * (u + v) / 2 for (a, u), (b, v) in pairwise(pts)) / 3_600_000


def test_a_steady_pace_reads_as_its_hourly_rate() -> None:
    # $0.10 a minute for two hours: $6/h away from the edges
    t = [i * MIN for i in range(121)]
    (run,) = _rate(t, [0.1] * 121, 0, 120 * MIN, [])
    middle = [v for tm, v in zip(run["t"], run["rate"], strict=True) if 20 * MIN < tm < 100 * MIN]
    assert middle and all(abs(v - 6) < 0.05 for v in middle)


def test_each_run_holds_its_own_cost_and_a_pause_is_not_bridged() -> None:
    # a burst just before a two-hour pause, a slow trickle after it
    t = [0, 5 * MIN, 9 * MIN, 10 * MIN, 130 * MIN, 150 * MIN, 170 * MIN]
    usd = [0.5, 0.5, 3.0, 4.0, 0.1, 0.1, 0.1]
    gaps = [{"start": 10 * MIN, "end": 130 * MIN}]
    before, after = _rate(t, usd, 0, 170 * MIN, gaps)
    assert before["end"] == 10 * MIN and after["start"] == 130 * MIN
    assert _area(before) == pytest.approx(8.0, rel=0.02)
    assert _area(after) == pytest.approx(0.3, rel=0.02)
    # the burst's high rate stops at the pause: the first minutes after it are slow
    assert max(before["rate"]) > 20
    assert all(v < 2 for v in after["rate"])


def test_a_lone_request_between_pauses_still_shows() -> None:
    t = [0, 60 * MIN, 120 * MIN]
    gaps = [{"start": 0, "end": 60 * MIN}, {"start": 60 * MIN, "end": 120 * MIN}]
    runs = _rate(t, [1.0, 2.0, 1.0], 0, 120 * MIN, gaps)
    assert len(runs) == 3
    alone = runs[1]
    assert alone["end"] - alone["start"] == MIN  # drawn a minute long
    assert _area(alone) == pytest.approx(2.0, rel=0.02)
    assert sum(_area(r) for r in runs) == pytest.approx(4.0, rel=0.02)


def test_no_requests_no_rate() -> None:
    (run,) = _rate([], [], 0, 30 * MIN, [])
    assert run["rate"] and all(v == 0 for v in run["rate"])


def test_a_preset_shows_its_hours_of_active_time_per_screen() -> None:
    got = _run(
        "const a = 30 * 60, gaps = 3, room = 900;"
        "const w = m.zoomWidth(4, a, gaps, room);"
        "console.log(JSON.stringify({ w, gap: m.TL.gap, back: m.zoomHours(w, a, gaps, room),"
        " fit: m.zoomWidth('fit', a, gaps, room), one: m.zoomWidth(1, a, gaps, room),"
        " short: m.zoomWidth(12, 120, gaps, room),"
        " closest: m.zoomWidth(m.ZOOM_MIN, a, gaps, room),"
        " clamped: [m.zoomClamp(100, room, 5e5), m.zoomClamp(9e5, room, 5e5),"
        " m.zoomClamp(2000, room, 5e5), m.zoomClamp(2000, room, 10)] }))"
    )
    assert isinstance(got, dict)
    # 30 h of active time at 4 h per screen: 7.5 screens of active time, plus the pauses
    assert got["w"] == pytest.approx(900 * 7.5 + 3 * got["gap"])
    assert got["back"] == pytest.approx(4)
    assert got["fit"] == 900
    assert got["one"] == pytest.approx(900 * 30 + 3 * got["gap"])  # no cap: 30 screens
    assert got["closest"] == pytest.approx(900 * 120 + 3 * got["gap"])
    assert got["short"] < 900  # a 2 h session fits at 12 h a screen
    assert got["clamped"] == [900, 5e5, 2000, 900]


# a 40 h span with 30 h of active time: runs of 10 h, 5 h and 15 h between two pauses
_AXIS = (
    "const H = 3600e3, gaps = [{ start: 10 * H, end: 14 * H }, { start: 19 * H, end: 25 * H }];"
    "const ax = m.timeAxis(0, 40 * H, gaps);"
)


def test_a_squeezed_pause_keeps_its_pixels_at_every_zoom() -> None:
    got = _run(
        _AXIS + "const room = 900, n = ax.gaps.length;"
        "const widths = [m.zoomWidth('fit', ax.activeMinutes, n, room),"
        " m.zoomWidth(12, ax.activeMinutes, n, room)];"
        "console.log(JSON.stringify({ gap: m.TL.gap, widths, bands: widths.map((w) =>"
        " gaps.map((g) => ax(g.end, w) - ax(g.start, w))) }))"
    )
    assert isinstance(got, dict)
    fit, twelve = got["widths"]
    assert twelve > fit  # 12 h a screen is zoomed in from Fit all
    for bands in got["bands"]:
        assert bands == pytest.approx([got["gap"], got["gap"]])


def test_many_pauses_take_at_most_their_share_of_the_plot() -> None:
    # 62 pauses at 14 px would fill 868 of 900 px; they narrow to leave the activity room
    got = _run(
        "const room = 900, n = 62, w = m.zoomWidth('fit', 3000, n, room);"
        "console.log(JSON.stringify({ share: m.TL.gapShare, used: n * m.gapPx(w, n) / w,"
        " wide: m.zoomWidth(12, 3000, n, room) }))"
    )
    assert isinstance(got, dict)
    assert got["used"] == pytest.approx(got["share"])
    assert got["share"] <= 0.3
    assert got["wide"] > 900  # zoomed in, the pauses get back their full width


def test_active_time_has_one_scale_across_the_runs_at_every_width() -> None:
    # the same pixels per minute in each run, so a run's spend-rate area stays in step
    # with its cost however far the plot is zoomed
    got = _run(
        _AXIS + "console.log(JSON.stringify([900, 2500, 16000].map((w) => ["
        " [0, 10], [14, 19], [25, 40]].map(([a, b]) => (ax(b * H, w) - ax(a * H, w)) / (b - a))"
        ".concat([ax(40 * H, w)]))))"
    )
    assert isinstance(got, list)
    for w, row in zip([900, 2500, 16000], got, strict=True):
        *per_hour, right = row
        assert per_hour == pytest.approx([per_hour[0]] * 3)
        assert per_hour[0] * 30 == pytest.approx(w - 2 * 14)
        assert right == pytest.approx(w)


def test_zooming_keeps_the_time_under_the_pointer_in_place() -> None:
    got = _run(
        _AXIS + "const lab = 160, from = 2000, to = 5000, scroll = 300, offset = 500;"
        "const s2 = m.zoomScroll(scroll, offset, lab, ax, from, to);"
        "const before = ax.time(scroll + offset - lab, from);"
        "const after = ax.time(s2 + offset - lab, to);"
        "const o = lab + ax(12 * H, from), s3 = m.zoomScroll(0, o, lab, ax, from, to);"
        "console.log(JSON.stringify([before, after, ax.time(s3 + o - lab, to) / H,"
        " m.zoomScroll(0, 170, lab, ax, 1000, 500)]))"
    )
    assert isinstance(got, list)
    before, after, in_gap, at_start = got
    assert after == pytest.approx(before)  # the same moment stays under the pointer
    assert in_gap == pytest.approx(12)  # inside a squeezed pause too
    assert at_start == 0  # never scrolled before the start


def test_the_zoom_round_trips_through_the_url() -> None:
    got = _run(
        "console.log(JSON.stringify([m.zoomParam('fit'), m.zoomParam(4), m.zoomParam(2.4567),"
        " m.zoomParam(null), m.parseZoom('fit'), m.parseZoom('12h'), m.parseZoom('2.5h'),"
        " m.parseZoom('0h'), m.parseZoom('wide'), m.parseZoom(null)]))"
    )
    assert got == ["fit", "4h", "2.5h", None, "fit", 12, 2.5, None, None, None]


def test_only_the_view_and_a_screen_on_either_side_is_drawn() -> None:
    got = _run(
        "console.log(JSON.stringify([m.drawWindow(0, 900, 100000),"
        " m.drawWindow(50000, 900, 100000),"
        " m.drawWindow(99100, 900, 100000), m.drawWindow(0, 900, 700),"
        " m.drawWindow(200000, 900, 100000, 100)]))"
    )
    assert got == [
        [0, 1800],  # at the start: nothing to draw before it
        [49100, 51800],
        [98200, 100000],  # at the end
        [0, 700],  # a plot narrower than the view is drawn whole
        [99900, 100000],  # scrolled past the end (mid-zoom): the end
    ]


def test_a_window_slices_the_points_in_it_and_one_past_each_edge() -> None:
    got = _run(
        "const ts = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90], px = (t) => t * 10;"
        "console.log(JSON.stringify([m.sliceWindow(ts, px, 250, 520),"
        " m.sliceWindow(ts, px, 0, 900),"
        " m.sliceWindow(ts, px, -50, 5), m.sliceWindow(ts, px, 950, 2000),"
        " m.sliceWindow(ts, px, 210, 290), m.sliceWindow([], px, 0, 100)]))"
    )
    assert got == [
        [2, 7],  # 30, 40 and 50 are inside; 20 and 60 carry the line past the edges
        [0, 10],
        [0, 2],
        [9, 10],  # past the last point: only it, to end the line at
        [2, 4],  # no point inside: the two around it, so the line still crosses
        [0, 0],
    ]
