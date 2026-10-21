"""`capcom record-limits`: record the subscription's usage windows from Claude Code's
status line.

Claude Code runs the `statusLine` command on every refresh and writes a JSON object to
its stdin; for Pro and Max plans it holds `rate_limits.five_hour` and
`rate_limits.seven_day`, each with `used_percentage` and `resets_at` (epoch seconds).
This command sits in front of the user's own status line command
(`capcom record-limits | <their command>`): it copies stdin to stdout byte for byte,
then appends one JSONL `LimitSample` per window to `<cache>/capcom/limits.jsonl` when
the window's percentage or reset time changed since its last sample. With `--line`
(no status line of one's own) it prints a short line of its own instead of the copy.

It must never break the status line: every error is swallowed, the copy still goes
out, and the exit status is 0. It is on the status line's path, so it imports only
the standard library modules it needs (no index, no prices), and holds a lock only
while appending. Concurrent sessions are serialized by `flock` on a lock file; a
writer that cannot get the lock within a few milliseconds skips this refresh (the
next one records the value).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, BinaryIO

WINDOWS = ("five_hour", "seven_day")
SAMPLES_FILE = "limits.jsonl"
STATE_FILE = "limits.state.json"
LOCK_FILE = "limits.lock"
MAX_INPUT = 1 << 20  # larger stdin is passed through but not parsed
RESET_SLACK_S = 60  # reset times closer than this are the same reset (they jitter)
LOCK_TRIES = 25  # times 2 ms: give up on the lock after about 50 ms

Reading = tuple[str, float, float | None]  # window, used_percentage, resets_at (epoch s)


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def readings(payload: Any) -> tuple[list[Reading], str | None]:
    """The windows a status line payload reports, and its session id. Every field is
    optional; a window without a numeric `used_percentage` is left out."""
    if not isinstance(payload, dict):
        return [], None
    sid = payload.get("session_id")
    session = sid if isinstance(sid, str) and 0 < len(sid) <= 128 else None
    limits = payload.get("rate_limits")
    out: list[Reading] = []
    if isinstance(limits, dict):
        for w in WINDOWS:
            win = limits.get(w)
            if not isinstance(win, dict):
                continue
            used = _number(win.get("used_percentage"))
            if used is None or not 0 <= used <= 1000:
                continue
            resets = _number(win.get("resets_at"))
            if resets is not None and resets > 1e11:  # milliseconds, by mistake
                resets /= 1000
            out.append((w, used, resets))
    return out, session


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _changed(prev: Any, used: float, resets: float | None) -> bool:
    if not isinstance(prev, list) or len(prev) != 2:
        return True
    p_used, p_resets = _number(prev[0]), _number(prev[1])
    if p_used != used:
        return True
    if (resets is None) != (p_resets is None):
        return True
    return resets is not None and p_resets is not None and abs(resets - p_resets) >= RESET_SLACK_S


def _under(path: str, roots: Sequence[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def cache_dir(env: Mapping[str, str]) -> str | None:
    """`$XDG_CACHE_HOME/capcom` (or `~/.cache/capcom`); None when that would be inside
    Claude Code's or OpenClaw's folders, which capcom never writes."""
    home = env.get("HOME") or os.path.expanduser("~")
    base = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    target = os.path.join(os.path.expanduser(base), "capcom")
    claude = env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    openclaw = env.get("OPENCLAW_STATE_DIR") or os.path.join(home, ".openclaw")
    if _under(target, [os.path.expanduser(claude), os.path.expanduser(openclaw)]):
        return None
    return target


def _lock(fd: int) -> bool:
    import fcntl

    for _ in range(LOCK_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(0.002)
    return False


def record(data: bytes, directory: str, now: float | None = None) -> int:
    """Append a sample per window of the status line payload `data` whose value
    changed since its last sample; return how many were appended. Raises on I/O
    errors (the caller swallows them)."""
    if not data or len(data) > MAX_INPUT:
        return 0
    try:
        payload = json.loads(data)
    except ValueError:
        return 0
    found, session = readings(payload)
    if not found:
        return 0
    now = time.time() if now is None else now
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock = os.open(os.path.join(directory, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _lock(lock):
            return 0
        state_path = os.path.join(directory, STATE_FILE)
        state: dict[str, Any] = {}
        try:
            with open(state_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError):
            pass
        lines: list[bytes] = []
        for window, used, resets in found:
            if not _changed(state.get(window), used, resets):
                continue
            sample = {
                "time": _iso(now),
                "window": window,
                "used_percentage": used,
                "resets_at": None if resets is None else _iso(resets),
                "session_id": session,
                "source": "statusline",
            }
            lines.append(json.dumps(sample, separators=(",", ":")).encode() + b"\n")
            state[window] = [used, resets]
        if not lines:
            return 0
        out = os.open(
            os.path.join(directory, SAMPLES_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        try:
            os.write(out, b"".join(lines))  # one write: whole lines, even unlocked
        finally:
            os.close(out)
        tmp = f"{state_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, state_path)
        return len(lines)
    finally:
        os.close(lock)  # releases the flock


def status_text(data: bytes) -> str:
    """The short line `--line` prints: the model and both windows' percentages."""
    try:
        payload = json.loads(data)
    except ValueError:
        return "capcom"
    found, _ = readings(payload)
    parts: list[str] = []
    model = payload.get("model") if isinstance(payload, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name:
        parts.append(name[:40])
    labels = {"five_hour": "5h", "seven_day": "7d"}
    for window, used, resets in found:
        text = f"{labels[window]} {used:.0f}%"
        if resets is not None and window == "five_hour":
            text += f" (resets {datetime.fromtimestamp(resets).strftime('%H:%M')})"
        parts.append(text)
    return " · ".join(parts) or "capcom"


def run(
    argv: Sequence[str],
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """The command: always exits 0, and stdout always gets the copy of stdin (or the
    short line with `--line`) whatever happens to the recording."""
    real_stdout = stdout is None
    data = b""
    with contextlib.suppress(BaseException):
        data = (stdin or sys.stdin.buffer).read()
    with contextlib.suppress(BaseException):
        out = stdout or sys.stdout.buffer
        if "--line" in argv:
            out.write(status_text(data).encode("utf-8", "replace") + b"\n")
        else:
            out.write(data)
        out.flush()
        if real_stdout:
            # let the next command in the pipe see the end of its input now
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
    with contextlib.suppress(BaseException):
        directory = cache_dir(os.environ if env is None else env)
        if directory is not None:
            record(data, directory)
    return 0
