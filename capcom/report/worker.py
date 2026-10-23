"""The warmer's process: `capcom serve` computes the replies it warms ahead of the
clicks here, in a child process, so background work never holds the server's GIL.
While a page's request or job is in flight the child is stopped outright
(SIGSTOP) and continued after (SIGCONT): a click never shares the CPU with the
warmer, however long the warmed reply's own loops run between progress points.

The parent (`Worker`) starts the child on first use with this server's settings,
sends it one request at a time as a JSON line, and reads frames back: a JSON
header line, then `n` bytes of body. The child sends "step" frames (progress and,
now and then, the reply so far) and one "done" frame (status, body). It reads the
index read-only, like the server, and caches nothing: the parent caches the reply.
It exits on end of input, and dies with the parent (PR_SET_PDEATHSIG), so a
stopped child never outlives a killed server. Linux sends that signal when the
thread that forked the child ends, not the process, so every child is forked
from one thread that lives as long as the server (`_spawn`), never from the job
thread that happened to need it first. A child that dies anyway is started
again, and the reply asked of it once more.

    python -m capcom.report.worker   # the child; spoken to over stdin and stdout
"""

from __future__ import annotations

import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from capcom.report.jobs import Cancelled, Job, Result

if TYPE_CHECKING:
    from capcom.report.server import Site

PARTIAL_EVERY = 0.25  # seconds between two replies so far sent by the child
TICK = 0.05  # seconds between two looks at whether the child must pause
PARENT_ENV = "CAPCOM_WORKER_PARENT"  # the server's pid, for the child to check


class _Spawner:
    """One daemon thread that forks every child, for the life of the process: the
    child's death signal is tied to the thread that forked it (prctl(2)), so a
    child forked from a job's thread was killed as soon as that job ended."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._todo: queue.SimpleQueue[tuple[list[str], dict[str, Any], threading.Event]]
        self._todo = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    def popen(self, argv: list[str]) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="capcom-worker-spawn", daemon=True
                )
                self._thread.start()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._todo.put((argv, box, done))
        done.wait()
        if "error" in box:
            raise box["error"]
        proc: subprocess.Popen[bytes] = box["proc"]
        return proc

    def _loop(self) -> None:
        env = os.environ | {PARENT_ENV: str(os.getpid())}
        while True:
            argv, box, done = self._todo.get()
            try:
                box["proc"] = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True, env=env
                )
            except Exception as err:  # (OSError, mostly) raised in the asking thread
                box["error"] = err
            finally:
                done.set()


_spawn = _Spawner()


def _spec(site: Site) -> dict[str, Any]:
    """What the child needs to build the same replies as the server."""

    def path(p: Path | None) -> str | None:
        return None if p is None else str(p)

    return {
        "index_path": str(site.index_path),
        "openclaw_root": path(site.openclaw_root),
        "state_dir": path(site.state_dir),
        "changes_path": path(site.changes_path),
        "day_cache": path(site.day_cache),
        "agent_dirs": None if site.agent_dirs is None else [str(p) for p in site.agent_dirs],
        "watching": site.scanner is not None,
        "tz": _tz_spec(site.tz),
    }


def _tz_spec(tz: tzinfo | None) -> str | float | None:
    """A ZoneInfo's name, a fixed zone's offset in seconds, or None (the machine's)."""
    if tz is None:
        return None
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    offset = tz.utcoffset(None)
    return None if offset is None else offset.total_seconds()


def _tz(spec: str | float | None) -> tzinfo | None:
    from zoneinfo import ZoneInfo

    if spec is None:
        return None
    if isinstance(spec, str):
        return ZoneInfo(spec)
    return timezone(timedelta(seconds=spec))


class Worker:
    """The parent's side: one child process, one reply at a time."""

    def __init__(self, site: Site, argv: list[str] | None = None) -> None:
        self.site = site
        self.argv = argv or [sys.executable, "-m", "capcom.report.worker"]
        self._lock = threading.Lock()  # one reply at a time
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = b""
        self.started = 0  # child processes started (tests)
        self.stopped = 0  # times the child was paused (tests)

    # ------------------------------------------------------------------ the child

    def _ensure(self) -> subprocess.Popen[bytes]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._proc = _spawn.popen(self.argv)
        self._buf = b""
        self.started += 1
        self._write({"spec": _spec(self.site)})
        return self._proc

    def _write(self, msg: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self._proc.stdin.flush()

    def _frame(self, timeout: float) -> tuple[dict[str, Any], bytes] | None:
        """The next frame from the child, or None after `timeout` seconds."""
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        end = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                head = json.loads(self._buf[:nl])
                n = int(head.get("n", 0))
                if len(self._buf) >= nl + 1 + n:
                    body = self._buf[nl + 1 : nl + 1 + n]
                    self._buf = self._buf[nl + 1 + n :]
                    return head, body
            left = end - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                return None
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise EOFError("the warmer's process ended")
            self._buf += chunk

    def _signal(self, sig: int) -> None:
        if self._proc is not None and self._proc.poll() is None:
            os.kill(self._proc.pid, sig)

    def kill(self) -> None:
        """Stop the child now (a cancelled reply, or the server's end)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for f in (proc.stdin, proc.stdout):
            if f is not None:
                f.close()

    close = kill

    # ------------------------------------------------------------------ a reply

    def run(
        self,
        job: Job,
        name: str,
        parts: list[str],
        query: dict[str, str],
        now: datetime,
        fallback: Callable[[], Result],
    ) -> Result:
        """Compute one reply in the child, as `job`'s work: its progress and the
        reply so far go to `job`; the child is stopped whenever `job` must yield.
        If a page wants this reply while the child is busy with another one, it is
        computed here (`fallback`) at once instead."""
        while not self._lock.acquire(timeout=TICK):
            job._check()
            if not job.low:
                return fallback()
        try:
            return self._run(job, name, parts, query, now)
        finally:
            self._lock.release()

    def _run(
        self, job: Job, name: str, parts: list[str], query: dict[str, str], now: datetime
    ) -> Result:
        try:
            return self._once(job, name, parts, query, now)
        except (OSError, EOFError, ValueError):
            pass  # the child died under it: ask a fresh one, once (a second death is raised)
        return self._once(job, name, parts, query, now)

    def _once(
        self, job: Job, name: str, parts: list[str], query: dict[str, str], now: datetime
    ) -> Result:
        """One attempt at the reply; a child that ended or garbled it is killed."""
        jobs = job.jobs
        try:
            self._ensure()
            self._write({"name": name, "parts": parts, "query": query, "now": now.isoformat()})
            paused_at: float | None = None
            while True:
                job._check()
                pause = jobs.must_yield(job)
                if pause and paused_at is None:
                    self._signal(signal.SIGSTOP)
                    self.stopped += 1
                    paused_at = time.monotonic()
                elif not pause and paused_at is not None:
                    self._signal(signal.SIGCONT)
                    job.paused += time.monotonic() - paused_at
                    paused_at = None
                frame = self._frame(TICK)
                if frame is None:
                    continue
                head, body = frame
                if head.get("t") == "step":
                    job.publish(float(head.get("p", 0.0)), body or None)
                elif head.get("t") == "done":
                    return Result(int(head["status"]), body, cacheable=bool(head["cacheable"]))
        except Cancelled:
            self.kill()  # its reply is no longer wanted: start a fresh child next time
            raise
        except (OSError, EOFError, ValueError):
            self.kill()
            raise


# ---------------------------------------------------------------------- the child


class _Watching:
    """Stands in for the server's scanner: the child never scans, but its replies
    must say what the server's would."""

    running = False
    first_scan = False


def _die_with_parent() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass  # not Linux: end of input still ends the child
    parent = os.environ.get(PARENT_ENV)
    if parent is not None and parent != str(os.getppid()):
        os._exit(0)  # the server ended before the signal was armed: nobody to answer


def main(stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None) -> int:
    from capcom.cost.prices import load_prices
    from capcom.report import server
    from capcom.report.analysis import default_analysis

    _die_with_parent()
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    first = inp.readline()
    if not first:
        return 0
    spec = json.loads(first)["spec"]

    def path(p: str | None) -> Path | None:
        return None if p is None else Path(p)

    site = server.Site(
        index_path=Path(spec["index_path"]),
        tz=_tz(spec.get("tz")),
        table=load_prices(),
        analysis=default_analysis(),
        openclaw_root=path(spec["openclaw_root"]),
        state_dir=path(spec["state_dir"]),
        scanner=_Watching() if spec["watching"] else None,  # type: ignore[arg-type]
        cache=None,
        jobs=None,
        changes_path=path(spec["changes_path"]),
        day_cache=path(spec["day_cache"]),
        agent_dirs=None if spec["agent_dirs"] is None else [Path(p) for p in spec["agent_dirs"]],
    )

    def send(head: dict[str, Any], body: bytes = b"") -> None:
        out.write(json.dumps(head | {"n": len(body)}).encode() + b"\n" + body)
        out.flush()

    last = -1e9  # when the child last sent a reply so far

    def step(progress: float, partial: Callable[[], Any] | None) -> None:
        nonlocal last
        body = b""
        if partial is not None and time.monotonic() - last >= PARTIAL_EVERY:
            body = json.dumps(partial(), separators=(",", ":"), allow_nan=False).encode()
            last = time.monotonic()
        send({"t": "step", "p": progress}, body)

    for line in inp:
        req = json.loads(line)
        last = -1e9
        now = datetime.fromisoformat(req["now"])
        result = server.compute(site, req["name"], req["parts"], req["query"], now, None, step)
        head = {"t": "done", "status": int(result.status), "cacheable": result.cacheable}
        send(head, result.body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
