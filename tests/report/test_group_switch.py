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


# the Day chart's state: the pressed "Split by" button, the legend's labels, the
# query in the address bar and the day replies the page asked for
DAY_STATE = """(() => {
  const card = [...document.querySelectorAll('#main section.card')]
    .find((c) => c.querySelector('h2')?.textContent === 'The day, adding up');
  const seg = card.querySelector('.seg[aria-label="Split by"]');
  return {
    pressed: seg && seg.querySelector('button[aria-pressed="true"]').textContent,
    legend: [...card.querySelectorAll('.legend > span:not(.total)')]
      .map((s) => s.childNodes[1].textContent),
    search: location.search,
    asked: performance.getEntriesByType('resource').map((e) => new URL(e.name))
      .filter((u) => u.pathname.startsWith('/api/day/')).map((u) => u.pathname + u.search),
  };
})()"""


def test_day_chart_splits_by_the_group_in_the_url(tmp_path: Path) -> None:
    binary = layout.find_chrome()
    assert binary is not None
    day = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    with layout.synthetic_site(tmp_path) as base:
        chrome = layout.Chrome(binary, tmp_path / "profile")
        try:
            # Spend's day bars, split by model, open their day split the same way
            assert layout.load(chrome, f"{base}/spend?group=model")
            hrefs = chrome.js("[...document.querySelectorAll('a.day')].map((a) => a.search)")
            assert hrefs and set(hrefs) == {"?group=model"}

            assert layout.load(chrome, f"{base}/day/{day}?group=model")
            got = chrome.js(DAY_STATE)
            assert got["pressed"] == "Model"
            assert got["asked"] == [f"/api/day/{day}?group=model"]
            assert got["legend"] and all(x.startswith("claude-") for x in got["legend"])
            # where the money went lists the models too, under the agent types
            models = chrome.js(
                "(() => { const t = [...document.querySelectorAll('#main h2.small')]"
                ".find((e) => e.textContent === 'By model');"
                " return t && [...t.nextElementSibling"
                ".querySelectorAll('.bitem .top span:first-child')]"
                ".map((s) => s.textContent); })()"
            )
            # (the chart names a model by its price-table id, the list as logged)
            assert models and len(models) == len(got["legend"])
            assert all(m.startswith("claude-") for m in models)

            chrome.js(
                "[...document.querySelectorAll('#main .seg button')]"
                ".find((b) => b.textContent.trim() === 'Agent type').click()"
            )
            time.sleep(0.1)
            assert chrome.js(layout.READY, timeout=30)
            got = chrome.js(DAY_STATE)
            assert got["pressed"] == "Agent type" and got["search"] == "?group=agent"
            assert got["asked"][-1] == f"/api/day/{day}?group=agent"
            assert "main" in got["legend"]
            # the day before and after keep the split
            nav = "[...document.querySelectorAll('#main .head a.link')].map((a) => a.search)"
            assert chrome.js(nav) == ["?group=agent", "?group=agent"]

            # back to the default: the plain path, and the kinds of tokens again
            chrome.js(
                "[...document.querySelectorAll('#main .seg button')]"
                ".find((b) => b.textContent.trim() === 'Category').click()"
            )
            time.sleep(0.1)
            assert chrome.js(layout.READY, timeout=30)
            got = chrome.js(DAY_STATE)
            assert got["pressed"] == "Category" and got["search"] == ""
            assert got["asked"][-1] == f"/api/day/{day}"
            assert "Cache reads" in got["legend"]
        finally:
            chrome.close()


# each "What spent each window" block on Limits: its pressed "Split by" button, its
# legend, its table's member column; and the query and the limits replies asked for
LIMITS_STATE = """(() => {
  const blocks = [...document.querySelectorAll('#main h3')]
    .filter((e) => e.textContent === 'What spent each window').map((h3) => {
      const head = h3.parentElement;
      return {
        pressed: head.querySelector('.seg[aria-label="Split by"] button[aria-pressed="true"]')
          ?.textContent,
        legend: [...head.nextElementSibling.querySelectorAll('span')]
          .map((s) => s.textContent),
        column: head.parentElement.querySelectorAll('details.astable th')[1]?.textContent,
      };
    });
  return {
    blocks, search: location.search,
    asked: performance.getEntriesByType('resource').map((e) => new URL(e.name))
      .filter((u) => u.pathname === '/api/limits').map((u) => u.search),
  };
})()"""


def test_limits_split_what_spent_each_window_by_the_group_in_the_url(tmp_path: Path) -> None:
    binary = layout.find_chrome()
    assert binary is not None
    with layout.synthetic_site(tmp_path) as base:
        chrome = layout.Chrome(binary, tmp_path / "profile")
        try:
            assert layout.load(chrome, f"{base}/limits?group=agent")
            got = chrome.js(LIMITS_STATE)
            assert got["asked"] == ["?days=14&group=agent"]
            assert len(got["blocks"]) == 4  # the 5-hour and weekly windows of 2 accounts
            for b in got["blocks"]:
                assert b["pressed"] == "Agent type" and b["column"] == "Agent type"
                assert "main" in b["legend"]

            chrome.js(
                "[...document.querySelectorAll('#main .seg[aria-label=\"Split by\"] button')]"
                ".find((b) => b.textContent.trim() === 'Category').click()"
            )
            time.sleep(0.1)
            assert chrome.js(layout.READY, timeout=30)
            got = chrome.js(LIMITS_STATE)
            assert got["search"] == "?group=category"
            assert got["asked"][-1] == "?days=14&group=category"
            for b in got["blocks"]:
                assert b["pressed"] == "Category" and b["column"] == "Category"
                assert "Cache reads" in b["legend"]
        finally:
            chrome.close()


# a limit window's "How it filled up": its pressed "Split by" button, its legend, the
# fitted line's bands in the chart; and the query and the window replies asked for
WINDOW_STATE = """(() => {
  const card = [...document.querySelectorAll('#main section.card')]
    .find((c) => c.querySelector('h2')?.firstChild.textContent === 'How it filled up');
  return {
    pressed: card.querySelector('.seg[aria-label="Split by"] button[aria-pressed="true"]')
      ?.textContent,
    legend: [...card.querySelectorAll('.legend > span')].map((s) => s.childNodes[1].textContent),
    bands: card.querySelectorAll('svg path.fitband').length,
    search: location.search,
    asked: performance.getEntriesByType('resource').map((e) => new URL(e.name))
      .filter((u) => u.pathname === '/api/limit-window')
      .map((u) => u.searchParams.get('group')),
  };
})()"""


def test_limit_window_splits_the_fitted_line_by_the_group_in_the_url(tmp_path: Path) -> None:
    binary = layout.find_chrome()
    assert binary is not None
    with layout.synthetic_site(tmp_path) as base:
        chrome = layout.Chrome(binary, tmp_path / "profile")
        try:
            assert layout.load(chrome, f"{base}/limits")
            href = chrome.js(
                "document.querySelector(\"#main a[href^='/limits/window/']\").getAttribute('href')"
            )
            assert layout.load(chrome, f"{base}{href}?group=agent")
            got = chrome.js(WINDOW_STATE)
            assert got["pressed"] == "Agent type"
            assert got["asked"] == ["agent"]
            assert got["legend"][:2] == ["Readings", "Fitted from the spend"]
            members = got["legend"][2:]
            assert "main thread" in members and got["bands"] == len(members)

            chrome.js(
                "[...document.querySelectorAll('#main .seg[aria-label=\"Split by\"] button')]"
                ".find((b) => b.textContent.trim() === 'Category').click()"
            )
            time.sleep(0.1)
            assert chrome.js(layout.READY, timeout=30)
            got = chrome.js(WINDOW_STATE)
            assert got["pressed"] == "Category" and got["search"] == "?group=category"
            assert got["asked"][-1] == "category"
            assert "Cache reads" in got["legend"] and got["bands"] == len(got["legend"]) - 2
        finally:
            chrome.close()
