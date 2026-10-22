"""The layout harness: every page of the website, at phone to wide-desktop widths and
in both themes, checked in headless Chrome for horizontal page scroll, overlapping
text or controls, targets too small to hit, and clipped text.

The data is synthetic (made up here, never read from ~/.claude); the server is the
real one, on 127.0.0.1 and a free port. Chrome is driven over its DevTools pipe
(`--remote-debugging-pipe`: NUL-separated JSON on fds 3 and 4), so the harness needs
the standard library only. The checks themselves are one JavaScript function,
`layout_checks.js`, that returns the problems found, each naming the element.

    uv run python -m tests.report.layout --out /some/dir   # all pages, sheets in /some/dir
    uv run python -m tests.report.layout --widths 360,1440 --themes light

`tests/report/test_layout.py` runs it under pytest when Chrome is installed.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from capcom.cost.prices import load_prices
from capcom.limits.model import append_samples
from capcom.report.analysis import Analysis, default_analysis
from capcom.report.server import Site, make_server
from capcom.store.index import SqliteIndex, open_index
from capcom.types import (
    AgentInfo,
    Batch,
    Event,
    EventKind,
    Evidence,
    FileState,
    Finding,
    Index,
    LimitSample,
    LimitWindow,
    LogFile,
    LogKind,
    PriceTable,
    Record,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)

CHECKS_JS = Path(__file__).with_name("layout_checks.js")
# (label, CSS width, device scale factor): 1280 at 200 % zoom is a 640 px viewport
WIDTHS: tuple[tuple[str, int, float], ...] = (
    ("360", 360, 1),
    ("390", 390, 1),
    ("768", 768, 1),
    ("1024", 1024, 1),
    ("1280", 1280, 1),
    ("1280@200%", 640, 2),
    ("1440", 1440, 1),
    ("1920", 1920, 1),
    ("2560", 2560, 1),
)
THEMES = ("light", "dark")
TOUCH_BELOW = 900  # the widths that get the top bar and 44 px targets
HEIGHT = 900


# --------------------------------------------------------------------------- synthetic data

PROJECTS = (
    "-home-user-alpha",
    "-home-user-beta",
    "-home-user-a-rather-long-project-directory-name-for-the-layout-tests",
    "-home-user-gamma",
    "-tmp-scratch-7",
    "-home-user-delta-service",
)
TITLES = (
    "Fix the parser",
    "A long synthetic session title that keeps on going to test how narrow cards wrap it",
    "Refactor the store",
    None,
    "Plan the milestone and write the notes for the next three weeks of work",
    "Tune prompts",
    "Supercalifragilisticexpialidocious-unbroken-identifier-in-a-title-for-overflow",
)
_ids = iter(range(1, 10_000_000))


def _req(
    sid: str,
    when: datetime,
    project: str,
    agent: str | None = None,
    model: str = "claude-opus-5-5",
    tools: Sequence[tuple[str, str | None]] = (),
    **usage: int,
) -> Request:
    i = next(_ids)
    return Request(
        message_id=f"msg_{i}",
        session_id=sid,
        agent_id=agent,
        project_dir=project,
        timestamp=when,
        model=model,
        usage=Usage(**usage),
        tool_calls=tuple(ToolCall(f"tu_{i}_{k}", n, t) for k, (n, t) in enumerate(tools)),
    )


def _put(
    ix: SqliteIndex,
    path: str,
    kind: LogKind,
    project: str,
    sid: str,
    agent: str | None,
    records: Sequence[Record],
) -> None:
    log = LogFile(Path(path), kind, project, sid, agent)
    ix.apply(log, Batch(list(records), 100), FileState(log.path, 1, 100, 1, 100))


def build_index(path: Path, now: datetime) -> None:
    """Thirty days of made-up sessions over six projects: long titles and project
    names, subagents, a burst that makes a spike, and eight sessions live now."""
    ix = open_index(path)
    models = ("claude-opus-5-5", "claude-sonnet-4-5", "claude-haiku-4-5-20251001")
    long_path = "/home/user/a/very/deeply/nested/folder/structure/with/a_long_file_name.py"
    n = 0
    for day in range(30, -1, -1):
        for k in range(2 if day % 3 else 4):
            n += 1
            sid = f"s{n:03d}"
            project = PROJECTS[(n + day) % len(PROJECTS)]
            live = day == 0 and k < 2
            start = (
                now - timedelta(days=day, hours=2 + 3 * k)
                if not live
                else now - timedelta(hours=1 + k)
            )
            steps = 6 + (n * 7) % 30
            burst = day in (2, 6) and k == 0
            reqs = []
            for i in range(steps):
                when = start + timedelta(minutes=4 * i)
                if live and i == steps - 1:
                    when = now - timedelta(minutes=2 + k)
                if when > now:
                    break
                reqs.append(
                    _req(
                        sid,
                        when,
                        project,
                        model=models[(n + i) % 3],
                        tools=[("Read", long_path), ("Bash", "pytest")] if i % 4 == 0 else (),
                        input=200 + 50 * i,
                        cache_read=20_000 + 3_000 * i,
                        cache_write_5m=(3_000_000 if burst and 2 < i < 8 else 4_000),
                        output=(60_000 if burst and 2 < i < 8 else 300 + 40 * (i % 9)),
                    )
                )
            if not reqs:
                continue
            title = TITLES[n % len(TITLES)]
            info = SessionInfo(
                sid,
                project,
                cwd="/home/user/x",
                title=title,
                git_branch="main",
                first_seen=reqs[0].timestamp,
                last_seen=reqs[-1].timestamp,
            )
            recs: list[Record] = [info, *reqs]
            if n % 5 == 0:
                recs.append(
                    Event(
                        EventKind.COMPACTION,
                        sid,
                        None,
                        reqs[len(reqs) // 2].timestamp,
                        f"c{n}",
                        {"trigger": "auto", "pre_tokens": 180_000, "post_tokens": 20_000},
                    )
                )
            _put(ix, f"/logs/{project}/{sid}.jsonl", LogKind.SESSION, project, sid, None, recs)
            if n % 3 == 0:
                aid = f"a{n}"
                sub = [
                    _req(
                        sid,
                        r.timestamp + timedelta(seconds=30),
                        project,
                        aid,
                        model="claude-haiku-4-5-20251001",
                        cache_write_5m=6_000,
                        output=200,
                    )
                    for r in reqs[:4]
                ]
                agent = AgentInfo(
                    aid,
                    sid,
                    agent_type="scout",
                    finished=True,
                    description="Map the parser and every module that calls it",
                )
                _put(
                    ix,
                    f"/logs/{project}/{sid}/subagents/agent-{aid}.jsonl",
                    LogKind.SUBAGENT,
                    project,
                    sid,
                    aid,
                    [agent, *sub],
                )
    # six more live sessions, so the live list is full
    for k in range(6):
        sid, project = f"live{k}", PROJECTS[k]
        reqs = [
            _req(
                sid, now - timedelta(minutes=9 - k + 3 * j), project, cache_read=50_000, output=900
            )
            for j in range(3)
            if 9 - k - 3 * j > 0
        ]
        info = SessionInfo(
            sid,
            project,
            title=TITLES[(k + 1) % len(TITLES)],
            first_seen=reqs[0].timestamp,
            last_seen=reqs[-1].timestamp,
        )
        _put(ix, f"/logs/{project}/{sid}.jsonl", LogKind.SESSION, project, sid, None, [info, *reqs])
    ix.close()


def write_limits(state_dir: Path, now: datetime) -> None:
    """Three days of status-line readings of both windows, rising between resets."""
    samples = []
    t = now - timedelta(days=3)
    while t <= now:
        hours = (t - (now - timedelta(days=3))).total_seconds() / 3600
        five_reset = now - timedelta(days=3) + timedelta(hours=5 * (int(hours // 5) + 1))
        five = min(99.0, 3 + (hours % 5) * 9)
        week = min(99.0, 10 + hours * 0.8)
        samples.append(LimitSample(t, LimitWindow.FIVE_HOUR, five, five_reset, "s001"))
        samples.append(LimitSample(t, LimitWindow.SEVEN_DAY, week, now + timedelta(days=4), "s001"))
        t += timedelta(minutes=20)
    append_samples(state_dir, samples)


def findings(index: Index, table: PriceTable, since: datetime, until: datetime) -> list[Finding]:
    """Made-up setup findings with the long parts real ones can have."""
    ev = (
        Evidence(
            "Read of /home/user/a/very/deeply/nested/folder/structure/with/a_long_file_name.py"
            " 42 times",
            session_id="s001",
        ),
        Evidence("12 sessions loaded the same 40k-token instructions file", session_id="s002"),
    )
    change = (
        '{"permissions": {"deny": ["Read(/home/user/a/very/deeply/nested/folder/structure/'
        'with/node_modules/**)"]}, "env": {"MAX_THINKING_TOKENS": "8000"}}'
    )
    return [
        Finding(
            "big_reads",
            "Large files read again and again",
            "all",
            "One file was read 42 times across 12 sessions, about 1.2M tokens of input.",
            12.5,
            "Point the agent at the part it needs, or add the folder to the deny list.",
            change,
            ev,
        ),
        Finding(
            "long_instructions",
            "An instructions file that every session loads, and that grew past 40k tokens",
            PROJECTS[2],
            "Every session pays for it before the first reply.",
            4.25,
            "Split the rarely used parts into files read on demand.",
            None,
            ev[1:],
        ),
        Finding("small", "A small one", "-home-user-beta", "Little to save.", 0.3, "Optional."),
    ]


@contextlib.contextmanager
def synthetic_site(workdir: Path) -> Iterator[str]:
    """Serve the synthetic data; yield the base URL."""
    now = datetime.now(UTC).replace(microsecond=0)
    index = workdir / "index.sqlite"
    build_index(index, now)
    state = workdir / "state"
    write_limits(state, now)
    real = default_analysis()
    analysis = Analysis(
        real.cumulative_fn,
        real.per_step_fn,
        real.find_spikes_fn,
        real.explain_fn,
        findings,
        real.regimes_fn,
    )
    site = Site(index, load_prices(), analysis, tz=UTC, state_dir=state)
    server = make_server(site, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- Chrome over a pipe


def find_chrome() -> str | None:
    env = os.environ.get("CAPCOM_CHROME")
    if env:
        return env if Path(env).exists() else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class CdpError(RuntimeError):
    pass


class Chrome:
    """Headless Chrome with one page, driven over --remote-debugging-pipe."""

    def __init__(self, binary: str, profile: Path) -> None:
        to_child_r, self._w = os.pipe()
        self._r, from_child_w = os.pipe()
        # the child reads commands on fd 3 and writes replies on fd 4
        hi_r, hi_w = os.dup(to_child_r), os.dup(from_child_w)
        for fd in (to_child_r, from_child_w):
            os.close(fd)

        args = [
            binary,
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
            "--disable-default-apps",
            "--mute-audio",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "about:blank",
        ]
        if os.environ.get("CI"):
            # CI runners (Ubuntu 24.04) block the user namespaces Chrome's sandbox needs,
            # so Chrome exits at once; it only loads this suite's synthetic local pages
            args[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
        # a shell puts the pipe ends on fds 3 and 4, where Chrome expects them; bash,
        # since dash (Ubuntu's /bin/sh) rejects fd numbers above 9 ("Bad fd number")
        wire = f'exec "$@" 3<&{hi_r} 4>&{hi_w} {hi_r}<&- {hi_w}>&-'
        # Chrome's own errors go to a file, so a Chrome that dies says why
        profile.mkdir(parents=True, exist_ok=True)
        self._errlog = profile / "chrome-stderr.log"
        with self._errlog.open("wb") as err:
            self.proc = subprocess.Popen(
                [shutil.which("bash") or "/bin/bash", "-c", wire, "sh", *args],
                pass_fds=(hi_r, hi_w),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
            )
        os.close(hi_r)
        os.close(hi_w)
        self._buf = b""
        self._next = 0
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]
        self.page("Page.enable")
        self.page("Runtime.enable")
        self.page("Emulation.setTimezoneOverride", {"timezoneId": "UTC"})

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        self._next += 1
        msg: dict[str, Any] = {"id": self._next, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while True:
            while b"\0" not in self._buf:
                if time.monotonic() > deadline:
                    raise CdpError(f"{method}: no reply in {timeout} s")
                chunk = os.read(self._r, 1 << 20)
                if not chunk:
                    raise CdpError("Chrome closed the pipe" + self._last_errors())
                self._buf += chunk
            raw, self._buf = self._buf.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") != self._next:
                continue  # an event, or a late reply
            if "error" in reply:
                raise CdpError(f"{method}: {reply['error'].get('message')}")
            result: dict[str, Any] = reply.get("result", {})
            return result

    def page(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        return self.call(method, params, self.session, timeout)

    def js(self, expr: str, timeout: float = 30) -> Any:
        out = self.page(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True},
            timeout,
        )
        if "exceptionDetails" in out:
            d = out["exceptionDetails"]
            raise CdpError(f"script error: {d.get('exception', {}).get('description') or d}")
        return out.get("result", {}).get("value")

    def viewport(self, width: int, scale: float, theme: str) -> None:
        self.page(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": HEIGHT, "deviceScaleFactor": scale, "mobile": False},
        )
        self.page(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": theme}]},
        )

    def key(self, key: str, shift: bool = False) -> None:
        codes = {"Escape": 27, "Tab": 9, "Enter": 13}
        base = {
            "key": key,
            "code": key,
            "windowsVirtualKeyCode": codes[key],
            "modifiers": 8 if shift else 0,
        }
        self.page("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
        self.page("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def click(self, x: float, y: float) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.page(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    def shot(self, width: int, height: int, scale: float) -> bytes:
        out = self.page(
            "Page.captureScreenshot",
            {
                "format": "jpeg",
                "quality": 62,
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": scale},
            },
            timeout=60,
        )
        return base64.b64decode(out["data"])

    def _last_errors(self) -> str:
        try:
            lines = self._errlog.read_text(errors="replace").strip().splitlines()[-25:]
        except OSError:
            return ""
        return ("; Chrome said:\n" + "\n".join(lines)) if lines else ""

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.call("Browser.close", timeout=5)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for fd in (self._r, self._w):
            with contextlib.suppress(OSError):
                os.close(fd)


# --------------------------------------------------------------------------- the run

READY = """new Promise((done) => {
  const t0 = Date.now();
  const tick = () => {
    const main = document.getElementById('main');
    const ok = !window.__stale && document.readyState === 'complete' && main
      && main.childElementCount && !main.querySelector(':scope > p.loading')
      && !document.body.textContent.includes('Reading the index…');
    if (ok || Date.now() - t0 > 15000) setTimeout(() => done(!!ok), 400); else setTimeout(tick, 80);
  };
  tick();
})"""
SETTLE = (
    "new Promise((d) => requestAnimationFrame(() => requestAnimationFrame(() => "
    "setTimeout(() => d(true), 250))))"
)


@dataclass
class Shot:
    page: str
    width: str
    theme: str
    jpeg: bytes
    css_width: int


@dataclass
class Result:
    problems: list[dict[str, Any]] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def lines(self) -> list[str]:
        return [
            f"{p['page']} @ {p['width']} {p['theme']}: {p['kind']}: {p['el']}"
            + (f" ({p['detail']})" if p.get("detail") else "")
            for p in self.problems
        ]


def discover(chrome: Chrome, base: str) -> list[tuple[str, str]]:
    """The pages to check: every link of the navigation, plus a session, a day and a
    spike found by following the site's own links. A page added to the navigation
    is checked without a change here."""
    load(chrome, base + "/")
    nav = chrome.js("""[...document.querySelectorAll('#sidebar .nav a[href]')]
      .map((a) => [a.textContent.trim().toLowerCase() || a.getAttribute('href'),
                   a.getAttribute('href')])""")
    pages: list[tuple[str, str]] = [(name, href) for name, href in nav]
    for name, list_page, selector in (
        ("session", "/sessions", "#main a[href^='/session/']"),
        ("day", "/spend", "#main a[href^='/day/']"),
    ):
        load(chrome, base + list_page)
        href = chrome.js(
            f'(document.querySelector("{selector}") || {{}}).getAttribute'
            f"?.call(document.querySelector(\"{selector}\"), 'href')"
        )
        if href:
            pages.append((name, href))
    seen: set[str] = set()
    return [(n, h) for n, h in pages if not (h in seen or seen.add(h))]  # type: ignore[func-returns-value]


def load(chrome: Chrome, url: str) -> bool:
    with contextlib.suppress(CdpError):
        chrome.js("window.__stale = true")
    chrome.page("Page.navigate", {"url": url})
    return bool(chrome.js(READY, timeout=30))


def check(chrome: Chrome, opts: dict[str, Any]) -> list[dict[str, Any]]:
    src = CHECKS_JS.read_text()
    out = chrome.js(f"({src})({json.dumps(opts)})")
    return list(out or [])


def drawer_checks(
    chrome: Chrome, touch: bool, shoot: Callable[[], None] | None = None
) -> list[dict[str, Any]]:
    """Open the menu, check the drawer, close it with Escape, and check focus."""
    probs: list[dict[str, Any]] = []
    btn = chrome.js("""(() => { const b = document.querySelector('[aria-controls="sidebar"]');
      if (!b || !b.offsetParent) return null; const r = b.getBoundingClientRect();
      return [r.x + r.width / 2, r.y + r.height / 2]; })()""")
    if not touch:
        if btn:
            probs.append({"kind": "menu-shown-on-desktop", "el": "the menu button"})
        return probs
    if not btn:
        return [{"kind": "no-menu-button", "el": "the top bar"}]
    chrome.click(*btn)
    chrome.js(SETTLE)
    state = chrome.js("""(() => { const b = document.querySelector('[aria-controls="sidebar"]');
      const nav = document.getElementById('sidebar');
      return { expanded: b.getAttribute('aria-expanded'),
               inside: nav.contains(document.activeElement),
               shown: nav.checkVisibility() }; })()""")
    if state != {"expanded": "true", "inside": True, "shown": True}:
        probs.append({"kind": "drawer-open", "el": "#sidebar", "detail": json.dumps(state)})
    probs += check(chrome, {"touch": touch, "modal": "#sidebar"})
    if shoot is not None:
        shoot()
    for _ in range(25):  # the focus stays in the drawer
        chrome.key("Tab")
    if not chrome.js(
        "document.getElementById('sidebar').contains(document.activeElement) "
        "|| document.activeElement.getAttribute('aria-controls') === 'sidebar'"
    ):
        probs.append({"kind": "focus-escapes-drawer", "el": "#sidebar"})
    chrome.key("Escape")
    chrome.js(SETTLE)
    state = chrome.js("""(() => { const b = document.querySelector('[aria-controls="sidebar"]');
      return { expanded: b.getAttribute('aria-expanded'), focus: document.activeElement === b,
               shown: document.getElementById('sidebar').checkVisibility() }; })()""")
    if state != {"expanded": "false", "focus": True, "shown": False}:
        probs.append({"kind": "drawer-escape", "el": "#sidebar", "detail": json.dumps(state)})
    return probs


def one_width(
    chrome: Chrome,
    res: Result,
    page: str,
    theme: str,
    label: str,
    width: int,
    scale: float,
    out_dir: Path | None,
) -> None:
    """Check the loaded page at one width (and, on the first page, the menu sheet)."""
    chrome.viewport(width, scale, theme)
    chrome.js(SETTLE)
    touch = width < TOUCH_BELOW
    found = check(chrome, {"touch": touch})
    sheets = out_dir is not None and scale == 1
    if sheets:
        res.shots.append(screenshot(chrome, page, label, theme, width))
    if page == res.pages[0]:

        def shoot() -> None:
            jpeg = chrome.shot(width, HEIGHT, min(1.0, COLUMN / width))
            res.shots.append(Shot("menu", label, theme, jpeg, width))

        found += drawer_checks(chrome, touch, shoot if sheets else None)
    res.problems += [{"page": page, "width": label, "theme": theme, **p} for p in found]


def run(
    out_dir: Path | None,
    widths: Sequence[str] | None = None,
    themes: Sequence[str] = THEMES,
    pages: Sequence[str] | None = None,
) -> Result:
    binary = find_chrome()
    if binary is None:
        raise RuntimeError("no Chrome found (set CAPCOM_CHROME)")
    sizes = [w for w in WIDTHS if widths is None or w[0] in widths]
    res = Result()
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="capcom-layout-") as tmp:
        work = Path(tmp)
        with synthetic_site(work) as base:
            chrome = Chrome(binary, work / "profile")
            try:
                found = discover(chrome, base)
                found = [p for p in found if pages is None or p[0] in pages]
                res.pages = [n for n, _ in found]
                for name, href in found:
                    for theme in themes:
                        first = sizes[0]
                        chrome.viewport(first[1], first[2], theme)
                        if not load(chrome, base + href):
                            res.problems.append(
                                {
                                    "page": name,
                                    "width": first[0],
                                    "theme": theme,
                                    "kind": "not-ready",
                                    "el": href,
                                }
                            )
                        for label, width, scale in sizes:
                            one_width(chrome, res, name, theme, label, width, scale, out_dir)
                if out_dir is not None:
                    write_sheets(chrome, res, out_dir)
            finally:
                chrome.close()
    res.seconds = time.monotonic() - t0
    return res


# --------------------------------------------------------------------------- contact sheets

COLUMN = 360  # px each screenshot gets on a sheet
MAX_TALL = 1500  # px of sheet height per screenshot


def screenshot(chrome: Chrome, page: str, label: str, theme: str, width: int) -> Shot:
    tall = int(chrome.js("document.documentElement.scrollHeight"))
    scale = min(1.0, COLUMN / width)
    height = min(tall, int(MAX_TALL / scale))
    chrome.js("window.scrollTo(0, 0)")
    return Shot(page, label, theme, chrome.shot(width, height, scale), width)


def _sheet(chrome: Chrome, title: str, cells: list[tuple[str, Shot]], path: Path) -> None:
    figs = "".join(
        f"<figure><figcaption>{html.escape(cap)}</figcaption>"
        f'<img src="data:image/jpeg;base64,{base64.b64encode(s.jpeg).decode()}"></figure>'
        for cap, s in cells
    )
    doc = (
        "<!doctype html><meta charset=utf-8><style>body{margin:0;padding:12px;background:#888;"
        "font:13px system-ui;display:flex;flex-direction:column;gap:8px;width:max-content}"
        "h1{margin:0;font-size:16px}.row{display:flex;gap:10px;align-items:flex-start}"
        "figure{margin:0;display:flex;flex-direction:column;gap:4px}"
        "img{outline:1px solid #333;max-width:360px}</style>"
        f"<h1>{html.escape(title)}</h1><div class=row>{figs}</div>"
    )
    chrome.page(
        "Emulation.setDeviceMetricsOverride",
        {"width": 800, "height": 600, "deviceScaleFactor": 1, "mobile": False},
    )
    frame = chrome.page("Page.getFrameTree")["frameTree"]["frame"]["id"]
    chrome.page("Page.setDocumentContent", {"frameId": frame, "html": doc})
    chrome.js("Promise.all([...document.images].map((i) => i.decode().catch(() => 0)))")
    w, h = chrome.js("[document.body.scrollWidth, document.body.scrollHeight]")
    path.write_bytes(chrome.shot(int(w), int(h), 1))


def write_sheets(chrome: Chrome, res: Result, out_dir: Path) -> None:
    """One sheet per width (every page, light above dark) and one per page (every
    width, light theme)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chrome.page("Page.navigate", {"url": "about:blank"})
    for label in dict.fromkeys(s.width for s in res.shots):
        for theme in THEMES:
            cells = [(s.page, s) for s in res.shots if s.width == label and s.theme == theme]
            if cells:
                _sheet(
                    chrome, f"{label} px, {theme}", cells, out_dir / f"width-{label}-{theme}.jpg"
                )
    for page in res.pages:
        cells = [(s.width, s) for s in res.shots if s.page == page and s.theme == "light"]
        if cells:
            _sheet(chrome, page, cells, out_dir / f"page-{page}.jpg")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help="folder for the contact sheets")
    ap.add_argument("--widths", help="comma-separated labels, e.g. 360,1440")
    ap.add_argument("--themes", default="light,dark")
    ap.add_argument("--pages", help="comma-separated page names")
    a = ap.parse_args(argv)
    res = run(
        a.out,
        a.widths.split(",") if a.widths else None,
        a.themes.split(","),
        a.pages.split(",") if a.pages else None,
    )
    for line in res.lines():
        print(line)
    print(
        f"{len(res.problems)} problems on {len(res.pages)} pages ({', '.join(res.pages)}) "
        f"in {res.seconds:.0f} s",
        file=sys.stderr,
    )
    return 1 if res.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
