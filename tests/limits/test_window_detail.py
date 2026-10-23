"""A usage window on its own page: its API, and the page's words (drawn by node).
All data is synthetic."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from capcom.limits.model import sample_json
from capcom.report.server import Site
from capcom.types import LimitSample, LimitWindow
from tests.limits.test_model import TABLE
from tests.limits.test_pages import FAKE_DOM, STATIC
from tests.report.conftest import FAKE, NOW, Client, ago, build_index, serve

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
ISO = "new Date(t).toISOString()"
H5, D7 = LimitWindow.FIVE_HOUR, LimitWindow.SEVEN_DAY
A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"


# ------------------------------------------------------------------ the page's API


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_index(tmp_path_factory.mktemp("window-index") / "index.sqlite")


@pytest.fixture
def client(built: Path, tmp_path: Path) -> Iterator[tuple[Client, Path]]:
    folder = tmp_path / "capcom"
    folder.mkdir()
    shutil.copy(built, folder / "index.sqlite")
    state = tmp_path / "state"
    state.mkdir()
    site = Site(folder / "index.sqlite", TABLE, FAKE, clock=lambda: NOW, tz=UTC, state_dir=state)
    for c in serve(site):
        yield c, state


def _write(state: Path, ss: list[LimitSample]) -> None:
    with (state / "limits.jsonl").open("a") as f:
        for s in ss:
            f.write(sample_json(s) + "\n")


def _windows() -> list[LimitSample]:
    """Two 5-hour windows: one closed (reset an hour ago), one current."""
    old_reset, new_reset = ago(hours=1), NOW + timedelta(hours=2)
    return [
        LimitSample(ago(hours=5, minutes=30), H5, 10.0, old_reset, "s1"),
        LimitSample(ago(hours=3), H5, 40.0, old_reset, "s1"),
        LimitSample(ago(hours=1, minutes=30), H5, 70.0, old_reset, "s1"),
        LimitSample(ago(minutes=50), H5, 5.0, new_reset, "s1"),
        LimitSample(ago(minutes=5), H5, 20.0, new_reset, "s1"),
    ]


def test_each_window_links_to_its_page_and_the_page_has_its_parts(
    client: tuple[Client, Path],
) -> None:
    c, state = client
    _write(state, _windows())
    status, lim, _ = c.get("/api/limits?days=14")
    assert status == 200
    segs = lim["accounts"][0]["windows"]["five_hour"]["segments"]
    paths = [s["path"] for s in segs]
    acct = lim["accounts"][0]["id"]
    assert paths[1] == f"/limits/window/{acct}/5-hour/2026-10-21T0900"
    status, body, _ = c.get(paths[1])
    assert status == 200 and b"app.js" in body  # the page itself
    status, d, _ = c.get(f"/api/limit-window?account={acct}&window=5-hour&start=2026-10-21T0900")
    assert status == 200
    assert d["window"] == "five_hour" and d["start"] == "2026-10-21T09:00:00+00:00"
    assert d["resets_at"] == "2026-10-21T14:00:00+00:00" and not d["current"]
    assert d["peak"] == 70.0 and d["points"][0][1] == 0.0 and d["start_known"]
    assert d["after"] == paths[0] and d["before"] is None
    assert d["totals"]["usd"] > 0 and d["by_project"] and d["sessions"]
    assert {"by_agent", "by_model", "spikes"} <= d.keys()
    assert all(g["percent"] is not None for g in d["by_project"])
    times = d["fitted"]["times"]
    assert times == sorted(times) and len(times) == len(d["fitted"]["percent"])
    assert d["fitted"]["percent"] == sorted(d["fitted"]["percent"])  # it only adds up
    cats = d["totals"]["categories"]
    assert sum(c["percent"] for c in cats.values()) == pytest.approx(d["fitted_percent"], 1e-3)
    # the current window, by a time inside it rather than its start
    inside = f"/api/limit-window?account={acct}&window=five_hour&start=2026-10-21T1430"
    status, cur, _ = c.get(inside)
    assert status == 200 and cur["current"] and cur["before"] == paths[1]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("account=nobody&window=5-hour&start=2026-10-21T0900", 404),
        ("account={acct}&window=5-hour&start=2026-10-01T0900", 404),
        ("account={acct}&window=daily&start=2026-10-21T0900", 400),
        ("account={acct}&window=5-hour&start=yesterday", 400),
    ],
)
def test_window_api_errors(client: tuple[Client, Path], query: str, code: int) -> None:
    c, state = client
    _write(state, _windows())
    acct = c.get("/api/limits")[1]["accounts"][0]["id"]
    status, _, _ = c.get("/api/limit-window?" + query.format(acct=acct))
    assert status == code


def _run(body: str, module: str = "limits.js") -> Any:
    script = FAKE_DOM + f"const m = await import('./{module}');\nconst iso = (t) => {ISO};\n" + body
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


@needs_node
def test_window_page_words() -> None:
    d = {
        "window": "five_hour",
        "start": "2026-10-22T18:20:00+00:00",
        "resets_at": "2026-10-22T23:20:00+00:00",
        "until": "2026-10-22T23:20:00+00:00",
        "current": False,
        "peak": 99,
        "hit_limit": False,
        "start_known": True,
        "totals": {"usd": 147.15},
        "own_usd_per_percent": 1.48,
        "fit": {"usd_per_percent": 1.5},
        "inferred_usd": 21.03,
        "account": {"several": True},
    }
    got = _run(
        f"const d = {json.dumps(d)};"
        " console.log(JSON.stringify([m.windowTitle(d), m.windowStory(d)]));",
        module="limitwindow.js",
    )
    assert got[0] == "Oct 22 18:20 → 23:20"
    assert got[1] == (
        "It reached 99% of the limit on $147.15 of API-equivalent spend. In this window 1% "
        "cost about $1.48, against $1.50 on average over every window. Of the spend, $21.03 "
        "came from sessions with no reading, put on this account by time."
    )
