"""Ask Claude on a selection (static/ask.js) in headless Chrome, on the synthetic site
with a fake `claude` on PATH: the button beside the selected text (shown once the
selection settles, by mouse, keyboard or touch, never over the selection, following
it as the page scrolls, gone when it is cleared or on Escape, never for the chat's
own text or a field), the question it asks after the opt-in, what a selection that
crosses session titles, evidence rows or pasted config sends (none of them), and the
sidebar's network line with Ask Claude on, off and under `--no-ask`."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from capcom.report.ask import EXPLAIN_SELECTION
from tests.report import layout
from tests.report import test_ask_panel as panel

# the panel's fixtures and helpers: Chrome, and the fake `claude` with its prompts
chrome = panel.chrome
runs = panel.runs
prompts = panel.prompts
until = panel.until
SEP = "\u203a"  # between the parts of where a selection sits

pytestmark = pytest.mark.skipif(
    layout.find_chrome() is None or os.environ.get("CAPCOM_LAYOUT") == "0",
    reason="needs Chrome (set CAPCOM_CHROME), or CAPCOM_LAYOUT=0 skips it",
)

NET_OFF = "Local and read-only. No network."
NET_ON = "Local and read-only. Only Ask Claude uses the network, through your claude CLI."

SCROLLER = (
    "(() => { const m = document.getElementById('main');"
    " return m.scrollHeight > m.clientHeight + 1 ? m : document.scrollingElement; })()"
)
SHOWN = "!document.querySelector('.asksel').hidden"
HIDDEN = "document.querySelector('.asksel').hidden"


def select(chrome: layout.Chrome, js_range: str) -> None:
    """Select a range (a JS expression), as the keyboard would: no pointer down."""
    chrome.js(
        f"(() => {{ const r = {js_range}; const s = getSelection(); s.removeAllRanges();"
        " s.addRange(r); })()"
    )


def node_range(selector: str) -> str:
    return (
        f"(() => {{ const r = document.createRange();"
        f" r.selectNodeContents(document.querySelector({selector!r})); return r; }})()"
    )


def overlaps_selection(chrome: layout.Chrome) -> bool:
    return chrome.js(
        "(() => { const b = document.querySelector('.asksel').getBoundingClientRect();"
        " return [...getSelection().getRangeAt(0).getClientRects()].some((r) =>"
        " r.width > 0 && b.left < r.right && b.right > r.left && b.top < r.bottom"
        " && b.bottom > r.top); })()"
    )


def test_button_beside_a_selection(tmp_path: Path, runs: Path, chrome: layout.Chrome) -> None:
    with layout.synthetic_site(tmp_path) as base:
        chrome.viewport(1280, 1, "light")
        assert layout.load(chrome, base + "/checks")
        assert chrome.js(HIDDEN)
        chrome.js(
            "document.querySelectorAll('details.rollup').forEach((d) => { d.open = true; })"
        )  # the findings, folded by kind

        # by keyboard (no pointer): shown once it settles, beside the text, not over it
        select(chrome, node_range("section.card.check .fix p"))
        until(chrome, SHOWN)
        assert not overlaps_selection(chrome)
        near = chrome.js(
            "(() => { const b = document.querySelector('.asksel').getBoundingClientRect();"
            " const rs = getSelection().getRangeAt(0).getClientRects();"
            " const last = rs[rs.length - 1];"
            " return b.top >= last.bottom && b.top - last.bottom < 20"
            " && b.left >= 0 && b.right <= innerWidth; })()"
        )
        assert near
        assert chrome.js("document.querySelector('.asksel').textContent") == "Ask Claude"
        assert chrome.js("!!document.querySelector('.asksel svg.mascot.mini')")

        # it follows the text as the page scrolls
        # on a short window, so the page scrolls (in its main panel or the window)
        chrome.js("getSelection().removeAllRanges()")
        chrome.page(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 520, "deviceScaleFactor": 1, "mobile": False},
        )
        chrome.js(
            "document.querySelector('section.card.check .para').scrollIntoView({block: 'center'})"
        )
        select(chrome, node_range("section.card.check .para"))
        until(chrome, SHOWN)
        before = chrome.js("document.querySelector('.asksel').getBoundingClientRect().top")
        chrome.js(SCROLLER + ".scrollBy(0, 60)")
        until(
            chrome,
            f"Math.abs(document.querySelector('.asksel').getBoundingClientRect().top"
            f" - ({before} - 60)) < 2",
        )
        assert not overlaps_selection(chrome)
        # scrolled out of sight: out of the way until it comes back (room made to scroll)
        chrome.js("document.getElementById('main').style.paddingBottom = '1500px'")
        moved = chrome.js(
            f"(() => {{ const s = {SCROLLER}; const y = s.scrollTop;"
            " s.scrollBy(0, document.querySelector('section.card.check .para')"
            ".getBoundingClientRect().bottom + 10); return s.scrollTop - y; })()"
        )
        assert moved > 0, "the page did not scroll"
        until(chrome, "document.querySelector('.asksel').style.visibility === 'hidden'")
        chrome.js("document.getElementById('main').style.paddingBottom = ''")

        # cleared: gone; Escape: gone
        chrome.js("getSelection().removeAllRanges()")
        until(chrome, HIDDEN)
        chrome.viewport(1280, 1, "light")
        chrome.js(SCROLLER + ".scrollTop = 0")
        select(chrome, node_range("section.card.check .para"))
        until(chrome, SHOWN)
        chrome.key("Escape")
        until(chrome, HIDDEN)

        # with the mouse: not while dragging, then shown once the button is let go
        box = chrome.js(
            "(() => { const r = document.querySelector('section.card.check .para')"
            ".getBoundingClientRect(); return [r.left + 2, r.top + 8, r.left + 160]; })()"
        )
        x0, y, x1 = box
        mouse = "Input.dispatchMouseEvent"
        chrome.js("getSelection().removeAllRanges()")
        chrome.page(
            mouse, {"type": "mousePressed", "x": x0, "y": y, "button": "left", "clickCount": 1}
        )
        for i in range(1, 9):
            chrome.page(
                mouse,
                {"type": "mouseMoved", "x": x0 + (x1 - x0) * i / 8, "y": y, "button": "left"},
            )
        time.sleep(0.4)
        assert chrome.js("getSelection().toString().length > 3")
        assert chrome.js(HIDDEN)
        chrome.page(
            mouse, {"type": "mouseReleased", "x": x1, "y": y, "button": "left", "clickCount": 1}
        )
        until(chrome, SHOWN)
        assert not overlaps_selection(chrome)
        # a click elsewhere clears the selection, and the button with it
        chrome.click(x1 + 300, y + 200)
        until(chrome, HIDDEN)

        # never in the chat itself, nor in a field
        chrome.js("document.querySelector('.askbtn').click()")
        select(chrome, node_range(".askoptin p"))
        time.sleep(0.5)
        assert chrome.js(HIDDEN)
        chrome.js("document.querySelector('.askoptin .btn:not(.primary)').click()")
        chrome.js(
            "(() => { const f = document.getElementById('capcom-find');"
            " f.value = 'parser'; f.focus(); f.select(); })()"
        )
        time.sleep(0.5)
        assert chrome.js(HIDDEN)

        # on touch: further below, clear of the selection's handles
        chrome.js("document.activeElement.blur()")
        chrome.js(
            "document.dispatchEvent(new PointerEvent('pointerdown',"
            " {pointerType: 'touch', bubbles: true}))"
        )
        select(chrome, node_range("section.card.check .fix p"))
        until(chrome, SHOWN)
        gap = chrome.js(
            "(() => { const b = document.querySelector('.asksel').getBoundingClientRect();"
            " const rs = getSelection().getRangeAt(0).getClientRects();"
            " return b.top - rs[rs.length - 1].bottom; })()"
        )
        assert 24 <= gap <= 40
        assert not overlaps_selection(chrome)

        # on a phone too, inside the screen, and big enough to tap
        chrome.js("getSelection().removeAllRanges()")
        until(chrome, HIDDEN)
        chrome.viewport(390, 2, "dark")
        chrome.js(SCROLLER + ".scrollTop = 0")
        select(chrome, node_range("section.card.check .para"))
        until(chrome, SHOWN)
        fits = chrome.js(
            "(() => { const b = document.querySelector('.asksel').getBoundingClientRect();"
            " return b.left >= 0 && b.right <= innerWidth && b.height >= 43.5; })()"
        )
        assert fits


SESSION_TITLES = [t for t in layout.TITLES if t]


def test_a_selection_asks_without_the_private_parts(
    tmp_path: Path,
    runs: Path,
    chrome: layout.Chrome,
) -> None:
    with layout.synthetic_site(tmp_path) as base:
        chrome.viewport(1280, 1, "light")
        assert layout.load(chrome, base + "/checks")
        # what a closed <details> folds away is not shown, so not part of the selection
        folded = chrome.js(
            "window.capcomAsk.pickedText((() => { const r = document.createRange();"
            " r.selectNodeContents(document.querySelector('section.card.rollups')); return r; })())"
            ".text"
        )
        assert "Large files read again and again" in folded
        assert "One file was read 42 times" not in folded
        chrome.js("document.querySelectorAll('details.rollup').forEach((d) => { d.open = true; })")
        # from the card's heading over its evidence rows and the config to paste
        select(
            chrome,
            "(() => { const c = document.querySelector('section.card.check');"
            " const r = document.createRange(); r.setStartBefore(c.querySelector('h2'));"
            " r.setEndAfter(c.querySelector('.fix')); return r; })()",
        )
        until(chrome, SHOWN)
        chrome.js("document.querySelector('.asksel').click()")
        # off until turned on: the notice says what it will explain, and nothing goes
        assert not chrome.js("document.getElementById('askpanel').hidden")
        assert chrome.js("document.querySelector('.askoptin b').textContent") == (
            "the text you selected"
        )
        time.sleep(0.3)
        assert prompts(runs) == []
        chrome.js("document.querySelector('.askoptin .btn.primary').click()")
        until(chrome, "document.querySelectorAll('.asksugg .askchip').length === 3")
        sent = prompts(runs)[0]
        assert "<selection>" in sent and EXPLAIN_SELECTION in sent
        heads = ["Checks", "By kind", "Large files read again and again", "Every project"]
        where = f" {SEP} ".join(heads)
        assert f'(at "{where}")' in sent
        assert "One file was read 42 times across 12 sessions" in sent
        assert "[left out]" in sent
        assert "marked [left out]: session names, evidence rows, pasted config." in sent
        for private in ("a_long_file_name", "40k-token instructions", "MAX_THINKING_TOKENS"):
            assert private not in sent
        # the chat shows the text asked about and says what was left out
        quote = chrome.js("document.querySelector('.askmsg.me .askquote').textContent")
        assert quote.startswith("Every project") and "a_long_file_name" not in quote
        left = chrome.js("document.querySelector('.askmsg.me .askleft').textContent")
        assert left == "(session names, evidence rows and pasted config left out)"
        # a follow-up keeps the selection as the topic
        chrome.js("document.querySelector('.asksugg .askchip').click()")
        until(chrome, "document.querySelectorAll('.askmsg.bot').length === 2")
        time.sleep(0.3)
        assert "<selection>" in prompts(runs)[1]

        # a session's page: its title is its heading, and a subagent's lane is named by
        # its spawn description; a selection over them never sends either
        chrome.js("window.capcomAsk.close()")
        assert layout.load(chrome, base + "/session/s006")  # titled, with a subagent
        until(chrome, "document.getElementById('main').textContent.includes('Map the parser')")
        assert chrome.js("document.querySelector('#main h1').textContent") == layout.TITLES[6]
        select(
            chrome,
            "(() => { const r = document.createRange();"
            " r.selectNodeContents(document.getElementById('main')); return r; })()",
        )
        until(chrome, SHOWN)
        chrome.js("document.querySelector('.asksel').click()")
        until(
            chrome,
            "document.querySelectorAll('.askmsg.bot').length === 1"
            " && !!document.querySelector('.asksugg')",
        )
        # spikes: the detail names its session in a note
        assert layout.load(chrome, base + "/spikes")
        select(chrome, node_range("section.card.detail"))
        until(chrome, SHOWN)
        chrome.js("document.querySelector('.asksel').click()")
        until(
            chrome,
            "document.querySelectorAll('.askmsg.bot').length === 1"
            " && !!document.querySelector('.asksugg')",
        )
        # the whole sessions list
        assert layout.load(chrome, base + "/sessions")
        select(
            chrome,
            "(() => { const r = document.createRange();"
            " r.selectNodeContents(document.body); return r; })()",
        )
        until(chrome, SHOWN)
        chrome.js("document.querySelector('.asksel').click()")
        until(
            chrome,
            "document.querySelectorAll('.askmsg.bot').length === 1"
            " && !!document.querySelector('.asksugg')",
        )
        sent = prompts(runs)
        assert len(sent) == 5
        for prompt in sent[2:]:
            assert "<selection>" in prompt and "session names" in prompt
            for title in SESSION_TITLES:
                assert title not in prompt, title
        assert "Map the parser" not in "".join(sent)  # a subagent's spawn description


def test_network_line_follows_ask(tmp_path: Path, runs: Path, chrome: layout.Chrome) -> None:
    line = "document.querySelector('#sidebar .netnote').textContent"
    with layout.synthetic_site(tmp_path) as base:
        chrome.viewport(1280, 1, "light")
        assert layout.load(chrome, base + "/")
        until(chrome, "!!document.querySelector('#sidebar .netnote')")
        assert chrome.js(line) == NET_OFF
        chrome.js("document.querySelector('.askbtn').click()")
        chrome.js("document.querySelector('.askoptin .btn.primary').click()")
        assert chrome.js(line) == NET_ON
        # the mascot's name, in the greeting and the round button's label
        assert "I\u2019m Roger!" in chrome.js("document.querySelector('.askhello').textContent")
        assert chrome.js("document.querySelector('.askbtn').getAttribute('aria-label')") == (
            "Ask Roger"
        )
        # the sidebar is drawn anew on each live refresh, and on a reload
        assert layout.load(chrome, base + "/spend")
        until(chrome, "!!document.querySelector('#sidebar .netnote')")
        assert chrome.js(line) == NET_ON
        chrome.js("document.querySelector('.askbtn').click()")
        chrome.js("document.querySelector('.asklink').click()")
        assert chrome.js(line) == NET_OFF
        chrome.js("document.querySelector('.askoptin .btn.primary').click()")
    # --no-ask: no chat and the plain promise, even with Ask turned on in this browser
    with layout.synthetic_site(tmp_path / "off", ask_on=False) as base:
        assert layout.load(chrome, base + "/")
        chrome.js("localStorage.setItem('capcom-ask-on', '1')")
        assert layout.load(chrome, base + "/")
        until(chrome, "!!document.querySelector('#sidebar .netnote')")
        assert chrome.js(line) == NET_OFF
        assert chrome.js("!document.querySelector('.asksel')")
