"""The spike detail's "What to change": named causes' advice only, drawn by node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

NAMED = {"cause": "subagent_fanout", "advice": "Cap the subagents."}
REST = {"cause": "unexplained", "advice": "Nothing to change yet. If this pattern comes back..."}


def _advice(causes: list[dict[str, str]]) -> list[str]:
    script = (
        "globalThis.Node = class {};\n"
        "const m = await import('./spikes.js');\n"
        f"console.log(JSON.stringify(m.spikeAdvice({json.dumps(causes)})));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result: list[str] = json.loads(out.stdout)
    return result


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_named_cause_hides_the_unexplained_advice() -> None:
    assert _advice([NAMED, REST]) == ["Cap the subagents."]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_unexplained_advice_shows_without_a_named_cause() -> None:
    assert _advice([REST]) == [REST["advice"]]
    assert _advice([])[0].startswith("Nothing to change yet")


def _sorted(sort: str | None) -> list[int]:
    items = [
        {"n": 1, "excess_usd": 9.0, "start": "2026-10-03T10:00:00Z"},
        {"n": 2, "excess_usd": 4.0, "start": "2026-10-01T10:00:00Z"},
        {"n": 3, "excess_usd": 6.0, "start": "2026-10-05T10:00:00Z"},
        {"n": 4, "excess_usd": 4.0, "start": "2026-10-02T10:00:00Z"},
    ]
    script = (
        "globalThis.Node = class {};\n"
        "const m = await import('./spikes.js');\n"
        f"const out = m.sortSpikes({json.dumps(items)}, {json.dumps(sort)});\n"
        "console.log(JSON.stringify(out.map((it) => it.n)));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result: list[int] = json.loads(out.stdout)
    return result


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_spike_list_sorts_by_cost_or_date_keeping_marker_numbers() -> None:
    assert _sorted(None) == [1, 3, 2, 4]  # default: cost, highest first
    assert _sorted("bogus") == [1, 3, 2, 4]
    assert _sorted("cost-asc") == [2, 4, 3, 1]  # ties keep the marker order
    assert _sorted("date-desc") == [3, 1, 4, 2]
    assert _sorted("date-asc") == [2, 4, 1, 3]
