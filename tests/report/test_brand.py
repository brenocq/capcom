"""capcom's mark is Roger: the brand of the sidebar and the phone topbar draws his head
with ask.js's mascot(), the favicon is that same head, and the old radio-signal mark
is gone from the website and the README's pictures."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "capcom" / "report" / "static"
SVG = "{http://www.w3.org/2000/svg}"
OLD_ARC = "a5.7 5.7"  # the radio mark's inner arcs


def head_parts() -> list[tuple[str, dict[str, str]]]:
    """The parts mascot(size, true) draws (those outside its `if (!mini)` block), as
    (tag, attributes without class), in drawing order."""
    js = (STATIC / "ask.js").read_text()
    src = re.search(r"export function mascot\(.*?\n}\n", js, re.S)
    assert src
    body = src.group()
    full_only = re.search(r"if \(!mini\) \{.*?\n  \}\n", body, re.S)
    assert full_only
    head = body[full_only.end() :]
    out = []
    for tag, attrs in re.findall(r"s\('(\w+)', \{([^}]*)\}", head):
        if tag in ("g", "svg"):
            continue
        pairs = {k: v.strip() for k, v in re.findall(r"'?([\w-]+)'?: '?([^',]+)'?", attrs)}
        pairs.pop("class", None)
        out.append((tag, pairs))
    return out


def test_brand_draws_roger_head() -> None:
    app = (STATIC / "app.js").read_text()
    brands = re.findall(r"h\('a\.brand', \{ href: '/' \}, (\w+)\(([^)]*)\)", app)
    assert len(brands) == 2  # the sidebar and the phone topbar
    for fn, args in brands:
        assert fn == "mascot"
        assert args.replace(" ", "").endswith(",true")  # the head, still
    assert "function mark(" not in app


def test_favicon_is_roger_head() -> None:
    root = ET.fromstring((STATIC / "favicon.svg").read_text())
    drawn = [
        (el.tag.removeprefix(SVG), {k: v for k, v in el.attrib.items() if k != "class"})
        for el in root
        if el.tag != f"{SVG}style"
    ]
    assert drawn == head_parts()
    w, h = root.attrib["viewBox"].split()[2:]
    assert w == h  # square, as a tab icon


def test_favicon_follows_dark_tabs() -> None:
    fav = (STATIC / "favicon.svg").read_text()
    css = (STATIC / "ask.css").read_text()
    dark = re.search(r'\[data-theme="dark"\]\s*\{([^}]*)\}', css)
    assert dark
    tokens = dict(re.findall(r"--(mz-[\w-]+):\s*([^;]+);", dark.group(1)))
    media = fav.split("prefers-color-scheme:dark", 1)
    assert len(media) == 2
    assert tokens["mz-band"] in media[1]  # the headphones light up on a dark tab
    assert tokens["mz-body"] in media[1]


def test_old_mark_is_gone() -> None:
    places = [*STATIC.glob("*"), *(ROOT / ".github" / "scripts").rglob("*.py")]
    assert places
    for p in places:
        text = p.read_text(errors="ignore")
        assert OLD_ARC not in text, p
        assert "radio-signal" not in text, p
    assert "radio-signal" not in (ROOT / "README.md").read_text()
