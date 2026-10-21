"""The README banner: capcom's mark and wordmark beside a spend chart drawing itself.

    python .github/scripts/banner/build.py <data.json> <out-dir>

<data.json> is demo/data.py's output (capcom run on the synthetic logs). Writes
<out-dir>/banner.svg, 880 x 200: the radio-signal mark pulsing like a
transmission, "capcom" as outlines (wordmark.py, no web font), the subtitle in
the system font, and the two weeks' cumulative spend by project in the
website's band style, drawing in from the left while the numbered spike
markers pop onto the total line as it reaches them; the total is labeled at
its right end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from svg import bands, keyframes, mark, r, series_colors, svg, usd
from wordmark import GLYPHS, UPM

W, H = 880, 200
LOOP = 10.0
DRAW = (0.5, 4.8)  # the chart draws in over these seconds
FADE = (9.2, 9.7)  # and fades out before the loop restarts
SUBTITLE = "Mission control for your Claude Code spend"


def wordmark(x: float, baseline: float, size: float, tracking: float = -0.01) -> tuple[str, float]:
    s = size / UPM
    out, pen = [], x
    for ch in "capcom":
        adv, d = GLYPHS[ch]
        out.append(
            f'<path transform="translate({r(pen, 2)} {r(baseline, 2)}) scale({r(s, 5)})" d="{d}"/>'
        )
        pen += (adv + tracking * UPM) * s
    return f'<g class="fg">{"".join(out)}</g>', pen - x


def pulse(peaks: list[float], lag: float) -> list[tuple[float, str]]:
    """Opacity frames: dim, flaring at each peak (+lag), back to full at the end."""
    lo, frames = "opacity:.28", [(0, "opacity:.28")]
    for p in peaks:
        p += lag
        frames += [(p - 0.05, lo), (p + 0.12, "opacity:1"), (p + 0.9, lo)]
    frames += [(LOOP - 0.4, lo), (LOOP, "opacity:1")]
    return frames


def build(data: dict, out: Path) -> Path:
    cum = data["spend_project"]["cumulative"]
    times = [t / 1000 for t in cum["times"]]
    members = cum["values"]
    total = cum["total"]
    colors = series_colors([m["label"] for m in cum["members"]])
    box = (506, 34, 300, 132)
    ymax = max(total) * 1.04
    defs, chart, points = bands(times, members, total, colors, box, ymax, "b")
    x0, y0, w, h = box

    # Spike markers on the total line at each spike's start, as on the website.
    marks = []
    for m in data["spend_project"]["spikes"]["marks"]:
        t = m["t"] / 1000
        i = min(range(len(times)), key=lambda k: abs(times[k] - t))
        px, py = points[i]
        at = DRAW[0] + (px - x0) / w * (DRAW[1] - DRAW[0])
        if marks and px - marks[-1][1] < 19:  # keep close markers apart, as a pair
            px = marks[-1][1] + 19
        marks.append((m["n"], px, py - 15, at))

    css = [
        ".chart{animation:fade 10s linear infinite}",
        keyframes(
            "fade",
            LOOP,
            [
                (0, "opacity:0"),
                (0.3, "opacity:1"),
                (FADE[0], "opacity:1"),
                (FADE[1], "opacity:0"),
                (LOOP, "opacity:1"),
            ],
        ),
        ".reveal{transform-box:fill-box;transform-origin:0 0;animation:draw 10s infinite}",
        keyframes(
            "draw",
            LOOP,
            [
                (0, "transform:scaleX(0)"),
                (
                    DRAW[0],
                    "transform:scaleX(0);animation-timing-function:cubic-bezier(.45,.05,.4,1)",
                ),
                (DRAW[1], "transform:scaleX(1)"),
                (LOOP, "transform:scaleX(1)"),
            ],
        ),
        ".pin circle{fill:var(--fg);stroke:var(--panel);stroke-width:2.5}"
        ".pin text{fill:var(--panel);font-size:10.5px;font-weight:700;text-anchor:middle}"
        ".pin{transform-box:fill-box;transform-origin:50% 50%}",
        ".m .a1{animation:a1 10s infinite}.m .a2{animation:a2 10s infinite}",
        keyframes("a1", LOOP, pulse([0.4, 3.0, 5.6, 8.2], 0)),
        keyframes("a2", LOOP, pulse([0.4, 3.0, 5.6, 8.2], 0.22)),
        ".sub{fill:var(--muted);font-size:19px}",
        ".total{fill:var(--fg);font-size:14px;font-weight:600}",
        ".end{animation:end 10s infinite}",
        keyframes(
            "end",
            LOOP,
            [
                (0, "opacity:0"),
                (DRAW[1] - 0.1, "opacity:0"),
                (DRAW[1] + 0.3, "opacity:1"),
                (LOOP, "opacity:1"),
            ],
        ),
    ]
    pins = []
    for k, (n, px, py, at) in enumerate(marks):
        css.append(f".p{k}{{animation:p{k} 10s infinite}}")
        css.append(
            keyframes(
                f"p{k}",
                LOOP,
                [
                    (0, "transform:scale(0)"),
                    (at, "transform:scale(0)"),
                    (at + 0.16, "transform:scale(1.3)"),
                    (at + 0.34, "transform:scale(1)"),
                    (LOOP, "transform:scale(1)"),
                ],
            )
        )
        pins.append(
            f'<g class="pin p{k}"><circle cx="{r(px)}" cy="{r(py)}" r="8.5"/>'
            f'<text x="{r(px)}" y="{r(py + 3.7)}">{n}</text></g>'
        )

    defs += (
        f'<clipPath id="clip"><rect class="reveal" x="{x0 - 2}" y="{y0 - 30}" '
        f'width="{w + 6}" height="{h + 34}"/></clipPath>'
    )
    word, _ = wordmark(104, 108, 88)
    body = [
        mark(52, 84, 68, "m"),
        word,
        f'<text class="sub" x="106" y="152">{SUBTITLE}</text>',
        '<g class="chart">',
        f'<line class="grid" x1="{x0}" y1="{y0 + h + 0.5}" x2="{x0 + w}" y2="{y0 + h + 0.5}" '
        'style="stroke-dasharray:none"/>',
        f'<g clip-path="url(#clip)">{chart}</g>',
        *pins,
        f'<g class="end"><circle cx="{r(points[-1][0])}" cy="{r(points[-1][1])}" r="3.2" '
        f'style="fill:var(--fg)"/><text class="total" x="{r(points[-1][0] + 8)}" '
        f'y="{r(points[-1][1] + 5)}">{usd(total[-1], cents=False)}</text></g>',
        "</g>",
    ]
    label = (
        "capcom: a radio-signal mark and the word capcom, beside a two-week spend chart "
        "drawing itself with numbered spike markers. Mission control for your Claude "
        "Code spend."
    )
    path = out / "banner.svg"
    path.write_text(svg(W, H, label, "".join(css), "".join(body), defs))
    return path


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    p = build(json.loads(Path(sys.argv[1]).read_text()), out)
    print(f"{p}  {p.stat().st_size / 1024:.1f} KiB")


if __name__ == "__main__":
    main()
