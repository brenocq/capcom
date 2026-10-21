"""Scan the logs into the index: read what each file gained since the last scan.

For each log the source discovers: stat it (and its meta.json); skip it unopened when
its inode, size, mtime and meta mtime match what the index remembers. Read it again
from 0, after dropping its rows, when its inode changed, it shrank below the stored
offset, or its first bytes changed (an in-place rewrite that kept the inode).
Otherwise read from the stored offset (a file whose offset was set back to 0 is read
again even with an unchanged stat); when only the meta.json changed, the source is
called at the current offset so it emits the agent's facts again. Each batch is
stored with the file's new offset in one transaction, so an interrupted scan resumes
where it stopped. Files Claude Code deleted keep their rows: the index is the history.

OpenClaw's database (`LogKind.OPENCLAW_DB`) changes in place, and in WAL mode its
size and mtime say nothing about new rows: it is read on every scan from the stored
rowid, with no head hash and no size checks. When its highest rowid is below the
cursor (the table was rebuilt) or its inode changed, it is read again from 0 without
dropping its rows: each reply's id is unique, so rows upsert, and replies OpenClaw
pruned stay.

`run_scan` scans the Claude Code projects folder, then the OpenClaw state dir.
The logs are only opened for reading: by the sources, and here to hash a file's head.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from capcom.config import Config
from capcom.store.index import FileExtra, SqliteIndex, check_index_path, open_index
from capcom.types import Batch, FileState, LogFile, LogKind, ScanStats, Source

HEAD_BYTES = 4096  # the head hashed to catch in-place rewrites
_PROGRESS_EVERY_S = 0.25


class _Progress:
    """One line on stderr, rewritten in place on a terminal, printed once otherwise."""

    def __init__(self, out: TextIO | None, total: int) -> None:
        self.out = out
        self.total = total
        self.live = out is not None and out.isatty()
        self.last = 0.0
        self.failed = 0

    def update(self, stats: ScanStats, force: bool = False) -> None:
        if self.out is None:
            return
        now = time.monotonic()
        if not force and (not self.live or now - self.last < _PROGRESS_EVERY_S):
            return
        self.last = now
        line = (
            f"capcom scan: {stats.files_seen}/{self.total} files, "
            f"{stats.bytes_read / 1e6:.1f} MB, {stats.records} records"
        )
        if self.failed:
            line += f", {self.failed} failed"
        self.out.write(("\r" + line) if self.live else line)
        if force:
            self.out.write("\n")
        self.out.flush()

    def error(self, log: LogFile, exc: Exception) -> None:
        self.failed += 1
        if self.out is not None:
            lead = "\n" if self.live else ""
            what = f"{type(exc).__name__}: {exc}"
            self.out.write(f"{lead}capcom scan: failed on {log.path}: {what}\n")


@runtime_checkable
class RowSource(Protocol):
    """A source of databases read by rowid (LogKind.OPENCLAW_DB)."""

    def last_row(self, log: LogFile) -> int | None:
        """The highest rowid now, or None when the database cannot be read."""
        ...


def _default_source() -> Source:
    # The adapters live in capcom.sources; they are loaded by name when a scan starts,
    # so the store neither imports nor type-checks against them at load time.
    module = importlib.import_module("capcom.sources.claude_code")
    source: Source = module.ClaudeCodeSource()
    return source


def _openclaw_source() -> Source:
    module = importlib.import_module("capcom.sources.openclaw")
    source: Source = module.OpenClawSource()
    return source


def _mtime_ns(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _head_hash(path: Path, length: int) -> str | None:
    """Hash of the first `length` bytes, or None when the file is shorter."""
    with path.open("rb") as f:
        data = f.read(length)
    if len(data) < length:
        return None
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def scan_database(index: SqliteIndex, source: Source, log: LogFile, stats: ScanStats) -> None:
    """Bring a database read by rowid up to date (FileState.offset is the last rowid
    read). Updates `stats` in place."""
    try:
        st = log.path.stat()
    except FileNotFoundError:
        return
    stats.files_seen += 1
    state = index.file_state(log.path)
    start = 0 if state is None else state.offset
    if start > 0 and state is not None:
        last = source.last_row(log) if isinstance(source, RowSource) else None
        if st.st_ino != state.inode or (last is not None and last < start):
            start = 0  # replaced or rebuilt: read it all again; rows upsert by id
            stats.files_reset += 1
    cursor = start
    for batch in source.parse(log, start):
        cursor = batch.end_offset
        index.apply(log, batch, FileState(log.path, st.st_ino, st.st_size, st.st_mtime_ns, cursor))
        stats.lines += batch.lines
        stats.records += len(batch.records)
        stats.skipped += batch.skipped
    if state is None or cursor == start:
        # Nothing new: remember the stat (and a reset cursor) all the same.
        index.apply(
            log,
            Batch([], cursor),
            FileState(log.path, st.st_ino, st.st_size, st.st_mtime_ns, cursor),
        )
    if cursor > start:
        stats.files_read += 1


def scan_file(index: SqliteIndex, source: Source, log: LogFile, stats: ScanStats) -> None:
    """Bring one file's rows up to date. Updates `stats` in place."""
    if log.kind == LogKind.OPENCLAW_DB:
        scan_database(index, source, log, stats)
        return
    try:
        st = log.path.stat()
    except FileNotFoundError:
        return  # removed between discovery and now
    stats.files_seen += 1
    meta_mtime = _mtime_ns(log.meta_path)
    state = index.file_state(log.path)
    extra = index.file_extra(log.path) if state is not None else None
    start = 0
    if state is not None and extra is not None:
        reset = st.st_ino != state.inode or st.st_size < state.offset
        # An offset of 0 on a non-empty file means "read it again" (a schema migration
        # queues files that way), even when its stat is unchanged.
        queued = state.offset == 0 and st.st_size > 0
        unchanged = st.st_size == state.size and st.st_mtime_ns == state.mtime_ns
        if not reset and not queued and unchanged:
            if meta_mtime == extra.meta_mtime_ns:
                return  # nothing changed
        elif not reset and extra.head_len:
            reset = _head_hash(log.path, extra.head_len) != extra.head_hash
        if reset:
            index.forget_file(log.path)
            stats.files_reset += 1
            extra = None
        else:
            start = state.offset

    offset = start
    applied = False
    for batch in source.parse(log, start):
        offset = batch.end_offset
        size = max(st.st_size, offset)  # the file may have grown while being read
        index.apply(log, batch, FileState(log.path, st.st_ino, size, st.st_mtime_ns, offset))
        applied = True
        stats.lines += batch.lines
        stats.records += len(batch.records)
        stats.skipped += batch.skipped
    if not applied:
        # Nothing complete to read (a partial line, a touch): remember the new stat so
        # the next scan skips the file until it changes again.
        state = FileState(log.path, st.st_ino, st.st_size, st.st_mtime_ns, start)
        index.apply(log, Batch([], start), state)

    # The head hashed is the first bytes already consumed (complete lines never change
    # in an append-only log), up to HEAD_BYTES; once full it is kept as it is.
    head_len = extra.head_len if extra is not None else 0
    head_hash = extra.head_hash if extra is not None else None
    if head_len < HEAD_BYTES and min(offset, HEAD_BYTES) > head_len:
        head_len = min(offset, HEAD_BYTES)
        head_hash = _head_hash(log.path, head_len)
        if head_hash is None:
            head_len = 0
    index.set_file_extra(log.path, FileExtra(head_len, head_hash, meta_mtime))

    if offset > start:
        stats.files_read += 1
        stats.bytes_read += offset - start


def scan_all(
    index: SqliteIndex,
    source: Source,
    config: Config,
    progress: TextIO | None = None,
    extra: Sequence[tuple[Source, Path]] = (),
) -> ScanStats:
    """Scan every log the source finds under `config.projects_dir`, then every log each
    `extra` source finds under its root, into `index`. A file that fails is reported
    on `progress` and skipped; the scan goes on."""
    t0 = time.monotonic()
    stats = ScanStats()
    logs = [(source, log) for log in source.discover(config.projects_dir)]
    for other, root in extra:
        logs += [(other, log) for log in other.discover(root)]
    shown = _Progress(progress, len(logs))
    for src, log in logs:
        try:
            scan_file(index, src, log, stats)
        except Exception as exc:  # one bad file must not stop the scan: retried next time
            shown.error(log, exc)
            stats.files_failed += 1
        shown.update(stats)
    stats.seconds = time.monotonic() - t0
    shown.update(stats, force=True)
    for other, _ in extra:
        unread = getattr(other, "compressed_unread", 0)
        if unread and progress is not None:
            progress.write(
                f"capcom scan: {unread} compressed OpenClaw rows left unread "
                "(reading them needs Python 3.14's compression.zstd)\n"
            )
    return stats


def run_scan(config: Config, quiet: bool = False, source: Source | None = None) -> ScanStats:
    """Scan the logs into the index at `config.index_path`: Claude Code's projects
    folder, then OpenClaw's databases under `config.openclaw_root`. A `source` (tests
    pass their own) replaces both. An index path inside the log folders raises
    ValueError."""
    check_index_path(config.index_path, (config.claude_root, config.openclaw_root))
    extra: list[tuple[Source, Path]] = []
    if source is None:
        src = _default_source()
        extra.append((_openclaw_source(), config.openclaw_root))
    else:
        src = source
    with open_index(config.index_path) as index:
        return scan_all(index, src, config, None if quiet else sys.stderr, extra)
