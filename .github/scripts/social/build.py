"""The repository's social preview card, 1280 x 640 as GitHub asks for.

    python .github/scripts/social/build.py <data.json> [<assets-dir>]   (default .github/assets)

<data.json> is demo/data.py's output; <assets-dir> holds banner.svg (build_all.py
makes both, from the synthetic logs). Writes social.png there: on GitHub's dark
page color, the banner's mark, wordmark, Roger with his "Roger that!" bubble and
the subtitle (its chart cropped away, its still frame: reduced motion), the
install command under them, and on the right the banner's spend chart, drawn
larger by the banner's own code (banner/build.py chart_svg) in its final frame.
Rendered in headless Chrome (chrome.py). GitHub takes the card by hand:
Settings, General, Social preview, Edit, Upload.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chrome import Chrome

HERE = Path(__file__).resolve().parents[1]  # .github/scripts
W, H = 1280, 640
INSTALL = "uv tool install git+https://github.com/brenocq/capcom"
# The banner's left part, in its own units: the mark, "capcom", Roger, his bubble
# and the subtitle (its chart hidden, as the bubble reaches over its left edge).
CROP = (10, 4, 540, 170)  # x, y, w, h
# The chart, in its own units, drawn at twice that size: its plot, and room above
# for the spike markers and right of it for the total's label.
CHART = (300, 236)
PLOT = (6, 30, 246, 192)
SCALE = 2.0


def chart_svg(data: dict) -> str:
    """banner/build.py's chart_svg (loaded by path: two scripts are called build.py)."""
    spec = importlib.util.spec_from_file_location("banner_build", HERE / "banner" / "build.py")
    assert spec and spec.loader
    banner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(banner)
    w, h = CHART
    return str(banner.chart_svg(data, w, h, PLOT))


def page(banner: str, chart: str) -> str:
    """The card's page. Both SVGs are inline, where the emulated reduced motion stops
    their animations (an <img> would not see it); their ids differ (b, c)."""
    x, y, w, h = CROP
    svg = re.sub(
        r'<svg ([^>]*?)width="\d+" height="\d+" viewBox="[^"]*"',
        rf'<svg \1width="{w}" height="{h}" viewBox="{x} {y} {w} {h}"',
        banner,
        count=1,
    )
    bw = 510  # the banner column's width
    cw, ch = (v * SCALE for v in CHART)
    left = W - 40 - cw
    top = (H - ch) / 2
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
.left .chart {{ display: none; }}
.install {{ align-self: flex-start; margin-left: 10px; padding: 11px 16px; border-radius: 10px;
  background: rgba(255, 255, 255, .06); border: 1px solid rgba(255, 255, 255, .10);
  color: #e6edf3; white-space: nowrap;
  font: 500 13px ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }}
.install b {{ color: #D97757; font-weight: 600; margin-right: 10px; }}
.plot {{ position: absolute; left: {left:.1f}px; top: {top:.1f}px; }}
.plot > svg {{ width: {cw:.1f}px; height: {ch:.1f}px; display: block; }}
</style>
<div class="left">{svg}<div class="install"><b>$</b>{INSTALL}</div></div>
<div class="plot">{chart}</div>
"""


def build(assets: Path, data: dict) -> Path:
    assets = assets.resolve()
    banner = (assets / "banner.svg").read_text()
    html = assets / ".social.html"
    html.write_text(page(banner, chart_svg(data)))
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
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    data = json.loads(Path(sys.argv[1]).read_text())
    assets = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "assets"
    print(build(assets, data))


if __name__ == "__main__":
    main()
