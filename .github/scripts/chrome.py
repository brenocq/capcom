"""Headless Chrome over the DevTools protocol, standard library only.

Chrome runs with --remote-debugging-pipe: commands go to it as NUL-terminated
JSON on fd 3 and replies come back on fd 4, so no port is opened and no
websocket library is needed. It runs in a memory-capped systemd scope when
systemd-run is there (MemoryMax=2G or $CHROME_MEMORY_MAX, no swap), with a throwaway profile and its
background networking off: the only pages it loads are local.

    with Chrome(990, 620) as c:
        c.goto("http://127.0.0.1:8000/")
        c.screenshot(Path("shot.png"))

CHROME=/path overrides the browser (default: google-chrome).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

FLAGS = [
    "--headless=new",
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--no-pings",
    "--metrics-recording-only",
    "--mute-audio",
    "--hide-scrollbars",
    "--font-render-hinting=none",
    "--allow-file-access-from-files",
    # Tabs in the background keep rendering: the demo drives two at once.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]


class Chrome:
    def __init__(self, width: int, height: int, scale: float = 1.0) -> None:
        self.profile = tempfile.mkdtemp(prefix="capcom-chrome-")
        exe = os.environ.get("CHROME") or shutil.which("google-chrome") or "chromium"
        cmd = [exe, *FLAGS, f"--user-data-dir={self.profile}", f"--window-size={width},{height}"]
        if shutil.which("systemd-run"):
            cmd = [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "-p",
                f"MemoryMax={os.environ.get('CHROME_MEMORY_MAX', '2G')}",
                "-p",
                "MemorySwapMax=0",
                *cmd,
            ]
        # Both child ends above fd 10 first, so the dup2s below never clobber each other.
        r, self._w = os.pipe()
        self._r, w = os.pipe()
        to_chrome_r, from_chrome_w = (fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in (r, w))
        os.close(r)
        os.close(w)
        # Chrome reads fd 3 and writes fd 4: dup the pipe ends there in the child.
        self.proc = subprocess.Popen(
            cmd,
            pass_fds=(3, 4),
            preexec_fn=lambda: (os.dup2(to_chrome_r, 3), os.dup2(from_chrome_w, 4)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._buf = b""
        self._id = 0
        self.events: list[dict[str, Any]] = []
        target = self.call("Target.createTarget", url="about:blank", session=False)["targetId"]
        self.session = self.call(
            "Target.attachToTarget", targetId=target, flatten=True, session=False
        )["sessionId"]
        Tab(self, self.session).call("Page.enable")
        Tab(self, self.session).call("Runtime.enable")
        self.viewport(width, height, scale)

    # -- protocol

    def call(self, method: str, session: bool | str = True, **params: Any) -> Any:
        """A command to the browser (session=False), the first tab (True) or a tab id."""
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method, "params": params}
        if session:
            msg["sessionId"] = self.session if session is True else session
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        while True:
            reply = self._read()
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            self.events.append(reply)

    def _read(self) -> dict[str, Any]:
        while b"\0" not in self._buf:
            chunk = os.read(self._r, 1 << 20)
            if not chunk:
                raise RuntimeError("Chrome closed the pipe")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\0", 1)
        return dict(json.loads(line))

    def wait_event(self, method: str, sid: str | None = None, timeout: float = 20) -> None:
        end = time.time() + timeout
        while time.time() < end:
            for i, e in enumerate(self.events):
                if e.get("method") == method and (sid is None or e.get("sessionId") == sid):
                    self.events.pop(i)
                    return
            self.events.append(self._read())
        raise TimeoutError(method)

    def tab(self, width: int, height: int, scale: float = 1.0) -> Tab:
        """Another tab, with its own viewport."""
        target = self.call("Target.createTarget", url="about:blank", newWindow=True, session=False)[
            "targetId"
        ]
        sid = self.call("Target.attachToTarget", targetId=target, flatten=True, session=False)[
            "sessionId"
        ]
        t = Tab(self, sid)
        t.call("Page.enable")
        t.call("Runtime.enable")
        t.viewport(width, height, scale)
        return t

    # -- the first tab

    def viewport(self, width: int, height: int, scale: float = 1.0) -> None:
        Tab(self, self.session).viewport(width, height, scale)

    def media(self, dark: bool = False, reduced: bool = False) -> None:
        Tab(self, self.session).media(dark, reduced)

    def transparent(self) -> None:
        Tab(self, self.session).transparent()

    def goto(self, url: str, settle: float = 0.3) -> None:
        Tab(self, self.session).goto(url, settle)

    def eval(self, js: str) -> Any:
        return Tab(self, self.session).eval(js)

    def screenshot(self, path: Path, clip: dict[str, float] | None = None) -> None:
        Tab(self, self.session).screenshot(path, clip)

    def close(self) -> None:
        with contextlib.suppress(RuntimeError, OSError):
            self.call("Browser.close", session=False)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        os.close(self._w)
        os.close(self._r)
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self) -> Chrome:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Tab:
    """One tab of a Chrome: the same pipe, its own session."""

    def __init__(self, chrome: Chrome, sid: str) -> None:
        self.c, self.sid = chrome, sid

    def call(self, method: str, **params: Any) -> Any:
        return self.c.call(method, session=self.sid, **params)

    def viewport(self, width: int, height: int, scale: float = 1.0) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=scale,
            mobile=False,
        )

    def media(self, dark: bool = False, reduced: bool = False) -> None:
        self.call(
            "Emulation.setEmulatedMedia",
            features=[
                {"name": "prefers-color-scheme", "value": "dark" if dark else "light"},
                {
                    "name": "prefers-reduced-motion",
                    "value": "reduce" if reduced else "no-preference",
                },
            ],
        )

    def transparent(self) -> None:
        self.call(
            "Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0}
        )

    def goto(self, url: str, settle: float = 0.3) -> None:
        self.c.events = [
            e
            for e in self.c.events
            if not (e.get("method") == "Page.loadEventFired" and e.get("sessionId") == self.sid)
        ]
        self.call("Page.navigate", url=url)
        self.c.wait_event("Page.loadEventFired", self.sid)
        time.sleep(settle)

    def eval(self, js: str) -> Any:
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"{js[:80]}: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    def screenshot(self, path: Path, clip: dict[str, float] | None = None) -> None:
        params: dict[str, Any] = {"format": "png"}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        data = self.call("Page.captureScreenshot", **params)["data"]
        path.write_bytes(base64.b64decode(data))

    def mouse(self, kind: str, x: float, y: float, **kw: Any) -> None:
        """A real mouse event: mouseMoved, mousePressed or mouseReleased."""
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, **kw)

    def click(self, x: float, y: float) -> None:
        self.mouse("mousePressed", x, y, button="left", clickCount=1)
        self.mouse("mouseReleased", x, y, button="left", clickCount=1)
