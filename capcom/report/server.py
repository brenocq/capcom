"""`capcom serve`: the local website.

Standard library only. The server binds to 127.0.0.1, sends no CORS headers, and
refuses requests whose Host header is not this machine (so a web page cannot reach it
through DNS rebinding). Each API request opens the index read-only and closes it when
done. Pages are static files from `static/`; the API is in `api.py` and `didit.py`,
documented in `API.md`.

Everything is GET but two POSTs. `POST /api/changes`, the one write, marks a finding
as applied (or edits or unmarks a mark) in capcom.changes' own file. It needs the
per-launch token the pages carry in a meta tag, an Origin (or Referer) of this
server, a JSON body of at most 4 KB, and a body that passes `didit.write`'s schema;
`--read-only` turns it off (405). `POST /api/ask` (`ask.py`) streams an answer from
the user's own `claude` CLI about a part of a page, behind the same checks with a
body of at most 20 KB, two at a time, with a timeout, its child killed by PID when
the page goes away; `--no-ask` turns it off (405). It writes nothing, so it stays on
under `--read-only`.

API replies are cached per index generation (`ApiCache`): the key holds the index
files' sizes and modification times and the index's `PRAGMA data_version` on a held
read connection, so any write invalidates every entry and a scan that wrote nothing
none, plus only the query parameters the endpoint reads. Under `capcom serve` the
generation, once it moved, holds a minute (`ApiCache.current`): the scan writes
every few seconds while a session runs, and the pages need not follow each write.
Entries also expire after a few minutes, since some numbers move with the clock
alone, and the cache is bounded in bytes.

The index stays fresh through a `Scanner`: an incremental scan at start and then
every few seconds, in one background thread, so a session shows in "Live now" soon
after it writes. With nothing new, a scan is one stat per log and two indexed
lookups, a few milliseconds, and it writes nothing. Only that thread writes the
index (SQLite's WAL lets the handlers read while it does), and a lock makes sure two
scans never overlap.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib
import io
import json
import math
import os
import secrets
import select
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from collections.abc import Callable, Sequence
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
from capcom.limits.record import ACCOUNTS_FILE, SESSIONS_FILE
from capcom.report import api, ask, didit
from capcom.report.analysis import Analysis, Unavailable, default_analysis
from capcom.report.jobs import Job, Jobs, Result, computing_body
from capcom.report.limit_window import limit_window
from capcom.report.worker import Worker
from capcom.store.index import open_index
from capcom.types import GroupBy, PriceTable

HOST = "127.0.0.1"
STATIC = Path(__file__).parent / "static"
PAGES = {"", "spend", "sessions", "session", "day", "spikes", "checks", "changes", "limits"}
WITH_ARG = {"session", "day"}  # pages and endpoints that take one path segment
MAY_ARG = {"changes"}  # pages and endpoints with or without one
MAX_BODY = 4096  # bytes: the one POST's JSON body
TOKEN_HEADER = "X-Capcom-Token"
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
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
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
SCAN_INTERVAL = 3.0  # seconds from the end of one background scan to the next
SCAN_REST = 4.0  # ...or this many times as long as that scan took, when longer
GEN_HOLD = 60.0  # serve: seconds the pages' index generation holds after it moved
POLL_WAIT = 0.5  # seconds a poll waits for a newer partial reply or the end
LEAVE_TICK = 0.05  # seconds between two looks, while a poll waits, for its page gone
PRIORITY_HEADER = "X-Capcom-Priority"  # "low": a prefetch, run as background work
MARKED = {"checks", "changes"}  # replies that show marks: keyed on the marks file too
DAY_CACHE = "changes-days.sqlite"  # in the cache folder: the file `capcom changes` keeps


class ApiCache:
    """Encoded API replies by (generation, endpoint, arguments), least recently used
    first out once they pass `max_bytes`; an entry older than `ttl` seconds of the
    site's clock is computed again. Thread-safe."""

    def __init__(
        self, max_bytes: int = CACHE_BYTES, ttl: float = CACHE_TTL, hold: float = 0.0
    ) -> None:
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.hold = hold
        self._entries: OrderedDict[tuple[Any, ...], tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self._watch: DataVersion | None = None
        self._shown: tuple[int, ...] = ()  # the generation `current` gives
        self._shown_at = -math.inf  # when it moved there (time.monotonic)
        # the last reply of each view (the key without its generation and date),
        # kept after its generation ended, with when it was computed: shown, marked
        # stale, while the new one is computed
        self._stale: OrderedDict[tuple[Any, ...], tuple[float, bytes]] = OrderedDict()
        self._stale_bytes = 0

    def current(self, index_path: Path, now: float | None = None) -> tuple[int, ...]:
        """The index generation the cached replies and /api/version go by: the one
        `generation` gives, except that once it moved it stays `hold` seconds. The
        background scan writes every few seconds while a session runs, and each
        move turns every cached reply stale (open pages fetch their view again, the
        warmer starts over); held, that happens at most once per `hold`, and a first
        write after a quiet `hold` still shows at once. Live now reads uncached."""
        gen = generation(index_path, self.watch(index_path))
        t = time.monotonic() if now is None else now
        with self._lock:
            if gen != self._shown and t - self._shown_at >= self.hold:
                self._shown, self._shown_at = gen, t
            return self._shown

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

    def entry(self, key: tuple[Any, ...], now: float) -> tuple[float, bytes] | None:
        """When `key`'s reply was computed and the reply, if it is fresh; neither
        counted as a hit or a miss nor moved up (a look for a reply to build on)."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or now - entry[0] > self.ttl or now < entry[0]:
                return None
            return entry

    def put(self, key: tuple[Any, ...], now: float, body: bytes) -> None:
        if len(body) > self.max_bytes // 4:
            return  # one reply never crowds out the rest
        with self._lock:
            if key in self._entries:
                self._drop(key)
            others = [k for k in self._entries if k[0] != key[0]]
            if any(self._entries[k][0] > now for k in others):
                # asked for before a reply of a newer generation was (a job that
                # began before the index changed): already outdated, it only stands
                # in for its view, and the newer generation's replies stay (a
                # group switch is made from them, server.py REGROUP)
                self._keep_stale(key[2:], now, body)
                return
            # entries of an older generation are dead: drop them first
            for old in others:
                self._retire(old)
            self._entries[key] = (now, body)
            self._bytes += len(body)
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def stale(self, key: tuple[Any, ...]) -> bytes | None:
        """The newest reply of `key`'s view from an earlier generation or day, if
        kept: the one computed last, so a stand-in never shows older totals than
        another one would."""
        with self._lock:
            view = key[2:]
            best: tuple[float, bytes] | None = None
            # an entry of an older generation is retired only when a newer one is put
            for k in reversed(self._entries):
                if k[2:] == view and k != key and (best is None or self._entries[k][0] > best[0]):
                    best = self._entries[k]
            kept = self._stale.get(view)
            if kept is not None and (best is None or kept[0] > best[0]):
                best = kept
            return best[1] if best is not None else None

    def _drop(self, key: tuple[Any, ...]) -> None:
        _, body = self._entries.pop(key)
        self._bytes -= len(body)

    def _retire(self, key: tuple[Any, ...]) -> None:
        """Drop an entry, keeping its reply as its view's stale stand-in (bounded
        by half the cache's bytes)."""
        stamp, body = self._entries[key]
        self._drop(key)
        self._keep_stale(key[2:], stamp, body)

    def _keep_stale(self, view: tuple[Any, ...], stamp: float, body: bytes) -> None:
        """Keep `body` as `view`'s stale stand-in, unless the one kept is newer."""
        kept = self._stale.get(view)
        if kept is not None:
            if kept[0] > stamp:
                return
            self._stale_bytes -= len(self._stale.pop(view)[1])
        self._stale[view] = (stamp, body)
        self._stale_bytes += len(body)
        while self._stale_bytes > self.max_bytes // 2:
            self._stale_bytes -= len(self._stale.popitem(last=False)[1][1])

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


def _changes_stamp(site: Site) -> tuple[int, int]:
    """Size and modification time of the marks (changes.jsonl): a mark changes Checks
    and Did it work? without a scan."""
    try:
        path = site.changes_path or Path(site.analysis.changes().store_path())
        st = os.stat(path)
    except (OSError, ValueError, Unavailable):  # ValueError: a path it refuses
        return (-1, -1)
    return (st.st_size, st.st_mtime_ns)


def _limits_stamp(state_dir: Path | None) -> tuple[int, int]:
    """Size and modification time of the usage samples, plus those of the accounts
    the recorder keeps beside them (a switch changes what the cards show)."""
    if state_dir is None:
        return (-1, -1)
    try:
        st = os.stat(samples_path(state_dir))
    except OSError:
        return (-1, -1)
    size, mtime = st.st_size, st.st_mtime_ns
    for name in (ACCOUNTS_FILE, SESSIONS_FILE):
        with contextlib.suppress(OSError):
            extra = os.stat(state_dir / name)
            size, mtime = size + extra.st_size, max(mtime, extra.st_mtime_ns)
    return (size, mtime)


# the endpoints whose replies read the usage samples (their limits cards)
SAMPLED = frozenset({"limits", "overview", "limit-window"})

# the query parameters each endpoint reads: the cache key holds only these
PARAMS: dict[str, tuple[str, ...]] = {
    "overview": ("group",),
    "spend": ("days", "group"),
    "sessions": ("days", "limit", "sort", "q"),
    "session": (),
    "day": (),
    "hour": ("start",),
    "spikes": ("days", "spike", "project", "cause"),
    "checks": ("days",),
    "limits": ("days",),
    "limit-window": ("account", "window", "start"),
    "changes": ("days",),
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
    worker: Worker | None = None  # the warmer's process; None: it computes in a thread
    poll_wait: float = POLL_WAIT  # seconds a poll waits for news of its job
    changes_path: Path | None = None  # the marks; None: capcom.changes' own file
    read_only: bool = False  # True: no POST /api/changes, no buttons that write
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))  # per launch
    launch: str = field(default_factory=lambda: secrets.token_hex(6))  # not the token
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    day_cache: Path | None = None  # capcom.changes' finished days; None: not kept
    agent_dirs: Sequence[Path] | None = None  # where noticed() reads; None: its own
    request_timeout: float = 30.0  # seconds a client may take to send its request
    ask_on: bool = True  # False (--no-ask): no POST /api/ask, no Ask Claude in the pages
    ask_slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(ask.SLOTS)
    )
    ask_timeout: float = ask.TIMEOUT  # seconds one answer may take


class Scanner:
    """Incremental scans in one background thread: one at start, then the next
    `pause(took)` seconds after the previous one ends: `interval`, or `rest` times as
    long as that scan took when that is longer, so a slow scan (the first, a large
    catch-up) never keeps a core busy. `scan_once` returns False without scanning
    when a scan is already running, so two never overlap."""

    def __init__(
        self,
        scan: Callable[[], object],
        interval: float = SCAN_INTERVAL,
        name: str = "capcom-scan",
        rest: float = SCAN_REST,
    ) -> None:
        self._scan = scan
        self.interval = interval
        self.rest = rest
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

    def pause(self, took: float) -> float:
        """Seconds to wait after a scan that took `took` seconds."""
        return max(self.interval, self.rest * took)

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.scan_once()
            self._stop.wait(self.pause(time.monotonic() - t0))

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
# the pages whose default view follows the group picked last (their shared "Split
# by" switch): the warmer warms those defaults with the group the pages ask for
GROUPED = frozenset({"overview", "spend"})
WARM_IDLE = 1800.0  # seconds without a page request after which the warmer rests
WARM_REST = 3.0  # after each reply, rest this many times as long as it took (~1/4 core)
WARM_RECENT = 1800.0  # a view a page opened this many seconds ago is still warmed


class Warmer:
    """One low-priority thread that fills the cache with the pages' usual views
    after startup and whenever the index's generation (or the date) changes: the
    views a page asked for in the last `recent` seconds, newest first, then every
    page's default view (Overview's and Spend's split by the group a page picked
    last, as the pages ask for it). Its jobs run as background jobs (`capcom.report.jobs`),
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
        self.group: str | None = None  # the group Overview or Spend asked for last

    def saw(self, name: str, query: dict[str, str]) -> None:
        """A page asked for `name` with `query`: that view is warmed first for the
        next `recent` seconds."""
        now = time.monotonic()
        self._seen = now
        group = query.get("group") or "category"
        if name in GROUPED and group in {g.value for g in GroupBy} and group != self.group:
            self.group = group
            self._warm = None  # the defaults changed: warm them again
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
        defaults = [
            (n, tuple(sorted(self._default(n, vs[0]).items())))
            for n, vs in self.views.items()
            if vs
        ]
        out: list[tuple[str, dict[str, str]]] = []
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for tag in [*opened, *defaults]:
            if tag not in seen:
                seen.add(tag)
                out.append((tag[0], dict(tag[1])))
        return out

    def _default(self, name: str, query: dict[str, str]) -> dict[str, str]:
        """A page's default view, split by the group picked last when it has the
        switch (Overview leaves `group` out for the default, by category)."""
        if self.group is None or name not in GROUPED:
            return query
        if "group" in query or self.group != "category":
            return {**query, "group": self.group}
        return query

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
            work = _work(site, name, [name], query, now, key, background=True)
            t0 = time.monotonic()
            job = site.jobs.submit(key, key[0], work, (name, key[4]), background=True)
            job.done.wait()
            if job.cancelled is not None:
                return False
            self.computed += 1
            spent = time.monotonic() - t0 - job.paused
            if self._stop.wait(min(30.0, max(0.0, spent) * self.rest)):
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
            elif path == "/favicon.ico":  # browsers ask for it before reading the page
                self._static("favicon.svg")
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

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _same_origin(self) -> bool:
        """The request comes from a page of this server: its Origin, or its Referer
        when a browser sends no Origin, is http://127.0.0.1:<port> or localhost."""
        port = getattr(self.server, "server_port", 0)
        own = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin.lower() in own
        referer = self.headers.get("Referer")
        if not referer:
            return False
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}".lower() in own

    def do_POST(self) -> None:
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # TimeoutError: a body slower than request_timeout; the socket closes
        except Exception:
            print("capcom serve: error while answering a request:", file=sys.stderr)
            traceback.print_exc()
            with contextlib.suppress(OSError):
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"internal error", "text/plain")

    def _post(self) -> None:
        """The one write, POST /api/changes, and POST /api/ask (see the module's
        docstring)."""
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
            return
        route = unquote(urlsplit(self.path).path).rstrip("/")
        if route == "/api/ask":
            self._ask()
            return
        if route != "/api/changes":
            self._not_allowed()
            return
        site = self.site
        if site.read_only:
            self._not_allowed()
            return
        body = self._checked_body(MAX_BODY)
        if body is _REFUSED:
            return
        try:
            index = _open(site)
        except IndexMissing as err:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "no_index", str(err))
            return
        try:
            now = site.clock()
            ctx = self._context(index, now)
            payload = didit.write(ctx, body, lock=site.write_lock, known=self._known(now))
            self._json(HTTPStatus.OK, payload)
        except api.ApiError as err:
            self._error(err.status, err.code, err.message)
        except Unavailable:
            self._error(HTTPStatus.NOT_IMPLEMENTED, "unavailable", "capcom.changes is missing.")
        except KeyError:  # unmarked meanwhile
            self._error(HTTPStatus.NOT_FOUND, "not_found", "no such change")
        except ValueError as err:  # capcom.changes refused it
            self._error(HTTPStatus.BAD_REQUEST, "bad_request", str(err))
        except sqlite3.Error:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE, "index_busy", "The index could not be read."
            )
        finally:
            index.close()

    def _checked_body(self, limit: int) -> Any:
        """A POST's JSON body, once it passed the checks every POST does: an Origin
        of this server, this launch's token, a JSON type, a Content-Length of at
        most `limit` bytes. `_REFUSED` (the reply sent) when one fails."""
        site = self.site
        if not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "cross_origin", "Only capcom's own pages can write.")
            return _REFUSED
        token = self.headers.get(TOKEN_HEADER) or ""
        if not hmac.compare_digest(token.encode(), site.token.encode()):
            self._error(
                HTTPStatus.FORBIDDEN,
                "bad_token",
                "The page's write token is not this launch's: reload the page.",
            )
            return _REFUSED
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "bad_type", "The body must be application/json."
            )
            return _REFUSED
        raw_length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or raw_length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "no_length", "Content-Length is required.")
            return _REFUSED
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", f"At most {limit} bytes.")
            return _REFUSED
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "bad_json", "The body is not JSON.")
            return _REFUSED

    def _ask(self) -> None:
        """POST /api/ask: the answer of the user's `claude` CLI, streamed as JSON
        lines (ask.py). The CLI runs only here, when a page asks."""
        site = self.site
        if not site.ask_on:
            self._not_allowed()
            return
        body = self._checked_body(ask.MAX_BODY)
        if body is _REFUSED:
            return
        try:
            prompt = ask.build_prompt(ask.parse(body))
        except ask.AskError as err:
            self._error(err.status, err.code, err.message)
            return
        claude = ask.find_claude()
        if claude is None:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "no_claude",
                "capcom could not find the claude command. Install Claude Code, or start "
                "capcom serve where claude is on the PATH.",
            )
            return
        if not site.ask_slots.acquire(blocking=False):
            self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "busy",
                f"Claude is answering {ask.SLOTS} questions already: wait for one to finish.",
            )
            return
        child = None
        try:
            try:
                child = ask.start(claude, prompt)
            except OSError:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "no_claude", "The claude command did not start."
                )
                return
            self.close_connection = True  # the reply ends when the connection closes
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Connection", "close")
            self.end_headers()
            for line in ask.stream(child, self._gone, site.ask_timeout):
                self.wfile.write(json.dumps(line, separators=(",", ":")).encode() + b"\n")
                self.wfile.flush()
        finally:
            if child is not None:
                child.close()  # killed by its PID if it still runs: the page went away
            site.ask_slots.release()

    def _known(self, now: datetime) -> Callable[[int], list[didit.Known] | None]:
        """The findings of a Checks window from its cached reply, if there is one."""

        def known(days: int) -> list[didit.Known] | None:
            site = self.site
            if site.cache is None:
                return None
            queries: list[dict[str, str]] = [{"days": str(days)}] + ([{}] if days == 14 else [])
            for query in queries:
                key = cache_key(site, "checks", ["checks"], query, now)
                body = site.cache.get(key, now.timestamp())
                if body is not None:
                    rows = json.loads(body)["findings"]
                    return [
                        (r["kind"], r["scope"], r["title"], float(r["saving_usd_per_week"]))
                        for r in rows
                    ]
            return None

        return known

    def _context(self, index: Any, now: datetime) -> api.Context:
        return context(self.site, index, now)

    def _page(self, path: str) -> None:
        parts = path.strip("/").split("/")
        window = len(parts) == 5 and parts[:2] == ["limits", "window"]  # a usage window
        if not window and (
            parts[0] not in PAGES or len(parts) > (2 if parts[0] in WITH_ARG | MAY_ARG else 1)
        ):
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        try:
            _open(self.site).close()
        except IndexMissing as err:
            scanning = self.site.scanner is not None and self.site.scanner.running
            page = NO_INDEX_PAGE.format(reason=err, action=SCANNING if scanning else RUN_SCAN)
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, page.encode(), TYPES[".html"])
            return
        self._send(HTTPStatus.OK, self._index_html(), TYPES[".html"])

    def _index_html(self) -> bytes:
        """index.html with this launch's write token (and read-only flag) in meta tags:
        another site's page cannot read it, so it cannot write."""
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        metas = f'<meta name="capcom-token" content="{self.site.token}">\n'
        if self.site.read_only:
            metas += '<meta name="capcom-read-only" content="1">\n'
        if not self.site.ask_on:
            metas += '<meta name="capcom-ask" content="off">\n'
        return html.replace("</head>", metas + "</head>", 1).encode()

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
        if route.strip("/") == "version":  # the pages' 10 s look for news: never a job
            self._json(HTTPStatus.OK, version(self.site))
            return
        if name in MAY_ARG and len(parts) == 2 and not parts[1]:
            parts = parts[:1]
        sizes = {1, 2} if name in MAY_ARG else {2} if name in WITH_ARG else {1}
        if name not in ENDPOINTS or len(parts) not in sizes:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")
            return
        site = self.site
        low = (self.headers.get(PRIORITY_HEADER) or "").lower() == "low"
        if site.jobs is None or low:
            self._reply(name, parts, query, low)
            return
        site.jobs.user_begin()  # background work waits while a page's request is in flight
        try:
            self._reply(name, parts, query, low)
        finally:
            site.jobs.user_end()

    def _reply(self, name: str, parts: list[str], query: dict[str, str], low: bool) -> None:
        site = self.site
        job_id = query.pop("job", None)
        now = site.clock()
        if site.cache is None or name in UNCACHED:
            result = compute(site, name, parts, query, now)
            self._send(result.status, result.body, _ctype(result))
            return
        key = cache_key(site, name, parts, query, now)
        if site.warmer is not None and not low:
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
        if job is None or not _same_view(job.key, key) or (job.cancelled and job.result is None):
            job = jobs.find(key)
        if job is None:
            work = _work(site, name, parts, query, now, key, background=low)
            job = jobs.submit(key, key[0], work, (name, key[4]), background=low)
        elif not low:
            job.poll()
        if not job_id:
            job.done.wait(site.patience)
        elif not self._wait_news(job, site.poll_wait):
            job.leave()  # its page stopped following it: the page clicked goes first
            self.close_connection = True
            return
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

    def _wait_news(self, job: Job, timeout: float) -> bool:
        """A poll's wait: until `job` ends or has a newer partial reply, at most
        `timeout` seconds, looking every LEAVE_TICK seconds whether the page closed
        the connection (it aborts its poll when it navigates away); False if so."""
        version = job.version
        end = time.monotonic() + timeout
        while job.version == version and not job.done.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            job.wait_news(version, min(LEAVE_TICK, left))
            if self._gone():
                return False
        return True

    def _gone(self) -> bool:
        """The client closed the connection: it is readable with nothing to read (a
        page sends nothing more while it waits for a reply)."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True


def version(site: Site) -> dict[str, Any]:
    """What a page compares every 10 s to know its data changed (`/api/version`):
    short digests of the index's generation, the usage samples' stamp and the marks'
    stamp, when the index was last written, and this launch's id (a restart changes
    it, so an open page knows to fetch the new write token). A few stat calls and one
    PRAGMA on a held connection: no index is opened, nothing is computed."""
    if site.cache is not None:
        gen = site.cache.current(site.index_path)
    else:
        gen = generation(site.index_path)

    def digest(x: Any) -> str:
        return hashlib.blake2s(repr(x).encode(), digest_size=6).hexdigest()

    updated = None
    mtimes = []
    for p in (site.index_path, site.index_path.with_name(site.index_path.name + "-wal")):
        with contextlib.suppress(OSError):
            mtimes.append(os.stat(p).st_mtime)
    if mtimes:
        when = datetime.fromtimestamp(max(mtimes), UTC)
        updated = when.astimezone(site.tz).isoformat(timespec="seconds")
    return {
        "index": digest(gen),
        "samples": digest(_limits_stamp(site.state_dir)),
        "marks": digest(_changes_stamp(site)),
        "updated": updated,
        "launch": site.launch,
    }


_REFUSED = object()  # _checked_body: a check failed and its reply is sent


def _same_view(a: Any, b: tuple[Any, ...]) -> bool:
    """Two cache keys of the same view, maybe of different index generations (or
    usage samples, or marks): a poll keeps following the job its page started."""
    if not isinstance(a, tuple) or len(a) != len(b):
        return False
    stamps = ("samples", "marks")
    args = [tuple(x for x in k[4] if x[0] not in stamps) for k in (a, b)]
    return a[1:4] == b[1:4] and args[0] == args[1]


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
    is open. The marks file (changes.jsonl) counts the same way for the replies
    that show marks (`MARKED`)."""
    assert site.cache is not None
    gen = site.cache.current(site.index_path)
    today = now.astimezone(site.tz).date().isoformat()  # the windows start at midnight
    args: tuple[Any, ...] = tuple((k, query[k]) for k in PARAMS.get(name, ()) if k in query)
    if name in SAMPLED:
        args += (("samples", _limits_stamp(site.state_dir)),)
    if name in MARKED:  # a mark writes without a scan
        args += (("marks", _changes_stamp(site)),)
    return (gen, today, name, tuple(parts[1:]), args)


def _work(
    site: Site,
    name: str,
    parts: list[str],
    query: dict[str, str],
    now: datetime,
    key: tuple[Any, ...],
    background: bool = False,
) -> Callable[[Job], Result]:
    """A job's work: compute the reply in the job's thread, or, for background
    work when the server has a worker process, there (and cache it here)."""
    worker = site.worker

    def here(job: Job) -> Result:
        return compute(site, name, parts, query, now, key, job.step)

    if not background or worker is None or _base(site, name, parts, query, now) is not None:
        return here  # (a reply made from another one is quick: no need for the worker)

    def there(job: Job) -> Result:
        result = worker.run(job, name, parts, query, now, lambda: here(job))
        first_scan = site.scanner is not None and site.scanner.first_scan
        ok = result.status == HTTPStatus.OK and result.cacheable and not first_scan
        if ok and site.cache is not None:
            site.cache.put(key, now.timestamp(), result.body)
        return result

    return there


def _encode(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def _err(status: int, code: str, message: str, **extra: Any) -> Result:
    return Result(status, _encode({"error": code, "message": message} | extra), cacheable=False)


def context(
    site: Site,
    index: Any,
    now: datetime,
    step: Callable[[float, Callable[[], Any] | None], None] | None = None,
) -> api.Context:
    """The API's context for one reply (or the one POST)."""
    return api.Context(
        index=index,
        table=site.table,
        analysis=site.analysis,
        now=now,
        tz=site.tz,
        index_path=site.index_path,
        openclaw_root=site.openclaw_root,
        state_dir=site.state_dir,
        scanning=site.scanner is not None and site.scanner.first_scan,
        changes_path=site.changes_path,
        day_cache=site.day_cache,
        agent_dirs=site.agent_dirs,
        watching=site.scanner is not None,
        step=step,
    )


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
        ctx = context(site, index, now, step)
        base = _base(site, name, parts, query, now)
        stamp = now.timestamp()
        if base is not None:
            # as old as the reply it is made from: it shows the same moment
            stamp, payload = base[0], REGROUP[name](ctx, query, json.loads(base[1]))
        else:
            payload = ENDPOINTS[name](ctx, parts[1] if len(parts) == 2 else "", query)
        body = _encode(payload)
        if key is not None and site.cache is not None and not ctx.scanning:
            site.cache.put(key, stamp, body)
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


# Replies made from the cached reply of the same view split by another group, at a
# fraction of the cost: only Overview's chart of today depends on its group, so a
# click on its group switch computes that chart, not the whole page again.
REGROUP: dict[str, Callable[[api.Context, dict[str, str], dict[str, Any]], Any]] = {
    "overview": api.overview_regroup,
}


def _base(
    site: Site, name: str, parts: list[str], query: dict[str, str], now: datetime
) -> tuple[float, bytes] | None:
    """A cached reply that `name`'s reply for `query` can be made from (`REGROUP`):
    the same view split by another group, at the same state of the index (and of
    the usage samples), with when it was computed. The newest one, when several
    are: the reply a page shows last, whose moment its switch keeps."""
    if name not in REGROUP or site.cache is None:
        return None
    key = cache_key(site, name, parts, query, now)
    others = [{}, *({**query, "group": g.value} for g in GroupBy)]
    best: tuple[float, bytes] | None = None
    for other in others:
        okey = cache_key(site, name, parts, other, now)
        if okey != key:
            entry = site.cache.entry(okey, now.timestamp())
            if entry is not None and (best is None or entry[0] > best[0]):
                best = entry
    return best


Endpoint = Callable[[api.Context, str, dict[str, str]], Any]
ENDPOINTS: dict[str, Endpoint] = {
    "status": lambda c, _, q: api.status(c),
    "overview": lambda c, _, q: api.overview(c, q),
    "spend": lambda c, _, q: api.spend(c, q),
    "sessions": lambda c, _, q: api.sessions(c, q),
    "session": lambda c, sid, q: api.session(c, sid),
    "day": lambda c, d, q: api.day(c, d),
    "hour": lambda c, _, q: api.hour(c, q),
    "live": lambda c, _, q: api.live(c),
    "spikes": lambda c, _, q: api.spikes(c, q),
    "checks": lambda c, _, q: api.checks(c, q),
    "limits": lambda c, _, q: api.limits(c, q),
    "limit-window": lambda c, _, q: limit_window(c, q),
    "changes": lambda c, arg, q: didit.route(c, arg, q),
}


class _Server(ThreadingHTTPServer):
    # socketserver listens with a backlog of 5. A page load opens a connection per
    # module at once, and macOS resets the ones a full queue cannot hold; one reset
    # import keeps app.js from running and leaves the page blank.
    request_queue_size = 128
    daemon_threads = True


def make_server(site: Site, port: int) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1:`port` (0: any free port); not started."""
    # `timeout`: a socket timeout, so a client that sends less than it announced (or
    # nothing) cannot hold a thread
    handler = type("SiteHandler", (Handler,), {"site": site, "timeout": site.request_timeout})
    return _Server((HOST, port), handler)


def run_serve(
    config: Config,
    port: int,
    open_browser: bool,
    scan: bool = True,
    read_only: bool = False,
    ask_claude: bool = True,
) -> int:
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
        cache=ApiCache(hold=GEN_HOLD),
        read_only=read_only,
        day_cache=config.cache_dir / DAY_CACHE,
        ask_on=ask_claude,
    )
    try:
        server = make_server(site, port)
    except OSError as err:
        print(f"capcom serve: cannot listen on {HOST}:{port} ({err})", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    fresh = f"rescanning every {SCAN_INTERVAL:g} s" if scanner else "no background scan"
    fresh += ", read-only" if read_only else ""
    fresh += "" if ask_claude else ", no Ask Claude"
    print(f"capcom: serving {url} (local only, {fresh}; Ctrl+C to stop)", flush=True)
    if scanner is not None:
        scanner.start()
    site.worker = Worker(site)
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
        site.worker.close()
        if scanner is not None:
            scanner.stop(timeout=5)
            if scanner.running:
                print(
                    "capcom serve: stopped in the middle of a scan. The index keeps what was "
                    "written so far; the next scan reads the rest.",
                    file=sys.stderr,
                )
    return 0
