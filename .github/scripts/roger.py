"""Roger, capcom's mascot, for the README's pictures: read from the website's own code.

The website draws Roger in capcom/report/static/ask.js (mascot(): s('tag', {...})
calls) and colors and waves him in ask.css (the --mz-* tokens, the mz-wave and
mz-blink keyframes). This module reads both files, so the banner and the social
card show the very Roger the website shows, and a change to him there reaches
the README on the next build. Nothing about him is redrawn here.

    shapes()     the parts of the full Roger, in drawing order: (group, tag, attrs)
    VIEWBOX      his viewBox, (x, y, w, h)
    feet()       the bottom of his body, in viewBox units
    colors()     his tokens for each theme: {"light": {...}, "dark": {...}}
    frames(name) a keyframes rule of ask.css as [(fraction 0..1, transform)]
    ORIGINS      the transform-origin of the arm and the eyes, in viewBox units
    group(...)   Roger as a nested <svg> placed in a picture, classes rg-*
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"
JS = STATIC / "ask.js"
CSS = STATIC / "ask.css"

Attrs = dict[str, str]


class _Parser:
    """Just enough of JavaScript for mascot(): s('tag', {key: value, ...spread}, child...)
    with numbers and single-quoted strings as values."""

    def __init__(self, text: str, consts: dict[str, Attrs]) -> None:
        self.t, self.i, self.consts = text, 0, consts

    def ws(self) -> None:
        while self.i < len(self.t) and self.t[self.i] in " \t\r\n":
            self.i += 1

    def eat(self, s: str) -> None:
        self.ws()
        if not self.t.startswith(s, self.i):
            raise ValueError(f"ask.js: expected {s!r} at {self.t[self.i : self.i + 40]!r}")
        self.i += len(s)

    def peek(self, s: str) -> bool:
        self.ws()
        return self.t.startswith(s, self.i)

    def value(self) -> str:
        self.ws()
        m = re.compile(r"'([^']*)'|(-?[\d.]+)").match(self.t, self.i)
        if not m:
            raise ValueError(f"ask.js: unknown value at {self.t[self.i : self.i + 40]!r}")
        self.i = m.end()
        return m.group(1) if m.group(1) is not None else m.group(2)

    def obj(self) -> Attrs:
        self.eat("{")
        out: Attrs = {}
        while not self.peek("}"):
            if self.peek("..."):
                self.eat("...")
                m = re.compile(r"\w+").match(self.t, self.i)
                assert m
                self.i = m.end()
                out.update(self.consts[m.group()])
            else:
                m = re.compile(r"'([^']+)'|(\w+)").match(self.t, self.i)
                assert m, self.t[self.i : self.i + 40]
                self.i = m.end()
                self.eat(":")
                out[m.group(1) or m.group(2)] = self.value()
            if self.peek(","):
                self.eat(",")
        self.eat("}")
        return out

    def call(self, group: str, out: list[tuple[str, str, Attrs]]) -> None:
        self.eat("s(")
        tag = self.value()
        self.eat(",")
        attrs = self.obj()
        if tag == "g":
            while self.peek(","):
                self.eat(",")
                self.call(attrs["class"], out)
        else:
            out.append((group, tag, attrs))
        self.eat(")")


def _mascot_source() -> str:
    text = JS.read_text()
    m = re.search(r"export function mascot\(.*?\n}\n", text, re.S)
    if not m:
        raise ValueError(f"no mascot() in {JS}")
    return m.group()


def shapes() -> list[tuple[str, str, Attrs]]:
    """The full Roger's parts (mini=false), in the order ask.js draws them: (the
    group they sit in, "" or a class such as mz-arm; the tag; its attributes)."""
    src = _mascot_source()
    consts = {
        name: _Parser(body, {}).obj()
        for name, body in re.findall(r"const (\w+) = (\{[^}]*\});", src)
    }
    out: list[tuple[str, str, Attrs]] = []
    for push in re.finditer(r"parts\.push\(", src):
        p = _Parser(src, consts)
        p.i = push.end()
        while True:
            p.call("", out)
            if not p.peek(","):
                break
            p.eat(",")
    return out


def _viewbox() -> tuple[float, float, float, float]:
    m = re.search(r"viewBox: mini \? '[^']*' : '([^']*)'", _mascot_source())
    if not m:
        raise ValueError("no viewBox in mascot()")
    x, y, w, h = (float(v) for v in m.group(1).split())
    return x, y, w, h


VIEWBOX = _viewbox()


def feet() -> float:
    """Where Roger stands: the bottom of his body (the mz-body rect), in viewBox units."""
    for _, tag, a in shapes():
        if tag == "rect" and a.get("class") == "mz-body":
            return float(a["y"]) + float(a["height"])
    raise ValueError("no body in mascot()")


def colors() -> dict[str, dict[str, str]]:
    """Roger's --mz-* tokens per theme, from ask.css's light and dark blocks."""
    css = CSS.read_text()
    out = {}
    for theme, sel in (
        ("light", r':root, \[data-theme="light"\]'),
        ("dark", r'\[data-theme="dark"\]'),
    ):
        m = re.search(sel + r"\s*\{([^}]*)\}", css)
        if not m:
            raise ValueError(f"no {theme} tokens in {CSS}")
        out[theme] = dict(re.findall(r"--(mz-[\w-]+):\s*([^;]+);", m.group(1)))
    return out


def rule(selector: str) -> str:
    """The declarations of one rule of ask.css, such as '.mascot .mz-cheek'."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS.read_text())
    if not m:
        raise ValueError(f"no {selector} in {CSS}")
    return m.group(1).strip()


def frames(name: str) -> list[tuple[float, str]]:
    """A keyframes rule of ask.css as [(fraction, transform)], sorted, a stop listed
    with several percentages repeated for each."""
    css = CSS.read_text()
    m = re.search(r"@keyframes " + re.escape(name) + r"\s*\{", css)
    if not m:
        raise ValueError(f"no @keyframes {name} in {CSS}")
    depth, end = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(css[end], 0)
        end += 1
    out = []
    for stops, decl in re.findall(
        r"([\d%,\s]+)\{\s*transform:\s*([^;}]+);?\s*\}", css[m.end() : end - 1]
    ):
        for s in stops.split(","):
            out.append((float(s.strip().rstrip("%")) / 100, decl.strip()))
    return sorted(out)


def _origin(cls: str) -> tuple[float, float]:
    m = re.search(r"transform-origin:\s*([\d.]+)px ([\d.]+)px", rule(f".mascot .{cls}"))
    if not m:
        raise ValueError(f"no transform-origin for .{cls}")
    return float(m.group(1)), float(m.group(2))


ORIGINS = {"arm": _origin("mz-arm"), "eyes": _origin("mz-eyes")}


def theme_css(prefix: str = "svg") -> str:
    """Roger's colors as custom properties on `prefix` (light, then dark under
    prefers-color-scheme), and the rules that paint his classes with them."""

    def vars_(theme: str) -> str:
        return prefix + "{" + ";".join(f"--{k}:{v}" for k, v in colors()[theme].items()) + "}"

    cheek = re.search(r"opacity:\s*([\d.]+)", rule(".mascot .mz-cheek"))
    glint = re.search(r"fill:\s*(#[0-9A-Fa-f]+)", rule(".mascot .mz-glint"))
    return (
        vars_("light")
        + "@media (prefers-color-scheme:dark){"
        + vars_("dark")
        + "}"
        + ".mz-body{fill:var(--mz-body)}.mz-body-stroke{stroke:var(--mz-body)}"
        + ".mz-face{fill:var(--mz-face)}.mz-ink{fill:var(--mz-ink)}"
        + f".mz-glint{{fill:{glint.group(1) if glint else '#fff'}}}"
        + f".mz-cheek{{fill:var(--mz-cheek);opacity:{cheek.group(1) if cheek else '.75'}}}"
        + ".mz-ink-stroke{stroke:var(--mz-ink)}"
        + ".mz-band{fill:var(--mz-band)}.mz-band-stroke{stroke:var(--mz-band)}"
        + ".rg-arm,.rg-eyes{transform-box:view-box}"
        + f".rg-arm{{transform-origin:{ORIGINS['arm'][0]:g}px {ORIGINS['arm'][1]:g}px}}"
        + f".rg-eyes{{transform-origin:{ORIGINS['eyes'][0]:g}px {ORIGINS['eyes'][1]:g}px}}"
    )


def group(x: float, y: float, width: float, cls: str = "roger") -> str:
    """Roger as a nested <svg> of `width` with its top-left corner at (x, y), his own
    viewBox inside it, so the arm and the eyes turn about the same points as on the
    website. The arm's group has class rg-arm, the eyes' rg-eyes."""
    vx, vy, vw, vh = VIEWBOX
    height = width * vh / vw
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for g, tag, attrs in shapes():
        a = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        key = g or f"_{len(order)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f"<{tag} {a}/>")
    parts = []
    for key in order:
        inner = "".join(groups[key])
        if key.startswith("_"):
            parts.append(inner)
        else:
            parts.append(f'<g class="{key.replace("mz-", "rg-")}">{inner}</g>')
    return (
        f'<svg class="{cls}" x="{round(x, 2):g}" y="{round(y, 2):g}" width="{width:g}" '
        f'height="{round(height, 2):g}" '
        f'viewBox="{vx:g} {vy:g} {vw:g} {vh:g}" overflow="visible">{"".join(parts)}</svg>'
    )


def motion(part: str) -> tuple[str, float, str]:
    """How the website animates a part ("arm" or "eyes") when Roger waves: its
    keyframes' name, their length in seconds and their timing function."""
    m = re.search(
        r"animation:\s*([\w-]+)\s+([\d.]+)s\s+([\w-]+)", rule(f".mascot.waving .mz-{part}")
    )
    if not m:
        raise ValueError(f"no wave animation for .mz-{part}")
    return m.group(1), float(m.group(2)), m.group(3)


def on_loop(part: str, starts: list[float]) -> list[tuple[float, str]]:
    """The website's animation of a part played at each of `starts` (seconds into a
    loop), as (seconds, declarations) frames for svg.keyframes(); at rest between."""
    name, seconds, _ = motion(part)
    fs = frames(name)
    rest = f"transform:{fs[0][1]}"
    out = [(0.0, rest)]
    for s in starts:
        out += [(s + f * seconds, f"transform:{tr}") for f, tr in fs]
    return out
