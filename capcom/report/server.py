"""`capcom serve`: the local website.

Standard library only. The server binds to 127.0.0.1, answers GET only, sends no
CORS headers, and refuses requests whose Host header is not this machine (so a web
page cannot reach it through DNS rebinding). Each API request opens the index
read-only and closes it when done. Pages are static files from `static/`; the API
is in `api.py` and documented in `API.md`.

The index stays fresh through a `Scanner`: an incremental `run_scan` at start and
then every minute, in one background thread. Only that thread writes the index
(SQLite's WAL lets the handlers read while it does), and a lock makes sure two scans
never overlap.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from capcom.config import Config
from capcom.cost.prices import load_prices
from capcom.report import api
from capcom.report.analysis import Analysis, Unavailable, default_analysis
from capcom.store.index import open_index
from capcom.types import PriceTable

HOST = "127.0.0.1"
STATIC = Path(__file__).parent / "static"
PAGES = {"", "spend", "sessions", "session", "spikes", "checks"}
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
RUN_SCAN = "Run <code>capcom scan</code> in a terminal, then reload this page."
SCANNING = "capcom serve is reading the logs into the index now; reload in a few minutes."
NO_INDEX_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>capcom: no index</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="bare"><main class="panel narrow">
<h1>No index to read yet</h1>
<p>{reason}</p>
<p>{action}</p>
</main></body></html>
"""


@dataclass(slots=True)
class Site:
    """What a request handler needs: where the index is, the prices, the analysis
    modules and the clock (injectable for tests)."""

    index_path: Path
    table: PriceTable
    analysis: Analysis
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    tz: tzinfo | None = None
    openclaw_root: Path | None = None
    scanner: Scanner | None = None


class Scanner:
    """Incremental scans in one background thread: one at start, then one every
    `interval` seconds after the previous one ends. `scan_once` returns False
    without scanning when a scan is already running, so two never overlap."""

    def __init__(
        self, scan: Callable[[], object], interval: float = 60.0, name: str = "capcom-scan"
    ) -> None:
        self._scan = scan
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.scans = 0  # completed scans, failed ones included

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def scan_once(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._scan()
        except Exception:
            print("capcom serve: the background scan failed:", file=sys.stderr)
            traceback.print_exc()
        finally:
            self.scans += 1
            self._lock.release()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Ask the loop to end after the current scan; wait up to `timeout`."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)


class IndexMissing(Exception):
    pass


def _open(site: Site) -> Any:
    try:
        return open_index(site.index_path, readonly=True)
    except FileNotFoundError:
        raise IndexMissing("capcom has not built its index yet.") from None
    except RuntimeError:
        raise IndexMissing("The index was written by another version of capcom.") from None
    except (ValueError, sqlite3.Error):
        raise IndexMissing("The index could not be opened.") from None


class Handler(BaseHTTPRequestHandler):
    site: Site  # set on the subclass made by make_server
    server_version = "capcom"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet: the paths may hold session ids; nothing to log

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in {"127.0.0.1", "localhost"}

    def _send(self, status: int, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        self._send(status, body, TYPES[".json"])

    def _error(self, status: int, code: str, message: str, **extra: Any) -> None:
        self._json(status, {"error": code, "message": message} | extra)

    def do_GET(self) -> None:
        try:
            if not self._host_ok():
                self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
                return
            url = urlsplit(self.path)
            path = unquote(url.path)
            if path.startswith("/api/"):
                self._api(path[5:], dict(parse_qsl(url.query)))
            elif path.startswith("/static/"):
                self._static(path[8:])
            else:
                self._page(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # the details go to the terminal only; the reply says nothing about them
            print("capcom serve: error while answering a request:", file=sys.stderr)
            traceback.print_exc()
            with contextlib.suppress(OSError):
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"internal error", "text/plain")

    def do_HEAD(self) -> None:
        self.do_GET()  # _send leaves the body out

    def _not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _page(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if parts[0] not in PAGES or len(parts) > (2 if parts[0] == "session" else 1):
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        try:
            _open(self.site).close()
        except IndexMissing as err:
            scanning = self.site.scanner is not None and self.site.scanner.running
            page = NO_INDEX_PAGE.format(reason=err, action=SCANNING if scanning else RUN_SCAN)
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, page.encode(), TYPES[".html"])
            return
        self._static("index.html")

    def _static(self, name: str) -> None:
        root = STATIC.resolve()
        try:
            target = (root / name).resolve()
            ok = bool(name) and target.is_relative_to(root) and target.is_file()
            ctype = TYPES.get(target.suffix) if ok else None
            body = target.read_bytes() if ctype else b""
        except (ValueError, OSError):  # a NUL byte, a name too long, a vanished file
            ctype = None
        if ctype is None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        self._send(HTTPStatus.OK, body, ctype, cache=False)

    def _api(self, route: str, query: dict[str, str]) -> None:
        parts = route.strip("/").split("/")
        name = parts[0]
        if name not in ENDPOINTS or len(parts) != (2 if name == "session" else 1):
            self._error(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")
            return
        try:
            index = _open(self.site)
        except IndexMissing as err:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE, "no_index", f"{err} Run `capcom scan` first."
            )
            return
        try:
            ctx = api.Context(
                index=index,
                table=self.site.table,
                analysis=self.site.analysis,
                now=self.site.clock(),
                tz=self.site.tz,
                index_path=self.site.index_path,
                openclaw_root=self.site.openclaw_root,
            )
            payload = ENDPOINTS[name](ctx, parts[1] if len(parts) == 2 else "", query)
            self._json(HTTPStatus.OK, payload)
        except api.ApiError as err:
            self._error(err.status, err.code, err.message)
        except Unavailable as err:
            self._error(
                HTTPStatus.NOT_IMPLEMENTED,
                "unavailable",
                f"Coming in milestone {err.milestone}.",
                module=err.module,
                milestone=err.milestone,
            )
        except sqlite3.Error:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE, "index_busy", "The index could not be read."
            )
        finally:
            index.close()


Endpoint = Callable[[api.Context, str, dict[str, str]], Any]
ENDPOINTS: dict[str, Endpoint] = {
    "status": lambda c, _, q: api.status(c),
    "overview": lambda c, _, q: api.overview(c),
    "spend": lambda c, _, q: api.spend(c, q),
    "sessions": lambda c, _, q: api.sessions(c, q),
    "session": lambda c, sid, q: api.session(c, sid),
    "live": lambda c, _, q: api.live(c),
    "spikes": lambda c, _, q: api.spikes(c, q),
    "checks": lambda c, _, q: api.checks(c, q),
}


def make_server(site: Site, port: int) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1:`port` (0: any free port); not started."""
    handler = type("SiteHandler", (Handler,), {"site": site})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def run_serve(config: Config, port: int, open_browser: bool, scan: bool = True) -> int:
    scanner = None
    if scan:
        from capcom.store.scan import run_scan

        scanner = Scanner(lambda: run_scan(config, quiet=True))
    site = Site(
        index_path=config.index_path,
        table=load_prices(),
        analysis=default_analysis(),
        openclaw_root=config.openclaw_root,
        scanner=scanner,
    )
    try:
        server = make_server(site, port)
    except OSError as err:
        print(f"capcom serve: cannot listen on {HOST}:{port} ({err})", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    fresh = "rescanning every minute" if scanner else "no background scan"
    print(f"capcom: serving {url} (local only, {fresh}; Ctrl+C to stop)", flush=True)
    if scanner is not None:
        scanner.start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if scanner is not None:
            scanner.stop(timeout=5)
    return 0
