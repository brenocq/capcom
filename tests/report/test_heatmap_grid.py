"""The heatmap's past-seven-days grid (heatmap.js), built by node in a daylight-saving
zone on synthetic hours."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import pytest

from tests.report.test_charts import FAKE_DOM, STATIC

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def grid(now: str, hours: list[dict[str, Any]], tz: str = "Europe/Berlin") -> Any:
    script = (
        FAKE_DOM
        + f"""
const m = await import('./heatmap.js');
const g = m.pastWeekGrid({json.dumps(hours)}, Date.parse({json.dumps(now)}));
console.log(JSON.stringify({{
  max: g.max, total: g.total,
  rows: g.rows.map((r) => ({{
    label: r.label, today: r.today, midnight: r.date.getHours() + r.date.getMinutes(),
    cells: r.cells.map((c) => ({{
      starts: c.starts.map((t) => new Date(t).toISOString()), usd: c.usd, project: c.project,
      future: c.future, partial: c.partial, missing: c.missing,
    }})),
  }})),
  behind: m.datesBehind({json.dumps(hours)}, 6, 2).map((x) => new Date(x.start).toISOString()),
}}));
"""
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "TZ": tz},
    )
    return json.loads(out.stdout)


def test_seven_local_days_oldest_first_with_today_partial() -> None:
    hours = [
        {"start": "2026-10-21T05:00:00+02:00", "usd": 149.49, "project": "alpha"},
        {"start": "2026-10-27T10:00:00+01:00", "usd": 2.0, "project": "beta"},
        {"start": "2026-10-14T05:00:00+02:00", "usd": 9.0, "project": "old"},  # before the week
    ]
    g = grid("2026-10-27T10:30:00+01:00", hours)
    rows = g["rows"]
    assert [r["label"] for r in rows] == [
        "Wed Oct 21", "Thu Oct 22", "Fri Oct 23", "Sat Oct 24", "Sun Oct 25", "Mon Oct 26",
        "Tue Oct 27",
    ]  # fmt: skip
    assert all(r["midnight"] == 0 for r in rows)  # each row starts at local midnight
    assert [r["today"] for r in rows] == [False] * 6 + [True]
    wed5 = rows[0]["cells"][5]
    assert wed5["usd"] == 149.49 and wed5["project"] == "alpha"
    assert wed5["starts"] == ["2026-10-21T03:00:00.000Z"]
    today = rows[6]["cells"]
    assert today[9]["future"] is False and today[9]["partial"] is False
    assert today[10]["partial"] is True and today[10]["future"] is False and today[10]["usd"] == 2
    assert all(c["future"] for c in today[11:]) and not any(
        c["future"] for r in rows[:6] for c in r["cells"]
    )
    assert g["max"] == 149.49 and g["total"] == 151.49  # the hour before the week is left out


def test_the_night_clocks_fall_back_has_two_two_oclocks() -> None:
    hours = [
        {"start": "2026-10-25T02:00:00+02:00", "usd": 1.0, "project": "first"},
        {"start": "2026-10-25T02:00:00+01:00", "usd": 3.0, "project": "second"},
        {"start": "2026-10-25T03:00:00+01:00", "usd": 0.5, "project": "after"},
    ]
    g = grid("2026-10-27T10:30:00+01:00", hours)
    sun = g["rows"][4]
    assert sun["label"] == "Sun Oct 25"
    two = sun["cells"][2]
    assert two["starts"] == ["2026-10-25T00:00:00.000Z", "2026-10-25T01:00:00.000Z"]
    assert two["usd"] == 4.0 and two["project"] == "second"  # the costlier of the two
    assert sun["cells"][3]["usd"] == 0.5 and sun["cells"][3]["starts"] == [
        "2026-10-25T02:00:00.000Z"
    ]
    assert sum(len(c["starts"]) for c in sun["cells"]) == 25
    # the dates behind the typical week's Sunday 02:00: both hours, newest first
    assert g["behind"] == ["2026-10-25T01:00:00.000Z", "2026-10-25T00:00:00.000Z"]


def test_the_night_clocks_spring_forward_has_no_two_oclock() -> None:
    g = grid("2026-03-31T08:00:00+02:00", [])
    sun = g["rows"][4]
    assert sun["label"] == "Sun Mar 29" and sun["midnight"] == 0
    assert sun["cells"][2]["missing"] is True and sun["cells"][2]["future"] is False
    assert sun["cells"][3]["starts"] == ["2026-03-29T01:00:00.000Z"]
    assert sum(len(c["starts"]) for c in sun["cells"]) == 23
    assert all(len(c["starts"]) == 1 for r in g["rows"][5:] for c in r["cells"])
    assert g["rows"][6]["cells"][8]["partial"] is True and g["total"] == 0


def test_a_zone_without_daylight_saving() -> None:
    g = grid("2026-10-21T00:10:00-03:00", [], tz="America/Sao_Paulo")
    rows = g["rows"]
    assert rows[0]["label"] == "Thu Oct 15" and rows[-1]["label"] == "Wed Oct 21"
    assert rows[-1]["cells"][0]["partial"] is True and all(
        c["future"] for c in rows[-1]["cells"][1:]
    )
