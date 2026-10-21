"""`capcom record-limits`: the status line passes through unchanged whatever happens,
and samples are appended only when a window moves. Payloads are synthetic."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

from capcom.limits import record as rec
from capcom.limits.model import load_samples
from capcom.types import LimitWindow

RESET_5H = 1_792_600_000
RESET_7D = 1_793_000_000


def payload(five: float | None = 12, week: float | None = 40, **extra: object) -> bytes:
    limits: dict[str, object] = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": RESET_5H}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week, "resets_at": RESET_7D}
    body = {
        "session_id": "sess-1",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "rate_limits": limits,
        "cost": {"total_cost_usd": 1.25},
        **extra,
    }
    body["workspace"] = {"current_dir": "/home/user/projé ✓"}
    return json.dumps(body, indent=2, ensure_ascii=False).encode() + b"\n\n"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache")}


def cache(env: dict[str, str]) -> Path:
    return Path(env["XDG_CACHE_HOME"]) / "capcom"


def run(data: bytes, env: dict[str, str], *argv: str) -> tuple[int, bytes]:
    out = io.BytesIO()
    code = rec.run(list(argv), stdin=io.BytesIO(data), stdout=out, env=env)
    return code, out.getvalue()


def lines(env: dict[str, str]) -> list[dict[str, object]]:
    path = cache(env) / rec.SAMPLES_FILE
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_passthrough_is_byte_identical(env: dict[str, str]) -> None:
    data = payload()
    code, out = run(data, env)
    assert code == 0 and out == data
    got = lines(env)
    assert [g["window"] for g in got] == ["five_hour", "seven_day"]
    assert got[0]["used_percentage"] == 12 and got[0]["session_id"] == "sess-1"
    assert got[0]["source"] == "statusline"
    samples = load_samples(cache(env) / rec.SAMPLES_FILE)
    assert samples[0].window == LimitWindow.FIVE_HOUR
    assert samples[0].resets_at is not None and samples[0].resets_at.timestamp() == RESET_5H


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json at all \xff\xfe",
        b'{"rate_limits": "nope"}',
        b'{"rate_limits": {"five_hour": {"used_percentage": "12"}}}',
        b"[1, 2, 3]",
        payload(five=None, week=None),
    ],
)
def test_bad_input_passes_through_and_records_nothing(data: bytes, env: dict[str, str]) -> None:
    code, out = run(data, env)
    assert code == 0 and out == data
    assert lines(env) == []


def test_unwritable_cache_still_passes_through(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(blocker)}  # a file, not a folder
    data = payload()
    code, out = run(data, env)
    assert code == 0 and out == data


def test_never_writes_under_claude(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    env = {
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(claude),
        "XDG_CACHE_HOME": str(claude / "cache"),
    }
    code, out = run(payload(), env)
    assert code == 0 and out == payload()
    assert not claude.exists()


def test_broken_stdout_and_stdin_exit_zero(env: dict[str, str]) -> None:
    class Broken(io.BytesIO):
        def write(self, b: object) -> int:
            raise BrokenPipeError

        def read(self, n: int | None = -1) -> bytes:
            raise OSError("gone")

    assert rec.run([], stdin=Broken(), stdout=io.BytesIO(), env=env) == 0
    assert rec.run([], stdin=io.BytesIO(payload()), stdout=Broken(), env=env) == 0
    assert len(lines(env)) == 2  # recorded although stdout was gone


def test_appends_only_on_change(env: dict[str, str]) -> None:
    run(payload(12, 40), env)
    run(payload(12, 40), env)  # same: nothing
    run(payload(13, 40), env)  # the 5-hour window moved
    run(payload(13, 40.0), env)
    run(payload(13, None), env)  # weekly missing: nothing for it
    got = lines(env)
    assert [(g["window"], g["used_percentage"]) for g in got] == [
        ("five_hour", 12),
        ("seven_day", 40),
        ("five_hour", 13),
    ]
    # a new reset time is a change even at the same percentage
    body = json.loads(payload(13, 40))
    body["rate_limits"]["five_hour"]["resets_at"] = RESET_5H + 5 * 3600
    run(json.dumps(body).encode(), env)
    # a reset time that jitters by a few seconds is not
    body["rate_limits"]["five_hour"]["resets_at"] = RESET_5H + 5 * 3600 + 2
    run(json.dumps(body).encode(), env)
    assert len(lines(env)) == 4


def test_line_mode_prints_its_own_line(env: dict[str, str]) -> None:
    code, out = run(payload(41, 86), env, "--line")
    assert code == 0
    text = out.decode()
    assert text.startswith("Opus 5.5 · 5h 41% (resets ") and text.endswith("· 7d 86%\n")
    assert len(lines(env)) == 2
    code, out = run(b"garbage", env, "--line")
    assert code == 0 and out == b"capcom\n"


def _worker(cache_home: str, value: int, n: int) -> None:
    env = {"HOME": cache_home, "XDG_CACHE_HOME": cache_home}
    for i in range(n):
        rec.run([], stdin=io.BytesIO(payload(value + i, 50)), stdout=io.BytesIO(), env=env)


def test_concurrent_writers(tmp_path: Path) -> None:
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(str(tmp_path), 10, 20)) for _ in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    path = tmp_path / "capcom" / rec.SAMPLES_FILE
    raw = path.read_bytes().splitlines()
    parsed = [json.loads(x) for x in raw]  # every line whole
    five = [p["used_percentage"] for p in parsed if p["window"] == "five_hour"]
    # each value at most once in a row, and the weekly value once in all
    assert all(a != b for a, b in pairwise(five))
    assert sum(1 for p in parsed if p["window"] == "seven_day") == 1
    assert set(five) <= set(range(10, 30)) and 29 in five


def test_command_line_end_to_end(env: dict[str, str]) -> None:
    data = payload(7, 8)
    full = os.environ | env
    done = subprocess.run(
        [sys.executable, "-m", "capcom.cli", "record-limits"],
        input=data,
        capture_output=True,
        env=full,
        timeout=30,
    )
    assert done.returncode == 0 and done.stdout == data and done.stderr == b""
    assert len(lines(env)) == 2
