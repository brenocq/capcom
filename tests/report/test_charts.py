"""The stacked cumulative chart's sums, run through node on synthetic series."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_stack_lines_add_each_series_to_those_below() -> None:
    script = (
        "import('./charts.js').then((m) => console.log(JSON.stringify("
        "m.stackLines([[0, 2, 5], [0, 1, 1], [0, 0, 3]]))))"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    # the top line is the total; each band's thickness is its own series
    assert json.loads(out.stdout) == [[0, 2, 5], [0, 3, 6], [0, 3, 9]]
