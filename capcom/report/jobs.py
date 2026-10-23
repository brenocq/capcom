"""Background jobs for `capcom serve`: slow API replies computed off the request
thread, with progress and partial replies, and a warmer that computes the pages'
usual views ahead of the clicks.

A reply that is not cached becomes a `Job`, keyed like the cache (so two requests
for the same view share one). The endpoint calls `Job.step(progress, partial)`
after each chunk of work (through `api.Context.progress`); `step` records the
progress, builds the reply so far now and then, and is also where a job is
stopped: when the index changed under it (its generation is no longer current),
when it ran past its time limit, or when it was cancelled. It is also where a
background job waits: the warmer's jobs, prefetches, and jobs no page has polled
for a while pause there while a page's job runs or a page's request is in flight
(and for `quiet` seconds after), so a click never queues behind background work.
The warmer's own jobs compute in a separate process (`capcom.report.worker`) that
is stopped outright while a page needs the CPU.

Threads, not processes: the work is SQLite reads and Python loops, and at most
`max_running` jobs compute at once (each holds about one window of requests in
memory), so the server stays under its memory cap.
"""

from __future__ import annotations

import itertools
import json
import secrets
import sys
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any

JOB_TIMEOUT = 120.0  # seconds a job may compute
ABANDONED = 2.5  # seconds without a poll after which a page's job runs as background
QUIET = 0.5  # seconds after a page's last request before background work goes on
PARTIAL_EVERY = 0.25  # seconds between two builds of the reply so far
KEEP_DONE = 60.0  # seconds a finished job's reply stays for its pollers
MAX_RUNNING = 3  # jobs computing at once (memory: each holds a window of requests)


class Cancelled(Exception):
    """Raised inside a job's work to stop it: `reason` is "stale", "timeout" or
    "cancelled"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class Result:
    status: int
    body: bytes
    cacheable: bool = True


Work = Callable[["Job"], Result]


class Job:
    """One reply being computed. Thread-safe; `step` runs on the job's thread."""

    def __init__(self, jobs: Jobs, key: Hashable, gen: Hashable, background: bool) -> None:
        self.jobs = jobs
        self.key = key
        self.gen = gen
        self.id = secrets.token_hex(6)
        self.background = background
        now = jobs.clock()
        self.created = now
        self.started: float | None = None
        self.last_poll = now
        self.finished: float | None = None
        self.result: Result | None = None
        self.cancelled: str | None = None
        self._progress = 0.0
        self._shown = 0.0  # the largest progress reported to a poller (never goes back)
        self.partial: bytes | None = None  # the encoded reply so far
        self._partial_at = -1e9
        self.done = threading.Event()
        self.expected = 0.0  # seconds this kind of reply took last time (0: unknown)
        self.paused = 0.0  # seconds spent paused (not counted in its duration)
        self.version = 0  # bumped on each new partial reply and at the end
        self._news = threading.Condition()

    # ------------------------------------------------------------- the job's side

    def step(self, progress: float, partial: Callable[[], Any] | None = None) -> None:
        """After a chunk of work: record progress, maybe publish the reply so far,
        stop if the job must stop, and wait while it must yield."""
        self._check()
        self._progress = max(self._progress, min(0.99, progress))
        now = self.jobs.clock()
        if partial is not None and now - self._partial_at >= self.jobs.partial_every:
            body = json.dumps(partial(), separators=(",", ":"), allow_nan=False).encode()
            self.publish(progress, body)
        t0 = self.jobs.clock()
        self.jobs.wait_turn(self)
        self.paused += self.jobs.clock() - t0
        self._check()

    def publish(self, progress: float, partial: bytes | None = None) -> None:
        """Record progress and, when given, the encoded reply so far (the step of a
        job computed elsewhere: the worker process)."""
        self._progress = max(self._progress, min(0.99, progress))
        if partial is not None:
            self.partial = partial
            self._partial_at = self.jobs.clock()
            self._bump()

    def _bump(self) -> None:
        with self._news:
            self.version += 1
            self._news.notify_all()

    def wait_news(self, version: int, timeout: float) -> None:
        """Wait until the job ends or has a newer partial reply than `version`, at
        most `timeout` seconds (a poll's long wait)."""
        with self._news:
            self._news.wait_for(lambda: self.version != version or self.done.is_set(), timeout)

    def _check(self) -> None:
        if self.cancelled is not None:
            raise Cancelled(self.cancelled)
        if self.jobs.current_gen is not None and self.gen != self.jobs.current_gen and self.low:
            self.cancelled = "stale"  # a page's job ends its reply, as the click saw the data
        elif self.started is not None and self.jobs.clock() - self.started > self.jobs.timeout:
            self.cancelled = "timeout"
        if self.cancelled is not None:
            raise Cancelled(self.cancelled)

    # ----------------------------------------------------------- the poller's side

    def poll(self) -> None:
        """A page asked about this job: it is wanted, so it runs at a page's priority."""
        self.last_poll = self.jobs.clock()
        self.background = False

    def leave(self) -> None:
        """The page following this job went away (its poll's connection closed: a
        click elsewhere): it runs as background work from now on, so it yields to
        the page clicked, until a page polls it again."""
        self.last_poll = min(self.last_poll, self.jobs.clock() - self.jobs.abandoned - 1)

    @property
    def low(self) -> bool:
        """Runs as background work: the warmer's, or one no page polled lately."""
        return self.background or self.jobs.clock() - self.last_poll > self.jobs.abandoned

    def progress(self) -> float:
        """0..1 for a poller, never less than the last one given: the job's own
        progress, or, before its first step and between steps, an estimate from
        how long this reply took last time."""
        p = self._progress
        if self.started is not None and self.expected > 0:
            p = max(p, min(0.9, 0.9 * (self.jobs.clock() - self.started) / self.expected))
        if self.result is not None:
            p = 1.0
        self._shown = max(self._shown, round(p, 3))
        return self._shown

    def timed_out(self) -> bool:
        started = self.started if self.started is not None else self.created
        return self.result is None and self.jobs.clock() - started > self.jobs.timeout


class Jobs:
    """The running and recently finished jobs, by key and by id."""

    def __init__(
        self,
        max_running: int = MAX_RUNNING,
        timeout: float = JOB_TIMEOUT,
        abandoned: float = ABANDONED,
        partial_every: float = PARTIAL_EVERY,
        keep_done: float = KEEP_DONE,
        clock: Callable[[], float] = time.monotonic,
        quiet: float = QUIET,
    ) -> None:
        self.timeout = timeout
        self.abandoned = abandoned
        self.quiet = quiet
        self.partial_every = partial_every
        self.keep_done = keep_done
        self.clock = clock
        self.current_gen: Hashable | None = None
        self._by_key: dict[Hashable, Job] = {}
        self._by_id: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._turn = threading.Condition(self._lock)
        self._slots = threading.BoundedSemaphore(max_running)
        self._durations: dict[Hashable, float] = {}  # kind -> seconds, last run
        self.peak_running = 0
        self._running = 0
        self._max = max_running
        self._users = 0  # pages' requests in flight
        self._last_user = -1e9  # when the last one ended

    def user_begin(self) -> None:
        """A page's request came in: background work pauses until it ends."""
        with self._lock:
            self._users += 1

    def user_end(self) -> None:
        with self._lock:
            self._users -= 1
            self._last_user = self.clock()
            self._turn.notify_all()

    def must_yield(self, job: Job) -> bool:
        """Whether background `job` must wait now: a page's request is in flight or
        ended less than `quiet` seconds ago, or a page's job runs or waits."""
        with self._lock:
            return self._yield(job)

    def _yield(self, job: Job) -> bool:
        if not job.low:
            return False
        if self._users > 0 or self.clock() - self._last_user < self.quiet:
            return True
        return self._pages(job, started=False) or self._pages(job, started=True)

    def set_gen(self, gen: Hashable) -> None:
        """The index's current generation: jobs of another one are forgotten (a new
        request starts a new one); background ones stop at their next step, while a
        page's job finishes the reply its page is polling for, so a scan in the
        middle of a click never makes the click start over."""
        with self._lock:
            if gen == self.current_gen:
                return
            self.current_gen = gen
            for key, job in list(self._by_key.items()):
                if job.gen != gen:
                    if job.result is None and job.low:
                        job.cancelled = job.cancelled or "stale"
                    del self._by_key[key]
            self._turn.notify_all()

    def find(self, key: Hashable) -> Job | None:
        with self._lock:
            self._expire()
            job = self._by_key.get(key)
            return job if job is not None and job.cancelled is None else None

    def by_id(self, job_id: str) -> Job | None:
        with self._lock:
            self._expire()
            return self._by_id.get(job_id)

    def submit(
        self,
        key: Hashable,
        gen: Hashable,
        work: Work,
        kind: Hashable = None,
        background: bool = False,
    ) -> Job:
        """The job for `key`: the running one when there is one (a page's request
        lifts a background job to its own priority), else a new one, started."""
        with self._lock:
            self._expire()
            job = self._by_key.get(key)
            if job is not None and job.cancelled is None:
                if not background:
                    job.poll()
                    self._turn.notify_all()
                return job
            job = Job(self, key, gen, background)
            job.expected = self._durations.get(kind, 0.0)
            self._by_key[key] = job
            self._by_id[job.id] = job
        thread = threading.Thread(
            target=self._run, args=(job, work, kind), name="capcom-job", daemon=True
        )
        thread.start()
        return job

    def _run(self, job: Job, work: Work, kind: Hashable) -> None:
        try:
            # a page's job takes a free slot at once; a background one waits its turn
            self.wait_turn(job)
            with self._slots:
                with self._lock:
                    self._running += 1
                    self.peak_running = max(self.peak_running, self._running)
                try:
                    job.started = self.clock()
                    job._check()
                    result = work(job)
                    with self._lock:
                        self._durations[kind] = self.clock() - job.started - job.paused
                    job.result = result
                finally:
                    with self._lock:
                        self._running -= 1
                        self._turn.notify_all()
        except Cancelled as err:
            job.cancelled = err.reason
        except Exception:  # the work reports the errors it expects as a Result
            # the details go to the terminal only; the reply says nothing about them
            print("capcom serve: error while answering a request:", file=sys.stderr)
            traceback.print_exc()
            job.result = Result(500, b"internal error", cacheable=False)
        finally:
            job.finished = self.clock()
            with self._lock:
                if job.cancelled is not None and self._by_key.get(job.key) is job:
                    del self._by_key[job.key]
                self._turn.notify_all()
            job.done.set()
            job._bump()

    def wait_turn(self, job: Job) -> None:
        """Block a background job while a page's request or job is in flight (and
        `quiet` seconds after). A background job that holds the last slot while a
        page's job waits for one is cancelled instead (pausing it would keep the
        page waiting for good)."""
        with self._lock:
            while job.cancelled is None and not job.timed_out() and self._yield(job):
                if (
                    job.started is not None
                    and self._running >= self._max
                    and self._pages(job, started=False)
                ):
                    job.cancelled = "cancelled"
                    break
                self._turn.wait(0.05)

    def _pages(self, job: Job, started: bool) -> bool:
        """Whether a page's job other than `job` is running (`started`) or waiting."""
        return any(
            j is not job
            and j.result is None
            and j.cancelled is None
            and not j.low
            and (j.started is not None) == started
            for j in self._by_key.values()
        )

    def _expire(self) -> None:
        now = self.clock()
        for job_id, job in list(self._by_id.items()):
            if job.finished is not None and now - job.finished > self.keep_done:
                del self._by_id[job_id]
                if self._by_key.get(job.key) is job:
                    del self._by_key[job.key]

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for j in self._by_key.values() if j.result is None)


def computing_body(job: Job, partial: bytes | None, source: str | None) -> bytes:
    """The 202 reply: the job's id and progress, and the reply so far when there is
    one (`source` says what it is: "chunks" for this job's own partial reply,
    "stale" for the reply of an earlier index generation)."""
    head = {"status": "computing", "job": job.id, "progress": job.progress(), "source": source}
    text = json.dumps(head, separators=(",", ":"))
    return (text[:-1] + ',"partial":').encode() + (partial or b"null") + b"}"


def interleave[T](first: Iterable[T], rest: Iterable[T]) -> list[T]:
    """`first`, then the items of `rest` not in it, in order, each once."""
    seen: set[Any] = set()
    out: list[T] = []
    for item in itertools.chain(first, rest):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
