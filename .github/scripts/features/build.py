"""The README's eight feature cards, 420 x 240 animated SVGs.

    python .github/scripts/features/build.py <data.json> <out-dir>

<data.json> is demo/data.py's output: every chart and number on a card is what
capcom computed on the synthetic logs (spend by project, a spike's window and
cause, the fan-out session's subagents, the checks rolled up by kind, the usage
windows and their forecast, `capcom calibrate`); the OpenClaw and Private cards
are diagrams. Writes feature-{adding-up,why,fan-out,checks,limits,openclaw,trust,
private}.svg into <out-dir>.

Each card runs on one loop: the scene fades in, plays, holds its last frame and
fades out. Every element's own style is its last frame, so prefers-reduced-motion
(all animations off) shows the finished picture.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    body = (
        '<g class="scene">'
        + legend(PX + 16, PY + 20, list(zip(colors[:3], labels[:3], strict=False)))
        + f'<line x1="{x0}" y1="{y0 + h + 0.5}" x2="{x0 + w}" y2="{y0 + h + 0.5}" '
        'style="stroke:var(--border)"/>'
        + f'<g clip-path="url(#ac)">{chart}</g>'
        + "".join(pins)
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
        f"{usd(total[-1], cents=False)}.",
        c.css(),
        body,
        defs,
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


def fan_out(data: dict[str, Any]) -> str:
    c = Css()
    sess = data["fan_out_session"]
    spike = next(s for s in data["spikes"]["spikes"] if s["cause"] == "fan_out")
    agents = sorted(sess["agents"], key=lambda a: ms(a["first"]))
    burst = [a for a in agents if ms(spike["start"]) - 60_000 <= ms(a["first"]) <= ms(spike["end"])]
    t0 = min(ms(a["first"]) for a in burst) - 2 * 60_000
    t1 = max(ms(a["last"]) for a in burst) + 90_000
    types = list(dict.fromkeys(a["agent_type"] for a in burst))
    color = {t: f"var(--s{i + 1})" for i, t in enumerate(types)}
    x0, y0, w = PX + 52, PY + 34, PW - 68
    X = lambda t: x0 + (min(max(t, t0), t1) - t0) / (t1 - t0) * w  # noqa: E731
    lane = min(6.5, 82 / len(burst))
    out = [text(PX + 16, y0 + 5, "main", "muted", 10.5)]
    # The main thread: a lane that runs on through the burst.
    m0, m1 = X(ms(sess["main"]["first"])), X(ms(sess["main"]["last"]))
    grow = c.step(
        "transform:scaleX(0)", "transform:scaleX(1)", 0.3, 4.2, ease="linear", origin="0 0"
    )
    out.append(
        f'<rect class="{grow}" x="{r(m0)}" y="{y0}" width="{r(m1 - m0)}" height="6" '
        f'rx="3" style="fill:var(--fg);fill-opacity:.7"/>'
    )
    for i, a in enumerate(burst):
        ax, ay = X(ms(a["first"])), y0 + 12 + i * lane
        aw = max(X(ms(a["last"])) - ax, 4)
        at = 0.3 + (ax - m0) / max(m1 - m0, 1) * 4.2 + i * 0.06
        k = c.step(
            "transform:scaleX(0);opacity:0",
            "transform:scaleX(1);opacity:1",
            at,
            0.5,
            origin="0 50%",
        )
        out.append(
            f'<rect class="{k}" x="{r(ax)}" y="{r(ay)}" width="{r(aw)}" '
            f'height="{r(lane - 1.6)}" rx="1.5" style="fill:{color[a["agent_type"]]}"/>'
        )
    out.append(text(PX + 16, y0 + 12 + lane * len(burst) / 2 + 4, "agents", "muted", 10.5))
    note_t = 0.3 + 4.2 * 0.75
    note = c.step(
        "opacity:0;transform:translateY(4px)", "opacity:1;transform:translateY(0)", note_t, 0.5
    )
    head = spike["headline"]
    amount = f"+{usd(spike['excess_usd'])}"
    out.append(
        f'<g class="{note}"><text x="{PX + PW - 16}" y="{PY + 20}" font-size="12" '
        f'text-anchor="end"><tspan class="fg" font-weight="600">{escape(head)}</tspan>'
        f'<tspan class="muted"> · </tspan><tspan class="clay" font-weight="600">'
        f"{escape(amount)}</tspan></text></g>"
    )
    out.append(legend(PX + 16, PY + PH - 10, [(color[t], t) for t in types]))
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Fan-out",
        [
            "Subagents get lanes of their own: see a burst of",
            "spawns, what each cost and which call started it.",
        ],
        f"Fan-out: a session's main thread runs along a timeline while {len(burst)} "
        f"subagent lanes burst in below it; {head}, {amount}.",
        c.css(),
        body,
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
    rows = sorted(kinds, key=lambda k: -k["saving_usd_per_week"])[:4]
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
    if calls.get("findings"):
        y = PY + 52 + len(rows) * 20
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
            "Setup habits by kind, ranked by what they cost a",
            "week, net of a fresh start, with the change to make.",
        ],
        f"Checks: up to ${weekly} a week counting up, then one row per kind of habit, "
        + ", ".join(f"{k['title']} {usd(k['saving_usd_per_week'])}" for k in rows)
        + ", each bar split by the projects it spans; overlapping kinds are counted once"
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


def openclaw(data: dict[str, Any]) -> str:
    c = Css()
    del data
    sources = [
        ("Claude Code", "~/.claude", PY + 22),
        ("OpenClaw", "~/.openclaw", PY + 86),
    ]
    out = []
    for i, (name, path, y) in enumerate(sources):
        k = c.step(
            "opacity:0;transform:translateY(4px)",
            "opacity:1;transform:translateY(0)",
            0.4 + i * 0.3,
            0.4,
        )
        out.append(
            f'<g class="{k}"><rect x="{PX + 16}" y="{y}" width="104" height="42" rx="7" '
            'style="fill:var(--surface);stroke:var(--border)"/>'
            + text(PX + 26, y + 18, name, "fg", 11.5, weight=600)
            + f'<text class="mono muted" x="{PX + 26}" y="{y + 33}" font-size="10">'
            + f"{path}</text></g>"
        )
    lx0, lx1 = PX + 182, PX + PW - 16
    lanes = [
        (
            "your sessions",
            "var(--s1)",
            PY + 36,
            0,
            [(0.02, 0.2), (0.3, 0.52), (0.63, 0.75), (0.84, 0.97)],
        ),
        (
            "OpenClaw, through Claude Code",
            "var(--s2)",
            PY + 80,
            0,
            [(0.12, 0.3), (0.46, 0.58), (0.7, 0.92)],
        ),
        (
            "OpenClaw, straight to a provider",
            "var(--s3)",
            PY + 124,
            1,
            [(0.05, 0.16), (0.24, 0.4), (0.55, 0.66), (0.78, 0.88)],
        ),
    ]
    for i, (label, color, y, src, spans) in enumerate(lanes):
        sy = sources[src][2] + 21
        t = 1.2 + i * 0.45
        flow = c.step("stroke-dashoffset:120", "stroke-dashoffset:0", t, 0.6, ease="ease-in-out")
        mx = (PX + 120 + lx0) / 2
        out.append(
            f'<path class="{flow}" d="M{PX + 121} {sy}C{r(mx)} {sy} {r(mx)} {y} {lx0 - 6} {y}" '
            f'fill="none" stroke-width="1.5" stroke-dasharray="120" style="stroke:{color}"/>'
        )
        lab = c.step("opacity:0", "opacity:1", t + 0.3, 0.4)
        out.append(f'<g class="{lab}">' + text(lx0, y - 9, label, "muted", 10) + "</g>")
        out.append(
            f'<line x1="{lx0}" y1="{y + 0.5}" x2="{lx1}" y2="{y + 0.5}" '
            'style="stroke:var(--border)"/>'
        )
        for j, (a, b) in enumerate(spans):
            bx = lx0 + a * (lx1 - lx0)
            k = c.step(
                "transform:scaleX(0)",
                "transform:scaleX(1)",
                t + 0.6 + j * 0.18,
                0.4,
                origin="0 50%",
            )
            out.append(
                f'<rect class="{k}" x="{r(bx)}" y="{y - 4}" width="{r((b - a) * (lx1 - lx0))}" '
                f'height="8" rx="2" style="fill:{color}"/>'
            )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "OpenClaw",
        [
            "Its Claude Code sessions and its direct calls to",
            "model providers, on the same timeline, read-only.",
        ],
        "OpenClaw: Claude Code's logs and OpenClaw's feed three lanes of one timeline: your own "
        "sessions, OpenClaw's sessions through Claude Code, and OpenClaw's calls straight to a "
        "model provider.",
        c.css(),
        body,
    )


def trust(data: dict[str, Any]) -> str:
    c = Css()
    rows = [x for x in data["calibrate"] if x["scope"] == "comparable"]
    cs = sum(x["cost_state"] for x in rows)
    cc = sum(x["capcom"] for x in rows)
    spread = (cc - cs) / cs * 100
    x0, y0, w = PX + 16, PY + 26, PW - 32
    lw = 120
    out = []
    for i, (label, v, color) in enumerate(
        [("Claude Code's own", cs, "var(--muted)"), ("capcom", cc, "var(--accent)")]
    ):
        y = y0 + i * 24
        full = (w - lw - 70) * v / max(cs, cc)
        k = c.step(
            "transform:scaleX(0)",
            "transform:scaleX(1)",
            0.4 + i * 0.15,
            2.2,
            ease="cubic-bezier(.3,.6,.3,1)",
            origin="0 0",
        )
        out.append(text(x0, y + 4, label, "fg" if i else "muted", 11, weight=600 if i else 400))
        out.append(
            f'<rect class="{k}" x="{x0 + lw}" y="{y - 6}" width="{r(full)}" height="12" '
            f'rx="3" style="fill:{color};fill-opacity:{1 if i else 0.55}"/>'
        )
        n = c.step("opacity:0", "opacity:1", 2.4, 0.4)
        out.append(f'<g class="{n}">' + text(x0 + lw + full + 6, y + 4, usd(v), "fg", 11) + "</g>")
    # Each comparable session's spread on a -5..+5 % strip, landing inside the 2 % band.
    sy = y0 + 76
    sx0, sw = x0 + 30, w - 60
    S = lambda p: sx0 + (p + 5) / 10 * sw  # noqa: E731
    band = c.step("opacity:0", "opacity:1", 2.7, 0.5)
    out.append(
        f'<rect class="{band}" x="{r(S(-2))}" y="{sy - 14}" width="{r(S(2) - S(-2))}" '
        'height="28" rx="4" style="fill:var(--ok);fill-opacity:.16"/>'
    )
    out.append(
        f'<line x1="{sx0}" y1="{sy}" x2="{sx0 + sw}" y2="{sy}" style="stroke:var(--border)"/>'
    )
    for p in (-5, -2, 0, 2, 5):
        out.append(
            f'<line x1="{r(S(p))}" y1="{sy - 3}" x2="{r(S(p))}" y2="{sy + 3}" '
            'style="stroke:var(--border)"/>'
        )
        out.append(text(S(p), sy + 26, f"{p:+d} %" if p else "0", "muted", 10, "middle"))
    for i, row in enumerate(rows):
        jitter = ((i * 7) % 5 - 2) * 2.2
        start = -4.6 + (i * 37 % 92) / 10
        k = c.step(
            f"transform:translate({r(S(start) - S(row['spread']))}px,-22px);opacity:0",
            "transform:translate(0,0);opacity:1",
            1.0 + i * 0.08,
            0.9,
            ease="cubic-bezier(.3,.8,.3,1)",
        )
        out.append(
            f'<circle class="{k}" cx="{r(S(row["spread"]))}" cy="{r(sy + jitter)}" '
            'r="3" style="fill:var(--accent);fill-opacity:.85"/>'
        )
    lab = c.step("opacity:0", "opacity:1", 3.2, 0.4)
    out.append(
        f'<g class="{lab}">'
        + text(S(2) + 8, sy - 6, "within 2 %", "fg", 11.5, weight=600)
        + text(S(2) + 8, sy + 8, f"{len(rows)} sessions", "muted", 10.5)
        + "</g>"
    )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Trust",
        ["Checked against Claude Code's own cost records,", "session by session."],
        f"Trust: capcom's total ({usd(cc)}) against Claude Code's own ({usd(cs)}), "
        f"{spread:+.1f} %, and each session's spread landing within 2 %.",
        c.css(),
        body,
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
        ("Reads the logs read-only", "never writes, moves or locks them"),
        ("Makes no network requests", "prices ship with it"),
        ("Nothing leaves your machine", "the website is on 127.0.0.1"),
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
        "Private: a padlock snapping shut beside three checks: reads the logs "
        "read-only, makes no network requests, nothing leaves your machine.",
        c.css(),
        body,
    )


CARDS: dict[str, Callable[[dict[str, Any]], str]] = {
    "adding-up": adding_up,
    "why": why,
    "fan-out": fan_out,
    "checks": checks,
    "limits": limits,
    "openclaw": openclaw,
    "trust": trust,
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
