"""`capcom serve`: the local website.

Standard library only. The server binds to 127.0.0.1, answers GET only, sends no
CORS headers, and refuses requests whose Host header is not this machine (so a web
page cannot reach it through DNS rebinding). Each API request opens the index
read-only and closes it when done. Pages are static files from `static/`; the API
is in `api.py` and documented in `API.md`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
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
NO_INDEX_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>capcom: no index</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="bare"><main class="panel narrow">
<h1>No index to read yet</h1>
<p>{reason}</p>
<p>Run <code>capcom scan</code> in a terminal, then reload this page.</p>
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
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
            return
        url = urlsplit(self.path)
        path = unquote(url.path)
        try:
            if path.startswith("/api/"):
                self._api(path[5:], dict(parse_qsl(url.query)))
            elif path.startswith("/static/"):
                self._static(path[8:])
            else:
                self._page(path)
        except BrokenPipeError:
            pass

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
            body = NO_INDEX_PAGE.format(reason=err).encode()
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, body, TYPES[".html"])
            return
        self._static("index.html")

    def _static(self, name: str) -> None:
        root = STATIC.resolve()
        target = (root / name).resolve()
        if not name or not target.is_relative_to(root) or not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        ctype = TYPES.get(target.suffix)
        if ctype is None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        self._send(HTTPStatus.OK, target.read_bytes(), ctype, cache=False)

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


def run_serve(config: Config, port: int, open_browser: bool) -> int:
    site = Site(index_path=config.index_path, table=load_prices(), analysis=default_analysis())
    try:
        server = make_server(site, port)
    except OSError as err:
        print(f"capcom serve: cannot listen on {HOST}:{port} ({err})", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"capcom: serving {url} (local only, read-only; Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
