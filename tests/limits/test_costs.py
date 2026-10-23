"""What 1% of a window costs, by model and token type: the kinds with a tiny share of
the spend fitted together, each row's range, and the card's words (drawn by node in
a minimal fake DOM). All data is synthetic."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import pytest

from capcom.limits.model import MERGED, Fit, analyze
from capcom.limits.view import costs_json
from capcom.types import LimitWindow
from tests.limits.fakes import FakeIndex, minutes, req
from tests.limits.test_model import TABLE, synthetic
from tests.limits.test_pages import FAKE_DOM, STATIC

H5 = LimitWindow.FIVE_HOUR
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def test_kinds_with_a_tiny_share_of_the_spend_are_weighed_together() -> None:
    index, ss = synthetic(12)
    # a few haiku requests with a handful of input tokens: far under 1 % of the spend
    extra = [req(s.time - minutes(2), model="claude-haiku-5-5", input=12) for s in ss[::9]]
    index = FakeIndex([*index.reqs, *extra])
    f = analyze(index, TABLE, ss, H5, now=ss[-1].time + minutes(1)).fit
    assert f is not None and f.method == "per_type"
    assert ("claude-haiku-5-5", "input") in f.merged
    assert ("claude-haiku-5-5", "input") not in f.weights
    costs = costs_json(f)
    assert costs is not None
    assert all("haiku" not in r["model"] for r in costs["rows"])
    assert costs["merged"] is not None and "haiku-5-5 input" in costs["merged"]["kinds"]
    for r in costs["rows"]:
        assert r["tokens_low"] <= r["tokens"] <= r["tokens_high"]
        assert r["usd"] is not None and r["usd_low"] <= r["usd"] <= r["usd_high"]
    labels = [r["label"] for r in costs["rows"]]
    assert "opus-5-5 cache reads" in labels and "opus-5-5 output" in labels


def test_rows_by_their_range() -> None:
    sure, rough, unknown = ("m", "cache_read"), ("m", "output"), ("m", "input")
    f = Fit(
        H5,
        "per_type",
        40,
        40,
        1.0,
        weights={sure: 1.0, rough: 20.0, unknown: 400.0},
        spread={sure: 0.1, rough: 8.0, unknown: 500.0, MERGED: 0.01},
        behind={sure: 1e9, rough: 1e6, unknown: 1e4},
        usd_behind={sure: 200.0, rough: 25.0, unknown: 0.05},
        merged=(("n", "input"),),
        merged_per_usd=0.5,
    )
    costs = costs_json(f)
    assert costs is not None
    rows = {r["category"]: r for r in costs["rows"]}
    assert set(rows) == {"cache_read", "output"}  # input's range reaches zero
    assert costs["left_out"] == ["m input"]
    assert not rows["cache_read"]["rough"] and rows["cache_read"]["tokens"] == 1_000_000
    assert rows["cache_read"]["usd"] == pytest.approx(0.2)  # 1M tokens at $0.20 per M
    assert rows["output"]["rough"]  # 12 to 28: more than twice
    assert costs["merged"]["usd"] == pytest.approx(2.0)


def _run(body: str) -> Any:
    script = FAKE_DOM + "const m = await import('./limits.js');\n" + body
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


COSTS = {
    "rows": [
        {
            "model": "claude-opus-5-5",
            "category": "output",
            "label": "opus-5-5 output",
            "tokens": 43000,
            "tokens_low": 31000,
            "tokens_high": 70000,
            "usd": 0.86,
            "usd_low": 0.62,
            "usd_high": 1.4,
            "rough": True,
        },
        {
            "model": "claude-opus-5-5",
            "category": "cache_read",
            "label": "opus-5-5 cache reads",
            "tokens": 8_000_000,
            "tokens_low": 7_300_000,
            "tokens_high": 9_000_000,
            "usd": 1.61,
            "usd_low": 1.45,
            "usd_high": 1.8,
            "rough": False,
        },
    ],
    "merged": {
        "kinds": [
            "fable-5 input",
            "fable-5 output",
            "fable-5 cache reads",
            "fable-5 cache writes",
            "opus-5-5 input",
        ],
        "share": 0.016,
        "usd": 2.46,
        "usd_low": 1.74,
        "usd_high": 4.2,
        "rough": True,
    },
    "left_out": ["sonnet-5-5 output"],
    "min_share": 0.01,
    "rough_ratio": 2,
}


@needs_node
def test_the_cost_card_says_what_it_means_and_how_sure_it_is() -> None:
    w = {"label": "5-hour window", "fit": {"costs": COSTS}}
    text = _run(
        f"console.log(JSON.stringify(m.costsBlock({json.dumps(w)}).map(texts).join(' / ')));"
    )
    assert "How many tokens of each kind use up 1% of your 5-hour window" in text
    assert "Opus-5-5 cache-read tokens count the least: 8.0M of them make 1%" in text
    assert "Opus-5-5 output tokens count the most: 43k make 1%" in text
    assert "8.0M tokens · $1.61" in text and "Likely between 7.3M and 9.0M tokens" in text
    assert "Rough: between 31k and 70k tokens ($0.62 to $1.40)" in text
    assert "Everything else (5 kinds, 1.6% of the spend)" in text
    assert "all fable-5 tokens and opus-5-5 input" in text
    assert "Left out, as the readings cannot tell their cost from zero: sonnet-5-5 output" in text


@needs_node
def test_no_cost_card_for_a_per_dollar_fit() -> None:
    assert (
        _run("console.log(JSON.stringify(m.costsBlock({ label: 'x', fit: { costs: null } })));")
        is None
    )
