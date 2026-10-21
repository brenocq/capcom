"""The README demo: the real `capcom serve`, on the synthetic logs, driven in headless Chrome.

    python .github/scripts/demo/record.py <work-dir> <out-dir> [--stills <dir>] [--theme light|dark]

<work-dir> is demo/data.py's (it is built there first when missing). One tab
shows capcom's website, clicked with real mouse events, telling one story in six
steps: the spend jumped (Spend over 14 days by project, the cursor across the
steep rise), who spent it (split by agent type: the coder subagents), why (the
rise's spike marker opens the Spikes page on its cause), where (its session's
page: the context, the cost of each request, the subagent lanes), is it a habit
(Checks: the same cause on top), fix it (a change copied). A second tab draws
each moment in a macOS-style window (frame.html) with a cursor and a caption
strip naming the steps, on a transparent page. Everything shown is capcom's own
output on the synthetic data; nothing on the page is staged.

It records twice, once per GitHub theme (prefers-color-scheme emulated in both
tabs; the site follows it, as nothing is saved in its fresh profile's
localStorage): ffmpeg joins each run's frames into <out-dir>/demo-<theme>.webp
(animated, looping, with a cross-fade back to the start) and
<out-dir>/demo-<theme>.png (a still of the "why" step). With --stills, a PNG of
every step's last frame goes there too, for checking by eye. Pages render at 0.8
device pixels per CSS pixel, so the 1,240-pixel-wide frame comes out 992 pixels
wide, sharp at that size.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import data  # noqa: E402
from chrome import Chrome, Tab  # noqa: E402

APP_W, APP_H = 1160, 720
FRAME_W, FRAME_H = 1240, 845
SCALE = 0.8
FPS = 25  # ffmpeg's concat demuxer times frames in 1/25 s
STEP = 1 / FPS
STEPS = ["Spend jumped", "Who spent it?", "Why?", "Where?", "Is it a habit?", "Fix it"]
THEMES = ("light", "dark")
# Lossy WebP at this quality (with lossless alpha) when the lossless file would be
# over the size budget.
MAX_BYTES = 4 * 1024 * 1024
QUALITY = 90

# Finds an element by selector and text (or attribute) and returns its box; the
# cursor aims at its center, or `dx` pixels from its left edge.
FIND = """(() => {{
  const el = [...document.querySelectorAll({sel!r})]
    .find((e) => (e.getAttribute({attr!r}) || e.textContent || '').includes({text!r}));
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [r.x, r.y, r.width, r.height];
}})()"""


class Recorder:
    def __init__(self, chrome: Chrome, base: str, work: Path, theme: str) -> None:
        dark = theme == "dark"
        self.app = Tab(chrome, chrome.session)
        self.app.viewport(APP_W, APP_H, SCALE)
        self.app.media(dark=dark)
        self.frame = chrome.tab(FRAME_W, FRAME_H, SCALE)
        self.frame.media(dark=dark)
        self.frame.transparent()
        self.frame.goto((HERE / "frame.html").as_uri(), settle=0.1)
        self.frame.eval(
            f"document.body.classList.toggle('dark', {str(dark).lower()}); "
            f"steps({json.dumps(STEPS)})"
        )
        self.base = base
        self.dir = work
        self.shots = 0
        self.frames: list[tuple[Path, float]] = []
        self.x, self.y = APP_W * 0.62, APP_H * 0.55
        self.press = False
        self.step = 0
        self.page: Path | None = None
        self.stills: list[tuple[str, Path]] = []

    # -- capture

    def snap(self) -> None:
        """A new screenshot of the app, the background of the frames after it."""
        self.shots += 1
        self.page = self.dir / f"app-{self.shots:04d}.png"
        self.app.screenshot(self.page)

    def emit(self, dur: float, next_page: Path | None = None, mix: float = 0) -> None:
        assert self.page is not None
        src = self.page.as_uri()
        nxt = f', next: "{next_page.as_uri()}", mix: {mix}' if next_page else ""
        self.frame.eval(
            f'show({{src: "{src}", x: {self.x:.1f}, y: {self.y:.1f}, step: {self.step}, '
            f"press: {str(self.press).lower()}{nxt}}})"
        )
        png = self.dir / f"frame-{len(self.frames):05d}.png"
        self.frame.screenshot(png, clip={"x": 0, "y": 0, "width": FRAME_W, "height": FRAME_H})
        self.frames.append((png, dur))
        if len(self.frames) % 100 == 0:
            print(f"frame {len(self.frames)}", file=sys.stderr, flush=True)

    # -- actions

    def open(self, path: str, settle: float = 1.2) -> None:
        self.app.goto(self.base + path, settle=settle)
        self.snap()

    def hold(self, seconds: float) -> None:
        self.emit(seconds)

    def box(self, sel: str, text: str = "", attr: str = "") -> tuple[float, float, float, float]:
        at = self.app.eval(FIND.format(sel=sel, text=text, attr=attr or "data-none"))
        if not at:
            raise RuntimeError(f"no {sel} with {text!r}")
        x, y, w, h = (float(v) for v in at)
        return x, y, w, h

    def where(self, sel: str, text: str = "", attr: str = "") -> tuple[float, float]:
        x, y, w, h = self.box(sel, text, attr)
        return x + min(w / 2, 60), y + h / 2

    def move(self, to: tuple[float, float], seconds: float = 0.6, hover: bool = False) -> None:
        """The cursor glides to `to` (ease in-out); with `hover`, the page sees it move."""
        x0, y0 = self.x, self.y
        n = max(2, round(seconds / STEP))
        for i in range(1, n + 1):
            k = i / n
            e = 0.5 - 0.5 * math.cos(math.pi * k)
            self.x = x0 + (to[0] - x0) * e
            self.y = y0 + (to[1] - y0) * e
            if hover:
                self.app.mouse("mouseMoved", self.x, self.y)
                time.sleep(0.03)
                self.snap()
            self.emit(STEP)

    def click(self, settle: float = 1.2, step: int | None = None, ready: str = "") -> None:
        """A click where the cursor is; `step` moves the caption on as it lands, and
        the next screenshot waits for an element matching `ready` (a page loading)."""
        self.press = True
        self.emit(0.12)
        self.app.click(self.x, self.y)
        self.press = False
        time.sleep(settle)
        for _ in range(100):
            if not ready or self.app.eval(f"!!document.querySelector({ready!r})"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"no {ready} after the click")
        time.sleep(0.3 if ready else 0)
        if step is not None:
            self.step = step
        self.snap()
        self.emit(STEP)

    def go(self, sel: str, text: str = "", attr: str = "", seconds: float = 0.6, **kw: Any) -> None:
        self.move(self.where(sel, text, attr), seconds)
        self.click(**kw)

    def scroll(self, dy: float, seconds: float = 0.7) -> None:
        """Scrolls the page by `dy` (eased), the cursor riding along."""
        n = max(2, round(seconds / (2 * STEP)))  # 12.5 frames a second: a smaller file
        done = 0.0
        for i in range(1, n + 1):
            k = i / n
            target = dy * (0.5 - 0.5 * math.cos(math.pi * k))
            self.app.eval(f"window.scrollBy(0, {target - done:.1f})")
            done = target
            time.sleep(0.02)
            self.snap()
            self.emit(2 * STEP)

    def keep_hover(self, on: bool) -> None:
        """Each screenshot of the app tab sends the chart a mouseleave, which hides its
        guide and tooltip; block it while the cursor rests on the chart."""
        if on:
            self.app.eval(
                "window.__keep = (e) => e.stopImmediatePropagation();"
                " document.addEventListener('mouseleave', window.__keep, true)"
            )
        else:
            # Then the leave the page missed, to everything under the cursor.
            self.app.eval(
                "document.removeEventListener('mouseleave', window.__keep, true);"
                f" for (let e = document.elementFromPoint({self.x:.1f}, {self.y:.1f}); e;"
                " e = e.parentElement) e.dispatchEvent(new Event('mouseleave'))"
            )

    def still(self, name: str) -> None:
        self.stills.append((name, self.frames[-1][0]))

    def loop_back(self, first: Path, start: tuple[float, float], seconds: float = 0.6) -> None:
        """Cross-fade to the first frame's page, the cursor gliding back to its start."""
        n = round(seconds / STEP)
        x0, y0 = self.x, self.y
        for i in range(1, n + 1):
            e = 0.5 - 0.5 * math.cos(math.pi * i / n)
            self.x, self.y = x0 + (start[0] - x0) * e, y0 + (start[1] - y0) * e
            if i == n:
                self.step = 0
            self.emit(STEP, next_page=first, mix=i / n)


def across_rise(rec: Recorder, spike: dict[str, Any], window: dict[str, Any]) -> None:
    """Hovers the chart from a few hours before the spike's rise to just after it, so
    the tooltip's numbers climb, and leaves the cursor at the top of the rise."""
    x, _, w, _ = rec.box(".chart")
    since, until = (
        datetime.fromisoformat(window[k]).timestamp() * 1000 for k in ("since", "until")
    )

    def at(t: float) -> float:
        return x + (t - since) / (until - since) * w

    _, my, _, mh = rec.box(".spikemark", "Cache expired", "aria-label")
    before, after = at(spike["start"] - 4 * 3600e3), at(spike["end"] + 1800e3)
    rec.move((before, my + mh + 70), 0.5)
    rec.keep_hover(True)
    rec.move((after, my + mh + 40), 1.0, hover=True)


def script(rec: Recorder, numbers: dict[str, Any]) -> None:
    """The spend jumped -> who -> why -> where -> is it a habit -> fix it."""
    spike = max(numbers["spikes"]["spikes"], key=lambda s: s["excess_usd"])
    window = numbers["spend_project"]["window"]

    # 1. Spend jumped: 14 days by project, the cursor across the steepest rise.
    rec.open("/spend?days=14&group=project")
    first = rec.page
    assert first is not None
    start = (rec.x, rec.y)
    rec.hold(0.6)
    across_rise(rec, spike, window)
    rec.hold(2.2)
    rec.still("1-spend")

    # 2. Who spent it: the same chart by agent type, the coder subagents climbing.
    rec.keep_hover(False)
    rec.snap()
    rec.go(".seg button", "Agent type", seconds=0.5, step=1)
    rec.hold(0.3)
    across_rise(rec, spike, window)
    rec.hold(2.0)
    rec.still("2-agent")

    # 3. Why: the rise's marker opens the Spikes page on that spike.
    rec.keep_hover(False)
    rec.snap()
    rec.go(".spikemark", "Cache expired", "aria-label", seconds=0.5, step=2, ready=".detail")
    rec.hold(0.3)
    _, top, _, _ = rec.box(".detail")
    rec.move((rec.x + 260, rec.y + 120), 0.3)
    rec.scroll(top - 12, seconds=0.6)
    rec.hold(2.4)
    rec.still("3-spike")

    # 4. Where: down the evidence to "Open the session", then the session's page,
    # a pause past the cache lifetime and what the request after it wrote.
    _, link, _, lh = rec.box(".detail a.link", "Open the session")
    rec.step = 3
    rec.scroll(link + lh + 60 - APP_H, seconds=0.6)
    rec.go(".detail a.link", "Open the session", seconds=0.5, ready="div.lane")
    rec.hold(0.3)
    _, card, _, _ = rec.box("section.card", "How the session unfolded")
    rec.scroll(card - 14, seconds=0.6)
    tick = rec.app.eval(
        "(() => { const t = document.querySelectorAll('.pausetick, .pausechip');"
        " const r = t[Math.min(1, t.length - 1)].getBoundingClientRect();"
        " return [r.x + r.width / 2, r.y + r.height / 2]; })()"
    )
    rec.move((tick[0] - 40, tick[1] + 40), 0.5)
    rec.keep_hover(True)
    rec.move((tick[0], tick[1]), 0.4, hover=True)
    rec.hold(2.3)
    rec.still("4-session")
    rec.keep_hover(False)

    # 5. Is it a habit: Checks, the same cause on top across the project.
    rec.go(".nav a", "Checks", seconds=0.6, step=4, ready="section.card.check")
    rec.hold(0.3)
    _, card, _, _ = rec.box("section.card.check", "Subagent caches expired")
    rec.move((rec.x + 420, rec.y + 160), 0.3)
    rec.scroll(card - 14, seconds=0.6)
    rec.hold(2.3)
    rec.still("5-checks")

    # 6. Fix it: down to the first change to paste, and copy it.
    _, copy, _, ch = rec.box("button.copy", "Copy")
    rec.step = 5
    rec.scroll(copy + ch + 70 - APP_H, seconds=0.9)
    rec.go("button.copy", "Copy", seconds=0.5, settle=0.3)
    rec.move((rec.x + 60, rec.y + 34), 0.3)
    rec.hold(2.8)
    rec.still("6-copy")
    rec.loop_back(first, start)


def encode(frames: list[tuple[Path, float]], out: Path, tmp: Path) -> str:
    """Joins the frames into an animated WebP at `out`: lossless when that fits the
    size budget, else lossy at QUALITY (alpha kept lossless). Returns which."""
    # Merge runs of identical frames into one longer frame.
    merged: list[list[object]] = []
    for png, dur in frames:
        data_ = png.read_bytes()
        if merged and merged[-1][2] == data_:
            merged[-1][1] = float(merged[-1][1]) + dur  # type: ignore[arg-type]
        else:
            merged.append([png, dur, data_])
    lines = []
    for png, dur, _ in merged:
        lines += [f"file '{png}'", f"duration {max(round(float(dur) * FPS), 1) / FPS:.2f}"]  # type: ignore[arg-type]
    lines.append(f"file '{merged[-1][0]}'")
    (tmp / "frames.txt").write_text("\n".join(lines) + "\n")
    total = sum(float(d) for _, d, _ in merged)  # type: ignore[arg-type]
    print(f"{len(frames)} frames, {len(merged)} distinct, {total:.1f} s")
    for mode, args in (
        ("lossless", ["-lossless", "1", "-quality", "100"]),
        (f"lossy q{QUALITY}", ["-lossless", "0", "-quality", str(QUALITY)]),
    ):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(tmp / "frames.txt"),
                "-fps_mode",
                "passthrough",
                "-c:v",
                "libwebp_anim",
                *args,
                "-compression_level",
                "6",
                "-pix_fmt",
                "bgra" if mode == "lossless" else "yuva420p",
                "-loop",
                "0",
                str(out),
            ],
            check=True,
        )
        if out.stat().st_size <= MAX_BYTES:
            return mode
    return mode


def record(base: str, work: Path, out: Path, theme: str, stills: Path | None) -> None:
    numbers = json.loads((work / "data.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix=f"capcom-demo-{theme}-"))
    try:
        with Chrome(APP_W, APP_H) as chrome:
            rec = Recorder(chrome, base, tmp, theme)
            try:
                script(rec, numbers)
            except Exception:
                if stills and rec.page:  # what the page showed when the script failed
                    stills.mkdir(parents=True, exist_ok=True)
                    rec.snap()
                    shutil.copy(rec.page, stills / f"demo-{theme}-failed.png")
                raise
            webp = out / f"demo-{theme}.webp"
            mode = encode(rec.frames, webp, tmp)
            shutil.copy(dict(rec.stills)["3-spike"], out / f"demo-{theme}.png")
            if stills:
                stills.mkdir(parents=True, exist_ok=True)
                for name, png in rec.stills:
                    shutil.copy(png, stills / f"demo-{theme}-{name}.png")
        print(f"{webp}  {webp.stat().st_size / 1024:.0f} KiB, {mode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--stills", type=Path)
    ap.add_argument("--theme", choices=THEMES, action="append", help="default: both")
    a = ap.parse_args()
    work = a.work.resolve()
    if not (work / "data.json").exists():
        data.build(work)
    a.out.mkdir(parents=True, exist_ok=True)
    with data.serve(work) as base:
        for theme in a.theme or THEMES:
            record(base, work, a.out, theme, a.stills)


if __name__ == "__main__":
    main()
