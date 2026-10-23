"""The repository's social preview card, 1280 x 640 as GitHub asks for.

    python .github/scripts/social/build.py [<assets-dir>]   (default .github/assets)

Reads banner.svg and demo-dark.png from <assets-dir> (build_all.py makes both, from
the synthetic logs) and writes social.png there: on GitHub's dark page color, the
banner's mark, wordmark and subtitle (its chart cropped away, its still frame:
reduced motion), the install command under them, and the demo's still of the "why"
step on the right, cropped to its window. Rendered in headless Chrome (chrome.py).
GitHub takes the card by hand: Settings, General, Social preview, Edit, Upload.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chrome import Chrome

W, H = 1280, 640
INSTALL = "uv tool install git+https://github.com/brenocq/capcom"
# The banner's left part, in its own units: the mark, "capcom", Roger and the subtitle.
CROP = (10, 26, 488, 148)  # x, y, w, h
# The demo still: a 1240 x 845 frame (in CSS pixels; demo/frame.html) whose window
# sits at (40, 22), 1160 wide, its title bar and screen 34 + 720 tall.
FRAME_W = 1240
WINDOW = (40, 22, 1160, 754)


def page(banner: str, still: Path) -> str:
    x, y, w, h = CROP
    svg = re.sub(
        r'<svg ([^>]*?)width="\d+" height="\d+" viewBox="[^"]*"',
        rf'<svg \1width="{w}" height="{h}" viewBox="{x} {y} {w} {h}"',
        banner,
        count=1,
    )
    bw = 460  # the banner column's width
    k = 640 / WINDOW[2]  # the window drawn 640 px wide
    wx, wy, ww, wh = (v * k for v in WINDOW)
    left = W - 44 - ww
    top = (H - wh) / 2
    return f"""<!doctype html><meta charset=utf-8><style>
html, body {{ margin: 0; width: {W}px; height: {H}px; overflow: hidden; }}
body {{
  position: relative;
  background:
    radial-gradient(620px 420px at 16% 40%, rgba(217, 119, 87, .16), transparent 70%),
    radial-gradient(720px 520px at 96% 100%, rgba(42, 120, 214, .12), transparent 70%),
    #0d1117;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial,
    sans-serif;
}}
.left {{ position: absolute; left: 52px; top: 0; bottom: 0; width: {bw}px; display: flex;
  flex-direction: column; justify-content: center; gap: 34px; }}
.left > svg {{ width: {bw}px; height: auto; display: block; }}
.left .say {{ display: none; }}
.install {{ align-self: flex-start; margin-left: 10px; padding: 11px 16px; border-radius: 10px;
  background: rgba(255, 255, 255, .06); border: 1px solid rgba(255, 255, 255, .10);
  color: #e6edf3; white-space: nowrap;
  font: 500 13px ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }}
.install b {{ color: #D97757; font-weight: 600; margin-right: 10px; }}
.win {{ position: absolute; left: {left:.1f}px; top: {top:.1f}px; width: {ww:.1f}px;
  height: {wh:.1f}px; overflow: hidden; border-radius: {12 * k:.1f}px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, .10), 0 18px 50px rgba(0, 0, 0, .55); }}
.win img {{ position: absolute; left: {-wx:.1f}px; top: {-wy:.1f}px;
  width: {FRAME_W * k:.1f}px; }}
</style>
<div class="left">{svg}<div class="install"><b>$</b>{INSTALL}</div></div>
<div class="win"><img src="{still.as_uri()}"></div>
"""


def build(assets: Path) -> Path:
    assets = assets.resolve()
    banner = (assets / "banner.svg").read_text()
    still = assets / "demo-dark.png"
    html = assets / ".social.html"
    html.write_text(page(banner, still))
    out = assets / "social.png"
    try:
        with Chrome(W, H) as c:
            c.media(dark=True, reduced=True)
            c.goto(html.as_uri(), settle=1.0)
            c.screenshot(out, clip={"x": 0, "y": 0, "width": W, "height": H})
    finally:
        html.unlink(missing_ok=True)
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[2]  # .github
    assets = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "assets"
    print(build(assets))


if __name__ == "__main__":
    main()
