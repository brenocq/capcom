"""Scan the logs into the index: read what each file gained since the last scan.

For each log the source discovers: stat it (and its meta.json); skip it unopened when
its inode, size, mtime and meta mtime match what the index remembers. Read it again
from 0, after dropping its rows, when its inode changed, it shrank below the stored
offset, or its first bytes changed (an in-place rewrite that kept the inode).
Otherwise read from the stored offset; when only the meta.json changed, the source is
called at the current offset so it emits the agent's facts again. Each batch is
stored with the file's new offset in one transaction, so an interrupted scan resumes
where it stopped. Files Claude Code deleted keep their rows: the index is the history.

The logs are only opened for reading: by the source, and here to hash a file's head.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import time
from pathlib import Path
from typing import TextIO

from capcom.config import Config
from capcom.store.index import FileExtra, SqliteIndex, check_index_path, open_index
from capcom.types import Batch, FileState, LogFile, ScanStats, Source

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


def _default_source() -> Source:
    # The Claude Code adapter lives in capcom.sources; it is loaded by name when a scan
    # starts, so the store neither imports nor type-checks against it at load time.
    module = importlib.import_module("capcom.sources.claude_code")
    source: Source = module.ClaudeCodeSource()
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


def scan_file(index: SqliteIndex, source: Source, log: LogFile, stats: ScanStats) -> None:
    """Bring one file's rows up to date. Updates `stats` in place."""
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
        if not reset and st.st_size == state.size and st.st_mtime_ns == state.mtime_ns:
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
    index: SqliteIndex, source: Source, config: Config, progress: TextIO | None = None
) -> ScanStats:
    """Scan every log the source finds under `config.projects_dir` into `index`. A file
    that fails is reported on `progress` and skipped; the scan goes on."""
    t0 = time.monotonic()
    stats = ScanStats()
    logs = list(source.discover(config.projects_dir))
    shown = _Progress(progress, len(logs))
    for log in logs:
        try:
            scan_file(index, source, log, stats)
        except Exception as exc:  # one bad file must not stop the scan: retried next time
            shown.error(log, exc)
        shown.update(stats)
    stats.seconds = time.monotonic() - t0
    shown.update(stats, force=True)
    return stats


def run_scan(config: Config, quiet: bool = False, source: Source | None = None) -> ScanStats:
    """Scan the logs into the index at `config.index_path`. `source` defaults to the
    Claude Code adapter; tests pass their own. An index path inside the log folders
    raises ValueError."""
    check_index_path(config.index_path, (config.claude_root, config.openclaw_root))
    src = _default_source() if source is None else source
    with open_index(config.index_path) as index:
        return scan_all(index, src, config, None if quiet else sys.stderr)
