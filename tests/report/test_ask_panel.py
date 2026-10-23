"""The Ask Claude panel (static/ask.js) in headless Chrome, on the synthetic site with
a fake `claude` on PATH: the button and the marks, the one-time opt-in notice
(nothing is sent before it), a mark's question with its part attached, the
streamed answer with suggested replies that ask the next question with the chat
so far, what the part leaves out, the panel's own layout, reduced motion and
`--no-ask`."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from capcom.report.ask import EXPLAIN
from tests.report import layout
from tests.report.test_ask import FAKE_CLAUDE

pytestmark = pytest.mark.skipif(
    layout.find_chrome() is None or os.environ.get("CAPCOM_LAYOUT") == "0",
    reason="needs Chrome (set CAPCOM_CHROME), or CAPCOM_LAYOUT=0 skips it",
)

NOTICE = (
    "Ask Claude sends the numbers and labels on this part of the page to Claude through "
    "your own claude CLI. No text from your logs is sent."
)


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable))
    exe.chmod(0o755)
    out = tmp_path / "runs"
    out.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(out))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    return out


@pytest.fixture
def chrome(tmp_path: Path) -> Iterator[layout.Chrome]:
    binary = layout.find_chrome()
    assert binary
    c = layout.Chrome(binary, tmp_path / "profile")
    try:
        yield c
    finally:
        c.close()


def prompts(folder: Path) -> list[str]:
    return [json.loads(p.read_text())["prompt"] for p in sorted(folder.glob("run-*.json"))]


def until(chrome: layout.Chrome, cond: str, timeout: float = 10) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = chrome.js(cond)
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"never true: {cond}")


def test_panel_opt_in_mark_answer_and_suggestions(
    tmp_path: Path, runs: Path, chrome: layout.Chrome
) -> None:
    with layout.synthetic_site(tmp_path) as base:
        chrome.viewport(1280, 1, "light")
        assert layout.load(chrome, base + "/checks")
        # the button, closed; a mark on every finding card
        assert chrome.js("!!document.querySelector('#ask .askbtn')")
        assert chrome.js("document.getElementById('askpanel').hidden")
        cards = chrome.js("document.querySelectorAll('section.card.check').length")
        assert (
            cards
            and chrome.js("document.querySelectorAll('section.card.check .askmark').length")
            == cards
        )

        # off until turned on: the notice, and nothing sent
        chrome.js("document.querySelector('.askbtn').click()")
        assert not chrome.js("document.getElementById('askpanel').hidden")
        assert chrome.js("document.querySelector('.askoptin p').textContent") == NOTICE
        chrome.js("document.querySelector('.askmark').click()")
        label = chrome.js("document.querySelector('.askoptin b').textContent")
        assert label
        time.sleep(0.3)
        assert prompts(runs) == []

        # turned on: the mark's question goes, with its part; the answer streams in
        chrome.js("document.querySelector('.askoptin .btn.primary').click()")
        assert chrome.js("localStorage.getItem('capcom-ask-on')") == "1"
        until(chrome, "document.querySelectorAll('.asksugg .askchip').length === 3")
        answer = chrome.js("[...document.querySelectorAll('.askmsg.bot')].pop().textContent")
        assert answer.strip() == "You load tools you rarely use. Turn them off."
        assert chrome.js("document.querySelector('.askmsg.me .askpart b').textContent") == label
        first = prompts(runs)[0]
        assert label in first and EXPLAIN in first and "SUGGESTIONS" not in answer
        # the part's numbers in capcom's words, never the evidence rows or session links
        assert "up to $" in first
        assert "a_long_file_name" not in first and "40k-token instructions" not in first

        # a suggested reply asks the next question, with the chat so far
        chip = chrome.js("document.querySelector('.asksugg .askchip').textContent")
        chrome.js("document.querySelector('.asksugg .askchip').click()")
        until(
            chrome,
            "document.querySelectorAll('.askmsg.bot').length === 2"
            " && !!document.querySelector('.asksugg')",
        )
        second = prompts(runs)[1]
        assert f"<message>\n{chip}\n</message>" in second
        assert "You: You load tools you rarely use." in second and label in second

        # the panel's own layout, wide and on a phone
        problems = layout.check(chrome, {"modal": "#askpanel", "ignore": ".tip"})
        assert problems == [], problems
        chrome.viewport(390, 2, "dark")
        chrome.js(layout.SETTLE)
        problems = layout.check(chrome, {"modal": "#askpanel", "ignore": ".tip", "touch": True})
        assert problems == [], problems
        assert chrome.js(
            "(() => { const r = document.getElementById('askpanel').getBoundingClientRect();"
            " return r.left >= 0 && r.right <= innerWidth && r.top >= 0; })()"
        )

        # Escape closes it; a reload remembers the opt-in and says hello instead
        chrome.js(
            "document.getElementById('askpanel').dispatchEvent("
            "new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
        )
        assert chrome.js("document.getElementById('askpanel').hidden")
        assert layout.load(chrome, base + "/checks")
        chrome.js("document.querySelector('.askbtn').click()")
        assert chrome.js(
            "!document.querySelector('.askoptin') && !!document.querySelector('.askhello')"
        )
        # "Turn off" forgets it
        chrome.js("document.querySelector('.asklink').click()")
        assert chrome.js("localStorage.getItem('capcom-ask-on')") is None
        assert chrome.js("!!document.querySelector('.askoptin')")


def test_marks_on_other_pages_and_reduced_motion(
    tmp_path: Path, runs: Path, chrome: layout.Chrome
) -> None:
    with layout.synthetic_site(tmp_path) as base:
        chrome.viewport(1280, 1, "light")
        chrome.page(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
        )
        assert layout.load(chrome, base + "/spikes")
        assert chrome.js("!!document.querySelector('section.card.detail .askmark')")
        assert layout.load(chrome, base + "/")
        assert chrome.js("!!document.querySelector('aside.card .askmark')")
        # never waves: not on load, and its animation is off even if asked
        time.sleep(2.0)
        assert not chrome.js("!!document.querySelector('.mascot.waving')")
        chrome.js("document.querySelector('.askbtn .mascot').classList.add('waving')")
        name = chrome.js(
            "getComputedStyle(document.querySelector('.askbtn .mz-arm')).animationName"
        )
        assert name == "none"


def test_no_ask_hides_everything(tmp_path: Path, runs: Path, chrome: layout.Chrome) -> None:
    with layout.synthetic_site(tmp_path, ask_on=False) as base:
        chrome.viewport(1280, 1, "light")
        assert layout.load(chrome, base + "/checks")
        assert chrome.js("!document.getElementById('ask') && !document.querySelector('.askmark')")
