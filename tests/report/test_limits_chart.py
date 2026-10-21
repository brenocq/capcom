"""The limits charts' run-out mark and start, run by node on synthetic forecasts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"
HOUR = 3600e3
X0 = 1792458000000  # 2026-10-20 01:00 UTC, a Tuesday
X1 = X0 + 168 * HOUR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(expr: str) -> object:
    script = (
        "globalThis.document = { createElement: () => ({}), createElementNS: () => ({}) };\n"
        "const m = await import('./limits.js');\n"
        "const iso = (t) => new Date(t).toISOString();\n"
        f"console.log(JSON.stringify({expr}));\n"
    )
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


def _mark(mid: float | None, early: float | None, late: float | None) -> object:
    fc = {
        "runs_out_at": None if mid is None else f"iso({X0 + mid * HOUR})",
        "runs_out_early": None if early is None else f"iso({X0 + early * HOUR})",
        "runs_out_late": None if late is None else f"iso({X0 + late * HOUR})",
    }
    body = ", ".join(f"{k}: {v if v else 'null'}" for k, v in fc.items())
    return _run(f"m.runoutMark({{{body}}}, {X0}, {X1}, true)")


def test_mark_near_the_right_edge_flips_left_with_its_range() -> None:
    m = _mark(150, 140, 160)
    assert isinstance(m, dict)
    assert m["title"] == "Runs out ~Mon 07:00"
    assert m["sub"] == "between Sun 21:00 and Mon 17:00"
    assert m["side"] == "left"
    assert m["band"][0] < m["f"] < m["band"][1]


def test_mark_early_in_the_window_sits_right() -> None:
    m = _mark(30, None, None)
    assert isinstance(m, dict)
    assert m["side"] == "right" and m["sub"] is None and m["band"] is None


def test_no_mark_when_the_forecast_lasts_until_the_reset() -> None:
    assert _mark(None, 150, None) is None
    assert _mark(200, 150, None) is None  # past the reset


def test_page_chart_starts_at_the_window_start() -> None:
    now = X0 + 60 * HOUR
    pts = f"[[{X0}, 0], [{X0 + 50 * HOUR}, 88]]"
    segs = f"[{{ start: iso({X0}), points: {pts}, resets_at: iso({X1}) }}]"
    assert _run(f"m.chartStart({segs}, {now}, 14)") == X0
