"""The README banner: capcom's mark, wordmark and Roger beside a spend chart drawing itself.

    python .github/scripts/banner/build.py <data.json> <out-dir>

<data.json> is demo/data.py's output (capcom run on the synthetic logs). Writes
<out-dir>/banner.svg, 880 x 200: the radio-signal mark pulsing like a
transmission, "capcom" as outlines (wordmark.py, no web font), Roger (the
website's mascot, read from its own code by roger.py) standing on the
wordmark's baseline, the subtitle in the system font, and the two weeks'
cumulative spend by project in the website's band style, drawing in from the
left while the three largest spikes' numbered markers pop onto the total line
as it reaches them, each on the steep step it explains; the total is labeled
right of its end, inside the picture. Roger blinks while the chart draws, then
waves once with the website's own wave and says, in a chat bubble like his
replies on the website, what the checks would save a week.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import roger
from svg import bands, eased_at, keyframes, mark, r, series_colors, spike_pins, svg, usd
from wordmark import GLYPHS, UPM

W, H = 880, 200
LOOP = 10.0
DRAW = (0.5, 4.8)  # the chart draws in over these seconds
FADE = (9.2, 9.7)  # and fades out before the loop restarts
SUBTITLE = "Mission control for your Claude Code spend"
ROGER = (409, 80)  # left edge and width of Roger, standing on the wordmark's baseline
BLINK = 2.6  # Roger blinks while the chart draws,
WAVE = 5.1  # then waves once it is drawn,
SAY = 5.5  # and his bubble pops up, gone with the chart's fade.


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


def bubble(x: float, y: float, text: str) -> tuple[float, str]:
    """A chat bubble like Roger's replies on the website (ask.css .askmsg.bot: the card
    color, a hairline border, round but for the corner by him) with its lower left
    corner at (x, y). Returns its width and markup."""
    w, h, rr, rs = 9 + 7.3 * len(text), 26, 12, 4
    d = (
        f"M{r(x + rs)} {r(y)}H{r(x + w - rr)}a{rr} {rr} 0 0 0 {rr}-{rr}V{r(y - h + rr)}"
        f"a{rr} {rr} 0 0 0-{rr}-{rr}H{r(x + rr)}a{rr} {rr} 0 0 0-{rr} {rr}V{r(y - rs)}"
        f"a{rs} {rs} 0 0 0 {rs} {rs}Z"
    )
    return w, (
        f'<g class="say"><path class="surface" d="{d}"/>'
        f'<text class="said" x="{r(x + w / 2)}" y="{r(y - 8.5)}">{text}</text></g>'
    )


def build(data: dict, out: Path) -> Path:
    cum = data["spend_project"]["cumulative"]
    times = [t / 1000 for t in cum["times"]]
    members = cum["values"]
    total = cum["total"]
    colors = series_colors([m["label"] for m in cum["members"]])
    box = (516, 34, 266, 132)  # leaves room right of the plot for the total's label
    ymax = max(total) * 1.04
    defs, chart, points = bands(times, members, total, colors, box, ymax, "b")
    x0, y0, w, h = box

    # The three largest spikes, each sitting on the top of the steepest step of its span.
    marks = [
        (n, px, py, DRAW[0] + eased_at((px - x0) / w) * (DRAW[1] - DRAW[0]))
        for n, px, py in spike_pins(
            data["spend_project"]["spikes"]["marks"], times, total, points, 3, 24
        )
    ]

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
        ".total{fill:var(--fg);font-size:14px;font-weight:600;"
        "paint-order:stroke;stroke:var(--panel);stroke-width:3px;stroke-linejoin:round}",
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
            f'<g class="pin p{k}"><circle cx="{r(px)}" cy="{r(py - 11)}" r="8.5"/>'
            f'<text x="{r(px)}" y="{r(py - 7.3)}">{n}</text></g>'
        )

    defs += (
        f'<clipPath id="clip"><rect class="reveal" x="{x0 - 2}" y="{y0 - 30}" '
        f'width="{w + 6}" height="{h + 34}"/></clipPath>'
    )
    word, _ = wordmark(104, 108, 88)

    # Roger on the baseline, right of the wordmark, his raised hand toward the chart.
    rx, rw = ROGER
    vx, vy, vw, _ = roger.VIEWBOX
    k = rw / vw
    top = 108 - (roger.feet() - vy) * k  # his body's bottom on the baseline
    weekly = round(data["checks"]["total"]["usd_per_week"])
    said = f"Up to ${weekly}/wk to save"
    _, say = bubble(rx + (37 - vx) * k, top + (9.5 - vy) * k, said)
    arm = roger.on_loop("arm", [WAVE])
    eyes = roger.on_loop("eyes", [BLINK, WAVE])
    _, _, ease = roger.motion("arm")
    css += [
        roger.theme_css(),
        f".rg-arm{{animation:rg-arm 10s {ease} infinite}}",
        keyframes("rg-arm", LOOP, arm),
        f".rg-eyes{{animation:rg-eyes 10s {ease} infinite}}",
        keyframes("rg-eyes", LOOP, eyes),
        ".said{fill:var(--fg);font-size:13px;font-weight:600;text-anchor:middle}",
        ".say{transform-box:fill-box;transform-origin:0 100%;animation:say 10s infinite}",
        keyframes(
            "say",
            LOOP,
            [
                (0, "transform:scale(0);opacity:1"),
                (SAY, "transform:scale(0);opacity:1"),
                (SAY + 0.18, "transform:scale(1.08);opacity:1"),
                (SAY + 0.34, "transform:scale(1);opacity:1"),
                (FADE[0], "transform:scale(1);opacity:1"),
                (FADE[1], "transform:scale(1);opacity:0"),
                (LOOP, "transform:scale(1);opacity:1"),
            ],
        ),
    ]
    body = [
        mark(52, 84, 68, "m"),
        word,
        roger.group(rx, top, rw),
        say,
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
        "capcom: a radio-signal mark, the word capcom and Roger, a terracotta capsule in "
        "mission-control headphones, beside a two-week spend chart drawing itself with "
        f"numbered spike markers; Roger waves and says: {said}. Mission control for your "
        "Claude Code spend."
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
