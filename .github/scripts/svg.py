"""What the README's SVGs share: the colors, the card frame, keyframes, a few shapes.

Each SVG works on both GitHub themes: its colors are custom properties, GitHub's
Primer neutrals plus the website's chart colors (capcom/report/static/style.css:
the SERIES palette, --gray, the terracotta accent), light by default and dark
under prefers-color-scheme. Under prefers-reduced-motion every animation is off
and what shows is the final frame: an element's own style is its last state,
and the keyframes only lead up to it. Text is in GitHub's system font stack;
only the banner's wordmark is outlines.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape
from typing import Any

PRIMER = {
    "light": {
        "fg": "#1f2328",
        "muted": "#59636e",
        "border": "#d1d9e0",
        "surface": "#f6f8fa",
        "panel": "#ffffff",
    },
    "dark": {
        "fg": "#f0f6fc",
        "muted": "#9198a1",
        "border": "#3d444d",
        "surface": "#151b23",
        "panel": "#0d1117",
    },
}
# The website's tokens (style.css): chart series, the gray of "usual pace", the
# accent and the clay of "+$" amounts.
SITE = {
    "light": {
        "s1": "#2a78d6",
        "s2": "#eb6834",
        "s3": "#1baf7a",
        "s4": "#eda100",
        "s5": "#e87ba4",
        "s6": "#008300",
        "gray": "#D1CFC5",
        "accent": "#D97757",
        "clay": "#A84E2D",
        "ok": "#558A42",
    },
    "dark": {
        "s1": "#3987e5",
        "s2": "#d95926",
        "s3": "#199e70",
        "s4": "#c98500",
        "s5": "#d55181",
        "s6": "#008300",
        "gray": "#5E5D59",
        "accent": "#D97757",
        "clay": "#F0A07E",
        "ok": "#7FB069",
    },
}
ACCENT = "#D97757"
FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'

W, H = 420, 240
PANEL = (12, 12, 396, 150)  # x, y, w, h of a card's picture


def r(n: float, digits: int = 1) -> str:
    """A short number for SVG markup: no trailing zeros, no leading zero."""
    if digits == 0:
        return str(round(n))
    s = f"{round(n, digits):.{digits}f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s.replace("0.", ".", 1) if s.startswith("0.") else s.replace("-0.", "-.", 1)


def _vars(theme: str) -> str:
    return (
        "svg{" + ";".join(f"--{k}:{v}" for k, v in {**PRIMER[theme], **SITE[theme]}.items()) + "}"
    )


def theme_css() -> str:
    return (
        _vars("light")
        + "@media (prefers-color-scheme:dark){"
        + _vars("dark")
        + "}"
        + f"text{{font-family:{FONT}}}.mono{{font-family:{MONO}}}"
        + ".fg{fill:var(--fg)}.muted{fill:var(--muted)}.clay{fill:var(--clay)}"
        + ".surface{fill:var(--surface);stroke:var(--border)}"
        + ".panel{fill:var(--panel);stroke:var(--border)}"
        + ".grid{stroke:var(--border);stroke-dasharray:2 3}"
        + ".title{fill:var(--fg);font-size:16px;font-weight:600}"
        + ".desc{fill:var(--muted);font-size:13px}"
    )


REDUCED = "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"


def keyframes(name: str, loop: float, frames: Sequence[tuple[float, str]]) -> str:
    """Keyframes on a loop of `loop` seconds: frames as (seconds, declarations)."""
    body = "".join(f"{r(t / loop * 100, 2)}%{{{decl}}}" for t, decl in frames)
    return f"@keyframes {name}{{{body}}}"


def anim(cls: str, name: str, loop: float, extra: str = "", timing: str = "linear") -> str:
    """The rule running keyframes `name` on `.cls`, forever, on the shared loop."""
    return (
        f".{cls}{{animation:{name} {r(loop, 2)}s {timing} infinite{';' + extra if extra else ''}}}"
    )


def svg(width: int, height: int, label: str, css: str, body: str, defs: str = "") -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">',
            f"<title>{escape(label)}</title>",
            f"<style>{theme_css()}{css}{REDUCED}</style>",
            f"<defs>{defs}</defs>" if defs else "",
            body,
            "</svg>",
            "",
        ]
    )


def card(title: str, lines: Iterable[str], label: str, css: str, body: str, defs: str = "") -> str:
    """A feature card: its surface, the picture panel, a title and up to two lines."""
    x, y, w, h = PANEL
    desc = "".join(
        f'<tspan x="{x + 4}" y="{H - 34 + i * 18}">{escape(t)}</tspan>' for i, t in enumerate(lines)
    )
    frame = (
        f'<rect class="surface" x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="12"/>'
        f'<rect class="panel" x="{x + 0.5}" y="{y + 0.5}" width="{w - 1}" height="{h - 1}" rx="8"/>'
    )
    text = (
        f'<text class="title" x="{x + 4}" y="{H - 55}">{escape(title)}</text>'
        f'<text class="desc">{desc}</text>'
    )
    return svg(W, H, label, css, frame + body + text, defs)


def fade(id_: str, color: str, top: float, bottom: float) -> str:
    """A vertical gradient of one color, as the website's bands use."""
    return (
        f'<linearGradient id="{id_}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" style="stop-color:{color};stop-opacity:{top}"/>'
        f'<stop offset="1" style="stop-color:{color};stop-opacity:{bottom}"/></linearGradient>'
    )


def bands(
    times: Sequence[float],
    members: Sequence[Sequence[float]],
    total: Sequence[float],
    colors: Sequence[str],
    box: tuple[float, float, float, float],
    ymax: float,
    prefix: str,
) -> tuple[str, str, list[tuple[float, float]]]:
    """The website's adding-up chart (charts.js bandChart) in a box: each member's
    cumulative line from zero, largest first, with a band down to the next lower
    line in its own color and a vertical fade; the total on top in the text color,
    its band a neutral gray down to the largest member. Returns (defs, markup, the
    total line's points)."""
    x0, y0, w, h = box
    t0, t1 = times[0], times[-1]
    n = len(times)

    def X(i: int) -> float:
        return x0 + (times[i] - t0) / (t1 - t0) * w

    def Y(v: float) -> float:
        return y0 + h - v / ymax * h

    def line(vals: Sequence[float]) -> str:
        return "".join(f"{'L' if i else 'M'}{r(X(i))} {r(Y(vals[i]))}" for i in range(n))

    def band(up: Sequence[float], lo: Sequence[float] | None) -> str:
        d = line(up)
        for i in reversed(range(n)):
            d += f"L{r(X(i))} {r(Y(lo[i] if lo else 0))}"
        return d + "Z"

    defs = fade(f"{prefix}t", "var(--fg)", 0.18, 0.02) + "".join(
        fade(f"{prefix}{i}", c, 0.32, 0.04) for i, c in enumerate(colors)
    )
    out = [f'<path d="{band(total, members[0])}" fill="url(#{prefix}t)"/>']
    for i, m in enumerate(members):
        lo = members[i + 1] if i + 1 < len(members) else None
        out.append(f'<path d="{band(m, lo)}" fill="url(#{prefix}{i})"/>')
    for i in reversed(range(len(members))):
        out.append(
            f'<path d="{line(members[i])}" fill="none" stroke-width="1.6" '
            f'stroke-linejoin="round" style="stroke:{colors[i]}"/>'
        )
    out.append(
        f'<path d="{line(total)}" fill="none" stroke-width="2.2" stroke-linejoin="round" '
        f'style="stroke:var(--fg)"/>'
    )
    return defs, "".join(out), [(X(i), Y(total[i])) for i in range(n)]


def series_colors(labels: Sequence[str]) -> list[str]:
    """The website's colors for chart members: SERIES in order, gray for (other)."""
    return ["var(--gray)" if lb == "(other)" else f"var(--s{i + 1})" for i, lb in enumerate(labels)]


def mark(cx: float, cy: float, size: float, cls: str = "") -> str:
    """capcom's radio-signal mark (app.js mark(), a 24-unit icon) centered on (cx, cy):
    a dot with two pairs of arcs, in the accent. Arcs carry classes a1 (inner) and a2
    (outer) for animation."""
    s = size / 24
    arcs = [
        ("a1", "M8 8a5.7 5.7 0 0 0 0 8"),
        ("a1", "M16 8a5.7 5.7 0 0 1 0 8"),
        ("a2", "M5 5a9.9 9.9 0 0 0 0 14"),
        ("a2", "M19 5a9.9 9.9 0 0 1 0 14"),
    ]
    paths = "".join(f'<path class="{c}" d="{d}"/>' for c, d in arcs)
    return (
        f'<g class="{cls}" transform="translate({r(cx - 12 * s, 2)} {r(cy - 12 * s, 2)}) '
        f'scale({r(s, 4)})" fill="none" stroke="{ACCENT}" stroke-width="2" '
        f'stroke-linecap="round"><circle class="dot" cx="12" cy="12" r="2.4" fill="{ACCENT}" '
        f'stroke="none"/>{paths}</g>'
    )


def usd(v: float, cents: bool = True) -> str:
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


def spike_pins(
    marks: Sequence[dict[str, Any]],
    times: Sequence[float],
    total: Sequence[float],
    points: Sequence[tuple[float, float]],
    count: int,
    gap: float,
) -> list[tuple[int, float, float]]:
    """Where the numbered spike markers go on a drawn total line: the `count` largest
    spikes by excess, at least `gap` pixels apart, renumbered in time order, each at
    the middle of the steepest step of the total inside its span (from the first
    sample at or after its start to the first at or after its end): the point at the
    top of that step, where a marker sits on the rise it explains, leaving the rise
    itself in view. `times` in seconds, `points` from bands()."""
    n = len(times)

    def after(t: float) -> int:
        return next((i for i in range(n) if times[i] >= t), n - 1)

    pins: list[tuple[float, float, float]] = []
    for m in sorted(marks, key=lambda m: -m["excess_usd"]):
        lo = max(after(m["t"] / 1000), 1)
        hi = max(after(m.get("end", m["t"]) / 1000), lo)
        i = max(range(lo, hi + 1), key=lambda k: total[k] - total[k - 1])
        x, y = points[i]
        if all(abs(x - p[1]) >= gap for p in pins):
            pins.append((m["t"], x, y))
        if len(pins) == count:
            break
    pins.sort()
    return [(k + 1, x, y) for k, (_, x, y) in enumerate(pins)]


def eased_at(
    fraction: float, ease: tuple[float, float, float, float] = (0.45, 0.05, 0.4, 1)
) -> float:
    """The share of a draw-in's duration after which a cubic-bezier `ease` has
    revealed `fraction` of the width: when the line reaches a marker there."""

    def bez(s: float, a: float, b: float) -> float:
        return 3 * (1 - s) ** 2 * s * a + 3 * (1 - s) * s**2 * b + s**3

    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if bez(mid, ease[1], ease[3]) < fraction:
            lo = mid
        else:
            hi = mid
    return bez(lo, ease[0], ease[2])
