"""Scan the logs into the index: read what each file gained since the last scan.

For each log the source discovers: stat it; skip it unopened when its inode, size and
mtime match what the index remembers; read it again from 0 when its inode changed or
it shrank below the stored offset (after dropping its rows); otherwise read from the
stored offset. Each batch is stored with the file's new offset in one transaction, so
an interrupted scan resumes where it stopped. Files Claude Code deleted keep their
rows: the index is the history.

The logs are only ever opened for reading, by the source.
"""

from __future__ import annotations

import importlib
import sys
import time
from typing import TextIO

from capcom.config import Config
from capcom.store.index import open_index
from capcom.types import Batch, FileState, Index, LogFile, ScanStats, Source

_PROGRESS_EVERY_S = 0.25


class _Progress:
    """One line on stderr, rewritten in place on a terminal, printed once otherwise."""

    def __init__(self, out: TextIO | None, total: int) -> None:
        self.out = out
        self.total = total
        self.live = out is not None and out.isatty()
        self.last = 0.0

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
        self.out.write(("\r" + line) if self.live else line)
        if force:
            self.out.write("\n")
        self.out.flush()


def _default_source() -> Source:
    # The Claude Code adapter lives in capcom.sources; it is loaded by name when a scan
    # starts, so the store neither imports nor type-checks against it at load time.
    module = importlib.import_module("capcom.sources.claude_code")
    source: Source = module.ClaudeCodeSource()
    return source


def scan_file(index: Index, source: Source, log: LogFile, stats: ScanStats) -> None:
    """Bring one file's rows up to date. Updates `stats` in place."""
    try:
        st = log.path.stat()
    except FileNotFoundError:
        return  # removed between discovery and now
    stats.files_seen += 1
    state = index.file_state(log.path)
    start = 0
    if state is not None:
        if st.st_ino != state.inode or st.st_size < state.offset:
            index.forget_file(log.path)
            stats.files_reset += 1
        elif st.st_size == state.size and st.st_mtime_ns == state.mtime_ns:
            return
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
        index.apply(
            log, Batch([], start), FileState(log.path, st.st_ino, st.st_size, st.st_mtime_ns, start)
        )
    if offset > start:
        stats.files_read += 1
        stats.bytes_read += offset - start


def scan_all(
    index: Index, source: Source, config: Config, progress: TextIO | None = None
) -> ScanStats:
    """Scan every log the source finds under `config.projects_dir` into `index`."""
    t0 = time.monotonic()
    stats = ScanStats()
    logs = list(source.discover(config.projects_dir))
    shown = _Progress(progress, len(logs))
    for log in logs:
        try:
            scan_file(index, source, log, stats)
        except OSError as exc:  # unreadable or vanished mid-read: try again next scan
            if progress is not None:
                progress.write(f"\ncapcom scan: skipped {log.path}: {exc.strerror or exc}\n")
        shown.update(stats)
    stats.seconds = time.monotonic() - t0
    shown.update(stats, force=True)
    return stats


def run_scan(config: Config, quiet: bool = False, source: Source | None = None) -> ScanStats:
    """Scan the logs into the index at `config.index_path`. `source` defaults to the
    Claude Code adapter; tests pass their own."""
    src = _default_source() if source is None else source
    with open_index(config.index_path) as index:
        return scan_all(index, src, config, None if quiet else sys.stderr)
