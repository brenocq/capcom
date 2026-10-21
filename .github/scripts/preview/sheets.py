"""Contact sheets of the README's SVGs: several moments of each, on GitHub's light
and dark page colors, for checking them by eye.

    python .github/scripts/preview/sheets.py <out-dir> <file.svg>... [--at 0.5,2,4,7]

Each SVG is inlined several times, its animations paused at each moment (the
Web Animations API), plus a last copy with the animations cancelled: the frame
prefers-reduced-motion shows. Writes <out-dir>/sheet-<name>-{light,dark}.png.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chrome import Chrome

PAGE = {"light": "#ffffff", "dark": "#0d1117"}
FG = {"light": "#59636e", "dark": "#9198a1"}


def unique(svg: str, k: int) -> str:
    """The SVG with its ids suffixed, so inlined copies don't share clip paths."""
    for i in set(re.findall(r'id="([^"]+)"', svg)):
        svg = svg.replace(f'id="{i}"', f'id="{i}-{k}"').replace(f"#{i})", f"#{i}-{k})")
        svg = svg.replace(f'href="#{i}"', f'href="#{i}-{k}"')
    return svg


def page(svg: str, at: list[float], theme: str) -> str:
    cells = (
        "".join(
            f'<figure data-t="{t}">{unique(svg, k)}<figcaption>{t:g} s</figcaption></figure>'
            for k, t in enumerate(at)
        )
        + f'<figure data-t="still">{unique(svg, 99)}'
        + "<figcaption>reduced motion</figcaption></figure>"
    )
    return (
        "<!doctype html><meta charset=utf-8><style>"
        f"body{{margin:0;padding:16px;background:{PAGE[theme]};display:flex;flex-wrap:wrap;"
        "gap:14px;width:max-content;max-width:1900px}"
        f"figure{{margin:0}}figcaption{{font:12px sans-serif;color:{FG[theme]};padding-top:4px}}"
        f"</style><body>{cells}</body>"
    )


PAUSE = """
for (const f of document.querySelectorAll('figure')) {
  const t = f.dataset.t;
  for (const a of f.querySelector('svg').getAnimations({subtree: true})) {
    if (t === 'still') a.cancel(); else { a.pause(); a.currentTime = parseFloat(t) * 1000; }
  }
}
const b = document.body.getBoundingClientRect();
new Promise(done => requestAnimationFrame(() => requestAnimationFrame(
  () => done([Math.ceil(b.width), Math.ceil(b.height)]))))
"""


def sheets(out: Path, files: list[Path], at: list[float], chrome: Chrome) -> list[Path]:
    made = []
    for f in files:
        svg = f.read_text()
        for theme in ("light", "dark"):
            chrome.media(dark=theme == "dark")
            html = out / f".sheet-{f.stem}.html"
            html.write_text(page(svg, at, theme))
            chrome.goto(html.as_uri(), settle=0.2)
            w, h = chrome.eval(PAUSE)
            chrome.viewport(w, h)
            time.sleep(0.3)  # paused compositor animations repaint on the next frames
            png = out / f"sheet-{f.stem}-{theme}.png"
            chrome.screenshot(png)
            html.unlink()
            made.append(png)
    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("files", type=Path, nargs="+")
    ap.add_argument("--at", default="0.4,1.5,3,5,8")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    with Chrome(1200, 800) as c:
        for p in sheets(a.out, a.files, [float(x) for x in a.at.split(",")], c):
            print(p)


if __name__ == "__main__":
    main()
