"""Performance check of the store: generate a synthetic log set, scan it, report.

    uv run python -m tests.store.perf --mb 400 --dir ~/.cache/capcom-perf

Generates (once, reused while the size matches) a projects folder of synthetic
JSON-lines logs in the fake source's format (tests/store/fake_source.py), with line
sizes and a record mix close to Claude Code's: replies written two or three times
while streaming, events, and large lines the source skips (prompts, tool results).
Then it times a first scan into a fresh index, a rescan with nothing new, a rescan
after appending to a few files, and a full read of the requests, and prints rows per
second and the peak memory of the process. Run it under the memory cap
(AGENTS.md, "Heavy jobs"). Keep `--dir` off tmpfs: files there count as memory.
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from capcom.config import Config
from capcom.store.index import open_index
from capcom.store.scan import scan_all
from tests.store.fake_source import FakeSource

_KINDS = ("tool_result", "spawn", "turn", "tool_set", "api_request", "compaction")
_TOOLS = ("Read", "Bash", "Edit", "Grep", "Agent", "WebFetch")


def _pad(rng: random.Random, n: int) -> str:
    return "x" * max(0, int(rng.expovariate(1 / n)))


def _write_session(f: TextIO, rng: random.Random, session: str, target: int) -> int:
    """Write one log of about `target` bytes; returns its size."""
    size = 0

    def put(rec: dict[str, Any]) -> None:
        nonlocal size
        line = json.dumps(rec) + "\n"
        f.write(line)
        size += len(line)

    t = rng.uniform(0, 3e7)
    n = 0
    put({"t": "ses", "s": session, "title": "synthetic", "first": t})
    while size < target:
        n += 1
        t += rng.expovariate(1 / 20)
        mid = f"{session}-m{n}"
        tools = [[f"{mid}-tu", rng.choice(_TOOLS), "/home/u/proj/file.py"]]
        for i in range(rng.choice((1, 2, 3))):  # streaming: one id, several records
            rec = {
                "t": "req",
                "id": mid,
                "s": session,
                "ts": t,
                "in": 3,
                "cr": rng.randrange(10_000, 200_000),
                "cw5": rng.randrange(0, 5_000),
                "out": 4 if i == 0 else rng.randrange(10, 3_000),
                "tools": tools,
                "pad": _pad(rng, 1_200),
            }
            put(rec)
        kind = rng.choice(_KINDS)
        attrs = {"tool": rng.choice(_TOOLS), "chars": rng.randrange(100, 50_000), "size_tokens": 9}
        put({"t": "ev", "kind": kind, "s": session, "ts": t + 1, "attrs": attrs})
        put({"t": "user", "pad": _pad(rng, 4_000)})  # skipped by the source
        if n % 25 == 0:
            put({"t": "cost", "s": session, "usd": n * 0.01})
    return size


def generate(projects: Path, mb: int, seed: int = 7) -> int:
    """Write about `mb` megabytes of logs under `projects`; returns the file count."""
    rng = random.Random(seed)
    target = mb * 1_000_000
    written = 0
    files = 0
    while written < target:
        project = projects / f"-home-u-proj{files % 40}"
        session = f"s{files:05d}"
        size = min(int(rng.lognormvariate(13, 1.3)), 40_000_000, target - written + 1)
        path = project / f"{session}.jsonl"
        if rng.random() < 0.4:  # a subagent log instead of a session log
            path = project / session / "subagents" / f"agent-a{files:05d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            written += _write_session(f, rng, session, max(size, 2_000))
        files += 1
    return files


def _peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


@dataclass
class Result:
    files: int
    mb: float
    records: int
    first_s: float
    rescan_s: float
    append_s: float
    append_records: int
    read_rows: int
    read_s: float
    index_mb: float
    peak_mb: float

    def lines(self) -> list[str]:
        return [
            f"logs: {self.files} files, {self.mb:.0f} MB",
            f"first scan: {self.first_s:.1f} s, {self.mb / self.first_s:.0f} MB/s, "
            f"{self.records / self.first_s:,.0f} records/s ({self.records:,} records)",
            f"rescan, nothing new: {self.rescan_s:.2f} s",
            f"rescan after appends: {self.append_s:.2f} s ({self.append_records} records)",
            f"read all requests: {self.read_rows:,} rows in {self.read_s:.1f} s, "
            f"{self.read_rows / max(self.read_s, 1e-9):,.0f} rows/s",
            f"index: {self.index_mb:.0f} MB; peak memory (RSS): {self.peak_mb:.0f} MB",
        ]


def run(base: Path, mb: int) -> Result:
    claude = base / "claude"
    projects = claude / "projects"
    stamp = base / "generated-mb"
    if not (stamp.is_file() and stamp.read_text() == str(mb)):
        shutil.rmtree(claude, ignore_errors=True)
        generate(projects, mb)
        stamp.write_text(str(mb))
    cache = base / "cache"
    shutil.rmtree(cache, ignore_errors=True)
    config = Config(claude, base / "openclaw", cache, base / "state")
    source = FakeSource(batch_lines=2_000)

    with open_index(config.index_path) as ix:
        first = scan_all(ix, source, config)
        rescan = scan_all(ix, source, config)
        logs = sorted(projects.rglob("*.jsonl"))[::50]
        for i, log in enumerate(logs):
            with log.open("a") as f:
                f.write(json.dumps({"t": "req", "id": f"appended-{i}", "ts": 1}) + "\n")
        append = scan_all(ix, source, config)
        t0 = time.monotonic()
        rows = sum(1 for _ in ix.requests())
        read_s = time.monotonic() - t0
    return Result(
        files=first.files_seen,
        mb=first.bytes_read / 1e6,
        records=first.records,
        first_s=first.seconds,
        rescan_s=rescan.seconds,
        append_s=append.seconds,
        append_records=append.records,
        read_rows=rows,
        read_s=read_s,
        index_mb=config.index_path.stat().st_size / 1e6,
        peak_mb=_peak_mb(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mb", type=int, default=300)
    parser.add_argument("--dir", type=Path, default=Path("~/.cache/capcom-perf"))
    args = parser.parse_args()
    for line in run(args.dir.expanduser(), args.mb).lines():
        print(line)


if __name__ == "__main__":
    main()
