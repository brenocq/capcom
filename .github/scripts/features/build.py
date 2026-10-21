"""The README's six feature cards, 420 x 240 animated SVGs.

    python .github/scripts/features/build.py <data.json> <out-dir>

<data.json> is demo/data.py's output: every chart and number on a card is what
capcom computed on the synthetic logs (spend by project, a spike's window and
cause, the fan-out session's subagents, the checks, `capcom calibrate`). Writes
feature-{adding-up,why,fan-out,checks,trust,private}.svg into <out-dir>.

Each card runs on one loop: the scene fades in, plays, holds its last frame and
fades out. Every element's own style is its last frame, so prefers-reduced-motion
(all animations off) shows the finished picture.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from svg import PANEL, bands, card, keyframes, r, series_colors, usd

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
    pins, last = [], -99.0
    for m in data["spend_project"]["spikes"]["marks"]:
        i = min(range(len(times)), key=lambda k: abs(times[k] - m["t"] / 1000))
        px, py = points[i]
        px = max(px, last + 17)
        last = px
        at = draw[0] + (px - x0) / w * (draw[1] - draw[0])
        k = c.step(
            "transform:scale(0)",
            "transform:scale(1)",
            at,
            0.3,
            ease="cubic-bezier(.3,1.6,.5,1)",
            origin="50% 50%",
        )
        pins.append(
            f'<g class="{k}"><circle cx="{r(px)}" cy="{r(py - 13)}" r="7.5" '
            f'style="fill:var(--fg);stroke:var(--panel);stroke-width:2"/>'
            f'<text x="{r(px)}" y="{r(py - 9.6)}" font-size="9.5" font-weight="700" '
            f'text-anchor="middle" style="fill:var(--panel)">{m["n"]}</text></g>'
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
    agents = sorted(sess["agents"], key=lambda a: a["first"])
    burst = [a for a in agents if spike["start"] - 60_000 <= a["first"] <= spike["end"]]
    t0 = min(a["first"] for a in burst) - 2 * 60_000
    t1 = max(a["last"] for a in burst) + 90_000
    types = list(dict.fromkeys(a["agent_type"] for a in burst))
    color = {t: f"var(--s{i + 1})" for i, t in enumerate(types)}
    x0, y0, w = PX + 52, PY + 34, PW - 68
    X = lambda t: x0 + (min(max(t, t0), t1) - t0) / (t1 - t0) * w  # noqa: E731
    lane = min(6.5, 88 / len(burst))
    out = [text(PX + 16, y0 + 5, "main", "muted", 10.5)]
    # The main thread: a lane that runs on through the burst.
    m0, m1 = X(sess["main"]["first"]), X(sess["main"]["last"])
    grow = c.step(
        "transform:scaleX(0)", "transform:scaleX(1)", 0.3, 4.2, ease="linear", origin="0 0"
    )
    out.append(
        f'<rect class="{grow}" x="{r(m0)}" y="{y0}" width="{r(m1 - m0)}" height="6" '
        f'rx="3" style="fill:var(--fg);fill-opacity:.7"/>'
    )
    span = X(burst[-1]["first"]) - X(burst[0]["first"])
    for i, a in enumerate(burst):
        ax, ay = X(a["first"]), y0 + 12 + i * lane
        aw = max(X(a["last"]) - ax, 4)
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
    del span
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


def checks(data: dict[str, Any]) -> str:
    c = Css()
    total = data["checks"]["total"]
    weekly = round(total["usd_per_week"])
    finding = max(
        (f for f in data["checks"]["findings"] if f["change"] and "\n" in f["change"]),
        key=lambda f: f["saving_usd_per_week"],
    )
    # "up to $NN" with its digits rolling up like an odometer.
    x0, y0 = PX + 18, PY + 30
    out = [text(x0, y0, f"Across {total['findings']} findings", "muted", 11)]
    out.append(text(x0, y0 + 34, "up to", "muted", 13))
    digits = str(weekly)
    size, lh = 34, 40
    dx = x0 + 40
    defs = (
        f'<clipPath id="od"><rect x="{dx - 2}" y="{y0 + 8}" width="120" height="{lh}"/></clipPath>'
    )
    strips = [text(dx, y0 + 38, "$", "fg", size, weight=600)]
    cw = size * 0.58
    for i, d in enumerate(digits):
        seq = list(range(10)) * (len(digits) - i) + list(range(int(d) + 1))
        dist = (len(seq) - 1) * lh
        k = c.step(
            "transform:translateY(0)",
            f"transform:translateY(-{dist}px)",
            0.4,
            2.0 + i * 0.25,
            ease="cubic-bezier(.2,.7,.25,1)",
        )
        tsp = "".join(
            f'<tspan x="{r(dx + cw * (i + 1))}" dy="{lh if j else 0}">{n}</tspan>'
            for j, n in enumerate(seq)
        )
        strips.append(
            f'<g class="{k}"><text class="fg" x="{r(dx + cw * (i + 1))}" '
            f'y="{y0 + 38}" font-size="{size}" font-weight="600">{tsp}</text></g>'
        )
    out.append(f'<g clip-path="url(#od)">{"".join(strips)}</g>')
    wk = c.step("opacity:0", "opacity:1", 2.6, 0.4)
    out.append(
        f'<g class="{wk}">' + text(x0, y0 + 64, "a week, at this pace", "muted", 12) + "</g>"
    )
    # The change to paste, line by line, then "Copied".
    bx, by, bw = PX + 168, PY + 14, PW - 182
    out.append(text(bx, by + 12, finding["title"], "fg", 11, weight=600))
    out.append(
        text(
            bx,
            by + 26,
            f"{finding['scope_label']} · up to {usd(finding['saving_usd_per_week'])}/wk",
            "muted",
            10.5,
        )
    )
    lines = finding["change"].split("\n")
    code_y = by + 36
    bh = 12 + len(lines) * 13
    out.append(
        f'<rect x="{bx}" y="{code_y}" width="{bw}" height="{bh}" rx="6" '
        'style="fill:var(--surface);stroke:var(--border)"/>'
    )
    for i, ln in enumerate(lines):
        k = c.step(
            "opacity:0;transform:translateX(-6px)",
            "opacity:1;transform:translateX(0)",
            3.0 + i * 0.22,
            0.3,
        )
        out.append(
            f'<text class="mono fg {k}" x="{bx + 9}" y="{code_y + 17 + i * 13}" '
            f'font-size="10" xml:space="preserve">{escape(ln)}</text>'
        )
    t_copy = 3.0 + len(lines) * 0.22 + 0.8
    cx, cy = bx + bw - 54, code_y + bh - 24
    a = c.step("opacity:1", "opacity:0", t_copy, 0.15)
    b = c.step("opacity:0", "opacity:1", t_copy, 0.15)
    chip = (
        f'<rect x="{cx}" y="{cy}" width="48" height="18" rx="5" '
        'style="fill:var(--panel);stroke:var(--border)"/>'
    )
    out.append(
        chip
        + f'<g class="{a}">'
        + text(cx + 24, cy + 12.5, "Copy", "fg", 10.5, "middle")
        + "</g>"
        + f'<g class="{b}">'
        + text(cx + 24, cy + 12.5, "Copied", "clay", 10.5, "middle", 600)
        + "</g>"
    )
    body = '<g class="scene">' + "".join(out) + "</g>"
    return card(
        "Checks",
        ["Setup habits ranked by what they cost a week,", "each with the change to paste."],
        f"Checks: up to ${weekly} a week counting up, then the change for "
        f"'{finding['title']}' appearing line by line and copied.",
        c.css(),
        body,
        defs,
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
