"""The README's eight feature cards, 420 x 240 animated SVGs.

    python .github/scripts/features/build.py <data.json> <out-dir>

<data.json> is demo/data.py's output: every chart and number on a card is what
capcom computed on the synthetic logs (spend by project and one day by hour, a
spike's window and cause, a session's timeline with its pauses, a coder agent's
steps, the checks rolled up by kind with a marked change's verdict, the usage
windows and their forecast); the Ask Roger card keeps one real answer as text,
and the Private card is a diagram. Writes feature-{adding-up,why,timeline,
every-step,checks,limits,ask-roger,private}.svg into <out-dir>.

Each card runs on one loop: the scene fades in, plays, holds its last frame and
fades out. Every element's own style is its last frame, so prefers-reduced-motion
(all animations off) shows the finished picture.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))

import roger
import synthetic  # the Every step card shows its made-up test output
from svg import (
    PANEL,
    bands,
    card,
    eased_at,
    fade,
    keyframes,
    ms,
    r,
    series_colors,
    spike_pins,
    usd,
)

PX, PY, PW, PH = PANEL
LOOP = 9.0


class Css:
    """Keyframes for one card, on its loop: `step` moves an element from state `a` to
    `b` at time t (and holds `b`, its own style, to the end)."""

    def __init__(self, loop: float = LOOP) -> None:
        self.loop = loop
        self.rules: list[str] = [
            f".scene{{animation:scene {r(loop, 2)}s linear infinite}}",
            keyframes(
                "scene",
                loop,
                [
                    (0, "opacity:0"),
                    (0.3, "opacity:1"),
                    (loop - 0.7, "opacity:1"),
                    (loop - 0.2, "opacity:0"),
                    (loop, "opacity:0"),
                ],
            ),
        ]
        self.n = 0

    def step(
        self,
        a: str,
        b: str,
        t: float,
        d: float = 0.4,
        ease: str = "cubic-bezier(.3,.7,.4,1)",
        origin: str = "",
    ) -> str:
        """A new class animating a -> b over [t, t + d]; returns the class name."""
        self.n += 1
        name = f"k{self.n}"
        box = f"transform-box:fill-box;transform-origin:{origin};" if origin else ""
        self.rules.append(f".{name}{{{box}{b};animation:{name} {r(self.loop, 2)}s infinite}}")
        self.rules.append(
            keyframes(
                name,
                self.loop,
                [(0, a), (t, f"{a};animation-timing-function:{ease}"), (t + d, b), (self.loop, b)],
            )
        )
        return name

    def css(self) -> str:
        return "".join(self.rules)


def text(
    x: float,
    y: float,
    s: str,
    cls: str = "muted",
    size: float = 11,
    anchor: str = "start",
    weight: int = 400,
    extra: str = "",
) -> str:
    w = f' font-weight="{weight}"' if weight != 400 else ""
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (
        f'<text class="{cls}" x="{r(x)}" y="{r(y)}" font-size="{size}"{w}{a}{extra}>'
        f"{escape(s)}</text>"
    )


def legend(x: float, y: float, items: list[tuple[str, str]], gap: float = 14) -> str:
    """Colored squares with labels; label widths estimated with slack for wide fonts."""
    out = []
    for color, label in items:
        out.append(
            f'<rect x="{r(x)}" y="{r(y - 7)}" width="8" height="8" rx="2" style="fill:{color}"/>'
        )
        out.append(text(x + 12, y, label, size=10.5))
        x += 12 + len(label) * 6.2 + gap
    return "".join(out)


# ------------------------------------------------------------------ cards


def adding_up(data: dict[str, Any]) -> str:
    c = Css()
    cum = data["spend_project"]["cumulative"]
    times = [t / 1000 for t in cum["times"]]
    labels = [m["label"] for m in cum["members"]]
    colors = series_colors(labels)
    total = cum["total"]
    box = (PX + 16, PY + 34, PW - 86, PH - 50)
    defs, chart, points = bands(times, cum["values"], total, colors, box, max(total) * 1.04, "a")
    x0, y0, w, h = box
    draw = (0.4, 4.2)
    reveal = c.step(
        "transform:scaleX(0)",
        "transform:scaleX(1)",
        draw[0],
        draw[1] - draw[0],
        ease="cubic-bezier(.45,.05,.4,1)",
        origin="0 0",
    )
    defs += (
        f'<clipPath id="ac"><rect class="{reveal}" x="{x0 - 2}" y="{y0 - 30}" '
        f'width="{w + 6}" height="{h + 34}"/></clipPath>'
    )
    pins = []
    marks = data["spend_project"]["spikes"]["marks"]
    for n, px, py in spike_pins(marks, times, total, points, 4, 20):
        at = draw[0] + eased_at((px - x0) / w) * (draw[1] - draw[0])
        k = c.step(
            "transform:scale(0)",
            "transform:scale(1)",
            at,
            0.3,
            ease="cubic-bezier(.3,1.6,.5,1)",
            origin="50% 50%",
        )
        pins.append(
            f'<g class="{k}"><circle cx="{r(px)}" cy="{r(py - 10)}" r="7.5" '
            f'style="fill:var(--fg);stroke:var(--panel);stroke-width:2"/>'
            f'<text x="{r(px)}" y="{r(py - 6.6)}" font-size="9.5" font-weight="700" '
            f'text-anchor="middle" style="fill:var(--panel)">{n}</text></g>'
        )
    end = c.step("opacity:0", "opacity:1", draw[1], 0.4)
    ex, ey = points[-1]
    drill = drill_down(c, data["spike_day"], times, box)
    body = (
        '<g class="scene">'
        + legend(PX + 16, PY + 20, list(zip(colors[:3], labels[:3], strict=False)))
        + f'<line x1="{x0}" y1="{y0 + h + 0.5}" x2="{x0 + w}" y2="{y0 + h + 0.5}" '
        'style="stroke:var(--border)"/>'
        + f'<g clip-path="url(#ac)">{chart}</g>'
        + "".join(pins)
        + drill
        + f'<g class="{end}"><circle cx="{r(ex)}" cy="{r(ey)}" r="3" style="fill:var(--fg)"/>'
        + text(ex + 7, ey + 4, usd(total[-1], cents=False), "fg", 13, weight=600)
        + text(ex + 7, ey + 18, "14 days", "muted", 10.5)
        + "</g></g>"
    )
    return card(
        "Adding up",
        [
            "Every session and subagent on one timeline, by",
            "project, model or agent type, spikes numbered.",
        ],
        "Adding up: two weeks of spend drawing in, one line per project and the total, numbered "
        "spike markers popping onto the total line, which ends at "
        f"{usd(total[-1], cents=False)}; then the steepest day opens into its 24 hours.",
        c.css(),
        body,
        defs,
    )


def drill_down(
    c: Css, day: dict[str, Any], times: list[float], box: tuple[float, float, float, float]
) -> str:
    """The drill-down beat: the spike's day lights up on the chart, then opens into
    an inset of its 24 hourly bars, as a click on a day does on the website."""
    x0, y0, w, h = box
    X = lambda t: x0 + (t - times[0]) / (times[-1] - times[0]) * w  # noqa: E731
    a = X(ms(day["window"]["since"]) / 1000)
    b = X(ms(day["window"]["until"]) / 1000)
    lit = c.step("opacity:0", "opacity:1", 5.0, 0.3)
    hours = day["hourly"]["hours"]
    top = max(hr["total"] for hr in hours)
    peak = max(range(len(hours)), key=lambda i: hours[i]["total"])
    ix, iy, iw, ih = x0 + 4, y0 - 4, 160, 52
    bw = (iw - 16) / len(hours)
    bars = []
    for i, hr in enumerate(hours):
        bh = max(hr["total"] / top * (ih - 26), 0.8)
        k = c.step(
            "transform:scaleY(0)", "transform:scaleY(1)", 5.7 + i * 0.03, 0.3, origin="50% 100%"
        )
        color = "var(--accent)" if i == peak else "var(--gray)"
        bars.append(
            f'<rect class="{k}" x="{r(ix + 8 + i * bw)}" y="{r(iy + ih - 8 - bh)}" '
            f'width="{r(bw - 1.2)}" height="{r(bh)}" style="fill:{color}"/>'
        )
    on = datetime.fromisoformat(day["window"]["since"])
    pop = c.step(
        "opacity:0;transform:scale(.85)", "opacity:1;transform:scale(1)", 5.4, 0.35, origin="100% 0"
    )
    return (
        f'<rect class="{lit}" x="{r(a)}" y="{y0}" width="{r(b - a)}" height="{h}" rx="2" '
        'style="fill:var(--accent);fill-opacity:.14"/>'
        f'<g class="{pop}">'
        f'<path d="M{r(ix + iw)} {r(iy + ih / 2)}L{r(a)} {r(y0 + 8)}" stroke-dasharray="2 3" '
        'style="stroke:var(--muted)"/>'
        f'<rect x="{r(ix)}" y="{r(iy)}" width="{iw}" height="{ih}" rx="6" '
        'style="fill:var(--surface);stroke:var(--border)"/>'
        + text(ix + 8, iy + 14, f"{on:%b} {on.day}, by hour", "fg", 10, weight=600)
        + text(
            ix + iw - 8,
            iy + 14,
            f"{hours[peak]['start'][11:16]} · {usd(hours[peak]['total'], cents=False)}",
            "clay",
            10,
            "end",
            600,
        )
        + "".join(bars)
        + "</g>"
    )


def why(data: dict[str, Any]) -> str:
    c = Css()
    spikes = [s for s in data["spikes"]["spikes"] if s["confidence"] == "clear"]
    s = max(spikes, key=lambda x: x["excess_usd"])
    win = s["window"]
    usual, extra = win["usual"], win["extra"]
    n = len(usual)
    top = max(u + e for u, e in zip(usual, extra, strict=True)) * 1.05
    x0, y0, w, h = PX + 16, PY + 30, PW - 32, 70
    bw = w / n
    out = []
    for i in range(n):
        x = x0 + i * bw + 1
        uh = usual[i] / top * h
        eh = extra[i] / top * h
        k = c.step(
            "transform:scaleY(0)", "transform:scaleY(1)", 0.3 + i * 0.025, 0.35, origin="50% 100%"
        )
        if uh > 0.3:
            out.append(
                f'<rect class="{k}" x="{r(x)}" y="{r(y0 + h - uh)}" width="{r(bw - 2)}" '
                f'height="{r(uh)}" style="fill:var(--gray)"/>'
            )
        if eh > 0.3:
            k2 = c.step(
                "transform:scaleY(0)", "transform:scaleY(1)", 1.6 + i * 0.05, 0.5, origin="50% 100%"
            )
            out.append(
                f'<rect class="{k2}" x="{r(x)}" y="{r(y0 + h - uh - eh)}" '
                f'width="{r(bw - 2)}" height="{r(eh)}" rx="1.5" '
                'style="fill:var(--accent)"/>'
            )
    # The cause types in: a clip widening in steps, one per character.
    head = s["headline"]
    amount = f"+{usd(s['excess_usd'])}"
    chars = len(head) + 3 + len(amount)
    t0, t1 = 2.9, 2.9 + chars * 0.045
    typing = c.step(
        "transform:scaleX(0)",
        "transform:scaleX(1)",
        t0,
        t1 - t0,
        ease=f"steps({chars})",
        origin="0 0",
    )
    caret = c.step("opacity:1", "opacity:0", t1 + 0.6, 0.01)
    ty = y0 + h + 24
    defs = (
        f'<clipPath id="wc"><rect class="{typing}" x="{x0 - 2}" y="{ty - 16}" '
        f'width="{w + 4}" height="22"/></clipPath>'
    )
    line = (
        f'<text x="{x0}" y="{ty}" font-size="13"><tspan class="fg" font-weight="600">'
        f'{escape(head)}</tspan><tspan class="muted"> · </tspan>'
        f'<tspan class="clay" font-weight="600">{escape(amount)}</tspan></text>'
    )
    sub = c.step("opacity:0", "opacity:1", t1 + 0.2, 0.4)
    when = f"{s['title']} · {s['cause_label']}, clear cause"
    body = (
        '<g class="scene">'
        + legend(
            x0,
            PY + 20,
            [("var(--gray)", "usual pace"), ("var(--accent)", "extra, above the baseline")],
        )
        + f'<line x1="{x0}" y1="{y0 + h + 0.5}" x2="{x0 + w}" y2="{y0 + h + 0.5}" '
        'style="stroke:var(--border)"/>'
        + "".join(out)
        + f'<g clip-path="url(#wc)">{line}</g>'
        + f'<rect class="{caret}" x="{x0 - 1}" y="{ty - 12}" width="1.5" height="15" '
        f'style="fill:var(--fg)"/>'
        + f'<g class="{sub}">'
        + text(x0, ty + 16, when, "muted", 10.5)
        + "</g></g>"
    )
    return card(
        "Why",
        [
            "Each spike gets its cause from the logs, with",
            "the dollars it added and what to change.",
        ],
        f"Why: a spike's bars, the usual pace in gray and the extra rising in terracotta, "
        f"then its cause typing in: {head}, {amount}.",
        c.css(),
        body,
        defs,
    )


def spend_rate(
    t: list[float], cost: list[float], runs: list[tuple[float, float]], window: float = 600e3
) -> list[list[tuple[float, float]]]:
    """The session page's spend rate (session.js spendRate): each request's cost
    spread over a triangle `window` wide on either side of it, cut at its run's
    edges and scaled back up, in $/hour, sampled every 2 minutes of each run."""

    def cdf(u: float) -> float:
        if u <= -window:
            return 0.0
        if u < 0:
            return (u + window) ** 2 / (2 * window * window)
        if u < window:
            return 1 - (window - u) ** 2 / (2 * window * window)
        return 1.0

    out = []
    for r0, r1 in runs:
        r1 = max(r1, r0 + 60e3)
        own = [k for k in range(len(t)) if r0 <= t[k] <= r1]
        weight = {k: cost[k] / max(cdf(r1 - t[k]) - cdf(r0 - t[k]), 1e-9) for k in own}
        n = max(2, math.ceil((r1 - r0) / 120e3) + 1)
        pts = []
        for j in range(n):
            u = r0 + (r1 - r0) * j / (n - 1)
            v = sum(
                weight[k] * (1 - abs(u - t[k]) / window) / window
                for k in own
                if abs(u - t[k]) <= window
            )
            pts.append((u, v * 3600e3))
        out.append(pts)
    return out


def timeline(data: dict[str, Any]) -> str:
    c = Css()
    sess = data["idle_session"]
    req = sess["requests"]
    t0, t1 = ms(sess["session"]["first_seen"]), ms(sess["session"]["last_seen"])
    pauses = [(ms(p["start"]), ms(p["end"])) for p in sess["pauses"]]
    edges = [t0] + [x for p in pauses for x in p] + [t1]
    runs = [(edges[2 * i], edges[2 * i + 1]) for i in range(len(pauses) + 1)]
    x0, w = PX + 64, PW - 80
    sliver = 14.0
    active = sum(b - a for a, b in runs)
    # Fit all (linear time) and the session page's squeezed axis: each pause a
    # fixed hatched sliver, the runs sharing the rest by their length.
    lin, sq = [], []
    sx = x0
    for a, b in runs:
        lin.append((x0 + (a - t0) / (t1 - t0) * w, (b - a) / (t1 - t0) * w))
        sw = (b - a) / active * (w - sliver * len(pauses))
        sq.append((sx, sw))
        sx += sw + sliver
    cy, ch = PY + 30, 46  # context
    my, ay, ry, rh = PY + 82, PY + 96, PY + 110, 30  # main, Explore, spend rate
    peak = sess["main"]["peak_context"] * 1.08
    main_t = [(req["t"][i], req["context"][i]) for i in range(len(req["t"])) if req["agent"][i] < 0]
    squeeze = (3.5, 0.8)
    groups = []
    for i, ((a, b), (sx, sw)) in enumerate(zip(runs, sq, strict=True)):
        X = lambda tt, a=a, b=b, sx=sx, sw=sw: sx + (tt - a) / max(b - a, 1) * sw  # noqa: E731
        pts = [(X(tt), cy + ch - v / peak * ch) for tt, v in main_t if a <= tt <= b]
        d = f"M{r(sx)} {cy + ch}" + "".join(f"L{r(px)} {r(py)}" for px, py in pts)
        d += f"L{r(sx + sw)} {r(pts[-1][1] if pts else cy + ch)}L{r(sx + sw)} {cy + ch}Z"
        line = "".join(f"{'L' if j else 'M'}{r(px)} {r(py)}" for j, (px, py) in enumerate(pts))
        ticks = "".join(
            f'<rect x="{r(X(ms(g["first"])))}" y="{ay}" '
            f'width="{r(max(X(ms(g["last"])) - X(ms(g["first"])), 3))}" height="6" rx="1" '
            'style="fill:var(--s3)"/>'
            for g in sess["agents"]
            if a <= ms(g["first"]) <= b
        )
        lx, lw = lin[i]
        k = lw / sw
        move = c.step(
            f"transform:translateX({r(lx - sx, 2)}px) scaleX({r(k, 4)})",
            "transform:translateX(0) scaleX(1)",
            squeeze[0],
            squeeze[1],
            ease="cubic-bezier(.6,0,.3,1)",
            origin="0 0",
        )
        groups.append(
            f'<g class="{move}"><rect x="{r(sx)}" y="{cy}" width="{r(sw)}" height="1" '
            'fill="none"/>'
            f'<path d="{d}" fill="url(#tlf)"/>'
            f'<path d="{line}" fill="none" stroke-width="1.4" vector-effect="non-scaling-stroke" '
            'style="stroke:var(--s1)"/>'
            f'<rect x="{r(sx)}" y="{my}" width="{r(sw)}" height="6" '
            'style="fill:var(--fg);fill-opacity:.7"/>'
            f"{ticks}</g>"
        )
    draw = c.step(
        "transform:scaleX(0)", "transform:scaleX(1)", 0.3, 2.4, ease="linear", origin="0 0"
    )
    defs = (
        fade("tlf", "var(--s1)", 0.35, 0.04)
        + fade("tlr", "var(--accent)", 0.45, 0.06)
        + f'<clipPath id="tlc"><rect class="{draw}" x="{x0 - 2}" y="{cy - 4}" '
        f'width="{w + 4}" height="{ay + 10 - cy}"/></clipPath>'
        '<pattern id="tlh" width="5" height="5" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)"><rect width="5" height="5" style="fill:var(--panel)"/>'
        '<line x1="0" y1="0" x2="0" y2="5" stroke-width="2.2" style="stroke:var(--border)"/>'
        "</pattern>"
    )
    out = [
        text(PX + 16, cy + 27, "context", "muted", 10),
        text(PX + 16, my + 6, "main", "muted", 10),
        text(PX + 16, ay + 6, "Explore \u00d72", "muted", 10),
        text(PX + 16, ry + rh - 11, "spend", "muted", 10),
        text(PX + 16, ry + rh + 1, "$/hour", "muted", 9.5),
        f'<g clip-path="url(#tlc)">{"".join(groups)}</g>',
    ]
    # The pauses: hatched slivers with their length, once squeezed.
    hatch = c.step("opacity:0", "opacity:1", squeeze[0] + squeeze[1] - 0.1, 0.4)
    marks = []
    for i, p in enumerate(sess["pauses"]):
        px = sq[i][0] + sq[i][1]
        mins = round(p["minutes"])
        label = f"{mins // 60} h {mins % 60:02}" if mins >= 60 else f"{mins} min"
        pw = len(label) * 5.6 + 10
        marks.append(
            f'<rect x="{r(px + 1)}" y="{cy - 2}" width="{sliver - 2}" '
            f'height="{ry + rh + 2 - cy}" fill="url(#tlh)"/>'
            f'<rect x="{r(px + sliver / 2 - pw / 2)}" y="{PY + 7}" width="{r(pw)}" height="15" '
            'rx="7.5" style="fill:var(--surface);stroke:var(--border)"/>'
            + text(px + sliver / 2, PY + 18, label, "fg", 9.5, "middle", 600)
        )
    out.append(f'<g class="{hatch}">{"".join(marks)}</g>')
    # The spend-rate lane draws once the axis is squeezed: it jumps after each pause,
    # where the whole context is written to the cache again.
    rates = spend_rate(req["t"], req["usd"], runs)
    top = max(v for run in rates for _, v in run) * 1.08
    lane = []
    for (a, b), (sx, sw), run in zip(runs, sq, rates, strict=True):
        X = lambda tt, a=a, b=b, sx=sx, sw=sw: sx + (tt - a) / max(b - a, 1) * sw  # noqa: E731
        pts = [(X(tt), ry + rh - v / top * rh) for tt, v in run]
        tr = "".join(f"L{r(px)} {r(py)}" for px, py in pts)
        lane.append(
            f'<path d="M{r(sx)} {ry + rh}{tr}L{r(sx + sw)} {ry + rh}Z" fill="url(#tlr)"/>'
            f'<path d="M{tr[1:]}" fill="none" stroke-width="1.4" style="stroke:var(--accent)"/>'
        )
    rate_on = c.step(
        "transform:scaleX(0)",
        "transform:scaleX(1)",
        squeeze[0] + squeeze[1] + 0.4,
        1.6,
        ease="cubic-bezier(.45,.05,.4,1)",
        origin="0 0",
    )
    defs += (
        f'<clipPath id="tlrc"><rect class="{rate_on}" x="{x0 - 2}" y="{ry - 4}" '
        f'width="{w + 4}" height="{rh + 6}"/></clipPath>'
    )
    out.append(
        f'<line x1="{x0}" y1="{ry + rh + 0.5}" x2="{x0 + w}" y2="{ry + rh + 0.5}" '
        'style="stroke:var(--border)"/>'
        f'<g clip-path="url(#tlrc)">{"".join(lane)}</g>'
    )
    body = '<g class="scene">' + "".join(out) + "</g>"
    p1, p2 = sess["pauses"][:2]
    return card(
        "Timeline",
        [
            "Each session's context, threads and subagents on",
            "one axis, long pauses squeezed, with the spend rate.",
        ],
        f"Timeline: a four-hour session drawn on linear time, mostly empty, then its "
        f"{round(p1['minutes'])} and {round(p2['minutes'])} minute pauses squeeze into "
        "hatched slivers and a spend-rate lane draws beneath, jumping after each pause.",
        c.css(),
        body,
        defs,
    )


KIND_COLOR = {
    "think": "var(--s1)",
    "test": "var(--s2)",
    "files": "var(--s3)",
    "other": "var(--gray)",
}


def every_step(data: dict[str, Any]) -> str:
    c = Css()
    ag = data["coder_agent"]
    th, st, rq = ag["thread"], ag["steps"], ag["requests"]
    n = len(st["start"])
    t0, t1 = st["start"][0], st["end"][-1]
    x0, w = PX + 16, PW - 32
    X = lambda tt: x0 + (tt - t0) / (t1 - t0) * w  # noqa: E731
    sy, sh = PY + 30, 14
    runs = [i for i in range(n) if st["kind"][i] == "test" and "backfill" in st["short"][i]]
    pick = runs[1]
    out = [
        text(x0, PY + 20, f"{th['name']} · {th['agent_type']}", "fg", 11, weight=600),
        text(
            PX + PW - 16,
            PY + 20,
            f"{round(th['alive_seconds'] / 60)} min · {usd(th['usd'])}",
            "muted",
            10.5,
            "end",
        ),
    ]
    strip = []
    for i in range(n):
        a, b = X(st["start"][i]), X(st["end"][i])
        strip.append(
            f'<rect x="{r(a)}" y="{sy}" width="{r(max(b - a - 0.6, 0.8))}" height="{sh}" '
            f'style="fill:{KIND_COLOR.get(st["kind"][i], "var(--gray)")}"/>'
        )
    fill = c.step(
        "transform:scaleX(0)", "transform:scaleX(1)", 0.3, 1.8, ease="linear", origin="0 0"
    )
    defs = (
        f'<clipPath id="esc"><rect class="{fill}" x="{x0}" y="{sy - 2}" width="{w}" '
        f'height="{sh + 4}"/></clipPath>'
    )
    out.append(f'<g clip-path="url(#esc)">{"".join(strip)}</g>')
    for i in runs:
        a, b = X(st["start"][i]), X(st["end"][i])
        mins = round((st["end"][i] - st["start"][i]) / 60e3)
        at = 0.3 + (b - x0) / w * 1.8
        k = c.step("opacity:0", "opacity:1", at, 0.3)
        label = text((a + b) / 2, sy + 10.5, f"{mins} min", "", 9.5, "middle", 600)
        out.append(f'<g class="{k}" style="fill:var(--panel)">{label}</g>')
    out.append(
        legend(
            x0,
            PY + PH - 10,
            [
                ("var(--s1)", "thinking"),
                ("var(--s3)", "files"),
                ("var(--s2)", "make backfill-test"),
            ],
            10,
        )
    )
    # The second run opens: an outline on its step, then its fold drops in.
    a, b = X(st["start"][pick]), X(st["end"][pick])
    ring_on = c.step("opacity:0", "opacity:1", 2.5, 0.3)
    out.append(
        f'<rect class="{ring_on}" x="{r(a - 1.5)}" y="{sy - 2.5}" width="{r(b - a + 2.4)}" '
        f'height="{sh + 5}" rx="3" fill="none" stroke-width="1.8" style="stroke:var(--fg)"/>'
    )
    run = synthetic.test_output("events", 1, len(runs), 4000).splitlines()
    error = next(x for x in run if x.startswith("E ") and "AssertionError" in x)
    summary = next(x for x in run if " failed" in x and " passed" in x)
    secs = round((st["end"][pick] - st["start"][pick]) / 1000)
    lines = [
        ("$ make backfill-test TABLE=events", "fg"),
        (f"Exit code 2 · {secs // 60} min {secs % 60} s", "muted"),
        ("E   " + error[1:].strip(), "clay"),
        (summary.strip("= "), "muted"),
    ]
    bx, by, bw, bh = x0, sy + 26, 250, 62
    drop = c.step(
        "opacity:0;transform:scaleY(.4)",
        "opacity:1;transform:scaleY(1)",
        2.9,
        0.35,
        origin="50% 0",
    )
    box = [
        f'<path d="M{r((a + b) / 2)} {sy + sh + 3}V{by}" stroke-width="1" '
        'style="stroke:var(--border)"/>',
        f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="6" '
        'style="fill:var(--surface);stroke:var(--border)"/>',
    ]
    for j, (s, cls) in enumerate(lines):
        k = c.step("opacity:0", "opacity:1", 3.3 + j * 0.35, 0.25)
        box.append(
            f'<g class="{k}">'
            + text(bx + 9, by + 16 + j * 13, s, f"{cls} mono", 9.5, weight=600 if j == 0 else 400)
            + "</g>"
        )
    out.append(f'<g class="{drop}">{"".join(box)}</g>')
    # What reading that output cost: the next request, a full cache rewrite.
    nxt = st["next"][pick]
    k = c.step(
        "opacity:0;transform:translateX(-6px)", "opacity:1;transform:translateX(0)", 4.9, 0.4
    )
    rx = bx + bw + 14
    out.append(
        f'<g class="{k}">'
        + text(rx, by + 14, "next request", "muted", 10.5)
        + text(rx, by + 34, usd(rq["usd"][nxt]), "clay", 18, weight=600)
        + text(rx, by + 50, f"{rq['cache_write'][nxt] / 1e3:.0f}k tokens", "muted", 10)
        + text(rx, by + 62, "written to cache", "muted", 10)
        + "</g>"
    )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Every step",
        [
            "One agent from spawn to hand-back: what it ran,",
            "for how long, and what each step cost.",
        ],
        f"Every step: a coder agent's steps filling a strip left to right, three long test "
        f"runs in orange; the second opens to its command, its failing test and exit code, "
        f"beside the next request, {usd(rq['usd'][nxt])}.",
        c.css(),
        body,
        defs,
    )


def odometer(
    c: Css, x: float, y: float, value: int, size: float, t: float, cid: str
) -> tuple[str, str]:
    """`$value` with its digits rolling up like an odometer from t; returns (defs, markup)."""
    digits = str(value)
    lh = size * 1.18
    cw = size * 0.58
    defs = (
        f'<clipPath id="{cid}"><rect x="{r(x - 2)}" y="{r(y - size * 0.95)}" '
        f'width="{r(cw * (len(digits) + 1) + 6)}" height="{r(lh)}"/></clipPath>'
    )
    strips = [text(x, y, "$", "fg", size, weight=600)]
    for i, d in enumerate(digits):
        seq = list(range(10)) * (len(digits) - i) + list(range(int(d) + 1))
        dist = (len(seq) - 1) * lh
        k = c.step(
            "transform:translateY(0)",
            f"transform:translateY(-{r(dist)}px)",
            t,
            2.0 + i * 0.25,
            ease="cubic-bezier(.2,.7,.25,1)",
        )
        tsp = "".join(
            f'<tspan x="{r(x + cw * (i + 1))}" dy="{r(lh) if j else 0}">{n}</tspan>'
            for j, n in enumerate(seq)
        )
        strips.append(
            f'<g class="{k}"><text class="fg" x="{r(x + cw * (i + 1))}" y="{r(y)}" '
            f'font-size="{size}" font-weight="600">{tsp}</text></g>'
        )
    return defs, f'<g clip-path="url(#{cid})">{"".join(strips)}</g>'


def checks(data: dict[str, Any]) -> str:
    c = Css()
    ch = data["checks"]
    total = ch["total"]
    weekly = round(total["usd_per_week"])
    kinds = [k for k in ch["rollups"] if not k["judgement_call"]]
    rows = sorted(kinds, key=lambda k: -k["saving_usd_per_week"])[:3]
    applied = next((k for k in rows if k.get("applied")), None)
    worked = next(
        (m for m in data["changes"]["changes"] if applied and m["kind"] == applied["kind"]), None
    )
    calls = total.get("judgement_calls") or {}
    x0, w = PX + 16, PW - 32
    # The total, its digits rolling up, and what it adds.
    defs, odo = odometer(c, x0 + 40, PY + 28, weekly, 24, 0.4, "od")
    out = [text(x0, PY + 28, "up to", "muted", 12), odo]
    wk = c.step("opacity:0", "opacity:1", 2.4, 0.4)
    out.append(
        f'<g class="{wk}">'
        + text(x0 + 40 + 14 * (len(str(weekly)) + 1) + 8, PY + 28, "a week", "muted", 12)
        + text(
            PX + PW - 16,
            PY + 28,
            f"{len(kinds)} kinds · overlaps counted once",
            "muted",
            10.5,
            "end",
        )
        + "</g>"
    )
    # One row per kind: its saving, its bar split by the projects it spans.
    top = rows[0]["saving_usd_per_week"]
    for i, k in enumerate(rows):
        y = PY + 48 + i * 20
        overlap = k["counted"] == "none"
        t = 0.6 + i * 0.35
        show = c.step(
            "opacity:0;transform:translateX(-6px)", "opacity:1;transform:translateX(0)", t, 0.35
        )
        n = len(k["scopes"])
        where = f"{n} {k['scope_noun']}{'s' if n > 1 else ''}"
        amount = f"{usd(k['saving_usd_per_week'])}/wk"
        out.append(
            f'<g class="{show}">'
            + text(x0, y, k["title"], "fg", 11, weight=600)
            + text(
                PX + PW - 16,
                y,
                amount,
                "muted" if overlap else "clay",
                11,
                "end",
                600,
            )
            + text(
                PX + PW - 16 - len(amount) * 6.6 - 8,
                y,
                "overlaps" if overlap else where,
                "muted",
                10,
                "end",
            )
            + "</g>"
        )
        if k is applied:
            cx = x0 + len(k["title"]) * 5.7 + 8
            chip = c.step(
                "opacity:0;transform:scale(.6)",
                "opacity:1;transform:scale(1)",
                2.6,
                0.3,
                ease="cubic-bezier(.3,1.6,.5,1)",
                origin="50% 50%",
            )
            out.append(
                f'<g class="{chip}"><rect x="{r(cx)}" y="{y - 9.5}" width="44" height="13" '
                'rx="6.5" fill="none" style="stroke:var(--ok)"/>'
                f'<text x="{r(cx + 22)}" y="{y}" font-size="9" font-weight="600" '
                'text-anchor="middle" style="fill:var(--ok)">applied</text></g>'
            )
        grow = c.step("transform:scaleX(0)", "transform:scaleX(1)", t + 0.2, 0.7, origin="0 0")
        bx, bars = x0, []
        full = w * k["saving_usd_per_week"] / top
        for j, sc in enumerate(k["scopes"][:6]):
            bw = full * sc["share"]
            bars.append(
                f'<rect x="{r(bx)}" y="{r(y + 4)}" width="{r(max(bw - 1, 0.6))}" height="5" '
                f'style="fill:var(--s{j + 1})"/>'
            )
            bx += bw
        out.append(
            f'<g class="{grow}" style="opacity:{0.35 if overlap else 1}">' + "".join(bars) + "</g>"
        )
    if worked and worked["verdict"] == "working":
        y = PY + 46 + len(rows) * 20
        on = datetime.fromisoformat(worked["applied_on"])
        per = worked["measure"].replace(" 1,000 ", " 1k ")
        per = per[0].lower() + per[1:]
        wk = c.step(
            "opacity:0;transform:translateY(-5px)", "opacity:1;transform:translateY(0)", 3.0, 0.4
        )
        out.append(
            f'<g class="{wk}">'
            f'<circle cx="{x0 + 5}" cy="{y - 3.5}" r="5" style="fill:var(--ok)"/>'
            f'<path d="M{x0 + 2.6} {y - 3.6}l1.8 1.8l3 -3.6" fill="none" stroke-width="1.5" '
            'stroke-linecap="round" stroke-linejoin="round" style="stroke:var(--panel)"/>'
            f'<text x="{x0 + 15}" y="{y}" font-size="9.5">'
            '<tspan font-weight="600" style="fill:var(--ok)">Worked</tspan>'
            f'<tspan class="muted"> since {on:%b} {on.day}: {worked["before"]:.1f} → '
            f"{worked['after']:.0f} {per}, "
            f"{usd(worked['measured_usd_per_week'])}/wk saved</tspan></text></g>"
        )
    if calls.get("findings"):
        y = PY + 52 + (len(rows) + 1) * 20
        n = calls["findings"]
        jc = c.step("opacity:0", "opacity:1", 0.6 + len(rows) * 0.35 + 0.4, 0.4)
        out.append(
            f'<g class="{jc}">'
            + f'<rect x="{x0}" y="{y - 10}" width="{w}" height="15" rx="4" fill="none" '
            'stroke-dasharray="3 3" style="stroke:var(--border)"/>'
            + text(
                x0 + 7,
                y + 1,
                f"+ {n} judgement call{'s' if n > 1 else ''} on quality, up to "
                f"{usd(calls['usd_per_week'])}/wk, kept apart",
                "muted",
                10,
            )
            + "</g>"
        )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Checks",
        [
            "Setup habits ranked by what they cost a week, with",
            "the change to make, and later whether it worked.",
        ],
        f"Checks: up to ${weekly} a week counting up, then one row per kind of habit, "
        + ", ".join(f"{k['title']} {usd(k['saving_usd_per_week'])}" for k in rows)
        + ", each bar split by the projects it spans; overlapping kinds are counted once"
        + (
            f"; {applied['title']} is marked applied, and it worked: "
            f"{worked['before']:.1f} to {worked['after']:.0f} "
            f"{worked['measure'][0].lower()}{worked['measure'][1:]}, "
            f"{usd(worked['measured_usd_per_week'])} a week saved"
            if applied and worked and worked["verdict"] == "working"
            else ""
        )
        + (
            f", and {calls['findings']} judgement call kept apart."
            if calls.get("findings")
            else "."
        ),
        c.css(),
        body,
        defs,
    )


def ring(c: Css, cx: float, cy: float, pct: float, t: float, warn: bool) -> str:
    """The website's usage ring: a track and an arc of `pct` percent, filling in from t."""
    rad = 12.0
    circ = 2 * math.pi * rad
    off = circ * (1 - min(pct, 100) / 100)
    k = c.step(
        f"stroke-dashoffset:{r(circ)}", f"stroke-dashoffset:{r(off)}", t, 1.2, ease="ease-out"
    )
    color = "var(--clay)" if warn else "var(--accent)"
    return (
        f'<circle cx="{r(cx)}" cy="{r(cy)}" r="{rad}" fill="none" stroke-width="4" '
        'style="stroke:var(--border)"/>'
        f'<circle class="{k}" cx="{r(cx)}" cy="{r(cy)}" r="{rad}" fill="none" stroke-width="4" '
        f'stroke-linecap="round" stroke-dasharray="{r(circ)}" '
        f'transform="rotate(-90 {r(cx)} {r(cy)})" style="stroke:{color}"/>'
    )


def limits(data: dict[str, Any]) -> str:
    c = Css()
    wins = data["limits"]["windows"]
    week, five = wins["seven_day"], wins["five_hour"]
    fc = week["forecast"]
    fan = fc["fan"]
    # the current window and the one before it: a full week, then the forecast
    cur = week["segments"][0]
    t0 = ms(cur["start"]) - 7 * 86400e3
    segs = [s for s in reversed(week["segments"]) if s["points"] and ms(s["resets_at"]) > t0]
    t1 = fan[-1][0]
    top = max([100.0] + [p[3] for p in fan])
    ymax = math.ceil(top / 25) * 25 if top > 100 else 100
    x0, y0, w, h = PX + 40, PY + 30, 222, 100
    X = lambda t: x0 + (t - t0) / (t1 - t0) * w  # noqa: E731
    Y = lambda v: y0 + h - v / ymax * h  # noqa: E731
    out = [text(PX + 16, PY + 18, "Weekly limit", "fg", 11, weight=600)]
    for v in (0, 50, 100):
        style = "stroke:var(--accent);stroke-dasharray:4 3" if v == 100 else "stroke:var(--border)"
        if v == 50:
            style += ";stroke-dasharray:2 3"
        out.append(f'<line x1="{x0}" y1="{r(Y(v))}" x2="{x0 + w}" y2="{r(Y(v))}" style="{style}"/>')
        out.append(text(x0 - 6, Y(v) + 3.5, f"{v}%", "muted", 9.5, "end"))
    # Each window's readings: a rising line that holds to its reset, then drops to 0.
    lines = []
    for sg in segs:
        pts = sg["points"]
        pts = [p for p in pts if p[0] >= t0]
        d = "".join(f"{'L' if i else 'M'}{r(X(t))} {r(Y(v))}" for i, (t, v) in enumerate(pts))
        if sg["reset"]:
            rt = ms(sg["resets_at"])
            d += f"L{r(X(rt))} {r(Y(pts[-1][1]))}"
            lines.append(
                f'<path d="M{r(X(rt))} {r(Y(pts[-1][1]))}V{r(Y(0))}" fill="none" '
                'stroke-dasharray="3 3" style="stroke:var(--muted)"/>'
            )
        lines.append(
            f'<path d="{d}" fill="none" stroke-width="1.8" stroke-linejoin="round" '
            'style="stroke:var(--fg)"/>'
        )
    # The forecast from now to the reset: the 80 % range as a fan, the median dashed.
    band = "".join(f"{'L' if i else 'M'}{r(X(p[0]))} {r(Y(p[3]))}" for i, p in enumerate(fan))
    band += "".join(f"L{r(X(p[0]))} {r(Y(p[2]))}" for p in reversed(fan)) + "Z"
    med = "".join(f"{'L' if i else 'M'}{r(X(p[0]))} {r(Y(p[1]))}" for i, p in enumerate(fan))
    last = segs[-1]["points"][-1]
    now_x = X(fan[0][0])
    draw = (0.4, 3.6)
    reveal = c.step(
        "transform:scaleX(0)",
        "transform:scaleX(1)",
        draw[0],
        draw[1] - draw[0],
        ease="cubic-bezier(.45,.05,.4,1)",
        origin="0 0",
    )
    defs = fade("lf", "var(--accent)", 0.34, 0.06) + (
        f'<clipPath id="lc"><rect class="{reveal}" x="{x0 - 2}" y="{y0 - 4}" '
        f'width="{r(now_x - x0 + 3)}" height="{h + 8}"/></clipPath>'
    )
    fan_in = c.step("transform:scaleX(0)", "transform:scaleX(1)", draw[1], 1.0, origin="0 0")
    fan_svg = (
        f'<g class="{fan_in}"><path d="{band}" fill="url(#lf)"/>'
        f'<path d="{med}" fill="none" stroke-width="1.8" stroke-dasharray="5 3" '
        'style="stroke:var(--accent)"/>'
        f'<path d="M{r(X(last[0]))} {r(Y(last[1]))}L{r(now_x)} {r(Y(fan[0][1]))}" fill="none" '
        'stroke-width="1.5" stroke-dasharray="2 3" style="stroke:var(--fg)"/></g>'
    )
    nowk = c.step("opacity:0", "opacity:1", draw[1] - 0.2, 0.3)
    out.append(f'<g clip-path="url(#lc)">{"".join(lines)}</g>' + fan_svg)
    out.append(
        f'<g class="{nowk}"><line x1="{r(now_x)}" y1="{y0 - 2}" x2="{r(now_x)}" y2="{y0 + h}" '
        'style="stroke:var(--muted);stroke-dasharray:1 2"/>'
        + text(now_x, y0 - 5, "now", "muted", 9.5, "middle")
        + "</g>"
    )
    # Both windows: where each stands and where it lands at its reset.
    rx = PX + 290
    for i, (name, wv) in enumerate([("5-hour", five), ("Weekly", week)]):
        f = wv["forecast"]
        cy = PY + 46 + i * 58
        t = 0.6 + i * 0.4
        out.append(ring(c, rx, cy, f["now"], t, f["high"] >= 100))
        k = c.step("opacity:0", "opacity:1", draw[1] + 0.5 + i * 0.3, 0.4)
        out.append(
            text(rx + 20, cy - 8, name, "fg", 11, weight=600)
            + text(rx + 20, cy + 6, f"{f['now']:.0f}% now", "muted", 10.5)
            + f'<g class="{k}">'
            + text(rx + 20, cy + 20, f"{f['median']:.0f}% at reset", "clay", 10.5, weight=600)
            + "</g>"
        )
    body = '<g class="scene">' + "".join(out) + "</g>"
    fw, ff = week["forecast"], five["forecast"]
    return card(
        "Limits",
        [
            "Records your 5-hour and weekly usage from the",
            "status line, and forecasts each at its reset.",
        ],
        f"Limits: the weekly usage rising and dropping to zero at each reset, then a "
        f"forecast fan from now to the next reset (median {fw['median']:.0f} %, "
        f"{fw['low']:.0f} to {fw['high']:.0f} %); beside it, the 5-hour window at "
        f"{ff['now']:.0f} % now and {ff['median']:.0f} % at its reset, the weekly at "
        f"{fw['now']:.0f} % and {fw['median']:.0f} %.",
        c.css(),
        body,
        defs,
    )


# Ask Roger's answer on the synthetic logs, about the largest spike, asked "What does
# this show, and what stands out?" in `capcom serve`: Haiku through the claude CLI,
# captured once (the card must not need a network call to build). Its numbers are
# checked against the data below, so a change to the synthetic logs fails loudly.
ASK_QUESTION = "What does this show, and what stands out?"
ASK_ANSWER = (
    "This part shows a $51.96 spike in quarry-etl, caused by the cache expiring during "
    "long tool calls. The biggest cause is 12 requests that ran after a Bash call took "
    "13 minutes, past the 5-minute cache lifetime, so 7.7M tokens were written again."
)
ASK_CHIPS = ["What is the prompt cache?", "How do I run long jobs safely?"]


def wrap(s: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    return [*lines, cur]


def ask_roger(data: dict[str, Any]) -> str:
    c = Css()
    s = max(data["spikes"]["spikes"], key=lambda x: x["excess_usd"])
    if usd(s["excess_usd"]) not in ASK_ANSWER or s["project"] not in ASK_ANSWER:
        raise SystemExit(
            f"Ask Roger's kept answer no longer matches the top spike ({s['headline']}, "
            f"{usd(s['excess_usd'])}): capture it again in capcom serve"
        )
    out = []
    x0 = PX + 16
    about = c.step("opacity:0", "opacity:1", 0.3, 0.3)
    out.append(
        f'<g class="{about}">'
        + text(x0, PY + 20, "About", "muted", 10)
        + text(x0 + 32, PY + 20, s["headline"], "fg", 10, weight=600)
        + "</g>"
    )
    # The question, a bubble on the right.
    qw = len(ASK_QUESTION) * 5.5 + 20
    qx = PX + PW - 16 - qw
    ask = c.step(
        "opacity:0;transform:translateY(5px)", "opacity:1;transform:translateY(0)", 0.7, 0.35
    )
    out.append(
        f'<g class="{ask}"><rect x="{r(qx)}" y="{PY + 25}" width="{r(qw)}" height="20" '
        f'rx="10" style="fill:var(--clay)"/>'
        f'<text x="{r(qx + qw / 2)}" y="{PY + 39}" font-size="10.5" text-anchor="middle" '
        f'style="fill:var(--panel)">{escape(ASK_QUESTION)}</text></g>'
    )
    # Roger waves, and his answer types in beside him.
    rw = 40
    lines = wrap(ASK_ANSWER, 66)
    bx, by = PX + 62, PY + 50
    bw, bh = PX + PW - 16 - bx, 16 + 13 * len(lines)
    _, vy, vw, _ = roger.VIEWBOX
    rx_, ry_ = PX + 14, by + bh - (roger.feet() - vy) * rw / vw  # his feet on the bubble's base
    bub = c.step(
        "opacity:0;transform:scale(.9)", "opacity:1;transform:scale(1)", 1.4, 0.3, origin="0 100%"
    )
    out.append(
        roger.group(rx_, ry_, rw)
        + f'<g class="{bub}"><rect x="{r(bx)}" y="{by}" width="{r(bw)}" height="{bh}" rx="9" '
        'style="fill:var(--surface);stroke:var(--border)"/>'
        f'<path d="M{r(bx + 0.5)} {by + bh - 16}l-7 8l7 -2z" style="fill:var(--surface)"/></g>'
    )
    t = 1.8
    clips = []
    for j, line in enumerate(lines):
        d = len(line) * 0.018
        k = c.step(
            "transform:scaleX(0)",
            "transform:scaleX(1)",
            t,
            d,
            ease=f"steps({len(line)})",
            origin="0 0",
        )
        clips.append(
            f'<clipPath id="arc{j}"><rect class="{k}" x="{r(bx + 8)}" y="{by + 3 + j * 13}" '
            f'width="{r(bw - 12)}" height="13"/></clipPath>'
        )
        out.append(
            f'<g clip-path="url(#arc{j})">'
            + text(bx + 10, by + 14 + j * 13, line, "fg", 10)
            + "</g>"
        )
        t += d + 0.05
    cx = bx
    chips = []
    for j, chip in enumerate(ASK_CHIPS):
        cw = len(chip) * 4.9 + 16
        k = c.step("opacity:0", "opacity:1", t + 0.2 + j * 0.2, 0.3)
        chips.append(
            f'<g class="{k}"><rect x="{r(cx)}" y="{by + bh + 6}" width="{r(cw)}" height="17" '
            'rx="8.5" fill="none" style="stroke:var(--border)"/>'
            + text(cx + cw / 2, by + bh + 18, chip, "muted", 9.5, "middle")
            + "</g>"
        )
        cx += cw + 6
    out += chips
    arm = roger.on_loop("arm", [1.0])
    eyes = roger.on_loop("eyes", [1.0, 5.5])
    _, _, ease = roger.motion("arm")
    css = (
        c.css()
        + roger.theme_css()
        + f".rg-arm{{animation:rg-arm {r(LOOP, 2)}s {ease} infinite}}"
        + keyframes("rg-arm", LOOP, arm)
        + f".rg-eyes{{animation:rg-eyes {r(LOOP, 2)}s {ease} infinite}}"
        + keyframes("rg-eyes", LOOP, eyes)
    )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Ask Roger",
        [
            "Ask about any chart or number on the page: a",
            "plain answer from capcom's own totals, if you opt in.",
        ],
        f"Ask Roger: the mascot waves as a question about the largest spike gets a typed "
        f"answer: {ASK_ANSWER}",
        css,
        body,
        defs="".join(clips),
    )


def private(data: dict[str, Any]) -> str:
    c = Css()
    cx, cy = PX + 70, PY + 78
    # A padlock whose shackle drops shut.
    shut = c.step(
        "transform:translateY(-9px)",
        "transform:translateY(0)",
        1.0,
        0.35,
        ease="cubic-bezier(.5,0,.6,1.6)",
    )
    glow = c.step("opacity:0", "opacity:1", 1.3, 0.4)
    out = [
        f'<circle class="{glow}" cx="{cx}" cy="{cy}" r="46" style="fill:var(--accent);'
        'fill-opacity:.12"/>',
        f'<path class="{shut}" d="M{cx - 14} {cy - 4}v-12a14 14 0 0 1 28 0v12" fill="none" '
        'stroke-width="6" stroke-linecap="round" style="stroke:var(--fg)"/>',
        f'<rect x="{cx - 24}" y="{cy - 6}" width="48" height="38" rx="7" '
        'style="fill:var(--accent)"/>',
        f'<circle cx="{cx}" cy="{cy + 9}" r="4.5" style="fill:var(--panel)"/>',
        f'<rect x="{cx - 1.8}" y="{cy + 10}" width="3.6" height="10" rx="1.8" '
        'style="fill:var(--panel)"/>',
    ]
    rows = [
        ("Reads your logs read-only", "never writes, moves or locks them"),
        ("No network", "Ask Roger only, when you turn it on"),
        ("Tool details read live", "never stored, cached or exported"),
    ]
    x0 = PX + 140
    for i, (head, sub) in enumerate(rows):
        y = PY + 38 + i * 40
        k = c.step(
            "opacity:0;transform:translateX(-8px)",
            "opacity:1;transform:translateX(0)",
            1.7 + i * 0.5,
            0.45,
        )
        tick = (
            f'<circle cx="{x0 + 8}" cy="{y - 4}" r="8" style="fill:var(--ok)"/>'
            f'<path d="M{x0 + 4} {y - 4}l3 3l5 -6" fill="none" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round" style="stroke:var(--panel)"/>'
        )
        out.append(
            f'<g class="{k}">{tick}'
            + text(x0 + 24, y, head, "fg", 12.5, weight=600)
            + text(x0 + 24, y + 15, sub, "muted", 11)
            + "</g>"
        )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Private",
        [
            "Your logs hold your conversations: capcom reads",
            "them where they are and keeps it all local.",
        ],
        "Private: a padlock snapping shut beside three checks: reads your logs "
        "read-only, no network (Ask Roger only, when you turn it on), tool details read "
        "live and never stored.",
        c.css(),
        body,
    )


CARDS: dict[str, Callable[[dict[str, Any]], str]] = {
    "adding-up": adding_up,
    "why": why,
    "timeline": timeline,
    "every-step": every_step,
    "checks": checks,
    "limits": limits,
    "ask-roger": ask_roger,
    "private": private,
}


def build(data: dict[str, Any], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for name, fn in CARDS.items():
        p = out / f"feature-{name}.svg"
        p.write_text(fn(data))
        made.append(p)
    return made


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    for p in build(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2])):
        print(f"{p}  {p.stat().st_size / 1024:.1f} KiB")


if __name__ == "__main__":
    main()
