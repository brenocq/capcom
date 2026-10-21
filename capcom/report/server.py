"""`capcom serve`: the local website.

Standard library only. The server binds to 127.0.0.1, answers GET only, sends no
CORS headers, and refuses requests whose Host header is not this machine (so a web
page cannot reach it through DNS rebinding). Each API request opens the index
read-only and closes it when done. Pages are static files from `static/`; the API
is in `api.py` and documented in `API.md`.

API replies are cached per index generation (`ApiCache`): the key holds the index
files' sizes and modification times and the index's `PRAGMA data_version` on a held
read connection, so any write invalidates every entry and a scan that wrote nothing
none, plus only the query parameters the endpoint reads. Entries also expire after
a few minutes, since some numbers move with the clock alone, and the cache is
bounded in bytes.

The index stays fresh through a `Scanner`: an incremental `run_scan` at start and
then every minute, in one background thread. Only that thread writes the index
(SQLite's WAL lets the handlers read while it does), and a lock makes sure two scans
never overlap.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, unquote, urlsplit

from capcom.config import Config
from capcom.cost.prices import load_prices
from capcom.limits.cli import adopt_samples
from capcom.limits.model import samples_path
from capcom.report import api
from capcom.report.analysis import Analysis, Unavailable, default_analysis
from capcom.report.jobs import Job, Jobs, Result, computing_body
from capcom.store.index import open_index
from capcom.types import PriceTable

HOST = "127.0.0.1"
STATIC = Path(__file__).parent / "static"
PAGES = {"", "spend", "sessions", "session", "day", "spikes", "checks", "limits"}
WITH_ARG = {"session", "day"}  # pages and endpoints that take one path segment
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


CACHE_BYTES = 32 * 1024 * 1024  # encoded replies kept, all endpoints together
CACHE_TTL = 300.0  # seconds: "today", "last hour" and "running" move with the clock
UNCACHED = {"status", "live"}  # cheap, and about the current minute
PATIENCE = 0.3  # seconds a request waits for its reply before answering 202


class ApiCache:
    """Encoded API replies by (generation, endpoint, arguments), least recently used
    first out once they pass `max_bytes`; an entry older than `ttl` seconds of the
    site's clock is computed again. Thread-safe."""

    def __init__(self, max_bytes: int = CACHE_BYTES, ttl: float = CACHE_TTL) -> None:
        self.max_bytes = max_bytes
        self.ttl = ttl
        self._entries: OrderedDict[tuple[Any, ...], tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self._watch: DataVersion | None = None
        # the last reply of each view (the key without its generation and date),
        # kept after its generation ended: shown, marked stale, while the new one
        # is computed
        self._stale: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        self._stale_bytes = 0

    def watch(self, index_path: Path) -> DataVersion:
        """The held data_version reader of `index_path`, made on first use."""
        with self._lock:
            if self._watch is None or self._watch.index_path != index_path:
                self._watch = DataVersion(index_path)
            return self._watch

    def get(self, key: tuple[Any, ...], now: float) -> bytes | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or now - entry[0] > self.ttl or now < entry[0]:
                if entry is not None:
                    self._retire(key)
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[1]

    def put(self, key: tuple[Any, ...], now: float, body: bytes) -> None:
        if len(body) > self.max_bytes // 4:
            return  # one reply never crowds out the rest
        with self._lock:
            if key in self._entries:
                self._drop(key)
            # entries of an older generation are dead: drop them first
            for old in [k for k in self._entries if k[0] != key[0]]:
                self._retire(old)
            self._entries[key] = (now, body)
            self._bytes += len(body)
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def stale(self, key: tuple[Any, ...]) -> bytes | None:
        """The last reply of `key`'s view from an earlier generation or day, if kept."""
        with self._lock:
            view = key[2:]
            # an entry of an older generation is retired only when a newer one is put
            for k in reversed(self._entries):
                if k[2:] == view and k != key:
                    return self._entries[k][1]
            return self._stale.get(view)

    def _drop(self, key: tuple[Any, ...]) -> None:
        _, body = self._entries.pop(key)
        self._bytes -= len(body)

    def _retire(self, key: tuple[Any, ...]) -> None:
        """Drop an entry, keeping its reply as its view's stale stand-in (bounded
        by half the cache's bytes)."""
        body = self._entries[key][1]
        self._drop(key)
        view = key[2:]
        if view in self._stale:
            self._stale_bytes -= len(self._stale.pop(view))
        self._stale[view] = body
        self._stale_bytes += len(body)
        while self._stale_bytes > self.max_bytes // 2:
            self._stale_bytes -= len(self._stale.popitem(last=False)[1])

    def __len__(self) -> int:
        return len(self._entries)


class DataVersion:
    """`PRAGMA data_version` of the index, read on one held read-only connection: it
    changes whenever another connection commits, even two commits within one tick of
    the file clock that leave the WAL's size alone. The connection reopens when the
    index file is replaced (a new inode), and `epoch` counts the reopenings, since a
    new connection's numbers restart. Thread-safe."""

    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path
        self.epoch = 0
        self._conn: sqlite3.Connection | None = None
        self._inode = -1
        self._lock = threading.Lock()

    def read(self) -> tuple[int, int]:
        with self._lock:
            try:
                inode = os.stat(self.index_path).st_ino
            except OSError:
                self.close()
                return (self.epoch, -1)
            try:
                if self._conn is None or inode != self._inode:
                    self.close()
                    uri = self.index_path.resolve().as_uri() + "?mode=ro"
                    self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                    self._inode = inode
                    self.epoch += 1
                row = self._conn.execute("PRAGMA data_version").fetchone()
                return (self.epoch, int(row[0]))
            except sqlite3.Error:
                self.close()
                return (self.epoch, -1)

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
            self.epoch += 1


def generation(index_path: Path, watch: DataVersion | None = None) -> tuple[int, ...]:
    """A key that changes whenever the index does: the size and modification time of
    index.sqlite and its WAL (writes land there until a checkpoint), and, with
    `watch`, the index's data_version on a held connection. A scan that writes
    nothing leaves it alone."""
    out: list[int] = []
    for p in (index_path, index_path.with_name(index_path.name + "-wal")):
        try:
            st = os.stat(p)
        except OSError:
            st = None
        # an empty WAL is no write: the first reader creates one, and a checkpoint
        # that empties it has already changed the main file
        if st is None or st.st_size == 0:
            out += [-1, -1]
        else:
            out += [st.st_size, st.st_mtime_ns]
    if watch is not None:
        out += watch.read()
    return tuple(out)


def _limits_stamp(state_dir: Path | None) -> tuple[int, int]:
    """Size and modification time of the usage samples."""
    if state_dir is None:
        return (-1, -1)
    try:
        st = os.stat(samples_path(state_dir))
    except OSError:
        return (-1, -1)
    return (st.st_size, st.st_mtime_ns)


# the endpoints whose replies read the usage samples (their limits cards)
SAMPLED = frozenset({"limits", "overview"})

# the query parameters each endpoint reads: the cache key holds only these
PARAMS: dict[str, tuple[str, ...]] = {
    "overview": (),
    "spend": ("days", "group"),
    "sessions": ("days", "limit", "sort", "q"),
    "session": (),
    "day": (),
    "spikes": ("days", "spike", "project", "cause"),
    "checks": ("days",),
    "limits": ("days",),
}


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
    state_dir: Path | None = None  # where the usage samples are; None: no samples
    scanner: Scanner | None = None
    cache: ApiCache | None = field(default_factory=ApiCache)  # None: no caching
    jobs: Jobs | None = field(default_factory=Jobs)  # None: every reply computed in the request
    patience: float = PATIENCE  # seconds a request waits for its job before a 202
    warmer: Warmer | None = None


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

    @property
    def first_scan(self) -> bool:
        """True while the first scan has not finished: the index may be partial."""
        return self._thread.is_alive() and self.scans == 0 and not self._stop.is_set()

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


# The views the warmer may compute ahead of the clicks, with the query the page
# sends: each page's default request first. Only the defaults and the views a page
# opened lately are warmed (`Warmer.order`); the other tabs are listed for tests
# and for a warmer given more room.
WARM_VIEWS: dict[str, list[dict[str, str]]] = {
    "overview": [{}],
    "spikes": [{"days": d} for d in ("14", "1", "7", "30", "90")],
    "spend": [{"days": d, "group": "category"} for d in ("14", "7", "30", "90")],
    "sessions": [{"days": d, "sort": "usd", "limit": "300"} for d in ("14", "1", "7", "30", "0")],
    "checks": [{"days": d} for d in ("14", "7", "30")],
    "limits": [{"days": d} for d in ("14", "7", "30", "90")],
}
WARM_IDLE = 1800.0  # seconds without a page request after which the warmer rests
WARM_REST = 3.0  # after each reply, rest this many times as long as it took (~1/4 core)
WARM_RECENT = 1800.0  # a view a page opened this many seconds ago is still warmed


class Warmer:
    """One low-priority thread that fills the cache with the pages' usual views
    after startup and whenever the index's generation (or the date) changes: the
    views a page asked for in the last `recent` seconds, newest first, then every
    page's default view. Its jobs run as background jobs (`capcom.report.jobs`),
    so a page's request never waits for it; a new generation stops its job and
    starts the round over. It rests `rest` times as long as each reply took, so
    it keeps about 1 / (1 + rest) of a core busy at most, and stops warming when
    no page asked for anything in `idle` seconds."""

    def __init__(
        self,
        site: Site,
        views: dict[str, list[dict[str, str]]] | None = None,
        rest: float = WARM_REST,
        idle: float = WARM_IDLE,
        tick: float = 2.0,
        recent: float = WARM_RECENT,
    ) -> None:
        self.site = site
        self.views = WARM_VIEWS if views is None else views
        self.rest = rest
        self.idle = idle
        self.tick = tick
        self.recent = recent
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="capcom-warm", daemon=True)
        # the views pages asked for: (name, sorted query) -> when, newest last
        self._opened: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._seen = time.monotonic()
        self._warm: tuple[Any, ...] | None = None  # the (generation, date) last warmed
        self.computed = 0  # replies the warmer computed

    def saw(self, name: str, query: dict[str, str]) -> None:
        """A page asked for `name` with `query`: that view is warmed first for the
        next `recent` seconds."""
        now = time.monotonic()
        self._seen = now
        if name in self.views:
            tag = (name, tuple(sorted(query.items())))
            self._opened.pop(tag, None)
            self._opened[tag] = now
            for old, when in list(self._opened.items()):
                if now - when > self.recent:
                    del self._opened[old]

    def order(self) -> list[tuple[str, dict[str, str]]]:
        """The views to keep warm: the ones opened lately, newest first, then each
        page's default view."""
        now = time.monotonic()
        opened = [tag for tag, when in reversed(self._opened.items()) if now - when <= self.recent]
        defaults = [(n, tuple(sorted(vs[0].items()))) for n, vs in self.views.items() if vs]
        out: list[tuple[str, dict[str, str]]] = []
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for tag in [*opened, *defaults]:
            if tag not in seen:
                seen.add(tag)
                out.append((tag[0], dict(tag[1])))
        return out

    def warm_round(self) -> bool:
        """Compute the views not cached yet; True when the round got through."""
        site = self.site
        if site.cache is None or site.jobs is None:
            return True
        now = site.clock()
        mark = cache_key(site, "overview", ["overview"], {}, now)[:2]
        if mark == self._warm or time.monotonic() - self._seen > self.idle:
            return True
        for name, query in self.order():
            if self._stop.is_set():
                return False
            now = site.clock()
            key = cache_key(site, name, [name], query, now)
            if key[:2] != mark:
                return False  # the index changed: start over
            if site.cache.get(key, now.timestamp()) is not None:
                continue
            site.jobs.set_gen(key[0])
            work = _work(site, name, [name], query, now, key)
            t0 = time.monotonic()
            job = site.jobs.submit(key, key[0], work, (name, key[4]), background=True)
            job.done.wait()
            if job.cancelled is not None:
                return False
            self.computed += 1
            if self._stop.wait(min(30.0, (time.monotonic() - t0) * self.rest)):
                return False
        self._warm = mark
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.warm_round()
            except Exception:
                print("capcom serve: warming the cache failed:", file=sys.stderr)
                traceback.print_exc()
            self._stop.wait(self.tick)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)


class FailuresOnly(io.StringIO):
    """A progress stream for `scan_all` that passes on only the lines about files
    that failed, so the background scan stays quiet otherwise."""

    def __init__(self, out: TextIO | None = None) -> None:
        super().__init__()
        self._out = out
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text.replace("\r", "\n")
        *lines, self._pending = self._pending.split("\n")
        out = self._out or sys.stderr
        for line in lines:
            if ": failed on " in line:
                print("capcom serve: " + line.removeprefix("capcom scan: "), file=out, flush=True)
        return len(text)


def background_scan(config: Config, out: TextIO | None = None) -> object:
    """One incremental scan, as `run_scan` does it, reporting only the failed files."""
    from capcom.store.index import check_index_path
    from capcom.store.scan import scan_all

    check_index_path(config.index_path, (config.claude_root, config.openclaw_root))
    source = importlib.import_module("capcom.sources.claude_code").ClaudeCodeSource()
    with open_index(config.index_path) as index:
        return scan_all(index, source, config, FailuresOnly(out))


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
        if parts[0] not in PAGES or len(parts) > (2 if parts[0] in WITH_ARG else 1):
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
        if name not in ENDPOINTS or len(parts) != (2 if name in WITH_ARG else 1):
            self._error(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")
            return
        site = self.site
        job_id = query.pop("job", None)
        now = site.clock()
        if site.cache is None or name in UNCACHED:
            result = compute(site, name, parts, query, now)
            self._send(result.status, result.body, _ctype(result))
            return
        key = cache_key(site, name, parts, query, now)
        if site.warmer is not None:
            site.warmer.saw(name, query)
        hit = site.cache.get(key, now.timestamp())
        if hit is not None:
            self._send(HTTPStatus.OK, hit, TYPES[".json"])
            return
        jobs = site.jobs
        if jobs is None:
            result = compute(site, name, parts, query, now, key)
            self._send(result.status, result.body, _ctype(result))
            return
        jobs.set_gen(key[0])
        job = jobs.by_id(job_id) if job_id else None
        if job is None or job.key != key or (job.cancelled and job.result is None):
            job = jobs.find(key)
        if job is None:
            job = jobs.submit(
                key, key[0], _work(site, name, parts, query, now, key), (name, key[4])
            )
        else:
            job.poll()
        if not job_id:
            job.done.wait(site.patience)
        if job.result is not None:
            self._send(job.result.status, job.result.body, _ctype(job.result))
        elif job.timed_out() or job.cancelled == "timeout":
            job.cancelled = "timeout"
            self._error(
                HTTPStatus.GATEWAY_TIMEOUT, "timeout", "This reply took too long to compute."
            )
        else:
            partial, source = job.partial, "chunks"
            if partial is None:
                partial, source = site.cache.stale(key), "stale"
            body = computing_body(job, partial, source if partial is not None else None)
            self._send(HTTPStatus.ACCEPTED, body, TYPES[".json"])


def _ctype(result: Result) -> str:
    return TYPES[".json"] if result.body[:1] in (b"{", b"[") else "text/plain"


def cache_key(
    site: Site, name: str, parts: list[str], query: dict[str, str], now: datetime
) -> tuple[Any, ...]:
    """The cache's and the jobs' key of a request: the index generation, the local
    date, the endpoint, its path argument and the query parameters it reads. The
    usage samples, which the recorder writes without a scan (every status line
    refresh), count only for the replies that read them (`SAMPLED`): in the
    generation they would turn every page's cached reply stale whenever a session
    is open."""
    assert site.cache is not None
    gen = generation(site.index_path, site.cache.watch(site.index_path))
    today = now.astimezone(site.tz).date().isoformat()  # the windows start at midnight
    args: tuple[Any, ...] = tuple((k, query[k]) for k in PARAMS.get(name, ()) if k in query)
    if name in SAMPLED:
        args += (("samples", _limits_stamp(site.state_dir)),)
    return (gen, today, name, tuple(parts[1:]), args)


def _work(
    site: Site,
    name: str,
    parts: list[str],
    query: dict[str, str],
    now: datetime,
    key: tuple[Any, ...],
) -> Callable[[Job], Result]:
    return lambda job: compute(site, name, parts, query, now, key, job.step)


def _encode(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def _err(status: int, code: str, message: str, **extra: Any) -> Result:
    return Result(status, _encode({"error": code, "message": message} | extra), cacheable=False)


def compute(
    site: Site,
    name: str,
    parts: list[str],
    query: dict[str, str],
    now: datetime,
    key: tuple[Any, ...] | None = None,
    step: Callable[[float, Callable[[], Any] | None], None] | None = None,
) -> Result:
    """One API reply, computed now (in the request's thread or in a job's), and
    cached under `key` when it can be."""
    try:
        index = _open(site)
    except IndexMissing as err:
        scanning = site.scanner is not None and site.scanner.running
        action = SCANNING if scanning else "Run `capcom scan` first."
        return _err(
            HTTPStatus.SERVICE_UNAVAILABLE, "no_index", f"{err} {action}", scanning=scanning
        )
    try:
        ctx = api.Context(
            index=index,
            table=site.table,
            analysis=site.analysis,
            now=now,
            tz=site.tz,
            index_path=site.index_path,
            openclaw_root=site.openclaw_root,
            state_dir=site.state_dir,
            scanning=site.scanner is not None and site.scanner.first_scan,
            step=step,
        )
        payload = ENDPOINTS[name](ctx, parts[1] if len(parts) == 2 else "", query)
        body = _encode(payload)
        if key is not None and site.cache is not None and not ctx.scanning:
            site.cache.put(key, now.timestamp(), body)
        return Result(HTTPStatus.OK, body, cacheable=not ctx.scanning)
    except api.ApiError as err:
        return _err(err.status, err.code, err.message)
    except Unavailable as err:
        return _err(
            HTTPStatus.NOT_IMPLEMENTED,
            "unavailable",
            f"Coming in milestone {err.milestone}.",
            module=err.module,
            milestone=err.milestone,
        )
    except sqlite3.Error:
        return _err(HTTPStatus.SERVICE_UNAVAILABLE, "index_busy", "The index could not be read.")
    finally:
        index.close()


Endpoint = Callable[[api.Context, str, dict[str, str]], Any]
ENDPOINTS: dict[str, Endpoint] = {
    "status": lambda c, _, q: api.status(c),
    "overview": lambda c, _, q: api.overview(c),
    "spend": lambda c, _, q: api.spend(c, q),
    "sessions": lambda c, _, q: api.sessions(c, q),
    "session": lambda c, sid, q: api.session(c, sid),
    "day": lambda c, d, q: api.day(c, d),
    "live": lambda c, _, q: api.live(c),
    "spikes": lambda c, _, q: api.spikes(c, q),
    "checks": lambda c, _, q: api.checks(c, q),
    "limits": lambda c, _, q: api.limits(c, q),
}


class _Server(ThreadingHTTPServer):
    # socketserver listens with a backlog of 5. A page load opens a connection per
    # module at once, and macOS resets the ones a full queue cannot hold; one reset
    # import keeps app.js from running and leaves the page blank.
    request_queue_size = 128
    daemon_threads = True


def make_server(site: Site, port: int) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1:`port` (0: any free port); not started."""
    handler = type("SiteHandler", (Handler,), {"site": site})
    return _Server((HOST, port), handler)


def run_serve(config: Config, port: int, open_browser: bool, scan: bool = True) -> int:
    adopt_samples(config)
    scanner = None
    if scan:
        scanner = Scanner(lambda: background_scan(config))
    site = Site(
        index_path=config.index_path,
        table=load_prices(),
        analysis=default_analysis(),
        openclaw_root=config.openclaw_root,
        state_dir=config.state_dir,
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
    site.warmer = Warmer(site)
    site.warmer.start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        site.warmer.stop(timeout=1)
        if scanner is not None:
            scanner.stop(timeout=5)
            if scanner.running:
                print(
                    "capcom serve: stopped in the middle of a scan. The index keeps what was "
                    "written so far; the next scan reads the rest.",
                    file=sys.stderr,
                )
    return 0
