"""A small run of the performance check (tests/store/perf.py), so it keeps working.

The real measurement is `python -m tests.store.perf --mb 400` under the memory cap.
"""

from __future__ import annotations

from pathlib import Path

from tests.store.perf import run


def test_perf_harness(tmp_path: Path) -> None:
    result = run(tmp_path, mb=3)
    print("\n" + "\n".join(result.lines()))
    assert result.files > 1 and result.records > 1_000
    assert result.append_records > 0
    assert result.rescan_s < result.first_s
    assert result.read_rows > 0
