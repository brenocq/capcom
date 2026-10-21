"""The README demo: the real `capcom serve`, on the synthetic logs, driven in headless Chrome.

    python .github/scripts/demo/record.py <work-dir> <out-dir> [--stills <dir>]

<work-dir> is demo/data.py's (it is built there first when missing). One tab
shows capcom's website, clicked with real mouse events: Spend over 14 days by
project, hovering a point of the chart, then switching to Model, then Spikes,
opening one with a clear cause, then Checks, copying a change. Overview is left
out (its limit cards are empty until milestone 5). A second tab draws each moment in
a macOS-style window (frame.html) with a cursor, on a transparent page; ffmpeg
joins the frames into <out-dir>/demo.webp (animated, lossless, looping, with a
cross-fade back to the start) and <out-dir>/demo.png (a still of the Spikes
page). With --stills, a PNG of every scene's last frame goes there too, for
checking by eye. Pages render at 0.8 device pixels per CSS pixel, so the
1,240-pixel-wide window comes out 992 pixels wide, sharp at that size.
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import data  # noqa: E402
from chrome import Chrome, Tab  # noqa: E402

APP_W, APP_H = 1160, 720
SCALE = 0.8
FPS = 25  # ffmpeg's concat demuxer times frames in 1/25 s
STEP = 1 / FPS


class Recorder:
    def __init__(self, chrome: Chrome, base: str, work: Path) -> None:
        self.app = Tab(chrome, chrome.session)
        self.app.viewport(APP_W, APP_H, SCALE)
        self.frame = chrome.tab(1240, 834, SCALE)
        self.frame.transparent()
        self.frame.goto((HERE / "frame.html").as_uri(), settle=0.1)
        self.base = base
        self.dir = work
        self.shots = 0
        self.frames: list[tuple[Path, float]] = []
        self.x, self.y = APP_W * 0.62, APP_H * 0.55
        self.press = False
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
            f'show({{src: "{src}", x: {self.x:.1f}, y: {self.y:.1f}, '
            f"press: {str(self.press).lower()}{nxt}}})"
        )
        png = self.dir / f"frame-{len(self.frames):05d}.png"
        self.frame.screenshot(png, clip={"x": 0, "y": 0, "width": 1240, "height": 834})
        self.frames.append((png, dur))
        if len(self.frames) % 50 == 0:
            print(f"frame {len(self.frames)}", file=sys.stderr, flush=True)

    # -- actions

    def open(self, path: str, settle: float = 1.2) -> None:
        self.app.goto(self.base + path, settle=settle)
        self.snap()

    def hold(self, seconds: float) -> None:
        self.emit(seconds)

    def where(self, selector: str, text: str = "") -> tuple[float, float]:
        js = (
            f"(() => {{ const el = [...document.querySelectorAll({selector!r})]"
            f".find((e) => e.textContent.includes({text!r}));"
            " if (!el) return null; const r = el.getBoundingClientRect();"
            " return [r.x + Math.min(r.width / 2, 60), r.y + r.height / 2]; })()"
        )
        at = self.app.eval(js)
        if not at:
            raise RuntimeError(f"no {selector} with {text!r}")
        return float(at[0]), float(at[1])

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

    def click(self, settle: float = 1.2) -> None:
        self.press = True
        self.emit(0.12)
        self.app.click(self.x, self.y)
        self.press = False
        time.sleep(settle)
        self.app.mouse("mouseMoved", self.x, self.y)
        self.snap()
        self.emit(STEP)

    def go(self, selector: str, text: str, seconds: float = 0.6, settle: float = 1.2) -> None:
        self.move(self.where(selector, text), seconds)
        self.click(settle)

    def scroll(self, dy: float, seconds: float = 0.7) -> None:
        n = max(2, round(seconds / STEP))
        done = 0.0
        for i in range(1, n + 1):
            k = i / n
            target = dy * (0.5 - 0.5 * math.cos(math.pi * k))
            self.app.eval(f"window.scrollBy(0, {target - done:.1f})")
            done = target
            time.sleep(0.02)
            self.snap()
            self.emit(STEP)

    def still(self, name: str) -> None:
        self.stills.append((name, self.frames[-1][0]))

    def loop_back(self, first: Path, seconds: float = 0.5) -> None:
        """Cross-fade to the first frame's page, the cursor fading back to its start."""
        n = round(seconds / STEP)
        for i in range(1, n + 1):
            self.emit(STEP, next_page=first, mix=i / n)


def script(rec: Recorder) -> None:
    """Spend (14 days by project, a hovered point) -> Model -> Spikes (a clear cause) -> Checks."""
    rec.open("/spend?days=14&group=project")
    first = rec.page
    assert first is not None
    rec.hold(0.8)
    x, y = rec.where(".chart")
    rec.move((x + 250, y + 40), 0.6)
    # Each screenshot of the app tab sends the chart a mouseleave, which hides its
    # guide and tooltip; block it while the cursor rests on the chart.
    rec.app.eval(
        "window.__keep = (e) => e.stopImmediatePropagation();"
        " document.addEventListener('mouseleave', window.__keep, true)"
    )
    rec.move((x + 470, y + 10), 0.9, hover=True)
    rec.hold(1.4)
    rec.still("spend")
    rec.app.eval("document.removeEventListener('mouseleave', window.__keep, true)")
    mx, my = rec.where(".seg button", "Model")
    rec.move(((rec.x + mx) / 2, y - 60), 0.3)
    rec.app.eval(
        "document.querySelector('.guide').parentElement.dispatchEvent(new Event('mouseleave'))"
    )
    rec.snap()
    rec.move((mx, my), 0.3)
    rec.click()
    rec.hold(1.6)
    rec.still("model")
    rec.go("nav a", "Spikes")
    rec.hold(1.0)
    rec.go(".spikelist button", "new subagents", seconds=0.7)
    rec.move((rec.x + 330, rec.y + 40), 0.4)
    top = rec.app.eval("document.querySelector('.detail').getBoundingClientRect().y - 24")
    rec.scroll(float(top), seconds=0.8)
    rec.hold(2.0)
    rec.still("spike")
    rec.go("nav a", "Checks")
    rec.hold(0.6)
    target = rec.app.eval(
        "(() => { const b = [...document.querySelectorAll('button.copy')][0];"
        " const r = b.getBoundingClientRect(); return r.y - 470; })()"
    )
    rec.scroll(float(target), seconds=1.0)
    rec.hold(0.5)
    rec.go("button.copy", "Copy", seconds=0.6, settle=0.3)
    rec.move((rec.x + 60, rec.y + 34), 0.35)
    rec.hold(1.6)
    rec.still("checks")
    rec.loop_back(first)


def encode(frames: list[tuple[Path, float]], out: Path, tmp: Path) -> None:
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
            "-lossless",
            "1",
            "-compression_level",
            "6",
            "-quality",
            "100",
            "-pix_fmt",
            "bgra",
            "-loop",
            "0",
            str(out / "demo.webp"),
        ],
        check=True,
    )
    total = sum(float(d) for _, d, _ in merged)  # type: ignore[arg-type]
    print(f"{len(frames)} frames, {len(merged)} distinct, {total:.1f} s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--stills", type=Path)
    a = ap.parse_args()
    work = a.work.resolve()
    if not (work / "data.json").exists():
        data.build(work)
    a.out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="capcom-demo-"))
    try:
        with data.serve(work) as base, Chrome(APP_W, APP_H) as chrome:
            rec = Recorder(chrome, base, tmp)
            script(rec)
            encode(rec.frames, a.out, tmp)
            stills = dict(rec.stills)
            shutil.copy(stills["spike"], a.out / "demo.png")
            if a.stills:
                a.stills.mkdir(parents=True, exist_ok=True)
                for name, png in rec.stills:
                    shutil.copy(png, a.stills / f"demo-{name}.png")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    p = a.out / "demo.webp"
    print(f"{p}  {p.stat().st_size / 1024:.0f} KiB")


if __name__ == "__main__":
    main()
