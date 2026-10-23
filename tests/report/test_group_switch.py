"""Overview's group switch in headless Chrome, while another process writes the
index (a scanning server beside a `--no-scan` one): the top cards never show older
totals than the page already did, and stay the same across a switch when the data
did not change. Synthetic data only (tests/report/layout.py); runs when Chrome is
installed."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from capcom.cost.prices import load_prices
from capcom.report.analysis import Analysis, default_analysis
from capcom.report.server import Site, make_server
from capcom.store.index import open_index
from capcom.types import LogKind, SessionInfo
from tests.report import layout

pytestmark = pytest.mark.skipif(
    layout.find_chrome() is None or os.environ.get("CAPCOM_LAYOUT") == "0",
    reason="needs Chrome (set CAPCOM_CHROME), or CAPCOM_LAYOUT=0 skips it",
)

SLOW = 1.0  # seconds today's chart takes: a reply computed whole is a job, with a 202

# the cost card's texts, each time it changes, from the moment it is called
WATCH = """(() => { clearInterval(window.__iv); const seen = window.__seen = [];
  const look = () => { const c = document.querySelector('#main .card.tight .big');
    const t = c ? c.textContent : null; if (seen[seen.length - 1] !== t) seen.push(t); };
  look(); window.__iv = setInterval(look, 10); })()"""


@contextlib.contextmanager
def _site(work: Path) -> Iterator[str]:
    """The synthetic site, its today's chart slowed down; yield the base URL."""
    now = datetime.now(UTC).replace(microsecond=0)
    layout.build_index(work / "index.sqlite", now)
    layout.write_limits(work / "state", now)
    real = default_analysis()

    def slow(*args: Any, **kw: Any) -> Any:
        time.sleep(SLOW)
        return real.cumulative_fn(*args, **kw)

    analysis = Analysis(
        slow, real.per_step_fn, real.find_spikes_fn, real.explain_fn, layout.findings
    )
    site = Site(work / "index.sqlite", load_prices(), analysis, tz=UTC, state_dir=work / "state")
    server = make_server(site, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _spend(index: Path, n: int) -> None:
    """Another process's scan: three costly requests of a new session, just now."""
    now = datetime.now(UTC).replace(microsecond=0)
    sid = f"later{n}"
    reqs = [
        layout._req(sid, now - timedelta(seconds=30 - i), "-home-user-alpha", output=500_000)
        for i in range(3)
    ]
    info = SessionInfo(
        sid, "-home-user-alpha", cwd="/x", first_seen=reqs[0].timestamp, last_seen=now
    )
    ix = open_index(index)
    try:
        layout._put(
            ix, f"/logs/{sid}.jsonl", LogKind.SESSION, "-home-user-alpha", sid, None, [info, *reqs]
        )
    finally:
        ix.close()


def _usd(text: str) -> float:
    return float(text.replace("≥", "").replace("$", "").replace(",", "").strip())


def _switch(chrome: layout.Chrome, label: str) -> list[float]:
    """Click the chart's group, wait for the page to settle; the cost card's values
    from just before the click to the end."""
    chrome.js(WATCH)
    chrome.js(
        "[...document.querySelectorAll('#main .seg button')]"
        f".find((b) => b.textContent.trim() === {label!r}).click()"
    )
    time.sleep(0.1)
    assert chrome.js(layout.READY, timeout=30)
    time.sleep(1.0)  # the numbers tween to their new values
    return [_usd(t) for t in chrome.js("window.__seen") if t]


def _card(chrome: layout.Chrome) -> float:
    return _usd(chrome.js("document.querySelector('#main .card.tight .big').textContent"))


def test_group_switch_never_shows_older_totals(tmp_path: Path) -> None:
    binary = layout.find_chrome()
    assert binary is not None
    with _site(tmp_path) as base:
        chrome = layout.Chrome(binary, tmp_path / "profile")
        try:
            assert layout.load(chrome, base + "/?group=project")
            before = _card(chrome)
            _spend(tmp_path / "index.sqlite", 1)
            shown = _switch(chrome, "Agent type")[-1]
            assert shown > before  # the new requests are in
            # back to a split the page loaded before the index changed: its kept
            # reply is older than the one on screen and is not shown again
            seen = _switch(chrome, "Project")
            assert seen[-1] == shown and min(seen) == shown

            # a fresh page on one split; the index changes; the other split's only
            # reply on the server is from before: never drawn, even as a stand-in
            _spend(tmp_path / "index.sqlite", 2)
            assert layout.load(chrome, base + "/?group=agent")
            shown = _card(chrome)
            _spend(tmp_path / "index.sqlite", 3)
            seen = _switch(chrome, "Project")
            assert seen == sorted(seen), f"went back: {seen}"
            assert seen[-1] > shown
        finally:
            chrome.close()
